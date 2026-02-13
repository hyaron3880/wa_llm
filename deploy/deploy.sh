#!/usr/bin/env bash
set -euo pipefail

# ──────────────────────────────────────────────────────────────
# deploy.sh — Pull latest images and restart wa_llm on VPS
#
# Run from your Mac:
#   ./deploy/deploy.sh <VPS_IP>
#
# Or from the VPS itself (no argument needed):
#   /opt/wa_llm/deploy.sh
# ──────────────────────────────────────────────────────────────

REMOTE_DIR="/opt/wa_llm"
SSH_USER="root"
COMPOSE_FILE="docker-compose.prod.yml"

# ── Detect if running locally or on VPS ──────────────────────

run_deploy() {
    local dir="$1"

    echo "==> Pulling latest images..."
    docker compose -f "${dir}/${COMPOSE_FILE}" pull

    echo "==> Restarting services..."
    docker compose -f "${dir}/${COMPOSE_FILE}" up -d

    echo ""
    echo "==> Waiting for services to start..."
    sleep 5

    echo ""
    echo "--- Container status ---"
    docker compose -f "${dir}/${COMPOSE_FILE}" ps

    # Verify web-server is running
    if docker compose -f "${dir}/${COMPOSE_FILE}" ps web-server 2>/dev/null | grep -q "Up\|running"; then
        echo ""
        echo "--- Recent web-server logs ---"
        docker compose -f "${dir}/${COMPOSE_FILE}" logs --tail=20 web-server
        echo ""
        echo "==> Deploy complete!"
    else
        echo ""
        echo "ERROR: web-server container is not running"
        docker compose -f "${dir}/${COMPOSE_FILE}" logs --tail=50 web-server
        exit 1
    fi
}

# If running on the VPS (no argument, and the dir exists locally)
if [[ $# -eq 0 ]] && [[ -d "${REMOTE_DIR}" ]] && [[ -f "${REMOTE_DIR}/${COMPOSE_FILE}" ]]; then
    echo "==> Running deploy locally on VPS..."
    run_deploy "${REMOTE_DIR}"
    exit 0
fi

# Running from Mac — need VPS_IP
if [[ $# -lt 1 ]]; then
    echo "Usage: $0 <VPS_IP>"
    echo "Example: $0 168.119.x.x"
    exit 1
fi

VPS_IP="$1"
SSH_TARGET="${SSH_USER}@${VPS_IP}"

echo "==> Deploying to ${SSH_TARGET}..."

# If compose files were updated locally, sync them first
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

if [[ -f "${PROJECT_DIR}/${COMPOSE_FILE}" ]]; then
    echo "==> Syncing compose files..."
    scp -q "${PROJECT_DIR}/docker-compose.prod.yml" "${SSH_TARGET}:${REMOTE_DIR}/docker-compose.prod.yml"
    scp -q "${PROJECT_DIR}/docker-compose.base.yml" "${SSH_TARGET}:${REMOTE_DIR}/docker-compose.base.yml"
fi

ssh "${SSH_TARGET}" bash <<'REMOTE_SCRIPT'
set -euo pipefail

cd /opt/wa_llm

echo "==> Pulling latest images..."
docker compose -f docker-compose.prod.yml pull

echo "==> Restarting services..."
docker compose -f docker-compose.prod.yml up -d

echo ""
echo "==> Waiting for services to start..."
sleep 5

echo ""
echo "--- Container status ---"
docker compose -f docker-compose.prod.yml ps

# Verify web-server is running
if docker compose -f docker-compose.prod.yml ps web-server 2>/dev/null | grep -q "Up\|running"; then
    echo ""
    echo "--- Recent web-server logs ---"
    docker compose -f docker-compose.prod.yml logs --tail=20 web-server
    echo ""
    echo "==> Deploy complete!"
else
    echo ""
    echo "ERROR: web-server container is not running"
    docker compose -f docker-compose.prod.yml logs --tail=50 web-server
    exit 1
fi
REMOTE_SCRIPT
