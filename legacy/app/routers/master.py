"""Phase 1 — Routing / Work-Instruction master (MES-owned). WI-1..WI-3, WI-7, INT-3."""
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse

from .. import db
from ..deps import get_conn, render

router = APIRouter()


# ---- models ----
@router.get("/models")
def models(request: Request, conn=Depends(get_conn)):
    rows = db.query(conn, "SELECT * FROM models ORDER BY id DESC")
    return render(request, "models.html", nav="models", models=rows)


@router.post("/models")
def add_model(name: str = Form(...), type: str = Form("active"), conn=Depends(get_conn)):
    conn.execute("INSERT INTO models(name, type, created_at) VALUES (?,?,?)", (name, type, db.now()))
    conn.commit()
    return RedirectResponse("/models", status_code=303)


# ---- operations catalog ----
@router.get("/operations")
def operations(request: Request, conn=Depends(get_conn)):
    rows = db.query(conn, "SELECT * FROM operations ORDER BY id DESC")
    return render(request, "operations.html", nav="operations", operations=rows)


@router.post("/operations")
def add_operation(code: str = Form(...), name: str = Form(...), work_center: str = Form(...),
                  std_setup_min: float = Form(0), std_run_min: float = Form(0),
                  conn=Depends(get_conn)):
    conn.execute(
        "INSERT INTO operations(code, name, work_center, std_setup_min, std_run_min, created_at)"
        " VALUES (?,?,?,?,?,?)", (code, name, work_center, std_setup_min, std_run_min, db.now()))
    conn.commit()
    return RedirectResponse("/operations", status_code=303)


# ---- routings (versioned) ----
@router.get("/routings")
def routings(request: Request, conn=Depends(get_conn)):
    rows = db.query(conn,
        "SELECT r.*, m.name AS model_name, m.type AS model_type FROM routings r "
        "JOIN models m ON m.id=r.model_id ORDER BY r.name, r.version DESC")
    return render(request, "routings.html", nav="routings", routings=rows,
                  models=db.query(conn, "SELECT * FROM models ORDER BY name"))


@router.post("/routings")
def create_routing(name: str = Form(...), model_id: int = Form(...), conn=Depends(get_conn)):
    conn.execute(
        "INSERT INTO routings(name, model_id, version, status, created_at) VALUES (?,?,1,'draft',?)",
        (name, model_id, db.now()))
    rid = conn.execute("SELECT last_insert_rowid() AS i").fetchone()["i"]
    conn.commit()
    return RedirectResponse(f"/routings/{rid}", status_code=303)


@router.get("/routings/{rid}")
def routing_detail(rid: int, request: Request, conn=Depends(get_conn)):
    r = db.one(conn, "SELECT r.*, m.name AS model_name FROM routings r "
                     "JOIN models m ON m.id=r.model_id WHERE r.id=?", (rid,))
    if not r:
        return RedirectResponse("/routings", status_code=303)
    steps = db.query(conn,
        "SELECT ro.id AS ro_id, ro.step_number, o.code, o.name, o.work_center "
        "FROM routing_operations ro JOIN operations o ON o.id=ro.operation_id "
        "WHERE ro.routing_id=? ORDER BY ro.step_number", (rid,))
    instr = {}
    for s in steps:
        instr[s["ro_id"]] = db.query(conn,
            "SELECT * FROM work_instructions WHERE routing_operation_id=? ORDER BY step_number",
            (s["ro_id"],))
    return render(request, "routing_detail.html", nav="routings", r=r, steps=steps, instr=instr,
                  operations=db.query(conn, "SELECT * FROM operations ORDER BY code"))


@router.post("/routings/{rid}/operations")
def add_routing_op(rid: int, operation_id: int = Form(...), conn=Depends(get_conn)):
    nxt = db.one(conn, "SELECT COALESCE(MAX(step_number),0)+1 AS s FROM routing_operations "
                       "WHERE routing_id=?", (rid,))["s"]
    conn.execute("INSERT INTO routing_operations(routing_id, step_number, operation_id) VALUES (?,?,?)",
                 (rid, nxt, operation_id))
    conn.commit()
    return RedirectResponse(f"/routings/{rid}", status_code=303)


