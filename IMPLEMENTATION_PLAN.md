# Atlas MES — Implementation Plan

**Status:** v1.0 — 2026-07-14. Execution-ready plan for an AI agent team.
**Governing documents (precedence order):**
1. `documentation/MES_Product_Design_Document.md` (PDD) — wins on philosophy.
2. `documentation/MES_Design_Document.md` (DD, v1.1) — wins on technical matters. Numbered requirements are the contract.
3. `documentation/JB2_vs_MES_Capability_Split.md` — never build the left column; never expect JB2 to accept the right column.
4. `documentation/pistol_flow_visual.html` **§4** — approved visual design for the pipeline dashboard (one card per gun; first-pass / rework / stalled / overdue / queued / in-transit states; KPI strip; provenance-tagged fields; color language JB2 `#1f6f8b`, MES `#c0392b`, write-backs `#f2a541`).

**Non-negotiable constraints (restated):** Python 3.12 / FastAPI · PostgreSQL 16 · Caddy · one Linux server · systemd. No new infra without a CR. Phase 0 blocks everything. Never build what JB2 owns. Never block a floor action on a JB2 round trip. All JB2 writes go through the idempotent outbox — no direct API calls from request handlers.

**Deployment & credentials (confirmed by Will 2026-07-14):** target = Will's Ubuntu Linux server, fronted by Caddy. Live JB2 credentials live in **repo-root `.env`** (gitignored — never commit): `JobBoss2__ApiBaseUrl`, `JobBoss2__AuthBaseUrl`, `JobBoss2__ClientId`, `JobBoss2__ClientSecret`. All config loaders use these exact key names. **Every user-facing screen (station, dashboard, admin/library) must be mobile-friendly** — Android tablets are the primary floor device; hardware scanners/kiosk enrollment are bypassed for now (manual-entry fallbacks required, CR-009).

---

## 0. Documented contradictions (flagged, not silently resolved)

