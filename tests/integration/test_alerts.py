"""Integration tests for P4-05 alerts -- DD N5, §9.8 (station heartbeat).

Zero network: sqlite in-memory DB, same `Base.metadata.create_all` pattern
as tests/integration/test_health.py. `app.ops.alerts.check_alerts` is
exercised directly against seeded rows (no HTTP layer -- there's no
endpoint, just the worker-loop function + the floorworker wiring).
"""
from __future__ import annotations

import dataclasses
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.config import config as real_config
from app.domain.models_floor import Station
from app.domain.models_jb2 import Base, JB2Outbox, SyncRun

# Registers the full model graph (work_orders etc.) on the shared Base.metadata
# -- jb2_outbox.work_order_id's FK needs work_orders to exist even when this
# test file runs standalone (metadata is otherwise only as complete as
# whatever else has been imported this process, per test_finish_and_writeback
# .py's own note).
from app.main import app as _main_app  # noqa: F401,E402
from app.ops import alerts


@pytest.fixture(autouse=True)
def _reset_cooldowns():
    alerts._LAST_SENT.clear()
    yield
    alerts._LAST_SENT.clear()


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


def _seed_healthy_sync(session: Session, now: datetime, resource: str = "orders") -> None:
    session.add(
        SyncRun(
            id=uuid.uuid4(), resource=resource, started_at=now - timedelta(seconds=5),
            finished_at=now, fetched=1, changed=0, error=None,
        )
    )
    session.commit()


def _seed_station(session: Session, *, last_seen_at: datetime | None, name: str = "St1") -> Station:
    station = Station(
        id=uuid.uuid4(), name=name, kiosk_token=f"tok-{uuid.uuid4()}", active=True,
        last_seen_at=last_seen_at,
    )
    session.add(station)
    session.commit()
    return station


def _seed_failed_outbox(session: Session) -> uuid.UUID:
    row_id = uuid.uuid4()
    session.add(
        JB2Outbox(
            id=row_id, kind="time_ticket", payload={}, idempotency_key=f"k-{row_id}",
            status="failed", attempts=8, last_error="JB2 500", work_order_id=None,
        )
    )
    session.commit()
    return row_id


# -- no false positives when healthy --------------------------------------------


def test_no_alerts_when_everything_healthy(db_session):
    now = datetime.now(timezone.utc)
    _seed_healthy_sync(db_session, now)
    _seed_station(db_session, last_seen_at=now)

    result = alerts.check_alerts(db_session, now)
    assert result == []


# -- each condition fires exactly once, then cools down ------------------------


def test_outbox_failed_fires_once_then_cools_down(db_session):
    now = datetime.now(timezone.utc)
    _seed_healthy_sync(db_session, now)
    _seed_station(db_session, last_seen_at=now)
    _seed_failed_outbox(db_session)

    found = alerts.check_alerts(db_session, now)
    assert [a.kind for a in found] == ["outbox_failed"]
    alert = found[0]

    assert alerts.send_alert(alert, now=now) is True
    # same sweep cadence (seconds later) -- still cooling down, no re-send.
    assert alerts.send_alert(alert, now=now + timedelta(seconds=1)) is False
    assert alerts.send_alert(alert, now=now + timedelta(minutes=5)) is False
    # cooldown elapsed (default 30 min) -- fires again.
    cooldown = timedelta(minutes=real_config.alert_cooldown_min + 1)
    assert alerts.send_alert(alert, now=now + cooldown) is True


def test_sync_stall_alert_fires_after_10_min_no_success(db_session):
    now = datetime.now(timezone.utc)
    stale = now - timedelta(minutes=11)
    db_session.add(
        SyncRun(
            id=uuid.uuid4(), resource="orders", started_at=stale, finished_at=stale,
            fetched=1, changed=0, error=None,
        )
    )
    db_session.commit()
    _seed_station(db_session, last_seen_at=now)

    found = alerts.check_alerts(db_session, now)
    assert [a.kind for a in found] == ["sync_stalled"]
    assert found[0].key == "sync_stalled:orders"

    # cooldown behaves the same as any other alert signature.
    assert alerts.send_alert(found[0], now=now) is True
    assert alerts.send_alert(found[0], now=now + timedelta(minutes=1)) is False


def test_sync_never_succeeded_also_stalls(db_session):
    now = datetime.now(timezone.utc)
    db_session.add(
        SyncRun(
            id=uuid.uuid4(), resource="orders", started_at=now, finished_at=now,
            fetched=0, changed=0, error="boom",
        )
    )
    db_session.commit()

    found = alerts.check_alerts(db_session, now)
    assert any(a.key == "sync_stalled:orders" for a in found)


def test_station_offline_alert_fires_after_15_min_silence(db_session):
    now = datetime.now(timezone.utc)
    _seed_healthy_sync(db_session, now)
    offline_station = _seed_station(
        db_session, last_seen_at=now - timedelta(minutes=20), name="Offline1",
    )
    _seed_station(db_session, last_seen_at=now - timedelta(minutes=2), name="Online1")

    found = alerts.check_alerts(db_session, now)
    assert [a.kind for a in found] == ["station_offline"]
    assert found[0].key == f"station_offline:{offline_station.id}"


