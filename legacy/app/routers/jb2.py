"""Phase 2 — JB2 read-only mirror surface. INT-1,2,5,6,7."""
from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse

from .. import config, db
from ..deps import get_conn, render
from ..services import jb2_adapter, wip

router = APIRouter()


@router.get("/jobs")
def jobs(request: Request, conn=Depends(get_conn)):
    rows = db.query(conn, "SELECT * FROM jobs ORDER BY released, priority, due_date")
    jobs = []
    for j in rows:
        st = wip.compute_state(conn, j["id"]) if j["released"] else None
        jobs.append({**dict(j), "lifecycle": st["lifecycle"] if st else "not_released"})
    return render(request, "jobs.html", nav="jobs", jobs=jobs, jb2_enabled=config.JB2_ENABLED)


@router.post("/jobs/sync")
def sync(request: Request, conn=Depends(get_conn)):
    if not config.JB2_ENABLED:
        return RedirectResponse("/jobs?msg=JB2+not+configured+(demo+data+only)", status_code=303)
    n = jb2_adapter.sync_jobs(conn)
    return RedirectResponse(f"/jobs?msg=Synced+{n}+jobs+from+JB2", status_code=303)


@router.get("/reconciliation")
def reconciliation(request: Request, conn=Depends(get_conn)):
    rows = db.query(conn, "SELECT * FROM reconciliation_log ORDER BY id DESC LIMIT 200")
    return render(request, "reconciliation.html", nav="reconciliation", rows=rows)
