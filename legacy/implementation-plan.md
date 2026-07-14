# Implementation Plan — Atlas MES ("Make Ready" / ProjectAMR)

> **Build status (2026-06-30): Phases 0–5 implemented and verified** (smoke test +
> HTTP end-to-end pass). Phase 2 JB2 adapter is built against the documented contract but
> needs live tenant credentials to sync — runs on demo seed until then. Phase 6 = stubs only.


Phased build. **Vertical slices** — each later phase ships one working end-to-end domain,
not a horizontal layer. Every phase ends with a **STOP-AND-REPORT** checkpoint: no phase
begins until the prior phase is reported and approved. Requirement IDs reference
`requirements.md`. Consult `library.md` before starting any task; update it after every
component change. Any deviation → `change-requests.md`.

**Build order rationale:** definition (we have a seed) → mirror the things JB2 owns →
make WIP move → render dispatch/station over WIP → layer quality on WIP. Each slice is
demoable on its own.

---

## Phase 0 — Research + Foundation

- **Goal:** Ground the design in MES references; stand up a runnable, empty app skeleton.
- **Scope:** Research (ISA-95, MESA-11, lifecycle/NCR/work-instruction patterns) ✅ done.
  Foundation: FastAPI app, Jinja templating wired (reuse seed `engineer/base.html`
  pattern), SQLite store + migration mechanism, Docker + Caddy, seed dashboard served
  from real (empty) `/api/*` endpoints, health check.
- **Dependencies:** none.
- **Deliverables:** `research-notes.md` ✅; the 4 governing docs ✅ (this set); app
  skeleton booting in Docker; empty-state dashboard rendering.
- **Exit / acceptance:** `docker compose up` boots; Caddy fronts the app; dashboard renders
  with zero data and no errors; health endpoint green; migrations create an empty schema.
  Satisfies NF-1, NF-2, NF-5 (skeleton level).
- **STOP-AND-REPORT:** Research summary + architecture direction reported & **approved**
  (done). Governing docs drafted → **report for review before any code** (current
  checkpoint). Foundation code is the *first code* and waits for that approval.

---

## Phase 1 — Routing & Work-Instruction Master (Definition)

- **Goal:** MES owns and can author the routing/operation master + work instructions.
- **Scope:** Models, Operations, Routings (**versioned**), Line Balances — built up from
  the seed. Work-instruction steps (text + local image) attached to operations.
  Revision model so a version supersedes without mutating prior versions.
- **Requirements:** WI-1, WI-2, WI-3, WI-7, WI-8; INT-3 (MES as SoR here).
- **Dependencies:** Phase 0 foundation.
- **Deliverables:** routing/operation/model/line-balance CRUD views + APIs; work-instruction
  editor; version supersede logic; library.md entries for each component.
- **Exit / acceptance:** can author a routing version with ordered operations + instruction
  steps; publishing creates a new version leaving prior versions readable; line balance ties
  to a routing version. Dashboard counts reflect real data.
- **STOP-AND-REPORT:** demo the authoring flow; confirm the version/revision model before
  WIP depends on it.

---

## Phase 2 — JB2 Read Layer: Jobs/Orders + Applied Routings Mirror

- **Goal:** Mirror the things JB2 owns into MES, read-only, deduped, reconciled.
- **Scope:** JB2 adapter (filtered polls → SQLite cache); jobs/orders mirror (jobs via
  `shopview/get-jobs`); per-order applied routing read (`order-routings`); reconciliation
  log; MES-side dedup. Overlay hook so Phase-1 work instructions can attach to JB2 ops.
- **Requirements:** INT-1, INT-2, INT-5, INT-6, INT-7, INT-8; INT-4 (Airtable non-auth).
- **Dependencies:** Phase 1 (instructions to overlay); JB2 facts in memory
  (`reference-jb2-api-endpoint-catalog`, `project-jb2-discovery-2026-05-10`).
- **Deliverables:** `jobboss2` integration adapter; mirror tables + sync job; reconciliation
  log view; guard enforcing "every JB2 read is filtered" (INT-5).
