"""Demo/training seed (P2-14, DD N7) — replaces legacy/app/services/demo_seed.py.

Builds a complete, demoable Apollo training setup entirely through the real
domain functions (app.domain.library / app.domain.workorders) -- no raw
INSERTs -- so the seeded data exercises the exact same binding/freeze rules
production orders do:

  * Product "Apollo" (variant_schema caliber/finish) + part maps
    (APOLLO-9-BLK / APOLLO-9-FDE / APOLLO-45-BLK).
  * Three published instruction sets, one per op (CNC Slide/Barrel Fit/Final
    QC), covering the mixed substep types: action, a measurement with
    tolerances, a 9mm-only conditional substep, a photo, and a signoff.
  * One demo work order (APOLLO-9-BLK x2) with a frozen plan bound via
    app.domain.workorders.create_from_line_item.

Idempotent: re-running checks for a Product named "Apollo" first and does
nothing else if found (no duplicate rows).

Usage:
    .venv/bin/python tools/seed_demo.py [--db sqlite:///demo.db]
"""
from __future__ import annotations

import argparse
import importlib.util
import sys
import uuid
from datetime import date, datetime, timezone
from pathlib import Path

# ponytail: repo isn't pip-installed, so `python tools/seed_demo.py` needs the
# repo root on sys.path for `import app...` -- same fix pytest gets for free
# via tests/__init__.py + rootdir insertion.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.config import config
from app.domain import library, workorders
from app.domain.models_execution import PlanOperation, Unit, WorkOrder
from app.domain.models_jb2 import Base, JB2OrderLineItem, JB2OrderRouting
from app.domain.models_library import Product, ProductPartMap


def _mirror_common(now: datetime) -> dict:
    return {"payload": {}, "content_hash": "seed", "jb2_last_modified": None, "synced_at": now}


def _build_instruction_set(
    session: Session,
    product: Product,
    *,
    title: str,
    op_code: str,
    step_title: str,
    substeps: list[dict],
) -> None:
    iset = library.create_set(
        session,
        product_id=product.id,
        title=title,
        operation_match={"op_codes": [op_code], "work_centers": []},
        created_by="demo-seed",
    )
    session.flush()
    step = library.add_step(session, iset.id, step_title, seq=1, who="demo-seed")
    for seq, sub in enumerate(substeps, start=1):
        library.add_substep(session, step.id, sub.pop("type"), sub.pop("title"), seq=seq,
                             who="demo-seed", **sub)
    library.submit_for_review(session, iset.id)
    library.publish(session, iset.id, "demo-seed-approver")


def seed(session: Session) -> bool:
    """Returns True if data was seeded, False if it already existed (no-op)."""
    existing = session.scalars(select(Product).where(Product.name == "Apollo")).one_or_none()
    if existing is not None:
        return False

    now = datetime.now(timezone.utc)

    product = Product(
        id=uuid.uuid4(),
        name="Apollo",
        description="Apollo 9mm/.45 pistol — training product for demo/onboarding.",
        variant_schema={"caliber": ["9mm", ".45"], "finish": ["BLK", "FDE"]},
        active=True,
    )
    session.add(product)
    session.flush()

    part_maps = [
        ("APOLLO-9-BLK", {"caliber": "9mm", "finish": "BLK"}),
        ("APOLLO-9-FDE", {"caliber": "9mm", "finish": "FDE"}),
        ("APOLLO-45-BLK", {"caliber": ".45", "finish": "BLK"}),
    ]
    for part_number, variant_values in part_maps:
        session.add(
            ProductPartMap(
                id=uuid.uuid4(),
                product_id=product.id,
                jb2_part_number=part_number,
                variant_values=variant_values,
            )
        )

    _build_instruction_set(
        session,
        product,
        title="Apollo — CNC Slide",
        op_code="OP10",
        step_title="Mill slide",
        substeps=[
            {"type": "action", "title": "Load fixture"},
            {
                "type": "measurement",
                "title": "Verify chamber depth",
                "measurement_spec": {
                    "name": "chamber_depth", "unit": "in", "nominal": 0.5,
                    "tol_plus": 0.01, "tol_minus": 0.01,
                },
            },
            {
                "type": "action",
                "title": "Verify 9mm chamber relief cut",
                "condition": {"field": "caliber", "in": ["9mm"]},
            },
        ],
    )
    _build_instruction_set(
        session,
        product,
        title="Apollo — Barrel Fit",
        op_code="OP20",
        step_title="Fit barrel",
        substeps=[
            {"type": "action", "title": "Press-fit barrel to slide"},
            {"type": "photo", "title": "Photo of barrel/slide fit"},
        ],
    )
    _build_instruction_set(
        session,
        product,
        title="Apollo — Final QC",
        op_code="OP30",
        step_title="Final inspection",
        substeps=[
            {"type": "inspection", "title": "Visual/function inspection"},
            {"type": "signoff", "title": "QC signoff", "signoff_role": "QC Lead"},
        ],
    )
    session.flush()

    line_item = JB2OrderLineItem(
        id=uuid.uuid4(),
        jb2_id="demo-seed-li-1",
        jb2_order_id=None,
        part_number="APOLLO-9-BLK",
        description="Apollo 9mm BLK — demo order",
        qty=2,
        due_date=date(2026, 9, 1),
        **_mirror_common(now),
    )
    session.add(line_item)
    session.flush()

    for seq, (op_code, desc) in enumerate(
        [("OP10", "CNC Slide"), ("OP20", "Barrel Fit"), ("OP30", "Final QC")], start=1
    ):
        session.add(
            JB2OrderRouting(
                id=uuid.uuid4(),
                jb2_id=f"demo-seed-routing-{seq}",
                jb2_line_item_id=line_item.id,
                seq=seq * 10,
                operation_code=op_code,
                description=desc,
                work_center_code=None,
                est_setup_hrs=None,
                est_run_hrs=None,
                **_mirror_common(now),
            )
        )
    session.flush()

    # ponytail: no explicit PDF-generation call here -- app.domain.workorders
    # .create_from_line_item (when app/pdf is present) already auto-generates
    # the v1 build guide for a non-blocked work order (P2-11). Calling it
    # again here would just produce a duplicate v2 row.
    workorders.create_from_line_item(session, line_item)
    return True


