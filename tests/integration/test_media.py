"""Integration tests for substep media upload/attach/annotation (P2-06) —
DD §7.2, C21.

Same zero-network sqlite pattern as tests/integration/test_library_ui.py.
The artifact dir is overridden per-test via `monkeypatch` + `dataclasses.
replace` on the shared `config` singleton, so uploads land under `tmp_path`
instead of the real ARTIFACT_DIR.
"""
from __future__ import annotations

import dataclasses
import io
import re
import uuid

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool
from starlette.testclient import TestClient

from app.api import media as media_module
from app.config import config
from app.db import get_session
from app.domain.models_jb2 import Base
from app.domain.models_library import Substep
from app.main import app  # noqa: F401 -- registers library/media tables + routes

PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\xcf\xc0"
    b"\x00\x00\x03\x01\x01\x00\x18\xdd\x8d\xb0\x00\x00\x00\x00IEND\xaeB`\x82"
)


@pytest.fixture
def engine():
    eng = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture
def db_session(engine):
    with Session(engine) as s:
        yield s


@pytest.fixture
def client(engine, tmp_path, monkeypatch):
    def _override():
        session = Session(engine)
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_session] = _override
    monkeypatch.setattr(
        media_module, "config", dataclasses.replace(config, artifact_dir=str(tmp_path))
    )
    with TestClient(app, follow_redirects=True) as c:
        yield c
    del app.dependency_overrides[get_session]


def _create_set(client, title="Apollo Op40 Assembly"):
    resp = client.post(
        "/admin/library",
        data={
            "title": title,
            "product_id": "",
            "operation_match": '{"op_codes": ["OP40"], "work_centers": []}',
            "who": "alice",
        },
    )
    assert resp.status_code == 200
    m = re.search(r"/admin/library/sets/([0-9a-f-]{36})", str(resp.url))
    assert m, resp.url
    return uuid.UUID(m.group(1))


def _add_substep(client, set_id, db_session) -> uuid.UUID:
    from app.domain.models_library import Step

    resp = client.post(
        f"/admin/library/sets/{set_id}/steps",
        data={"title": "Step 1", "who": "alice"},
    )
    assert resp.status_code == 200
    step = db_session.scalars(select(Step).where(Step.instruction_set_id == set_id)).first()
    resp = client.post(
        f"/admin/library/steps/{step.id}/substeps",
        data={"type": "photo", "title": "Photo of slide", "who": "alice"},
    )
    assert resp.status_code == 200
    sub = db_session.scalars(select(Substep).where(Substep.step_id == step.id)).first()
    return sub.id


def test_upload_roundtrip(client, tmp_path):
    resp = client.post(
        "/admin/media",
        files={"file": ("photo.png", io.BytesIO(PNG_BYTES), "image/png")},
    )
    assert resp.status_code == 200, resp.text
    url = resp.json()["url"]
    assert url.startswith("/artifacts/library/")
    assert (tmp_path / "library").is_dir()

    get_resp = client.get(url)
    assert get_resp.status_code == 200
    assert get_resp.content == PNG_BYTES


def test_upload_bad_content_type_415(client):
    resp = client.post(
        "/admin/media",
        files={"file": ("evil.txt", io.BytesIO(b"not an image"), "text/plain")},
    )
    assert resp.status_code == 415


def test_upload_oversize_413(client):
    big = b"\x00" * (8 * 1024 * 1024 + 1)
    resp = client.post(
        "/admin/media",
        files={"file": ("big.png", io.BytesIO(big), "image/png")},
    )
    assert resp.status_code == 413


def test_traversal_attempt_404(client):
    resp = client.get("/artifacts/library/..%2F..%2Fconfig.py")
    assert resp.status_code == 404


def test_attach_and_remove_media_on_draft_substep(client, db_session):
    set_id = _create_set(client)
    substep_id = _add_substep(client, set_id, db_session)

    upload = client.post(
        "/admin/media", files={"file": ("photo.png", io.BytesIO(PNG_BYTES), "image/png")}
    )
    url = upload.json()["url"]

    media_payload = f'[{{"kind": "photo", "url": "{url}", "caption": "slide face"}}]'
    resp = client.post(
        f"/admin/library/substeps/{substep_id}/media",
        data={"media": media_payload, "who": "alice"},
    )
    assert resp.status_code == 200

    db_session.expire_all()
    sub = db_session.get(Substep, substep_id)
    assert sub.media == [{"kind": "photo", "url": url, "caption": "slide face"}]

    resp = client.post(
        f"/admin/library/substeps/{substep_id}/media",
        data={"media": "[]", "who": "alice"},
    )
    assert resp.status_code == 200
    db_session.expire_all()
    sub = db_session.get(Substep, substep_id)
    assert sub.media == []


def test_attach_media_on_published_set_rejected(client, db_session):
    set_id = _create_set(client)
    substep_id = _add_substep(client, set_id, db_session)

    client.post(f"/admin/library/sets/{set_id}/submit", data={"who": "alice"})
    client.post(f"/admin/library/sets/{set_id}/publish", data={"who": "alice"})

    resp = client.post(
        f"/admin/library/substeps/{substep_id}/media",
        data={"media": '[{"kind": "photo", "url": "/artifacts/library/x.png"}]', "who": "alice"},
    )
    assert resp.status_code == 200  # 303 -> ?error= page, followed
    assert "error=" in str(resp.url)

    db_session.expire_all()
    sub = db_session.get(Substep, substep_id)
    assert sub.media == []


def test_preview_partial_renders_media_img(client, db_session):
    set_id = _create_set(client)
    substep_id = _add_substep(client, set_id, db_session)

    upload = client.post(
        "/admin/media", files={"file": ("photo.png", io.BytesIO(PNG_BYTES), "image/png")}
    )
    url = upload.json()["url"]
    media_payload = f'[{{"kind": "photo", "url": "{url}", "caption": "slide face"}}]'
    client.post(
        f"/admin/library/substeps/{substep_id}/media",
        data={"media": media_payload, "who": "alice"},
    )

    resp = client.get(f"/admin/library/sets/{set_id}/preview")
    assert resp.status_code == 200
    assert f'src="{url}"' in resp.text
    assert "slide face" in resp.text
