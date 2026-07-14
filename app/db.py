"""DB engine/session for the API process (P1-10 health/admin pages).

ponytail: one lazily-built sessionmaker + a FastAPI dependency is the whole
data-access layer here — no repository/unit-of-work scaffolding for a
handful of read endpoints and one replay POST. Built lazily (not at import
time) so importing this module never fails on a dev box with no
DATABASE_URL set yet.
"""
from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import config

_session_factory: sessionmaker | None = None


def _get_session_factory() -> sessionmaker:
    global _session_factory
    if _session_factory is None:
        config.validate(["database_url"])
        engine = create_engine(config.database_url, pool_pre_ping=True)
        _session_factory = sessionmaker(bind=engine)
    return _session_factory


def get_session() -> Iterator[Session]:
    """FastAPI dependency: yields a Session, always closed after the request."""
    session = _get_session_factory()()
    try:
        yield session
    finally:
        session.close()