def _print_summary(session: Session) -> None:
    product = session.scalars(select(Product).where(Product.name == "Apollo")).one()
    wo = session.scalars(select(WorkOrder).where(WorkOrder.product_id == product.id)).first()

    print("\n=== Demo seed summary ===")
    print(f"{'Product':<20} {product.name} ({product.id})")
    print(f"{'Part maps':<20} " + ", ".join(
        m.jb2_part_number
        for m in session.scalars(
            select(ProductPartMap).where(ProductPartMap.product_id == product.id)
        ).all()
    ))
    print("Instruction sets:")
    from app.domain.models_library import InstructionSet
    for iset in session.scalars(
        select(InstructionSet).where(InstructionSet.product_id == product.id)
    ).all():
        print(f"  - {iset.title:<28} v{iset.version:<3} {iset.state}")

    if wo is None:
        print("Work order:          <none>")
        return
    units = session.scalars(select(Unit).where(Unit.work_order_id == wo.id)).all()
    plan_ops = session.scalars(
        select(PlanOperation)
        .where(PlanOperation.work_order_id == wo.id)
        .order_by(PlanOperation.seq)
    ).all()
    print(f"Work order:           {wo.id} status={wo.status} qty={wo.qty}")
    print(f"  Units:               {len(units)} ({', '.join(u.status for u in units)})")
    print("  Plan operations:")
    for op in plan_ops:
        print(f"    seq={op.seq:<3} {op.title:<20} blocked={op.blocked} status={op.status}")

    if importlib.util.find_spec("app.pdf") is not None:
        from app.domain.models_execution import PlanPdf
        for pdf in session.scalars(
            select(PlanPdf).where(PlanPdf.work_order_id == wo.id)
        ).all():
            print(f"  Build guide PDF:     v{pdf.version} {pdf.path}")
    print()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", help="SQLAlchemy DB URL (overrides DATABASE_URL)")
    args = parser.parse_args()

    db_url = args.db or config.database_url
    if not db_url:
        raise SystemExit("no DB URL: pass --db or set DATABASE_URL")

    if db_url.startswith("postgresql://"):  # psycopg3, same coercion as migrations/env.py
        db_url = db_url.replace("postgresql://", "postgresql+psycopg://", 1)
    engine = create_engine(db_url)
    Base.metadata.create_all(engine)  # no-op if already migrated
    session_factory = sessionmaker(bind=engine)

    with session_factory() as session:
        seeded = seed(session)
        session.commit()
        print("Seeded new demo data." if seeded else "Demo data already present -- no-op.")
        _print_summary(session)


if __name__ == "__main__":
    main()
