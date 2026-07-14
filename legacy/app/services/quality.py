"""NCR workflow (QA-1..QA-7). Flag -> hold -> disposition -> downstream WIP action.
Held qty is enforced in wip.record_event via held_qty(). Disposition emits WIP events
with bypass_hold=True (the disposition IS the resolution of the hold).
"""
from .. import db
from . import wip

STATES = ("open", "under_review", "dispositioned")
DISPOSITIONS = ("rework", "scrap", "use_as_is", "return_to_vendor", "regrade")
_LEGAL = {"open": {"under_review", "dispositioned"}, "under_review": {"dispositioned"}}


def create_ncr(conn, job_id, step_number, qty, defect_type, reported_by,
               part_number=None, detail=None):
    if qty <= 0:
        raise ValueError("qty must be positive")
    avail = wip.remaining(conn, job_id, step_number) - wip.held_qty(conn, job_id, step_number)
    if qty - avail > 1e-6:
        raise ValueError(f"only {max(avail,0):g} unheld qty at step {step_number}")
    cur = conn.execute(
        "INSERT INTO ncrs(job_id, step_number, part_number, qty, defect_type, detail, "
        "state, reported_by, created_at) VALUES (?,?,?,?,?,?, 'open', ?, ?)",
        (job_id, step_number, part_number, qty, defect_type, detail, reported_by, db.now()),
    )
    conn.commit()
    return cur.lastrowid


def set_state(conn, ncr_id, new_state):
    ncr = db.one(conn, "SELECT * FROM ncrs WHERE id=?", (ncr_id,))
    if not ncr:
        raise ValueError("no such NCR")
    if new_state not in _LEGAL.get(ncr["state"], set()):
        raise ValueError(f"illegal transition {ncr['state']} -> {new_state}")
    conn.execute("UPDATE ncrs SET state=? WHERE id=?", (new_state, ncr_id))
    conn.commit()


def disposition(conn, ncr_id, disposition, reviewer, justification, rework_to_step=None):
    ncr = db.one(conn, "SELECT * FROM ncrs WHERE id=?", (ncr_id,))
    if not ncr:
        raise ValueError("no such NCR")
    if ncr["state"] == "dispositioned":
        raise ValueError("NCR already dispositioned (immutable; create a successor) ")
    if disposition not in DISPOSITIONS:
        raise ValueError(f"unknown disposition {disposition}")
    if not justification:
        raise ValueError("justification required")

    job_id, step, qty = ncr["job_id"], ncr["step_number"], ncr["qty"]

    def _distribute(kind, to_step, to_intra, reason):
        # consume `qty` from the step's active intra cells (queue/run/to_move)
        need = qty
        for intra, bal in wip.step_cells(conn, job_id, step):
            if need <= 1e-6:
                break
            take = min(need, bal)
            wip.record_event(conn, job_id, kind, take, reviewer, from_step=step,
                             from_intra=intra, to_step=to_step, to_intra=to_intra,
                             reason=reason, bypass_hold=True)
            need -= take

    if disposition == "scrap":
        _distribute("scrap", step, "scrap", f"NCR#{ncr_id} scrap")
    elif disposition == "rework":
        dest = rework_to_step if rework_to_step is not None else step
        _distribute("rework_return", dest, "queue", f"NCR#{ncr_id} rework")
    # use_as_is / return_to_vendor / regrade: releasing the hold is enough (qty stays,
    # NCR no longer open so held_qty drops -> qty becomes movable). RTV physically leaves
    # but with no JB2 write-back yet we record the disposition only (INT-9 future).

    conn.execute(
        "UPDATE ncrs SET state='dispositioned', disposition=?, reviewer=?, justification=?, "
        "rework_to_step=?, dispositioned_at=? WHERE id=?",
        (disposition, reviewer, justification, rework_to_step, db.now(), ncr_id),
    )
    conn.commit()


def correct(conn, ncr_id, **kw):
    """Closed NCRs are immutable (QA-7): a correction is a NEW linked NCR."""
    old = db.one(conn, "SELECT * FROM ncrs WHERE id=?", (ncr_id,))
    if not old:
        raise ValueError("no such NCR")
    cur = conn.execute(
        "INSERT INTO ncrs(job_id, step_number, part_number, qty, defect_type, detail, "
        "state, supersedes_ncr_id, reported_by, created_at) "
        "VALUES (?,?,?,?,?,?, 'open', ?, ?, ?)",
        (old["job_id"], old["step_number"], kw.get("part_number", old["part_number"]),
         kw.get("qty", old["qty"]), kw.get("defect_type", old["defect_type"]),
         kw.get("detail", old["detail"]), ncr_id, kw.get("reported_by", "correction"), db.now()),
    )
    conn.commit()
    return cur.lastrowid
