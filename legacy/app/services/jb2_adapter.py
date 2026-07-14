"""JobBOSS2 read-only mirror (CR-001 / INT-1,2,5,6,8).

Ground-truth constraints baked in (from prior JB2 sandbox discovery):
- jobs are reachable only via shopview/get-jobs, not a top-level /jobs resource.
- EVERY read must carry >=1 filter (INT-5): unbounded scans 500.
- JB2 ignores Idempotency-Key -> we dedup on jb2_job_number (INT-6).
- dates UTC yyyy-MM-ddTHH:mm:ssZ; filter syntax ?field[op]=value; paging take/skip (INT-8).

If JB2 is not configured the app runs on demo-seeded data (see demo_seed.py); this module
is import-safe and sync_* become no-ops. Live field mapping is best-effort and flagged
TODO until validated against the tenant.
"""
import httpx

from .. import config, db


class Jb2NotConfigured(RuntimeError):
    pass


def _get(path, params):
    if not config.JB2_ENABLED:
        raise Jb2NotConfigured("AMR_JB2_BASE_URL / AMR_JB2_TOKEN not set")
    if not params:
        raise ValueError("INT-5: every JB2 read must include at least one filter")
    url = f"{config.JB2_BASE_URL}/api/v1/{path.lstrip('/')}"
    headers = {"Authorization": f"Bearer {config.JB2_TOKEN}"}
    r = httpx.get(url, params=params, headers=headers, timeout=30)
    r.raise_for_status()
    body = r.json()
    return body.get("Data", body) if isinstance(body, dict) else body


def sync_jobs(conn):
    """Pull open jobs + their applied routings into the mirror. Returns count synced."""
    if not config.JB2_ENABLED:
        return 0
    # jobs via shopview; filter required (INT-5). Field names below are best-effort —
    # TODO: validate against tenant shopview/get-jobs payload before production.
    filt = dict(p.split("=", 1) for p in config.JB2_JOBS_FILTER.split("&") if "=" in p)
    rows = _get("shopview/get-jobs", filt or {"take": "200"})
    n = 0
    for r in rows:
        jb2_no = str(r.get("jobNumber") or r.get("job") or r.get("JobNumber") or "").strip()
        if not jb2_no:
            continue
        _upsert_job(conn, jb2_no, r)
        _sync_applied_routing(conn, jb2_no, r.get("orderNumber") or r.get("order"))
        n += 1
    conn.commit()
    return n


def _upsert_job(conn, jb2_no, r):
    fields = {
        "order_number": r.get("orderNumber") or r.get("order"),
        "customer_code": r.get("customerCode"),
        "part_number": r.get("partNumber") or r.get("part"),
        "qty": float(r.get("quantity") or r.get("qty") or 0),
        "due_date": r.get("dueDate") or r.get("promiseDate"),
        "priority": int(r.get("priority") or 5),
        "synced_at": db.now(),
        "source": "jb2",
    }
    existing = db.one(conn, "SELECT * FROM jobs WHERE jb2_job_number=?", (jb2_no,))
    if existing:
        # JB2 wins on its owned fields; log divergence rather than silently clobber (INT-7)
        for k in ("qty", "due_date", "priority", "customer_code"):
            if existing[k] is not None and str(existing[k]) != str(fields[k]) and fields[k] is not None:
                _reconcile(conn, "job", jb2_no, k, existing[k], fields[k])
        conn.execute(
            "UPDATE jobs SET order_number=?, customer_code=?, part_number=?, qty=?, "
            "due_date=?, priority=?, synced_at=?, source='jb2' WHERE jb2_job_number=?",
            (fields["order_number"], fields["customer_code"], fields["part_number"],
             fields["qty"], fields["due_date"], fields["priority"], fields["synced_at"], jb2_no),
        )
    else:  # dedup on unique jb2_job_number (INT-6)
        conn.execute(
            "INSERT INTO jobs(jb2_job_number, order_number, customer_code, part_number, qty, "
            "due_date, priority, status, source, synced_at) VALUES (?,?,?,?,?,?,?, 'open','jb2',?)",
            (jb2_no, fields["order_number"], fields["customer_code"], fields["part_number"],
             fields["qty"], fields["due_date"], fields["priority"], fields["synced_at"]),
        )


def _sync_applied_routing(conn, jb2_no, order_number):
    if not order_number:
        return
    job = db.one(conn, "SELECT id FROM jobs WHERE jb2_job_number=?", (jb2_no,))
    rows = _get("order-routings", {"orderNumber": str(order_number)})  # filtered (INT-5)
    conn.execute("DELETE FROM applied_routings WHERE job_id=?", (job["id"],))
    for r in rows:
        conn.execute(
            "INSERT OR REPLACE INTO applied_routings(job_id, step_number, operation_code, "
            "operation_name, work_center, std_run_min, synced_at) VALUES (?,?,?,?,?,?,?)",
            (job["id"], int(r.get("stepNumber") or 0), r.get("operationCode") or "",
             r.get("operationDescription") or r.get("operationCode") or "",
             r.get("workCenter") or "UNSET", float(r.get("runHours") or 0) * 60, db.now()),
        )


def _reconcile(conn, entity_type, key, field, mes_value, jb2_value, note=None):
    conn.execute(
        "INSERT INTO reconciliation_log(entity_type, entity_key, field, mes_value, jb2_value, "
        "note, detected_at) VALUES (?,?,?,?,?,?,?)",
        (entity_type, str(key), field, str(mes_value), str(jb2_value), note, db.now()),
    )
