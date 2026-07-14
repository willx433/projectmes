"""Integration tests for products + part-map + variants CRUD (P2-02) —
DD §4.6, §5.

Zero network: sqlite in-memory DB, same portable-types trick
tests/integration/test_health.py already uses (models_library.py shares
app.domain.models_jb2.Base's metadata, so Base.metadata.create_all(engine)
creates products/product_part_map too once the module is imported).
"""
from __future__ import annotations

import uuid
from datetime import date

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool
from starlette.testclient import TestClient

from app.db import get_session
from app.domain.models_jb2 import Base, JB2OrderLineItem, MappingException
from app.main import app  # noqa: F401 -- importing app.api.products registers library tables


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
def client(engine):
    def _override():
        session = Session(engine)
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_session] = _override
    with TestClient(app) as c:
        yield c
    del app.dependency_overrides[get_session]


# -- product CRUD roundtrip ----------------------------------------------------

def test_product_crud_roundtrip(client):
    resp = client.post(
        "/api/v1/products",
        json={
            "name": "Apollo",
            "description": "9mm / .45 pistol",
            "variant_schema": {"caliber": ["9mm", ".45"], "finish": ["BLK", "FDE"]},
        },
    )
    assert resp.status_code == 201
    product = resp.json()
    assert product["name"] == "Apollo"
    assert product["active"] is True
    product_id = product["id"]

    resp = client.get(f"/api/v1/products/{product_id}")
    assert resp.status_code == 200
    assert resp.json()["name"] == "Apollo"

    resp = client.get("/api/v1/products")
    assert resp.status_code == 200
    assert [p["name"] for p in resp.json()] == ["Apollo"]

    resp = client.patch(f"/api/v1/products/{product_id}", json={"description": "updated"})
    assert resp.status_code == 200
    assert resp.json()["description"] == "updated"

    resp = client.delete(f"/api/v1/products/{product_id}")
    assert resp.status_code == 204

    resp = client.get(f"/api/v1/products/{product_id}")
    assert resp.status_code == 404


def test_duplicate_product_name_is_409(client):
    client.post("/api/v1/products", json={"name": "Apollo"})
    resp = client.post("/api/v1/products", json={"name": "Apollo"})
    assert resp.status_code == 409


# -- part-number mapping --------------------------------------------------------

def test_duplicate_part_number_mapping_is_409(client):
    product = client.post("/api/v1/products", json={"name": "Apollo"}).json()
    resp = client.post(
        f"/api/v1/products/{product['id']}/part-map", json={"jb2_part_number": "APOLLO-9-BLK"}
    )
    assert resp.status_code == 201

    other_product = client.post("/api/v1/products", json={"name": "Athena"}).json()
    resp = client.post(
        f"/api/v1/products/{other_product['id']}/part-map",
        json={"jb2_part_number": "APOLLO-9-BLK"},
    )
    assert resp.status_code == 409


def test_variant_values_must_match_product_variant_schema(client):
    product = client.post(
        "/api/v1/products",
        json={"name": "Apollo", "variant_schema": {"caliber": ["9mm", ".45"]}},
    ).json()

    # unknown key -> 400
    resp = client.post(
        f"/api/v1/products/{product['id']}/part-map",
        json={"jb2_part_number": "APOLLO-9-BLK", "variant_values": {"finish": "BLK"}},
    )
    assert resp.status_code == 400

    # allowed key -> 201
    resp = client.post(
        f"/api/v1/products/{product['id']}/part-map",
        json={"jb2_part_number": "APOLLO-9-BLK", "variant_values": {"caliber": "9mm"}},
    )
    assert resp.status_code == 201


def test_part_map_delete(client):
    product = client.post("/api/v1/products", json={"name": "Apollo"}).json()
    mapping = client.post(
        f"/api/v1/products/{product['id']}/part-map", json={"jb2_part_number": "APOLLO-9-BLK"}
    ).json()

    resp = client.delete(f"/api/v1/part-map/{mapping['id']}")
    assert resp.status_code == 204
    resp = client.delete(f"/api/v1/part-map/{mapping['id']}")
    assert resp.status_code == 404


