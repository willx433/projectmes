# Change Requests — Atlas MES (ProjectAMR)

Running log of every deviation from the plan or requirements, and every load-bearing
decision that resolves an open item. **Nothing changes silently.** Newest first.

**Entry format:** ID · date · what changed · why · impact on other docs.

---

## CR-002 — Scrap recorded at the originating step (WIP event semantics)

- **Date:** 2026-06-30
- **What changed:** A scrap/reject disposition records its WIP event with `to_step =
  from_step` and `to_intra = 'scrap'` (rather than `to_step = NULL`). Scrapped qty now
  accumulates in that step's `scrap` cell instead of silently vanishing from the log.
- **Why:** PT-2 lists Reject/Scrap as intra-steps; recording at the step preserves a
  per-step scrap tally and keeps `compute_state` reconstructable (qty out of `to_move`
  must land somewhere). Caught by `test_smoke.py` (scrap assertion).
- **Impact:** `app/services/quality.py` (`_distribute` scrap call); WIP `compute_state`
  already counts `scrap`/`reject` as outflow for `_left`. No requirement text change —
  this realizes PT-2/PT-4 as written. `library.md` unaffected.

---

## CR-001 — Phase-0 integration-ownership decisions

- **Date:** 2026-06-30
- **What changed:** Resolved the four open integration questions from `research-notes.md`,
  closing the system-of-record (SoR) matrix:
  1. **Architecture direction approved as-is** — ISA-95 8-activity loop as backbone,
     MESA-11 as scope fence, keep the seed's Jinja server-render + versioned-routing model,
     WIP-move-as-append-only-event core.
  2. **Airtable = planning scratch only** — never a system of record; MES may read it for
     context, never as truth.
  3. **Per-order applied routing = JB2-owned** — MES reads JB2 `order-routings` and overlays
     its own work instructions; MES does not author the applied routing.
  4. **MES is a read-only island vs JB2 for now** — no write-back (e.g. `time-tickets`) in
     core phases; WIP must be designed so write-back can be added later.
- **Why:** These were flagged as load-bearing ambiguities blocking `requirements.md`.
  Decided via direct confirmation from William (propose-then-confirm), not invented.
  JB2's GET-only `non-conformances`/CAPA surface forces MES to own live quality regardless.
- **Impact:**
  - `requirements.md` — seeded "Locked decisions" + INT-1..INT-9 reflect this matrix.
  - `implementation-plan.md` — Phase 2 scoped as JB2 **read** layer; write-back deferred to
    Phase 6 stub (INT-9).
  - `library.md` — owning-system column reflects JB2 (jobs, applied routing) vs MES
    (routing master, WIP, NCR); Airtable absent (non-authoritative).
  - Memory — captured in `project-atlas-mes-amr`, `reference-mes-frameworks`.

---

*(No further entries yet. Add one per deviation/decision as the build proceeds.)*
