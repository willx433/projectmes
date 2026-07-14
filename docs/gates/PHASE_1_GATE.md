# Phase 1 Gate — Skeleton + sync

**Executed by:** Fable, 2026-07-14.
**Verdict: CONDITIONAL PASS** (one carried exception: live Postgres — see below).

## Acceptance criteria (DD §19 Phase 1)

| Criterion | Result | Evidence |
|---|---|---|
| New JB2 order appears in MES ≤ 90 s | PASS | Orders cadence 60 s; integration test `test_sync.py` (fixture order lands next cycle); **live read sync against the real tenant**: 15 orders (30-day window), 257 line items, 556 routings, 465 materials, 8,795 parts, 383 documents, 42 work centers, 32 op codes, 102 employees, 21 reason codes — zero errors. |
| Sync survives restarts/outages | PASS | Tests: checkpoint resume after simulated restart; per-resource failure isolation (one 500 doesn't stop others, recorded in sync_runs); circuit breaker open/half-open; outbox accumulates during outage and drains after. |
| Repo, CI, deploy pipeline, Caddy, Postgres, migrations | PASS (files) / DEFERRED (live DB) | CI green (86 tests, ruff, guards); systemd units pass `systemd-analyze verify`; migrations verified via offline SQL only — **no Postgres available on the dev box**. |
| Admin health pages | PASS | /health, /health/jb2, /health/sync, /health/outbox truthful under healthy/breaker-open/backlog states (tests); /admin/health renders with outbox replay. |

## Defects found during gate (both fixed and re-verified)

- **G1-D1** — initial backfill unbounded: first live run mirrored 16,938 historical orders and the line-item child fan-out (3 calls/item, 0.5 s throttle) would have taken days. Fix (P1-R1): first-run checkpoint floor = now − `SYNC_BACKFILL_DAYS` (default 30) persisted immediately; child fan-out skipped for closed orders. Re-verified live: full first sync in 398 s, zero errors.
- **G1-D2** — the 30-day floor also windowed the small master tables (rarely modified → 0 rows fetched: reason-codes/employees/work-centers/op-codes came back empty). Fix: masters set `supports_last_mod=False` (always full-pull + hash-diff — they are tiny). Re-verified live: 42/32/102/21 rows.

## Known limitations accepted (documented in code)

- `/health/jb2` breaker state is process-local; sync/outbox workers run as separate
  systemd units in prod, so the API's view is its own client only. Upgrade path
  (persisted heartbeat) noted in `app/jb2/client.py`.
- Outbox `sent→confirmed` collapses to one transition until Phase 3 adds
  verification reads (documented in drainer docstring).

## Carried exceptions

1. **Live Postgres verification** (migrations, JSONB/UUID DDL, real `alembic upgrade head`) —
   dev box has no Docker/Postgres/sudo. **Precondition of Gate 2.** Needs Will:
   Docker Desktop WSL integration, `apt install postgresql`, or a connection string
   to the Ubuntu server's Postgres.
2. Gate 0's write-path items (CR-012) remain a Gate 3 precondition — unchanged.
3. Deploy runbook unexecuted (no server access from this session) — verify at first
   real deploy; not a blocker for Phase 2 development.

## Remediation tasks

None open. G1-D1/G1-D2 closed within the gate.

**Phase 2 may begin** (library + plans + PDFs); exception 1 must clear before Gate 2 runs.
