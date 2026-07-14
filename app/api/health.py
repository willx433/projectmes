"""Admin health pages (P1-10) — DD §11, §9.9, §17.1.

Four JSON endpoints (`/health`, `/health/jb2`, `/health/sync`,
`/health/outbox`) plus a minimal server-rendered admin UI (`/admin/health`)
with a replay action for parked outbox rows. Read-only against
`app.domain.models_jb2` — no dependency on app/sync/worker.py's runtime
loop, only its `REGISTRY` (resource -> poll cadence), which is read-only
reuse, not an edit.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from urllib.parse import quote

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import REPO_ROOT
from app.db import get_session
from app.domain.models_jb2 import JB2Outbox, MappingException, SyncRun
from app.jb2.client import get_jb2_status
from app.outbox import drainer
from app.sync import checkpoints
from app.sync.engine import to_utc
from app.sync.worker import REGISTRY

router = APIRouter()
templates = Jinja2Templates(directory=str(REPO_ROOT / "templates"))

# ponytail: hardcoded fallback for any sync_runs resource not (yet) in
# worker.REGISTRY -- keeps this module from breaking if a resource is
# retired from the registry but old rows remain.
DEFAULT_CADENCE_S = 60.0
# "Backlog" for the overall /health verdict: oldest pending/failed outbox
# row older than this many seconds.
OUTBOX_BACKLOG_S = 600.0

OUTBOX_STATUSES = ("pending", "sent", "confirmed", "failed")


def _sync_status(session: Session) -> list[dict]:
    cadences = {r.name: r.cadence_s for r in REGISTRY}
    known_resources = set(cadences) | set(
        session.execute(select(SyncRun.resource).distinct()).scalars().all()
    )
    now = datetime.now(timezone.utc)

    out = []
    for resource in sorted(known_resources):
        latest = session.execute(
            select(SyncRun)
            .where(SyncRun.resource == resource)
            .order_by(SyncRun.started_at.desc())
            .limit(1)
        ).scalar_one_or_none()
        cadence = cadences.get(resource, DEFAULT_CADENCE_S)
        checkpoint = checkpoints.get_checkpoint(session, resource)

        age_s = None
        stalled = True
        if latest is not None:
            finished = to_utc(latest.finished_at or latest.started_at)
            age_s = (now - finished).total_seconds()
            stalled = age_s > 2 * cadence

        last_run_at = (
            latest.finished_at.isoformat() if latest and latest.finished_at else None
        )
        out.append({
            "resource": resource,
            "last_run_at": last_run_at,
            "fetched": latest.fetched if latest is not None else None,
            "changed": latest.changed if latest is not None else None,
            "error": latest.error if latest is not None else None,
            "checkpoint": checkpoint.isoformat() if checkpoint else None,
            "cadence_s": cadence,
            "age_s": age_s,
            "stalled": stalled,
        })
    return out


def _outbox_status(session: Session) -> dict:
    now = datetime.now(timezone.utc)
    counts = dict(
        session.execute(
            select(JB2Outbox.status, func.count()).group_by(JB2Outbox.status)
        ).all()
    )
    oldest_pending = session.execute(
        select(JB2Outbox.created_at)
        .where(JB2Outbox.status.in_(("pending", "failed")))
        .order_by(JB2Outbox.created_at)
        .limit(1)
    ).scalar_one_or_none()
    oldest_pending_age_s = (
        (now - to_utc(oldest_pending)).total_seconds() if oldest_pending is not None else None
    )
    parked = (
        session.execute(
            select(JB2Outbox).where(JB2Outbox.status == "failed").order_by(JB2Outbox.created_at)
        )
        .scalars()
        .all()
    )
    return {
        "counts": {status: counts.get(status, 0) for status in OUTBOX_STATUSES},
        "oldest_pending_age_s": oldest_pending_age_s,
        "parked": [
            {
                "id": str(row.id),
                "kind": row.kind,
                "attempts": row.attempts,
                "last_error": row.last_error,
                "created_at": row.created_at.isoformat(),
                "work_order_id": str(row.work_order_id) if row.work_order_id else None,
            }
            for row in parked
        ],
    }


def _mapping_exceptions(session: Session, limit: int = 50) -> list[MappingException]:
    return list(
        session.execute(
            select(MappingException)
            .where(MappingException.resolved.is_(False))
            .order_by(MappingException.created_at.desc())
            .limit(limit)
        ).scalars()
    )


# -- JSON endpoints ----------------------------------------------------------

@router.get("/health")
def health(request: Request, session: Session = Depends(get_session)) -> dict:
    reasons: list[str] = []

    try:
        session.execute(select(1))
        db_ok = True
    except Exception as exc:  # noqa: BLE001 -- report, don't crash the health check
        db_ok = False
        reasons.append(f"db unreachable: {exc}")

    jb2_status = get_jb2_status()
    breaker_state = jb2_status["breaker_state"]
    if breaker_state == "open":
        reasons.append("JB2 circuit breaker open")

    sync_resources = _sync_status(session)
    stalled = [r["resource"] for r in sync_resources if r["stalled"]]
    if stalled:
        reasons.append(f"sync stalled: {', '.join(stalled)}")

    outbox = _outbox_status(session)
    if outbox["counts"]["failed"]:
        reasons.append(f"{outbox['counts']['failed']} outbox row(s) parked")
    if outbox["oldest_pending_age_s"] and outbox["oldest_pending_age_s"] > OUTBOX_BACKLOG_S:
        reasons.append("outbox backlog")

    return {
        "status": "ok" if not reasons else "degraded",
        "version": getattr(request.app.state, "version", "dev"),
        "db_ok": db_ok,
        "jb2_breaker_state": breaker_state,
        "sync_stalled_resources": stalled,
        "outbox_failed": outbox["counts"]["failed"],
        "reasons": reasons,
    }


@router.get("/health/jb2")
def health_jb2() -> dict:
    status = get_jb2_status()
    return {
        "breaker_state": status["breaker_state"],
        "last_success_at": status["last_success_at"].isoformat()
        if status["last_success_at"] else None,
        "last_failure_at": status["last_failure_at"].isoformat()
        if status["last_failure_at"] else None,
    }


@router.get("/health/sync")
def health_sync(session: Session = Depends(get_session)) -> dict:
    return {"resources": _sync_status(session)}


@router.get("/health/outbox")
def health_outbox(session: Session = Depends(get_session)) -> dict:
    return _outbox_status(session)


# -- admin UI -----------------------------------------------------------------

@router.get("/admin/health")
def admin_health(request: Request, session: Session = Depends(get_session)):
    return templates.TemplateResponse(
        request,
        "admin/health.html",
        {
            "jb2_status": get_jb2_status(),
            "sync_resources": _sync_status(session),
            "outbox": _outbox_status(session),
            "mapping_exceptions": _mapping_exceptions(session),
            "error": request.query_params.get("error"),
        },
    )


@router.post("/admin/outbox/{outbox_id}/replay")
def admin_outbox_replay(outbox_id: uuid.UUID, session: Session = Depends(get_session)):
    try:
        drainer.replay(session, outbox_id)
    except ValueError as exc:
        return RedirectResponse(
            f"/admin/health?error={quote(str(exc))}", status_code=303
        )
    return RedirectResponse("/admin/health", status_code=303)
