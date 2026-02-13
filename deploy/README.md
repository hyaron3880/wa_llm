# Deploying wa_llm to Oracle Cloud (Free Tier)

Run the WhatsApp bot 24/7 on a free Oracle Cloud ARM VM instead of your Mac.

Voice transcription (whisper) is excluded to fit within the 4GB RAM free tier. The bot handles this gracefully — it works normally but won't transcribe voice messages.

## Architecture

```
┌──────────────────────────────────────────────────┐
│  Oracle Cloud VM.Standard.A1.Flex (ARM)          │
│  2 OCPUs, 4GB RAM (Always Free)                  │
│                                                   │
│  ┌──────────┐  ┌──────────┐                      │
│  │ postgres │  │ whatsapp │                      │
│  │ pgvector │  │ go-wa    │                      │
│  │ ~512MB   │  │ ~256MB   │                      │
│  └────┬─────┘  └────┬─────┘                      │
│       │              │                            │
│       └──────┬───────┘                            │
│              │                                    │
│       ┌──────┴──────┐                             │
│       │ web-server  │                             │
│       │ wa_llm      │                             │
│       │ ~256MB      │                             │
│       └─────────────┘                             │
│                                                   │
│  Total: ~1.5GB / 4GB                              │
└──────────────────────────────────────────────────┘
```

## Prerequisites

