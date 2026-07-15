# DB restore — from the local nightly pg_dump stopgap

**Scope (CR-008):** full offsite backup, WAL archiving, and a proven
RPO/RTO restore drill are deferred post-pilot pending Will providing an
offsite target — N1's availability requirement is only *partially* waived on
that basis. This runbook covers the one thing that exists today: restoring
from the local nightly `pg_dump` written by `mes-backup.timer` /
`deploy/backup/pg_dump.sh` to `/var/backups/mes/` on the same server the
database runs on. **If that server's disk is lost, this backup is lost with
it** — there is no offsite copy. Treat this as a stopgap for "bad migration /
fat-fingered delete / corrupt table," not disaster recovery.

## 0. Before you start

- Confirm you actually need a full restore (wrong-row deletes can sometimes
  be fixed with a targeted `DELETE`/`UPDATE` instead — `events` is
  append-only and never has cascade deletes, migrations 0001/0005/0006/0007,
  so the audit trail alone may tell you what to undo without a restore).
- Pick the dump: `ls -lt /var/backups/mes/mes-*.sql.gz | head`. Filenames are
  `mes-<YYYYMMDD-HHMMSS>.sql.gz`, kept 14 days
  (`MES_BACKUP_KEEP_DAYS`, pruned by the same script that writes them).
- Restoring loses everything written after that dump's timestamp —
  including outbox rows not yet drained to JB2. Note the current time so you
  can compare later.

## 1. Stop services

```bash
sudo systemctl stop mes-api mes-sync mes-outbox
```

Confirm nothing is still writing:

```bash
sudo systemctl status mes-api mes-sync mes-outbox --no-pager
```

## 2. Restore the dump

`DATABASE_URL` comes from `/etc/mes/.env` (same value the app uses).

```bash
set -a; source /etc/mes/.env; set +a

# WARNING: this drops and recreates the mes database's objects from scratch.
sudo -u postgres dropdb mes
sudo -u postgres createdb mes --owner=mes
gunzip -c /var/backups/mes/mes-<stamp>.sql.gz | psql "${DATABASE_URL}"
```

If you'd rather not drop the DB (e.g. restoring into a fresh server), skip
`dropdb`/`createdb` and just `createdb` once, then pipe the dump into it.

## 3. Check migration state matches code

```bash
cd /opt/mes
sudo -u mes venv/bin/alembic current
```

Compare the reported revision against `alembic heads` on the checked-out
code:

```bash
sudo -u mes venv/bin/alembic heads
```

If the dump is behind the checked-out code's migration head (e.g. the dump
predates a later migration), bring it forward:

```bash
sudo -u mes venv/bin/alembic upgrade head
```

If the dump is *ahead* of the checked-out code (restoring onto an older
deploy), check out the matching code tag first — don't downgrade a
production schema via `alembic downgrade` as a matter of course.

## 4. Restart services

```bash
sudo systemctl start mes-api mes-sync mes-outbox
sudo systemctl status mes-api mes-sync mes-outbox --no-pager
```

## 5. Verify

```bash
curl -fsS http://127.0.0.1:8000/healthz
curl -s http://127.0.0.1:8000/health | jq
```

`/healthz` → `200`. `/health` → `status: ok` (or `degraded` only for
reasons you'd expect right after a restore, e.g. sync resources briefly
`stalled` until the next cadence tick — not `db unreachable`).

Confirm a real sync cycle completes: watch `journalctl -u mes-sync -f` for
one full pass, then re-check `/health/sync` — each resource's `last_run_at`
should be recent and `stalled: false`, and `checkpoint` should be advancing
from wherever the restored dump's `sync_checkpoints` table left off (not
from epoch — a restore does not force a resync from scratch, checkpoints
restore along with everything else).

Spot-check the outbox too: `/health/outbox` — any rows that were `pending`
or `sent` (not yet `confirmed`) as of the dump timestamp will still be
there and the drainer should pick them back up on its own.

## 6. What this does NOT cover

- Point-in-time recovery (only nightly granularity — up to ~24h of data
  loss is possible depending on when the outage/corruption happened relative
  to the last 02:00 dump).
- Offsite/geographic redundancy — the dump lives on the same host as the
  live DB.
- A tested RTO/RPO number — CR-008 explicitly leaves N1 uncertified until
  the full offsite + WAL-archiving + drill plan (P4-07) is un-deferred.

Escalate to Will if a restore is ever actually needed in anger — this
stopgap was accepted as "better than nothing," not as a validated DR plan.
