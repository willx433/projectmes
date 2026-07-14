"""Environment config, loaded from real env vars with a repo-root .env fallback.

Exact keys (per IMPLEMENTATION_PLAN.md P1-01 / DD §16.3), never renamed:
JobBoss2__ApiBaseUrl, JobBoss2__AuthBaseUrl, JobBoss2__ClientId,
JobBoss2__ClientSecret, DATABASE_URL, MES_SECRET_KEY, ARTIFACT_DIR.
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

    return Config(
        jobboss2_api_base_url=get("JobBoss2__ApiBaseUrl"),
        jobboss2_auth_base_url=get("JobBoss2__AuthBaseUrl"),
        jobboss2_client_id=get("JobBoss2__ClientId"),
        jobboss2_client_secret=get("JobBoss2__ClientSecret"),
        database_url=get("DATABASE_URL"),
        mes_secret_key=get("MES_SECRET_KEY"),
        artifact_dir=get("ARTIFACT_DIR"),
        sync_backfill_days=sync_backfill_days,
    )


config = load_config()
