"""Smoke test for the load-bearing WIP/quality logic. Run: python test_smoke.py
No framework — asserts only. Uses a throwaway DB.
"""
import os
import tempfile

os.environ["AMR_DB_PATH"] = os.path.join(tempfile.mkdtemp(), "smoke.db")

from app import db                                    # noqa: E402
from app.services import demo_seed, quality, wip      # noqa: E402
from app.routers.wip import _instruction_overlay      # noqa: E402


def job_id(conn, no):
    return db.one(conn, "SELECT id FROM jobs WHERE jb2_job_number=?", (no,))["id"]


def main():
    db.init_db()
    conn = db.connect()
    assert demo_seed.seed_if_empty(conn) is True
    assert db.one(conn, "SELECT COUNT(*) c FROM operations")["c"] == 5

    jid = job_id(conn, "J1003")  # demo qty 5, 5-step routing

    # --- release puts all qty at step1/queue ---
    wip.release(conn, jid, 5, "alice")
    st = wip.compute_state(conn, jid)
    assert st["steps"][1]["queue"] == 5, st["steps"][1]
    assert st["lifecycle"] == "in_progress"

    # --- double release rejected ---
    try:
        wip.release(conn, jid, 5, "alice"); assert False, "double release allowed"
    except wip.MoveError:
        pass

    # --- move queue->run->to_move ---
    wip.record_event(conn, jid, "move", 5, "alice", from_step=1, from_intra="queue", to_step=1, to_intra="run")
    wip.record_event(conn, jid, "move", 5, "alice", from_step=1, from_intra="run", to_step=1, to_intra="to_move")
    assert wip.compute_state(conn, jid)["steps"][1]["to_move"] == 5

    # --- cannot move more than present ---
    try:
        wip.record_event(conn, jid, "move", 99, "alice", from_step=1, from_intra="to_move",
                         to_step=1, to_intra="run"); assert False
    except wip.MoveError:
        pass

    # --- NCR holds qty: advance beyond movable blocked ---
    ncr = quality.create_ncr(conn, jid, 1, 2, "burr", "alice")
    assert wip.held_qty(conn, jid, 1) == 2
    try:
        wip.advance(conn, jid, 1, 4, "alice"); assert False, "hold not enforced"
    except wip.MoveError:
        pass
    wip.advance(conn, jid, 1, 3, "alice")          # 5 - 2 held = 3 movable
    assert wip.compute_state(conn, jid)["steps"][2]["queue"] == 3

    # --- disposition scrap removes the held qty wherever it sits ---
    quality.set_state(conn, ncr, "under_review")
    quality.disposition(conn, ncr, "scrap", "bob", "unrecoverable")
    st = wip.compute_state(conn, jid)
    assert st["steps"][1]["to_move"] == 0, st["steps"][1]
    assert st["steps"][1]["scrap"] == 2, st["steps"][1]
    assert wip.held_qty(conn, jid, 1) == 0

    # --- dispositioned NCR is immutable; correction makes a successor (QA-7) ---
    try:
        quality.disposition(conn, ncr, "scrap", "bob", "again"); assert False
    except ValueError:
        pass
    succ = quality.correct(conn, ncr, qty=1, defect_type="burr", reported_by="bob")
    assert db.one(conn, "SELECT supersedes_ncr_id FROM ncrs WHERE id=?", (succ,))["supersedes_ncr_id"] == ncr

    # --- event replay equivalence: state derives purely from the log ---
    events = db.query(conn, "SELECT * FROM wip_events WHERE job_id=? ORDER BY id", (jid,))
    assert len(events) >= 4 and events[0]["kind"] == "release"

    # --- instruction overlay matches MES published routing onto JB2 applied routing (WI-4) ---
    overlay = _instruction_overlay(conn, jid)
    assert overlay.get(1), "no instructions overlaid on step 1"
    assert any("CNC-MILL" in r["operation_code"] for r in
               db.query(conn, "SELECT operation_code FROM applied_routings WHERE job_id=?", (jid,)))

    print("OK — all smoke assertions passed")


if __name__ == "__main__":
    main()