- **Exit / acceptance:** MES job list mirrors a filtered JB2 query; an order shows its JB2
  applied routing with MES instructions overlaid; re-poll creates no duplicates; an injected
  mismatch lands in the reconciliation log; no unfiltered JB2 GET is possible.
- **STOP-AND-REPORT:** demo the mirror + reconciliation against the JB2 sandbox; confirm
  the applied-routing overlay shape before WIP consumes it.

---

## Phase 3 — Production Tracking & WIP (the move engine)

- **Goal:** Make WIP move; everything downstream reads this event log.
- **Scope:** Release-to-floor; operation instances with intraoperation steps
  `Queue→Run→To-move→(Reject/Scrap)`; **append-only move events**; WIP state as a derived
  read-model; job lifecycle derived from events; actor attribution.
- **Requirements:** PT-1..PT-8; NF-3 (append-only audit).
- **Dependencies:** Phase 1 (routing), Phase 2 (jobs + applied routing to release against).
- **Deliverables:** move-event store; move API; WIP read-model service; release action;
  audit view; library.md entries.
- **Exit / acceptance:** release a job → WIP at op1/Queue; move qty op→op; current WIP
  reconstructable purely by replaying events; lifecycle state derives correctly; every
  move has actor + timestamp; views < 1 s on representative volume.
- **STOP-AND-REPORT:** demo a full move sequence + event-replay equivalence before building
  views/quality on top.

---

## Phase 4 — Scheduling & Dispatch (read-models over WIP)

- **Goal:** Planner and operator views over the WIP state from Phase 3.
- **Scope:** Dispatch board (all work centers, sequenced); operator station view (one work
  center + active op instructions + move/complete); configurable sequencing rule
  (due-date then priority); optional manual re-prioritization.
- **Requirements:** SD-1..SD-6; NF-4 (tablet usability).
- **Dependencies:** Phase 3 (WIP state), Phase 1 (instructions for station view).
- **Deliverables:** dispatch board view; station view; sequencing service; (optional) pin/bump.
- **Exit / acceptance:** board shows each WC's queue in sorted order and reorders on moves;
  operator advances a job from the station view; instructions render in context; station view
  passes tablet-width check.
- **STOP-AND-REPORT:** demo planner board + operator station on a tablet width before quality.

---

## Phase 5 — Quality / Inspection & Non-Conformance

- **Goal:** Flag → hold → disposition → downstream action, layered on WIP.
- **Scope:** NCR entity linked to a WIP op instance; hold that blocks moves; disposition
  state machine (rework/scrap/use-as-is/RTV/regrade) driving WIP move events; configurable
  single-approver MRB with full audit; (could) inspection checkpoints auto-creating NCRs.
- **Requirements:** QA-1..QA-7; NF-3.
- **Dependencies:** Phase 3 (WIP hold/move integration), Phase 4 (operator entry point).
- **Deliverables:** NCR model + workflow; hold integration with move engine; disposition
  actions; NCR audit/immutability; library.md entries.
- **Exit / acceptance:** flag a part at an op → held qty cannot move; disposition records
  actor+reason+timestamp; rework returns qty to a step, scrap removes it; closed NCRs are
  immutable (corrections create linked successors).
- **STOP-AND-REPORT:** demo a flag→hold→disposition cycle for each disposition path.

---

## Phase 6 — Future Stubs (documented, not built)

- **Goal:** Mark deferred scope so it isn't half-built.
- **Scope (stubs only):** OEE/machine telemetry (PA), full genealogy/traceability (PTG),
  maintenance (MM), and **JB2 write-back** (`time-tickets`) — INT-9 design hook validated.
- **Requirements:** NF-6; INT-9.
- **Exit / acceptance:** each is documented as a stub with an upgrade path; WIP design review
  confirms write-back can be added without schema rework. No partial implementations.
- **STOP-AND-REPORT:** present the deferred-scope register; decide next engagement.

---

## Cross-phase working rules

- Stop and report after every phase; do not run ahead.
- `library.md` consulted before each task, updated after each component change.
- Integration-native over connector-glue; surface assumptions, don't guess a SoR.
- Ambiguity on a load-bearing detail → ask, don't invent (propose-then-confirm).
