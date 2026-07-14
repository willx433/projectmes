-- Atlas MES schema. Applied idempotently by db.init_db().
-- Owning system per CR-001: MES owns master/WIP/quality; JB2 owns jobs + applied routing.

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_version (
  version INTEGER PRIMARY KEY,
  applied_at TEXT NOT NULL
);

-- ============ Domain 4: Routing / Work-Instruction master (MES-owned) ============

CREATE TABLE IF NOT EXISTS models (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  type TEXT NOT NULL DEFAULT 'active',     -- shown as status badge in seed UI
  created_at TEXT NOT NULL
);

-- Operation catalog (reusable steps). work_center is the dispatch grouping key.
CREATE TABLE IF NOT EXISTS operations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  code TEXT NOT NULL UNIQUE,
  name TEXT NOT NULL,
  work_center TEXT NOT NULL,
  std_setup_min REAL NOT NULL DEFAULT 0,
  std_run_min REAL NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);

-- A routing row = ONE immutable version. (name, model_id, version) is unique.
-- status: draft | published | superseded. New version supersedes, never mutates prior.
CREATE TABLE IF NOT EXISTS routings (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  model_id INTEGER NOT NULL REFERENCES models(id),
  version INTEGER NOT NULL DEFAULT 1,
  status TEXT NOT NULL DEFAULT 'draft',
  superseded_by INTEGER REFERENCES routings(id),
  created_at TEXT NOT NULL,
  UNIQUE (name, model_id, version)
);

-- Ordered operations within a routing version.
CREATE TABLE IF NOT EXISTS routing_operations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  routing_id INTEGER NOT NULL REFERENCES routings(id) ON DELETE CASCADE,
  step_number INTEGER NOT NULL,
  operation_id INTEGER NOT NULL REFERENCES operations(id),
  UNIQUE (routing_id, step_number)
);

-- Sequenced work-instruction steps on a routing operation (text + optional local image).
CREATE TABLE IF NOT EXISTS work_instructions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  routing_operation_id INTEGER NOT NULL REFERENCES routing_operations(id) ON DELETE CASCADE,
  step_number INTEGER NOT NULL,
  text TEXT NOT NULL,
  image_path TEXT,
  required INTEGER NOT NULL DEFAULT 1,
  UNIQUE (routing_operation_id, step_number)
);

CREATE TABLE IF NOT EXISTS line_balances (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  routing_id INTEGER NOT NULL REFERENCES routings(id),
  target_takt_minutes REAL NOT NULL,
  status TEXT NOT NULL DEFAULT 'draft',
  created_at TEXT NOT NULL
);

-- ============ Integration: JB2 read-only mirror (JB2-owned) ============

-- jobs/orders mirror. source: 'jb2' (synced) | 'demo' (seeded). Read-only vs JB2.
CREATE TABLE IF NOT EXISTS jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  jb2_job_number TEXT NOT NULL UNIQUE,     -- dedup key (INT-6: JB2 ignores Idempotency-Key)
  order_number TEXT,
  customer_code TEXT,
  part_number TEXT,
  model_id INTEGER REFERENCES models(id),  -- best-effort link to MES model
  qty REAL NOT NULL DEFAULT 0,
  due_date TEXT,
  priority INTEGER NOT NULL DEFAULT 5,
  status TEXT NOT NULL DEFAULT 'open',      -- open once released to floor
  released INTEGER NOT NULL DEFAULT 0,
  source TEXT NOT NULL DEFAULT 'demo',
  synced_at TEXT NOT NULL
);

-- per-order applied routing mirror (JB2 order-routings). MES instructions overlay this.
CREATE TABLE IF NOT EXISTS applied_routings (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
  step_number INTEGER NOT NULL,
  operation_code TEXT NOT NULL,             -- match key to MES operations.code for overlay
  operation_name TEXT NOT NULL,
  work_center TEXT NOT NULL,
  std_run_min REAL NOT NULL DEFAULT 0,
  synced_at TEXT NOT NULL,
  UNIQUE (job_id, step_number)
);

CREATE TABLE IF NOT EXISTS reconciliation_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  entity_type TEXT NOT NULL,
  entity_key TEXT NOT NULL,
  field TEXT,
  mes_value TEXT,
  jb2_value TEXT,
  note TEXT,
  resolved INTEGER NOT NULL DEFAULT 0,
  detected_at TEXT NOT NULL
);

-- ============ Domain 1: WIP — append-only move-event log (MES-owned) ============
-- Current WIP state is DERIVED from this log (PT-4). Never mutate; only append.
-- Each event subtracts qty from (from_step, from_intra) and adds to (to_step, to_intra).
-- intra step values: queue | run | to_move | scrap | reject
-- kind: release | move | advance | scrap | reject | rework_return
CREATE TABLE IF NOT EXISTS wip_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id INTEGER NOT NULL REFERENCES jobs(id),
  kind TEXT NOT NULL,
  from_step INTEGER,            -- applied_routings.step_number, NULL for release
  from_intra TEXT,
  to_step INTEGER,              -- NULL for terminal scrap
  to_intra TEXT,
  qty REAL NOT NULL,
  actor TEXT NOT NULL,
  reason TEXT,
  created_at TEXT NOT NULL
);

-- Operator instruction acknowledgements (WI-5).
CREATE TABLE IF NOT EXISTS instruction_acks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id INTEGER NOT NULL REFERENCES jobs(id),
  step_number INTEGER NOT NULL,
  instruction_id INTEGER NOT NULL REFERENCES work_instructions(id),
  actor TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE (job_id, step_number, instruction_id)
);

-- Deviations (WI-6): skipped / out-of-sequence required steps.
CREATE TABLE IF NOT EXISTS deviations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id INTEGER NOT NULL REFERENCES jobs(id),
  step_number INTEGER NOT NULL,
  kind TEXT NOT NULL,
  detail TEXT,
  actor TEXT NOT NULL,
  created_at TEXT NOT NULL
);

-- Planner manual re-prioritization (SD-5).
CREATE TABLE IF NOT EXISTS dispatch_overrides (
  job_id INTEGER PRIMARY KEY REFERENCES jobs(id),
  rank INTEGER NOT NULL,      -- lower sorts first
  actor TEXT NOT NULL,
  created_at TEXT NOT NULL
);

-- ============ Domain 3: Quality / NCR (MES-owned) ============
-- state: open | under_review | dispositioned
-- disposition: rework | scrap | use_as_is | return_to_vendor | regrade
CREATE TABLE IF NOT EXISTS ncrs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id INTEGER NOT NULL REFERENCES jobs(id),
  step_number INTEGER NOT NULL,
  part_number TEXT,
  qty REAL NOT NULL,
  defect_type TEXT NOT NULL,
  detail TEXT,
  state TEXT NOT NULL DEFAULT 'open',
  disposition TEXT,
  rework_to_step INTEGER,       -- for rework disposition
  reviewer TEXT,
  justification TEXT,
  supersedes_ncr_id INTEGER REFERENCES ncrs(id),
  reported_by TEXT NOT NULL,
  created_at TEXT NOT NULL,
  dispositioned_at TEXT
);

CREATE INDEX IF NOT EXISTS ix_wip_events_job ON wip_events(job_id);
CREATE INDEX IF NOT EXISTS ix_applied_routings_job ON applied_routings(job_id);
CREATE INDEX IF NOT EXISTS ix_ncrs_job ON ncrs(job_id, step_number);
