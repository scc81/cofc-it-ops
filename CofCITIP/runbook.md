# CofCITIP — JARVIS Install Runbook
**Node 1 (BB) | Ubuntu Server 24.04 LTS**
Last updated: 2026-06-08
Status: Living document — fill in commands and notes as you execute each step

---

## Pre-Flight Checklist

Before touching hardware, confirm:
- [ ] BB GPU model confirmed (RTX A2000 — 6GB or 12GB variant)
- [ ] M.2 NVMe slot count confirmed (2x assumed, verify physically)
- [ ] SATA SSD slot count confirmed (2x assumed, verify physically)
- [ ] 4x 500GB SSDs in hand
- [ ] Ubuntu Server 24.04 LTS ISO downloaded, USB installer created
- [ ] Static IP assigned for BB on the CofC IT VLAN
- [ ] SSH key pair generated on your Windows laptop
- [ ] Firewall rules coordinated with netsec (port list below)
- [ ] UPS plugged in and tested
- [ ] Philip aware of the build (heads-up sent)

**Required firewall ports (inbound to BB):**
| Port | Service |
|------|---------|
| 22 | SSH (restrict to IT VLAN only) |
| 11434 | Ollama API (localhost only — no external) |
| 9090 | Prometheus (internal only) |
| 3000 | Grafana (internal only) |
| 3001 | Uptime Kuma (internal only) |
| 8000 | ChromaDB HTTP (localhost only) |

---

## Host Hardening Reference (`scripts/bb-os-hardening.sh`)

Standing OS-level hardening + perf tuning for BB, separate from the one-time
install walkthrough below. Idempotent; safe to re-run. Run any time:

```bash
sudo bash scripts/bb-os-hardening.sh              # safe default path
sudo bash scripts/bb-os-hardening.sh --lock-ssh   # SEPARATE, opt-in SSH lockdown
```

### What the script does (section by section)
1. **APT maintenance** — `apt update`, prints upgradable list, `apt upgrade -y` only; holds any kernel/`nvidia-*`/`cuda-*` packages instead of upgrading them; `autoremove`/`autoclean`; reports pending reboot and held packages.
2. **UFW firewall** — installs if missing, default deny-in/allow-out, allows SSH (22); opens no JARVIS ports (loopback-only by design); enables and prints status.
3. **fail2ban** — installs if missing, enables sshd jail via drop-in, skips rewrite if already configured.
4. **SSH hardening** — key-presence check only; prints the two-step lockdown instructions; **never** disables password auth on this path.
5. **NVIDIA persistence** — enables persistence for the current boot (`-pm 1`) and enables `nvidia-persistenced` so it survives reboot.
6. **sysctl tuning** — sets `vm.swappiness=10` via `/etc/sysctl.d/` drop-in, applied without reboot.
7. **ulimits** — raises `nofile` to 65536 for `cofc-itip` via `/etc/security/limits.d/` drop-in (that user only).
8. **journald cap** — sets `SystemMaxUse=3G` via `journald.conf.d/` drop-in so logs can't fill the single-partition disk; restarts journald.
9. **Verification (warn-only)** — reports `fstrim.timer` status and NTP sync status; fixes nothing automatically.

### Why `dist-upgrade`/`full-upgrade` is never run automatically
A kernel or `nvidia-*`/`cuda-*` bump can break the confirmed-working CUDA/Ollama
stack. The script runs plain `apt upgrade` only and actively **holds** those
packages for the run. Upgrade them deliberately, with a validation + reboot
window: `sudo apt-mark unhold <pkg> && sudo apt install --only-upgrade <pkg>`.

### Why SSH lockdown is split into two steps (do not "fix" into one)
Disabling `PasswordAuthentication` is the single change that can lock the only
admin out of BB. It is therefore **isolated** behind `--lock-ssh`, which refuses
to run unless a key exists in `~steven/.ssh/authorized_keys`, requires an exact
typed confirmation that key login was verified from a *separate* terminal, and
validates `sshd -t` before reloading (rolling back on failure). The default run
only *checks for* a key and prints instructions. **Verify key login in a second
terminal before running `--lock-ssh`** — if key auth doesn't actually work and
you lock it, recovery needs console/physical access. Anyone tempted to collapse
this into one automatic step should understand this failure mode first.

