# Execution State-Machine Contract (P3-01)

**Author:** Fable, 2026-07-14. **Status:** binding for all Phase 3 implementation.
Sources: DD §5 (state machines), §6.3–6.8 (workflows), §17 (edge cases), §14 (auth),
CR-007 (serials pre-exist), CR-009 (manual-entry fallback), CR-010 (time tickets only).

**One validator.** Every floor endpoint calls `app/domain/statemachine.py`; the tablet
UI is thin (DD §12.1). No endpoint mutates state except through a transition function
listed here. Every transition appends an `events` row (§9.10) in the same transaction.

## 1. Entity states

```
WorkOrder: pending_sync → ready → in_progress → completed
                     ↘ blocked_no_instructions   ↘ cancelled | cancel_requested
                     (routing_drift is a flag-status, returns to prior on reconcile)

Unit:   queued → at_station → in_transit → at_station → … → done
        any-active → scrapped (terminal; lead + failure record required)
        at_station → rework(op K ≤ current) [first_pass=false forever, rework_count++]

PlanOperation (per unit tracked via UnitOpState, see §2):
        pending → active → done | skipped(lead)   [rework reopens: done → pending]

WorkSession: open → paused ⇄ open → closed(finished | clocked_out | auto_closed)

SubstepExecution: pending → in_progress → done | failed | skipped(lead)
```

## 2. Judgment call — per-unit operation state

DD §10's `plan_operations.status` is per work order, but units advance independently
(qty>1). **Resolution:** `plan_operations.status` stays as the aggregate roll-up;
per-unit progress lives in `step_executions`/`substep_executions` keyed by
`(plan_operation_id, unit_id)` exactly as DD §10 defines, plus `units.current_plan_op_id`
as the unit's position pointer. An operation is *done for a unit* when a
`step_executions` row exists per step with status done and a finish event was emitted.
`plan_operations.status = done` when done for all non-scrapped units. No new table.

## 3. Scan resolution table (POST /scan, §6.3)

Input: `payload` (string), `station_id` (kiosk token), optional `operator_session`.
Payload prefixes: `OP:` badge, `BOX:` box. Manual entry (CR-009) submits the same
payloads through the same endpoint — no separate path.

| # | Precondition | Result code | Effect / events |
|---|---|---|---|
| S1 | `OP:` payload, valid active operator | `operator_session_opened` | Open/refresh session at this station; close any session at another station (one-station rule §14). `auth_events` row. |
| S2 | `OP:` payload, unknown/revoked | `badge_rejected` | `auth_events(badge_fail)`. No state change. |
| S3 | `BOX:` no operator session at station | `operator_required` | Record `scans(result=rejected)`. |
| S4 | `BOX:` unknown payload | `unknown_box` | `scans(unknown_box)`. |
| S5 | `BOX:` known box, no active unit assignment | `unbound_box` | `scans(unbound_box)`; UI offers kit-up flow (lead or operator per config). |
| S6 | `BOX:` bound; unit's next op maps to THIS station's work center | `accepted` | Close transit hop if unit was in_transit (`transits.arrived_at`, seconds computed). Unit → at_station. Open WorkSession (kind per §6: first_pass/rework by unit flag; `setup` if first session at op and operator marks setup). `scans(accepted)`. |
| S7 | `BOX:` bound; WRONG station for next op | `wrong_station` | `scans(wrong_station)`. UI shows destination. Lead badge → override path O2. |
| S8 | `BOX:` bound; unit scrapped/done | `unit_terminal` | `scans(rejected)`, message. |
| S9 | `BOX:` bound; another operator has an open session on this unit at another station | `unit_busy_elsewhere` | Rejected + who/where. (Two operators SAME station = allowed, §17.7 — both sessions open.) |
| S10 | duplicate scan (same box+station+operator, session already open) | `already_active` | No-op (idempotent), no duplicate session. |

## 4. Override matrix (second badge, §14 / P7)

