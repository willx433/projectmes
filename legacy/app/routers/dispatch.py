"""Phase 4 — Scheduling & dispatch read-models. SD-1..SD-6."""
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse

from .. import db
from ..deps import get_conn, render
from ..services import sequencing

router = APIRouter()


@router.get("/dispatch")
def board(request: Request, conn=Depends(get_conn)):
    return render(request, "dispatch.html", nav="dispatch", board=sequencing.dispatch_board(conn))


@router.get("/stations")
def stations(request: Request, conn=Depends(get_conn)):
    wcs = sequencing.work_centers(conn)
    board = sequencing.dispatch_board(conn)
    counts = {wc: len(board.get(wc, [])) for wc in wcs}
    return render(request, "stations.html", nav="stations", work_centers=wcs, counts=counts)


@router.get("/stations/{wc}")
def station(wc: str, request: Request, conn=Depends(get_conn), error: str = "", msg: str = ""):
    items = sequencing.station(conn, wc)
    # overlay instructions per item (active op)
    from .wip import _instruction_overlay
    for it in items:
        ov = _instruction_overlay(conn, it["job_id"])
        it["instructions"] = ov.get(it["step"], [])
    return render(request, "station.html", nav="stations", wc=wc, items=items, error=error, msg=msg)


@router.post("/dispatch/override")
def override(job_id: int = Form(...), rank: int = Form(...), actor: str = Form(...),
             conn=Depends(get_conn)):
    conn.execute("INSERT INTO dispatch_overrides(job_id, rank, actor, created_at) VALUES (?,?,?,?) "
                 "ON CONFLICT(job_id) DO UPDATE SET rank=excluded.rank, actor=excluded.actor",
                 (job_id, rank, actor, db.now()))
    conn.commit()
    return RedirectResponse("/dispatch", status_code=303)