### Where to check each setting afterward
| Setting | Verify with |
|---------|-------------|
| Firewall | `sudo ufw status verbose` |
| fail2ban sshd jail | `sudo fail2ban-client status sshd` |
| Held packages | `apt-mark showhold` |
| NVIDIA persistence | `nvidia-smi -q -d PERSISTENCE_MODE` ; `systemctl status nvidia-persistenced` |
| Swappiness | `sysctl vm.swappiness` |
| nofile limit | as the user: `sudo -u cofc-itip bash -c 'ulimit -n'` (new login session) |
| journald usage/cap | `journalctl --disk-usage` ; `cat /etc/systemd/journald.conf.d/99-cofc-cap.conf` |
| Time sync | `timedatectl` |
| fstrim | `systemctl status fstrim.timer` |
| SSH auth (post-lock) | `sshd -T \| grep -i passwordauthentication` |

---

## Phase 0 — Hardware Prep

### 0.1 — RAID 10 Array (mdadm)
> Target: 4x 500GB SSD → ~1TB usable, RAID 10, single-drive fault tolerant

```bash
# Identify drives after boot
lsblk
# Expected: /dev/nvme0n1, /dev/nvme1n1 (M.2), /dev/sda, /dev/sdb (SATA)

# Install mdadm
sudo apt install -y mdadm

# Create RAID 10 array
sudo mdadm --create /dev/md0 --level=10 --raid-devices=4 \
  /dev/nvme0n1 /dev/nvme1n1 /dev/sda /dev/sdb

# Format
sudo mkfs.ext4 /dev/md0

# Mount
sudo mkdir -p /var/lib/cofc-itip
sudo mount /dev/md0 /var/lib/cofc-itip

# Persist across reboots
sudo mdadm --detail --scan >> /etc/mdadm/mdadm.conf
echo '/dev/md0 /var/lib/cofc-itip ext4 defaults 0 0' | sudo tee -a /etc/fstab

# Save array config
sudo update-initramfs -u
```

**Notes / actual device names observed:**
```
[fill in after hardware inventory]
```

---

## Phase 1 — OS Baseline

### 1.1 — Ubuntu Server 24.04 Install
- Boot from USB installer
- Disk target: OS on separate SSD (not RAID array) or first drive — confirm layout
- Enable OpenSSH server during install
- Hostname: `jarvis-bb` (or confirm with Philip)
- Static IP: `[YOUR_IP_HERE]`
- Create user: `cofc-itip`

**Post-install, confirm SSH access from Windows laptop before continuing:**
```powershell
ssh cofc-itip@[BB_IP]
```

### 1.2 — System Update + Base Packages
```bash
sudo apt update && sudo apt upgrade -y

sudo apt install -y \
  git curl wget build-essential python3 python3-pip python3-venv \
  htop nvtop net-tools ufw fail2ban \
  portaudio19-dev libsndfile1 ffmpeg \
  mdadm smartmontools

# Notes:
# portaudio + libsndfile: required for GLaDOS audio pipeline
# ffmpeg: required for Whisper and audio processing
# nvtop: GPU monitoring in terminal
```

### 1.3 — SSH Hardening
```bash
# Copy your SSH public key to BB first (from Windows laptop):
# ssh-copy-id cofc-itip@[BB_IP]

# Then harden sshd
sudo nano /etc/ssh/sshd_config
# Set:
#   PasswordAuthentication no
#   PermitRootLogin no
#   Port 22  (or non-standard port if IT policy requires)

sudo systemctl restart ssh
```

### 1.4 — UFW Firewall
```bash
sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw allow from [IT_VLAN_CIDR] to any port 22   # SSH from IT VLAN only
sudo ufw allow from 127.0.0.1 to any port 11434     # Ollama localhost only
sudo ufw allow from [IT_VLAN_CIDR] to any port 3000 # Grafana internal
sudo ufw allow from [IT_VLAN_CIDR] to any port 3001 # Uptime Kuma internal
sudo ufw enable
sudo ufw status verbose
```

---

## Phase 2 — NVIDIA Drivers + CUDA

> GPU confirmed: RTX A2000 (VRAM: [6GB / 12GB — fill in after inventory])
> Model stack decisions hang on this — confirm before choosing models.

### 2.1 — Install NVIDIA Drivers
```bash
# Check what Ubuntu detects
ubuntu-drivers devices

# Install recommended driver (likely nvidia-driver-550 or similar)
sudo apt install -y nvidia-driver-[VERSION]
sudo reboot

# After reboot, verify
nvidia-smi
# Expected output: GPU name, VRAM, driver version, CUDA version
```

