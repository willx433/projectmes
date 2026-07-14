# Atlas MES — "Make Ready" (ProjectAMR)

Manufacturing Execution System for Atlas Gun Works, integrated with
JobBOSS2 (JB2). New build in progress per `IMPLEMENTATION_PLAN.md`.

## Docs map

- `documentation/` — the contract. `MES_Design_Document.md` wins on technical
  matters; `MES_Product_Design_Document.md` wins on philosophy;
  `JB2_vs_MES_Capability_Split.md` defines what JB2 owns vs. what MES owns.
- `IMPLEMENTATION_PLAN.md` — phased work breakdown, task tiering, gates.
- `docs/` — living project docs: `jb2-api-findings.md`, `ESCALATIONS.md`,
  `CHANGE_REQUESTS.md`, `gates/`, `runbooks/`.
- `legacy/` — the superseded first-generation build, frozen for reference
  (see `legacy/README.md`).

## Dev setup

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env      # fill in JobBoss2 client id/secret
uvicorn app.main:app --reload
```

Health check: `GET /healthz`.

## Test

```bash
pytest
ruff check .
```
