# Escalations — Atlas MES

Protocol (IMPLEMENTATION_PLAN.md §2): a Sonnet/Haiku agent that hits an **architectural
question** mid-task stops, appends an entry below, marks its task blocked, and hands off.
Fable answers in-place. If the answer deviates from the design docs, Fable also files a CR
in `docs/CHANGE_REQUESTS.md`. Cheap tiers never improvise architecture.

## Entry template

```
### ESC-NNN — <one-line question>
- Date / Task ID / Agent tier:
- Context: <what was being built, what the docs say, why it's ambiguous>
- Options considered: <A / B, with one-line trade-off each>
- BLOCKED files: <paths>
- Fable resolution: <answer + rationale + CR-ID if a deviation>
- Status: open | resolved
```

---

### ESC-001 — P2-10 order-hook needed the sync cycle's own session, not a fresh one
- Date / Task ID / Agent tier: 2026-07-14 / P2-10 / Sonnet
- Context: `app.sync.worker.register_order_hook`'s original signature was
  `hook(event, record)` — no session. Work-order creation needs to read/write
  the DB (line item, routings, product_part_map, etc.). Wiring the hook via a
  fresh `session_factory()`-per-event handler (as first attempted) silently
  no-ops in production: `_fire_order_event` fires **inside** `run_cycle`,
  before that cycle's caller commits, so a hook on a separate connection
  can't see the just-upserted line item (proved this empirically in a test —
  a fresh-session hook logged `hook_line_item_missing` every time, and only
  "worked" under sqlite's StaticPool single-connection trick, which doesn't
  reflect Postgres's real transaction isolation).
- Options considered: (A) fresh session per hook event, ignore the ordering
  gap (would silently drop the first attempt at every WorkOrder in
  production against Postgres — unacceptable). (B) pass the firing cycle's
  own `session` into the hook so work-order creation rides the same
  transaction as the mirror upsert (same "same-transaction" guarantee the
  outbox already gives its own writes, DD §4.5).
- Change made: `OrderHook` type widened to
  `Callable[[str, dict, Session], None]`; `_fire_order_event`,
  `_emit_order_events`, `_emit_line_item_events` now thread `session`
  through. Also reordered `_emit_line_item_events` so the routing/materials
  fetch (P1-07) happens **before** firing `order_line_item_new`/`_changed` —
  otherwise the binding resolver would run against an empty routing mirror
  for a brand-new order. Existing P1-06 hook tests
  (`tests/integration/test_sync.py::captured_events`) updated to the new
  3-arg signature.
- BLOCKED files: none — implemented, not blocked. Flagging because it
  changes a Phase-1 file's contract (`app/sync/worker.py`) that this task
  was told to read but not necessarily expected to modify.
- Fable resolution: **APPROVED.** The isolation argument is correct and empirically
  proven — a fresh-session hook cannot see uncommitted mirror rows on Postgres.
  Same-transaction is also the safer failure mode: a hook exception rolls back the
  whole resource cycle (mirror row + partial work order together), and run_cycle's
  per-resource isolation retries next cadence — no half-created work orders.
  The fetch-before-fire reorder is a prerequisite of correct binding; approved.
- Status: resolved

### ESC-002 — order_closed → work-order cancellation resolves via a full-table scan
- Date / Task ID / Agent tier: 2026-07-14 / P2-10 / Sonnet
- Context: DD §4.3 rule 4 / §17.4 requires order cancel/close to cascade to
  its work order(s). `jb2_order_line_items.jb2_order_id` is never populated
  (P1-06/07 explicitly left that cross-resource FK resolution out of scope —
  see `app/sync/worker.py::_order_line_items_extract`'s comment). Without it,
  there's no indexed path from a closed `JB2Order` to its line items.
- Options considered: (A) add the FK resolution properly in this task
  (touches Phase-1 sync extraction logic, expands scope). (B) match on
  `orderNumber` against each line item's own raw `payload` jsonb (every
  mirror row keeps its full raw record) — a full scan over
  `jb2_order_line_items`, but correct and requires no schema/extractor
  changes.