@router.post("/routing-operations/{ro_id}/instructions")
def add_instruction(ro_id: int, text: str = Form(...), required: int = Form(1),
                    conn=Depends(get_conn)):
    rid = db.one(conn, "SELECT routing_id FROM routing_operations WHERE id=?", (ro_id,))["routing_id"]
    nxt = db.one(conn, "SELECT COALESCE(MAX(step_number),0)+1 AS s FROM work_instructions "
                       "WHERE routing_operation_id=?", (ro_id,))["s"]
    conn.execute("INSERT INTO work_instructions(routing_operation_id, step_number, text, required)"
                 " VALUES (?,?,?,?)", (ro_id, nxt, text, required))
    conn.commit()
    return RedirectResponse(f"/routings/{rid}", status_code=303)


@router.post("/routings/{rid}/publish")
def publish_routing(rid: int, conn=Depends(get_conn)):
    conn.execute("UPDATE routings SET status='published' WHERE id=? AND status='draft'", (rid,))
    conn.commit()
    return RedirectResponse(f"/routings/{rid}", status_code=303)


@router.post("/routings/{rid}/new-version")
def new_version(rid: int, conn=Depends(get_conn)):
    """Supersede: clone this routing into a new draft version (WI-2). Prior version untouched."""
    old = db.one(conn, "SELECT * FROM routings WHERE id=?", (rid,))
    nv = db.one(conn, "SELECT COALESCE(MAX(version),0)+1 AS v FROM routings "
                      "WHERE name=? AND model_id=?", (old["name"], old["model_id"]))["v"]
    conn.execute("INSERT INTO routings(name, model_id, version, status, created_at) "
                 "VALUES (?,?,?,'draft',?)", (old["name"], old["model_id"], nv, db.now()))
    new_id = conn.execute("SELECT last_insert_rowid() AS i").fetchone()["i"]
    for ro in db.query(conn, "SELECT * FROM routing_operations WHERE routing_id=? ORDER BY step_number", (rid,)):
        conn.execute("INSERT INTO routing_operations(routing_id, step_number, operation_id) VALUES (?,?,?)",
                     (new_id, ro["step_number"], ro["operation_id"]))
        nro = conn.execute("SELECT last_insert_rowid() AS i").fetchone()["i"]
        for wi in db.query(conn, "SELECT * FROM work_instructions WHERE routing_operation_id=?", (ro["id"],)):
            conn.execute("INSERT INTO work_instructions(routing_operation_id, step_number, text, "
                         "image_path, required) VALUES (?,?,?,?,?)",
                         (nro, wi["step_number"], wi["text"], wi["image_path"], wi["required"]))
    conn.execute("UPDATE routings SET status='superseded', superseded_by=? WHERE id=?", (new_id, rid))
    conn.commit()
    return RedirectResponse(f"/routings/{new_id}", status_code=303)


# ---- line balances ----
@router.get("/line-balances")
def line_balances(request: Request, conn=Depends(get_conn)):
    rows = db.query(conn, "SELECT lb.*, r.name AS routing_name, r.version FROM line_balances lb "
                          "JOIN routings r ON r.id=lb.routing_id ORDER BY lb.id DESC")
    return render(request, "line_balances.html", nav="line-balances", line_balances=rows,
                  routings=db.query(conn, "SELECT id, name, version FROM routings ORDER BY name, version"))


@router.post("/line-balances")
def add_lb(name: str = Form(...), routing_id: int = Form(...),
           target_takt_minutes: float = Form(...), conn=Depends(get_conn)):
    conn.execute("INSERT INTO line_balances(name, routing_id, target_takt_minutes, status, created_at)"
                 " VALUES (?,?,?, 'active', ?)", (name, routing_id, target_takt_minutes, db.now()))
    conn.commit()
    return RedirectResponse("/line-balances", status_code=303)
