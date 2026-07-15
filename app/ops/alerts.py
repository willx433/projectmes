"""Alerts (P4-05) -- DD N5: "alert (email) on: outbox failures, sync stalls
> 10 min, station offline > 15 min." Station heartbeat (§9.8) is the signal
the third check reads: `app/auth/deps.py`'s `require_station` stamps
`Station.last_seen_at` on every authenticated kiosk request (see that
module) -- no separate heartbeat endpoint/JS timer needed, a kiosk that's
actually in use is making requests constantly.

`check_alerts()` is a pure read -- three independent queries, one `Alert`
per distinct problem (never one row per outbox row/resource/station beyond
what's actually wrong -- that's the "no storms" requirement). `send_alert()`
does the actual notification + de-dup/cooldown so the same problem isn't
re-emailed every sweep.

Cooldown is a plain in-memory dict keyed by alert signature
(`kind:identifier`) -- ponytail: single-process cache, not a DB table. This
is correct for how this ships today: one `mes-floor` systemd unit calling
`run_alert_sweep` on a 60s timer (see app/floorworker/__main__.py), so there
is exactly one clock. It would under- or over-fire if this ever ran from
multiple worker processes (each keeps its own cooldown clock) -- a small,
real caveat, not a bug in the current single-process deployment. Upgrade
path if that ever changes: a tiny `alerts_sent(key, sent_at)` table with the
same cooldown check done as a DB read/write instead of a dict lookup.
"""
from __future__ import annotations

import logging
import smtplib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import config
from app.domain.models_floor import Station
from app.domain.models_jb2 import JB2Outbox, SyncRun
from app.sync.engine import to_utc

logger = logging.getLogger("app.ops.alerts")

SYNC_STALL_MIN = 10
STATION_OFFLINE_MIN = 15


@dataclass(frozen=True)
class Alert:
    kind: str  # "outbox_failed" | "sync_stalled" | "station_offline"
    key: str  # dedup/cooldown signature, unique per distinct problem
    message: str


# ponytail: single-process cooldown cache -- see module docstring.
_LAST_SENT: dict[str, datetime] = {}


def _outbox_alerts(session: Session) -> list[Alert]:
    failed = session.execute(select(JB2Outbox).where(JB2Outbox.status == "failed")).scalars().all()
    if not failed:
        return []
    example = next((r.last_error for r in failed if r.last_error), "unknown error")
    return [
        Alert(
            kind="outbox_failed",
            key="outbox_failed",
            message=f"{len(failed)} JB2 outbox row(s) failed/parked (e.g. {example})",
        )
    ]


def _sync_stall_alerts(session: Session, now: datetime) -> list[Alert]:
    resources = session.execute(select(SyncRun.resource).distinct()).scalars().all()
    alerts = []
    for resource in sorted(resources):
        latest_ok = session.execute(
            select(SyncRun)
            .where(
                SyncRun.resource == resource,
                SyncRun.error.is_(None),
                SyncRun.finished_at.is_not(None),
            )
            .order_by(SyncRun.finished_at.desc())
            .limit(1)
        ).scalar_one_or_none()
        if latest_ok is None:
            alerts.append(
                Alert(
                    kind="sync_stalled",
                    key=f"sync_stalled:{resource}",
                    message=f"sync '{resource}' has never completed successfully",
                )
            )
            continue
        age_s = (now - to_utc(latest_ok.finished_at)).total_seconds()
        if age_s > SYNC_STALL_MIN * 60:
            alerts.append(
                Alert(
                    kind="sync_stalled",
                    key=f"sync_stalled:{resource}",
                    message=(
                        f"sync '{resource}' has had no successful run in over "
                        f"{SYNC_STALL_MIN} min (last success {age_s / 60:.0f} min ago)"
                    ),
                )
            )
    return alerts


def _station_offline_alerts(session: Session, now: datetime) -> list[Alert]:
    stations = session.execute(select(Station).where(Station.active.is_(True))).scalars().all()
    alerts = []
    for station in stations:
        # ponytail: never-seen (freshly enrolled, not yet used) isn't an
        # offline regression -- nothing to alert on.
        if station.last_seen_at is None:
            continue
        age_s = (now - to_utc(station.last_seen_at)).total_seconds()
        if age_s > STATION_OFFLINE_MIN * 60:
            alerts.append(
                Alert(
                    kind="station_offline",
                    key=f"station_offline:{station.id}",
                    message=(
                        f"station '{station.name}' offline for {age_s / 60:.0f} min "
                        f"(last seen {to_utc(station.last_seen_at).isoformat()})"
                    ),
                )
            )
    return alerts


def check_alerts(session: Session, now: datetime | None = None) -> list[Alert]:
    """Read-only: every currently-true alert condition, one `Alert` per
    distinct problem. Callers decide whether/when to actually notify --
    see `send_alert`/`run_alert_sweep`."""
    now = now or datetime.now(timezone.utc)
    return [
        *_outbox_alerts(session),
        *_sync_stall_alerts(session, now),
        *_station_offline_alerts(session, now),
    ]


def _deliver_email(alert: Alert) -> None:
    if not config.alert_email_to or not config.alert_smtp_host:
        return  # dev default: log-only, no SMTP configured -- never crash
    msg = MIMEText(alert.message)
    msg["Subject"] = f"[Atlas MES] {alert.kind}"
    msg["From"] = config.alert_smtp_user or "mes-alerts@localhost"
    msg["To"] = config.alert_email_to
    recipients = [addr.strip() for addr in config.alert_email_to.split(",") if addr.strip()]
    try:
        with smtplib.SMTP(config.alert_smtp_host, config.alert_smtp_port, timeout=10) as smtp:
            if config.alert_smtp_user and config.alert_smtp_pass:
                smtp.starttls()
                smtp.login(config.alert_smtp_user, config.alert_smtp_pass)
            smtp.sendmail(msg["From"], recipients, msg.as_string())
    except (OSError, smtplib.SMTPException) as exc:
        # alerting must never crash the caller (the floorworker loop) --
        # log loudly and move on.
        logger.error(
            "alert_email_send_failed",
            extra={"alert_kind": alert.kind, "alert_key": alert.key, "error": str(exc)},
        )


def send_alert(alert: Alert, *, now: datetime | None = None) -> bool:
    """Log the alert always; email it too if SMTP is configured. Returns
    False (no-op) if this exact alert signature was already sent within
    ALERT_COOLDOWN_MIN -- the caller sweeps every ~60s but a real problem
    shouldn't re-page every sweep."""
    now = now or datetime.now(timezone.utc)
    cooldown = timedelta(minutes=max(config.alert_cooldown_min, 0))
    last = _LAST_SENT.get(alert.key)
    if last is not None and now - last < cooldown:
        return False

    logger.warning(
        "mes_alert",
        extra={"alert_kind": alert.kind, "alert_key": alert.key, "alert_message": alert.message},
    )
    _deliver_email(alert)
    _LAST_SENT[alert.key] = now
    return True


def run_alert_sweep(session_factory, now: datetime | None = None) -> list[Alert]:
    """Worker-loop entry point (`app/floorworker/__main__.py`): check, then
    send whatever isn't cooling down. Opens/closes its own session via
    `session_factory`, same pattern as `app.domain.sessions.run_idle_sweep`."""
    now = now or datetime.now(timezone.utc)
    session = session_factory()
    try:
        alerts = check_alerts(session, now)
    finally:
        session.close()
    return [alert for alert in alerts if send_alert(alert, now=now)]
