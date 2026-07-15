# JobBOSS² API Findings — Phase 0

Probed live against Atlas's tenant (`api-jb2.integrations.ecimanufacturing.com`) on
2026-07-14 using `tools/jb2probe.py`. Every claim below cites the fixture file that
recorded the request/response. All fixtures live in `tests/fixtures/jb2/`. The raw
spec is saved verbatim at `docs/openapi-jb2-2026-07-14.json` (902,558 bytes, 113 paths).

**Correction to DD §4.1 base URL note:** all `/api/v1/*` resource calls must include the
`/api/v1` prefix explicitly (`{ApiBaseUrl}/api/v1/{resource}`) — an unprefixed call to
e.g. `{ApiBaseUrl}/orders` 404s. Only `/openapi.json` sits at the bare host root. The
probe harness (`tools/jb2probe.py`) implements this correctly (`get()` prepends `/api/v1`).

---

## §2 — Time-ticket WRITE round trip (P0-R1, LIVE-VERIFIED 2026-07-15)

Resolved via authorized live writes against sandbox job **28962-07** step 10 (order 28962,
Will-designated fake order; `enteredBy: SANDBOXAPI` on every response confirms sandbox).
These findings **override** the spec-only assumptions and expose real bugs in the current
`app/outbox/payloads.py` builder — see CR-018.

1. **Header + detail MUST be created together, nested in one `POST /time-tickets`.** A
   standalone `POST /time-ticket-details` referencing a separately-created header fails
   `400 "Cannot find Time Ticket for employeeCode N and date D"` — even when the detail's
   `ticketDate` exactly matches the header's stored value. The working call is
   `POST /time-tickets` with a `timeTicketDetails: [ {...} ]` array (schema
   `TimeTicketCreate` → `TimeTicketTimeTicketDetailsCreate`). **This breaks the DD §4.4 /
   §4.5 two-step design (ensure-header-then-post-detail) and the current outbox's separate
   `time_ticket` + `time_ticket_detail` senders.**
2. **`timeStart` / `timeEnd` are `HH:MM` clock strings (max length 5), NOT ISO datetimes.**
   Posting ISO timestamps fails `400 "value for field timeStart exceeds maximum length of 5"`.
   The date comes from the header's `ticketDate`; the detail carries only wall-clock times.
   The current builder emits full ISO `yyyy-MM-ddTHH:mm:ssZ` — **wrong**.
3. **JB2 DERIVES `cycleTime` (decimal hours) from `timeStart`/`timeEnd`** — they are
   complementary, not alternatives. A 14:38→14:43 detail read back as `cycleTime: 0.083`
   (5 min), `setupTime: 0.0`. So a run session sends `timeStart`/`timeEnd`; `setupTime` is
   sent explicitly only for setup sessions. **Resolves the §4.7.2 `[VERIFY-JB2]`
   alternates-or-complements question: COMPLEMENTS (JB2 computes cycle from the clock).**
4. **`ticketDate` is timezone-normalized to the server's local date at midnight.** Sending
   `2026-07-15T00:00:00Z` stored `2026-07-14T04:00:00Z` (server is UTC−4; it takes the
   local-date and stores local-midnight-as-UTC). Send a date that resolves to the intended
   local day; the detail inherits the header's normalized date.
5. **`operationNumber` ≠ `stepNumber`.** Sending `operationNumber = stepNumber (10)` fails
   `400 "Operation Number '10' is invalid"`. It is a distinct numeric op id; **omit it** —
   `stepNumber` + `jobNumber` locate the routing operation fine.
