# Requirements — Atlas MES ("Make Ready" / ProjectAMR)

Source of truth for *what* the system must do. Derived from `research-notes.md` and the
Phase-0 decisions logged as CR-001 in `change-requests.md`. Implementation sequencing
lives in `implementation-plan.md`; components in `library.md`.

**Conventions**
- ID = `<DOMAIN>-<n>`. Domains: **PT** production tracking & WIP, **SD** scheduling &
  dispatch, **QA** quality/NCR, **WI** work instructions & routing, **INT** integration/
  ownership, **NF** non-functional.
- **Type:** F (functional) / NF (non-functional).
- **Priority:** MoSCoW — **M**ust / **S**hould / **C**ould / **W**on't-now.
- Each requirement is testable; acceptance criteria (AC) are the test.
- MESA-11 / ISA-95 tags show framework lineage (see `research-notes.md`).

---

## Locked decisions (from CR-001)

- **JB2** is system of record for **jobs/orders** and **per-order applied routings**.
- **MES** is system of record for **routing/operation master + work instructions**,
  **WIP/production tracking**, and **quality/NCR**.
- **Airtable** is **not authoritative** for any domain (planning scratch only).
- MES is a **read-only island vs JB2** for now — no write-back to JB2 in core phases.

---

## Domain 1 — Production Tracking & WIP (PT)

