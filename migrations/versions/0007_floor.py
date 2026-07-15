"""floor execution (P3-02)

Creates the rest of DD §10's "-- Execution" block that migration 0006
(P2-10) deferred: `build_boxes`, `box_assignments`, `stations`, `operators`,
`work_sessions`, `session_pauses`, `step_executions`, `substep_executions`,
`measurements`, `attachments`, `material_records`, `failures`,
`scrap_events`, `scans`, `transits`, `events`, `auth_events`, plus
`request_dedup` (see deviation below). See DD §10 for the literal schema
sketch, and **docs/state-machine.md** (P3-01, binding contract for all
Phase 3 work) for the per-unit-operation-state resolution (§2: no new
table -- `step_executions`/`substep_executions` are keyed
`(plan_operation_id, unit_id)`, `units.current_plan_op_id` from migration
0006 is the position pointer) and the scan/override/session rules that
drove a few of this migration's additive columns.

Table creation order (avoids forward-reference problems -- every FK target
exists by the time its referencing table is created):
  1. `operators`      (FK -> jb2_employees, already exists)
  2. `stations`        (no FKs out)
  3. `build_boxes`     (FK -> units [0006], stations [above])
  4. `box_assignments` (FK -> build_boxes, units)
  5. `work_sessions`   (FK -> units, plan_operations [0006], operators,
                        stations, jb2_outbox [0001])
  6. `session_pauses`  (FK -> work_sessions)
  7. `step_executions` (FK -> plan_operations, units, operators)
  8. `substep_executions` (FK -> step_executions, operators)
  9. `measurements`    (FK -> substep_executions, operators)
 10. `attachments`     (FK -> operators; entity_kind/entity_id is a plain
                        polymorphic pointer, no FK -- selects the target
                        table at the app layer)
 11. `material_records` (FK -> substep_executions, units, plan_operations,
                         operators)
 12. `failures`        (FK -> units, plan_operations, substep_executions,
                        failure_codes [0005], operators)
 13. `scrap_events`    (FK -> units, failures, operators)
 14. `scans`           (FK -> build_boxes, stations, operators,
                        work_sessions)
 15. `transits`        (FK -> units, stations)
 16. `events`          (FK -> operators, stations; bigserial PK)
 17. `auth_events`     (FK -> operators, stations)
 18. `request_dedup`   (no FKs)

DEVIATIONS from the DD §10 literal schema sketch:
  - `stations.work_center_code` (text) replaces the DD's
    `work_center_id fk`. `jb2_work_centers` is a sync mirror table that
    gets truncated/repopulated on a full refresh (see app/sync), so a hard
    FK from a durable floor table into a mirror row is a landmine -- a
    routine mirror refresh would either cascade-null stations or block the
    refresh outright. Joining on `jb2_work_centers.code` (a stable business
    key, not the mirror's surrogate `id`) avoids that; the join is done in
    application code, not the database. Documented here per the task
    brief's explicit call-out.
  - `request_dedup` is not in the DD §10 sketch. It is
    docs/state-machine.md §7's idempotency store ("All floor POSTs accept
    a client `request_id`; replays return the original result (server-side
    table or event-lookup dedup)"). Added as a small, contract-justified
    table addition per that doc and the task brief.
  - `work_sessions.close_reason` and the `superseded` boolean on
    `step_executions`/`substep_executions` are additive columns beyond the
    DD's literal list, required by state-machine.md §5 (rework marks prior
    executions `superseded` rather than deleting them -- history stays
    append-only) and §6/§17.8 (an `auto_closed` session must be
    distinguishable from `finished`/`clocked_out` so O6's lead-confirm gate
    knows what it's confirming).
  - `substep_executions.pass` is a SQL-safe column name (not a reserved
    word in Postgres) but collides with the Python keyword `pass` --
    mapped in `app/domain/models_floor.py` as attribute `pass_` via an
    explicit column-name override; the DDL column itself is named `pass`
    exactly per the DD/brief.
  - `events.id bigserial`: emitted here as a Postgres BIGSERIAL (a
    BigInteger primary key with autoincrement is SQLAlchemy/Alembic's
    standard way to get that on the Postgres dialect); the model docstring
    notes SQLite falls back to its native autoincrement rowid for the same
    column shape in unit tests.

Indexes per DD §10 "Key indexes" + this task's brief:
  - `scans(scanned_at)`
  - `events(entity_kind, entity_id, at)`
  - `units(work_order_id, status)` -- ADDITIVE to migration 0006, which only
    indexed `work_order_id` alone; the composite is new here since `status`
    wasn't part of 0006's index and this table already exists.
  - `work_sessions(operator_id, started_at)`
  - partial index `build_boxes(current_station_id) WHERE active` --
    Postgres-only (partial indexes aren't portable), guarded the same way
    migration 0006 guards `uq_units_serial_number_when_set` and migration
    0005 guards `uq_failure_codes_global_code`. Skipped entirely on SQLite;
    the model layer must not assume it's DB-enforced there.

No cascading deletes anywhere in this migration (N3) -- every FK is a plain
`ON DELETE NO ACTION` (SQLAlchemy/Postgres default), matching migrations
0001/0005/0006's convention.

`events` append-only: enforced at the application layer only in this
migration (no INSERT-only role/REVOKE here -- that needs a dedicated DB
role set up as part of deploy, which is a prod runbook item, not schema).
See the one-line addition to docs/runbooks/deploy.md alongside this
migration.

Revision ID: 0007
Revises: 0006
Create Date: 2026-07-14

"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0007"
down_revision: Union[str, None] = "0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _id_column() -> sa.Column:
    return sa.Column(
        "id",
        postgresql.UUID(as_uuid=True),
        primary_key=True,
        server_default=sa.text("gen_random_uuid()"),
    )


def upgrade() -> None:
    is_postgres = op.get_context().dialect.name == "postgresql"

    # 1. operators ------------------------------------------------------
    op.create_table(
        "operators",
        _id_column(),
        sa.Column(
            "jb2_employee_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("jb2_employees.id"),
            nullable=True,
        ),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("badge_qr", sa.Text(), nullable=True),
        sa.Column("pin_hash", sa.Text(), nullable=True),
        sa.Column("roles", postgresql.JSONB(astext_type=sa.Text()), nullable=False,
                   server_default="[]"),
        sa.Column("active", sa.Boolean(), nullable=False, server_default="true"),
        sa.UniqueConstraint("badge_qr", name="uq_operators_badge_qr"),
    )
    op.create_index("ix_operators_jb2_employee_id", "operators", ["jb2_employee_id"])

    # 2. stations ---------------------------------------------------------
    op.create_table(
        "stations",
        _id_column(),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("work_center_code", sa.Text(), nullable=True),
        sa.Column("location", sa.Text(), nullable=True),
        sa.Column("kiosk_token", sa.Text(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("kiosk_token", name="uq_stations_kiosk_token"),
    )
    op.create_index("ix_stations_work_center_code", "stations", ["work_center_code"])

    # 3. build_boxes --------------------------------------------------------
    op.create_table(
        "build_boxes",
        _id_column(),
        sa.Column("qr_payload", sa.Text(), nullable=False),
        sa.Column("label", sa.Text(), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column(
            "current_unit_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("units.id"),
            nullable=True,
        ),
        sa.Column(
            "current_station_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("stations.id"),
            nullable=True,
        ),
        sa.Column("last_scan_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("qr_payload", name="uq_build_boxes_qr_payload"),
    )
    op.create_index("ix_build_boxes_current_unit_id", "build_boxes", ["current_unit_id"])
    op.create_index(
        "ix_build_boxes_current_station_id", "build_boxes", ["current_station_id"]
    )
    if is_postgres:
        op.execute(
            "CREATE INDEX ix_build_boxes_current_station_active "
            "ON build_boxes (current_station_id) WHERE active"
        )

    # 4. box_assignments ------------------------------------------------
    op.create_table(
        "box_assignments",
        _id_column(),
        sa.Column(
            "box_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("build_boxes.id"),
            nullable=False,
        ),
        sa.Column(
            "unit_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("units.id"),
            nullable=False,
        ),
        sa.Column("assigned_by", sa.Text(), nullable=True),
        sa.Column(
            "assigned_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_box_assignments_box_id", "box_assignments", ["box_id"])
    op.create_index("ix_box_assignments_unit_id", "box_assignments", ["unit_id"])

    # 5. work_sessions ----------------------------------------------------
    op.create_table(
        "work_sessions",
        _id_column(),
        sa.Column(
            "unit_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("units.id"),
            nullable=False,
        ),
        sa.Column(
            "plan_operation_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("plan_operations.id"), nullable=False,
        ),
        sa.Column(
            "operator_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("operators.id"),
            nullable=False,
        ),
        sa.Column(
            "station_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("stations.id"),
            nullable=False,
        ),
        sa.Column(
            "started_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("close_reason", sa.Text(), nullable=True),
        sa.Column("lead_confirmed", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column(
            "jb2_outbox_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("jb2_outbox.id"),
            nullable=True,
        ),
        sa.CheckConstraint(
            "kind in ('first_pass','rework','setup')", name="ck_work_sessions_kind"
        ),
        sa.CheckConstraint(
            "close_reason is null or close_reason in "
            "('finished','clocked_out','auto_closed')",
            name="ck_work_sessions_close_reason",
        ),
    )
    op.create_index("ix_work_sessions_unit_id", "work_sessions", ["unit_id"])
    op.create_index(
        "ix_work_sessions_plan_operation_id", "work_sessions", ["plan_operation_id"]
    )
    op.create_index("ix_work_sessions_station_id", "work_sessions", ["station_id"])
    op.create_index(
        "ix_work_sessions_operator_id_started_at", "work_sessions",
        ["operator_id", "started_at"],
    )

    # 6. session_pauses ---------------------------------------------------
    op.create_table(
        "session_pauses",
        _id_column(),
        sa.Column(
            "work_session_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("work_sessions.id"), nullable=False,
        ),
        sa.Column("reason_code", sa.Text(), nullable=False),
        sa.Column(
            "started_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "reason_code in ('waiting_material','machine_down','break',"
            "'pulled_to_other_job','other')",
            name="ck_session_pauses_reason_code",
        ),
    )
    op.create_index("ix_session_pauses_work_session_id", "session_pauses", ["work_session_id"])

    # 7. step_executions ----------------------------------------------------
    op.create_table(
        "step_executions",
        _id_column(),
        sa.Column(
            "plan_operation_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("plan_operations.id"), nullable=False,
        ),
        sa.Column(
            "unit_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("units.id"),
            nullable=False,
        ),
        sa.Column("step_seq", sa.Integer(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="pending"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "completed_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("operators.id"),
            nullable=True,
        ),
        sa.Column("superseded", sa.Boolean(), nullable=False, server_default="false"),
    )
    op.create_index(
        "ix_step_executions_plan_operation_id", "step_executions", ["plan_operation_id"]
    )
    op.create_index("ix_step_executions_unit_id", "step_executions", ["unit_id"])

    # 8. substep_executions -------------------------------------------------
    op.create_table(
        "substep_executions",
        _id_column(),
        sa.Column(
            "step_execution_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("step_executions.id"), nullable=False,
        ),
        sa.Column("substep_seq", sa.Integer(), nullable=False),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="pending"),
        sa.Column(
            "operator_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("operators.id"),
            nullable=True,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("value_numeric", sa.Numeric(), nullable=True),
        sa.Column("value_text", sa.Text(), nullable=True),
        sa.Column("pass", sa.Boolean(), nullable=True),
        sa.Column("out_of_tolerance", sa.Boolean(), nullable=True),
        sa.Column("disposition", sa.Text(), nullable=True),
        sa.Column(
            "disposition_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("operators.id"),
            nullable=True,
        ),
        sa.Column("skip_reason", sa.Text(), nullable=True),
        sa.Column(
            "skip_authorized_by", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("operators.id"), nullable=True,
        ),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("superseded", sa.Boolean(), nullable=False, server_default="false"),
        sa.CheckConstraint(
            "status in ('pending','in_progress','done','failed','skipped')",
            name="ck_substep_executions_status",
        ),
    )
    op.create_index(
        "ix_substep_executions_step_execution_id", "substep_executions", ["step_execution_id"]
    )

    # 9. measurements -----------------------------------------------------
    op.create_table(
        "measurements",
        _id_column(),
        sa.Column(
            "substep_execution_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("substep_executions.id"), nullable=False,
        ),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("unit", sa.Text(), nullable=True),
        sa.Column("value", sa.Numeric(), nullable=False),
        sa.Column("nominal", sa.Numeric(), nullable=True),
        sa.Column("tol_plus", sa.Numeric(), nullable=True),
        sa.Column("tol_minus", sa.Numeric(), nullable=True),
        sa.Column("in_tolerance", sa.Boolean(), nullable=True),
        sa.Column("gauge_id", sa.Text(), nullable=True),
        sa.Column(
            "recorded_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("operators.id"),
            nullable=False,
        ),
        sa.Column(
            "recorded_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index(
        "ix_measurements_substep_execution_id", "measurements", ["substep_execution_id"]
    )

    # 10. attachments -------------------------------------------------------
    op.create_table(
        "attachments",
        _id_column(),
        sa.Column("entity_kind", sa.Text(), nullable=False),
        sa.Column("entity_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("sha256", sa.Text(), nullable=False),
        sa.Column(
            "uploaded_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("operators.id"),
            nullable=True,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint("kind in ('photo','file')", name="ck_attachments_kind"),
    )
    op.create_index("ix_attachments_entity_id", "attachments", ["entity_id"])

    # 11. material_records ---------------------------------------------
    op.create_table(
        "material_records",
        _id_column(),
        sa.Column(
            "substep_execution_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("substep_executions.id"), nullable=True,
        ),
        sa.Column(
            "unit_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("units.id"),
            nullable=False,
        ),
        sa.Column(
            "plan_operation_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("plan_operations.id"), nullable=False,
        ),
        sa.Column("part_number", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("lot", sa.Text(), nullable=True),
        sa.Column("qty_planned", sa.Numeric(), nullable=True),
        sa.Column("qty_used", sa.Numeric(), nullable=False),
        sa.Column("qty_scrapped", sa.Numeric(), nullable=False, server_default="0"),
        sa.Column("unit", sa.Text(), nullable=True),
        sa.Column("substitution_for", sa.Text(), nullable=True),
        sa.Column(
            "authorized_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("operators.id"),
            nullable=True,
        ),
        sa.Column(
            "recorded_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index(
        "ix_material_records_substep_execution_id", "material_records",
        ["substep_execution_id"],
    )
    op.create_index("ix_material_records_unit_id", "material_records", ["unit_id"])
    op.create_index(
        "ix_material_records_plan_operation_id", "material_records", ["plan_operation_id"]
    )

    # 12. failures ----------------------------------------------------------
    op.create_table(
        "failures",
        _id_column(),
        sa.Column(
            "unit_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("units.id"),
            nullable=False,
        ),
        sa.Column(
            "plan_operation_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("plan_operations.id"), nullable=False,
        ),
        sa.Column(
            "substep_execution_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("substep_executions.id"), nullable=True,
        ),
        sa.Column(
            "failure_code_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("failure_codes.id"), nullable=False,
        ),
        sa.Column("narrative", sa.Text(), nullable=True),
        sa.Column(
            "detected_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("operators.id"),
            nullable=False,
        ),
        sa.Column(
            "detected_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "introduced_at_op", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("plan_operations.id"), nullable=True,
        ),
        sa.Column("disposition", sa.Text(), nullable=False),
        sa.Column(
            "rework_to_op", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("plan_operations.id"), nullable=True,
        ),
        sa.Column(
            "authorized_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("operators.id"),
            nullable=True,
        ),
        sa.CheckConstraint(
            "disposition in ('rework_in_place','rework_to_op','scrap','use_as_is')",
            name="ck_failures_disposition",
        ),
    )
    op.create_index("ix_failures_unit_id", "failures", ["unit_id"])
    op.create_index("ix_failures_plan_operation_id", "failures", ["plan_operation_id"])
    op.create_index(
        "ix_failures_substep_execution_id", "failures", ["substep_execution_id"]
    )
    op.create_index("ix_failures_failure_code_id", "failures", ["failure_code_id"])

    # 13. scrap_events --------------------------------------------------
    op.create_table(
        "scrap_events",
        _id_column(),
        sa.Column(
            "unit_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("units.id"),
            nullable=False,
        ),
        sa.Column(
            "failure_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("failures.id"),
            nullable=False,
        ),
        sa.Column("cause_code", sa.Text(), nullable=True),
        sa.Column("narrative", sa.Text(), nullable=True),
        sa.Column("material_value_est", sa.Numeric(), nullable=True),
        sa.Column(
            "authorized_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("operators.id"),
            nullable=False,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "replacement_unit_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("units.id"),
            nullable=True,
        ),
    )
    op.create_index("ix_scrap_events_unit_id", "scrap_events", ["unit_id"])
    op.create_index("ix_scrap_events_failure_id", "scrap_events", ["failure_id"])

    # 14. scans ---------------------------------------------------------
    op.create_table(
        "scans",
        _id_column(),
        sa.Column(
            "box_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("build_boxes.id"),
            nullable=True,
        ),
        sa.Column(
            "station_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("stations.id"),
            nullable=False,
        ),
        sa.Column(
            "operator_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("operators.id"),
            nullable=True,
        ),
        sa.Column(
            "scanned_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("raw_payload", sa.Text(), nullable=False),
        sa.Column("result", sa.Text(), nullable=False),
        sa.Column(
            "override_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("operators.id"),
            nullable=True,
        ),
        sa.Column(
            "work_session_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("work_sessions.id"), nullable=True,
        ),
        sa.CheckConstraint(
            "result in ('accepted','wrong_station','unknown_box','unbound_box',"
            "'rejected')",
            name="ck_scans_result",
        ),
    )
    op.create_index("ix_scans_box_id", "scans", ["box_id"])
    op.create_index("ix_scans_station_id", "scans", ["station_id"])
    op.create_index("ix_scans_operator_id", "scans", ["operator_id"])
    op.create_index("ix_scans_scanned_at", "scans", ["scanned_at"])

    # 15. transits --------------------------------------------------------
    op.create_table(
        "transits",
        _id_column(),
        sa.Column(
            "unit_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("units.id"),
            nullable=False,
        ),
        sa.Column(
            "from_station_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("stations.id"),
            nullable=True,
        ),
        sa.Column(
            "to_station_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("stations.id"),
            nullable=True,
        ),
        sa.Column(
            "departed_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("arrived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("seconds", sa.Integer(), nullable=True),
    )
    op.create_index("ix_transits_unit_id", "transits", ["unit_id"])

    # 16. events (append-only audit trail) ---------------------------------
    op.create_table(
        "events",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")
        ),
        sa.Column(
            "actor_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("operators.id"),
            nullable=True,
        ),
        sa.Column(
            "station_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("stations.id"),
            nullable=True,
        ),
        sa.Column("entity_kind", sa.Text(), nullable=False),
        sa.Column("entity_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("verb", sa.Text(), nullable=False),
        sa.Column("before", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("after", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.create_index("ix_events_actor_id", "events", ["actor_id"])
    op.create_index(
        "ix_events_entity_kind_entity_id_at", "events", ["entity_kind", "entity_id", "at"]
    )
    # events is append-only by application-layer convention only (no
    # UPDATE/DELETE path in the domain code). DB-level REVOKE UPDATE/DELETE
    # needs a dedicated non-owner app role, which is a deploy/runbook
    # concern, not schema -- see docs/runbooks/deploy.md.

    # 17. auth_events -----------------------------------------------------
    op.create_table(
        "auth_events",
        _id_column(),
        sa.Column(
            "operator_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("operators.id"),
            nullable=True,
        ),
        sa.Column(
            "station_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("stations.id"),
            nullable=True,
        ),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column(
            "at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")
        ),
        sa.CheckConstraint(
            "kind in ('login','logout','badge_fail','pin_fail','override')",
            name="ck_auth_events_kind",
        ),
    )
    op.create_index("ix_auth_events_operator_id", "auth_events", ["operator_id"])

    # 18. request_dedup (state-machine.md §7 idempotency store) -----------
    op.create_table(
        "request_dedup",
        _id_column(),
        sa.Column("request_id", sa.Text(), nullable=False),
        sa.Column("endpoint", sa.Text(), nullable=False),
        sa.Column("response", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint("request_id", name="uq_request_dedup_request_id"),
    )

    # Additive index on the pre-existing `units` table (DD §10 key index
    # `units(work_order_id, status)` -- migration 0006 only indexed
    # `work_order_id` alone).
    op.create_index("ix_units_work_order_id_status", "units", ["work_order_id", "status"])


def downgrade() -> None:
    op.drop_index("ix_units_work_order_id_status", table_name="units")
    op.drop_table("request_dedup")
    op.drop_table("auth_events")
    op.drop_table("events")
    op.drop_table("transits")
    op.drop_table("scans")
    op.drop_table("scrap_events")
    op.drop_table("failures")
    op.drop_table("material_records")
    op.drop_table("attachments")
    op.drop_table("measurements")
    op.drop_table("substep_executions")
    op.drop_table("step_executions")
    op.drop_table("session_pauses")
    op.drop_table("work_sessions")
    op.drop_table("box_assignments")
    is_postgres = op.get_context().dialect.name == "postgresql"
    if is_postgres:
        op.execute("DROP INDEX IF EXISTS ix_build_boxes_current_station_active")
    op.drop_table("build_boxes")
    op.drop_table("stations")
    op.drop_table("operators")
