#!/usr/bin/env bash
set -euo pipefail

# ──────────────────────────────────────────────────────────────
# setup.sh — Provision a fresh VPS for wa_llm bot
#
# Run from your Mac:
#   ./deploy/setup.sh <VPS_IP>
#
# Prerequisites:
#   - SSH key added to the VPS (root access)
#   - .env file in the project root with your API keys
# ──────────────────────────────────────────────────────────────

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
REMOTE_DIR="/opt/wa_llm"
SSH_USER="root"

# ── Argument validation ──────────────────────────────────────

if [[ $# -lt 1 ]]; then
    echo "Usage: $0 <VPS_IP>"
    echo "Example: $0 168.119.x.x"
    exit 1
fi

VPS_IP="$1"
SSH_TARGET="${SSH_USER}@${VPS_IP}"

# ── Pre-flight checks ───────────────────────────────────────

echo "==> Pre-flight checks"

if [[ ! -f "${PROJECT_DIR}/.env" ]]; then
    echo "ERROR: .env file not found in ${PROJECT_DIR}"
    echo "Copy .env.example to .env and fill in your API keys first."
    exit 1
fi

if [[ ! -f "${PROJECT_DIR}/docker-compose.prod.yml" ]]; then
    echo "ERROR: docker-compose.prod.yml not found in ${PROJECT_DIR}"
    exit 1
fi

if [[ ! -f "${PROJECT_DIR}/docker-compose.base.yml" ]]; then
    echo "ERROR: docker-compose.base.yml not found in ${PROJECT_DIR}"
    exit 1
fi

# Validate .env has real API keys (not placeholders)
if grep -qE "(OPENROUTER_API_KEY|VOYAGE_API_KEY)=your" "${PROJECT_DIR}/.env"; then
    echo "ERROR: .env contains placeholder API keys. Fill them in first."
    exit 1
fi

# Test SSH connectivity
echo "==> Testing SSH connection to ${SSH_TARGET}..."
if ! ssh -o ConnectTimeout=10 -o BatchMode=yes "${SSH_TARGET}" "echo ok" >/dev/null 2>&1; then
    echo "ERROR: Cannot SSH into ${SSH_TARGET}"
    echo "Make sure:"
    echo "  1. The server is running"
    echo "  2. Your SSH key is added to the server"
    echo "  3. The IP address is correct"
    exit 1
fi

echo "==> SSH connection OK"

# ── Step 1: Check disk space and install Docker on VPS ───────

echo "==> Checking disk space on VPS..."

ssh "${SSH_TARGET}" bash <<'REMOTE_SCRIPT'
set -euo pipefail

AVAILABLE_GB=$(df -BG / | awk 'NR==2 {print $4}' | sed 's/G//')
if [[ ${AVAILABLE_GB} -lt 10 ]]; then
    echo "ERROR: Insufficient disk space (${AVAILABLE_GB}GB available, need at least 10GB)"
    exit 1
fi
echo "Disk space OK: ${AVAILABLE_GB}GB available"
REMOTE_SCRIPT

echo "==> Installing Docker on VPS..."

ssh "${SSH_TARGET}" bash <<'REMOTE_SCRIPT'
set -euo pipefail

export DEBIAN_FRONTEND=noninteractive

echo "--- Updating packages ---"
apt-get update -qq
apt-get upgrade -y -qq

echo "--- Installing prerequisites ---"
apt-get install -y -qq \
    ca-certificates \
    curl \
    gnupg \
    lsb-release \
    ufw

echo "--- Installing Docker ---"
if ! command -v docker &>/dev/null; then
    install -m 0755 -d /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
        -o /etc/apt/keyrings/docker.asc
    chmod a+r /etc/apt/keyrings/docker.asc

    echo \
      "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
      https://download.docker.com/linux/ubuntu \
      $(. /etc/os-release && echo "${VERSION_CODENAME}") stable" \
      > /etc/apt/sources.list.d/docker.list

    apt-get update -qq
    apt-get install -y -qq \
        docker-ce \
        docker-ce-cli \
        containerd.io \
        docker-buildx-plugin \
        docker-compose-plugin

    systemctl enable docker
    systemctl start docker
    echo "Docker installed successfully"
else
    echo "Docker already installed: $(docker --version)"
fi

echo "--- Docker Compose version ---"
docker compose version

echo "--- Configuring Docker log rotation ---"
mkdir -p /etc/docker
cat > /etc/docker/daemon.json <<'EOF'
{
  "log-driver": "json-file",
  "log-opts": {
    "max-size": "10m",
    "max-file": "3"
  }
}
EOF

systemctl restart docker
echo "Docker log rotation configured (10MB x 3 files per container)"
REMOTE_SCRIPT

echo "==> Docker installed"

# ── Step 2: Configure firewall ───────────────────────────────

echo "==> Configuring firewall..."

ssh "${SSH_TARGET}" bash <<'REMOTE_SCRIPT'
set -euo pipefail

echo "--- Setting up UFW ---"
ufw default deny incoming
ufw default allow outgoing

# Rate-limit SSH to prevent brute force (max 6 connections in 30 seconds)
ufw limit OpenSSH comment "SSH with rate limiting"

echo "y" | ufw enable
ufw status verbose
REMOTE_SCRIPT

echo "==> Firewall configured (SSH only, rate-limited)"

# ── Step 3: Create project directory and copy files ──────────

echo "==> Creating project directory on VPS..."

ssh "${SSH_TARGET}" "mkdir -p ${REMOTE_DIR}"

echo "==> Copying compose files..."

scp "${PROJECT_DIR}/docker-compose.prod.yml" "${SSH_TARGET}:${REMOTE_DIR}/docker-compose.prod.yml"
scp "${PROJECT_DIR}/docker-compose.base.yml" "${SSH_TARGET}:${REMOTE_DIR}/docker-compose.base.yml"

echo "==> Copying environment file as .env.prod..."

scp "${PROJECT_DIR}/.env" "${SSH_TARGET}:${REMOTE_DIR}/.env.prod"

# Secure the env file
ssh "${SSH_TARGET}" "chmod 600 ${REMOTE_DIR}/.env.prod"

echo "==> Files copied"

# ── Step 4: Create convenience docker-compose wrapper ────────

echo "==> Setting up Docker Compose project..."

ssh "${SSH_TARGET}" bash <<'REMOTE_SCRIPT'
set -euo pipefail

cd /opt/wa_llm

# Create a docker-compose.yml symlink so 'docker compose' works naturally
if [[ ! -f docker-compose.yml ]]; then
    ln -sf docker-compose.prod.yml docker-compose.yml
fi
REMOTE_SCRIPT

# ── Step 5: Pull images and start services ───────────────────

echo "==> Pulling Docker images (this may take a few minutes)..."

ssh "${SSH_TARGET}" bash <<'REMOTE_SCRIPT'
set -euo pipefail

cd /opt/wa_llm

docker compose -f docker-compose.prod.yml pull
echo "==> Starting services..."
docker compose -f docker-compose.prod.yml up -d

echo ""
echo "==> Waiting for postgres to become healthy..."
timeout 60 bash -c '
    until docker compose -f docker-compose.prod.yml ps postgres 2>/dev/null | grep -q "healthy"; do
        sleep 3
        echo "    waiting..."
    done
' || {
    echo "WARNING: Postgres did not become healthy within 60s"
    docker compose -f docker-compose.prod.yml logs --tail=20 postgres
}

echo ""
echo "--- Container status ---"
docker compose -f docker-compose.prod.yml ps
REMOTE_SCRIPT

echo ""
echo "=========================================="
echo "  Setup complete!"
echo "=========================================="
echo ""
echo "Next steps:"
echo ""
echo "  1. SCAN QR CODE via SSH tunnel (secure, no port exposed):"
echo "     ssh -L 3001:localhost:3001 ${SSH_TARGET}"
echo "     Then open http://localhost:3001 in your browser"
echo "     Login: admin / admin"
echo "     Go to 'Account' and scan the QR code with WhatsApp"
echo ""
echo "  2. VERIFY — SSH in and check logs:"
echo "     ssh ${SSH_TARGET}"
echo "     cd ${REMOTE_DIR}"
echo "     docker compose -f docker-compose.prod.yml logs -f web-server"
echo ""
echo "  3. MIGRATE DATA (optional) — See deploy/README.md"
echo ""
