# Phase 4 Gate — Dashboard + metrics + hardening

**Executed by:** Fable, 2026-07-15.
**Verdict: PASS** (pilot-ready on one product line; carried exceptions all non-blocking).

## Acceptance criteria (DD §19 Phase 4 → N1–N5)

| # | Requirement | Result | Evidence |
|---|---|---|---|
| **N1** | Availability — floor functions during JB2/cloud outage; recovery from backup | **PASS (floor-during-outage)** / **PARTIAL (backup)** | `test_offline.py`-class + outbox: floor actions succeed with JB2 down, outbox accumulates and drains on recovery; auto-closed sessions withhold JB2 post until lead confirm. Backup is stopgap-only per **CR-008** (deferred by Will) — nightly `pg_dump` timer ships, full offsite+drill deferred. Single-server risk accepted (DD N1). |
| **N2** | Performance — scan<1s, dashboard<2s, 25 stations | **PASS** | `tools/perf_check.py` / `docs/gates/phase4_perf.md` on real pg16, 50-unit WIP: dashboard pipeline p95 **352ms** (budget 2000), scan-accept p95 **~20ms** (budget 1000), drill-down p95 **~40ms**, metrics p95 **~35ms**. Board is N+1 (≈`units×8`) — fine at 50, linear scaling risk flagged for several-hundred-active-unit shops (batch follow-up recommended, not needed for one-line pilot). Two EXPLAIN-proven indexes **applied** (migration 0009). Real 25-connection concurrency test deferred to pre-pilot (TestClient can't parallelize; serial headroom is 18% of budget). |
| **N3** | Data integrity — server-side state machine, append-only, no cascade deletes | **PASS** | P3-14 property suite (obligations a–g); events table has no UPDATE/DELETE path; all FKs NO ACTION (0001/0005/0006/0007 verified); state transitions in DB transactions. |
| **N4** | Security — secrets, least-privilege, TLS, roles, no public exposure | **PASS w/ one accepted finding** | P4-10 review (below). |
| **N5** | Observability — JSON logs, /health*, alerts | **PASS** | Structured JSON logging; `/health` `/health/jb2` `/health/sync` `/health/outbox`; P4-05 alerts (outbox/sync-stall/station-offline, email or log-only) + station heartbeat via require_station. |

## P4-10 security review (Fable)

Clean: no hardcoded secrets (`.env`, gitignored, never in history); scrypt PIN hashing
(memory-hard); HMAC-SHA256 signed operator cookies; **all** SQL parameterized (metrics
`text()` uses bound `:cutoff`, static view names — no injection); path-traversal guards
on all three artifact servers (`.resolve()` + `parent==base`, build-record double-checked
for free-text serial keys); TLS internal in Caddyfile; floor endpoints gated
(station+operator+role, 39 dep refs); no public ingress (LAN + Caddy).

**Finding — CR-017 (accepted for v1, top hardening item):** office/admin pages
(products, library, workorders, boxes, health) have **no auth deps** — any LAN host can
author/publish instructions, remap products, kit-up, print badges unauthenticated.
Mitigated by the DD's internal-LAN-only posture (NF-2, §14). Fix before any broader
exposure: `require_role('admin')` on the `/admin/*` group. Not a pilot blocker.

## Dashboard (P4-02) vs approved visual

Pipeline board matches `pistol_flow_visual.html §4`: one card per unit, all 7 CR-004
states + colors (`#1f6f8b`/`#c0392b`/`#f2a541` + state colors), KPI strip, provenance,
grouping toggles. Live updates via SSE (CR-015) — event-table poll, <2s, multi-worker-safe.

## Carried exceptions (none block pilot)

- **CR-012 / P0-R1** (write-path live probe) — blocked on Will's dummy job number.
  Payload proven vs fake-JB2; `_time_fields()` isolates the one unresolved choice.
- **CR-008** — backup deferred; stopgap dump only.
- **CR-017** — admin-auth hardening (above).
- Board N+1 batching + real concurrency test — pre-pilot follow-ups.

## Suite

331 passing, ruff clean, migration head **0009**, live pg16 up/down verified through 0009.

**All four gates PASS. Proceed to closeout (dataflow.html, final CHANGE_REQUESTS).**