# -- delete-with-mappings guard -------------------------------------------------

def test_delete_product_with_mappings_requires_force(client):
    product = client.post("/api/v1/products", json={"name": "Apollo"}).json()
    client.post(
        f"/api/v1/products/{product['id']}/part-map", json={"jb2_part_number": "APOLLO-9-BLK"}
    )

    resp = client.delete(f"/api/v1/products/{product['id']}")
    assert resp.status_code == 409

    resp = client.delete(f"/api/v1/products/{product['id']}?force=true")
    assert resp.status_code == 204

    resp = client.get(f"/api/v1/products/{product['id']}")
    assert resp.status_code == 404


# -- unmapped-parts view ---------------------------------------------------------

def _seed_line_item(session: Session, part_number: str) -> None:
    session.add(
        JB2OrderLineItem(
            id=uuid.uuid4(),
            jb2_id=f"li-{part_number}",
            part_number=part_number,
            description="test line item",
            qty=1,
            due_date=date(2026, 8, 1),
            payload={},
            content_hash="x",
            synced_at=date(2026, 7, 14),
        )
    )
    session.commit()


def test_unmapped_parts_view_lists_seeded_part_and_clears_after_mapping(client, db_session):
    _seed_line_item(db_session, "APOLLO-9-BLK")

    resp = client.get("/api/v1/unmapped-parts")
    assert resp.status_code == 200
    body = resp.json()
    assert "APOLLO-9-BLK" in body["part_numbers"]

    product = client.post("/api/v1/products", json={"name": "Apollo"}).json()
    client.post(
        f"/api/v1/products/{product['id']}/part-map", json={"jb2_part_number": "APOLLO-9-BLK"}
    )

    resp = client.get("/api/v1/unmapped-parts")
    assert "APOLLO-9-BLK" not in resp.json()["part_numbers"]


def test_unmapped_parts_view_includes_unresolved_mapping_exceptions(client, db_session):
    db_session.add(
        MappingException(
            id=uuid.uuid4(), kind="part_number", value="MYSTERY-PART", resolved=False
        )
    )
    db_session.commit()

    resp = client.get("/api/v1/unmapped-parts")
    values = [e["value"] for e in resp.json()["mapping_exceptions"]]
    assert "MYSTERY-PART" in values


# -- admin UI (server-rendered) -------------------------------------------------

def test_admin_products_page_renders(client, db_session):
    _seed_line_item(db_session, "APOLLO-9-FDE")
    resp = client.get("/admin/products")
    assert resp.status_code == 200
    assert "APOLLO-9-FDE" in resp.text


def test_admin_create_product_and_add_mapping(client):
    resp = client.post(
        "/admin/products",
        data={"name": "Nyx", "variant_schema": '{"caliber": ["9mm"]}'},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    resp = client.get("/admin/products")
    assert "Nyx" in resp.text

    products = client.get("/api/v1/products").json()
    nyx = next(p for p in products if p["name"] == "Nyx")

    resp = client.post(
        f"/admin/products/{nyx['id']}/part-map",
        data={"jb2_part_number": "NYX-9-BLK", "variant_values": '{"caliber": "9mm"}'},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    detail = client.get(f"/admin/products/{nyx['id']}")
    assert "NYX-9-BLK" in detail.text


def test_admin_delete_product_with_mappings_shows_error_without_force(client):
    product = client.post("/api/v1/products", json={"name": "Ares"}).json()
    client.post(
        f"/api/v1/products/{product['id']}/part-map", json={"jb2_part_number": "ARES-45-BLK"}
    )

    resp = client.post(
        f"/admin/products/{product['id']}/delete", data={}, follow_redirects=False
    )
    assert resp.status_code == 303
    assert "error=" in resp.headers["location"]

    still_there = client.get(f"/api/v1/products/{product['id']}")
    assert still_there.status_code == 200

    resp = client.post(
        f"/admin/products/{product['id']}/delete",
        data={"force": "true"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    gone = client.get(f"/api/v1/products/{product['id']}")
    assert gone.status_code == 404
