"""Pipeline dashboard (P4-01/P4-02, DD §13.1, pistol_flow_visual §4, CR-004).

Three endpoints:
  - `GET /api/v1/dashboard/pipeline` -- the JSON the board (and tests) are
    built from: KPI strip + grouped unit cards. All state derivation lives
    in `app/domain/pipeline.py`; this module only shapes the HTTP surface.
  - `GET /dashboard` -- the server-rendered TV/office page.
  - `GET /dashboard/stream` -- SSE "refresh" pings so the page updates
    within DD N2's <2s window without a client polling loop of its own.

**SSE, not WebSocket -- flagged deviation, see docs/CHANGE_REQUESTS.md
CR-015.** DD §13.1 says "Live via WebSocket"; the plan's own P4-01 row
allows "WebSocket/SSE hub" and explicitly notes the multi-worker problem
(events emitted on one of the 4 uvicorn workers aren't visible to an
in-process pub/sub on another). SSE is one-way, which is all this read-only
board needs, proxies through Caddy with zero extra config (plain HTTP,
no Upgrade handshake to special-case), and -- because it's driven by
polling the `events` table's max id rather than an in-process publish --
is correct across however many workers the server runs, not just one.
`app/ws/hub.py`'s in-process publish/subscribe exists per the task shape
but is NOT what this stream uses.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import StreamingResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import REPO_ROOT, config
from app.db import _get_session_factory, get_session
from app.domain import pipeline
from app.domain.models_floor import Event

router = APIRouter()
templates = Jinja2Templates(directory=str(REPO_ROOT / "templates"))

TV_COOKIE = "dashboard_tv"
# How often the stream checks `events` for a new row, and how often it sends
# a comment-only keepalive so intermediate proxies don't time the connection
# out on an otherwise-silent long-lived response.
POLL_INTERVAL_S = 1.0
KEEPALIVE_EVERY_S = 15.0


def _tv_mode(request: Request) -> bool:
    """True if this request should render/behave as the nav-less, read-only
    TV kiosk view. No token configured -> any non-empty `?tv=`/cookie value
    opts in (there's nothing to mutate on this page regardless); a
    configured `DASHBOARD_TV_TOKEN` must match exactly."""
    token = request.query_params.get("tv") or request.cookies.get(TV_COOKIE)
    if not token:
        return False
    if config.dashboard_tv_token:
        return token == config.dashboard_tv_token
    return True


def _pipeline_payload(session: Session, group_by: str | None) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    cards = pipeline.build_board(session, now, config)
    return {
        "generated_at": now.isoformat(),
        "kpis": pipeline.compute_kpis(cards),
        "group_by": group_by if group_by in pipeline.GROUP_BY_CHOICES else None,
        "groups": pipeline.group_cards(cards, group_by),
    }


@router.get("/api/v1/dashboard/pipeline")
def api_pipeline(
    group_by: str | None = None, session: Session = Depends(get_session)
) -> dict[str, Any]:
    return _pipeline_payload(session, group_by)


@router.get("/dashboard")
def dashboard_page(
    request: Request,
    response: Response,
    group_by: str | None = None,
    session: Session = Depends(get_session),
):
    tv_mode = _tv_mode(request)
    if tv_mode:
        # persist across the page's own SSE-triggered reloads/navigation
        # without the token needing to stay in the URL.
        response.set_cookie(TV_COOKIE, request.query_params.get("tv") or "1", httponly=True)
    payload = _pipeline_payload(session, group_by)
    return templates.TemplateResponse(
        request,
        "dashboard/pipeline.html",
        {
            **payload,
            "tv_mode": tv_mode,
            "group_by_choices": pipeline.GROUP_BY_CHOICES,
        },
    )


def _latest_event_id(session: Session) -> int:
    return session.execute(select(func.max(Event.id))).scalar_one() or 0


def get_session_factory():
    """Separate (overridable) dependency rather than reusing `get_session`
    directly -- the stream holds no single request-scoped session open for
    its whole (potentially hours-long) life, it opens a fresh short-lived
    one per poll tick instead. Tests override this the same way other
    tests override `get_session`, pointing it at their own test engine."""
    return _get_session_factory()


@router.get("/dashboard/stream")
async def dashboard_stream(request: Request, session_factory=Depends(get_session_factory)):
    """Polls `events` for a new max id every ~1s (DD N2's <2s live-update
    budget) and pushes an SSE `refresh` ping the client uses to re-fetch the
    board -- see module docstring for why polling instead of a push hook."""

    async def gen():
        with session_factory() as session:
            last_id = _latest_event_id(session)

        last_keepalive = asyncio.get_event_loop().time()
        while True:
            if await request.is_disconnected():
                break
            await asyncio.sleep(POLL_INTERVAL_S)
            with session_factory() as session:
                current_id = _latest_event_id(session)
            if current_id != last_id:
                last_id = current_id
                yield "event: refresh\ndata: {}\n\n"
                last_keepalive = asyncio.get_event_loop().time()
            elif asyncio.get_event_loop().time() - last_keepalive > KEEPALIVE_EVERY_S:
                yield ": keepalive\n\n"
                last_keepalive = asyncio.get_event_loop().time()

    return StreamingResponse(gen(), media_type="text/event-stream")
