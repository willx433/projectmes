# Atlas MES — Design Document

**System:** In‑house Manufacturing Execution System (MES) integrated with JobBOSS² Cloud ERP
**Deployment target:** On‑prem Linux server, Caddy reverse proxy
**Stack:** Python 3.12 / FastAPI · PostgreSQL 16 · HTMX or React station UI · WeasyPrint (PDF)
**Audience:** This document is the source of truth for a coding agent to produce an implementation plan. Every requirement is numbered for traceability.
**Status:** Draft v1.1 — JB2 API surface verified against the live OpenAPI spec at `https://api-jb2.integrations.ecimanufacturing.com/openapi.json` (retrieved 2026‑07‑10). Remaining items marked `[VERIFY-JB2]` are narrow field‑level checks for Phase 0 (§4.7).

---

## Table of Contents

1. Executive Summary
2. Goals and Non‑Goals
3. System Context and Responsibility Split (JobBOSS² vs. MES)
4. JobBOSS² Integration Layer
5. Core Domain Model
6. Workflows (End‑to‑End)
7. Instruction Library Builder
8. Execution Plans and PDF Guide Generation
9. Data Capture Catalog (everything we record)
10. Database Schema
11. MES API Design
12. Station UI
13. Dashboard
14. Identity, Auth, and Roles
15. Non‑Functional Requirements
16. Deployment and Operations
17. Edge Cases and Failure Modes
18. Open Questions / Decisions Needed
19. Phased Implementation Roadmap
20. Glossary

---

## 1. Executive Summary

Atlas Gun Works runs order entry, estimating, routing definitions, purchasing, and invoicing in JobBOSS² (JB2) cloud ERP. JB2 is a good system of record but a weak shop‑floor execution tool: it cannot present rich step‑by‑step work instructions, capture per‑substep completion and dimensional data, generate per‑order PDF build guides, or track a physical build box through stations at the granularity we need.

The Atlas MES fills that gap. It:

- **Mirrors orders and routings from JB2.** When an order is created in JB2 (e.g., for an *Apollo*), the MES detects it, pulls its routing, and instantiates a tracked **work order** with an **execution plan** built from the instruction library for that product.
- **Tracks physical build boxes via QR codes** that match the routing QR codes already in use. A scan at a station tells the MES (and, where applicable, JB2) exactly where the box is and starts/stops labor.
- **Presents operators exact step‑by‑step instructions** for the operation at their station, with per‑substep completion buttons, dimension entry fields with tolerances, photo capture, and failure/scrap/rework actions.
- **Writes back to JB2** what JB2 natively understands: job clock‑on/clock‑off, quantities complete/scrapped at each routing step, and operation status — so JB2 scheduling, job costing, and invoicing keep working unchanged.
- **Records everything else itself** in PostgreSQL: substep timestamps, measured dimensions, material consumption deltas, failure causes, rework loops, first‑pass yield, operator identity, station dwell times, and a full audit trail.
- **Shows a live dashboard** of every unit in the pipeline, highlighting first‑pass work vs. rework, station load, and stalled boxes.

The governing principle for the JB2/MES split: **JB2 owns money and orders; the MES owns execution and evidence.** Anything JB2 can already record through its API, we push to JB2 and treat JB2 as authoritative. Everything finer‑grained lives only in the MES.

---

## 2. Goals and Non‑Goals

### 2.1 Goals

- **G1.** Zero double entry: orders, part numbers, routings, employees, and work centers originate in JB2 and flow into the MES automatically.
- **G2.** Every physical alteration/process a product undergoes has a written, versioned, step‑by‑step instruction that is presented on screen and available as a PDF guide.
- **G3.** Every unit (build box) is locatable in real time by scanning its routing QR code; location history is permanent.
- **G4.** Every substep completion, measurement, failure, scrap event, rework loop, and time interval is recorded with operator identity and timestamps.
- **G5.** JB2 remains correct without manual entry: labor time, quantities, and operation completion post back through the JB2 API where the API supports it.
- **G6.** A dashboard shows all WIP, distinguishing first‑pass units from rework, with drill‑down to the individual step.
- **G7.** The instruction library is maintainable by non‑developers (library builder UI), grouped by product name, versioned, and safe to edit while work is in flight.
- **G8.** The system runs entirely on one on‑prem Linux box behind Caddy; the only external dependency is the JB2 cloud API.

### 2.2 Non‑Goals (explicitly out of scope for v1)

- **NG1.** Replacing JB2 scheduling, estimating, quoting, purchasing, shipping, or accounting.
- **NG2.** Machine/CNC integration (MTConnect, probe data auto‑ingest). Designed for later; not in v1.
- **NG3.** Full SPC engine (control charts, Cpk). We capture the data so SPC can be layered on later.
- **NG4.** ATF A&D bound‑book compliance software. The MES records serial numbers and dispositions as data, but the licensed bound book process remains whatever Atlas uses today.
- **NG5.** Customer‑facing portals or notifications.
- **NG6.** Multi‑site support.

---

## 3. System Context and Responsibility Split

### 3.1 Context diagram (textual)

```
                    ┌────────────────────────────┐
                    │      JobBOSS² Cloud        │
                    │  (orders, routings, cost,  │
                    │   scheduling, invoicing)   │
                    └──────────▲─────────────────┘
                               │ REST API (poll + write-back)
                               │ HTTPS, API keys
┌──────────────────────────────┴───────────────────────────────┐
│                      Atlas MES (on-prem Linux)               │
│                                                              │
│  Caddy ──► FastAPI app ──► PostgreSQL                        │
│              │  ▲                                            │
│              │  └── Sync worker (JB2 poller / write-back)    │
│              ├── PDF service (WeasyPrint)                    │
│              └── WebSocket/SSE hub (dashboard live updates)  │
└──────▲──────────────▲──────────────▲────────────────▲────────┘
       │              │              │                │
  Station tablet  Station tablet  Admin/Library   Dashboard TV /
  + USB scanner   + USB scanner   Builder (office) office browsers
```

### 3.2 Capability matrix — what JB2 does vs. what the MES does

This is the heart of the requested "determine what JB2 can do and what we handle internally." Each row states the owner and how the systems reconcile. Rows marked `[VERIFY-JB2]` depend on API surface that Phase 0 must confirm.

