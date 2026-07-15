# Station enrollment — new tablet, operators, badges

For an admin setting up a new station kiosk (tablet) on the shop floor.
Office/admin pages currently have no auth gate on the LAN (CR-017, accepted
for v1 pilot) — do this from a trusted machine on the shop network.

## 1. Create the station (mints a one-time kiosk token)

Go to `/admin/stations` and fill in the **Enroll a station** form:

- **Name** — required, e.g. `Deburr 3`.
- **Work center code** (optional) — the JB2 work center code this station
  maps to (joins `jb2_work_centers.code`). Set this now if you know it; the
  idle-screen queue only shows units whose next operation's work center
  matches this field, so a station with no code shows an empty queue.
- **Location** (optional).

This is `POST /admin/stations`. On submit you're redirected back to the same
page with a yellow banner showing the new station's **kiosk token** —
**it is shown exactly once, right here, and is never displayed again.**
Copy it now.

```
Station <name> enrolled. Kiosk token (shown once — copy it into the
tablet's enrollment step now, it will not be shown again): <token>
```

If you lose it, there's no "reveal token" screen — create a new station row
(or ask an engineer to mint a fresh token directly against the `stations`
row; there's no UI for that either).

## 2. Point the tablet at the enrollment screen

On the tablet's Chromium kiosk, set the home/kiosk URL to:

```
https://<mes-host>/station/enroll
```

That page asks for the kiosk token. Paste the token from step 1, submit.
This is a **one-time step per tablet**: on success the server sets a
long-lived cookie (~10 years) scoped to `/station`, so the kiosk survives
reboots without re-enrolling. After this, re-point the kiosk browser's home
URL to plain `/station` — that's the screen operators badge into every day.

If the token is wrong or the station was deactivated, the enroll form
redirects back to itself with an error and keeps whatever you typed in the
field so you can fix a typo.

## 3. Create operators + print badges

Go to `/admin/operators`:

- **New operator** form: name, one or more roles (checkboxes — `operator`,
  `lead`, `quality`, `admin`, whatever `OPERATOR_ROLES` currently lists),
  and an optional PIN (numeric fallback login if a badge scanner is down or
  the badge card is lost).
- Submitting (`POST /admin/operators`) mints a stable `OP:{uuid}` badge
  payload for that operator and redirects back with a banner showing the
  raw badge payload once, for reference.
- Click **Print badge sheet** (`GET /admin/print/badges`) to generate a PDF
  with one row per active operator (name + badge QR). This can be re-run any
  time — reprinting shows the *same* badge code per operator (it doesn't
  invalidate previously printed cards), so keep the sheet as the reference
  copy, not the token banner.
- **Revoke badge (reissue)** on an operator's row mints a fresh `OP:{uuid}`
  for that person (old physical card stops working) — use this for a lost
  badge. Their PIN is untouched.

## 4. Confirm the station maps to the right JB2 work center

Back on `/admin/stations`, check the **Work center** column for the new row.
If it's blank or wrong, there's no edit form yet — recreate the station with
the correct `work_center_code` (a second enrollment token isn't a problem;
just re-enroll the tablet with the new token) or have an engineer patch the
`stations` row directly.

## 5. Smoke test

On the tablet: confirm `/station` shows the station name (not a redirect
back to `/station/enroll`), scan an operator's badge, confirm the name
appears next to the station name, and confirm a unit whose next operation
matches this work center shows up in the idle queue.
