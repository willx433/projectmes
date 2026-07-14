# Research Notes — Atlas MES (ProjectAMR)

Phase 0 research. Distilled MES reference patterns + how we apply each to our four
in-scope domains and our peer-integration model. Sources inline. This is a working
reference, not a spec — requirements.md derives from it after approval.

In-scope domains (the four this project serves):
1. **Production tracking & WIP**
2. **Scheduling & dispatch**
3. **Quality / inspection & non-conformance**
4. **Operator work instructions & routing**

Out of scope (future stubs only): OEE/machine telemetry, full material genealogy/traceability.

---

## 1. ISA-95 — the activity backbone

ISA-95 (IEC 62264) splits Level-3 plant operations into **four operations-management
domains**, and models **each** with the *same* generic activity loop:

- Domains: **Production**, **Quality**, **Maintenance**, **Inventory** operations.
- Generic activity loop (8, per domain): **Definition mgmt → Resource mgmt → Detailed
  scheduling → Dispatching → Execution → Data collection → Tracking → Performance
  analysis**, closed loop plan→execute→feedback.

**Why it matters for us:** it gives a domain-agnostic vocabulary. Our four in-scope
domains map cleanly onto ISA-95 Production + Quality operations, reusing the *same*
loop shape. The seed app already implements the **Definition mgmt** corner (Models /
Operations / Routings / Line Balances). We build the rest of the loop on top of it.

**How we'll apply:** structure the data model and the build phases around the loop:
definition (have it) → schedule → dispatch → execute/WIP-move → collect → track →
(quality runs the same loop in parallel). Don't invent a bespoke taxonomy.