| # | Capability | Owner | Notes / reconciliation |
|---|-----------|-------|------------------------|
| C1 | Customer orders, line items, due dates, pricing | **JB2** | MES mirrors read‑only. |
| C2 | Part numbers / product definitions | **JB2** | MES maps JB2 part numbers → MES products (for instruction grouping, e.g. "Apollo"). |
| C3 | Routing definition (sequence of operations, work centers, estimated times) | **JB2** | Authored in JB2; MES imports per order. MES never edits JB2 routings. |
| C4 | Work centers / stations list | **JB2** master, MES extension | MES adds station metadata JB2 lacks: physical device binding, scanner ID, instruction context. |
| C5 | Employee master | **JB2** master | MES mirrors (**verified:** `GET /api/v1/employees`) and adds badge QR / PIN. |
| C6 | Job clock‑on / clock‑off (labor tickets per operation) | **JB2 via MES** | **Verified:** `POST /api/v1/time-tickets` + `POST /api/v1/time-ticket-details` (fields incl. `timeStart`, `timeEnd`, `setupTime`, `cycleTime`, `operationNumber`, `stepNumber`, `workCenter`). MES posts a detail per work session so JB2 job costing stays accurate. |
| C7 | Quantity complete / scrapped per routing operation | **JB2 via MES** | **Verified:** `piecesFinished` / `piecesScrapped` on time‑ticket details; `actualPiecesGood` / `actualPiecesScrap` + `actualStartDate/EndDate` + `status` on `PATCH …/order-routings/{stepNumber}`. |
| C8 | Operation status (current operation of a job) | **JB2 via MES** | **Verified:** routing step `status` patchable; `OrderLineItem.currentWorkCenter` reflects position. MES posts transitions; MES stays authoritative for physical box location. |
| C9 | Scheduling / whiteboard, promised dates | **JB2** | MES displays JB2 due dates; does not schedule. |
| C10 | Job costing, invoicing, shipping docs | **JB2** | Fed by C6/C7 write‑backs. |
| C11 | Step‑by‑step work instructions, per substep | **MES only** | JB2 has no concept below the routing operation. |
| C12 | Instruction library, versioning, media, grouping by product | **MES only** | |
| C13 | Per‑order execution plan + PDF build guide | **MES only** | |
| C14 | Physical box (traveler) tracking via QR scan; location history | **MES only** | JB2 sees only operation-level status via C8. |
| C15 | Dimensional / measurement capture with tolerances | **MES only** | JB2 quality module is not granular enough and adds license cost. |
| C16 | Failure, scrap‑cause, rework‑loop tracking at substep level | **MES** (summary scrap qty → JB2 via C7) | |
| C17 | Material consumption *deltas* (actual vs. planned at a step) | **MES only** | **Verified:** `job-materials` is **GET‑only** in the API — actual consumption cannot be posted to JB2. MES is sole record; planned qty read from `job-materials`. |
| C18 | First‑pass‑yield analytics, station dwell, WIP dashboard | **MES only** | |
| C19 | Operator badge auth at stations | **MES only** | |
| C20 | Serial number capture per unit | **MES only** | **Verified:** no serial‑number resources in the JB2 API. |
| C21 | Attachments/prints on jobs (drawings) | **JB2** stores; MES reads | **Verified:** `document-controls`, `document-histories`, `document-review` are GET‑only — MES can list/link controlled documents but uploads its own media to local storage. |
| C22 | Quality records (non‑conformances, CAPA) | **MES** captures; JB2 read‑only | **Verified:** `non-conformances` and `corrective-preventive-actions` are **GET‑only** via API. MES quality capture cannot write JB2 quality module; if Atlas uses JB2 quality, entries there remain manual. |
| C23 | Payroll attendance (shift clock in/out) | **JB2** (optional MES assist) | **Verified:** `POST /api/v1/attendance-tickets` exists. Out of v1 scope; badge events could feed it later (§19 Phase 5). |
| C24 | Schedule visibility | **JB2** | **Verified:** `GET /api/v1/eci-aps/get-schedule` and ShopView endpoints (`get-jobs`, KPI feeds) available for display on the MES dashboard. |

### 3.3 Decision rules

- **R1.** If JB2's API can record a fact and JB2 consumes that fact downstream (costing, scheduling, invoicing), the MES writes it to JB2 and treats JB2 as authoritative for it.
- **R2.** If JB2 cannot represent a fact at the needed granularity, the MES is the sole system of record for it. Never store a "shadow copy" in JB2 via misused fields (no stuffing data into job notes).
- **R3.** All JB2 write‑backs are **idempotent and queued** (§4.5); a JB2/cloud outage must never stop the shop floor.
- **R4.** The MES never blocks an operator on a JB2 API round trip. Writes are async; reads are served from the local mirror.

---

## 4. JobBOSS² Integration Layer

### 4.1 Access and authentication (verified against live spec, 2026‑07‑10)

- **Docs portal:** `https://integrations.ecimanufacturing.com/` → JobBOSS² API (OAS 3.0). **Spec:** `https://api-jb2.integrations.ecimanufacturing.com/openapi.json`. **Base URL:** `https://api-jb2.integrations.ecimanufacturing.com/api/v1/`.
- **Auth:** OAuth 2.0 — access token obtained from the ECI **Management API token endpoint**, sent as `Authorization: Bearer <token>` on every call. A `POST /api/v1/oauth2Exchange` endpoint also exists. APIs must be **activated by ECI** for the tenant (Atlas already holds keys; confirm activation covers all needed resources). `[VERIFY-JB2]` token TTL and refresh cadence.
- **Conventions (verified):** all datetimes UTC (`yyyy-MM-ddTHH:mm:ssZ` or `yyyy-MM-dd`); default GETs return a *subset* of fields — request explicit `fields=` lists; filtering via `field[op]=value` with `eq, ne, gt, gte, lt, lte, in, notin, null`; paging via `take`/`skip` (default `take=200`); sorting via `sort=±field`; errors return `{Title, Status, Detail, TraceId}`.
- **Incremental sync (verified):** JB2 resources expose `lastModDate` — poll with `lastModDate[gte]=<checkpoint>`. (Note from docs: `revisedDate` cannot be null‑tested — causes a 500.)
- Keys/tokens are stored in the MES only as environment secrets (systemd credentials or `.env` with `0600`, never in the DB or repo).
- One integration module (`app/jb2/client.py`) wraps every call: retries with exponential backoff + jitter, timeout budget (10 s), structured logging of request id / status / latency, and a circuit breaker that flips the MES into **offline mode** (§17.1) after N consecutive failures.

### 4.2 Entities read from JB2 (mirror tables)

The sync worker polls JB2 and maintains local mirror tables (`jb2_*`). Endpoints verified in the live spec:

| MES mirror | JB2 endpoint (verified) | Poll cadence | Purpose / key fields |
|---|---|---|---|
| `jb2_orders` | `GET /orders` (`lastModDate[gte]=`) | 60 s | Detect new/changed/canceled orders. |
| `jb2_order_line_items` | `GET /order-line-items` | 60 s | `jobNumber`, `partNumber`, `quantityOrdered/ToMake`, `dueDate`, `status`, `priority`, `currentWorkCenter`, `revision`, plus `user_*` custom fields. |
| `jb2_order_routings` | `GET /order-routings` (filter by job) | On new/changed order | `stepNumber`, `operationCode`, `workCenter`(via op), `description`, `setupTime`, `cycleTime`, est. hours, `status`, `actualPiecesGood/Scrap`. |
| `jb2_order_materials` | `GET /job-materials` + `GET /job-requirements` | On new/changed order | Planned material per step (`partNumber`, `stepNumber`, qty, lot/bin fields). Read‑only. |
| `jb2_parts` | `GET /estimates` (+ `/materials` sub‑resource) | 15 min | Part master → product mapping, revisions. |
| `jb2_work_centers` | `GET /work-centers` | 15 min | Station mapping. |
| `jb2_operation_codes` | `GET /operation-codes` | 15 min | Instruction‑set binding keys. |
| `jb2_employees` | `GET /employees` | 15 min | Operator mirror for badge issuance. |
| `jb2_documents` | `GET /document-controls`, `/document-histories` | On order import | Controlled prints/drawings linked into execution plans (read‑only). |
| (display only) | `GET /eci-aps/get-schedule`, `GET /shopview/get-jobs` | 5 min | JB2 schedule/KPI overlay on dashboard. |

