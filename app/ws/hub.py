"""In-process pub/sub hub (P4-01, DD §11/§13.1).

NOT what actually powers the dashboard's live updates -- see
`app/api/dashboard.py`'s `/dashboard/stream` docstring and
docs/CHANGE_REQUESTS.md CR-015 for why. This module exists to satisfy the
"hub exposes publish()+subscribe()" shape asked for, and is useful if Atlas
is ever run single-process (e.g. `uvicorn --workers 1`, a dev box, or a
future switch to a shared broker): `publish()` fans a dict out to every
`subscribe()`-ed asyncio queue in *this* process only.

ponytail: deliberately not wired into `app.domain.events.emit()`. Doing so
would look like it makes the dashboard live across all 4 uvicorn workers,
but an in-process queue can't cross a process boundary -- a client whose
SSE connection lands on worker 2 would never see an event emitted while
its request was handled by worker 1. Wiring it in would be a correctness
regression disguised as a feature. Upgrade path if this ever needs to be
the real mechanism: a Postgres LISTEN/NOTIFY channel or Redis pub/sub
behind the same publish()/subscribe() shape -- swap this module's guts,
keep the interface.
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

_subscribers: set[asyncio.Queue] = set()


def publish(event: dict[str, Any]) -> None:
    """Fan `event` out to every currently-subscribed queue in this process.
    Never blocks -- a full queue (a slow/gone client) just drops the event
    rather than backing up the publisher."""
    for queue in list(_subscribers):
        try:
            queue.put_nowait(event)
        except asyncio.QueueFull:
            pass


async def subscribe() -> AsyncIterator[dict[str, Any]]:
    """Async generator yielding events published (in this process) after
    subscription starts. Caller iterates with `async for`; the queue is
    unregistered when the generator is closed/garbage-collected."""
    queue: asyncio.Queue = asyncio.Queue(maxsize=100)
    _subscribers.add(queue)
    try:
        while True:
            yield await queue.get()
    finally:
        _subscribers.discard(queue)