| # | Action | Required role | Records |
|---|---|---|---|
| O1 | Skip required substep | lead | `substep_executions.skipped` + skip_reason + skip_authorized_by; `events` |
| O2 | Work out of sequence (wrong station accept) | lead | `scans.override_by`; session opens against the out-of-seq op |
| O3 | Accept out-of-tolerance measurement (use-as-is deviation) | lead or quality | measurement row `disposition=use_as_is`, disposition_by |
| O4 | Scrap unit | lead | failure + scrap_event(authorized_by); replacement unit if remake |
| O5 | Send back to op K (rework) | lead | failure(disposition=rework_to_op, rework_to_op=K) |
| O6 | Confirm auto-closed session before JB2 post (§17.8) | lead | session flagged lead_confirmed; outbox enqueue happens HERE, not at auto-close |
| O7 | Plan regeneration / qty↓ disposition / cancel_requested resolution | lead | work-order events |
| O8 | Signoff substep | role named by substep.signoff_role | substep done with authorizer identity |

Override = second badge scan in the dialog; the authorizer's operator id lands on the
record. A lead's own work session is unaffected.

## 5. Substep / step / operation rules (§6.4–6.6)

- Substep completion requires: open WorkSession on (unit, op) by the acting operator;
  substep state pending/in_progress; all payload validation per type (measurement value
  numeric + spec present; photo attachment id; material record fields; signoff role badge).
- **Measurement out of tolerance** → substep BLOCKED in `failed`-pending-disposition:
  one of O3 (use-as-is), rework-here (substep resets to pending, rework time accrues),
  O5 (send back), O4 (scrap). No disposition → step cannot complete (§6.4).
- Step done when all required substeps done/skipped(O1). Operation finishable when all
  steps done — server-checked, `finish` idempotent (double-finish = no-op, §17.9).
- **Finish operation** (per unit): close session(finished); emit events; unit →
  in_transit(dest = next op) or done (last op — serial required if
  `product.require_serial_before_done`, CR-007: serial is *entered*, never generated);
  enqueue outbox `time_ticket_detail` (CR-010: time tickets ONLY) with idempotency key
  `wo:{wo_id}:unit:{unit_no}:op:{seq}:session:{session_id}`.
- Rework to op K: all UnitOp progress for ops K..current reset to pending for that unit
  (executions kept, marked superseded — history is append-only); unit.first_pass=false
  permanently; rework_count++; unit → in_transit(dest = op K).

## 6. Sessions & time (§6.7, §17.8)

- Pause reasons enum: waiting_material, machine_down, break, pulled_to_other_job, other.
- Clock-out mid-op: session closes(clocked_out), partial time; unit stays at_station;
  any qualified operator may resume (new session).
- Idle timeout (config, default 10 min no interaction): session auto-pauses; lead queue
  notified. Auto-close (config, default 60 min paused): session closed(auto_closed);
  **no JB2 post until O6 lead confirmation** — keeps garbage time out of costing.
- Session time = closed_at − opened_at − Σ(pauses). JB2 detail carries timeStart/timeEnd
  or setup/cycle per P0-04 findings (BLOCKED item; payload builder isolates this choice
  in one function pending P0-R1).

## 7. Idempotency & concurrency

- All floor POSTs accept a client `request_id`; replays return the original result
  (server-side table or event-lookup dedup) — §17.9.
- State reads/writes for one unit serialize via row-level lock on `units` (SELECT FOR
  UPDATE on Postgres; SQLite tests rely on single-writer).
- Box reassignment (§17.6): lead action; closes old box_assignments row (released_at),
  creates new; history preserved.

## 8. Kit-up (§6.1.5)

Preconditions: WO ready/in_progress, box active + unassigned, unit without box.
Effects: box_assignments row; serial entry field offered (CR-007); WO → in_progress on
first kit-up; unit stays queued until first accepted scan. Kit-up allowed by operator
role (config `KITUP_REQUIRES_LEAD`, default false).

## 9. Event verbs (append-only, §9.10)

`scan.accepted|rejected`, `session.opened|paused|resumed|closed`, `substep.done|failed|
skipped`, `measurement.recorded`, `failure.recorded`, `disposition.applied`,
`unit.moved|reworked|scrapped|done`, `box.assigned|released`, `wo.status_changed`,
`outbox.enqueued`, `auth.login|logout|badge_fail|override`. Actor id on every one.

## 10. Property-test obligations (P3-14)

From this table: (a) no sequence of API calls reaches an unlisted transition;
(b) S10/finish idempotency; (c) O1–O8 unreachable without the named role;
(d) out-of-tolerance without disposition blocks step completion; (e) rework resets
K..N and first_pass never returns to true; (f) auto_closed sessions produce no outbox
row before O6; (g) every transition emits exactly one events row.