The API has **no webhooks** — polling with `lastModDate` checkpoints is the mechanism.

**Sync mechanics:**

- Each mirror row stores the raw JB2 JSON (`jsonb payload`), extracted indexed columns, `jb2_last_modified`, `synced_at`, and a `content_hash` for change detection.
- Incremental sync by `last modified` filter where the API supports it; otherwise windowed full pulls with hash diffing.
- A `sync_runs` table records every run: resource, window, records fetched/changed, duration, errors — surfaced on an admin health page.

### 4.3 Order ingestion rules

1. New JB2 order line appears → if its part number maps to an MES **product** with a published instruction set, auto‑create an MES **work order** per line item (status `ready`), instantiate the route (§6.2), generate execution plan + PDF, and emit "new work" to the dashboard.
2. If the part number has no product mapping or the product has no published instruction set, create the work order in status `blocked_no_instructions` and flag it on the dashboard — this is a to‑do for the library builder, not an error.
3. Order changed in JB2 (qty, due date) → update mirror; propagate due date to work order; qty changes follow §17.4.
4. Order canceled/closed in JB2 → work order → `cancelled` (if not started) or flagged `cancel_requested` for a lead to disposition (if WIP exists).

### 4.4 Write‑backs to JB2

Verified write surface: `POST /time-tickets` (header: employee + date), `POST /time-ticket-details` (required: `employeeCode`, `jobNumber`, `ticketDate`; plus `stepNumber`, `operationNumber`, `workCenter`, `timeStart`, `timeEnd`, `setupTime`, `cycleTime`, `piecesFinished`, `piecesScrapped`, `reasonNumber`, `shift`, `comments`), `PATCH /time-ticket-details/{timeTicketGUID}`, `PATCH /orders/{orderNumber}/order-line-items/{itemNumber}/order-routings/{stepNumber}`.

| Trigger in MES | JB2 write (verified endpoint) | Notes |
|---|---|---|
| Work session closes (operator finishes or clocks out mid‑op) | `POST /time-ticket-details` with `timeStart`/`timeEnd` (or `setupTime`/`cycleTime`), `jobNumber`, `stepNumber`, `employeeCode`, `workCenter` | One detail per session. Ensure the day's time‑ticket header exists (`POST /time-tickets`, idempotent check first). JB2 costing accrues from these. |
| Operation finished for a unit | Same detail carries `piecesFinished`; additionally `PATCH …/order-routings/{stepNumber}` with `actualStartDate`/`actualEndDate`, `actualPiecesGood`, `status` | `[VERIFY-JB2]` exact writable field set of `OrderRoutingUpdate` — test in Phase 0; if `status` is derived by JB2, rely on time tickets alone. |
| Unit scrapped | `piecesScrapped` (+ `reasonNumber` from JB2 reason codes, mirrored via `GET /reason-codes`) on the closing detail; `actualPiecesScrap` on routing patch | Map MES failure codes → JB2 reason codes in `failure_codes.jb2_reason_number`. |
| Rework loop back to earlier operation | Additional time‑ticket details against the earlier step | JB2 has no first‑pass/rework flag — that distinction lives in MES only. |
| Material actuals | **Not possible** — `job-materials` is GET‑only | MES is sole record (C17). |
| Live "clocked on" state in JB2 | Not real‑time; JB2 learns at session close | Acceptable: MES dashboard is the live view (R4). Optional Phase 5: post open‑ended detail at start, PATCH at close — test whether JB2 tolerates it. |

### 4.5 Outbox pattern (reliability)

Every JB2 write is inserted into `jb2_outbox` (id, kind, payload, idempotency_key, status `pending|sent|confirmed|failed`, attempts, last_error, created_at) in the **same DB transaction** as the local event. A worker drains the queue in order per work‑order, retries with backoff, and never blocks the UI. Failed items after max retries surface on the admin health page for manual replay. Idempotency keys are deterministic (`wo:{id}:op:{seq}:start`) so replays cannot double‑post labor. If JB2 rejects a write with a permanent validation error, it is parked (`failed`) and the local record stands — MES data is never deleted to match JB2.

### 4.6 Mapping tables

- `product_part_map`: JB2 part number(s) → MES product. Many‑to‑one (part number aliases, e.g. `APOLLO-9-BLK` and `APOLLO-9-FDE` → product "Apollo") with optional variant attributes extracted (caliber, finish) that instructions can condition on.
- `station_work_center_map`: JB2 work center → MES station(s). One work center may correspond to several physical benches.
- `employee_map`: JB2 employee id → MES operator (badge).

Unmapped values never crash a sync; they land in `mapping_exceptions` and appear as admin to‑dos.

### 4.7 Phase 0 — API verification (first coding‑agent task)

Endpoint inventory is already verified from the live spec (2026‑07‑10). Phase 0 is now a **behavioral** test with Atlas's real keys against the tenant (use a dummy job), producing `docs/jb2-api-findings.md` + the saved `openapi.json`:

1. Token acquisition from the Management API: TTL, refresh, and which APIs ECI has activated for our credentials.
2. `POST /time-tickets` + `POST /time-ticket-details` round trip: required header/detail relationship, how entries appear in JB2 UI/costing, whether `timeStart/timeEnd` vs. `setupTime/cycleTime` are alternates or complements.
3. `PATCH …/order-routings/{stepNumber}`: which `OrderRoutingUpdate` fields are writable (`actualPiecesGood`, `actualPiecesScrap`, `actualStartDate/EndDate`, `status`) and JB2 UI effects.
4. Rate limits (not stated in docs) — probe polite ceilings; set client throttle accordingly.
5. Confirm `lastModDate` filter behavior on `orders`, `order-line-items`, `order-routings` (granularity, timezone).
6. Whether `OrderLineItemUpdate` allows writing any `user_*` field (candidate: store MES work‑order URL for cross‑navigation).
7. Reason‑code list (`GET /reason-codes`) → seed `failure_codes.jb2_reason_number` mapping.

Every remaining `[VERIFY-JB2]` tag must be resolved by this report before Phase 2 begins.

---

## 5. Core Domain Model

Entity overview (details in §10 schema):