6. **`workCenter` is a numeric id, not the string code.** Schema types it `integer`;
   routings expose it as a string (`"LASER"`). Omitted in the working call (nullable) — if
   sent, must be the numeric work-center id (needs a code→id map we don't yet build).
7. **`allowClosedJobs: true`** on the header lets labor post to non-open jobs — set it so a
   late write after a job closes doesn't 400.
8. **Detail `PATCH /time-ticket-details/{timeTicketGUID}` works** — `204 No Content`;
   `piecesFinished` 1→2 and `comments` confirmed writable on the returned GUID.

**Working minimal payload (live-confirmed):**
```json
POST /api/v1/time-tickets
{"employeeCode": 1, "ticketDate": "2026-07-15T00:00:00Z", "allowClosedJobs": true,
 "timeTicketDetails": [
   {"jobNumber": "28962-07", "stepNumber": 10, "timeStart": "14:38", "timeEnd": "14:43",
    "piecesFinished": 1, "piecesScrapped": 0, "comments": "..."}]}
```

**Test data to delete in the JB2 dashboard** (no API void): time tickets for employee
**Adam Nilson (code 1)**, ticket date **2026-07-14**, comments containing "MES P0-R1" /
"delete me" (header uniqueIDs 28637 + 28642 and their orphan siblings; detail GUID
`f93f95e4-a81d-43d1-89e7-555c9fc9b836`).

---

## §1 Auth — TTL, refresh strategy, activation matrix

**[VERIFY-JB2] token TTL and refresh cadence → RESOLVED.**

- Endpoint: `POST {AuthBaseUrl}/oauth2/api-user/token`, body `application/x-www-form-urlencoded`
  (`client_id`, `client_secret`, `grant_type=client_credentials`). JSON body → 415 (confirmed prior
  probe, unchanged).
- Response: `{access_token, token_type: "Bearer", expires_in: 3600}`. TTL is a flat **3600 s (1 hour)**.
- Two auth calls ~5 s apart returned **different** `access_token` values each time — the ECI token
  endpoint is **not** an issuer that reuses/caches a token per client; it mints a new one on every
  call. Fixture: `tests/fixtures/jb2/auth.json`.
- **Refresh strategy recommendation:** don't try to reuse a token past a single MES process's needs.
  Cache the token in-process (as the probe harness does) and treat it as good for ~3600 s from
  acquisition; refresh proactively a comfortable margin before expiry (e.g. at 3300 s) or reactively
  on a 401 (harness already does this). Because the auth endpoint mints fresh tokens for free, there
  is no benefit to a shared/persisted token cache across processes — just re-auth in-process at
  startup and on 401.

### Activation matrix

One filtered/capped `GET` per resource (`take=1`, plus a cheap filter where required). All fixtures:
`tests/fixtures/jb2/activation-<name>.json`.

| Resource | Path | Status | Note |
|---|---|---|---|
| orders | `/orders` | 200 | activated |
| order-line-items | `/order-line-items` | 200 | activated |
| order-routings | `/order-routings` | 200 | activated |
| job-materials | `/job-materials` | 200 | activated |
| job-requirements | `/job-requirements` | 200 | activated |
| estimates | `/estimates` | 200 | activated |
| work-centers | `/work-centers` | 200 | activated |
| operation-codes | `/operation-codes` | 200 | activated |
| employees | `/employees` | 200 | activated |
| reason-codes | `/reason-codes` | 200 | activated |
| document-controls | `/document-controls` | 200 | activated |
| document-histories | `/document-histories` | 200 | activated |
| time-tickets | `/time-tickets` | 200 | activated |
| time-ticket-details | `/time-ticket-details` | 200 | activated |
| attendance-tickets | `/attendance-tickets` | 200 | activated |
| eci-aps/get-schedule | `/eci-aps/get-schedule` | 200 | activated |
| shopview/get-jobs | `/shopview/get-jobs` | 200 (also observed 504) | activated, but see caveat below |
| non-conformances | `/non-conformances` | 200 | activated |

**All 18 resources the MES needs are activated for Atlas's credentials.** No 401/403/404 seen on any
of them — R4 ("APIs not activated for tenant") does not apply.

**Caveat — `shopview/get-jobs` and `eci-aps/get-schedule`:** per the spec
(`docs/openapi-jb2-2026-07-14.json`) both endpoints take **no query parameters at all** — `take=1`
was silently ignored on both. They return the **entire unfiltered dataset**.
- `shopview/get-jobs`: observed twice — one call succeeded in 29.9 s returning ~18 MB of JSON; a
  second call 504'd ("upstream request timeout") at 30.1 s.
- `eci-aps/get-schedule`: one call succeeded, returning a ~6.5 MB payload (29 top-level schedule
  task entries, each with a nested `Jobs[]` array — response shape is `{StartDateProject,
  EndDateProject, Data: [...]}`, not the usual `{Data:[...]}` convention).

Both endpoints are unpredictable-latency/heavy and should be polled infrequently (DD §4.2 already
specs 5 min cadence for both) with a generous client timeout budget (the client's blanket 10 s
timeout in §4.1/§16.3 will not survive either call — needs an endpoint-specific override) and
treated as a soft dependency (dashboard overlay, not sync-critical). Fixtures:
`tests/fixtures/jb2/activation-shopview-get-jobs.json` and
`tests/fixtures/jb2/activation-eci-aps-get-schedule.json` (both truncated to a couple of sample
records + timing/count metadata — the full multi-MB payloads were not persisted).

---

## §2 Time-tickets round trip — BLOCKED-ON-WRITE-ACCESS

P0-04 requires `POST /time-tickets` + `POST /time-ticket-details` against a dummy job to observe
header/detail relationship, JB2 UI/costing appearance, and whether `timeStart/timeEnd` vs
`setupTime/cycleTime` are alternates or complements. This task explicitly forbids POST/PATCH calls
and no dummy job number has been provided yet (IMPLEMENTATION_PLAN.md §9 lists "a dummy/test job in
the JB2 tenant" as still waiting on the human). **BLOCKED-ON-WRITE-ACCESS.**

What the live spec does confirm (read-only, no write performed):

- `TimeTicketCreate` (header): required `employeeCode` (int32), `ticketDate` (date-time); optional
  `timeTicketDetails[]`, `allowClosedJobs`.
- `TimeTicketDetailCreate`: required `employeeCode`, `jobNumber`, `ticketDate`; optional
  `stepNumber`, `operationNumber`, `workCenter` (int32 — note: **integer**, not the string
  `workCenter` used elsewhere e.g. `OrderRoutingUpdate.workCenter` is a string), `timeStart`/
  `timeEnd` (both plain `string`, no declared `date-time` format — format TBD, needs a live write
  test), `setupTime`/`cycleTime` (both `number`/double, independently nullable — spec does not
  declare them mutually exclusive with `timeStart/timeEnd`, so whether JB2 treats them as
  alternates or complements is a **behavioral** question the spec cannot answer; still
  BLOCKED-ON-WRITE-ACCESS), `piecesFinished`, `piecesScrapped`, `reasonNumber` (float, maps to
  `/reason-codes`), `shift`, `comments`.
- `PATCH /time-ticket-details/{timeTicketGUID}` uses `TimeTicketDetailUpdate` — same field set
  minus `employeeCode`/`ticketDate` (immutable after create).

---

## §3 Order-routings PATCH — writable field set

P0-05's live PATCH probe is **BLOCKED-ON-WRITE-ACCESS** (no dummy job, no write approval). However,
the writable field set question (§4.7.3) is **answerable directly from the spec without any write
call**, because the `OrderRoutingUpdate` schema is a strict allow-list:

```json
"OrderRoutingUpdate": {
  "type": "object",
  "properties": {
    "operationCode":       {"type": "string", "nullable": true},
    "employeeCode":        {"type": "string", "nullable": true},
    "estimatedEndDate":    {"type": "string", "format": "date-time", "nullable": true},
    "estimatedStartDate":  {"type": "string", "format": "date-time", "nullable": true},
    "workCenter":          {"type": "string", "nullable": true}
  },
  "additionalProperties": false
}
```

(`docs/openapi-jb2-2026-07-14.json` → `components.schemas.OrderRoutingUpdate`; identical shape at
`OrderLineItemOrderRoutingUpdate`, the nested variant used inside `OrderLineItemUpdate.orderRoutings[]`.)

**Verdict:** `actualPiecesGood`, `actualPiecesScrap`, `actualStartDate`, `actualEndDate`, and
`status` are **not present** in `OrderRoutingUpdate` at all, and `additionalProperties: false`
means the API will reject (not silently ignore) any request body containing them — this is
stronger than "derived/rejected", it's "not part of the writable contract." Only
`operationCode`, `employeeCode`, `estimatedStartDate/EndDate`, and `workCenter` are writable via
PATCH. Compare: the GET-side `OrderRouting` schema marks `actualPiecesGood/Scrap`,
`actualStartDate/EndDate`, and `status` all `"readOnly": true` — consistent with the PATCH schema
excluding them.

This **confirms** the risk R1 scenario the plan already anticipated ("`status` derived, actuals
rejected") and the fallback the DD already specifies applies as written: **rely on time-ticket
details alone for pieces-good/scrapped and start/end actuals; do not attempt to PATCH them onto
the routing.** `[VERIFY-JB2]` for the *exact* field set is **RESOLVED** by spec evidence (no live
PATCH needed to determine the schema — it's a closed allow-list). The *behavioral* question of
whether a live PATCH attempt with a forbidden field returns 400 vs silently strips the field
remains **BLOCKED-ON-WRITE-ACCESS**, but is now low-stakes since the client will simply never send
those fields.

---

## §4 `lastModDate` behavior — measured

Fixtures: `tests/fixtures/jb2/lastmod-*.json`.

- **orders**: `GET /orders?lastModDate[gte]=2020-01-01T00:00:00Z&take=5` → **200**. Sample record
  `orderNumber=10008`, `lastModDate="2023-10-25T14:30:13Z"` — **second granularity, UTC, `Z`
  suffix, no milliseconds**, matches DD §4.1's stated `yyyy-MM-ddTHH:mm:ssZ` convention. Default
  field set on `/orders` **does** include `lastModDate` without needing `fields=`.
  (`lastmod-orders-window.json`)
- **Boundary inclusivity**: re-queried with `lastModDate[gte]=<that record's exact value>` → 200,
  and the same `orderNumber=10008` record **was returned again**. **`gte` is inclusive at the exact
  timestamp** — a checkpoint re-poll using the last-seen `lastModDate` as the next `gte` will
  re-fetch that same record (expected/required for an overlap-window checkpoint strategy — dedupe
  by primary key + `content_hash`, don't assume `gte` becomes exclusive after the boundary).
  (`lastmod-orders-boundary.json`)
- **order-line-items**: `GET /order-line-items?lastModDate[gte]=...&take=5` → **200**, filter is
  accepted and doesn't error. **However**, unlike `/orders`, the **default field set on
  `/order-line-items` does NOT include `lastModDate`** — the field is silently absent from
  returned rows unless requested explicitly via `?fields=...,lastModDate`. Confirmed by re-querying
  with `fields=jobNumber,itemNumber,lastModDate` → the field appears, same second-granularity UTC
  format (`"2023-10-25T14:30:13Z"`). **Action for P1-03/P1-05: every sync-worker GET against
  order-line-items (and, by extension, any resource) must explicitly list `lastModDate` in
  `fields=` or the incremental-sync checkpoint will silently never advance/detect changes** — this
  is a sharper, more concrete restatement of DD §4.1's general "request explicit `fields=` lists"
  note. (`lastmod-order-line-items.json`, `lastmod-order-line-items-fields.json`)
- **order-routings**: required an `orderNumber` filter to avoid an unfiltered-GET 500, per this
  task's standing rule — used `orderNumber[eq]=10008` (from the orders result). `GET
  /order-routings?lastModDate[gte]=2020-01-01T00:00:00Z&orderNumber[eq]=10008&take=5` → **200**.
  Default field set here **does** include `lastModDate` (no `fields=` needed), same UTC
  second-granularity format (`"2024-07-09T12:47:25Z"`). (`lastmod-order-routings.json`)
- **`revisedDate[null]` 500**: not re-tested (per this task's standing instruction — known to 500,
  don't repeat it).

**Checkpoint algorithm recommendation:** poll each resource with `lastModDate[gte]=<last checkpoint>`,
always requesting `lastModDate` explicitly in `fields=` for resources (like order-line-items) whose
default field set omits it. Treat `gte` as inclusive-at-boundary — dedupe re-fetched rows by
primary key (`orderNumber`/`uniqueID`) + `content_hash`, don't shrink the window to exclusive. A
small fixed overlap (e.g. re-poll from `last_checkpoint - 60s`) is unnecessary extra insurance given
`gte` is confirmed inclusive at the exact value, but costs nothing against clock-skew edge cases —
recommend keeping a modest overlap anyway (e.g. 30–60 s) as defense against any server-side
timestamp truncation not observed in this sample.

---

## §5 Rate limits — observations

No `X-RateLimit-*`, `Retry-After`, or any other rate-limit-shaped response header was observed
across the ~30 live requests made in this probe session (2 auth calls, 1 spec fetch, 18 activation
GETs, 1 duplicate non-conformances refetch, 5 lastMod GETs, 1 reason-codes page, 2 direct
shopview/get-jobs calls). **No rate-limit headers observed across ~30 calls.** No 429 responses
seen either. The only non-200 statuses encountered were a transient 500 on the very first
`/openapi.json` attempt (succeeded on retry) and a 504 on the second `shopview/get-jobs` attempt
(that endpoint's own unfiltered-payload latency, not a rate limit).

**Client throttle recommendation (R2):** absent documented limits, the harness's politeness floor
of **≥0.5 s between requests** (used throughout this probe run) produced zero pushback. Recommend
Phase 1's `app/jb2/client.py` adopt this as the floor global throttle, with the existing
circuit-breaker (§4.1) as the safety net for any future 429/5xx storm rather than a hand-tuned
rate number — there is no evidence-based number higher than "be polite" to set.

---

## §6 `user_*` field writability on `OrderLineItemUpdate` — BLOCKED-ON-WRITE-ACCESS (spec resolved)

Live PATCH to confirm actual write acceptance is **BLOCKED-ON-WRITE-ACCESS** (no write approval,
no safe test line item designated). However, the schema itself answers the *candidate field*
question directly:

```json
"OrderLineItemUpdate": {
  "properties": {
    "user_Currency1": {"type": "number", "format": "double", "nullable": true},
    "user_Currency2": {"type": "number", "format": "double", "nullable": true},
    "user_Date1":     {"type": "string", "format": "date-time", "nullable": true},
    "user_Date2":     {"type": "string", "format": "date-time", "nullable": true},
    "user_Memo1":     {"type": "string", "nullable": true},
    "user_Number1":   {"type": "number", "format": "double", "nullable": true},
    "user_Number2":   {"type": "number", "format": "double", "nullable": true},
    "user_Number3":   {"type": "number", "format": "double", "nullable": true},
    "user_Number4":   {"type": "number", "format": "double", "nullable": true},
    "user_Text1":     {"type": "string", "nullable": true},
    "user_Text2":     {"type": "string", "nullable": true},
    "user_Text3":     {"type": "string", "nullable": true},
    "user_Text4":     {"type": "string", "nullable": true},
    "orderRoutings":  {"type": "array", "items": {"$ref": "#/components/schemas/OrderLineItemOrderRoutingUpdate"}, "nullable": true}
  },
  "additionalProperties": false
}
```

Candidate for "store MES work-order URL for cross-navigation" (§4.7.6): **`user_Text1`–`user_Text4`**
(plain nullable strings, no format/length constraint declared) are the best fit — a URL is just a
string, and there are 4 generic text slots available (assuming none are already claimed by JB2
tenant configuration for another purpose — that tenant-config question is itself
BLOCKED-ON-WRITE-ACCESS / needs a read of current values on a real line item, not schema). No
`user_URL`-typed field exists; use `user_Text1` (or whichever generic text field ECI/Atlas confirms
is unclaimed) unless a live test reveals field-length limits too short for a URL. **Live-write
verdict on whether the API actually accepts a value here (vs. silently rejecting per some
undocumented tenant customization) remains BLOCKED-ON-WRITE-ACCESS.**

---

## §7 Reason codes

Full dump: `GET /reason-codes` with `take=200&skip=0` → 21 rows on page 0 (< `take`, so pagination
stopped after 1 page — no page 2/3 needed). Fixture: `tests/fixtures/jb2/reason-codes.json`
(`{"Data": [...]}`, 21 records, matches the `{"Data":[...]}` envelope convention).

Sample rows (see fixture for all 21):

| reasonCode | reasonCodeID | description | active | createRMA |
|---|---|---|---|---|
| BAD DIMNSION | 21 | Dimension out of specification | true | true |
| BURRS | 20 | Burrs | true | true |
| COSMETIC | 19 | Scratch/Cosmetic issue | true | true |
| DAMAGED MATL | 2 | Damaged material | true | true |
| FAILED TEST | 26 | Good Heat, Failed Hardness | true | true |

Mapping note for `failure_codes.jb2_reason_number` (DD §4.4, P2-08): map on **`reasonCodeID`**
(the integer used as `reasonNumber` on `time-ticket-details`), not `uniqueID` or the string
`reasonCode` — the time-ticket-detail write field is `reasonNumber` (float) and lines up with
`reasonCodeID`, confirmed by field name/type match in `TimeTicketDetailCreate`/`Update` schemas.
Every MES failure code should map to one of these 21 `reasonCodeID`s or explicitly opt out (per
P2-08's acceptance criterion).

---

## `[VERIFY-JB2]` tag resolution summary

| Tag (DD location) | Status |
|---|---|
| Token TTL and refresh cadence (§4.1) | **RESOLVED** — 3600 s TTL, fresh token per auth call, no server-side reuse; recommend in-process cache + proactive refresh (§1 above). |
| API activation for all needed resources (§4.1) | **RESOLVED** — all 18 resources activated (200), see activation matrix §1. |
| Exact writable field set of `OrderRoutingUpdate` (§4.4/§4.7.3) | **RESOLVED by spec** — `operationCode`, `employeeCode`, `estimatedStartDate/EndDate`, `workCenter` only; actuals/status excluded and rejected by `additionalProperties:false`. Live-PATCH behavioral confirmation is **BLOCKED-ON-WRITE-ACCESS** but low-stakes (client will never send the excluded fields). |
| `timeStart/timeEnd` vs `setupTime/cycleTime` — alternates or complements (§4.4/§4.7.2) | **BLOCKED-ON-WRITE-ACCESS** — spec allows both independently, doesn't resolve intended usage; needs P0-04 live round trip on a dummy job. |
| Time-ticket header/detail relationship + JB2 UI/costing appearance (§4.7.2) | **BLOCKED-ON-WRITE-ACCESS** — needs dummy job + write approval (P0-04). |
| `user_*` field writability on `OrderLineItemUpdate` (§4.7.6) | **RESOLVED (candidates)** — `user_Text1`–`4` are the field-type match; **BLOCKED-ON-WRITE-ACCESS** for whether a write actually sticks / isn't already claimed by tenant config. |
| Rate limits (§4.7.4) | **RESOLVED (by absence)** — no headers/429s observed in ~30 calls; adopt ≥0.5s politeness floor + existing breaker, no evidence for a stricter number. |
| `lastModDate` filter behavior — granularity/timezone/boundary (§4.1/§4.7.5) | **RESOLVED** — second-granularity UTC, `gte` inclusive at exact boundary, default field sets vary by resource (order-line-items omits it by default; orders/order-routings include it). See §4. |
| Reason-code list → `failure_codes.jb2_reason_number` seed (§4.7.7) | **RESOLVED** — 21 codes fetched, map via `reasonCodeID`. See §7. |

Remaining BLOCKED items (§2, §3's live-PATCH confirmation, §6's live-write confirmation) all share
the same root blocker: **no dummy/test job number has been provided and this task is explicitly
read-only.** Per IMPLEMENTATION_PLAN.md §9, that job number is still "waiting on the human." Once
provided, P0-04/P0-05/P0-07's write portion can close out these items without further architectural
research — the spec has already answered every question the spec *can* answer.

## Files produced

- `tools/jb2probe.py` — probe harness (auth/spec/activation/lastmod/reason-codes/endpoint subcommands)
- `docs/openapi-jb2-2026-07-14.json` — verbatim OpenAPI spec snapshot
- `docs/jb2-api-findings.md` — this file
- `tests/fixtures/jb2/*.json` — 26 fixture files (auth, 18 activation probes, 5 lastmod probes,
  reason-codes dump + its page-0 raw capture)