| # | Contradiction | Resolution per precedence | Logged |
|---|---|---|---|
| X1 | Legacy governing docs (`requirements.md` INT-9/NF-1, `change-requests.md` CR-001: SQLite stack, MES = read-only island vs JB2, quantity-based WIP) vs DD (PostgreSQL 16, write-backs via outbox, unit-level tracking). | DD wins (technical). Legacy doc set and app are **superseded**, archived, mined for reusable logic only. | docs/CHANGE_REQUESTS.md CR-001 |
| X2 | DD §16.1 offers "Docker Compose **or** bare systemd"; user constraint fixes systemd. Existing repo is Docker-only. | systemd for prod (DD's assumed default). Docker Compose retained **dev-only convenience**, non-authoritative. | CR-002 |
| X3 | DD §12.3 leaves HTMX vs React open (§18.4). PDD P8 (boring tech, no build chain) and DD's own stated preference point one way. | **HTMX + Alpine.js** chosen now (architecture decision, this plan). Templates shared with WeasyPrint per DD §8. Escape hatch: if the library builder's drag/annotate UX exceeds HTMX, that one screen may become a self-contained JS island — via CR, not improvisation. | CR-003 |
| X4 | pistol_flow_visual §4 shows an **in-transit** card state and KPI strip; DD §13.1 lists green/amber/red/purple/grey/hatched but no in-transit card, and the visual omits `blocked_no_instructions` (hatched). | Union of both: implement all DD §13.1 states **plus** the visual's in-transit card and KPI strip. Visual governs look; DD governs the state list. | CR-004 |
| X5 | DD §14 Caddy terminates TLS (internal CA); existing Caddyfile is plain :80. | DD wins — TLS internal. | (covered by CR-001) |
| X6 | Working copy had no `.git` despite a linked GitHub repo. | **Resolved 2026-07-14:** `git init -b main` + remote `origin = https://github.com/willx433/projectmes.git` (remote verified empty — no history conflict). `.env` confirmed gitignored (`git status` shows only `.env.example`, which contains no secrets). P0-01 makes the baseline commit/push after the legacy restructure. | — |

---

## 1. Codebase assessment

Verdict key: **keep** (use as-is) · **refactor** (reshape into new tree) · **replace** (rebuild to DD spec; old file archived) · **discard** (archive, no successor).
All "replace/discard" files move to `legacy/` in P0-01 — nothing is deleted; the old app remains runnable from `legacy/` for reference.

### Documentation & governance

| File | Verdict | Justification |
|---|---|---|
| `documentation/*.md`, `pistol_flow_visual.html` | **keep** | The contract. Read-only. |
| `requirements.md`, `implementation-plan.md`, `library.md`, `change-requests.md`, `research-notes.md` | **discard** (archive) | Governing docs of the superseded build (X1). CR-001 records the supersession; new change log lives in `docs/CHANGE_REQUESTS.md`. |
| `README.md` | **replace** | Rewrite for the new system in P1-01. |
| `index.html` (root seed) | **discard** | Seed for the legacy engineer dashboard; superseded by DD §12/§13 UIs. |
| `*:Zone.Identifier` files | **discard** | Windows download metadata, delete outright. |

### Infrastructure

| File | Verdict | Justification |
|---|---|---|
| `Caddyfile` | **replace** | No TLS, wrong upstream layout; DD §16.2 sketch is the target. |
| `Dockerfile`, `docker-compose.yml` | **refactor** → `deploy/dev/` | Dev-only convenience (X2); prod is systemd. Needs Postgres service added for dev. |
| `amr.db`, `amr.db-*`, `__pycache__/` | **discard** | Legacy SQLite artifacts. Add to `.gitignore`. |

### Python app (`app/`) — legacy generation, built to superseded requirements

| File | Verdict | Justification |
|---|---|---|
| `app/main.py`, `config.py`, `deps.py` | **replace** | Trivial (103 lines total); new app needs app-factory, Postgres, worker processes — nothing worth carrying. |
| `app/db.py` | **replace** | SQLite + idempotent-script "migrations"; DD §10 mandates Postgres + Alembic. |
| `app/schema.sql` | **replace** | Models a quantity-based, order-of-magnitude-simpler domain (no units, boxes, sessions, substeps, measurements, outbox). DD §10 schema is the target. |
| `app/services/jb2_adapter.py` | **replace** (mine it) | Read-only, no OAuth, no retries/backoff/circuit-breaker, uses `shopview/get-jobs` as the jobs source instead of `orders`/`order-line-items` per DD §4.2. **Carry forward its hard-won constants:** filter-required guard (unbounded reads 500), MES-side dedup (JB2 ignores Idempotency-Key), UTC date format, `field[op]=` filter syntax. |
| `app/services/wip.py` | **discard** (reference) | Sound append-only/replay engine, but for divisible *quantities* at routing steps. DD §5 tracks serialized **units** with substep executions — different state machine. Its hold-guard and replay-equivalence *test ideas* carry into P3-14. |
| `app/services/quality.py` | **discard** (reference) | NCR-on-quantity model; DD §6.5 failure/disposition model is per-unit with lead badge auth. Disposition-drives-WIP-event pattern is the right instinct — reuse the pattern, not the code. |
| `app/services/sequencing.py` | **discard** | Dispatch board is explicitly not in DD scope (JB2 APS owns scheduling; MES displays). |
| `app/services/demo_seed.py` | **replace** | New seed script per DD N7 (demo product + instruction set for training). |
| `app/routers/*.py` | **discard** | Thin wrappers over discarded services; endpoints don't match DD §11. |
| `app/templates/engineer/base.html`, `static/app.css` | **refactor** | Nav-shell + self-contained tablet-friendly CSS pattern is exactly PDD P8/P10; carry the approach (and any surviving CSS) into the new shared template base. |
| `app/templates/*.html` (13 views) | **discard** | Views of the discarded domain. `stations.html`/`station.html` layout ideas may inform DD §12.2 screens, but the screens are specified fresh. |
| `test_smoke.py` | **discard** (reference) | Tests the discarded WIP engine; its assertion style (replay equivalence, hold enforcement, immutability) seeds the Phase 3 property tests. |
| `requirements.txt` | **replace** | New pins: + psycopg, SQLAlchemy/Alembic, WeasyPrint, segno, pytest, ruff, httpx (kept). |

**Net:** ~3,400 lines exist; roughly 200 lines of *ideas* survive (JB2 wire-format constants, base-template pattern, test philosophy). Nothing usable is being rebuilt — the two systems share a name, not a domain model.

### New repo layout (DD §19)

```
ProjectAMR/
  app/            api/  domain/  jb2/  sync/  outbox/  pdf/  ws/  auth/
  templates/      (shared: station UI + PDF)   static/
  migrations/     (alembic)
  tests/          unit/  statemachine/  integration/  fixtures/jb2/
  tools/          jb2probe.py  gen_dataflow.py  seed_demo.py
  deploy/         Caddyfile  systemd/  deploy.sh  backup/  dev/ (compose)
  docs/           jb2-api-findings.md  ESCALATIONS.md  CHANGE_REQUESTS.md  gates/  runbooks/
  legacy/         (entire superseded build, frozen)
```

---

## 2. Agent tiering policy (binding on every task)

| Tier | Does | Never does |
|---|---|---|
| **Fable** | Architecture & judgment: interface/contract definitions, DB migration review, state-machine design, security review, JB2 payload-mapping sign-off, phase-gate execution, all escalations & ambiguity resolution. | Routine implementation code. |
| **Sonnet** | All standard implementation: endpoints, workers, services, templates, tests, docs. | Architectural improvisation. |
| **Haiku** | Mechanical work: scaffolding, fixtures, repetitive CRUD, formatting, lint, boilerplate templates. | Anything requiring a design decision. |

**Escalation protocol:** a Sonnet/Haiku agent hitting an architectural question mid-task **stops**, appends the question to `docs/ESCALATIONS.md` (template in that file), marks the task blocked, and hands off. Fable answers in the same file (and raises a CR if the answer deviates from the DD). No cheap-tier architecture, ever.

**Tier column note:** `F/S` means Fable designs or reviews the named artifact, Sonnet implements. Review is part of the task's definition of done, not a separate task.

---

## 3. Work breakdown by phase

Task fields: **ID · Description · Tier · Deps · DD ref · Files · Acceptance criteria (AC)**. All tasks ≤ 1 agent-day. Files are relative to repo root; `M:` = new Alembic migration.

### Phase 0 — JB2 behavioral verification (≈3 days) — BLOCKS EVERYTHING

Goal: resolve every `[VERIFY-JB2]` tag in `docs/jb2-api-findings.md`; record fixtures for the fake-JB2 server. Needs live tenant credentials (see §8 Waiting-on).

| ID | Description | Tier | Deps | DD ref | Files | AC |
|---|---|---|---|---|---|---|
| P0-01 | Git already initialized on `main` with remote `origin = https://github.com/willx433/projectmes.git` (empty remote, `.env` gitignored — done 2026-07-14). Remaining: move legacy build (`app/`, `test_smoke.py`, `index.html`, old governing .md files, old `Caddyfile`) to `legacy/`; delete Zone.Identifier files; scaffold new tree per §1; baseline commit + first push. | Haiku | — | §19 | repo-wide | `git log` shows baseline; no secret in history (`git log --all -p -- .env` empty); `legacy/` app still runs; new tree matches §1 layout; push visible on GitHub. |
| P0-02 | Auth spike using **repo-root `.env`** (keys: `JobBoss2__ApiBaseUrl`, `JobBoss2__AuthBaseUrl`, `JobBoss2__ClientId`, `JobBoss2__ClientSecret` — the canonical credential location for all phases): obtain token from ECI auth endpoint; measure TTL; enumerate which APIs are activated for Atlas creds; document refresh strategy. | Sonnet | P0-01 | §4.1 | tools/jb2probe.py, docs/jb2-api-findings.md §1 | Probe reads creds from `.env` only (never hard-coded); findings record TTL, refresh cadence, activation matrix; probe re-runnable. |
| P0-03 | Probe harness: `tools/jb2probe.py` subcommands per probe; **records every request/response pair to `tests/fixtures/jb2/*.json`** (scrubbed of secrets) for the fake server. | Sonnet | P0-02 | §4.7, N6 | tools/jb2probe.py, tests/fixtures/jb2/ | Each probe run emits replayable fixture files; secrets never in fixtures (grep check). |
| P0-04 | Time-ticket round trip on a dummy job: header+detail relationship, `timeStart/End` vs `setupTime/cycleTime` semantics, appearance in JB2 UI/costing, PATCH of details. | Sonnet | P0-03 | §4.4, §4.7.2 | docs/jb2-api-findings.md §2 | Findings answer all §4.7.2 questions with recorded evidence; dummy-job cleanup steps documented (no API void path exists — manual dashboard cleanup noted). |
| P0-05 | Routing PATCH probe: writable field set of `OrderRoutingUpdate` (`actualPiecesGood/Scrap`, `actualStart/EndDate`, `status`), JB2 UI effects; fallback decision if `status` is derived. | Sonnet | P0-03 | §4.7.3, C7/C8 | docs/jb2-api-findings.md §3 | Every candidate field marked writable/rejected/derived with response evidence. |
| P0-06 | Incremental-sync probe: `lastModDate[gte]` behavior on orders / line-items / routings — timestamp granularity, timezone, boundary inclusivity, the `revisedDate` null-test 500. | Sonnet | P0-03 | §4.7.5, §4.1 | docs/jb2-api-findings.md §4 | Checkpoint algorithm (overlap window size) specified from measured behavior. |
| P0-07 | Rate-limit probe (polite ramp), `GET /reason-codes` dump → seed mapping data, `user_*` field writability on `OrderLineItemUpdate` (§4.7.6). | Sonnet | P0-03 | §4.7.4/6/7 | docs/jb2-api-findings.md §5–7, tests/fixtures/jb2/reason-codes.json | Client throttle number chosen from evidence; reason-code fixture saved; user-field verdict recorded. |
| P0-08 | Consolidate findings; resolve **every** `[VERIFY-JB2]` tag; define the frozen JB2 client contract (throttle, retry, checkpoint overlap, payload shapes) that Phase 1+ codes against. | **Fable** | P0-04..07 | §4.7 | docs/jb2-api-findings.md (final) | Zero unresolved `[VERIFY-JB2]`; contract section signed; `openapi.json` snapshot committed. |
| P0-09 | Fixture curation: normalize recorded fixtures into the fake-JB2 corpus (happy path + error shapes + pagination pages). | Haiku | P0-08 | N6 | tests/fixtures/jb2/ | Fixture index file lists every endpoint/scenario; JSON validates. |

**GATE 0** (see §5).

### Phase 1 — Skeleton + sync (1–2 wks)

Goal: new JB2 order appears in the MES mirror ≤ 90 s; sync survives restarts/outages; outbox machinery exists (empty).

| ID | Description | Tier | Deps | DD ref | Files | AC |
|---|---|---|---|---|---|---|
| P1-01 | App skeleton: FastAPI app factory, env config loaded from repo-root `.env` (JB2 keys as in P0-02, plus `DATABASE_URL`, `MES_SECRET_KEY`, `ARTIFACT_DIR`), structured JSON logging, `/healthz`; new README. | Sonnet | P0-01 | §16.3, N5 | app/main.py, app/config.py, app/logging.py, README.md | App boots against empty Postgres; config resolves the exact `.env` key names (no rename required of Will's file); health returns build version; logs are JSON. |
| P1-02 | Alembic baseline migration: all `jb2_*` mirror tables, `sync_runs`, `jb2_outbox`, `mapping_exceptions` exactly per DD §10. | S (**F reviews migration**) | P1-01 | §10 | M:0001_mirrors, app/domain/models_jb2.py | `alembic upgrade head` on empty DB = DD §10 mirror DDL; downgrade clean; Fable review note in PR. |
| P1-03 | JB2 client `app/jb2/client.py`: bearer auth + refresh (per findings), `fields=` on every GET, filter-required guard, retries w/ exponential backoff+jitter, 10 s timeout budget, throttle from P0-07, circuit breaker → offline mode flag, structured call logging. | Sonnet | P0-08, P1-01 | §4.1, R3/R4 | app/jb2/client.py | Unit tests prove: no unfiltered GET possible; breaker opens after N failures and half-opens; every call logged with latency/status. |
| P1-04 | Fake-JB2 server: FastAPI test app replaying `tests/fixtures/jb2/` (filtering, take/skip paging, lastModDate windows, error injection). | Sonnet | P0-09 | N6 | tests/fake_jb2/server.py | Whole test suite runs with zero network; error-injection hooks per endpoint. |
| P1-05 | Sync worker framework: per-resource poll loops with cadences per DD §4.2 table, `lastModDate` checkpoints (+overlap from P0-06), `content_hash` diffing, `sync_runs` recording, restart-safe. | Sonnet | P1-02, P1-03 | §4.2 | app/sync/worker.py, app/sync/checkpoints.py | Kill/restart mid-poll loses nothing (checkpoint replays overlap); every run rows into `sync_runs`. |
| P1-06 | Sync: orders + order-line-items (60 s cadence), incl. cancel/close detection flags. | Sonnet | P1-05 | §4.2, §4.3.3/4 | app/sync/resources/orders.py | Fixture order lands in mirror ≤ 90 s of appearing in fake-JB2; change + cancel reflected. |
| P1-07 | Sync: order-routings + job-materials/requirements triggered on new/changed order. | Sonnet | P1-06 | §4.2 | app/sync/resources/routings.py, materials.py | Order import pulls its full routing + planned materials; re-import idempotent. |
| P1-08 | Sync: parts/estimates, work-centers, operation-codes, employees, reason-codes, documents (15 min cadence; on-import for documents). | Haiku | P1-05 | §4.2 | app/sync/resources/masters.py | All six mirrors populate from fixtures; repeat runs no-op via hash. |
| P1-09 | Outbox: `jb2_outbox` writer API (same-transaction insert), drainer worker — per-work-order FIFO, deterministic idempotency keys, backoff, `pending→sent→confirmed/failed`, park-on-permanent-error, manual replay hook. | S (**F reviews contract**) | P1-02, P1-03 | §4.5, R3 | app/outbox/writer.py, app/outbox/drainer.py | Tests: duplicate drain never double-posts (idempotency); JB2 500s retry with backoff; permanent 4xx parks; local record never deleted. |
| P1-10 | Admin health pages: `/health`, `/health/jb2`, `/health/sync`, `/health/outbox` + minimal admin UI (sync runs table, outbox queue, mapping exceptions, breaker state banner). | Sonnet | P1-05, P1-09 | §11, §9.9, 17.1 | app/api/health.py, templates/admin/health.html | All four endpoints JSON-truthful under: healthy, JB2 down (breaker open), outbox backlog. |
| P1-11 | Deploy to Will's Ubuntu server: systemd units (`mes-api`, `mes-sync`, `mes-outbox`), Caddyfile per §16.2 (TLS internal, static+artifacts routes), `deploy.sh` (migrate → restart), `.env` with 0600 perms; **stopgap nightly `pg_dump` timer to a second local directory (CR-008)**; dev compose (api+postgres) under `deploy/dev/`. | Sonnet | P1-01 | §16.1/2, §14, §16.4 | deploy/systemd/*, deploy/Caddyfile, deploy/deploy.sh, deploy/dev/compose.yml | Runbook walk-through boots all services on the target server; Caddy serves TLS; deploy.sh idempotent; dump timer fires. |
| P1-12 | CI: pytest + ruff on every commit; fake-JB2 fixture tests in CI; coverage floor on `app/jb2`, `app/sync`, `app/outbox`. | Sonnet | P1-04 | N6, §16.5 | .github/workflows/ci.yml (or local runner script) | CI red on lint or test failure; suite needs no network. |
| P1-13 | Display feeds: APS schedule + ShopView polls into display-only cache. **CR-011:** both endpoints ignore all query params (full 18 MB / 6.5 MB payloads, 30 s+, 504s) — bespoke 60–120 s timeout, cadence ≥5 min, streamed parse, isolated worker. | Haiku | P1-05 | §4.2, C24, CR-011 | app/sync/resources/display.py | Schedule JSON cached and served at `/api/v1/jb2-schedule`; a 504/timeout on these feeds never affects other sync (test with error injection). |

**GATE 1.**

### Phase 2 — Library + plans + PDFs (2–3 wks)

Goal: create an Apollo instruction set → a JB2 Apollo order auto-produces a work order, frozen plan, and PDF with correct routing binding.

| ID | Description | Tier | Deps | DD ref | Files | AC |
|---|---|---|---|---|---|---|
| P2-01 | Migration: `products`, `product_part_map`, `instruction_sets`, `steps`, `substeps`, `failure_codes` per §10; uniqueness (product, operation_match, version). | S (**F reviews**) | P1-02 | §10 | M:0002_library | Upgrade/downgrade clean; constraints enforce version uniqueness and scope rules (global vs product). |
| P2-02 | Products + part-map + variant attributes CRUD (API + admin UI); unmapped part numbers → `mapping_exceptions` to-do list. | Haiku | P2-01 | §4.6, §5 | app/api/products.py, templates/admin/products.html | Map `APOLLO-9-*` aliases → one product with variant extraction; unmapped part surfaces as admin to-do, never crashes sync. |
| P2-03 | Instruction-set domain service: version lifecycle `draft→in_review→published→retired`, edit-published-creates-draft, publish bump, global-scope sets with product override, single/two-person publish behind a config flag. | Sonnet | P2-01 | §7.1, §7.3 | app/domain/library.py | State-machine unit tests: illegal transitions rejected; publishing never mutates prior versions; override resolution correct. |
| P2-04 | Builder UI: tree editor (set → steps → substeps), drag reorder, per-type substep create; HTMX. | Sonnet | P2-03 | §7.2 | templates/library/*.html, app/api/library.py | Author a 3-step/12-substep set entirely in UI; reorder persists; no JS build chain. |
| P2-05 | Substep editors per type: rich-text subset (bold/lists/callouts), measurement spec (name/unit/nominal/±tol/gauge/decimals), tool refs, signoff role, video link. | Sonnet | P2-04 | §7.2, §5 | templates/library/substep_*.html | Each of the 6 substep types round-trips through its editor; measurement spec validates (tol signs, decimals). |
| P2-06 | Image upload + annotation (arrows/circles), local storage under `ARTIFACT_DIR`, client-side resize ≤2 MP. | Sonnet | P2-04 | §7.2, C21 | app/api/media.py, static/annotate.js | Annotated image saves as new artifact (original kept); served locally only. |
| P2-07 | Conditional content: substep `condition jsonb` evaluated against work-order variant values at plan generation; builder UI for conditions. | Sonnet | P2-03 | §7.2, §4.6 | app/domain/conditions.py | 9mm-only substep appears in a 9mm plan, absent in .45 plan; malformed condition = validation error at author time, never at floor time. |
| P2-08 | Publish flow: diff vs current published, approver gate (config), clone across products; failure-code taxonomy editor (+ `jb2_reason_number` mapping from P0-07 fixture). | Sonnet | P2-03 | §7.2, §4.4 | app/api/publish.py, templates/library/diff.html | Diff shows step/substep-level changes; clone Apollo→Athena editable copy; every failure code maps or explicitly opts out of a JB2 reason. |
| P2-09 | Binding resolver: (product, exact op code) → (product, work center) → (product, fuzzy description, flagged) → unbound placeholder step; per DD §6.2. | Sonnet | P2-03, P1-07 | §6.2 | app/domain/binding.py | Unit tests cover all four rungs incl. fuzzy-flag and `blocked_no_instructions` placeholder. |
| P2-10 | Order ingestion → execution: WorkOrder + Units + **frozen** ExecutionPlan (`frozen_content` full copy), status flow incl. `blocked_no_instructions`; migration for `work_orders`, `units`, `plan_operations`, `plan_pdfs`. | S (**F reviews freeze semantics + migration**) | P2-09, P1-06 | §4.3, §5, §10 | M:0003_execution_core, app/domain/workorders.py | Fake-JB2 Apollo order → work order with frozen plan ≤ 90 s; publishing v(n+1) does not alter in-flight plan bytes (hash-compared); unmapped order → `blocked_no_instructions`, dashboard-flagged. |
| P2-11 | PDF build guide: WeasyPrint from the same Jinja templates as station UI; cover (order, product, variant, box QR via segno, serial placeholders), per-op sections, measurement tables with blank cells, plan-hash footer; immutable artifacts `artifacts/{wo}/guide-v{n}.pdf`. | Sonnet | P2-10 | §8 | app/pdf/guide.py, templates/guide/*.html | PDF regenerates as v+1 never overwrite; content matches frozen plan (spot-checked fields); renders in <30 s for a 7-op plan. |
| P2-12 | QR/badge printing: badge sheet PDF (`OP:{uuid}`), box label sheet (`BOX:` payloads), reusing the PDF generator. | Haiku | P2-11 | §14, §8 | app/pdf/badges.py | Printed payloads scan back to the right operator/box in tests (payload round-trip). |
| P2-13 | Order-change reconciliation: qty↑ adds units, qty↓ flags for lead pick, cancel → `cancelled`/`cancel_requested`, routing drift → `routing_drift` flag; per §17.4/17.5. | Sonnet | P2-10 | §17.4/5, §4.3 | app/domain/reconcile.py | Each of the five change scenarios produces exactly the specified state + flag; nothing silently mutates a frozen plan. |
| P2-14 | Demo seed: training product + published instruction set + fake order (DD N7); replaces legacy demo_seed. | Haiku | P2-10 | N7 | tools/seed_demo.py | Fresh install + seed → one plan + PDF visible end to end. |

**GATE 2.**

### Phase 3 — Floor execution (2–3 wks)

Goal: a unit travels 3 stations end-to-end; JB2 (fake, then live smoke) shows correct time tickets and quantities; §9.1–9.4 data queryable.

| ID | Description | Tier | Deps | DD ref | Files | AC |
|---|---|---|---|---|---|---|
| P3-01 | **Execution state-machine contract** (design, no code): unit/session/substep transitions, scan resolution table, override matrix, idempotency rules — the single server-side validator all endpoints call. | **Fable** | P2-10 | §5, §6.3–6.8, §17 | docs/state-machine.md | Every transition in DD §5 + every §17 edge case has a row: precondition → action → events emitted → outbox effects. Reviewed against §6 workflows line by line. |
| P3-02 | Migration: `build_boxes`, `box_assignments`, `stations`, `operators`, `work_sessions`, `session_pauses`, `step_executions`, `substep_executions`, `measurements`, `attachments`, `material_records`, `failures`, `scrap_events`, `scans`, `transits`, `events`, `auth_events` + §10 indexes. | S (**F reviews**) | P3-01 | §10 | M:0004_floor | DDL matches §10 incl. partial index on active boxes; append-only enforced (no UPDATE/DELETE grants on `events`). |
| P3-03 | Auth: station enrollment (kiosk token, httpOnly), badge QR / PIN operator sessions, idle expiry, one-active-station rule, roles array, office username/password sessions; `auth_events`. | S (**F security review**) | P3-02 | §14 | app/auth/*.py | Floor action requires station token **and** operator session; badge-in elsewhere moves the session; privileged actions demand second badge of correct role; all outcomes in `auth_events`. |
| P3-04 | `POST /scan` resolver + state machine core implementation per P3-01: badge vs box prefix, box→unit→expected-op resolution, wrong-station warning, lead override, unknown/unbound box screens. | Sonnet | P3-01, P3-03 | §6.3, §11 | app/domain/statemachine.py, app/api/scan.py | Property tests (P3-14) green; every §6.3 branch returns its specified machine-readable code. |
| P3-05 | Station UI shell: **mobile-first for Android tablets** (Chrome/Chromium kiosk) — responsive layout, ≥48 px touch targets, no hover-dependent interactions, no horizontal scroll, viewport meta; hidden always-focused scan input (with on-screen manual-entry fallback while hardware scanners are bypassed); idle screen w/ station queue; offline full-screen state; WebSocket per station. | Sonnet | P3-03 | §12.1/2, PDD P10 | templates/station/base.html, static/station.js | Renders correctly at Android-tablet widths (~768–1280 px, portrait + landscape, emulated in CI via Playwright viewport checks); all primary actions reachable without zoom; server-unreachable shows offline screen ≤ 5 s. |
| P3-06 | Execution screens: step rail + substep checklist, per-type controls (action/inspection/photo/material/signoff), server-validated completion, progress; HTMX round trips. | Sonnet | P3-04, P3-05 | §6.4, §12.2.3 | templates/station/execute.html, app/api/substeps.py | Cannot finish a step with required substeps open (server-enforced); each completion writes `substep_executions` + event with operator + timestamps. |
| P3-07 | Measurement entry: keypad overlay, nominal/tol display, instant in/out coloring, out-of-tolerance disposition dialog (accept-w/-deviation [lead], rework, scrap). | Sonnet | P3-06 | §6.4, §12.2.4 | templates/station/measure.html, app/api/measurements.py | Out-of-tol write blocked without disposition; disposition + authorizer recorded; `measurements` row complete per §9.4. |
| P3-08 | Failure / scrap / rework flows: fail → code+narrative+photo → disposition (rework-in-place / send-back-to-op-K reopening intermediates / scrap w/ lead badge + replacement-unit creation); first_pass flips permanently; rework sessions typed. | Sonnet | P3-04, P3-07 | §6.5 | app/domain/failures.py, templates/station/fail.html | Send-back re-opens ops K..N; `first_pass=false` sticks; scrap requires lead role; remake unit links `remake_of_unit_id`. |
| P3-09 | Work sessions: open on accepted scan, pause/resume with reason codes, mid-op clock-out (partial session), idle auto-pause → lead queue, `auto_closed` sessions withheld from JB2 until lead confirms. | Sonnet | P3-04 | §6.4/6.7, §17.8 | app/domain/sessions.py | Session intervals sum correctly across pauses; auto-closed session posts to outbox only after lead confirmation. |
| P3-10 | Finish-operation → outbox: time-ticket header ensure + detail (time or setup/cycle per P0-04 findings, pieces good/scrapped, reason code) — **time tickets are the sole write-back; routing-step PATCH dropped per CR-010** (spec-proven unwritable for actuals/status); rework posts to earlier step; **all via outbox writer, zero direct calls**. | S (**F reviews payload mapping vs findings**) | P3-04, P1-09 | §4.4 (as amended CR-010), §6.6 | app/outbox/payloads.py | Grep proves no `jb2/client` import outside sync/outbox; fake-JB2 receives exactly the CR-010-amended write set for a walked scenario; idempotency keys deterministic. |
| P3-11 | Kit-up + movement: box assign/reassign (lost-box path §17.6), `in_transit` on finish, arrival scan closes `transits` hop, live location; **serial entry at kit-up** — serials pre-exist in Atlas's system (DD §18.1 resolved), the MES records the existing serial, never generates one; required-before-done stays configurable. | Sonnet | P3-04 | §6.1.5, §6.7/8, §17.6, §18.1 | app/domain/boxes.py, templates/station/kitup.html | Transit seconds = t(arrive)−t(finish); reassignment preserves history; kit-up captures an operator-entered serial; unit can't reach `done` without serial when config demands. |
| P3-12 | Photos + materials: `<input capture>` upload → resize → `attachments`; material substep records qty used/scrapped vs plan, substitution w/ authorizer. | Sonnet | P3-06 | §9.5, §12.3 | app/api/attachments.py, app/api/materials.py | Photo lands under artifact dir linked to substep execution; material delta vs `jb2_order_materials` computed and stored. |
| P3-13 | Event/audit plumbing: every state transition appends to `events` (actor, entity, verb, before/after jsonb); helper used by all domain services; per-unit chronological timeline query. | Haiku | P3-02 | §9.10 | app/domain/events.py | Walking the Gate-3 scenario yields a complete gapless timeline; events table has no UPDATE path. |
| P3-14 | State-machine property tests: no illegal transition reachable via API; replay/idempotency (double-finish no-op, duplicate scan safe); NCR-style hold analogue (out-of-tol must disposition). | Sonnet | P3-04..P3-10 | N6, N3 | tests/statemachine/test_transitions.py | Property suite (hypothesis or exhaustive enumeration from P3-01 table) green; every §17 edge case has at least one test. |

**GATE 3.**

### Phase 4 — Dashboard + metrics + hardening (1–2 wks)

Goal: N1–N5 verified; pilot-ready on one product line.

| ID | Description | Tier | Deps | DD ref | Files | AC |
|---|---|---|---|---|---|---|
| P4-01 | WebSocket/SSE hub: event fan-out to dashboard + station queues; read-only TV tokens. | Sonnet | P3-13 | §11, §13.1 | app/ws/hub.py | Dashboard reflects a floor event < 2 s (N2); TV token cannot mutate anything. |
| P4-02 | Pipeline board per **pistol_flow_visual §4**: one card per unit (model, serial, order/line, box, station/op, operator+elapsed, op-segment bar, route %, due (JB2-tagged), footer flags); states green/purple(rework ×n)/amber(stalled)/red(overdue)/grey(queued)/in-transit/hatched(blocked); KPI strip (WIP, first-pass %, in rework, stalled, on-time); provenance note; grouping toggles. | Sonnet | P4-01 | §13.1 + visual §4 | templates/dashboard/pipeline.html, app/api/dashboard.py | Side-by-side render matches the approved visual's card anatomy and color language (#1f6f8b/#c0392b/#f2a541 + state colors); all 7 states representable; live updates. |
| P4-03 | Unit drill-down: full chronological timeline (scans, sessions, substeps, measurements, failures) from `events` + detail tables. | Sonnet | P3-13 | §13.1 | templates/dashboard/unit.html | Every §9.1–9.4 fact of the Gate-3 walk appears in the timeline. |
| P4-04 | Metrics as SQL views + page: FPY (unit+op), scrap Pareto, throughput, actual-vs-estimate per op, queue time by station, rework-hours %, WIP age, JB2 schedule overlay (P1-13). | Sonnet | P3-13 | §13.2, §9.11 | migrations M:0005_views, templates/dashboard/metrics.html | Each metric = one SQL view, hand-checked against the Gate-3 dataset; no separate analytics store. |
| P4-05 | Alerts: email on outbox failures, sync stall > 10 min, station offline > 15 min; station heartbeat. | Sonnet | P1-10 | N5, §9.8 | app/ops/alerts.py | Simulated stall/failure/offline each produce exactly one alert (no storms). |
| P4-06 | Backfill screen: lead enters paper-guide data after an MES outage; rows flagged `backfilled=true`; feeds normal event stream + outbox. | Sonnet | P3-06 | §17.2 | templates/admin/backfill.html, app/api/backfill.py | Backfilled op is distinguishable in timeline and included in metrics; JB2 posting identical to live path. |
| P4-07 | **[DEFERRED by Will 2026-07-14 — CR-008]** Backup/restore: nightly `pg_dump` + WAL archiving + restic (artifacts incl.), restore runbook, drill script; LUKS note. Local nightly `pg_dump` to a second directory ships in P1-11 as a stopgap; full offsite + drill configured post-pilot when Will provides a target. | Sonnet | P1-11 | §16.4, N1, N4 | deploy/backup/*, docs/runbooks/restore.md | (When un-deferred) scripted drill restores a scratch instance to ≤15 min-old state (RPO); Gate 4 checks only the stopgap dump exists. |
| P4-08 | Build-record PDF per serial (recommended-yes on DD §18.7 — pending your confirmation): all measurements, photos, operators, versions. | Sonnet | P2-11, P3-13 | §18.7, §9 | app/pdf/build_record.py | Completed unit → one PDF containing its full §9 record; immutable artifact. |
| P4-09 | Perf/load check: seeded data at 25-station concurrency; scan→render < 1 s LAN, dashboard < 2 s; pg indexes verified with EXPLAIN on hot queries. | Haiku (script) | P4-02 | N2 | tests/perf/load_check.py | Numbers recorded in gate report; misses become remediation tasks. |
| P4-10 | Security review: secrets handling, least-privilege JB2 key, TLS, role enforcement matrix, no public exposure, unattended-upgrades; fix list. | **Fable** | P3-03, P1-11 | N4, §14 | docs/gates/PHASE_4_GATE.md §security | Written review; all criticals fixed pre-gate. |

**GATE 4** → then closeout (§6).

---

## 4. Test plan per phase

Fixtures: all integration tests run against **fake-JB2** (P1-04) replaying Phase 0 recordings — zero network in CI (DD N6). Framework: pytest; property tests via hypothesis (already-installed-deps rule: hypothesis is the one new dev-dep, noted in CR-005).

| Phase | File | Asserts |
|---|---|---|
| 0 | `tests/test_fixtures_valid.py` | Every recorded fixture parses, is secret-free, and covers the endpoint list in the findings doc. |
| 1 | `tests/unit/test_jb2_client.py` | Filter guard (unfiltered GET impossible), `fields=` always present, retry/backoff schedule, circuit breaker open/half-open/close, throttle ceiling respected. |
| 1 | `tests/integration/test_sync.py` | New fixture order → mirror ≤ 90 s (accelerated clock); checkpoint overlap catches boundary-timestamp records; hash diff no-ops unchanged rows; `sync_runs` rows written; worker restart mid-window loses nothing. |
| 1 | `tests/integration/test_outbox.py` | Same-transaction enqueue (rollback removes both); per-WO FIFO; idempotent replay (drain twice → one POST at fake-JB2); permanent 4xx parks + surfaces; JB2-down accumulates then drains. |
| 1 | `tests/integration/test_health.py` | Health endpoints truthful in healthy / breaker-open / backlog states. |
| 2 | `tests/unit/test_library_versioning.py` | Lifecycle transitions legal-only; edit-published forks draft; publish bumps + rebinding future-only; global-vs-product override resolution. |
| 2 | `tests/unit/test_binding.py` | Four-rung resolution incl. fuzzy-flag and unbound placeholder. |
| 2 | `tests/unit/test_conditions.py` | Variant condition evaluation; malformed condition rejected at author time. |
| 2 | `tests/integration/test_order_to_plan.py` | Fixture Apollo order → work order + units + frozen plan; freeze immutability (publish v+1, byte-hash of frozen_content unchanged); `blocked_no_instructions` path; qty/cancel/drift reconciliation (§17.4/5) scenarios. |
| 2 | `tests/integration/test_pdf.py` | Guide PDF generated, versioned never overwritten, contains cover QR + measurement blanks; badge/box sheets round-trip payloads. |
| 3 | `tests/statemachine/test_transitions.py` | **Property tests:** from the P3-01 contract table, no illegal transition reachable via API; scan matrix (right/wrong/unknown/unbound/override); double-finish + duplicate-scan idempotent; out-of-tol requires disposition; scrap requires lead; rework reopens K..N and flips first_pass permanently. |
| 3 | `tests/integration/test_floor_e2e.py` | Scripted 3-station walk: sessions/pauses sum correctly; per-substep timestamps complete; fake-JB2 receives exactly the expected time-ticket header+details and routing patches with deterministic idempotency keys; auto-closed session withheld until lead confirm. |
| 3 | `tests/unit/test_auth.py` | Station+operator dual requirement; role matrix; second-badge privilege; idle expiry; one-station rule. |
| 3 | `tests/integration/test_offline.py` | JB2 down: floor actions all succeed, outbox accumulates, recovery drains (R3/R4, N1). |
| 4 | `tests/integration/test_dashboard.py` | Pipeline API exposes all 7 card states + KPI strip numbers consistent with DB; WS event → payload < 2 s; TV token read-only. |
| 4 | `tests/unit/test_metrics_views.py` | Each SQL view vs hand-computed values on the Gate-3 dataset (FPY, Pareto, actual-vs-est, queue time, rework %). |
| 4 | `tests/integration/test_backfill.py` | Backfilled data flagged, metrics-included, JB2-posted identically. |
| 4 | `tests/perf/load_check.py` | N2 numbers under 25-station simulated load. |

---

## 5. Phase-gate protocol (mandatory)

No phase starts until the prior gate is **PASS**. Gates are executed by **Fable** — never by the agents who wrote the code.

1. Run the full test suite (all phases so far). All green or FAIL.
2. Walk the DD §19 acceptance criteria for the phase one by one, **executing the system** where practical:
   - **Gate 0:** every `[VERIFY-JB2]` resolved in `docs/jb2-api-findings.md`; fixtures replay in CI.
   - **Gate 1:** inject a fixture order into fake-JB2 → appears in mirror ≤ 90 s; kill/restart sync mid-run and verify no loss; outage drill (breaker + recovery).
   - **Gate 2:** author an Apollo instruction set → fixture Apollo order auto-produces work order + frozen plan + PDF with correct binding; freeze-immutability check.
   - **Gate 3:** walk one unit through 3 stations end to end (scans, substeps, a measurement, one failure→rework, finish ops) and **diff the fake-JB2 received writes against the expected §4.4 set**; verify §9.1–9.4 queryability via the unit timeline. Live-tenant smoke of one time-ticket if creds allow.
   - **Gate 4:** N1–N5 checklist with evidence (perf numbers, restore-drill output, alert simulations, security review); pilot readiness statement.
3. Write `docs/gates/PHASE_N_GATE.md`: criteria checklist w/ evidence, test results, defects, explicit **PASS/FAIL**.
4. On FAIL: Fable creates remediation tasks (ID `PN-Rx`, tiered per §2) appended to this plan; gate re-runs from step 1.

---

## 6. Closeout artifacts (after Gate 4 PASS)

| ID | Description | Tier | AC |
|---|---|---|---|
| C-01 | `docs/dataflow.html` — single self-contained page (no external deps), **generated from the as-built code** (`tools/gen_dataflow.py` walks imports/route tables/worker registrations), visualizing: JB2 API ↔ `app/sync` ↔ mirror tables ↔ domain services ↔ `app/outbox` ↔ JB2 write-backs; station UI ↔ MES API ↔ Postgres; `app/pdf`; `app/ws` ↔ dashboard. Colors: JB2 `#1f6f8b`, MES `#c0392b`, write-backs `#f2a541`. Every arrow names the implementing module/file. | Sonnet builds, **Fable verifies every arrow against code** | Zero arrows without a real code path; page opens offline. |
| C-02 | Finalize `docs/CHANGE_REQUESTS.md`: every implementation deviation logged CR-00N with gate impact; future changes gated through this file. | Sonnet | No deviation discoverable in gate reports that lacks a CR. |
| C-03 | Runbooks: operator quick start, restore drill, JB2 outage, station enrollment. | Haiku | Each runbook executed once as written during Gate 4. |

`docs/CHANGE_REQUESTS.md` and `docs/ESCALATIONS.md` are initialized **now** (seeded with plan-time CRs) — see those files.

---

## 7. Risk register (top 10)

| # | Risk | Likelihood/Impact | Mitigation → tasks |
|---|---|---|---|
| R1 | ~~`OrderRoutingUpdate` PATCH fields not writable~~ **REALIZED 2026-07-14 (CR-010):** spec proves actuals/status absent from the PATCH schema. Fallback active: time tickets are the sole C7/C8 write-back (P3-10). Residual risk: how JB2 UI/costing surfaces operation completion from tickets alone — verify in P0-04. | — | CR-010; P0-04 verifies JB2-side visibility of ticket-only completion. |
| R2 | Undocumented rate limits → sync throttled or key suspended. | M/H | P0-07 polite probe sets client ceiling; P1-03 global throttle + breaker; cadences per DD §4.2 are modest. |
| R3 | `lastModDate` granularity/timezone quirks → silently missed orders. | M/H | P0-06 measures boundary behavior; P1-05 overlap-window checkpoints + `content_hash` full-pull fallback; Gate 1 restart drill. |
| R4 | Token TTL/refresh surprises or APIs not activated for tenant → integration dead in water. | M/H | P0-02 first task after scaffold; activation matrix in findings; escalate to ECI early (human dependency, §8). |
| R5 | Time-ticket semantics (`timeStart/End` vs `setupTime/cycleTime`, header/detail linkage) mismodeled → JB2 costing corrupted. | M/H | P0-04 round trip on dummy job with JB2-UI verification; P3-10 Fable payload review; `auto_closed` sessions withheld (§17.8) keep garbage time out. |
| R6 | Kiosk hardware reality (wedge scanner focus quirks, tablet camera, glove touch) breaks the scan-first UX. **Hardware bypassed for now (CR-009)** — risk shifts to "manual-entry fallback becomes the habit", violating PDD P2. | M/M | P3-05 ships scan input + manual fallback both; emulated Android-viewport checks in CI; real-hardware validation becomes a pre-pilot task when Will supplies a tablet/scanner; PDF paper fallback (P2-11) + backfill (P4-06) as designed degradation. |
| R7 | Single-server loss (disk/box) → data loss beyond RPO. | L/H | P4-07 nightly dump + WAL + offsite restic + quarterly drill; Gate 4 executes the drill, not just reads the runbook. |
| R8 | Instruction library authoring lags orders → floor blocked at `blocked_no_instructions`. | M/M | Designed as a to-do not an error (P2-10/P2-13 dashboard flags); clone tooling (P2-08) cuts authoring cost; pilot on one product line only (DD §19). |
| R9 | HTMX ceiling on builder UX (drag reorder, image annotation). | M/M | X3 escape hatch: isolated JS island for that screen only, via CR; annotation scoped minimal (arrows/circles) in P2-06. |
| R10 | Legacy-code gravity: agents "reuse" superseded WIP/quality logic that contradicts the unit-level model. | M/M | Legacy quarantined in `legacy/` (P0-01) with a README banning imports; CI grep forbids `from legacy` (P1-12); §1 verdicts are explicit. |

---

## 8. Definition of done (project) — traceable to PDD §7

The project is done when, after one product line runs a full quarter post-Gate 4:

| DoD | PDD §7 | Verified by |
|---|---|---|
| D1. 100 % of labor/quantity time tickets on the pilot line originate from MES write-backs; zero manual JB2 entry. | 7.1 | Outbox `confirmed` count vs JB2 ticket audit (query in metrics page, P4-04). |
| D2. Any unit's location answerable in one lookup; every completed unit has a full build record (instruction versions, measurements, operators, times). | 7.2 | Unit timeline (P4-03) + build-record PDF (P4-08) spot audits. |
| D3. First-pass yield is a number with a failure-cause Pareto, not a feeling. | 7.3 | Metrics views (P4-04) populated from real pilot data. |
| D4. Operators choose the tablet over the paper guide when both are offered. | 7.4 | Pilot observation + backfill-screen usage ≈ 0 outside outages. |
| D5. Instruction estimates visibly converge toward actuals release over release. | 7.5 | Actual-vs-estimate view trend (P4-04). |
| D6. All four gate reports PASS; closeout artifacts C-01..C-03 delivered; every deviation CR-logged. | — | docs/gates/*, docs/dataflow.html, docs/CHANGE_REQUESTS.md. |

---

## 9. Waiting on the human — status 2026-07-14

Resolved by Will:
- ~~JB2 credentials~~ → repo-root `.env` (`JobBoss2__*` keys). Still needed: confirmation that ECI has **activated** all required APIs for these creds — Phase 0 P0-02 discovers this empirically and reports.
- ~~Target server~~ → Will's Ubuntu server, Caddy-fronted (P1-11 as planned).
- ~~Git~~ → linked: `origin = https://github.com/willx433/projectmes.git` (remote was empty; init + remote add done 2026-07-14; baseline commit/push in P0-01).
- ~~18.1 serial timing~~ → serials pre-exist in Atlas's system; MES records them at kit-up, never generates (P3-11, CR-007).
- ~~Hardware~~ → bypassed for now (CR-009); manual-entry fallbacks + emulated Android-tablet testing; real-hardware validation deferred to pre-pilot.
- ~~Backup target~~ → deferred (CR-008); local nightly `pg_dump` stopgap only.

Still open (none block Phase 0 start):
1. **A dummy/test job in the JB2 tenant** Phase 0 can write time tickets against (P0-04/05) — no API void path exists; cleanup is manual in the JB2 dashboard. Name the job number to use.
2. Remaining DD §18 defaults — confirm or override: 18.2 operator metrics **admin-only**; 18.3 publish **single-approver**; 18.5 JB2 releases/lots in use? (if yes → CR + mirror task); 18.7 build-record PDF **yes**; 18.6 thresholds set with floor leads at pilot.
3. **Alert email destination** (P4-05).
4. Pre-pilot (not now): one Android tablet + USB wedge scanner for real-hardware validation (R6); offsite backup target to un-defer P4-07.
