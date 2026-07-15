"""`python -m app.sync` — the sync worker process (P1-05, DD §16.3, systemd unit mes-sync)."""
from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.config import config
from app.jb2.client import Jb2Client
from app.logging import configure_logging
from app.sync import hooks
from app.sync.worker import SyncWorker


def main() -> None:
    configure_logging()
    config.validate(["database_url"])
    engine = create_engine(config.database_url)
    session_factory = sessionmaker(bind=engine)
    client = Jb2Client.from_config(config)
    # P2-10: work-order creation/reconciliation subscribes to order/
    # line-item sync events here (not imported by app.sync.worker itself --
    # see app/sync/hooks.py docstring).
    hooks.register()
    SyncWorker(session_factory, client).run_forever()


if __name__ == "__main__":
    main()
