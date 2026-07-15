"""Environment config, loaded from real env vars with a repo-root .env fallback.

Exact keys (per IMPLEMENTATION_PLAN.md P1-01 / DD §16.3), never renamed:
JobBoss2__ApiBaseUrl, JobBoss2__AuthBaseUrl, JobBoss2__ClientId,
JobBoss2__ClientSecret, DATABASE_URL, MES_SECRET_KEY, ARTIFACT_DIR,
LIBRARY_REQUIRE_APPROVER (P2-03, DD §7.2 two-person publish gate; default
false = single-approver per plan §9), STATION_SESSION_IDLE_MIN (P3-03, DD
§14 "expires after configurable idle, e.g. 10 min"; default 10 -- P3-08/09
reuses this same knob as the no-substep-activity threshold before a
WorkSession auto-pauses, docs/state-machine.md §6),
KITUP_REQUIRES_LEAD (P3-11, docs/state-machine.md §8: "kit-up allowed by
operator role (config KITUP_REQUIRES_LEAD, default false)"),
SESSION_AUTO_CLOSE_MIN (P3-09, §17.8 "auto-close after ~60 min paused";
default 60), REQUIRE_SERIAL_BEFORE_DONE (P3-10, DD §6.8 "serial must be
present before `done`"; default true, global for v1 -- per-product
override deferred, see P3-10 task report).

DASHBOARD_TV_TOKEN (P4-01/02, DD §13.1 "renders read-only on TVs" -- optional
shared secret a wall-mounted browser puts in `?tv=`/cookie to get the
nav-less kiosk view; unset means anyone can request tv mode, fine for an
internal LAN-only board with no mutations on the page at all).
DASHBOARD_QUEUE_DWELL_HRS / DASHBOARD_STALLED_HRS (P4-02, DD §13.1 "amber =
queue dwell over threshold" -- two thresholds for the same amber `stalled`
card state: the stricter one (default 2h) applies when nobody has an open
WorkSession on the unit (it's just sitting); the looser one (default 4h)
applies when an operator IS badged in but hasn't finished in a long time.

ALERT_EMAIL_TO / ALERT_SMTP_HOST / ALERT_SMTP_PORT / ALERT_SMTP_USER /
ALERT_SMTP_PASS / ALERT_COOLDOWN_MIN (P4-05, DD N5: "alert (email) on:
outbox failures, sync stalls > 10 min, station offline > 15 min" --
app/ops/alerts.py). All optional; ALERT_EMAIL_TO/ALERT_SMTP_HOST unset means
log-only, the dev default -- see that module's docstring. ALERT_COOLDOWN_MIN
default 30 (minutes between re-sends of the same alert signature).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, fields
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> dict[str, str]:
    # ponytail: hand-rolled parser, no python-dotenv dep for ~10 lines of work.
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()
    return values


@dataclass(frozen=True)
class Config:
    jobboss2_api_base_url: str | None
    jobboss2_auth_base_url: str | None
    jobboss2_client_id: str | None
    jobboss2_client_secret: str | None
    database_url: str | None
    mes_secret_key: str | None
    artifact_dir: str | None
    sync_backfill_days: int
    library_require_approver: bool
    station_session_idle_min: int
    kitup_requires_lead: bool
    session_auto_close_min: int
    require_serial_before_done: bool
    dashboard_tv_token: str | None
    dashboard_queue_dwell_hrs: float
    dashboard_stalled_hrs: float
    alert_email_to: str | None
    alert_smtp_host: str | None
    alert_smtp_port: int
    alert_smtp_user: str | None
    alert_smtp_pass: str | None
    alert_cooldown_min: int

    def validate(self, required: list[str]) -> None:
        """Raise if any of the given attribute names are unset. Endpoints/workers
        call this for the keys they actually need — importing the module must
        never fail just because the dev box has an incomplete .env."""
        missing = [name for name in required if getattr(self, name, None) is None]
        if missing:
            raise RuntimeError(f"Missing required config: {', '.join(missing)}")

    def __repr__(self) -> str:  # never print secret values
        redacted = ", ".join(
            f"{f.name}=<set>" if getattr(self, f.name) else f"{f.name}=None"
            for f in fields(self)
        )
        return f"Config({redacted})"


def load_config(env_path: Path | None = None) -> Config:
    dotenv = _load_dotenv(env_path if env_path is not None else REPO_ROOT / ".env")

    def get(key: str) -> str | None:
        return os.environ.get(key, dotenv.get(key)) or None

    raw_backfill_days = get("SYNC_BACKFILL_DAYS")
    sync_backfill_days = int(raw_backfill_days) if raw_backfill_days else 30
    library_require_approver = (get("LIBRARY_REQUIRE_APPROVER") or "false").strip().lower() in (
        "1",
        "true",
        "yes",
    )
    raw_idle_min = get("STATION_SESSION_IDLE_MIN")
    station_session_idle_min = int(raw_idle_min) if raw_idle_min else 10
    kitup_requires_lead = (get("KITUP_REQUIRES_LEAD") or "false").strip().lower() in (
        "1",
        "true",
        "yes",
    )
    raw_auto_close_min = get("SESSION_AUTO_CLOSE_MIN")
    session_auto_close_min = int(raw_auto_close_min) if raw_auto_close_min else 60
    require_serial_before_done = (get("REQUIRE_SERIAL_BEFORE_DONE") or "true").strip().lower() in (
        "1",
        "true",
        "yes",
    )
    raw_queue_dwell_hrs = get("DASHBOARD_QUEUE_DWELL_HRS")
    dashboard_queue_dwell_hrs = float(raw_queue_dwell_hrs) if raw_queue_dwell_hrs else 2.0
    raw_stalled_hrs = get("DASHBOARD_STALLED_HRS")
    dashboard_stalled_hrs = float(raw_stalled_hrs) if raw_stalled_hrs else 4.0
    raw_alert_smtp_port = get("ALERT_SMTP_PORT")
    alert_smtp_port = int(raw_alert_smtp_port) if raw_alert_smtp_port else 587
    raw_alert_cooldown_min = get("ALERT_COOLDOWN_MIN")
    alert_cooldown_min = int(raw_alert_cooldown_min) if raw_alert_cooldown_min else 30

    return Config(
        jobboss2_api_base_url=get("JobBoss2__ApiBaseUrl"),
        jobboss2_auth_base_url=get("JobBoss2__AuthBaseUrl"),
        jobboss2_client_id=get("JobBoss2__ClientId"),
        jobboss2_client_secret=get("JobBoss2__ClientSecret"),
        database_url=get("DATABASE_URL"),
        mes_secret_key=get("MES_SECRET_KEY"),
        # ponytail: dev fallback so a fresh checkout can upload/serve media
        # without an .env — P2-06 needs a real default, not None.
        artifact_dir=get("ARTIFACT_DIR") or str(REPO_ROOT / "artifacts"),
        sync_backfill_days=sync_backfill_days,
        library_require_approver=library_require_approver,
        station_session_idle_min=station_session_idle_min,
        kitup_requires_lead=kitup_requires_lead,
        session_auto_close_min=session_auto_close_min,
        require_serial_before_done=require_serial_before_done,
        dashboard_tv_token=get("DASHBOARD_TV_TOKEN"),
        dashboard_queue_dwell_hrs=dashboard_queue_dwell_hrs,
        dashboard_stalled_hrs=dashboard_stalled_hrs,
        alert_email_to=get("ALERT_EMAIL_TO"),
        alert_smtp_host=get("ALERT_SMTP_HOST"),
        alert_smtp_port=alert_smtp_port,
        alert_smtp_user=get("ALERT_SMTP_USER"),
        alert_smtp_pass=get("ALERT_SMTP_PASS"),
        alert_cooldown_min=alert_cooldown_min,
    )


config = load_config()
