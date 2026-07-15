# JB2 / cloud outage response

Atlas MES is local-first (DD §12.1, R3/R4): the shop floor keeps running
against the local Postgres database even when JobBoss2 (JB2) or the
internet link to it is down. This runbook covers what still works, what
queues up, how to confirm recovery, and when to escalate.

## 1. What still works during the outage

Nothing on the tablet changes for operators. Badge-in, box scan, working
steps, measurements, dispositions, Fail, Finish operation, pause/resume,
clock-out all write to the local DB directly and do not call JB2 inline.
See `docs/runbooks/operator-quickstart.md` for the floor-side flow; the only
floor-visible outage symptom is the full-screen "MES offline" page, and that
only appears if the **local** API itself is unreachable (network to the
server down, or the server process down) — not merely JB2 being down.

## 2. What queues up

Every finish-operation write generates outbox rows (`jb2_outbox`, kinds
`time_ticket` / `time_ticket_detail`) instead of calling JB2 synchronously.
A separate outbox drainer process (`mes-outbox.service`) attempts these on
its own loop and retries with backoff; it is what actually talks to JB2, not
the floor request path.

Read-side sync (orders, routings, materials, etc., `mes-sync.service`) also
keeps trying on its own per-resource cadence and just accumulates stale data
while JB2 is down — no floor impact beyond "queue/board data may be a bit
old."

**Write path is time-tickets only (CR-010):** JB2's `order-routings` PATCH
can't carry actuals or status, so all finish/writeback data rides
`time_ticket` + `time_ticket_detail` rows only. Nothing else needs a queued
JB2 write.

## 3. Check the breaker / outbox state

```bash
curl -s https://<mes-host>/health          | jq
curl -s https://<mes-host>/health/jb2      | jq
curl -s https://<mes-host>/health/outbox   | jq
curl -s https://<mes-host>/health/sync     | jq
```

- `/health/jb2` → `breaker_state` is one of `closed` (healthy), `open`
  (tripped after consecutive failures, calls fail fast without hitting the
  network), `half-open` (probing one call to see if JB2 recovered).
- `/health/outbox` → `counts` by status (`pending`, `sent`, `confirmed`,
  `failed`) plus `oldest_pending_age_s` and the list of `parked` (failed)
  rows with `last_error`.
- `/health` rolls all of the above into one `status: ok|degraded` verdict
  with a `reasons` list (breaker open, sync stalled, outbox rows parked,
  outbox backlog >10 min).
- Or just open `/admin/health` for the same data as a page, with the parked
  outbox rows and a per-row **Replay** button.

Breaker state is per-process — the API's own view only, since sync/outbox
run as separate systemd units (documented limitation, Phase 1 gate).
Checking `/health/jb2` from the API doesn't tell you the outbox drainer's
own breaker state; if you need that specifically, check the outbox
service's logs (`journalctl -u mes-outbox -e`) for breaker transitions.

## 4. Confirm recovery

1. Breaker closes: `/health/jb2` → `breaker_state: closed`, `last_success_at`
   moving forward.
2. Outbox drains: `/health/outbox` → `pending`/`failed` counts trending to
   zero, `oldest_pending_age_s` shrinking. Parked (`failed`) rows do **not**
   auto-retry — see step 5.
3. Sync resumes: `/health/sync` → each resource's `stalled: false` and
   `age_s` back under its cadence; `checkpoint` continuing to advance (sync
   resumes from its last per-resource checkpoint, not from scratch — no data
   is re-pulled or skipped).
4. `/health` overall → `status: ok`, empty `reasons`.

## 5. Manual replay of parked (failed) outbox rows

A row only parks as `failed` after either a permanent error (4xx from JB2 —
won't succeed on retry without a data fix) or `MAX_ATTEMPTS` (8) transient
retries exhausted. Parked rows do not retry on their own.

On `/admin/health`, each parked row's table entry has a **Replay** button
(`POST /admin/outbox/{outbox_id}/replay`) — resets `status` back to
`pending`, clears `attempts`/`last_error`, and the drainer's next pass picks
it up again. Fix the underlying cause first if the `last_error` indicates a
permanent problem (bad mapping, rejected payload) — replaying without fixing
the cause just re-parks it.

Note the FIFO ordering guarantee: outbox rows are drained per
`work_order_id` stream, oldest first, and **a parked row blocks every later
row in its own work order's stream** (but never blocks other work orders).
So one bad row can silently stall a whole work order's writebacks until it's
replayed or fixed.

## 6. When to escalate vs. wait it out

Wait it out (transient, no action needed beyond monitoring):
- Breaker `half-open`/`open` with a recent `last_failure_at` and normal
  network conditions — the breaker will self-probe and close on its own.
- Outbox rows retrying with `next_attempt_at` set, `attempts` < 8.

Escalate immediately:
- `last_error` on a parked row mentions auth/token/credential rejection
  (401/403 from JB2) — this is not something a replay fixes; JB2 API
  credentials likely expired or were rotated. Check `/etc/mes/.env`'s
  `JobBoss2__*` values against current credentials.
- `parked` count is growing and `last_error` shows the same 4xx repeatedly
  across many rows — a systemic mapping/schema problem (e.g. a
  `MappingException` surfaced on `/admin/health`), not a one-off.
- Outbox backlog age keeps growing for well over the drainer's backoff
  ceiling (15 min) with the breaker reporting `closed` — the drainer process
  itself may be down (`systemctl status mes-outbox`, `journalctl -u
  mes-outbox -e`).