**Actual driver version installed:** ___________
**VRAM confirmed:** ___________ (6GB or 12GB — critical for model selection)

### 2.2 — CUDA Toolkit
```bash
# Install CUDA toolkit matching driver version
# Check https://developer.nvidia.com/cuda-downloads for exact command
# Example for CUDA 12.x:
wget https://developer.download.nvidia.com/compute/cuda/repos/ubuntu2404/x86_64/cuda-keyring_1.1-1_all.deb
sudo dpkg -i cuda-keyring_1.1-1_all.deb
sudo apt update
sudo apt install -y cuda-toolkit-12-[X]

# Verify
nvcc --version
```

---

## Phase 3 — Ollama

### 3.1 — Install Ollama
```bash
curl -fsSL https://ollama.com/install.sh | sh

# Verify service running
systemctl status ollama
ollama --version
```

### 3.2 — Configure for GPU
```bash
# Ollama auto-detects NVIDIA GPU if drivers are installed correctly
# Verify GPU is in use:
ollama run mistral "hello" &
nvidia-smi  # Should show memory usage on GPU
```

### 3.3 — Pull Models
> Final model selection depends on confirmed VRAM. Pull accordingly.

**If 6GB VRAM:**
```bash
ollama pull mistral          # 4.1GB — fast voice model
ollama pull llama3.1:8b      # 4.7GB — can't run both at once, swap as needed
ollama pull qwen2.5-coder:7b # code tasks
ollama pull nomic-embed-text # embeddings for ChromaDB
```

**If 12GB VRAM:**
```bash
ollama pull mistral          # always-loaded voice model
ollama pull llama3.1:8b      # general queries
ollama pull qwen2.5-coder:14b
ollama pull nomic-embed-text
# 70B models require offloading — test before committing
```

**Models actually pulled:**
```
[fill in after hardware confirmed]
```

### 3.4 — Ollama as Systemd Service
```bash
# Ollama installer creates this automatically — verify:
sudo systemctl enable ollama
sudo systemctl status ollama

# Check service file location:
cat /etc/systemd/system/ollama.service
```

---

## Phase 4 — Python Environment

### 4.1 — Create venv
```bash
sudo mkdir -p /opt/cofc-itip
sudo chown cofc-itip:cofc-itip /opt/cofc-itip
cd /opt/cofc-itip

python3 -m venv venv
source venv/bin/activate
```

### 4.2 — Install Python Dependencies
```bash
pip install --upgrade pip

pip install \
  chromadb \
  ollama \
  requests \
  pvporcupine \
  pyaudio \
  msal \
  gql \
  python-dotenv \
  prometheus-client \
  schedule

# GLaDOS — install from source
cd /opt/cofc-itip
git clone https://github.com/dnhkng/GlaDOS.git
cd GlaDOS
pip install -e .
```

**Notes on dependency conflicts / version pins:**
```
[fill in as you encounter them]
```

---

## Phase 5 — ChromaDB

### 5.1 — Initialize ChromaDB
```bash
mkdir -p /var/lib/cofc-itip/chroma

# Test initialization
python3 - <<'EOF'
import chromadb
client = chromadb.PersistentClient(path="/var/lib/cofc-itip/chroma")
ctx = client.get_or_create_collection("context")
beh = client.get_or_create_collection("behavioral")
print("ChromaDB initialized. Collections:", client.list_collections())
EOF
```

### 5.2 — Seed Initial Context
```bash
# Run the memory seed script (create this as you build out environment facts)
# /opt/cofc-itip/scripts/seed_context.py
# Seeds ChromaDB with:
#   - Team structure (Philip, Greg, Mitch, Matt, Andrew, Alejandro, Joe G)
#   - Environment facts (Intune, Jamf, SentinelOne, Taegis, AppsAnywhere)
#   - Naming conventions, group structures, policy names
#   - Known blast radius thresholds, approval flows

python3 /opt/cofc-itip/scripts/seed_context.py
```

---

## Phase 6 — JARVIS Core

### 6.1 — Clone Repo and Deploy
```bash
cd /opt/cofc-itip
git clone https://github.com/scc81/cofc-it-ops.git
ln -s /opt/cofc-itip/cofc-it-ops/CofCITIP/jarvis_core.py /opt/cofc-itip/jarvis_core.py
ln -s /opt/cofc-itip/cofc-it-ops/CofCITIP/tools /opt/cofc-itip/tools
```

