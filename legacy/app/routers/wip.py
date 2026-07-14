"""Phase 3 — Production tracking & WIP. PT-1..PT-8, WI-4/5/6."""
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse

from .. import db
from ..deps import get_conn, render
from ..services import wip

router = APIRouter()


def _instruction_overlay(conn, job_id):
    """Attach MES work instructions to the JB2 applied-routing ops, matched by op code.
    Job is revision-locked to the published routing for its model (WI-4)."""
    job = db.one(conn, "SELECT * FROM jobs WHERE id=?", (job_id,))
    overlay = {}
    if not job or job["model_id"] is None:
        return overlay
    routing = db.one(conn,
        "SELECT id FROM routings WHERE model_id=? AND status='published' "
        "ORDER BY version DESC LIMIT 1", (job["model_id"],))
    if not routing:
        return overlay
    for s in db.query(conn, "SELECT step_number, operation_code FROM applied_routings WHERE job_id=?", (job_id,)):
        ro = db.one(conn,
            "SELECT ro.id FROM routing_operations ro JOIN operations o ON o.id=ro.operation_id "
            "WHERE ro.routing_id=? AND o.code=?", (routing["id"], s["operation_code"]))
        if ro:
            overlay[s["step_number"]] = db.query(conn,
                "SELECT * FROM work_instructions WHERE routing_operation_id=? ORDER BY step_number",
                (ro["id"],))
    return overlay


@router.get("/jobs/{job_id}")
def job_detail(job_id: int, request: Request, conn=Depends(get_conn),
               error: str = "", msg: str = ""):
    job = db.one(conn, "SELECT * FROM jobs WHERE id=?", (job_id,))
    if not job:
        return RedirectResponse("/jobs", status_code=303)
    steps = db.query(conn, "SELECT * FROM applied_routings WHERE job_id=? ORDER BY step_number", (job_id,))
    state = wip.compute_state(conn, job_id)
    overlay = _instruction_overlay(conn, job_id)
    held = {s["step_number"]: wip.held_qty(conn, job_id, s["step_number"]) for s in steps}
    acks = {(a["step_number"], a["instruction_id"]) for a in
            db.query(conn, "SELECT step_number, instruction_id FROM instruction_acks WHERE job_id=?", (job_id,))}
    events = db.query(conn, "SELECT * FROM wip_events WHERE job_id=? ORDER BY id DESC", (job_id,))
    ncrs = db.query(conn, "SELECT * FROM ncrs WHERE job_id=? ORDER BY id DESC", (job_id,))
    return render(request, "job_detail.html", nav="jobs", job=job, steps=steps, state=state,
                  overlay=overlay, held=held, acks=acks, events=events, ncrs=ncrs,
                  intra=wip.INTRA, error=error, msg=msg)


def _back(job_id, redirect, error="", msg=""):
    url = redirect or f"/jobs/{job_id}"
    sep = "&" if "?" in url else "?"
    if error:
        url = f"{url}{sep}error={error.replace(' ', '+')}"
    elif msg:
        url = f"{url}{sep}msg={msg.replace(' ', '+')}"
    return RedirectResponse(url, status_code=303)


@router.post("/jobs/{job_id}/release")
def release(job_id: int, qty: float = Form(...), actor: str = Form(...),
            redirect: str = Form(""), conn=Depends(get_conn)):
    try:
        wip.release(conn, job_id, qty, actor)
    except wip.MoveError as e:
        return _back(job_id, redirect, error=str(e))
    return _back(job_id, redirect, msg="Released")


@router.post("/jobs/{job_id}/move")
def move(job_id: int, step: int = Form(...), from_intra: str = Form(...),
         to_intra: str = Form(...), qty: float = Form(...), actor: str = Form(...),
         redirect: str = Form(""), conn=Depends(get_conn)):
    try:
        wip.record_event(conn, job_id, "move", qty, actor, from_step=step,
                         from_intra=from_intra, to_step=step, to_intra=to_intra)
    except wip.MoveError as e:
        return _back(job_id, redirect, error=str(e))
    return _back(job_id, redirect, msg="Moved")


@router.post("/jobs/{job_id}/advance")
def advance(job_id: int, step: int = Form(...), qty: float = Form(...),
            actor: str = Form(...), redirect: str = Form(""), conn=Depends(get_conn)):
    try:
        wip.advance(conn, job_id, step, qty, actor)
    except wip.MoveError as e:
        return _back(job_id, redirect, error=str(e))
    return _back(job_id, redirect, msg="Advanced")


@router.post("/jobs/{job_id}/ack")
def ack(job_id: int, step: int = Form(...), instruction_id: int = Form(...),
        actor: str = Form(...), redirect: str = Form(""), conn=Depends(get_conn)):
    # out-of-sequence guard (WI-6): lower-numbered required steps on this op must be acked first
    wi = db.one(conn, "SELECT * FROM work_instructions WHERE id=?", (instruction_id,))
    prior_unacked = db.query(conn,
        "SELECT w.id FROM work_instructions w WHERE w.routing_operation_id=? AND w.step_number<? "
        "AND w.required=1 AND w.id NOT IN (SELECT instruction_id FROM instruction_acks WHERE job_id=? AND step_number=?)",
        (wi["routing_operation_id"], wi["step_number"], job_id, step))
    if prior_unacked:
        conn.execute("INSERT INTO deviations(job_id, step_number, kind, detail, actor, created_at) "
                     "VALUES (?,?, 'out_of_sequence', ?, ?, ?)",
                     (job_id, step, f"acked instr {instruction_id} before earlier required step", actor, db.now()))
    conn.execute("INSERT OR IGNORE INTO instruction_acks(job_id, step_number, instruction_id, actor, created_at)"
                 " VALUES (?,?,?,?,?)", (job_id, step, instruction_id, actor, db.now()))
    conn.commit()
    return _back(job_id, redirect, msg="Acknowledged")
