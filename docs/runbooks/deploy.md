# Deploy runbook — Atlas MES (cold start on Will's Ubuntu server)

Target: Ubuntu 24.04 LTS, systemd (CR-002), Caddy TLS (`tls internal`), Postgres 16.
Follow in order. Every command is copy-pasteable as-is (adjust the repo URL/host
if they differ).

## 1. System packages

```bash
sudo apt update
sudo apt install -y python3.12 python3.12-venv python3-pip git curl \
    libpango-1.0-0 libpangocairo-1.0-0 libgdk-pixbuf2.0-0 libffi-dev \
    libcairo2 shared-mime-info fonts-liberation
```

(The `libpango`/`libcairo`/`libgdk-pixbuf` set is for WeasyPrint, needed
starting Phase 2 PDF generation — installing now avoids a second round trip.)

## 2. PostgreSQL 16

```bash
sudo apt install -y postgresql-16
sudo systemctl enable --now postgresql

sudo -u postgres psql -c "CREATE USER mes WITH PASSWORD 'CHANGE_ME';"
sudo -u postgres psql -c "CREATE DATABASE mes OWNER mes;"
```

Replace `CHANGE_ME` with a real password and use the same value in
`/etc/mes/.env`'s `DATABASE_URL` below.

## 3. `mes` service user

```bash
sudo useradd --system --create-home --home-dir /opt/mes --shell /usr/sbin/nologin mes
sudo mkdir -p /var/lib/mes/artifacts /var/lib/mes/static /var/backups/mes
sudo chown -R mes:mes /var/lib/mes /var/backups/mes
```

## 4. Clone the repo

```bash
sudo -u mes git clone https://github.com/willx433/projectmes.git /opt/mes
cd /opt/mes
```

## 5. `/etc/mes/.env`

```bash
sudo mkdir -p /etc/mes
sudo cp /opt/mes/.env.example /etc/mes/.env
sudo chown mes:mes /etc/mes/.env
sudo chmod 0600 /etc/mes/.env
sudo -u mes -e /etc/mes/.env   # fill in JobBoss2__* creds, DATABASE_URL, MES_SECRET_KEY, ARTIFACT_DIR
```

`DATABASE_URL` should be `postgresql://mes:CHANGE_ME@localhost:5432/mes`
(same password as step 2). `ARTIFACT_DIR` should be `/var/lib/mes/artifacts`.
Generate `MES_SECRET_KEY` with `python3 -c "import secrets; print(secrets.token_hex(32))"`.

## 6. First deploy

```bash
sudo chmod +x /opt/mes/deploy/deploy.sh /opt/mes/deploy/backup/pg_dump.sh
sudo -u mes /opt/mes/deploy/deploy.sh
```

This creates the venv, installs deps, runs `alembic upgrade head`. The
service-restart step will fail the first time (units aren't installed yet) —
that's expected; continue to step 7 and re-run `deploy.sh` afterward.

## 7. systemd units

```bash
sudo cp /opt/mes/deploy/systemd/mes-api.service \
        /opt/mes/deploy/systemd/mes-sync.service \
        /opt/mes/deploy/systemd/mes-outbox.service \
        /opt/mes/deploy/systemd/mes-backup.service \
        /opt/mes/deploy/systemd/mes-backup.timer \
        /etc/systemd/system/

sudo systemctl daemon-reload
sudo systemctl enable --now mes-api mes-sync mes-outbox
sudo systemctl enable --now mes-backup.timer
```

## 8. Caddy

```bash
sudo apt install -y debian-keyring debian-archive-keyring apt-transport-https
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
    | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
    | sudo tee /etc/apt/sources.list.d/caddy-stable.list
sudo apt update
sudo apt install -y caddy

sudo cp /opt/mes/deploy/Caddyfile /etc/caddy/Caddyfile
sudo systemctl reload caddy
```

If the hostname `mes.atlas.internal` in the Caddyfile isn't resolvable on
the LAN, add it to `/etc/hosts` on client machines or point real DNS at it.
`tls internal` makes Caddy mint its own local CA cert — trust that CA on
client devices, or swap the line for a public ACME hostname later.

## 9. Re-run deploy.sh (services now exist)

```bash
sudo -u mes /opt/mes/deploy/deploy.sh
```

## 10. Verify

```bash
curl -k https://mes.atlas.internal/api/v1/... # smoke-test a real route once Phase 1+ endpoints exist
curl -fsS http://127.0.0.1:8000/healthz
sudo systemctl status mes-api mes-sync mes-outbox --no-pager
sudo systemctl list-timers mes-backup.timer
```

`/healthz` should return `200`. If it doesn't, check `journalctl -u mes-api -e`.

## 11. Upgrades later

```bash
sudo -u mes /opt/mes/deploy/deploy.sh          # latest on current branch
sudo -u mes /opt/mes/deploy/deploy.sh v1.2.3    # pinned tag
```

## Backups (CR-008 stopgap)

`mes-backup.timer` fires nightly at 02:00 (+ up to 5 min jitter),
runs `deploy/backup/pg_dump.sh`, writes gzipped dumps to
`/var/backups/mes/`, and prunes anything older than 14 days. This is a
**local-only stopgap** — full offsite backup + WAL archiving + restore
drills are deferred (CR-008, P4-07) until Will provides an offsite target.
Restore is manual for now: `gunzip -c /var/backups/mes/mes-<stamp>.sql.gz | psql "$DATABASE_URL"`.