### 6.2 — Credentials
```bash
sudo mkdir -p /etc/cofc-itip
sudo cp /opt/cofc-itip/cofc-it-ops/CofCITIP/install/config.env.template /etc/cofc-itip/config.env
sudo chmod 600 /etc/cofc-itip/config.env
sudo nano /etc/cofc-itip/config.env
# Fill in:
#   OLLAMA_HOST, INTUNE_CLIENT_ID, INTUNE_CLIENT_SECRET
#   JAMF_URL, JAMF_API_KEY, TAEGIS_API_KEY
#   NODE_NAME, WAKE_WORD, PRIMARY_MODEL, FAST_MODEL
```

### 6.3 — Smoke Test JARVIS Core
```bash
source /opt/cofc-itip/venv/bin/activate
cd /opt/cofc-itip
python3 jarvis_core.py
# Expected: "JARVIS Core initialized. Ready."
# Test a mock query to confirm Ollama + ChromaDB roundtrip
```

### 6.4 — JARVIS Systemd Service
```bash
sudo tee /etc/systemd/system/jarvis-core.service <<'EOF'
[Unit]
Description=JARVIS Core Agent
After=network.target ollama.service

[Service]
Type=simple
User=cofc-itip
WorkingDirectory=/opt/cofc-itip
EnvironmentFile=/etc/cofc-itip/config.env
ExecStart=/opt/cofc-itip/venv/bin/python3 /opt/cofc-itip/jarvis_core.py
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable jarvis-core
sudo systemctl start jarvis-core
sudo systemctl status jarvis-core
```

---

## Phase 7 — Voice Pipeline

### 7.1 — Porcupine Wake Word
```bash
# Porcupine free tier requires an access key from Picovoice
# https://console.picovoice.ai — sign up, get key, store in config.env

# "Boo Boo Kitty" is a custom wake word — train via Picovoice console
# Download the .ppn file → /opt/cofc-itip/wake_words/boo_boo_kitty.ppn

# Test wake word detection:
python3 -c "
import pvporcupine
import os
handle = pvporcupine.create(
    access_key=os.environ['PICOVOICE_KEY'],
    keyword_paths=['/opt/cofc-itip/wake_words/boo_boo_kitty.ppn']
)
print('Porcupine initialized. Frame length:', handle.frame_length)
handle.delete()
"
```

**Picovoice access key stored in:** `/etc/cofc-itip/config.env` → `PICOVOICE_KEY`
**Wake word .ppn file location:** `/opt/cofc-itip/wake_words/boo_boo_kitty.ppn`

### 7.2 — GLaDOS Voice Output
```bash
# Test GLaDOS TTS with target voice
cd /opt/cofc-itip/GlaDOS
python3 -c "
from glados import GLaDOS
tts = GLaDOS()
tts.speak('JARVIS online. Bloomberg Box operational.')
"
# Expected: audio output through connected speakers/monitors
```

### 7.3 — GLaDOS Systemd Service
```bash
sudo tee /etc/systemd/system/glados-voice.service <<'EOF'
[Unit]
Description=GLaDOS Voice Pipeline
After=jarvis-core.service

[Service]
Type=simple
User=cofc-itip
WorkingDirectory=/opt/cofc-itip
EnvironmentFile=/etc/cofc-itip/config.env
ExecStart=/opt/cofc-itip/venv/bin/python3 /opt/cofc-itip/voice_listener.py
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable glados-voice
sudo systemctl start glados-voice
```

---

## Phase 8 — Monitoring Stack

### 8.1 — Prometheus
```bash
# Download latest Prometheus
cd /tmp
wget https://github.com/prometheus/prometheus/releases/download/v2.51.0/prometheus-2.51.0.linux-amd64.tar.gz
tar xvf prometheus-*.tar.gz
sudo mv prometheus-2.51.0.linux-amd64 /opt/prometheus

# Create config
sudo tee /opt/prometheus/prometheus.yml <<'EOF'
global:
  scrape_interval: 15s

scrape_configs:
  - job_name: 'node'
    static_configs:
      - targets: ['localhost:9100']
  - job_name: 'gpu'
    static_configs:
      - targets: ['localhost:9835']
EOF

# Systemd service
sudo tee /etc/systemd/system/prometheus.service <<'EOF'
[Unit]
Description=Prometheus
After=network.target

[Service]
Type=simple
User=cofc-itip
ExecStart=/opt/prometheus/prometheus \
  --config.file=/opt/prometheus/prometheus.yml \
  --storage.tsdb.path=/var/lib/cofc-itip/prometheus \
  --storage.tsdb.retention.time=90d
Restart=on-failure

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable prometheus
sudo systemctl start prometheus
```

