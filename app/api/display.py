"""Display feed endpoints (P1-13, CR-011) -- DD §4.2 display row.

Serves the last-good cached payload for the two unfiltered/heavy JB2
display feeds (`shopview/get-jobs`, `eci-aps/get-schedule`) -- cache-only,
never calls JB2 directly from a request handler (every JB2 read goes
through the sync worker, see app/sync/display.py).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db import get_session
from app.domain.models_jb2 import DisplayCache

router = APIRouter()


def _cached(session: Session, key: str) -> dict:
    row = session.get(DisplayCache, key)
    if row is None:
        raise HTTPException(status_code=503, detail=f"{key} not cached yet")
    return {"fetched_at": row.fetched_at.isoformat(), "payload": row.payload}


@router.get("/api/v1/jb2-schedule")
def jb2_schedule(session: Session = Depends(get_session)) -> dict:
    return _cached(session, "jb2-schedule")


@router.get("/api/v1/jb2-shopview")
def jb2_shopview(session: Session = Depends(get_session)) -> dict:
    return _cached(session, "jb2-shopview")