Sources: [ISA-95 / MOM definition (Symestic)](https://www.symestic.com/en-us/what-is/manufacturing-operations-management),
[ISA-95 for MES & ERP integration (Symestic)](https://www.symestic.com/en-us/blog/mes/isa95),
[What is ISA-95 (ATS)](https://www.advancedtech.com/blog/what-is-isa-95/).

---

## 2. MESA-11 — the functional checklist

Eleven functional areas; we are explicitly building a **subset** and stubbing the rest.

| # | MESA-11 function | In/out for us | Note |
|---|---|---|---|
| 1 | Operations/Detail Scheduling | **IN** | domain 2 |
| 2 | Resource Allocation & Status | **IN (light)** | work centers, operators, station status |
| 3 | Dispatching Production Units | **IN** | domain 2 — dispatch lists |
| 4 | Document Control | **IN** | domain 4 — work instructions + revisioning |
| 5 | Data Collection/Acquisition | **IN (manual)** | operator entry; no machine telemetry |
| 6 | Labour Management | partial | operator assignment yes; attendance = JB2's |
| 7 | Quality Management | **IN** | domain 3 — inspection + NCR/disposition |
| 8 | Process Management | **IN** | domain 1+4 — routing enforcement, WIP move |
| 9 | Maintenance Management | OUT | future stub |
| 10 | Product Tracking & Genealogy | OUT | future stub (light WIP tracking only) |
| 11 | Performance Analysis | OUT (OEE) | future stub; basic counts only |

**How we'll apply:** MESA-11 is our scope fence. Each requirement in requirements.md
tags which MESA function it serves, so coverage gaps are visible.

Sources: [MESA-11 11 functions (Symestic)](https://www.symestic.com/en-us/what-is/mesa-11),
[Why so many standards — MESA/ISA-95 (Tulip)](https://tulip.co/blog/mes-isa-95-mes-11-cmes-namur/),
[History of MESA models](https://mesa.org/topics-resources/mesa-model/history-of-the-mesa-models/).

---

## 3. Work-order → routing → operation → WIP-move lifecycle

Established pattern (Oracle WIP, D365, Plex, Deskera):

- **Routing** = ordered list of operations (the "roadmap"); each operation has a
  work center, std setup/run time, and instructions.
- **Work order** released against a routing; quantity lands in the **Queue**
  intraoperation step of operation 1.
- Per-operation **intraoperation steps**: `Queue → Run → To-move → (Reject/Scrap)`.
- A **WIP move** transacts qty from one operation's step to the next; this is the
  atomic shop-floor event ("move 5 from op20-run to op30-queue").
- Work-order lifecycle states: **Released → Dispatched → In progress** (setup done,
  first good part) **→ Completed** (target qty, actuals posted).

**How we'll apply:** the WIP move is our core write transaction (domain 1). Model
operation instances with intraoperation step + on-hand qty per step; every move is an
append-only event so WIP state is reconstructable and we get a free audit trail. This
is also the natural JB2 `time-ticket` / `order-routing` sync boundary.

Sources: [Oracle WIP User's Guide](https://docs.oracle.com/cd/E18727_01/doc.121/e13678/T228107T228119.htm),
[Manufacturing order management (Symestic)](https://www.symestic.com/en-us/what-is/manufacturing-order-management),
[Routing in manufacturing (Deskera)](https://www.deskera.com/blog/understanding-routing-in-manufacturing/),
[WIP in-flight inventory (SG Systems)](https://sgsystemsglobal.com/glossary/work-in-process-wip-in-flight-inventory/).

---

## 4. Dispatch lists & station/queue views

- **Dispatch list** = per-work-center, ordered "what to run next" list, sequenced by
  priority/due-date/changeover, refreshed as state changes.
- **Station/queue view** = operator-facing: jobs currently in *this* work center's
  queue, with the active operation's instructions and a move/complete action.

**How we'll apply:** two read-models over the same WIP state — a planner dispatch board
(all work centers) and an operator station view (one work center). Both derive from the
move-event log; no separate dispatch table to keep in sync.

Sources: [Dispatch work orders (Microsoft D365)](https://learn.microsoft.com/en-us/dynamics365/supply-chain/asset-management/work-order-scheduling/dispatch-work-order),
[What is MES (Plex/Rockwell)](https://plex.rockwellautomation.com/en-us/products/manufacturing-execution-system/what-is-mes.html).

---

## 5. Non-conformance / disposition workflow (quality)

Standard NCR→MRB flow (ISO 9001 cl. 8.7, AS9100, IATF 16949):

1. Operator / inspection **flags** a defect → **NCR** created (part, lot, work order,
   operation, defect type, qty, inspection data).
2. Nonconforming material **segregated** (quarantine location / hold).
3. **MRB** (or single reviewer in a small shop) reviews → assigns **disposition** with
   documented justification.
4. Dispositions: **rework, scrap, use-as-is, return-to-vendor, regrade**.
5. Disposition **drives downstream action**: rework → new/again work order step; scrap →
   inventory decrement; etc. Full audit trail required.

**How we'll apply:** NCR is a first-class entity linked to the WIP operation instance.
A flagged part is held (can't move forward) until disposition. Disposition is a
state machine with a required reason + actor + timestamp. Atlas is a small shop → make
MRB a configurable single-approver step, not a heavyweight board, but keep the audit
fields so it scales. JB2 exposes `non-conformances` + `corrective-preventive-actions`
**GET-only** — so MES must *own* the active NCR workflow (see integration map).

Sources: [Nonconformance management guide (1factory)](https://www.1factory.com/quality-academy/guide-nonconformance-management.html),
[MRB process (Tulip)](https://tulip.co/blog/material-review-board/),
[MRB / disposition (SG Systems)](https://sgsystemsglobal.com/glossary/material-review-board-mrb/).

---

## 6. Operator work instructions & acknowledgment (document control)

- Instructions delivered **at point-of-use**, **in context** of the order/operation/
  variant — not a static doc dump.
- Steps shown in **defined sequence**; operator **acknowledges** each (click / "yes" /
  data entry / e-signature); skipping/reordering a critical step **triggers a deviation**.
- Every ack is logged with **timestamp + operator attribution** → audit trail.
- Instructions are **revision-controlled**; an order runs against the revision in effect
  when it was released (ECN/ECO discipline).

**How we'll apply:** work instructions attach to an **operation within a routing
version** (the seed already versions routings — keep that). Operator station view
renders the steps for the active operation and records acks against the WIP operation
instance. Revision lock = order carries its routing-version id. This is domain 4 and it
reuses the seed's routing/version model directly.

Sources: [Electronic work instructions (Siemens)](https://www.siemens.com/en-us/technology/electronic-work-instructions/),
[Digital work instructions (Symestic)](https://www.symestic.com/en-us/what-is/digital-work-instructions),
[Paperless manufacturing guide (Parsec)](https://www.parsec-corp.com/blog/the-complete-guide-to-implementing-a-paperless-manufacturing-system).

---

## 7. Peer integration — applying findings to MES / JobBOSS2 / Airtable

The frameworks above assume one MES owns everything. We don't: MES, **JobBOSS2 (JB2)**,
and **Airtable** are peers. The hard ground-truth from prior JB2 discovery shapes what's
even possible:

- JB2 write surface is **narrow and asymmetric** — most resources GET-only. Writable:
  orders, order-line-items, order-routings, quotes, estimates, customers, vendors,
  time-tickets, attendance-tickets, contacts, shipping-addresses, work-centers.
- **`non-conformances` and `corrective-preventive-actions` are GET-only** → JB2 cannot
  be the live system of record for our quality workflow.
- `ar-invoices` unbounded GET **500s** → always filter JB2 reads.
- JB2 **ignores `Idempotency-Key`** on order POST (dup orders observed) → our sync layer
  must dedup, not trust JB2.
- `jobs` only via `shopview/get-jobs`, not a top-level resource.
- Dates UTC `yyyy-MM-ddTHH:mm:ssZ`; filter ops `?field[op]=value`; paging `take/skip`.

(See memory: project-jb2-discovery-2026-05-10, project-jb2-pr7-phase-a-2026-05-11,
reference-jb2-api-endpoint-catalog.)

**Proposed system-of-record per domain** (DRAFT — needs your confirmation, esp. Airtable):

| Data domain | Proposed SoR | Flow | Reconciliation |
|---|---|---|---|
| Jobs / orders | **JB2** | MES pulls (filtered poll → SQLite cache) | read-only mirror; JB2 wins |
| Routing/op **master** + work instructions | **MES** | authored in MES | MES wins; JB2 order-routings derived per-order |
| Per-order applied routing | JB2 (or MES?) | **open question** | depends on where orders get routed |
| Production tracking / WIP moves | **MES** | live in MES | optional write-back to JB2 time-tickets |
| Quality / NCR / disposition | **MES** (JB2 NC is GET-only) | live in MES | JB2 NC = legacy read-only mirror at best |

**Reconciliation principle:** within an MES-owned domain, last-write-wins is fine.
Across systems, the **SoR wins** and any divergence is written to a **reconciliation
log / surfaced**, never silently overwritten (carry-over of the order-ref-as-truth rule
from the Shopware/NMI work). All JB2 reads filtered; all JB2 writes deduped MES-side.

---

## Open questions for William (blocking requirements.md)

1. **Airtable's role** — memory has *nothing* on it. What lives in Airtable today, and
   for which domain (if any) is it the system of record? Until answered I cannot place
   it in the ownership matrix.
2. **Order routing ownership** — when an order is created in JB2, is its routing assigned
   in JB2, or does MES own the applied routing and push `order-routings` to JB2? (Memory
   says shop flow "isn't established yet.")
3. **Write-back appetite** — do we push WIP/labor back to JB2 `time-tickets`, or is MES a
   read-from-JB2 / own-its-own-WIP island for now? (Affects phase ordering.)
4. **NCR of record** — confirm MES owns the live NCR workflow (JB2 NC API being GET-only
   forces this), with JB2 as a non-authoritative mirror.