### 8.2 — Node Exporter
```bash
cd /tmp
wget https://github.com/prometheus/node_exporter/releases/download/v1.8.0/node_exporter-1.8.0.linux-amd64.tar.gz
tar xvf node_exporter-*.tar.gz
sudo mv node_exporter-1.8.0.linux-amd64/node_exporter /usr/local/bin/

sudo tee /etc/systemd/system/node-exporter.service <<'EOF'
[Unit]
Description=Node Exporter
After=network.target

[Service]
Type=simple
User=cofc-itip
ExecStart=/usr/local/bin/node_exporter
Restart=on-failure

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable node-exporter
sudo systemctl start node-exporter

# Verify: curl http://localhost:9100/metrics
```

### 8.3 — NVIDIA GPU Exporter
```bash
cd /tmp
wget https://github.com/utkuozdemir/nvidia_gpu_exporter/releases/download/v1.2.0/nvidia_gpu_exporter_1.2.0_linux_amd64.tar.gz
tar xvf nvidia_gpu_exporter*.tar.gz
sudo mv nvidia_gpu_exporter /usr/local/bin/

sudo tee /etc/systemd/system/nvidia-gpu-exporter.service <<'EOF'
[Unit]
Description=NVIDIA GPU Exporter
After=network.target

[Service]
Type=simple
User=cofc-itip
ExecStart=/usr/local/bin/nvidia_gpu_exporter
Restart=on-failure

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable nvidia-gpu-exporter
sudo systemctl start nvidia-gpu-exporter

# Verify: curl http://localhost:9835/metrics
```

### 8.4 — Grafana
```bash
sudo apt install -y apt-transport-https software-properties-common
wget -q -O - https://packages.grafana.com/gpg.key | sudo apt-key add -
echo "deb https://packages.grafana.com/oss/deb stable main" | sudo tee /etc/apt/sources.list.d/grafana.list
sudo apt update
sudo apt install -y grafana

sudo systemctl enable grafana-server
sudo systemctl start grafana-server

# Default login: admin / admin → change immediately
# Add Prometheus as data source: http://localhost:9090
# Import dashboards:
#   - Node Exporter Full: dashboard ID 1860
#   - NVIDIA GPU: dashboard ID 14574
```

### 8.5 — Uptime Kuma
```bash
# Install via Docker or Node.js
# Node.js method (simpler, no Docker dependency):
sudo apt install -y nodejs npm
npm install -g uptime-kuma

# Or Docker method:
# sudo docker run -d --restart=always -p 3001:3001 \
#   -v uptime-kuma:/app/data --name uptime-kuma louislam/uptime-kuma:1

# Add monitors for:
#   - Ollama API: http://localhost:11434
#   - Prometheus: http://localhost:9090
#   - Grafana: http://localhost:3000
#   - ChromaDB: http://localhost:8000
#   - jarvis-core: systemd service check
#   - glados-voice: systemd service check
```

---

## Phase 9 — Backup Cron

### 9.1 — Nightly Backup Script
```bash
sudo tee /opt/cofc-itip/scripts/nightly_backup.sh <<'EOF'
#!/bin/bash
# CofCITIP Nightly Backup
# Runs: 2:00 AM daily
# Backs up: ChromaDB, Prometheus, config files
# Target: Node 2 (when online), then rsync to 5TB external

TIMESTAMP=$(date +%Y%m%d_%H%M%S)
BACKUP_DIR="/var/lib/cofc-itip/backups"
NODE2_HOST="[NODE2_IP]"  # fill in when Node 2 is online
EXTERNAL_MOUNT="/mnt/backup"

mkdir -p "$BACKUP_DIR"

# ChromaDB
tar -czf "$BACKUP_DIR/chroma_$TIMESTAMP.tar.gz" /var/lib/cofc-itip/chroma/
echo "[$(date)] ChromaDB backed up" >> /var/log/cofc-itip-backup.log

# Config files
tar -czf "$BACKUP_DIR/config_$TIMESTAMP.tar.gz" /etc/cofc-itip/
echo "[$(date)] Config backed up" >> /var/log/cofc-itip-backup.log

# Prometheus (TSDB)
tar -czf "$BACKUP_DIR/prometheus_$TIMESTAMP.tar.gz" /var/lib/cofc-itip/prometheus/
echo "[$(date)] Prometheus backed up" >> /var/log/cofc-itip-backup.log

# Prune backups older than 7 days locally
find "$BACKUP_DIR" -name "*.tar.gz" -mtime +7 -delete

# Rsync to Node 2 (uncomment when Node 2 online)
# rsync -av "$BACKUP_DIR/" cofc-itip@$NODE2_HOST:/var/lib/cofc-itip-backup/

echo "[$(date)] Backup complete" >> /var/log/cofc-itip-backup.log
EOF

chmod +x /opt/cofc-itip/scripts/nightly_backup.sh

# Add to cron
(crontab -l 2>/dev/null; echo "0 2 * * * /opt/cofc-itip/scripts/nightly_backup.sh") | crontab -
```

