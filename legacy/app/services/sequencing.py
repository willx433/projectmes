"""Dispatch read-models over WIP state (SD-1..SD-4). No separate dispatch table:
both the planner board and the operator station derive from wip_events + applied_routings.
"""
from .. import db
from . import wip


def _active_cells(conn, job_id):
    """Steps with qty currently in queue/run/to_move for this job."""
    st = wip.compute_state(conn, job_id)
    out = {}
    for step, d in st["steps"].items():
        active = d["queue"] + d["run"] + d["to_move"]
        if active > 1e-6:
            out[step] = {"queue": d["queue"], "run": d["run"], "to_move": d["to_move"],
                         "active": active, "held": wip.held_qty(conn, job_id, step)}
    return out


def _sort_key(row):
    # override rank first (SD-5), then due-date, then priority number (lower=urgent)
    return (row["rank"] if row["rank"] is not None else 1_000_000,
            row["due_date"] or "9999-12-31",
            row["priority"])


def dispatch_board(conn):
    """All work centers -> ordered list of {job, step, op, qty cells}. Sorted per WC."""
    jobs = db.query(conn, "SELECT * FROM jobs WHERE released=1")
    by_wc = {}
    for j in jobs:
        cells = _active_cells(conn, j["id"])
        if not cells:
            continue
        ov = db.one(conn, "SELECT rank FROM dispatch_overrides WHERE job_id=?", (j["id"],))
        rank = ov["rank"] if ov else None
        ops = {r["step_number"]: r for r in db.query(
            conn, "SELECT * FROM applied_routings WHERE job_id=?", (j["id"],))}
        for step, c in cells.items():
            op = ops.get(step)
            if not op:
                continue
            item = {"job_id": j["id"], "jb2_job_number": j["jb2_job_number"],
                    "part_number": j["part_number"], "due_date": j["due_date"],
                    "priority": j["priority"], "rank": rank, "step": step,
                    "operation_name": op["operation_name"], "work_center": op["work_center"],
                    **c}
            by_wc.setdefault(op["work_center"], []).append(item)
    for wc in by_wc:
        by_wc[wc].sort(key=_sort_key)
    return dict(sorted(by_wc.items()))


def station(conn, work_center):
    """One work center's queue (SD-2)."""
    return dispatch_board(conn).get(work_center, [])


def work_centers(conn):
    rows = db.query(conn, "SELECT DISTINCT work_center FROM applied_routings ORDER BY work_center")
    return [r["work_center"] for r in rows]
