# Atlas MES — Product Design Document

**Purpose:** Define the design philosophy behind the Atlas MES: a purpose-built shop-floor execution system that fills the space between JobBOSS² (ERP) and a traditional, heavyweight MES.
**Companions:** *MES_Design_Document.md* (technical specification) · *JB2_vs_MES_Capability_Split* (capability analysis)
**Status:** v1.0 — July 2026

---

## 1. The Problem

Atlas runs its business in JobBOSS². Orders, routings, costing, purchasing, and invoicing all live there, and they should — JB2 is a competent system of record for a job shop's commercial life. But walk to a bench on the shop floor and JB2 goes silent. It knows a job exists and which operation it's nominally at. It does not know which physical box the parts are in, what the machinist is supposed to do next, what dimension they just cut, whether that dimension was in tolerance, or that this is the second time this slide has been back to Op 20.

The traditional answer is a commercial MES. That answer fails a shop like Atlas in the opposite direction: traditional MES platforms assume they *are* the manufacturing system — they want to own routings, scheduling, labor, and inventory, which puts them in a permanent turf war with the ERP. They are priced and staffed for factories with an IT department, they take a year to deploy, and most of their surface area (finite scheduling engines, machine integration, multi-plant genealogy) solves problems Atlas doesn't have.

So there is a gap. On one side, an ERP that stops at the routing operation. On the other, MES suites that duplicate the ERP and drown a 20-person shop in configuration. The Atlas MES is designed to live precisely in that gap — and nowhere else.

## 2. Product Thesis

**Build the execution layer JobBOSS² is missing, and nothing JobBOSS² already has.**

Every feature proposal gets tested against this thesis. If JB2's API can record a fact and JB2 uses that fact downstream, the MES pushes the data to JB2 and does not compete with it. If JB2 cannot represent something — a substep, a measurement, a box scan, a rework loop — the MES owns it completely. There is no third category; nothing is stored in both places with divided authority.

This produces a clean division that we verified against JB2's actual API surface:

JB2 owns **money and orders**: customers, quotes, orders, routings, scheduling, costing, invoicing. The MES owns **execution and evidence**: instructions, scans, measurements, time at the substep level, failures, rework, and the physical whereabouts of every unit. The seam between them is JB2's REST API — orders and routings flow down automatically; labor and quantities flow back automatically. A person should never re-type into one system what the other already knows.

## 3. Design Principles

**P1 — The floor never waits on the cloud.** JB2 is a cloud product; the internet fails; ECI has maintenance windows. Every operator-facing action works against the local server and local database. Writes to JB2 queue and drain in the background. A JB2 outage is an admin-page banner, not a stopped shop.

**P2 — The scan is the interface.** An operator's entire interaction begins with two scans: their badge and the box. No logins to remember, no job numbers to type, no menus to navigate. The QR code on the traveler — the same routing QR already in use — is the key that unlocks location tracking, time tracking, and the correct work instructions simultaneously. If a workflow requires an operator to type an identifier, the workflow is wrong.

**P3 — Instructions are the product.** The heart of the system is not tracking — it's that every alteration and process a product goes through has an exact, versioned, illustrated, step-by-step instruction presented at the moment of work. Tracking is what we get almost for free *because* the operator is already working through the instructions on screen. This ordering matters: a tracking system operators tolerate captures worse data than an instruction system operators rely on.

**P4 — Record everything, interpret later.** Storage is cheap; a missed data point is gone forever. Every scan, timestamp, measurement, pause, override, failure, and material delta is recorded append-only with actor identity — even when we don't yet have a report that uses it. First-pass yield, bottleneck analysis, estimate tuning, and warranty traceability all fall out of data captured as a side effect of normal work, not as extra clerical effort.

**P5 — Truth at the unit, summaries upward.** JB2 thinks in quantities per operation. The MES thinks in individual serialized units in physical boxes. The MES keeps unit-level truth (which unit failed, which was reworked, which measurements belong to which serial) and rolls up honest aggregates for JB2. Never the reverse — an aggregate is derivable from units; units are not recoverable from an aggregate.

**P6 — Version everything a person authors.** Instructions change. Work in flight must not change under an operator's hands. Instruction sets are versioned with a publish gate; released work orders execute against a frozen copy forever. Every record notes which version the operator actually saw. "What did we tell the operator to do on March 3rd?" always has an exact answer.

**P7 — Escalate to a human, not around them.** Out-of-tolerance measurements, out-of-sequence scans, scrap decisions, and skipped steps are lead decisions, invoked by a second badge scan at the bench. The system never silently blocks work and never silently permits deviation — it records who decided, in the moment, with the context on screen.

