"""`python -m app.outbox` — the outbox drainer process (P1-11, DD §16.1).

systemd unit: mes-outbox.
"""
from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.config import config
from app.jb2.client import Jb2Client
from app.logging import configure_logging
from app.outbox.drainer import run_forever


def main() -> None:
    configure_logging()
    config.validate(["database_url"])
    engine = create_engine(config.database_url)
    session_factory = sessionmaker(bind=engine)
    client = Jb2Client.from_config(config)
    run_forever(session_factory, client)


if __name__ == "__main__":
    main()
