"""Seed demo data so the app is fully demoable without a live JB2 connection.
Idempotent: only seeds when the DB is empty. Jobs are marked source='demo' (non-authoritative).
"""
from .. import db


def seed_if_empty(conn):
    if db.one(conn, "SELECT 1 FROM models LIMIT 1"):
        return False
    t = db.now()

    # --- models ---
    conn.execute("INSERT INTO models(name, type, created_at) VALUES ('Hyperion 9mm','active',?)", (t,))
    model_id = conn.execute("SELECT last_insert_rowid() AS i").fetchone()["i"]

    # --- operation catalog (work_center is the dispatch key) ---
    ops = [
        ("CNC-MILL", "Mill slide", "MILL", 30, 12),
        ("DEBURR", "Deburr & inspect edges", "BENCH", 5, 6),
        ("CERAKOTE", "Cerakote finish", "FINISH", 20, 25),
        ("ASSY", "Final assembly", "ASSEMBLY", 10, 18),
        ("FUNCTEST", "Function test & proof", "TEST", 5, 10),
    ]
    op_ids = {}
    for code, name, wc, setup, run in ops:
        conn.execute(
            "INSERT INTO operations(code, name, work_center, std_setup_min, std_run_min, created_at)"
            " VALUES (?,?,?,?,?,?)", (code, name, wc, setup, run, t))
        op_ids[code] = conn.execute("SELECT last_insert_rowid() AS i").fetchone()["i"]

    # --- routing v1 (published) with ordered operations + work instructions ---
    conn.execute(
        "INSERT INTO routings(name, model_id, version, status, created_at) "
        "VALUES ('Hyperion 9mm Build', ?, 1, 'published', ?)", (model_id, t))
    routing_id = conn.execute("SELECT last_insert_rowid() AS i").fetchone()["i"]
    seq = ["CNC-MILL", "DEBURR", "CERAKOTE", "ASSY", "FUNCTEST"]
    for step, code in enumerate(seq, start=1):
        conn.execute(
            "INSERT INTO routing_operations(routing_id, step_number, operation_id) VALUES (?,?,?)",
            (routing_id, step, op_ids[code]))
        ro_id = conn.execute("SELECT last_insert_rowid() AS i").fetchone()["i"]
        for i, txt in enumerate([f"Set up {code} fixture", f"Run {code}", "Verify & sign off"], 1):
            conn.execute(
                "INSERT INTO work_instructions(routing_operation_id, step_number, text, required)"
                " VALUES (?,?,?,1)", (ro_id, i, txt))

    conn.execute(
        "INSERT INTO line_balances(name, routing_id, target_takt_minutes, status, created_at)"
        " VALUES ('Hyperion line v1', ?, 22.0, 'active', ?)", (routing_id, t))

    # --- demo jobs (mirror surrogate) + applied routings (what JB2 would own) ---
    demo_jobs = [
        ("J1001", "SO-5001", "AGENCY", "HYP-9", 10, "2026-07-05", 2),
        ("J1002", "SO-5002", "DEALER7", "HYP-9", 25, "2026-07-12", 5),
        ("J1003", "SO-5003", "AGENCY", "HYP-9", 5, "2026-07-02", 1),
    ]
    for jb2_no, order, cust, part, qty, due, pri in demo_jobs:
        conn.execute(
            "INSERT INTO jobs(jb2_job_number, order_number, customer_code, part_number, model_id, "
            "qty, due_date, priority, status, source, synced_at) "
            "VALUES (?,?,?,?,?,?,?,?, 'open','demo',?)",
            (jb2_no, order, cust, part, model_id, qty, due, pri, t))
        job_id = conn.execute("SELECT last_insert_rowid() AS i").fetchone()["i"]
        for step, code in enumerate(seq, start=1):
            o = next(x for x in ops if x[0] == code)
            conn.execute(
                "INSERT INTO applied_routings(job_id, step_number, operation_code, operation_name, "
                "work_center, std_run_min, synced_at) VALUES (?,?,?,?,?,?,?)",
                (job_id, step, code, o[1], o[2], o[4], t))

    conn.commit()
    return True
