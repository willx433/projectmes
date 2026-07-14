# Phase 0 Gate — JB2 behavioral verification

**Executed by:** Fable (planning architect), 2026-07-14.
**Verdict: CONDITIONAL PASS** (see exception; deviation logged as CR-012).

## Acceptance criteria (DD §19 Phase 0)

| Criterion | Result | Evidence |
|---|---|---|
| Every `[VERIFY-JB2]` resolved | **9 of 9 addressed: 6 fully RESOLVED, 3 BLOCKED-ON-WRITE-ACCESS** (all share one root cause: no dummy job number authorized for live writes) | `docs/jb2-api-findings.md` resolution table |
| `docs/jb2-api-findings.md` produced | PASS | file present, every claim cites a fixture |
| `openapi.json` snapshot saved | PASS | `docs/openapi-jb2-2026-07-14.json` (113 paths) |
| Fixtures recorded for fake-JB2 server | PASS | 26 files + `tests/fixtures/jb2/INDEX.md`; all valid JSON |
| Fixtures replay in CI | DEFERRED to P1-04/P1-12 (fake-JB2 server is a Phase 1 artifact) | — |

## Fable verification performed

- `OrderRoutingUpdate` schema claim independently re-derived from the spec snapshot
  (`additionalProperties:false`; only planning fields) — **confirmed** → CR-010.
- Secret scrub independently verified: client id, client secret, and `Bearer ` grepped
  across all fixtures and docs — zero hits.
- Legacy app still runs post-restructure (`legacy/test_smoke.py` passes).
- `.env` absent from git history (`git ls-files`: only `.env.example`).

## Defects found

None in delivered work. One environment gap: dev machine has no Docker or PostgreSQL
(and no passwordless sudo) — P1-02 migrations can be authored but not executed locally
until Will provides a Postgres (see plan §9).

## Exception (the "conditional" in the verdict)

The three write-path items (time-ticket round-trip semantics §2, live-PATCH behavioral
confirmation §3, `user_Text*` write test §6) are **blocked on Will**: a dummy JB2 job
number plus explicit authorization for live writes. Analysis:

- No Phase 1 task consumes write semantics (mirrors, sync, outbox *mechanics* only).
- No Phase 2 task consumes them (library, plans, PDFs).
- First consumer is **P3-10** (outbox payload mapping).

Therefore the write items are re-gated as a **precondition of Gate 3**, not of Gates 1–2.
P0-04 executes the moment the job number arrives, regardless of what phase is active.

## Remediation tasks

- **P0-R1** (Sonnet, blocked-on-human): execute P0-04 write round trip + §3/§6 live
  confirmations when the dummy job number + write authorization arrive; update findings;
  re-run this gate's exception check. **Blocks Gate 3.**

**Phase 1 may begin.**
