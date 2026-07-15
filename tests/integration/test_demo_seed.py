"""Integration coverage for tools/seed_demo.py (P2-14, DD N7).

Runs the seed twice against a tmp sqlite file and asserts: idempotent (row
counts stable on the second run), the demo work order has a frozen plan, and
-- feature-detected, since app/pdf (P2-11) may land concurrently with this
task -- a PlanPdf row when app.pdf is importable.
"""
from __future__ import annotations

import dataclasses
import importlib.util
import sys
from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
import seed_demo  # noqa: E402

from app.domain.models_execution import PlanOperation, Unit, WorkOrder
from app.domain.models_library import InstructionSet, Product, ProductPartMap


def _point_pdf_artifact_dir(monkeypatch, artifact_dir: Path) -> None:
    """app.pdf.guide reads `config.artifact_dir` (a module-level name bound
    at import time off the frozen app.config.Config dataclass) -- can't
    setattr a frozen dataclass, so swap the name binding in app.pdf.guide's
    own namespace instead. No-op if app.pdf isn't importable yet (P2-11,
    landing concurrently with this task)."""
    if importlib.util.find_spec("app.pdf") is None:
        return
    from app.pdf import guide as guide_module

    patched_config = dataclasses.replace(seed_demo.config, artifact_dir=str(artifact_dir))
    monkeypatch.setattr(guide_module, "config", patched_config)


def _counts(session: Session) -> dict[str, int]:
    return {
        "products": len(session.scalars(select(Product)).all()),
        "part_maps": len(session.scalars(select(ProductPartMap)).all()),
        "instruction_sets": len(session.scalars(select(InstructionSet)).all()),
        "work_orders": len(session.scalars(select(WorkOrder)).all()),
        "units": len(session.scalars(select(Unit)).all()),
        "plan_operations": len(session.scalars(select(PlanOperation)).all()),
    }


def test_seed_twice_is_idempotent(tmp_path, monkeypatch):
    db_path = tmp_path / "demo.db"
    artifact_dir = tmp_path / "artifacts"
    monkeypatch.setenv("ARTIFACT_DIR", str(artifact_dir))
    _point_pdf_artifact_dir(monkeypatch, artifact_dir)

    engine = create_engine(f"sqlite:///{db_path}")
    seed_demo.Base.metadata.create_all(engine)

    with Session(engine) as session:
        seeded_first = seed_demo.seed(session)
        session.commit()
        assert seeded_first is True
        first_counts = _counts(session)

    assert first_counts["products"] == 1
    assert first_counts["instruction_sets"] == 3
    assert first_counts["work_orders"] == 1
    assert first_counts["units"] == 2  # DD N7 / task brief: 2 units on the demo WO

    with Session(engine) as session:
        seeded_second = seed_demo.seed(session)
        session.commit()
        assert seeded_second is False  # no-op: Apollo product already exists
        second_counts = _counts(session)

    assert second_counts == first_counts


def test_demo_work_order_has_frozen_plan(tmp_path, monkeypatch):
    db_path = tmp_path / "demo.db"
    artifact_dir = tmp_path / "artifacts"
    monkeypatch.setenv("ARTIFACT_DIR", str(artifact_dir))
    _point_pdf_artifact_dir(monkeypatch, artifact_dir)

    engine = create_engine(f"sqlite:///{db_path}")
    seed_demo.Base.metadata.create_all(engine)

    with Session(engine) as session:
        seed_demo.seed(session)
        session.commit()

        wo = session.scalars(select(WorkOrder)).one()
        assert wo.status == "ready"
        assert wo.qty == 2

        plan_ops = session.scalars(
            select(PlanOperation).where(PlanOperation.work_order_id == wo.id)
        ).all()
        assert len(plan_ops) == 3
        assert all(not op.blocked for op in plan_ops)
        assert all(op.frozen_content.get("steps") for op in plan_ops)

        # app/pdf (P2-11) may or may not exist yet -- another agent is
        # building it concurrently. Feature-detect rather than hard-require.
        if importlib.util.find_spec("app.pdf") is not None:
            from app.domain.models_execution import PlanPdf

            pdfs = session.scalars(
                select(PlanPdf).where(PlanPdf.work_order_id == wo.id)
            ).all()
            assert len(pdfs) == 1
            assert pdfs[0].version == 1
