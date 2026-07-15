# Phase 3 Gate — Floor execution

**Executed by:** Fable, 2026-07-15.
**Verdict: PASS** (one defect found and fixed within the gate; one carried exception).

## Acceptance criteria (DD §19 Phase 3)

| Criterion | Result | Evidence |
|---|---|---|
| A unit travels 3 stations end-to-end | **PASS — executed on real PostgreSQL 16 via the full HTTP stack** | `tools/gate3_walk.py` (re-runnable gate evidence) + `docs/gates/phase3_walk_output.md`. Kit-up (box + serial) → 3 ops each: badge → scan (S6 accept) → substeps (action, in-tolerance measurement, inspection, lead signoff) → finish → transit hop closed by next scan → unit reaches `done` with serial, `first_pass=True`. |
| JB2 shows correct time tickets + quantities | **PASS** | Outbox drained to fake-JB2: exactly 3 `/time-ticket-details`, one per op (`stepNumber` 1/2/3, `jobNumber`, `workCenter`, `employeeCode`, `piecesFinished=1`, `piecesScrapped=0`, `timeStart`/`timeEnd`). **Zero `/order-routings` PATCH** — CR-010 assertion PASS. |
| §9.1–9.4 data queryable | **PASS** | Unit event timeline is complete and gapless (scans, sessions, substeps, measurement with in_tolerance flag, signoff with acting operator, transits, unit.done); measurements/sessions/scans/transits all queryable per unit. |
| State-machine integrity | **PASS** | P3-14 property suite (hypothesis + deterministic): obligations a–g green, no illegal transition reachable, role gating O1–O8, out-of-tol blocks finish, rework monotonic, one-event-per-transition. |

## Defect found and fixed IN GATE

**G3-D1 (correctness, high severity) — phantom labor hours in JB2 time tickets.**
The first walk emitted `timeStart='...08:10:06Z'` / `timeEnd='...12:10:06Z'` for a
sub-second session — a bogus 4-hour interval that would corrupt JB2 job costing on
every ticket. Root cause: `app/sync/engine.py::to_utc()` relabeled an aware
non-UTC datetime as-is instead of converting it; `format_jb2_datetime` then stamped
`Z` on EDT wall-clock. **Only reproducible on Postgres** (returns `timestamptz` in the
session zone) — every SQLite-based unit/integration test round-trips naive, which
masked it entirely. This is the exact class of bug the real-database gate exists to
catch. Fixed (`to_utc` now `.astimezone(utc)` for aware values); regression locked by
`tests/unit/test_jb2_datetime.py` (3 tests); re-run walk confirms `timeStart == timeEnd`
zone, no phantom gap.

## Carried exception — CLEARED 2026-07-15

- **CR-012 / P0-R1**: RESOLVED via authorized live writes against sandbox job 28962-07.
  The live contract differed materially from the spec (nested `POST /time-tickets`, `HH:MM`
  clock times, JB2-derived `cycleTime`, no `operationNumber`/`workCenter`) — see
  `docs/jb2-api-findings.md` §2. The builder was reworked to match (**CR-018**) and the
  gate-3 walk write-diff re-verified: emitted payload now byte-matches the live-confirmed
  shape, still zero routing PATCH (CR-010). The floor→JB2 write path is now proven against
  the real tenant, not just fake-JB2. (Remaining untested-live: `user_Text*` write, low
  value — not on any critical path.)

## Suite

290 passing (+3 G3-D1 regression = 293 after this gate's commit), ruff clean, single
migration head 0007, live Postgres up/down verified.

**Phase 4 (dashboard + hardening) may begin.**
