# Operator quickstart — Atlas MES station tablet

For the shop-floor operator working a station kiosk tablet (`/station`). One
screen at a time, in order.

## 1. Badge in

Scan your badge at the scan bar (always-focused input at the top of every
station screen). No badge in yet? The idle screen shows a yellow banner
**"Scan your badge to begin."**

If the scanner won't read the badge, type the badge code (or your PIN, if
your lead set one up) into the **Manual entry** box next to the scan input
and press Submit — every scan input on this tablet has that fallback
(hardware scanners aren't guaranteed yet, CR-009).

Once accepted, your name appears next to the station name at the top of
every screen. **Only one station at a time**: badging in here automatically
closes your session at whatever other station you were last logged into.

## 2. Pick a box off the queue, scan it

The idle screen (`/station`) lists units queued for this station's work
center, oldest due date first. Scan the box's barcode (or type it manually,
same fallback as badges).

What can happen:
- **Accepted** — takes you straight to the unit's scan-result screen.
- **"Scan your badge before scanning a box"** — you scanned a box before
  badging in. Badge in first.
- **"Wrong station"** — this box's next operation belongs at a different
  work center. The screen names the target work center and operation title.
  Either walk the box there, or (lead only) scan a lead badge in the
  override box on that screen to accept it here anyway.
- **Unknown / unbound box code** — box isn't recognized or isn't linked to a
  unit yet. Get a lead.

## 3. Scan-result screen

Shows the product, unit number, serial (if entered), order, due date, and a
progress line (`step X / Y complete`). The unit card's left border tells you
what kind of pass this is:
- **Blue border** — first pass (`unit-card-firstpass`).
- **Red border** — rework, with a rework count badge (`unit-card-rework`).

Press **Start operation** (or **Resume operation** if you already have a
session open) to go to the execution screen.

## 4. Work the steps

Left rail lists every step in the operation; the current step is highlighted,
completed steps show a checkmark. Each step's substeps appear as one control
row apiece:

| Substep type | What you do |
|---|---|
| action | **Mark done** button |
| inspection | **Pass** or **Fail** (with a reason) |
| measurement | **Enter reading** opens a numeric keypad |
| photo | pick/take a photo, **Upload photo** |
| material | type part number, qty used/scrapped, lot, UoM |
| signoff | requires a lead/quality badge scan in that row |

Required substeps that you can't do (broken tool, wrong gauge, etc.) can be
**Skip**ped, but skipping always needs a lead badge scan.

### Out-of-tolerance measurement

If a reading falls outside the spec's tolerance, the row turns into an error
banner showing the reading vs. nominal/tolerance, plus a disposition form:

- **Use as-is (deviation)** — lead or quality badge
- **Rework here** — no badge needed
- **Send back** — pick the target operation, lead badge
- **Scrap** — lead badge

Pick a disposition, a failure code, scan the authorizing badge if the option
needs one, and submit.

### Something wrong with the whole operation, not just one substep

Use the **Fail** button in the footer (not a per-substep control) — same
disposition choices (rework here / send back / scrap / use-as-is), plus a
failure code and a free-text narrative. Scrap and send-back leave the unit
and return you to the idle screen; rework-here and use-as-is keep you on the
operation.

### Footer controls

- **Pause** — pick a reason (waiting on material, machine down, break,
  pulled to other job, other).
- **Resume**
- **Clock out** — leaves the unit at this station for the next operator to
  pick up; confirms before submitting.
- **Finish operation** — disabled until every required substep is done
  (button shows how many are left). Submitting moves the unit to `in_transit`
  toward its next operation, or marks it `done` if this was the last one.

## 5. Move the box

After **Finish operation** succeeds you're returned to the idle screen. Move
the physical box to wherever its next operation happens — the queue at that
station's tablet will show it once its work center matches.

## If the tablet shows "MES offline"

A red full-screen banner means the last request failed outright (network
down, server down). **Stop working the tablet and switch to the paper build
guide for whatever unit you're on.** The screen retries the health check
every 5 seconds and returns you to `/station` automatically once the
connection is back — you don't need to do anything but wait or keep working
on paper.

Once the connection is restored, **do not try to re-enter what you did on
paper from memory yourself** — a lead uses the backfill screen
(`/admin/backfill`) to enter the paper record properly, timestamped and
tagged `[BACKFILLED]`, once the outage is over. Hand your paper traveler to
your lead.