| ID | Type | Pri | Requirement | AC |
|----|------|-----|-------------|----|
| PT-1 | F | M | A job released to the floor creates WIP positioned at the **Queue** step of operation 1 of its applied routing. | Releasing a job yields one WIP record at op1/Queue with full released qty. |
| PT-2 | F | M | WIP is tracked per operation with **intraoperation steps** `Queue → Run → To-move → (Reject/Scrap)`. | Each operation instance exposes qty held in each step; sums reconcile to released qty minus scrap. |
| PT-3 | F | M | A **WIP move** transacts a quantity from one step/operation to the next and is recorded as an **append-only event** (actor, qty, from, to, timestamp). | A move appends an immutable event; current WIP state is reconstructable purely from the event log. |
| PT-4 | F | M | Current WIP state is a **derived read-model** over the move-event log (no separately mutated state of record). | Replaying events from empty reproduces identical current state. |
| PT-5 | F | M | A job's lifecycle state is derived: **Released → In-progress → Completed** (completed when target qty reaches the final To-move/closed). | State transitions automatically from move events; no manual state field. |
| PT-6 | F | S | Partial moves and split quantities across operations are supported (a job's qty can straddle multiple operations). | Qty can be split; per-step sums remain consistent. |
| PT-7 | F | S | Every WIP event carries the **operator/actor attribution** for audit. | Each event row has a non-null actor; audit view lists who moved what when. |
| PT-8 | NF | M | WIP state for the active shop floor (hundreds of open ops) renders in **< 1 s** from the SQLite cache. | Station/board views load < 1 s with representative data volume. |

---

## Domain 2 — Scheduling & Dispatch (SD)

| ID | Type | Pri | Requirement | AC |
|----|------|-----|-------------|----|
| SD-1 | F | M | A **dispatch board** lists, per work center, the jobs/operations awaiting work, ordered by a sequencing rule. | Board shows each work center's queue in sorted order; reorders as state changes. |
| SD-2 | F | M | An **operator station view** shows the queue for a single work center plus the active operation's work instructions and a move/complete action. | Operator at WC sees only their queue + can advance a job from that view. |
| SD-3 | F | M | Default sequencing rule = **due-date then priority**; rule is centrally configurable. | Changing the rule reorders all dispatch lists; default behaves as specified. |
| SD-4 | F | S | Dispatch board and station view are **read-models over the same WIP move-event log** (no separate dispatch table to sync). | Both views derive from WIP state; a move updates both without a separate write. |
| SD-5 | F | C | Manual re-prioritization (planner pins/bumps a job) overrides the sequencing rule for that job. | A pinned job sorts ahead regardless of rule; override is logged. |
| SD-6 | NF | S | Station view is usable on a shop-floor tablet/kiosk (large touch targets, no horizontal scroll). | Station view passes a tablet-width render check; primary actions reachable without zoom. |

---

## Domain 3 — Quality / Inspection & Non-Conformance (QA)

| ID | Type | Pri | Requirement | AC |
|----|------|-----|-------------|----|
| QA-1 | F | M | An operator or inspector can **flag a non-conformance (NCR)** against a specific WIP operation instance, capturing part, qty, defect type, and free-text detail. | Flagging creates an NCR linked to the job/op/lot with required fields enforced. |
| QA-2 | F | M | Flagged quantity is **placed on hold** and **cannot move forward** until dispositioned. | A held qty is rejected by the move engine with a clear reason until disposition. |
| QA-3 | F | M | NCR carries a **disposition state machine**: `Open → Under-review → Dispositioned`, with disposition ∈ {rework, scrap, use-as-is, return-to-vendor, regrade}. | Only legal transitions allowed; disposition requires actor + reason + timestamp. |
| QA-4 | F | M | Disposition drives **downstream action**: rework → qty returns to a designated operation step; scrap → qty removed from WIP. | Each disposition produces the correct WIP move event(s); state stays consistent. |
| QA-5 | F | S | Single-approver MRB is **configurable**; audit fields (reviewer, justification, timestamps) are always captured regardless of approver count. | NCR record retains full audit trail; approver count is a config, not a schema change. |
| QA-6 | F | C | Inspection checkpoints can be attached to an operation (pass/fail + measured value) and a fail auto-creates an NCR. | A failed inspection at an op generates an NCR linked to that op. |
| QA-7 | NF | M | NCR records are **immutable once dispositioned** (correction = new linked record, not edit). | Attempting to edit a closed NCR is blocked; corrections create a linked successor. |

---

## Domain 4 — Operator Work Instructions & Routing (WI)

| ID | Type | Pri | Requirement | AC |
|----|------|-----|-------------|----|
| WI-1 | F | M | MES owns **routing master**: a routing is an ordered list of operations, each with a work center and standard times. (Inherited from seed.) | A routing can be authored with N ordered operations; order is enforced. |
| WI-2 | F | M | Routings are **versioned**; a new version supersedes without mutating prior versions. (Inherited from seed.) | Editing a published routing creates a new version; old versions remain readable. |
| WI-3 | F | M | **Work instructions** (sequenced steps; text + image) attach to an operation within a routing version. | An operation renders its ordered instruction steps in the station view. |
| WI-4 | F | M | A job is **revision-locked**: it runs against the routing-version work instructions in effect when it was released, even if a newer version is later published. | A job released on v1 keeps showing v1 instructions after v2 publishes. |
| WI-5 | F | M | Operators **acknowledge** each instruction step (click/data entry); acks are logged with operator + timestamp. | Advancing past a step requires an ack; ack appears in the audit trail. |
| WI-6 | F | S | A required step that is skipped or done out of sequence **raises a deviation** (logged; optionally blocks). | Out-of-sequence completion creates a deviation record. |
| WI-7 | F | S | Line Balances (takt-time balancing of a routing) are retained from the seed and tied to routing versions. | A line balance references a routing version and survives version changes of others. |
| WI-8 | NF | S | Work-instruction media is served locally (no external CDN); internal-network-only. | Instruction images load with the host offline from the public internet. |

---

## Integration & Ownership (INT)

| ID | Type | Pri | Requirement | AC |
|----|------|-----|-------------|----|
| INT-1 | F | M | **JB2 is system of record for jobs/orders.** MES holds a **read-only mirror** populated by **filtered** polls into SQLite. | MES job list matches JB2 for a filtered query; MES never writes orders to JB2. |
| INT-2 | F | M | **JB2 is system of record for per-order applied routings.** MES reads JB2 `order-routings`; MES overlays its own work instructions, never overwriting JB2's routing. | An order's operation sequence comes from JB2; MES instructions attach on top. |
| INT-3 | F | M | **MES is system of record for routing/op master, WIP, and quality/NCR.** These are authored and live in MES. | No external system is queried as the truth for these three; MES is authoritative. |
| INT-4 | F | M | **Airtable is never treated as a system of record.** MES may read it for planning context only. | No write path treats Airtable data as authoritative; reads are clearly labeled non-authoritative. |
| INT-5 | NF | M | All JB2 reads include **at least one filter** (unbounded `ar-invoices`/large scans 500). | No JB2 GET is issued without a filter param; a lint/guard enforces it. |
| INT-6 | NF | M | JB2 sync **dedups MES-side** (JB2 ignores `Idempotency-Key`; dup orders observed). | Re-polling the same JB2 record does not create duplicate mirror rows. |
| INT-7 | F | M | Cross-system divergence is **logged to a reconciliation log and surfaced**, never silently overwritten; the SoR wins. | A detected mismatch produces a reconciliation entry visible to an operator/planner. |
| INT-8 | NF | S | JB2 dates handled as UTC `yyyy-MM-ddTHH:mm:ssZ`; filter syntax `?field[op]=value`; paging `take/skip`. | Adapter round-trips JB2 dates and paging correctly against the documented contract. |
| INT-9 | F | W | Write-back to JB2 (`time-tickets`, etc.) is **out of scope now**; WIP design must not preclude adding it later. | WIP event model can emit a JB2 write later without schema rework (design review sign-off). |

---

## Non-Functional, cross-cutting (NF)

| ID | Type | Pri | Requirement | AC |
|----|------|-----|-------------|----|
| NF-1 | NF | M | Stack: Python/FastAPI + Jinja server-rendered templates + SQLite + Caddy + Docker on a Linux host. | App builds and runs via `docker compose up`; Caddy fronts it. |
| NF-2 | NF | M | **Internal-network-only**; no external auth layer unless a later requirement forces it. | No public ingress; no third-party auth dependency in core phases. |
| NF-3 | NF | M | All shop-floor mutations (moves, acks, dispositions) are **append-only / audit-logged** with actor + timestamp. | Every mutation has a corresponding immutable audit record. |
| NF-4 | NF | S | Operator-facing UI works on shop-floor tablets/kiosks (touch, large targets, offline of public internet). | Station/board views pass tablet render + offline-asset checks. |
| NF-5 | NF | S | Schema changes are **migration-managed** (versioned, repeatable). | A fresh DB and an upgraded DB reach identical schema via migrations. |
| NF-6 | NF | C | Basic counts/throughput visible (no OEE). Full OEE/telemetry, genealogy, maintenance are **future stubs**. | Out-of-scope items are present only as documented stubs, not partial builds. |
