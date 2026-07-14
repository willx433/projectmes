"""FastAPI app factory (DD §16.3, N5)."""
from __future__ import annotations

import subprocess

from fastapi import FastAPI

from app.config import REPO_ROOT
from app.logging import configure_logging


def _read_version() -> str:
    try:
        result = subprocess.run(
            ["git", "describe", "--tags", "--always", "--dirty"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=5,
        )
        version = result.stdout.strip()
        return version if result.returncode == 0 and version else "dev"
    except (OSError, subprocess.SubprocessError):
        return "dev"


def create_app() -> FastAPI:
    configure_logging()
    version = _read_version()  # read once at startup, not per-request

    app = FastAPI(title="Atlas MES")

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok", "version": version}

    return app


app = create_app()
