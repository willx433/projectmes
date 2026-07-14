#!/usr/bin/env bash
# Atlas MES deploy script (P1-11, DD §16.5). Idempotent: safe to re-run.
#
# Usage:
#   deploy.sh              # deploy whatever is on the current branch (git pull)
#   deploy.sh <git-tag>    # checkout a specific tag instead of pulling
#
# Run as the `mes` user from /opt/mes (the repo checkout).
set -euo pipefail

REPO_DIR="${MES_REPO_DIR:-/opt/mes}"
VENV_DIR="${MES_VENV_DIR:-${REPO_DIR}/venv}"
HEALTH_URL="${MES_HEALTH_URL:-http://127.0.0.1:8000/healthz}"
SERVICES=(mes-api mes-sync mes-outbox)

cd "${REPO_DIR}"

TAG="${1:-}"
if [[ -n "${TAG}" ]]; then
    echo "==> fetching and checking out tag ${TAG}"
    git fetch --tags
    git checkout "${TAG}"
else
    echo "==> pulling latest on current branch"
    git pull --ff-only
fi

if [[ ! -d "${VENV_DIR}" ]]; then
    echo "==> creating venv at ${VENV_DIR}"
    python3.12 -m venv "${VENV_DIR}"
fi

echo "==> syncing dependencies"
"${VENV_DIR}/bin/pip" install --upgrade pip
"${VENV_DIR}/bin/pip" install -r requirements.txt

echo "==> running migrations"
"${VENV_DIR}/bin/alembic" upgrade head

echo "==> restarting services"
for svc in "${SERVICES[@]}"; do
    sudo systemctl restart "${svc}.service"
done

echo "==> waiting for API to come up"
for i in $(seq 1 15); do
    if curl -fsS "${HEALTH_URL}" > /dev/null 2>&1; then
        echo "==> healthy: ${HEALTH_URL}"
        exit 0
    fi
    sleep 2
done

echo "!! deploy finished but ${HEALTH_URL} never became healthy" >&2
exit 1
