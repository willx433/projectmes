"""`python -m app.floorworker` -- the floor session-lifecycle worker process
(P3-08/09, DD §6.7/§17.8, docs/state-machine.md §6). Runs
`app.domain.sessions.run_idle_sweep` on a timer: auto-pauses idle work
sessions (no substep activity for STATION_SESSION_IDLE_MIN), then
auto-closes sessions paused longer than SESSION_AUTO_CLOSE_MIN (withheld
from JB2 until a lead confirms via POST /sessions/{id}/confirm, O6).

Also runs `app.ops.alerts.run_alert_sweep` (P4-05, DD N5) on the same
timer -- outbox failures, sync stalls, station offline are all
minutes-granularity conditions same as the session sweep, so one process/
one timer covers both; no second worker needed.

systemd unit: mes-floor (deploy/systemd/mes-floor.service).
"""
from __future__ import annotations

import time

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.config import config
from app.domain import sessions
from app.logging import configure_logging
from app.ops import alerts

# ponytail: sessions are minutes-granularity (STATION_SESSION_IDLE_MIN
# defaults to 10, SESSION_AUTO_CLOSE_MIN to 60) -- a 60s poll is plenty;
# no need for the sub-second cadence app/outbox's drainer uses. Same cadence
# comfortably covers the alert thresholds (10/15 min).
POLL_INTERVAL_S = 60.0


def main() -> None:
    configure_logging()
    config.validate(["database_url"])
    engine = create_engine(config.database_url)
    session_factory = sessionmaker(bind=engine)
    while True:
        sessions.run_idle_sweep(session_factory)
        alerts.run_alert_sweep(session_factory)
        time.sleep(POLL_INTERVAL_S)


if __name__ == "__main__":
    main()