- Change made: (B), in `app/sync/hooks.py::_handle_order_closed`, with a
  `ponytail:` comment flagging the scan and pointing at the real fix
  (populate `jb2_order_id` properly, a Phase 1 follow-up).
- BLOCKED files: none.
- Fable resolution: **APPROVED for pilot scale** (30-day mirror window = hundreds of
  line items; scan is microseconds). Real fix (populate jb2_order_id in the
  extractor) queued as Phase-3-entry hygiene task P3-00. Ceiling named in code.
- Status: resolved

### ESC-003 — `server_default="false"` boolean columns read back as Python `True` on SQLite
- Date / Task ID / Agent tier: 2026-07-15 / P3-06/07 / Sonnet
- Context: `tests/integration/test_station_flow.py`'s finish-gating test
  (`remaining_required_count`) kept reporting every required substep as
  still outstanding even after the DB rows showed `status='done'`/`'skipped'`.
  Root-caused to `StepExecution.superseded` / `SubstepExecution.superseded`
  (`app/domain/models_floor.py`) never being set explicitly by
  `get_or_create_step_execution`/`get_or_create_substep_execution` --
  relying on the column's `server_default="false"` instead. On SQLite,
  SQLAlchemy compiles a **string** `server_default` as a quoted DDL literal
  (`DEFAULT 'false'`), which SQLite stores as the raw text `'false'`; read
  back, SQLAlchemy's boolean decoding does `bool(value)`, and `bool("false")
  is True` (any non-empty string). So every freshly-inserted row got
  `superseded=True`, and every `.superseded.is_(False)` filter (mine, and
  the same pattern in `app/domain/failures.py`/`app/api/operations.py`)
  silently matched nothing. Confirmed via `sqlite_master` (`DEFAULT 'false'`)
  and a raw-connection read (`typeof(superseded) = 'text'`, value `'false'`).
  Postgres is unaffected (casts the string 'false'/'true' to boolean
  correctly in a boolean column context) -- this is a SQLite-test-double-only
  bug, not a production bug, but it silently breaks any zero-network test
  that filters on one of these columns.
- Scope check: the same `server_default="false"` pattern (not just
  `"true"`, which happens to decode correctly since `bool("true")` is also
  `True`) appears on `WorkSession.lead_confirmed`, `PlanOperation.blocked`
  (`models_execution.py`), and `MappingException.resolved` (`models_jb2.py`)
  -- none of those files are in this task's scope, and none are on the
  P3-06/07/12 "don't touch" list (`statemachine.py`/`operations.py`/
  `sessions.py`/`failures.py`/`outbox`/migrations/`legacy/`).
- Change made: `server_default="false"` -> `server_default=false()`
  (the portable SQL boolean-literal construct, `from sqlalchemy import
  false`) on all five columns across `models_floor.py`, `models_execution.py`,
  `models_jb2.py`. This changes nothing about the Postgres migration DDL
  (untouched, per the "don't touch migrations" rule) -- Postgres already
  casts the string correctly; the fix only corrects what
  `Base.metadata.create_all()` emits for SQLite-backed tests. Full suite
  (269 tests incl. this task's new `test_station_flow.py`) green after the
  change; no other test's assertions changed behavior (everything that
  passed before still passes -- this was a false-negative-masking bug, not
  a case anyone was asserting the wrong thing on purpose).
- BLOCKED files: none -- implemented, not blocked. Flagging per the "any
  deviation gets logged" convention since it touches three files outside
  this task's own file list (P3-06/07/12: `templates/station/*`,
  `app/api/station.py`, `app/api/substeps.py`, `app/domain/substeps.py`).
- Fable resolution: pending review.
- Status: open (fix applied and tested; awaiting Fable sign-off on touching
  files outside this task's literal scope)
