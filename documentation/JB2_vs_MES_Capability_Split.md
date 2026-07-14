# JobBOSS² vs. Atlas MES — Capability Split at a Glance

Companion to *MES_Design_Document.md*. Based on the live JobBOSS² OpenAPI spec (verified 2026‑07‑10).
Rule of thumb: **JB2 owns money and orders; the MES owns execution and evidence.**

---

## Side by side

| Area | ✅ JobBOSS² already handles | ❌ JobBOSS² cannot — MES handles |
|---|---|---|
| **Orders** | Order entry, line items, part numbers, quantities, due dates, revisions, priorities. API: `GET /orders`, `/order-line-items` with `lastModDate` incremental sync. | Detecting new orders and auto-creating tracked work orders with execution plans per unit. |
| **Routing** | Authoring the routing: step numbers, operation codes, work centers, setup/cycle estimates. API: `GET /order-routings`. | Binding each routing step to versioned step-by-step instructions; freezing a plan per order. |
| **Work instructions** | Nothing below the routing operation. One text description per step, no media, no versioning. | Entire instruction library: steps → substeps, images/video, tolerances, conditional content by variant, versioning, publish workflow, PDF build guides. |
| **Labor / time** | Per-employee labor tickets per job + operation: time start/end, setup vs. cycle, shift. API: `POST /time-tickets`, `POST /time-ticket-details`. **MES posts these automatically** — JB2 costing stays accurate. | Per-substep timestamps, pause reasons, setup/run split per session, rework time vs. first-pass time, queue/dwell time, estimated-vs-actual per step. |
| **Quantities** | Pieces finished / scrapped per operation (`piecesFinished`, `piecesScrapped`); routing actuals (`actualPiecesGood/Scrap`, `status` via PATCH — writable-field set to confirm in Phase 0). | Per-unit outcomes inside a batch, which unit failed, remake linkage from scrapped unit to replacement. |
| **Operation status** | "Job is at operation N" — routing step `status`, `OrderLineItem.currentWorkCenter`. MES pushes transitions. | Physical box location in real time, full scan/location history, in-transit tracking between stations. |
| **Scrap reasons** | A flat reason-code list (`GET /reason-codes`) attached to time tickets. | Full failure taxonomy per product/operation, narrative + photos, disposition workflow (rework-in-place / send back to op K / scrap with lead authorization), first-pass vs. rework flag. |
| **Materials** | Planned material per job/step, costs, lots/bins — **read-only** (`GET /job-materials`, `/job-requirements`). No API to post consumption. | Actual material used/scrapped per step, deltas vs. plan, substitutions with authorization. Sole system of record. |
| **Quality / measurements** | Non-conformances and CAPA exist in JB2 but are **read-only** via API; no dimensional capture at all. | Every measurement with nominal/tolerances, in/out-of-tolerance dispositions, inspections, sign-offs, photo evidence. Sole system of record. |
| **Serial numbers** | No serial-number resources in the API. | Serial capture per unit, serial-to-order-to-measurement traceability, per-serial build record. |
| **People** | Employee master (`GET /employees`); payroll attendance tickets (postable, optional later). | Badge/PIN auth at stations, roles (operator/lead/quality/librarian), who-did-what on every action, lead overrides. |
| **Scheduling** | Due dates, priorities, APS schedule (`GET /eci-aps/get-schedule`), ShopView KPIs — displayed by MES. | Live shop-floor reality: which box is where, who's working on it, what's stalled. |
| **Visibility** | Office-side reports and ShopView job lists. | Real-time pipeline dashboard: every unit, first-pass vs. rework highlighting, WIP aging, bottleneck/queue analytics, drill-down to the substep. |
| **Money** | Job costing, invoicing, purchasing, shipping — fed automatically by MES time/quantity write-backs. | Nothing. MES never touches money. |

---

## The one-sentence version

JobBOSS² knows **what to build, in what order, and what it cost**. The MES knows — and JB2 has no way to know — **exactly what happened to each physical unit: every instruction shown, box scanned, minute worked, dimension measured, failure dispositioned, and rework loop taken.**

## Integration summary

- **MES → JB2 (write, verified):** time-ticket details (labor + pieces good/scrapped + reason code), routing-step actuals/status.
- **JB2 → MES (read, verified):** orders, line items, routings, planned materials, parts, work centers, operation codes, employees, reason codes, documents, schedule.
- **Impossible via API (MES-only forever, until ECI adds endpoints):** material actuals, quality/NC writes, serials, anything sub-operation.