- **Product** — a sellable/buildable thing grouped by name ("Apollo", "Athena", "Nyx"). Groups instruction sets; maps to ≥1 JB2 part numbers. Optional variant axes (caliber, finish, grip size).
- **InstructionSet (ProcessTemplate)** — the library entry for *one operation type on one product* (e.g., "Apollo — Slide Lightening Cuts"), versioned. Composed of ordered **Steps**; each step has ordered **Substeps**.
- **Substep** — the atomic unit an operator checks off. Types: `action` (do X), `measurement` (record dimension w/ nominal + tolerances + unit + gauge), `inspection` (pass/fail + notes), `photo` (capture required), `material` (record material used/changed), `signoff` (lead/QC approval required).
- **RouteTemplate (implicit)** — the MES does *not* author routes; the route comes from the JB2 order routing. The MES binds each JB2 routing step to an InstructionSet via (product, operation/work‑center match) rules.
- **WorkOrder** — MES execution record for one JB2 order line item. Holds N **Units** (qty > 1) or 1 unit.
- **Unit** — one physical serialized item. First‑pass/rework status, serial number, current step.
- **BuildBox (Traveler)** — physical container with the routing QR code. Usually 1 box = 1 unit for pistols; the model allows 1 box = N units (small parts batches). Box ↔ unit assignment is explicit and re‑assignable (box recycling).
- **ExecutionPlan** — the frozen, versioned expansion of (work order × routing × instruction set versions) generated at release. In‑flight work always executes against its frozen plan, never against a live library edit.
- **StepExecution / SubstepExecution** — runtime records: who, when, station, result, measurements, failures.
- **Station** — physical location with a tablet + scanner; maps to a JB2 work center.
- **Operator** — person; mirrors JB2 employee; has badge QR + PIN.
- **WorkSession** — operator × station × unit × operation interval (clock‑on → clock‑out). Source for JB2 time tickets.
- **Event** — append‑only audit stream of everything (§9.10).

State machines:

```
WorkOrder: pending_sync → ready → in_progress → completed
                      ↘ blocked_no_instructions          ↘ cancelled / cancel_requested

Unit:      queued → at_station(op N) → in_transit → ... → done
             any-state → scrapped (terminal, requires reason + lead approval)
             at_station → rework(op K ≤ N)  [rework_count++, first_pass=false]

SubstepExecution: pending → in_progress → done | failed | skipped(reason, authorized_by)
```

---

## 6. Workflows (End‑to‑End)

### 6.1 Order arrives

1. Sync worker sees new JB2 order line (e.g., 1× Apollo, due 2026‑08‑15).
2. MES resolves part number → product "Apollo"; pulls the order's routing from JB2 (e.g., Op10 CNC Slide, Op20 Frame Fit, Op30 Barrel Fit, Op40 Assembly, Op50 Cerakote, Op60 Final QC, Op70 Test Fire).
3. For each routing step, the binding rules resolve the current **published** InstructionSet version for (Apollo, that operation).
4. MES creates WorkOrder + Unit(s) + frozen ExecutionPlan; generates the PDF build guide (§8); status `ready`; dashboard shows it in "Awaiting Start".
5. A build box is assigned: at kit‑up, a lead scans an unassigned box QR (or prints a new one) and scans/selects the work order → box bound to unit.

### 6.2 Route population

- The MES route **is** the JB2 order routing — same sequence numbers, same operation descriptions — so JB2 and MES never disagree about what the steps are.
- Binding rule resolution order: (product, exact JB2 operation code) → (product, work‑center) → (product, fuzzy op description match, flagged for confirmation) → unbound ⇒ `blocked_no_instructions` for that step (plan generates with a placeholder "no instructions — see lead" step; work order flagged).

### 6.3 Box arrives at a station — the scan

1. Operator is badge‑authenticated at the station (§14). Scans the box QR (USB wedge scanner ⇒ keyboard input into an always‑focused capture field).
2. MES resolves box → unit → work order → expected next operation.
3. **Validation:** Is this the right station for the unit's next operation? If yes → proceed. If no → big warning ("This box's next step is Op30 Barrel Fit at Station 4"), with lead‑override to proceed out of sequence (recorded).
4. On accept: `WorkSession` opens (unit, operator, station, operation, t_start); location updated; event logged; dashboard updates live. (JB2 learns of the labor when the session closes — §4.4.)
5. Screen shows the operation's instruction steps.

### 6.4 Step execution

- Steps presented in order; a step expands into substeps. Operator cannot complete a step until all required substeps are done (or lead‑authorized skip with reason).
- Substep types behave as defined in §5: action ⇒ "Done" button; measurement ⇒ numeric field with nominal/tolerance, instant in/out‑of‑tolerance coloring, out‑of‑tolerance requires a disposition (accept‑w/ deviation [lead], rework, scrap); inspection ⇒ pass/fail; photo ⇒ tablet camera or upload; material ⇒ record item + qty delta; signoff ⇒ second badge scan by authorized role.
- Each substep records: timestamps (started/completed), operator, values, notes, attachments.
- Timer runs per session; pause button for interruptions (reason coded: waiting‑material, machine‑down, break, pulled‑to‑other‑job).

### 6.5 Failure / scrap / rework paths

- **Substep failed:** operator taps "Fail" → picks failure code (library‑managed taxonomy per product/op) + notes + photo → chooses disposition:
  - **Rework here:** substep resets, rework counter increments, time keeps accruing (marked rework time).
  - **Send back to Op K:** unit re‑enters route at K; all intermediate ops re‑open; unit flagged `rework` (first_pass=false permanently); dashboard highlight flips.
  - **Scrap unit:** requires lead badge; captures scrap cause, material value lost (auto‑estimated from `job-materials` cost fields), disposition of the box; JB2 outbox posts scrap qty + reason code. If order qty must be re‑made, MES creates a replacement unit under the same work order flagged `remake`.
- **Complete failure of a piece** is the scrap path with cause taxonomy (material defect, machining error, tooling failure, out‑of‑spec supplier part, handling damage, other).

### 6.6 Operation complete

- All steps done → "Finish Operation" button → summary screen (elapsed, measurements out of tolerance, substep failures) → operator confirms.
- MES: closes WorkSession; JB2 outbox: time‑ticket detail (time + `piecesFinished`/`piecesScrapped`) and routing‑step patch; unit → `in_transit` with destination = next operation's station(s); dashboard updates.

### 6.7 Clock‑out and move

- "Finish" ends the operation; the operator's **clock‑out from the unit** is automatic with it. (Operators may also clock out mid‑operation — session closes, partial time posts, unit stays `at_station` unfinished, resumable by any qualified operator.)
- The box physically moves; nothing else is required until the next station's scan. Transit time = t(next scan) − t(finish), recorded per hop.

### 6.8 Final operation

- Last routing step completion moves unit → `done`; work order completes when all units are done or scrapped‑and‑resolved; JB2 gets final quantities. Serial number must be present before `done` is allowed (configurable per product).

---

## 7. Instruction Library Builder

### 7.1 Concepts

- Library is organized **by product** (Apollo, Athena…), then by **operation** (matching JB2 routing operations), holding **InstructionSets** with semantic versions and states `draft → in_review → published → retired`.
- Only `published` versions bind to new execution plans. In‑flight plans keep their frozen version forever (the plan stores a full copy, not a reference).
- Shared/common instruction sets (e.g., "Cerakote — standard") can be defined once at a **global scope** and attached to many products; product‑specific sets override globals.

