# Component Library — Atlas MES (ProjectAMR)

Quick-reference catalog of every sub-component (module, service, data model, API route
group, integration adapter, UI view). **Consult this before starting any task** to avoid
duplicating or contradicting existing components. **Update it whenever a component is added
or changed.**

**Status legend:** `seed` (origin index.html) · `planned` · `in-progress` · `done`.
**Owning system:** SoR for the data this component handles (per CR-001).

## Foundation (Phase 0) — status: done
| Component | Type | Purpose | Key file | Status |
|---|---|---|---|---|
| App skeleton | module | FastAPI app, lifespan init, router wiring, static mount | `app/main.py` | done |
| Config | module | env-driven settings (DB path, JB2 creds/filter) | `app/config.py` | done |
| DB layer | module | sqlite connect + idempotent schema init + `now()` | `app/db.py` | done |
| Schema | data | all-domain SQLite schema, applied idempotently | `app/schema.sql` | done |
| Web deps | module | shared Jinja templates + per-request connection | `app/deps.py` | done |
| Base layout + CSS | UI | nav shell, self-contained CSS, tablet-friendly | `templates/engineer/base.html`, `static/app.css` | done |
| Dashboard | UI view | seed stat-grid + recent routings/line-balances | `templates/dashboard.html` | seed→done |
| JSON `/api/*` | API route | models/operations/routings/line-balances for dashboard | `app/main.py` | done |
| Demo seed | service | seeds models/ops/routing+instructions/jobs when DB empty | `app/services/demo_seed.py` | done |
| Health | API route | `/healthz` liveness + jb2_enabled flag | `app/main.py` | done |

## Domain 4 — Routing/Work-Instruction master (MES) — Phase 1, done
| Component | Type | Purpose | Key file | Owning | Status |
|---|---|---|---|---|---|
| models / operations | data + routes | model + operation-catalog CRUD | `routers/master.py` | MES | done |
| routings (versioned) | data + routes | versioned routing; publish; new-version supersede | `routers/master.py` | MES | done |
| routing_operations | data | ordered ops within a routing version | `schema.sql` | MES | done |
| work_instructions | data + routes | sequenced steps on a routing op | `routers/master.py` | MES | done |
| line_balances | data + routes | takt-time balance tied to a routing version | `routers/master.py` | MES | done |
| routing UI | UI views | list, detail (steps+instructions), models, operations, line-balances | `templates/routing*.html` etc. | MES | done |

## Integration — JB2 read-only mirror (JB2) — Phase 2, done (live creds pending)
| Component | Type | Purpose | Key file | Owning | Status |
|---|---|---|---|---|---|
| JB2 adapter | integration adapter | filtered polls, dedup upsert, reconcile; INT-5 guard | `app/services/jb2_adapter.py` | JB2 | done (needs creds) |
| jobs mirror | data | read-only cache of JB2 jobs (dedup on jb2_job_number) | `schema.sql` | JB2 | done |
| applied_routings mirror | data | per-order JB2 order-routings | `schema.sql` | JB2 | done |
| reconciliation_log | data + view | cross-system divergence, surfaced not overwritten | `routers/jb2.py` | — | done |
| jobs UI + sync | UI + route | jobs list, sync trigger | `routers/jb2.py`, `templates/jobs.html` | JB2 | done |
| instruction overlay | service | match MES instructions onto JB2 applied-routing ops (WI-4) | `routers/wip.py:_instruction_overlay` | MES/JB2 | done |

## Domain 1 — WIP move engine (MES) — Phase 3, done
| Component | Type | Purpose | Key file | Owning | Status |
|---|---|---|---|---|---|
| wip_events | data | append-only move-event log (source of truth for WIP) | `schema.sql` | MES | done |
| WIP service | service | compute_state (replay), record_event (+hold guard), release/advance | `app/services/wip.py` | MES | done |
| WIP routes | API route | release / move / advance / ack | `routers/wip.py` | MES | done |
| instruction_acks / deviations | data | operator acks (WI-5) + out-of-sequence deviations (WI-6) | `schema.sql`, `routers/wip.py` | MES | done |
| job detail | UI view | per-step cells, moves, instructions+ack, NCR flag, audit | `templates/job_detail.html` | MES | done |

## Domain 2 — Scheduling & dispatch (MES) — Phase 4, done
| Component | Type | Purpose | Key file | Owning | Status |
|---|---|---|---|---|---|
| Sequencing service | service | dispatch board + station read-models over WIP | `app/services/sequencing.py` | MES | done |
| dispatch_overrides | data | planner pin/bump (SD-5) | `schema.sql` | MES | done |
| dispatch board / stations / station | UI views | planner board, station list, kiosk station view | `templates/dispatch.html`,`stations.html`,`station.html` | MES | done |

## Domain 3 — Quality / NCR (MES) — Phase 5, done
| Component | Type | Purpose | Key file | Owning | Status |
|---|---|---|---|---|---|
| ncrs | data | NCR linked to WIP op instance; immutable once closed | `schema.sql` | MES | done |
| Quality service | service | create/hold/review/disposition (drives WIP) + correct | `app/services/quality.py` | MES | done |
| Quality routes + UI | API + view | NCR list, create, review, disposition, correction | `routers/quality.py`,`templates/quality.html` | MES | done |

## Verification
| Component | Type | Purpose | Key file | Status |
|---|---|---|---|---|
| Smoke test | test | asserts WIP replay, hold guard, disposition, overlay, immutability | `test_smoke.py` | done |

## Future stubs (Phase 6, documented only — NOT built)
OEE/telemetry (PA), full genealogy (PTG), maintenance (MM), JB2 write-back (`time-tickets`,
INT-9). Not promoted into this catalog until built.