def test_never_seen_station_does_not_alert(db_session):
    """A freshly enrolled station with no heartbeat yet isn't a regression."""
    now = datetime.now(timezone.utc)
    _seed_healthy_sync(db_session, now)
    _seed_station(db_session, last_seen_at=None, name="BrandNew")

    found = alerts.check_alerts(db_session, now)
    assert found == []


def test_multiple_conditions_produce_multiple_distinct_alerts_no_storm(db_session):
    """Two failing stations + a stalled sync + a failed outbox row -> exactly
    one alert per distinct problem, not one per row (the "no storms"
    acceptance criterion)."""
    now = datetime.now(timezone.utc)
    _seed_failed_outbox(db_session)
    _seed_failed_outbox(db_session)  # a second parked row -- still one alert
    stale = now - timedelta(minutes=30)
    db_session.add(
        SyncRun(
            id=uuid.uuid4(), resource="orders", started_at=stale, finished_at=stale,
            fetched=1, changed=0, error=None,
        )
    )
    db_session.commit()
    _seed_station(db_session, last_seen_at=now - timedelta(minutes=30), name="Down1")
    _seed_station(db_session, last_seen_at=now - timedelta(minutes=45), name="Down2")

    found = alerts.check_alerts(db_session, now)
    keys = sorted(a.key for a in found)
    expected = sorted([
        "outbox_failed",
        "station_offline:" + str(_id_by_name(db_session, "Down1")),
        "station_offline:" + str(_id_by_name(db_session, "Down2")),
        "sync_stalled:orders",
    ])
    assert keys == expected
    assert len(found) == 4  # one per distinct problem, not one per row -- no storm


def _id_by_name(session: Session, name: str) -> uuid.UUID:
    from sqlalchemy import select

    return session.execute(select(Station.id).where(Station.name == name)).scalar_one()


# -- log-only when no SMTP configured -------------------------------------------


def test_send_alert_log_only_when_smtp_not_configured(monkeypatch, caplog):
    test_config = dataclasses.replace(real_config, alert_email_to=None, alert_smtp_host=None)
    monkeypatch.setattr(alerts, "config", test_config)

    calls = []
    monkeypatch.setattr(alerts.smtplib, "SMTP", lambda *a, **k: calls.append((a, k)) or None)

    alert = alerts.Alert(kind="outbox_failed", key="outbox_failed", message="2 rows parked")
    with caplog.at_level("WARNING", logger="app.ops.alerts"):
        sent = alerts.send_alert(alert)

    assert sent is True  # "sent" = processed/logged, not necessarily emailed
    assert calls == []  # never touched SMTP
    assert any("mes_alert" in r.message for r in caplog.records)


def test_send_alert_emails_when_smtp_configured(monkeypatch):
    test_config = dataclasses.replace(
        real_config, alert_email_to="lead@example.com", alert_smtp_host="smtp.example.com",
        alert_smtp_port=587, alert_smtp_user=None, alert_smtp_pass=None,
    )
    monkeypatch.setattr(alerts, "config", test_config)

    sent_mail = {}

    class _FakeSMTP:
        def __init__(self, host, port, timeout=None):
            sent_mail["host"] = host
            sent_mail["port"] = port

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def sendmail(self, from_addr, to_addrs, msg):
            sent_mail["from"] = from_addr
            sent_mail["to"] = to_addrs
            sent_mail["msg"] = msg

    monkeypatch.setattr(alerts.smtplib, "SMTP", _FakeSMTP)

    alert = alerts.Alert(kind="station_offline", key="station_offline:x", message="St1 offline")
    assert alerts.send_alert(alert) is True

    assert sent_mail["host"] == "smtp.example.com"
    assert sent_mail["to"] == ["lead@example.com"]
    assert "St1 offline" in sent_mail["msg"]


def test_send_alert_email_failure_does_not_raise(monkeypatch):
    test_config = dataclasses.replace(
        real_config, alert_email_to="lead@example.com", alert_smtp_host="smtp.example.com",
    )
    monkeypatch.setattr(alerts, "config", test_config)

    def _boom(*a, **k):
        raise OSError("connection refused")

    monkeypatch.setattr(alerts.smtplib, "SMTP", _boom)

    alert = alerts.Alert(kind="outbox_failed", key="outbox_failed", message="boom test")
    # must not raise -- alerting can never crash the floorworker loop.
    assert alerts.send_alert(alert) is True


# -- run_alert_sweep (floorworker entry point) ----------------------------------


def test_run_alert_sweep_uses_session_factory_and_applies_cooldown(engine):
    from sqlalchemy.orm import sessionmaker

    session_factory = sessionmaker(bind=engine)
    now = datetime.now(timezone.utc)
    with session_factory() as s:
        _seed_failed_outbox(s)

    sent_first = alerts.run_alert_sweep(session_factory, now=now)
    assert [a.kind for a in sent_first] == ["outbox_failed"]

    sent_second = alerts.run_alert_sweep(session_factory, now=now + timedelta(seconds=5))
    assert sent_second == []  # still cooling down
