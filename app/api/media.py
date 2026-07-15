"""Image upload + annotation media for instruction-set substeps (P2-06) —
DD §7.2, C21.

Files land under ``{ARTIFACT_DIR}/library/{uuid}.{ext}`` and are served back
via a path-traversal-safe route. Attach/remove is "overwrite the substep's
whole media list" — the router validates shape, `library.set_substep_media`
(flagged addition in app/domain/library.py) does the write with the usual
editable-state guard.
"""
from __future__ import annotations

import json
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.api.library import _back, _redirect_error, _substep_step_and_set
from app.config import config
from app.db import get_session
from app.domain import library

router = APIRouter()

ALLOWED_CONTENT_TYPES = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/webp": "webp",
}
MAX_UPLOAD_BYTES = 8 * 1024 * 1024


def _library_dir() -> Path:
    base = Path(config.artifact_dir or "./artifacts")
    d = base / "library"
    d.mkdir(parents=True, exist_ok=True)
    return d


@router.post("/admin/media")
async def upload_media(file: UploadFile) -> JSONResponse:
    ext = ALLOWED_CONTENT_TYPES.get(file.content_type)
    if ext is None:
        raise HTTPException(
            status_code=415, detail=f"unsupported content type '{file.content_type}'"
        )

    data = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="file exceeds 8 MB limit")

    name = f"{uuid.uuid4()}.{ext}"
    (_library_dir() / name).write_bytes(data)
    return JSONResponse({"url": f"/artifacts/library/{name}"})


@router.get("/artifacts/library/{name}")
def get_media(name: str) -> FileResponse:
    base = _library_dir().resolve()
    path = (base / name).resolve()
    # path-traversal guard: resolved path must still live directly under base
    if path.parent != base or not path.is_file():
        raise HTTPException(status_code=404)
    return FileResponse(path)


def _parse_media_list(raw: str) -> list[dict]:
    raw = raw.strip()
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except ValueError as exc:
        raise library.LibraryError(f"media: invalid JSON ({exc})")
    if not isinstance(value, list):
        raise library.LibraryError("media: must be a JSON array")
    cleaned = []
    for item in value:
        if not isinstance(item, dict) or not item.get("url"):
            raise library.LibraryError("media: each entry needs a 'url'")
        cleaned.append(
            {
                "kind": str(item.get("kind") or "photo"),
                "url": str(item["url"]),
                "caption": str(item.get("caption") or ""),
            }
        )
    return cleaned


@router.post("/admin/library/substeps/{substep_id}/media")
def admin_library_set_substep_media(
    substep_id: uuid.UUID,
    media: str = Form(""),
    who: str = Form(""),
    session: Session = Depends(get_session),
) -> RedirectResponse:
    _, set_id = _substep_step_and_set(session, substep_id)
    try:
        media_list = _parse_media_list(media)
        library.set_substep_media(session, substep_id, media_list, who=who.strip() or None)
        session.commit()
    except library.LibraryError as exc:
        session.rollback()
        return _redirect_error(_back(set_id) if set_id else "/admin/library", str(exc))
    return RedirectResponse(_back(set_id) if set_id else "/admin/library", status_code=303)
