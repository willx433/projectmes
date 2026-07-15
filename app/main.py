"""FastAPI app factory (DD §16.3, N5)."""
from __future__ import annotations

import subprocess

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

# P2-10: no API routes yet, but importing registers work_orders/units/
# plan_operations/plan_pdfs on the shared Base.metadata -- jb2_outbox.work_order_id
# now carries a real FK to work_orders.id (migration 0006), so anything that
# creates the full jb2 mirror schema (tests, tools) needs this table present too.
import app.domain.models_execution  # noqa: F401,E402
import app.domain.models_floor  # noqa: F401,E402 -- P3-03: operators/stations/auth_events
from app.api.auth import router as auth_router
from app.api.display import router as display_router
from app.api.health import router as health_router
from app.api.library import router as library_router
from app.api.media import router as media_router
from app.api.products import router as products_router
from app.api.scan import router as scan_router
from app.api.workorders import router as workorders_router
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
    app.state.version = version
    app.mount("/static", StaticFiles(directory=str(REPO_ROOT / "static")), name="static")
    app.include_router(health_router)
    app.include_router(display_router)
    app.include_router(products_router)
    app.include_router(library_router)
    app.include_router(media_router)
    app.include_router(workorders_router)
    app.include_router(auth_router)
    app.include_router(scan_router)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok", "version": version}

    return app


app = create_app()
