"""WIP move engine. Current state is DERIVED from the append-only wip_events log (PT-3/4).

intra steps: queue -> run -> to_move, then advance to next step's queue. scrap/reject terminal.
A move event subtracts qty from (from_step, from_intra) and adds to (to_step, to_intra).
"""
from .. import db

INTRA = ("queue", "run", "to_move", "scrap", "reject")
_EPS = 1e-6


class MoveError(ValueError):
    """Illegal move (insufficient qty, held qty, bad step)."""


def _events(conn, job_id):
    return db.query(conn, "SELECT * FROM wip_events WHERE job_id=? ORDER BY id", (job_id,))


def held_qty(conn, job_id, step):
    """Qty held by open/under_review NCRs at this step (QA-2)."""
    row = db.one(
        conn,
        "SELECT COALESCE(SUM(qty),0) AS h FROM ncrs "
        "WHERE job_id=? AND step_number=? AND state IN ('open','under_review')",
        (job_id, step),
    )
    return row["h"] or 0.0


def compute_state(conn, job_id):
    """Return {step: {intra: qty}} plus per-step entered/left, and lifecycle."""
    steps = {}

    def cell(step, intra):
        return steps.setdefault(step, {k: 0.0 for k in INTRA} | {"_entered": 0.0, "_left": 0.0})[intra]

    def add(step, intra, q):
        d = steps.setdefault(step, {k: 0.0 for k in INTRA} | {"_entered": 0.0, "_left": 0.0})
        d[intra] += q

    for e in _events(conn, job_id):
        fs, fi, ts, ti, q = e["from_step"], e["from_intra"], e["to_step"], e["to_intra"], e["qty"]
        if fs is not None and fi:
            add(fs, fi, -q)
            if ts != fs or ti in ("scrap", "reject"):
                add(fs, "_left", q)
        if ts is not None and ti:
            add(ts, ti, q)
            if fs != ts or fs is None:
                add(ts, "_entered", q)

    released = any(e["kind"] == "release" for e in _events(conn, job_id))
    lifecycle = "not_released"
    if released:
        # complete when no qty remains in any non-terminal intra across all steps
        active = sum(
            d[i] for d in steps.values() for i in ("queue", "run", "to_move")
        )
        lifecycle = "completed" if active <= _EPS else "in_progress"
    return {"steps": steps, "lifecycle": lifecycle}


def step_cells(conn, job_id, step, state=None):
    """Active intra balances at a step, in flow order. Used to distribute disposition qty."""
    state = state or compute_state(conn, job_id)
    d = state["steps"].get(step, {})
    return [(i, d.get(i, 0.0)) for i in ("queue", "run", "to_move") if d.get(i, 0.0) > _EPS]


def remaining(conn, job_id, step, state=None):
    """Qty at a step not yet sent onward (entered - left)."""
    state = state or compute_state(conn, job_id)
    d = state["steps"].get(step)
    return (d["_entered"] - d["_left"]) if d else 0.0


def record_event(conn, job_id, kind, qty, actor, *, from_step=None, from_intra=None,
                 to_step=None, to_intra=None, reason=None, bypass_hold=False):
    if qty <= 0:
        raise MoveError("qty must be positive")
    state = compute_state(conn, job_id)

    if from_step is not None and from_intra:
        bal = state["steps"].get(from_step, {}).get(from_intra, 0.0)
        if qty - bal > _EPS:
            raise MoveError(
                f"only {bal:g} in step {from_step}/{from_intra}, cannot move {qty:g}")

        is_outflow = (to_step != from_step) or (to_intra in ("scrap", "reject"))
        if is_outflow and not bypass_hold:
            movable = remaining(conn, job_id, from_step, state) - held_qty(conn, job_id, from_step)
            if qty - movable > _EPS:
                raise MoveError(
                    f"{held_qty(conn, job_id, from_step):g} held by NCR at step {from_step}; "
                    f"only {max(movable,0):g} movable")

    conn.execute(
        "INSERT INTO wip_events"
        "(job_id, kind, from_step, from_intra, to_step, to_intra, qty, actor, reason, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        (job_id, kind, from_step, from_intra, to_step, to_intra, qty, actor, reason, db.now()),
    )
    conn.commit()


# --- high-level shop-floor actions -------------------------------------------------

def release(conn, job_id, qty, actor):
    if db.one(conn, "SELECT 1 FROM wip_events WHERE job_id=? AND kind='release'", (job_id,)):
        raise MoveError("job already released")
    first = db.one(conn,
                   "SELECT MIN(step_number) AS s FROM applied_routings WHERE job_id=?", (job_id,))
    if not first or first["s"] is None:
        raise MoveError("job has no applied routing to release against")
    record_event(conn, job_id, "release", qty, actor, to_step=first["s"], to_intra="queue")
    conn.execute("UPDATE jobs SET released=1, status='open' WHERE id=?", (job_id,))
    conn.commit()


def next_step(conn, job_id, step):
    row = db.one(conn,
                 "SELECT MIN(step_number) AS s FROM applied_routings WHERE job_id=? AND step_number>?",
                 (job_id, step))
    return row["s"] if row else None


def advance(conn, job_id, step, qty, actor):
    """Move qty from this step's to_move into the next step's queue (or complete)."""
    nxt = next_step(conn, job_id, step)
    if nxt is None:
        # final step: to_move stays as completed output; nothing to advance to
        raise MoveError("final step — complete via to_move; nothing to advance to")
    record_event(conn, job_id, "advance", qty, actor,
                 from_step=step, from_intra="to_move", to_step=nxt, to_intra="queue")