### 7.2 Builder UI (office role)

- Tree editor: InstructionSet → Steps → Substeps, drag to reorder.
- Substep editor per type: rich text (limited set: bold, lists, warnings/callout boxes), image upload with annotation (arrows/circles), optional video link, tool/fixture references, and for measurements: name, unit, nominal, +tol, −tol, gauge id, decimal places.
- **Conditional content:** substeps may carry variant conditions (e.g., only when caliber = 9mm) evaluated against the work order's variant attributes at plan generation.
- Estimated minutes per step (feeds dashboard ETA and comparison vs. actual).
- Failure‑code taxonomy editor (per product and global).
- Preview: exactly what the station tablet will render; PDF preview.
- Publish flow: diff vs. current published version; requires a second user with `approver` role (configurable off for v1 if team is small).
- Clone across products ("copy Apollo Op40 assembly → Athena, then edit").

### 7.3 Versioning rules

- Any edit to a published set creates a new draft version; publishing bumps the version and re‑binds **future** plans only.
- An admin action "propagate to in‑flight" exists for safety‑critical corrections: it flags affected active work orders for lead review and regenerates their remaining (not‑yet‑started) steps, recording the version switch in the audit log.

---

## 8. Execution Plans and PDF Guide Generation

- At work‑order release the MES renders one **Build Guide PDF** per work order (optionally per unit when serialized content matters): cover page (order #, product, variant, qty, due date, box QR, serial placeholders), the full routing with one section per operation, every step/substep with images, measurement tables with blank cells (for the paper‑backup case), and footer with plan version hash + generation timestamp.
- Generator: WeasyPrint from the same HTML templates the station UI uses (single source of truth). QR codes rendered via `segno`.
- PDFs are immutable artifacts stored on disk (`/var/lib/mes/artifacts/{wo}/guide-v{n}.pdf`) and linked in the UI; regeneration (plan change, rework insert) produces v+1, never overwrites.
- The PDF is a *guide/backup*; the tablet flow is authoritative for data capture.

---

## 9. Data Capture Catalog

Everything the MES records. ("The more the better" — this section is intentionally exhaustive; the schema in §10 implements it.)

### 9.1 Identity & traceability
- Work order ↔ JB2 order/line ids; product, variant attributes; unit serial number; box id + QR payload; plan version hash; instruction set versions used per operation; drawing/attachment revisions shown to the operator.

### 9.2 Location & movement
- Every scan: box, station, operator, timestamp, accepted/rejected(+reason), override authorizer.
- Current location of every box; full location history; transit time between stations per hop; dwell time at station before work started (queue time) vs. active time.

### 9.3 Time
- Per WorkSession: start/stop, operator, station, operation; pauses with reason codes and durations; setup vs. run split (first session at an op may be marked `setup`); per‑substep started/completed timestamps; estimated vs. actual per step/op/order; rework time separated from first‑pass time; idle-in-queue per station.

### 9.4 Quality & measurements
- Every measurement: name, value, unit, nominal, tolerances, in/out flag, gauge id, operator, timestamp; out‑of‑tolerance dispositions and who authorized; inspection pass/fails; photos with substep linkage; signoffs (who, when, role); rework loops (from‑op, to‑op, cause, count per unit); first‑pass flag per unit and per operation; scrap events (cause code, narrative, photos, material value lost, authorized_by); deviations/lead overrides of any kind.

### 9.5 Material
- Planned material per op (from JB2 order materials); actual material recorded at `material` substeps: item, lot (free text v1), qty used, qty scrapped, delta vs. plan; substitutions (what replaced what, authorized_by); tooling consumed (inserts, bits) if the substep asks.

### 9.6 Failure & rework analytics inputs
- Failure code taxonomy hits by product/op/substep/station/operator; time‑to‑detect (failed at op N, introduced at op K when known); remake linkage (scrapped unit → replacement unit).

### 9.7 People
- Operator on every action; badge auth events (login/logout/station switches); concurrent sessions (two operators on one unit → both sessions recorded); who authored/approved each instruction version.

### 9.8 Stations & devices
- Station heartbeat (tablet online/offline, app version); scanner input errors (unparseable scans); which station executed which op (for A/B comparisons between benches).

### 9.9 Integration health
- Every JB2 API call (endpoint, latency, status); sync runs; outbox item lifecycle; mapping exceptions; drift detections (JB2 changed a synced order after release).

### 9.10 Audit stream
- Append‑only `events` table: every state transition, edit, override, login, publish, PDF generation — actor, timestamp, entity, before/after (jsonb). No deletes anywhere in the system; corrections are compensating events.

### 9.11 Derived metrics (computed, not stored per se)
- First‑pass yield (unit level & operation level), scrap rate by cause, throughput/day by product, average cycle time per op vs. estimate, WIP age distribution, station utilization, queue times, rework hours share, operator‑level cycle stats (visible to admins only — policy decision, see §18).

---

## 10. Database Schema (PostgreSQL 16)

Conventions: `id uuid pk default gen_random_uuid()`, `created_at/updated_at timestamptz`, soft‑delete nowhere (append‑only corrections), all FKs indexed, `jsonb` for raw payloads. Alembic migrations.

```
-- JB2 mirrors
jb2_orders(id, jb2_id uniq, order_number, customer, status, due_date, payload jsonb,
           content_hash, jb2_last_modified, synced_at)
jb2_order_line_items(id, jb2_id uniq, jb2_order_id fk, part_number, description, qty,
           due_date, payload, content_hash, synced_at)
jb2_order_routings(id, jb2_id uniq, jb2_line_item_id fk, seq int, operation_code,
           description, work_center_code, est_setup_hrs, est_run_hrs, payload, synced_at)
jb2_order_materials(id, jb2_id uniq, jb2_line_item_id fk, routing_seq, part_number,
           description, qty_planned, unit, unit_cost, payload, synced_at)
jb2_parts(id, jb2_id uniq, part_number, description, revision, payload, synced_at)
jb2_work_centers(id, jb2_id uniq, code, name, payload, synced_at)
jb2_employees(id, jb2_id uniq, employee_code, name, active, payload, synced_at)
jb2_operation_codes(id, code uniq, description, work_center_code, payload, synced_at)
jb2_reason_codes(id, reason_number uniq, description, payload, synced_at)
jb2_documents(id, jb2_id uniq, document_number, revision, linked_part_number, payload, synced_at)
sync_runs(id, resource, started_at, finished_at, fetched int, changed int, error text)
jb2_outbox(id, kind, payload jsonb, idempotency_key uniq, status, attempts, last_error,
           work_order_id fk null, created_at, sent_at, confirmed_at)
mapping_exceptions(id, kind, value, context jsonb, resolved bool, created_at)

-- Library
products(id, name uniq, description, variant_schema jsonb, active)
product_part_map(id, product_id fk, jb2_part_number uniq, variant_values jsonb)
instruction_sets(id, product_id fk null /*null = global*/, operation_match jsonb,
           title, state enum(draft,in_review,published,retired), version int,
           parent_version_id fk null, est_minutes, created_by fk, published_by fk,
           published_at)   -- (product_id, operation_match, version) unique
steps(id, instruction_set_id fk, seq, title, body_html, est_minutes)
substeps(id, step_id fk, seq, type enum(action,measurement,inspection,photo,material,signoff),
           title, body_html, required bool, condition jsonb null,
           measurement_spec jsonb null /*{name,unit,nominal,tol_plus,tol_minus,gauge,decimals}*/,
           media jsonb /*[{kind,url,caption}]*/, signoff_role null)
failure_codes(id, product_id fk null, code uniq-per-scope, label, category,
           jb2_reason_number int null /*maps to JB2 reason codes*/, active)

-- Execution
work_orders(id, jb2_line_item_id fk uniq, product_id fk, variant_values jsonb, qty,
           due_date, status enum, plan_version int, priority int, notes, created_at)
units(id, work_order_id fk, unit_no int, serial_number null uniq-when-set,
           status enum, first_pass bool default true, rework_count int default 0,
           remake_of_unit_id fk null, current_plan_op_id fk null, completed_at)
build_boxes(id, qr_payload uniq, label, active, current_unit_id fk null,
           current_station_id fk null, last_scan_at)
box_assignments(id, box_id fk, unit_id fk, assigned_by fk, assigned_at, released_at)
stations(id, name, work_center_id fk, location, kiosk_token uniq, active, last_seen_at)
operators(id, jb2_employee_id fk null, display_name, badge_qr uniq, pin_hash,
           roles text[] /*operator,lead,quality,librarian,admin*/, active)

plan_operations(id, work_order_id fk, seq, jb2_routing_id fk, operation_code, title,
           station_hint fk null, instruction_set_id fk, instruction_version int,
           frozen_content jsonb /*full copy of steps+substeps at release*/,
           status enum(pending,active,done,skipped), est_minutes)
plan_pdfs(id, work_order_id fk, version int, path, sha256, generated_at, generated_by fk)

work_sessions(id, unit_id fk, plan_operation_id fk, operator_id fk, station_id fk,
           started_at, ended_at null, kind enum(first_pass,rework,setup),
           jb2_outbox_id fk null /*time-ticket-detail posted at close*/)
session_pauses(id, work_session_id fk, reason_code, started_at, ended_at)

step_executions(id, plan_operation_id fk, unit_id fk, step_seq, status,
           started_at, completed_at, completed_by fk)
substep_executions(id, step_execution_id fk, substep_seq, type, status
           enum(pending,in_progress,done,failed,skipped),
           operator_id fk, started_at, completed_at,
           value_numeric null, value_text null, pass bool null,
           out_of_tolerance bool null, disposition enum null, disposition_by fk null,
           skip_reason null, skip_authorized_by fk null, notes)
measurements(id, substep_execution_id fk, name, unit, value numeric, nominal,
           tol_plus, tol_minus, in_tolerance bool, gauge_id, recorded_by fk, recorded_at)
attachments(id, entity_kind, entity_id, kind enum(photo,file), path, sha256,
           uploaded_by fk, created_at)
material_records(id, substep_execution_id fk null, unit_id fk, plan_operation_id fk,
           part_number, description, lot, qty_planned null, qty_used, qty_scrapped,
           unit, substitution_for null, authorized_by fk null, recorded_at)

failures(id, unit_id fk, plan_operation_id fk, substep_execution_id fk null,
           failure_code_id fk, narrative, detected_by fk, detected_at,
           introduced_at_op fk null,
           disposition enum(rework_in_place,rework_to_op,scrap,use_as_is),
           rework_to_op fk null, authorized_by fk null)
scrap_events(id, unit_id fk, failure_id fk, cause_code, narrative, material_value_est,
           authorized_by fk, created_at, replacement_unit_id fk null)

scans(id, box_id fk, station_id fk, operator_id fk, scanned_at, raw_payload,
           result enum(accepted,wrong_station,unknown_box,unbound_box,rejected),
           override_by fk null, work_session_id fk null)
transits(id, unit_id fk, from_station_id fk, to_station_id fk,
           departed_at, arrived_at, seconds int)

events(id bigserial, at timestamptz, actor_id fk null, station_id fk null,
           entity_kind, entity_id, verb, before jsonb null, after jsonb null)
auth_events(id, operator_id fk, station_id fk, kind enum(login,logout,badge_fail,pin_fail), at)
```

Key indexes: `scans(scanned_at)`, `events(entity_kind, entity_id, at)`, `units(work_order_id, status)`, `work_sessions(operator_id, started_at)`, `jb2_outbox(status, created_at)`, partial index `build_boxes(current_station_id) where active`.

Retention: everything kept indefinitely (volumes are small — a shop's decade of events fits easily in Postgres); nightly `pg_dump` + WAL archiving (§16.4).

---

## 11. MES API Design

Internal REST API (FastAPI, OpenAPI auto‑docs at `/api/docs`, JSON): consumed by station UI, dashboard, and admin UI. All endpoints under `/api/v1`. Auth: station kiosk token (device) + operator badge session (person) — both required for floor actions (§14).

**Floor (station UI):**
```
POST /scan                      {qr_payload} → resolves box/badge; core state machine entry
POST /sessions/{id}/pause|resume {reason_code}
GET  /units/{id}/active-plan    → current op, steps, substeps, progress
POST /substeps/{id}/complete    {value?, pass?, notes?}
POST /substeps/{id}/fail        {failure_code, narrative, disposition, rework_to_op?}
POST /substeps/{id}/skip        {reason} (lead badge required)
POST /attachments               multipart (photo)
POST /materials                 {substep_execution_id?, part_number, qty_used, ...}
POST /operations/{id}/finish    → closes op, posts JB2 outbox, returns next-station card
POST /units/{id}/scrap          {failure_id, cause, ...} (lead badge)
POST /auth/badge                {badge_qr|pin} → operator session at this station
POST /auth/logout
```

**Admin / library / dashboard:**
```
CRUD /products /instruction-sets /steps /substeps /failure-codes /stations /operators
POST /instruction-sets/{id}/publish | /clone | /preview-pdf
GET  /work-orders?status=…      GET /work-orders/{id} (full drill-down)
POST /work-orders/{id}/assign-box {box_qr}
POST /work-orders/{id}/regenerate-plan (lead; §7.3 rules)
GET  /dashboard/pipeline        → all WIP w/ first-pass|rework, location, age, due
GET  /dashboard/metrics?window= → FPY, scrap, throughput, cycle-vs-est, queue times
GET  /health /health/jb2 /health/outbox /health/sync
WS   /ws/dashboard              → live pipeline events (or SSE fallback)
```

Rules: all writes idempotent where retried by tablets (client‑generated request ids); server is the single validator of the state machine (tablet UI is thin); errors return machine‑readable codes the UI maps to operator‑friendly messages.

---

## 12. Station UI

### 12.1 Principles

- **Kiosk mode.** Each tablet runs a fullscreen browser (Chromium kiosk) pointed at `https://mes.local/station/{station}`; the device carries a station token. Survives reboot to the same screen.
- **Scanner‑first.** A hidden, always‑focused input captures USB‑wedge scans anywhere in the UI (scans end with Enter). Badge scans and box scans are distinguished by QR payload prefix (`OP:` vs `BOX:`).
- **Glove‑friendly:** minimum 48 px touch targets, large type, high contrast; no hover interactions; works at arm's length.
- **Thin client:** the tablet holds no state machine — every action round‑trips to the server (LAN, <50 ms). If the server is unreachable, the UI shows a full‑screen "MES offline — use paper guide" state (§17.2).

### 12.2 Screens

1. **Idle:** station name, operator login prompt ("scan your badge"), list of boxes currently queued at/inbound to this station.
2. **Scan result:** unit card (product, serial, order, due date, first‑pass/rework banner), operation title, progress (step 3/9), Start/Resume button — or the wrong‑station warning with lead‑override path.
3. **Execution:** left rail = step list with status ticks; main pane = current step (rich text, images tappable to zoom, video inline); substep checklist with type‑specific controls; sticky footer = elapsed timer, pause, fail, finish‑operation (disabled until complete).
4. **Measurement entry:** numeric keypad overlay, nominal/tolerance shown, immediate green/red feedback, out‑of‑tolerance triggers disposition dialog.
5. **Finish summary:** durations, measurements table, any flags; confirm → "Move box to: Station 4 — Barrel Fit" card with big arrow.
6. **Lead/override dialogs:** any privileged action prompts "lead badge scan" inline.

### 12.3 Implementation

- Server‑rendered HTMX + Alpine.js (preferred: no build chain, trivially cacheable, easy for a coding agent to keep consistent with PDF templates) — or React SPA if interactive richness demands it. Decision left to implementation plan; templates must remain shared with PDF rendering either way (§8).
- WebSocket per station for push (queue changes, lead responses).
- Photos: `<input capture>` from tablet camera or bench webcam; client resizes to ≤2 MP before upload.

---

## 13. Dashboard

### 13.1 Pipeline board (primary view, office TV + browsers)

- One card per active **unit**: product + variant, serial/unit #, order + due date, current station (or in‑transit), current operation, elapsed at station, operator, progress bar.
- **Color coding:** green = first‑pass on track; **amber** = queue dwell over threshold; **red** = overdue vs. JB2 due date or stalled > X hrs; **purple border = rework unit** (the requested first‑pass vs. rework highlight); grey = awaiting start; hatched = blocked_no_instructions.
- Grouping toggles: by station (lane view = shop layout), by product, by order, by due date.
- Click‑through: unit → full history timeline (every scan, session, substep, measurement, failure — the §9 record, chronologically).
- Live via WebSocket; renders read‑only on TVs (no auth beyond network), interactive for logged‑in office users.

### 13.2 Metrics view

- First‑pass yield by product / operation / date range; scrap Pareto by failure code; throughput (units/day, by product); actual vs. estimated minutes per operation (feeds instruction estimate tuning); queue time by station (bottleneck finder); rework hours as % of total; WIP age histogram; JB2 schedule overlay (`eci-aps/get-schedule`) vs. MES reality.
- All metrics are SQL views over the §10 tables — no separate analytics store in v1.

---

## 14. Identity, Auth, and Roles

- **Devices:** each station tablet holds a long‑lived station token (httpOnly cookie bound to device, issued by admin "enroll station" flow). Dashboard TVs get read‑only tokens.
- **Operators:** badge QR scan (payload `OP:{uuid}`, random, revocable) or PIN fallback on the tablet. A badge scan opens an operator session **at that station** (expires after configurable idle, e.g. 10 min, or on badge‑out or badge‑in at another station — one active station per operator).
- **Roles:** `operator` (execute), `lead` (overrides, scrap auth, out‑of‑sequence, plan regeneration), `quality` (signoffs, disposition of out‑of‑tolerance), `librarian` (instruction authoring/publishing), `admin` (mappings, stations, users, health). Office UI uses username/password + role, standard session auth.
- Badges are printed by the MES (PDF sheet of badge cards, §8 generator reused).
- Everything privileged records *who authorized* — a second badge scan, not a shared password.
- Network: MES reachable only on shop LAN/VLAN; Caddy terminates TLS with internal CA or `mes.<domain>` cert; no public exposure. JB2 API is the only outbound call.

---

## 15. Non‑Functional Requirements

- **N1 Availability:** shop floor must function during JB2/cloud outages (mirror + outbox absorb both directions). Single‑server is accepted risk in v1; recovery = restore from nightly backup + WAL (≤15 min data loss target, RPO; RTO ≤ 4 h with documented runbook).
- **N2 Performance:** scan→instructions render < 1 s on LAN; dashboard updates < 2 s after event; 25 concurrent stations comfortably (this is trivially within one FastAPI/Postgres box).
- **N3 Data integrity:** all state transitions in DB transactions; state machine enforced server‑side; append‑only events; no cascading deletes.
- **N4 Security:** secrets in env only; least‑privilege JB2 key; TLS everywhere via Caddy; regular OS patching (unattended‑upgrades); auditable everything; photos/PDFs on encrypted disk (LUKS) if the box is physically accessible.
- **N5 Observability:** structured JSON logs (uvicorn + app) → journald; `/health*` endpoints; optional Prometheus metrics + Grafana later; alert (email) on: outbox failures, sync stalls > 10 min, station offline > 15 min.
- **N6 Testability:** JB2 client behind an interface with a **fake JB2 server** (replay of recorded fixtures from Phase 0) so the whole system tests offline in CI; state machine covered by property‑style tests (no illegal transition reachable via API).
- **N7 Maintainability:** single repo, single deployable; Alembic migrations; seed script creates demo product/instructions for training.

---

## 16. Deployment and Operations

### 16.1 Topology

Single Linux server (Ubuntu 24.04 LTS), Docker Compose **or** bare systemd — implementation plan chooses; document assumes systemd for fewer moving parts:

```
caddy.service          → Caddy 2, ports 80/443
mes-api.service        → uvicorn app.main:app (workers=4)
mes-sync.service       → sync worker (poll loops)
mes-outbox.service     → outbox drainer
postgresql.service     → PostgreSQL 16 (localhost only)
```

### 16.2 Caddyfile (sketch)

```
mes.atlas.internal {
    encode zstd gzip
    handle /static/* { root * /var/lib/mes/static  file_server }
    handle /artifacts/* { root * /var/lib/mes  file_server }   # PDFs (auth via forward_auth)
    reverse_proxy /api/* 127.0.0.1:8000
    reverse_proxy /ws/*  127.0.0.1:8000
    reverse_proxy 127.0.0.1:8000
    tls internal   # or ACME if a public hostname is used
}
```

### 16.3 Environments

- `dev` (developer laptop, fake‑JB2 fixtures), `staging` (same server, second compose project / port, pointed at JB2 with a test company or read‑only key), `prod`.
- Config via env vars only: `JB2_BASE_URL, JB2_CLIENT_ID, JB2_CLIENT_SECRET, DATABASE_URL, MES_SECRET_KEY, ARTIFACT_DIR, …`.

### 16.4 Backup / restore

- Nightly `pg_dump` + continuous WAL archiving to a second disk **and** offsite (restic → S3‑compatible or NAS); `/var/lib/mes/artifacts` in the same restic set; documented quarterly restore drill.

### 16.5 Upgrades

- Git tag → CI builds artifact → `deploy.sh` runs migrations then restarts services; migrations must be backward‑compatible one release back (rolling safety); station tablets hard‑refresh via served version bump.

---

## 17. Edge Cases and Failure Modes

- **17.1 JB2 outage / auth failure:** circuit breaker opens; banner on admin UI; floor unaffected; outbox accumulates; sync resumes from checkpoints. Token refresh failures alert immediately.
- **17.2 MES/server outage:** stations show offline screen; crews fall back to the printed PDF guide and paper measurement boxes; on recovery, a lead uses the **backfill screen** to enter paper data (entries flagged `backfilled=true`, original paper retained).
- **17.3 Wrong/unknown/unbound box scans:** every case has a defined screen (§6.3, §12.2); all rejected scans recorded (`scans.result`).
- **17.4 Order qty changes in JB2:** qty↑ → MES adds units (new plans at current published versions, flagged for lead awareness); qty↓ → lead picks which unstarted units to cancel; started units need explicit disposition.
- **17.5 Routing changed in JB2 after release:** sync detects diff vs. frozen plan → work order flagged `routing_drift`; lead reconciles (regenerate remaining ops or ignore); never silently mutated.
- **17.6 Box lost/damaged:** lead reassigns unit to a new box (history preserved via `box_assignments`); old box retired.
- **17.7 Two operators, one unit:** allowed (team ops): both badge in; two sessions; both post to JB2 (JB2 model is per‑employee tickets, so this maps cleanly).
- **17.8 Operator forgets to finish/clock out:** idle timeout auto‑pauses session and pages the lead queue; auto‑closed sessions marked `auto_closed` and require lead confirmation before JB2 posting (keeps garbage time out of job costing).
- **17.9 Duplicate scans / double taps:** idempotent by client request id + server state machine (a second "finish" is a no‑op).
- **17.10 Clock skew:** tablets render server time; all timestamps server‑side; server on NTP.
- **17.11 Partial batch at an op (qty > 1 per box):** operator records per‑unit outcomes on the finish screen (n good, m scrapped with causes); JB2 gets aggregate; MES keeps per‑unit truth.
- **17.12 Expedites:** lead can pin priority on a work order; dashboard sorts accordingly; JB2 `priority` displayed alongside for conflict visibility.

---

## 18. Open Questions / Decisions Needed

1. **Serial number timing:** assigned at kit‑up or at final assembly? (Schema supports either; workflow default needed. Firearms serialization rules make this a compliance‑adjacent choice — decide with whoever owns ATF process.)
2. **Operator‑level performance metrics visibility** (§9.11): admin‑only, or open? Team‑culture decision.
3. **Publish approval workflow** (§7.2): enforce two‑person publish now or start single‑approver?
4. **HTMX vs. React** for station UI (§12.3) — pick during implementation planning after a spike.
5. **Does Atlas use JB2 releases/lots** on orders? If yes, mirror `releases` and display.
6. Threshold values: queue‑dwell amber/red, idle auto‑pause, session expiry — set with floor leads during pilot.
7. Should completed‑unit data auto‑generate a **build record PDF** (all measurements + photos per serial) for warranty/service files? (Cheap to add; recommend yes, Phase 4.)

---

## 19. Phased Implementation Roadmap

Each phase ends with a demoable milestone and acceptance criteria a coding agent can test against.

- **Phase 0 — JB2 behavioral verification (≈3 days).** §4.7 report; recorded fixtures for the fake‑JB2 test server. *Accept:* every `[VERIFY-JB2]` resolved; fixtures replay in CI.
- **Phase 1 — Skeleton + sync (1–2 wks).** Repo, CI, deploy pipeline, Caddy, Postgres, migrations; mirror sync for all §4.2 resources; admin health pages. *Accept:* new JB2 order appears in MES ≤ 90 s; sync survives restarts/outages.
- **Phase 2 — Library + plans + PDFs (2–3 wks).** Products, mappings, instruction builder, versioning/publish, plan generation with frozen content, PDF guides, QR/badge printing. *Accept:* create Apollo instruction set → JB2 Apollo order auto‑produces plan + PDF with correct routing binding.
- **Phase 3 — Floor execution (2–3 wks).** Station kiosk, badge auth, scan flow, step/substep execution, measurements, failures/scrap/rework, sessions, outbox write‑backs. *Accept:* full happy‑path unit travels 3 stations end‑to‑end; JB2 shows correct time tickets and quantities; all §9.1–9.4 data queryable.
- **Phase 4 — Dashboard + metrics + hardening (1–2 wks).** Pipeline board, first‑pass/rework highlighting, metrics views, alerts, backfill screen, backup drill, build‑record PDF (if #18.7 = yes). *Accept:* N1–N5 verified; pilot on one product line.
- **Phase 5 — Later.** Material adjustment exploration, attendance push, machine integration (NG2), SPC (NG3), live clock‑on experiments (§4.4 last row).

Suggested repo layout for the implementation plan:

```
mes/
  app/            (FastAPI: api/, domain/, jb2/, sync/, outbox/, pdf/, ws/)
  templates/      (shared HTML: station UI + PDF)
  migrations/
  tests/          (unit, state-machine, fake-jb2 integration)
  deploy/         (Caddyfile, systemd units, deploy.sh, backup scripts)
  docs/           (this document, jb2-api-findings.md, runbooks)
```

---

## 20. Glossary

| Term | Meaning |
|---|---|
| Build box / traveler | Physical container carrying one unit (or small batch) through the shop, labeled with the routing QR. |
| Execution plan | Frozen expansion of routing × instruction versions for one work order. |
| First pass | Unit that has never entered a rework loop. |
| Instruction set | Versioned step/substep instructions for one operation on one product. |
| JB2 | JobBOSS² cloud ERP (ECI Software Solutions). |
| Operation / step number | A line in the JB2 order routing (`stepNumber`), mirrored 1:1 by the MES plan. |
| Outbox | Queued, idempotent JB2 write‑backs. |
| Substep | Atomic checkable action within a step (action, measurement, inspection, photo, material, signoff). |
| Work order | MES execution record for one JB2 order line item. |
| Work session | One operator's continuous labor interval on one unit at one operation — maps to a JB2 time‑ticket detail. |

---

*End of document. Version 1.1 — generated 2026‑07‑10. JB2 API facts verified against the live OpenAPI spec; behavioral checks tracked as `[VERIFY-JB2]` for Phase 0.*
