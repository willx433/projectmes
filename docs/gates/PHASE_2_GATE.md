# Phase 2 Gate — Library + plans + PDFs

**Executed by:** Fable, 2026-07-14.
**Verdict: PASS.**

## Acceptance criteria (DD §19 Phase 2)

| Criterion | Result | Evidence |
|---|---|---|
| Create Apollo instruction set → JB2 Apollo order auto-produces plan + PDF with correct routing binding | PASS — **executed on real PostgreSQL 16.4** | `tools/seed_demo.py` against a live local Postgres: Apollo product + 3 published sets → work order `ready`, 3-op frozen plan (all bound, none blocked), build-guide PDF v1 (valid `%PDF-1.7`, sha256 recorded in `plan_pdfs`). |
| Freeze immutability | PASS | `test_order_to_plan.py::test_freeze_invariant_survives_new_published_version` — publish v2, frozen_content byte-hash unchanged. |
| Versioning/publish, builder UI, media, badges/QR | PASS | 194-test suite: lifecycle transitions, fork-on-edit, two-person publish flag, builder UI flows, sanitizer, annotation, badge/box sheets (`%PDF` asserted; WeasyPrint 69 works natively — no skips). |
| Binding rules (§6.2 four rungs) + conditional content | PASS | `test_binding.py` (all rungs incl. fuzzy 0.75 boundary + blocked), `test_conditions.py` (never-raises at floor time); conditional 9mm substep filtered in generated plan. |
| §17.4/17.5 reconciliation | PASS | `test_reconcile.py`: qty↑ adds units, qty↓ flags, due-date propagates, cancel vs cancel_requested, routing_drift w/ frozen bytes untouched, idempotent re-reconcile. |

## Prior carried exception — CLEARED

**Live Postgres verification** done this gate via portable PostgreSQL 16.4 binaries
(user-space, no sudo; libxml2 symlink shim): all 6 migrations upgrade AND downgrade
cleanly; outbox→work_orders FK enforced (bogus uuid rejected); full demo flow ran
against it. Note: the shimmed local instance is a *test* instance — prod still
installs distro Postgres per `docs/runbooks/deploy.md`.

## Flake check

Reported ordering-flake in `test_pdf.py` not reproduced: 3 consecutive full-suite
runs, 194/194 green each. Watching in CI; no action.

## Defects

- Minor: `tools/seed_demo.py` passed raw `postgresql://` to SQLAlchemy (psycopg2
  assumption). Fixed in-gate (scheme coercion identical to migrations/env.py).

## Carried exceptions (unchanged)

- CR-012 write-path items (time-ticket semantics, `user_Text*` write) → still a
  **Gate 3 precondition**, blocked on Will (dummy job number + write authorization).

## Remediation

- P3-00 (from ESC-002, approved): populate `jb2_order_line_items.jb2_order_id` in the
  sync extractor; replace the order_closed payload scan. Scheduled at Phase 3 entry.

**Phase 3 (floor execution) may begin.**
