# Atlas MES — "Make Ready" (ProjectAMR)

Manufacturing Execution System for Atlas Gun Works. Internal-network-only.
Python/FastAPI + Jinja (server-rendered) + SQLite + Caddy + Docker.

Governing docs (source of truth): `requirements.md`, `implementation-plan.md`,
`change-requests.md`, `library.md`, `research-notes.md`.

## Scope (built, Phases 0–5)
- **Routing & work-instruction master** (versioned, supersede, instruction acks)
- **JB2 read-only mirror** (jobs + applied routings, deduped, reconciliation log) — *needs creds*
- **WIP move engine** — append-only event log; state derived by replay
- **Scheduling & dispatch** — planner board + operator kiosk station views
- **Quality / NCR** — flag → hold → disposition (rework/scrap/use-as-is/RTV/regrade) → WIP action

Out of scope (stubs): OEE/telemetry, full genealogy, maintenance, JB2 write-back.

## Run (Docker — recommended)
```bash
docker compose up --build      # app behind Caddy on http://localhost
```
Boots with demo data (no JB2 needed). Health: `GET /healthz`.

## Run (local dev)
```bash
pip install -r requirements.txt
uvicorn app.main:app --reload --port 5055
# open http://localhost:5055
```

## Test
```bash
python test_smoke.py           # asserts WIP replay, hold guard, disposition, overlay
```

## Enable live JB2 sync
Set these (compose env or `.env`), then click **Sync from JB2** on the Jobs page:
```
AMR_JB2_BASE_URL=https://api-jb2.integrations.ecimanufacturing.com:443
AMR_JB2_TOKEN=<bearer token from JB2 Management API OAuth2>
AMR_JB2_JOBS_FILTER=status[ne]=Closed     # INT-5: every read must be filtered
```
Until set, the app runs on demo-seeded jobs (marked `source=demo`, non-authoritative).
The adapter's job/applied-routing field mapping is best-effort against the documented
contract and is flagged TODO in `app/services/jb2_adapter.py` — validate against the
tenant's `shopview/get-jobs` payload before production.

## Data model in one line
JB2 owns jobs + applied routings (read-only mirror). MES owns routing master, work
instructions, WIP (append-only `wip_events`), and quality/NCR. See `library.md`.
