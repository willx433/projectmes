"""Checks app/domain/models_jb2.py stays in sync with the hand-written
migration 0001_jb2_mirrors.py — both are maintained by hand, nothing
autogenerates one from the other (see docstrings in both files).

Reuses alembic's own offline-SQL path (`alembic upgrade head --sql`, the
same mechanism used to verify the migration manually) rather than mocking
Operations internals — it's the code path alembic itself considers
authoritative for "what DDL does this migration emit".
"""
from __future__ import annotations

import io
import re
from contextlib import redirect_stdout
from pathlib import Path

from alembic import command
from alembic.config import Config

from app.domain import (
    models_execution,  # noqa: F401  (registers Execution tables on Base.metadata)
    models_floor,  # noqa: F401  (registers Floor tables on Base.metadata)
    models_library,  # noqa: F401  (registers Library tables on Base.metadata)
)
from app.domain.models_jb2 import Base

REPO_ROOT = Path(__file__).resolve().parents[2]

EXPECTED_TABLES = {
    "jb2_orders",
    "jb2_order_line_items",
    "jb2_order_routings",
    "jb2_order_materials",
    "jb2_parts",
    "jb2_work_centers",
    "jb2_employees",
    "jb2_operation_codes",
    "jb2_reason_codes",
    "jb2_documents",
    "sync_runs",
    "jb2_outbox",
    "mapping_exceptions",
    "sync_checkpoints",
    "display_cache",
    "products",
    "product_part_map",
    "instruction_sets",
    "steps",
    "substeps",
    "failure_codes",
    "work_orders",
    "units",
    "plan_operations",
    "plan_pdfs",
    "operators",
    "stations",
    "build_boxes",
    "box_assignments",
    "work_sessions",
    "session_pauses",
    "step_executions",
    "substep_executions",
    "measurements",
    "attachments",
    "material_records",
    "failures",
    "scrap_events",
    "scans",
    "transits",
    "events",
    "auth_events",
    "request_dedup",
}

_CREATE_TABLE_RE = re.compile(r"CREATE TABLE (\w+) \((.*?)\n\);", re.DOTALL)
_COLUMN_RE = re.compile(r"^\s*(\w+) ", re.MULTILINE)
_SQL_KEYWORDS = {"CONSTRAINT", "PRIMARY", "FOREIGN", "UNIQUE", "CHECK"}
# Later migrations (e.g. 0003_outbox_next_attempt) evolve a table with
# ALTER TABLE ... ADD COLUMN rather than a fresh CREATE TABLE — pick those
# up too so the model/migration comparison covers the whole `head`, not
# just what 0001 created.
_ADD_COLUMN_RE = re.compile(r"ALTER TABLE (\w+) ADD COLUMN (\w+) ")


def _tables_from_migration_sql() -> dict[str, set[str]]:
    """Run `alembic upgrade head --sql` in-process and parse the emitted
    CREATE TABLE (and later ALTER TABLE ADD COLUMN) statements into
    {table_name: {column_names}}."""
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", "postgresql://mes:mes@localhost:5432/mes")

    buf = io.StringIO()
    with redirect_stdout(buf):
        command.upgrade(cfg, "head", sql=True)
    sql = buf.getvalue()

    tables: dict[str, set[str]] = {}
    for name, body in _CREATE_TABLE_RE.findall(sql):
        if name == "alembic_version":
            continue  # alembic's own bookkeeping table, not part of the contract
        columns = set()
        for line in body.strip().split("\n"):
            first_word = line.strip().split()[0].rstrip(",")
            if first_word.upper() in _SQL_KEYWORDS:
                continue
            columns.add(first_word)
        tables[name] = columns

    for name, column in _ADD_COLUMN_RE.findall(sql):
        tables.setdefault(name, set()).add(column)

    return tables


def test_migration_creates_exactly_the_contract_tables():
    tables = _tables_from_migration_sql()
    assert set(tables) == EXPECTED_TABLES


def test_every_model_table_matches_migration_tables():
    tables = _tables_from_migration_sql()
    model_tables = set(Base.metadata.tables)
    assert model_tables == EXPECTED_TABLES
    assert model_tables == set(tables)


def test_model_columns_match_migration_columns():
    migration_tables = _tables_from_migration_sql()
    for name, table in Base.metadata.tables.items():
        model_columns = {c.name for c in table.columns}
        migration_columns = migration_tables[name]
        assert model_columns == migration_columns, (
            f"{name}: model columns {model_columns} != migration columns {migration_columns}"
        )


def test_outbox_work_order_id_has_fk_to_work_orders():
    """Contract requirement (P2-10, migration 0006): work_order_id is a
    nullable uuid column with a real FK to work_orders.id now that
    work_orders exists -- the FK promised (but deferred) since migration
    0001 lands here."""
    outbox = Base.metadata.tables["jb2_outbox"]
    col = outbox.columns["work_order_id"]
    assert col.nullable is True
    assert len(col.foreign_keys) == 1
    fk = next(iter(col.foreign_keys))
    assert fk.target_fullname == "work_orders.id"