- An Oracle Cloud account (https://cloud.oracle.com — free tier, no ongoing charges)
- An SSH key pair on your Mac
- Your `.env` file with API keys filled in

### Generate SSH key (if you don't have one)

```bash
ssh-keygen -t ed25519 -C "wa-llm-deploy"
# Press Enter to accept defaults
cat ~/.ssh/id_ed25519.pub
# Copy this output — you'll paste it into Oracle Cloud
```

## Step 1: Create Oracle Cloud Account

1. Go to https://cloud.oracle.com and click **Sign Up**
2. Pick your home region (e.g., Frankfurt if in EU)
3. Enter email, name, and credit card ($1 verification charge, refunded)
4. Wait for account activation email

## Step 2: Create the ARM VM

1. Go to the Oracle Cloud dashboard
2. Click **Create a VM instance**
3. Configure:

| Setting | Value |
|---------|-------|
| Name | `wa-llm-bot` |
| Image | **Ubuntu 24.04** (Canonical) |
| Shape | **VM.Standard.A1.Flex** (ARM) |
| OCPUs | 2 |
| RAM | 4 GB |
| SSH Key | Paste your `~/.ssh/id_ed25519.pub` |

4. Click **Create** and note the public IP address

**Cost**: Always Free (no charges)

> **Note**: Oracle Cloud Ubuntu VMs use `ubuntu` as the default SSH user, not `root`.
> The setup script uses `root` — you may need to first enable root access:
> ```bash
> ssh ubuntu@<VM_IP> "sudo cp ~/.ssh/authorized_keys /root/.ssh/ && sudo chmod 600 /root/.ssh/authorized_keys"
> ```

## Step 3: Run the setup script

From the project root on your Mac:

```bash
./deploy/setup.sh <VM_IP>
```

This will:
- Check disk space (needs 10GB+)
- Install Docker and Docker Compose on the VM
- Configure Docker log rotation (prevents disk fill)
- Configure UFW firewall (SSH only, rate-limited)
- Copy `docker-compose.prod.yml`, `docker-compose.base.yml`, and `.env` to the VM
- Pull all Docker images
- Start 3 containers (postgres, whatsapp, web-server) and wait for health checks

## Step 4: Scan the WhatsApp QR code

Use an SSH tunnel so port 3001 is never exposed to the internet:

```bash
# Open SSH tunnel (keep this terminal open)
ssh -L 3001:localhost:3001 root@<VM_IP>
```

Then in your browser:
1. Open `http://localhost:3001`
2. Login with `admin` / `admin`
3. Go to **Account** and scan the QR code with your phone
4. Verify WhatsApp is connected
5. Close the SSH tunnel (Ctrl+C)

## Step 5: Migrate existing data (optional)

If you have conversation history in your local PostgreSQL:

```bash
# 1. Dump local databases
docker exec wa_llm-postgres-1 pg_dumpall -U user > /tmp/wa_llm_backup.sql

# 2. Copy to VM
scp /tmp/wa_llm_backup.sql root@<VM_IP>:/opt/wa_llm/

# 3. Stop web-server to avoid conflicts during restore
ssh root@<VM_IP> "cd /opt/wa_llm && docker compose -f docker-compose.prod.yml stop web-server whatsapp"

# 4. Restore on VM
ssh root@<VM_IP> "docker exec -i \$(docker ps -qf name=postgres) psql -U user -d postgres < /opt/wa_llm/wa_llm_backup.sql"

# 5. Restart services
ssh root@<VM_IP> "cd /opt/wa_llm && docker compose -f docker-compose.prod.yml up -d"

# 6. Verify row counts
ssh root@<VM_IP> "docker exec \$(docker ps -qf name=postgres) psql -U user -d postgres -c 'SELECT schemaname, tablename, n_live_tup FROM pg_stat_user_tables ORDER BY n_live_tup DESC;'"

# 7. Clean up backup files
rm /tmp/wa_llm_backup.sql
ssh root@<VM_IP> "rm /opt/wa_llm/wa_llm_backup.sql"
```

## Step 6: Verify everything works

```bash
# Check all containers are running (should show 3: postgres, whatsapp, web-server)
ssh root@<VM_IP> "cd /opt/wa_llm && docker compose -f docker-compose.prod.yml ps"

# Check web-server logs
ssh root@<VM_IP> "cd /opt/wa_llm && docker compose -f docker-compose.prod.yml logs --tail=50 web-server"

# Test: send a WhatsApp message to the bot — it should respond
# Then close your Mac lid — the bot should still respond
```

## Deploying updates

After pushing code to `main`, GitHub Actions builds a new Docker image. To deploy it:

```bash
./deploy/deploy.sh <VM_IP>
```

This syncs compose files, pulls the latest `ghcr.io/ilanbenb/wa_llm:latest` image, restarts services, and verifies the web-server started successfully.

### Auto-deploy via GitHub Actions (optional)

The workflow at `.github/workflows/deploy.yml` triggers automatically after a successful image build. To enable it:

1. Generate a dedicated deploy SSH key:
   ```bash
   ssh-keygen -t ed25519 -f ~/.ssh/wa_llm_deploy -N "" -C "github-actions-deploy"
   ```

2. Add the public key to the VM:
   ```bash
   ssh root@<VM_IP> "cat >> ~/.ssh/authorized_keys" < ~/.ssh/wa_llm_deploy.pub
   ```

3. Add secrets to GitHub (repo Settings > Secrets and variables > Actions):
   - `VPS_HOST`: Your VM IP address
   - `VPS_SSH_KEY`: Contents of `~/.ssh/wa_llm_deploy` (the private key)

4. Push to `main` — deployment will trigger automatically after the image is built and pushed.

## Operations

### View logs

```bash
ssh root@<VM_IP> "cd /opt/wa_llm && docker compose -f docker-compose.prod.yml logs -f web-server"
```

### Restart a service

```bash
ssh root@<VM_IP> "cd /opt/wa_llm && docker compose -f docker-compose.prod.yml restart web-server"
```

### Re-scan WhatsApp QR code

If the WhatsApp session expires:

```bash
# SSH tunnel (secure — no port exposed)
ssh -L 3001:localhost:3001 root@<VM_IP>

# Open http://localhost:3001 in your browser, scan QR
# Then Ctrl+C to close the tunnel
```

### Update environment variables

```bash
# Edit .env.prod on the VM
ssh root@<VM_IP> "nano /opt/wa_llm/.env.prod"

# Restart web-server to pick up changes
ssh root@<VM_IP> "cd /opt/wa_llm && docker compose -f docker-compose.prod.yml restart web-server"
```

### Full database backup

```bash
ssh root@<VM_IP> "docker exec \$(docker ps -qf name=postgres) pg_dumpall -U user" > wa_llm_backup_$(date +%Y%m%d).sql
```

## Troubleshooting

### Container keeps restarting

```bash
ssh root@<VM_IP> "cd /opt/wa_llm && docker compose -f docker-compose.prod.yml logs --tail=100 <service-name>"
```

### Out of memory

Check memory usage:

```bash
ssh root@<VM_IP> "docker stats --no-stream"
```

The 3-service setup (no whisper) uses ~1.5GB, well within the 4GB free tier limit.

### Cannot connect to WhatsApp admin UI

Use an SSH tunnel — never expose port 3001 directly:

```bash
ssh -L 3001:localhost:3001 root@<VM_IP>
# Then open http://localhost:3001
```