**P8 — Boring technology, one box.** One Linux server, Caddy, one Python service, PostgreSQL. No microservices, no message brokers, no Kubernetes, nothing that demands a platform team. The entire system must be restorable from a nightly backup by one competent person following a runbook. Ambition goes into the data model, not the infrastructure.

**P9 — Rework is a first-class citizen.** In a shop that fits and finishes precision parts, rework is normal work, not an exception code. The unit model carries first-pass status permanently, rework loops re-open earlier operations cleanly, rework time is tracked separately from first-pass time, and the dashboard shows the distinction at a glance. A system that hides rework produces flattering numbers and no improvement.

**P10 — Adoption over enforcement.** Tablets with gloves-on touch targets, sub-second response on scans, a paper PDF fallback that mirrors the screens, and nothing that punishes an operator for honesty (a fail button that triggers paperwork gets pressed less than one that triggers help). The measure of success is that the floor prefers using it to not using it.

## 4. What Fills the Gap (and What Doesn't)

The gap between JB2 and a traditional MES, concretely:

| Layer | JB2 (ERP) | Traditional MES | Atlas MES |
|---|---|---|---|
| Orders, costing, invoicing | ✅ owns | duplicates it | reads it, never touches money |
| Routing definitions | ✅ owns | wants to own it | imports it 1:1, never edits |
| Scheduling | ✅ owns (APS) | competing engine | displays JB2's dates, adds floor reality |
| Labor & quantities per operation | ✅ accepts via API | owns separately | captures, posts to JB2 |
| Step-by-step instructions | ❌ none | ✅ heavyweight, generic | ✅ core product, product-grouped library |
| Substep / measurement / tolerance capture | ❌ none | ✅ (SPC modules) | ✅ built-in, right-sized |
| Physical unit & box tracking | ❌ none | ✅ (complex genealogy) | ✅ one scan, unit-level |
| First-pass vs. rework analytics | ❌ none | partial | ✅ first-class |
| Machine integration, multi-plant, finite scheduling | ❌ | ✅ (the expensive part) | ✂️ deliberately excluded |

The excluded column is as much a design decision as the included one. Machine integration, SPC engines, finite scheduling, and multi-site support are explicitly out of scope — not because they're worthless, but because they're where MES projects go to die, and the data model is designed so they can be layered on later without rework.

## 5. Who It's For

**The operator** at a station: scans a badge and a box, sees exactly what to do with photos and tolerances, taps through substeps, types measurements on a keypad, and hits finish. Their entire clerical burden is the work itself.

**The lead** on the floor: sees every box's location and age live, gets pulled in by badge scan for overrides and scrap calls, dispositions rework, and reconciles order changes — with the authority trail recorded automatically.

**The librarian / engineer** in the office: builds and versions the instruction library grouped by product, with images, tolerances, and conditional content per variant; publishes deliberately; sees actual-vs-estimated times feed back into better instructions.

**The owner / manager**: watches the pipeline board — every unit, first-pass green or rework purple, aging and bottlenecks visible — and trusts that JB2's costing and invoicing stayed accurate without anyone double-entering a thing.

## 6. Experience Pillars

**Instant context.** Scan → full context in under a second: what this is, where it's been, what's next. The dashboard answers "where is everything?" without a click.

**One source of truth per fact.** Order facts come from JB2 and are read-only on the floor. Execution facts come from the floor and are read-only in reports. No screen ever asks a human to reconcile two systems.

**Evidence, not memory.** Photos, measurements, timestamps, and signoffs accumulate into a per-serial build record — the shop's institutional memory, queryable years later for a warranty claim or a process question.

**Progressive disclosure.** An operator sees one step at a time. A lead sees a station. A manager sees the pipeline. The same data model serves all three without any of them seeing the others' complexity.

## 7. Success Criteria

The product succeeds when, after one product line runs through it for a quarter:

1. Zero manual data entry into JB2 for labor or quantities on that line (measured: 100% of time tickets originate from MES write-backs).
2. Every unit's location is answerable in one lookup, and every completed unit has a full build record (instructions version, measurements, operators, times).
3. First-pass yield is *known* — with a number and a Pareto of failure causes — rather than felt.
4. Operators choose the tablet over the paper guide when both are available.
5. Instruction estimates converge toward actuals, visibly, release over release.

## 8. Philosophy of the Roadmap

Build the seam first (JB2 sync), then the library, then the floor, then the glass (dashboard) — in that order, because each layer is only as honest as the one beneath it. Ship to one product line, let the floor bend the design, then widen. Resist every feature that either duplicates JB2 or belongs to the traditional-MES weight class; the product's value is its restraint. The gap is the product.

---

*Technical decisions, schemas, API contracts, and phase plans live in MES_Design_Document.md. This document governs why; that one governs how.*
