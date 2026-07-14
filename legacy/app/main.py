"""Atlas MES (Make Ready) — FastAPI app entrypoint."""
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from . import config, db
from .deps import get_conn, render
from .routers import dispatch, jb2, master, quality, wip
from .services import demo_seed


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    conn = db.connect()
    try:
        demo_seed.seed_if_empty(conn)
    finally:
        conn.close()
    yield


app = FastAPI(title="Atlas Make Ready MES", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(Path(__file__).with_name("static"))), name="static")

app.include_router(master.router)
app.include_router(jb2.router)
app.include_router(wip.router)
app.include_router(dispatch.router)
app.include_router(quality.router)


@app.get("/healthz")
def healthz():
    return {"status": "ok", "jb2_enabled": config.JB2_ENABLED}


@app.get("/")
def dashboard(request: Request):
    return render(request, "dashboard.html", nav="dashboard")


# --- JSON endpoints the seed dashboard fetches ---
@app.get("/api/models")
def api_models(conn=Depends(get_conn)):
    return [dict(r) for r in db.query(conn, "SELECT * FROM models ORDER BY id DESC")]


@app.get("/api/operations")
def api_operations(conn=Depends(get_conn)):
    return [dict(r) for r in db.query(conn, "SELECT * FROM operations ORDER BY id DESC")]


@app.get("/api/routings")
def api_routings(conn=Depends(get_conn)):
    rows = db.query(conn,
        "SELECT r.*, m.name AS model_name, m.type AS model_type "
        "FROM routings r JOIN models m ON m.id=r.model_id ORDER BY r.id DESC")
    return [dict(r) for r in rows]


@app.get("/api/line-balances")
def api_line_balances(conn=Depends(get_conn)):
    rows = db.query(conn,
        "SELECT lb.*, r.name AS routing_name FROM line_balances lb "
        "JOIN routings r ON r.id=lb.routing_id ORDER BY lb.id DESC")
    return [dict(r) for r in rows]
