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