---

## Phase 10 — Monitor Layout (Kiosk Mode)

### 10.1 — Display Setup
> BB runs headless Ubuntu Server — X11 or Wayland needed for browser kiosk output.
> Option: Install minimal desktop (xorg + openbox) just for kiosk display.

```bash
sudo apt install -y xorg openbox chromium-browser

# Configure 4-monitor layout via xrandr
# [Fill in once physical monitor connections confirmed]
xrandr --query  # List connected displays

# Example kiosk launcher (one monitor = one dashboard):
# Monitor 1: Grafana fleet overview
# Monitor 2: Uptime Kuma service status
# Monitor 3: Custom endpoint dashboard
# Monitor 4: JARVIS interaction log or live briefing output
```

---

## Phase 11 — Final Verification

### 11.1 — Service Status Check
```bash
# All services should show active (running)
for service in ollama jarvis-core glados-voice prometheus node-exporter nvidia-gpu-exporter grafana-server; do
    echo "--- $service ---"
    systemctl is-active $service
done
```

### 11.2 — End-to-End Voice Test
1. Say "Boo Boo Kitty" → expect Porcupine wake detection
2. Ask "how many devices are non-compliant?" → expect JARVIS response (mock data or live)
3. Say "that's right" → expect positive feedback log entry in ChromaDB behavioral collection
4. Say "actually, the test group is managed by Greg" → expect correction stored in ChromaDB context collection

### 11.3 — Backup Dry Run
```bash
sudo /opt/cofc-itip/scripts/nightly_backup.sh
ls -lh /var/lib/cofc-itip/backups/
cat /var/log/cofc-itip-backup.log
```

---

## Known Blockers / TODOs at Time of Writing

| Item | Status | Notes |
|------|--------|-------|
| BB GPU VRAM (6GB vs 12GB) | ❌ UNCONFIRMED | Single biggest decision gate — all model choices depend on this |
| M.2 / SATA physical slot count | ❌ UNCONFIRMED | Needed before RAID setup |
| Picovoice API key + custom wake word .ppn | ❌ PENDING | Free tier available, train "Boo Boo Kitty" |
| Taegis API key | ❌ PENDING | Alejandro conversation required |
| Intune Graph API app registration | ❌ PENDING | Entra ID app reg + correct scopes |
| Jamf Pro API key | ❌ PENDING | Jamf service account needed |
| Node 2 IP / hostname | ❌ PENDING | Custom build pending Philip approval |
| IT VLAN CIDR | ❌ PENDING | For UFW rules |

---

## Appendix A — Service Dependency Order

```
Hardware → OS → NVIDIA Drivers → Ollama → ChromaDB → jarvis_core → glados_voice
                                        → Prometheus → Grafana
                                        → Node Exporter
                                        → GPU Exporter
                                        → Uptime Kuma
```

## Appendix B — Key File Paths

```
/etc/cofc-itip/config.env          ← credentials (chmod 600, never in git)
/etc/cofc-itip/config.env.template ← placeholder template (safe to commit)
/opt/cofc-itip/                    ← application root
/opt/cofc-itip/venv/               ← Python virtualenv
/opt/cofc-itip/cofc-it-ops/        ← git repo clone
/opt/cofc-itip/wake_words/         ← Porcupine .ppn files
/var/lib/cofc-itip/chroma/         ← ChromaDB persistent storage
/var/lib/cofc-itip/prometheus/     ← Prometheus TSDB
/var/lib/cofc-itip/backups/        ← local backup staging
/var/log/cofc-itip-backup.log      ← backup run log
```

---

*CofCITIP — Built by CofC IT. Owned by CofC IT. Runs on CofC IT.*
