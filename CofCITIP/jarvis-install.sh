#!/usr/bin/env bash
# =============================================================================
# jarvis-install.sh — CofCITIP / JARVIS installer for BB (Ubuntu 24.04)
# =============================================================================
# Fully idempotent: re-running on a configured system detects existing
# components and SKIPS them — never fails, never duplicates.
#
# Usage:
#   sudo ./jarvis-install.sh                      # full install
#   sudo DRY_RUN=true ./jarvis-install.sh         # show what would happen
#   sudo SKIP_MODELS=true ./jarvis-install.sh     # skip Ollama model pulls
#   sudo INSTALL_DIR=/opt/custom ./jarvis-install.sh
#
# Flags also accepted: --dry-run --skip-models --install-dir <path>
#
# DECISION LOG:
#   - Uptime Kuma via Docker, not npm. Rationale: npm install drags a Node
#     toolchain onto BB and fights apt-managed node versions; the official
#     Docker image is the project's recommended path, self-contained, and
#     survives Ubuntu upgrades. Docker is installed from Ubuntu's repo
#     (docker.io) — sufficient, no need for Docker's third-party apt source.
#   - Model stack sized for BB's CONFIRMED 6GB RTX A2000 (the build prompt
#     said 12GB; nvidia-smi confirmed 6GB on 2026-06-XX). Pulls:
#       mistral (fast tier), llama3:8b (primary), gemma2:9b (heavy),
#       nomic-embed-text (ChromaDB embeddings).
#     No 70B — does not fit 6GB even quantized.
#   - Prometheus/exporters as pinned-version binaries in /opt, not apt:
#     Ubuntu's packaged Prometheus lags badly; pinned binaries are
#     reproducible across reinstalls.
#   - Grafana via Grafana's official apt repo (Ubuntu has no grafana pkg).
#   - Service user 'cofc-itip': system account, no login shell, owns
#     /var/lib/cofc-itip and /etc/cofc-itip.
# =============================================================================

set -euo pipefail

# ── PARAMETERS ────────────────────────────────────────────────────────────────
INSTALL_DIR="${INSTALL_DIR:-/opt/cofc-itip}"
SKIP_MODELS="${SKIP_MODELS:-false}"
DRY_RUN="${DRY_RUN:-false}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run)      DRY_RUN=true; shift ;;
    --skip-models)  SKIP_MODELS=true; shift ;;
    --install-dir)  INSTALL_DIR="$2"; shift 2 ;;
    *) echo "Unknown flag: $1"; exit 2 ;;
  esac
done

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_USER="cofc-itip"
DATA_DIR="/var/lib/cofc-itip"
CONF_DIR="/etc/cofc-itip"
PROM_VERSION="2.53.4"
NODE_EXPORTER_VERSION="1.8.2"
NVIDIA_EXPORTER_VERSION="1.3.1"

# ── OUTPUT HELPERS ────────────────────────────────────────────────────────────
GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
ok()   { echo -e "${GREEN}[ OK ]${NC} $*"; }
skip() { echo -e "${YELLOW}[SKIP]${NC} $*"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $*"; }
fail() { echo -e "${RED}[FAIL]${NC} $*"; exit 1; }

run() {
  # Every state-changing command goes through run() — DRY_RUN prints instead.
  if [[ "$DRY_RUN" == "true" ]]; then
    echo -e "${YELLOW}[DRY ]${NC} $*"
  else
    "$@"
  fi
}

# ── PRE-FLIGHT ────────────────────────────────────────────────────────────────
[[ $EUID -eq 0 || "$DRY_RUN" == "true" ]] || fail "Run as root (sudo). Use DRY_RUN=true to preview unprivileged."
grep -qi "ubuntu" /etc/os-release || warn "Not Ubuntu — proceeding, but this is tested on Ubuntu 24.04 only."
echo "============================================="
echo " JARVIS / CofCITIP installer"
echo " INSTALL_DIR=$INSTALL_DIR  SKIP_MODELS=$SKIP_MODELS  DRY_RUN=$DRY_RUN"
echo "============================================="

# ── (a) SYSTEM PACKAGES ───────────────────────────────────────────────────────
echo; echo "── (a) System packages"
PKGS=(python3 python3-pip python3-venv git curl wget build-essential)
MISSING=()
for p in "${PKGS[@]}"; do
  dpkg -s "$p" &>/dev/null || MISSING+=("$p")
done
if [[ ${#MISSING[@]} -eq 0 ]]; then
  skip "All system packages already installed"
else
  run apt-get update -qq
  run apt-get install -y -qq "${MISSING[@]}"
  ok "Installed: ${MISSING[*]}"
fi

# ── (s) TAILSCALE (Session 6 — remote access control plane) ──────────────────
# ACCESS MODEL (read before changing anything here):
#   BB currently only answers on the CofC LAN at its static IP. This step joins
#   BB to Steven/IT's Tailscale TAILNET so the Caddy reverse proxy (section (t))
#   becomes reachable at BB's *Tailscale* IP (100.x.y.z) — NOT its campus/public
#   IP. Leadership (Zack/Sasan) and techs (Greg/Mitch/Matt) reach JARVIS by
#   being members of that same tailnet; nothing is exposed to the open internet.
#
#   This does NOT change JARVIS's egress posture: zero-egress for ops data is
#   untouched. Tailscale is an INBOUND reachability layer (WireGuard mesh), not
#   an outbound data path for ops/FERPA data.
#
# CONTROL PLANE CHOICE: Tailscale (hosted coordination server) is used here, not
#   headscale (self-hosted control plane). Rationale: lowest-effort path to a
#   working remote-access proof of concept. The data plane is WireGuard either
#   way — only key/ACL coordination is hosted. If self-hosting the control plane
#   later becomes a hard requirement (tighter alignment with the zero-egress
#   philosophy), swapping to headscale is a control-plane/login-server change,
#   not a re-architecture: install headscale on an on-prem node, point BB at it
#   with `tailscale up --login-server https://<headscale-host>`, and the Caddy
#   binding in section (t) is unaffected. Documented future swap — NOT done now.
echo; echo "── (s) Tailscale (remote access)"
if command -v tailscale &>/dev/null; then
  skip "Tailscale already installed ($(tailscale version 2>/dev/null | head -1))"
else
  # Official install script (curl|sh) — matches the Ollama pattern used in
  # section (b). The script adds Tailscale's apt repo and installs tailscaled.
  run bash -c "curl -fsSL https://tailscale.com/install.sh | sh"
  ok "Tailscale installed"
fi
# tailscaled is enabled by the install script; ensure it's up (idempotent).
if systemctl is-active --quiet tailscaled 2>/dev/null; then
  skip "tailscaled already running"
else
  run systemctl enable --now tailscaled && ok "tailscaled enabled + started" \
    || warn "tailscaled failed to start — check: journalctl -u tailscaled -n 50"
fi
# DELIBERATELY NOT auto-running `tailscale up`. Joining the tailnet requires
# browser auth or a manually-provisioned auth key — a one-time interactive step
# Steven runs by hand. NEVER bake an auth key into this committed script.
warn "MANUAL STEP REQUIRED: run 'sudo tailscale up' on BB and complete browser"
warn "  auth / approve the device in the Tailscale admin console. Until then,"
warn "  BB has NO Tailscale IP and the Caddy proxy (section (t)) is unreachable"
warn "  off-LAN. Note BB's assigned 100.x.y.z address from 'tailscale ip -4' —"
warn "  it goes into the Caddyfile bind placeholder (see config/Caddyfile)."

# ── (i) SERVICE USER (early — later steps chown to it) ───────────────────────
echo; echo "── (i) Service user"
if id "$SERVICE_USER" &>/dev/null; then
  skip "User $SERVICE_USER exists"
else
  run useradd --system --no-create-home --shell /usr/sbin/nologin "$SERVICE_USER"
  ok "Created system user $SERVICE_USER (no login shell)"
fi

# ── (b) OLLAMA + GPU DETECTION ────────────────────────────────────────────────
echo; echo "── (b) Ollama + GPU"
if command -v ollama &>/dev/null; then
  skip "Ollama already installed ($(ollama --version 2>/dev/null | head -1))"
else
  run bash -c "curl -fsSL https://ollama.com/install.sh | sh"
  ok "Ollama installed"
fi

GPU_DETECTED=false
GPU_NAME="none"
if command -v nvidia-smi &>/dev/null && nvidia-smi &>/dev/null; then
  GPU_NAME="$(nvidia-smi --query-gpu=name --format=csv,noheader | head -1)"
  GPU_VRAM="$(nvidia-smi --query-gpu=memory.total --format=csv,noheader | head -1)"
  GPU_DETECTED=true
  ok "GPU detected: $GPU_NAME ($GPU_VRAM)"
else
  warn "No NVIDIA GPU detected — CPU-only inference will be slow"
fi

if [[ "$SKIP_MODELS" == "true" ]]; then
  skip "Model pulls skipped (SKIP_MODELS=true)"
else
  pull_model() {
    if ollama list 2>/dev/null | awk '{print $1}' | grep -q "^$1"; then
      skip "Model $1 already pulled"
    else
      run ollama pull "$1" && ok "Pulled $1"
    fi
  }
  if [[ "$GPU_DETECTED" == "true" ]]; then
    # 6GB A2000 stack — see DECISION LOG at top.
    pull_model "mistral"
    pull_model "llama3:8b"
    pull_model "gemma2:9b"
    pull_model "nomic-embed-text"
  else
    pull_model "llama3.2:3b"
    pull_model "nomic-embed-text"
    warn "CPU fallback stack pulled (llama3.2:3b). Expect high latency."
  fi
fi

# ── (c) CHROMADB ──────────────────────────────────────────────────────────────
echo; echo "── (c) ChromaDB"
if python3 -c "import chromadb" &>/dev/null; then
  skip "chromadb python package present"
else
  # --ignore-installed: on Ubuntu 24.04 the apt-managed `click` package
  # blocks pip from uninstalling it during chromadb's dependency resolution,
  # failing the whole install. --ignore-installed sidesteps the uninstall.
  run pip3 install --quiet --break-system-packages --ignore-installed chromadb
  ok "chromadb installed"
fi
if [[ -d "$DATA_DIR/chroma" ]]; then
  skip "$DATA_DIR/chroma exists"
else
  run mkdir -p "$DATA_DIR/chroma"
  ok "Created $DATA_DIR/chroma"
fi
run chown -R "$SERVICE_USER:$SERVICE_USER" "$DATA_DIR"

# ── PYTHON PLATFORM DEPS (core requirements) ─────────────────────────────────
echo; echo "── Python platform dependencies"
run pip3 install --quiet --break-system-packages \
  structlog tenacity pybreaker pydantic fastapi uvicorn httpx \
  ratelimit python-dotenv msal requests
ok "Platform python deps present"

# ── (n) LITELLM PROXY (Session 4) ─────────────────────────────────────────────
# One OpenAI-compatible endpoint (:4000) in front of local Ollama + Claude API.
# jarvis_core's engines POST here instead of calling backends directly, so
# Node 2 is a litellm_config.yaml edit, not a code change. Idempotent: skip the
# pip install if the litellm CLI is already on PATH.
echo; echo "── (n) LiteLLM proxy"
# BUG 5: litellm was never actually installed. The prior block targeted
# /opt/cofc-itip/venv/bin/pip — a venv this installer never creates — so the
# pip call could not run and `litellm` never landed on PATH, while the
# jarvis-litellm unit crash-looped on a missing binary (see BUG 6). Install it
# the SAME way as every other JARVIS python dep above: system pip3 with
# --break-system-packages (Ubuntu 24.04 PEP-668 managed env). [proxy] extra
# pulls the proxy-server deps (bare `litellm` ships only the SDK, not the
# `litellm` proxy CLI the systemd unit ExecStarts).
if command -v litellm &>/dev/null; then
  skip "LiteLLM already installed ($(litellm --version 2>/dev/null | head -1))"
else
  run pip3 install --quiet --break-system-packages "litellm[proxy]"
  ok "LiteLLM (proxy extras) installed"
fi

# ── (o) mcpo + FastMCP (Session 4) ───────────────────────────────────────────
# mcpo wraps an MCP server as an OpenAPI REST API (:8000). It points at
# tools/mcp_readonly_server.py, a thin FastMCP stdio server exposing the
# READ-ONLY query tools only (no write actions — those stay gated in core).
# fastmcp is the SDK that read-only MCP server imports.
echo; echo "── (o) mcpo + FastMCP"
if command -v mcpo &>/dev/null; then
  skip "mcpo already installed"
else
  run pip3 install --quiet --break-system-packages mcpo
  ok "mcpo installed"
fi
if python3 -c "import mcp.server.fastmcp" &>/dev/null; then
  skip "FastMCP (mcp SDK) present"
else
  run pip3 install --quiet --break-system-packages fastmcp
  ok "FastMCP installed"
fi

# ── (p) SESSION 5 PYTHON DEPS (Docling ingestion + LangFuse SDK) ──────────────
# Docling: document -> markdown conversion for seed_context.py ingest path.
# langfuse: the observability client jarvis_core's LangFuseObserver imports
# (no-ops cleanly if creds are blank, so installing it is harmless even before
# the LangFuse server is deployed). websearch.py reuses already-installed
# httpx/pybreaker/tenacity/ratelimit from the platform deps above — nothing new.
echo; echo "── (p) Session 5 python deps (Docling + LangFuse)"
if python3 -c "import docling" &>/dev/null; then
  skip "docling present"
else
  # Docling pulls model/parsing deps; can be heavyweight. Tolerate partial
  # failure so a headless box still finishes — ingestion is run by hand later.
  if run pip3 install --quiet --break-system-packages docling; then
    ok "docling installed"
  else
    warn "docling install failed — seed_context --ingest will report docling_missing until installed; rerun on BB"
  fi
fi
if python3 -c "import langfuse" &>/dev/null; then
  skip "langfuse SDK present"
else
  run pip3 install --quiet --break-system-packages langfuse
  ok "langfuse SDK installed"
fi

# ── (d) VOICE PIPELINE (OpenWakeWord + faster-whisper + GLaDOS) ───────────────
# Installs exactly what voice_listener.py imports: openwakeword (wake),
# pyaudio (capture), faster-whisper (STT), and GLaDOS built from source (TTS).
# Porcupine was explicitly rejected (its free tier phones home for key
# validation and fails silently offline) — it is intentionally absent here.
# Heavy stack: tolerate partial failure so a headless/demo box with no audio
# hardware still completes the install.
echo; echo "── (d) Voice pipeline (OpenWakeWord + faster-whisper + GLaDOS)"
if python3 -c "import openwakeword" &>/dev/null; then
  skip "Voice pipeline python deps present"
else
  # ffmpeg: faster-whisper audio decode. portaudio19-dev: pyaudio build dep.
  run apt-get install -y -qq portaudio19-dev ffmpeg
  if run pip3 install --quiet --break-system-packages openwakeword pyaudio faster-whisper; then
    ok "openwakeword + pyaudio + faster-whisper installed"
  else
    warn "Voice deps partial failure — fine for headless/demo box; rerun on BB with audio hardware"
  fi
fi

# Pre-download bundled OWW wake models while network is available — fully
# offline forever after this one-time fetch. Default wake word is hey_jarvis
# (voice_listener.py default). The custom OpenWakeWord model is a separate
# Phase 2 training effort, tracked elsewhere — not installed here.
if run python3 -c "import openwakeword.utils; openwakeword.utils.download_models()"; then
  ok "OpenWakeWord models cached (hey_jarvis ready)"
else
  warn "OWW model pre-download failed — voice_listener will fetch on first run"
fi

# GLaDOS TTS — built from source into $INSTALL_DIR/GlaDOS (voice_listener.py
# does `sys.path.insert(0, "/opt/cofc-itip/GlaDOS")` then `from glados import
# GLaDOS`). Tolerate failure on a box without audio hardware.
if [[ -d "$INSTALL_DIR/GlaDOS" ]]; then
  skip "GLaDOS source already present at $INSTALL_DIR/GlaDOS"
else
  # TODO: replace with the actual GLaDOS source repo URL used on BB before
  # this runs on a real voice box. Left as a placeholder rather than a
  # fabricated URL so a wrong clone target can't silently install.
  GLADOS_REPO="https://example.invalid/REPLACE-WITH-ACTUAL-GLADOS-REPO.git"
  if run git clone "$GLADOS_REPO" "$INSTALL_DIR/GlaDOS" \
     && run pip3 install --quiet --break-system-packages -e "$INSTALL_DIR/GlaDOS"; then
    run chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR/GlaDOS"
    ok "GLaDOS built from source -> $INSTALL_DIR/GlaDOS"
  else
    warn "GLaDOS clone/build skipped or failed (placeholder repo URL, or no audio box) — fill in GLADOS_REPO and rerun on BB"
  fi
fi

# ── (f) PROMETHEUS + EXPORTERS ────────────────────────────────────────────────
echo; echo "── (f) Prometheus + exporters"
install_binary() {  # name, url, extract_path, dest
  local name="$1" url="$2" inner="$3" dest="$4"
  if [[ -x "$dest" ]]; then
    skip "$name already at $dest"
    return
  fi
  local tmp; tmp="$(mktemp -d)"
  run bash -c "wget -qO '$tmp/pkg.tar.gz' '$url' && tar -xzf '$tmp/pkg.tar.gz' -C '$tmp'"
  run bash -c "install -m 755 $tmp/$inner '$dest'"
  run rm -rf "$tmp"
  ok "$name installed -> $dest"
}

run mkdir -p /opt/prometheus /etc/prometheus
install_binary "prometheus" \
  "https://github.com/prometheus/prometheus/releases/download/v${PROM_VERSION}/prometheus-${PROM_VERSION}.linux-amd64.tar.gz" \
  "prometheus-${PROM_VERSION}.linux-amd64/prometheus" \
  "/opt/prometheus/prometheus"
install_binary "node_exporter" \
  "https://github.com/prometheus/node_exporter/releases/download/v${NODE_EXPORTER_VERSION}/node_exporter-${NODE_EXPORTER_VERSION}.linux-amd64.tar.gz" \
  "node_exporter-${NODE_EXPORTER_VERSION}.linux-amd64/node_exporter" \
  "/opt/prometheus/node_exporter"
if [[ "$GPU_DETECTED" == "true" ]]; then
  install_binary "nvidia_gpu_exporter" \
    "https://github.com/utkuozdemir/nvidia_gpu_exporter/releases/download/v${NVIDIA_EXPORTER_VERSION}/nvidia_gpu_exporter_${NVIDIA_EXPORTER_VERSION}_linux_x86_64.tar.gz" \
    "nvidia_gpu_exporter" \
    "/opt/prometheus/nvidia_gpu_exporter"
else
  skip "nvidia_gpu_exporter (no GPU)"
fi

if [[ -f /etc/prometheus/prometheus.yml ]]; then
  skip "/etc/prometheus/prometheus.yml exists — not overwriting"
else
  run bash -c "cat > /etc/prometheus/prometheus.yml <<'EOF'
global:
  scrape_interval: 15s
scrape_configs:
  - job_name: node
    static_configs: [{ targets: ['localhost:9100'] }]
  - job_name: nvidia_gpu
    static_configs: [{ targets: ['localhost:9835'] }]
  - job_name: jarvis_core
    metrics_path: /health
    static_configs: [{ targets: ['localhost:8081'] }]
EOF"
  ok "Wrote /etc/prometheus/prometheus.yml"
fi

# ── (g) GRAFANA ───────────────────────────────────────────────────────────────
echo; echo "── (g) Grafana"
if dpkg -s grafana &>/dev/null; then
  skip "Grafana already installed"
else
  run bash -c "mkdir -p /etc/apt/keyrings && \
    wget -qO- https://apt.grafana.com/gpg.key | gpg --dearmor > /etc/apt/keyrings/grafana.gpg"
  run bash -c "echo 'deb [signed-by=/etc/apt/keyrings/grafana.gpg] https://apt.grafana.com stable main' \
    > /etc/apt/sources.list.d/grafana.list"
  run apt-get update -qq
  run apt-get install -y -qq grafana
  ok "Grafana installed (default port 3000)"
fi

# ── (t) CADDY (Session 6 — reverse proxy with role-based routing) ────────────
# Caddy chosen over Nginx for this session: automatic config simplicity (one
# tiny Caddyfile, no manual cert/keyring dance for the internal use) matters
# more here than Nginx's larger ecosystem. The swap to Nginx later is
# straightforward — the routing rules in config/Caddyfile map 1:1 to Nginx
# location/proxy_pass + auth_basic. Installed from Caddy's official apt repo,
# matching the Grafana apt-repo pattern in section (g) above.
echo; echo "── (t) Caddy reverse proxy"
if dpkg -s caddy &>/dev/null; then
  skip "Caddy already installed"
else
  run bash -c "mkdir -p /etc/apt/keyrings && \
    wget -qO- 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
    | gpg --dearmor > /etc/apt/keyrings/caddy-stable.gpg"
  run bash -c "echo 'deb [signed-by=/etc/apt/keyrings/caddy-stable.gpg] https://dl.cloudsmith.io/public/caddy/stable/deb/debian any-version main' \
    > /etc/apt/sources.list.d/caddy-stable.list"
  run apt-get update -qq
  run apt-get install -y -qq caddy
  ok "Caddy installed (from official Cloudsmith apt repo)"
fi

# Deploy config/Caddyfile -> /etc/caddy/Caddyfile. Copied unconditionally so a
# git pull + rerun updates the deployed routing — BUT the deployed copy holds
# the REAL basic-auth hashes and REAL Tailscale bind IP, which the repo copy
# must never contain. To avoid clobbering Steven's hand-edited live secrets on
# rerun, only deploy if the target is absent; otherwise warn and preserve.
if [[ -f "$SRC_DIR/config/Caddyfile" ]]; then
  if [[ -f /etc/caddy/Caddyfile ]]; then
    skip "/etc/caddy/Caddyfile exists — preserved (holds real hashes + bind IP)"
    warn "  To pick up Caddyfile routing changes from the repo: diff the repo"
    warn "  copy against /etc/caddy/Caddyfile by hand, re-apply real hashes +"
    warn "  the 100.x.y.z bind, then 'sudo systemctl reload caddy'."
  else
    run mkdir -p /etc/caddy
    run cp "$SRC_DIR/config/Caddyfile" /etc/caddy/Caddyfile
    ok "Deployed config/Caddyfile -> /etc/caddy/Caddyfile (PLACEHOLDER hashes)"
    warn "  /etc/caddy/Caddyfile has PLACEHOLDER auth hashes + a TODO bind addr."
    warn "  It will NOT serve correctly until you: (1) replace the bind"
    warn "  placeholder with BB's Tailscale IP, (2) generate real hashes with"
    warn "  'caddy hash-password' for the leadership + techs accounts. See the"
    warn "  header comment in the file."
  fi
else
  warn "config/Caddyfile not found next to installer — Caddy will start with"
  warn "  its default config and NOT route /grafana or /jarvis. Place"
  warn "  config/Caddyfile and rerun."
fi

# ── (h) UPTIME KUMA (Docker — see DECISION LOG) ──────────────────────────────
echo; echo "── (h) Uptime Kuma"
if ! command -v docker &>/dev/null; then
  run apt-get install -y -qq docker.io
  run systemctl enable --now docker
  ok "Docker installed"
else
  skip "Docker present"
fi
if docker ps -a --format '{{.Names}}' 2>/dev/null | grep -q '^uptime-kuma$'; then
  skip "uptime-kuma container exists"
else
  run docker run -d --restart=always -p 3001:3001 \
    -v uptime-kuma:/app/data --name uptime-kuma louislam/uptime-kuma:1
  ok "Uptime Kuma running on :3001"
fi

# ── (q) SEARXNG (Docker — opt-in research backend, Session 5) ─────────────────
# Reuses the Docker presence established above (no second runtime). Bound to
# LOCALHOST ONLY (127.0.0.1:8888 -> container :8080). SearXNG ships with the
# JSON API DISABLED by default, so we write a minimal settings.yml enabling
# `formats: [html, json]` and a random secret — without this, format=json
# returns 403/500. Idempotent: skip if the container already exists.
# NOTE: SearXNG only does anything for JARVIS when EGRESS_RESEARCH != local;
# the container can run while the feature stays gated off in config.env.
echo; echo "── (q) SearXNG (opt-in research)"
SEARXNG_CONF_DIR="$DATA_DIR/searxng"
if docker ps -a --format '{{.Names}}' 2>/dev/null | grep -q '^searxng$'; then
  skip "searxng container exists"
else
  run mkdir -p "$SEARXNG_CONF_DIR"
  # Minimal settings.yml: enable JSON output + a generated secret_key. Only
  # written if absent so an admin's tuning survives reruns.
  if [[ "$DRY_RUN" != "true" ]]; then
    if [[ ! -f "$SEARXNG_CONF_DIR/settings.yml" ]]; then
      SEARXNG_SECRET="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
      cat > "$SEARXNG_CONF_DIR/settings.yml" <<EOF
# CofCITIP SearXNG — minimal config. JSON API enabled for the websearch tool.
use_default_settings: true
server:
  secret_key: "${SEARXNG_SECRET}"
  bind_address: "0.0.0.0"
  limiter: false           # single trusted local caller (jarvis_core); no bot limiter
search:
  formats:
    - html
    - json                 # REQUIRED — without this, format=json returns 403
EOF
      chown -R "$SERVICE_USER:$SERVICE_USER" "$SEARXNG_CONF_DIR" 2>/dev/null || true
      ok "Wrote SearXNG settings.yml (JSON API enabled)"
    else
      skip "SearXNG settings.yml exists — preserved"
    fi
  else
    echo -e "${YELLOW}[DRY ]${NC} write $SEARXNG_CONF_DIR/settings.yml (enable JSON)"
  fi
  # 127.0.0.1 publish keeps it on-box; container listens on 8080 internally.
  run docker run -d --restart=always -p 127.0.0.1:8888:8080 \
    -v "$SEARXNG_CONF_DIR:/etc/searxng:rw" \
    -e "SEARXNG_BASE_URL=http://localhost:8888/" \
    --name searxng searxng/searxng:latest
  ok "SearXNG running on 127.0.0.1:8888 (localhost only)"
fi

# ── (r) LANGFUSE (Docker Compose — observability, Session 5) ──────────────────
# Self-hosted LangFuse v3. Reuses Docker (adds the compose plugin if missing).
# LangFuse v3's self-host stack is NOT a single container — it needs Postgres +
# ClickHouse + Redis + MinIO + the web/worker services. The exact compose syntax
# and env-var names change between LangFuse releases, so this scaffolds the
# pieces with a clear TODO rather than risking a stale, wrong compose file.
#
# TODO (Steven): verify against the CURRENT LangFuse self-host docs before
#   `docker compose up` — https://langfuse.com/self-hosting/docker-compose
#   The canonical path is to fetch LangFuse's official docker-compose.yml:
#     curl -o "$LANGFUSE_DIR/docker-compose.yml" \
#       https://raw.githubusercontent.com/langfuse/langfuse/main/docker-compose.yml
#   then set the secrets in "$LANGFUSE_DIR/.env" (NEXTAUTH_SECRET, SALT,
#   ENCRYPTION_KEY, and the LANGFUSE_INIT_* / db passwords). Do NOT commit .env.
#   After it's up, create a project in the UI (default http://localhost:3000),
#   copy the public/secret keys into config.env (LANGFUSE_*), and restart
#   jarvis-core. Until then, LangFuseObserver no-ops cleanly — observability
#   stays disabled with no effect on queries.
echo; echo "── (r) LangFuse (observability — scaffold + TODO)"
LANGFUSE_DIR="$INSTALL_DIR/langfuse"
if ! docker compose version &>/dev/null 2>&1; then
  # compose v2 plugin; harmless if already present.
  run apt-get install -y -qq docker-compose-v2 || \
    warn "docker compose plugin not installed automatically — install before bringing LangFuse up"
fi
if docker ps -a --format '{{.Names}}' 2>/dev/null | grep -q 'langfuse'; then
  skip "LangFuse container(s) already present"
else
  run mkdir -p "$LANGFUSE_DIR"
  if [[ "$DRY_RUN" != "true" ]]; then
    # Scaffold a .env with the secrets we ARE confident LangFuse v3 needs,
    # generated locally. The compose file itself is intentionally fetched by
    # the admin (TODO above) to avoid shipping stale syntax.
    if [[ ! -f "$LANGFUSE_DIR/.env" ]]; then
      cat > "$LANGFUSE_DIR/.env" <<EOF
# LangFuse v3 self-host secrets — generated locally, DO NOT COMMIT.
# Verify required keys against current docs; these are the stable ones.
NEXTAUTH_SECRET=$(python3 -c 'import secrets;print(secrets.token_urlsafe(32))')
SALT=$(python3 -c 'import secrets;print(secrets.token_urlsafe(32))')
ENCRYPTION_KEY=$(python3 -c 'import secrets;print(secrets.token_hex(32))')
NEXTAUTH_URL=http://localhost:3000
# TODO: add db passwords + LANGFUSE_INIT_* per current self-host docs.
EOF
      chmod 600 "$LANGFUSE_DIR/.env"
      chown -R "$SERVICE_USER:$SERVICE_USER" "$LANGFUSE_DIR" 2>/dev/null || true
      ok "Scaffolded $LANGFUSE_DIR/.env (secrets generated; compose file = TODO)"
    else
      skip "LangFuse .env exists — preserved"
    fi
    warn "LangFuse compose file NOT fetched automatically — see TODO in installer; bring up manually after verifying current docs"
  else
    echo -e "${YELLOW}[DRY ]${NC} scaffold $LANGFUSE_DIR/.env + print LangFuse TODO"
  fi
fi

# ── (j) CONFIG SCAFFOLD ───────────────────────────────────────────────────────
echo; echo "── (j) Config scaffold"
run mkdir -p "$CONF_DIR" "$INSTALL_DIR"
if [[ -f "$CONF_DIR/config.env" ]]; then
  skip "$CONF_DIR/config.env exists — not overwriting (credentials preserved)"
else
  run bash -c "cat > '$CONF_DIR/config.env' <<'EOF'
# CofCITIP runtime config — chmod 600, NEVER commit to git.
OLLAMA_HOST=http://localhost:11434
FAST_MODEL=mistral
PRIMARY_MODEL=llama3:8b
HEAVY_MODEL=gemma2:9b
CHROMA_PATH=/var/lib/cofc-itip/chroma
AUDIT_DB=/var/lib/cofc-itip/audit.db
EGRESS_MODE=local
EGRESS_INFERENCE=local
EGRESS_RESEARCH=local
JARVIS_MOCK=true
ANTHROPIC_API_KEY=
LITELLM_HOST=http://localhost:4000
SEARXNG_HOST=http://localhost:8888
LANGFUSE_HOST=
LANGFUSE_PUBLIC_KEY=
LANGFUSE_SECRET_KEY=
INTUNE_TENANT_ID=
INTUNE_CLIENT_ID=
INTUNE_CLIENT_SECRET=
JAMF_URL=
JAMF_USERNAME=
JAMF_PASSWORD=
SN_INSTANCE=
SN_USER=
SN_PASS=
JARVIS_CORE_URL=http://127.0.0.1:8081
JARVIS_UI_KEY=
# BUG 6: Uptime Kuma PUSH monitor URL for crash-loop alerts. Create a Push-type
# monitor in Uptime Kuma (http://127.0.0.1:3001), paste its push URL here, then
# jarvis-crash-alert.sh will ping it when any JARVIS unit trips its StartLimit.
# Blank = structlog/journald alert only (still Prometheus-alertable).
UPTIME_KUMA_PUSH_URL=
EOF"
  ok "Wrote $CONF_DIR/config.env scaffold (mock mode ON by default)"
fi
run chown -R "$SERVICE_USER:$SERVICE_USER" "$CONF_DIR"
run chmod 600 "$CONF_DIR/config.env"
ok "$CONF_DIR/config.env owned by $SERVICE_USER, mode 600"

# FIX #7: generate JARVIS_UI_KEY if the line exists but is blank. Idempotent:
# a key already set on a prior run is left untouched (never regenerated — that
# would lock out installed phones). If the key line is missing entirely
# (hand-edited config), append one.
if [[ "$DRY_RUN" != "true" ]]; then
  if grep -q '^JARVIS_UI_KEY=$' "$CONF_DIR/config.env"; then
    UI_KEY="$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')"
    sed -i "s|^JARVIS_UI_KEY=\$|JARVIS_UI_KEY=${UI_KEY}|" "$CONF_DIR/config.env"
    ok "Generated JARVIS_UI_KEY"
  elif grep -q '^JARVIS_UI_KEY=' "$CONF_DIR/config.env"; then
    skip "JARVIS_UI_KEY already set — preserved"
  else
    UI_KEY="$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')"
    echo "JARVIS_UI_KEY=${UI_KEY}" >> "$CONF_DIR/config.env"
    ok "Appended generated JARVIS_UI_KEY (was missing)"
  fi
else
  echo -e "${YELLOW}[DRY ]${NC} generate JARVIS_UI_KEY if blank"
fi

# Deploy application code if running from a repo checkout. Each artifact is
# deployed independently and idempotently — deploying the UI must not be gated
# on whether core already exists (the old single-guard skipped UI/voice on any
# box that already had jarvis_core.py).
SRC_DIR="$(dirname "$0")"

if [[ -f "$SRC_DIR/jarvis_core.py" ]]; then
  run mkdir -p "$INSTALL_DIR"
  # jarvis_core.py + embedding.py (shared Chroma embedder, imported by core
  # AND seed_context.py) + tools/. Copy unconditionally so a git pull + rerun
  # actually updates the deployed code.
  run cp "$SRC_DIR/jarvis_core.py" "$INSTALL_DIR/"
  # BUG 2: embedding.py is REQUIRED — jarvis_core.py and seed_context.py both
  # `from embedding import get_embedding_function`. It was previously copied
  # only via a `[[ -f ]] && cp` guard that silently skipped when missing,
  # leaving a box where every /query crashed on import. Copy it unconditionally,
  # then HARD-verify it landed; a missing embedder fails the install loudly.
  run cp "$SRC_DIR/embedding.py" "$INSTALL_DIR/"
  if [[ "$DRY_RUN" != "true" ]] && [[ ! -f "$INSTALL_DIR/embedding.py" ]]; then
    fail "embedding.py did not deploy to $INSTALL_DIR — jarvis_core.py and \
seed_context.py import it and will crash every /query without it. \
Ensure embedding.py sits next to jarvis-install.sh and re-run."
  fi
  ok "embedding.py deployed and verified at $INSTALL_DIR/embedding.py"
  [[ -f "$SRC_DIR/event_bus.py" ]]     && run cp "$SRC_DIR/event_bus.py" "$INSTALL_DIR/"
  [[ -f "$SRC_DIR/seed_context.py" ]]  && run cp "$SRC_DIR/seed_context.py" "$INSTALL_DIR/"
  [[ -d "$SRC_DIR/tools" ]]            && run cp -r "$SRC_DIR/tools" "$INSTALL_DIR/"
  ok "Deployed core + embedding + event_bus + tools to $INSTALL_DIR"

  # Session 4: config/ holds litellm_config.yaml + mcpo_config.json. Copy the
  # whole dir so the LiteLLM and mcpo units find their configs at the paths
  # their ExecStart lines reference ($INSTALL_DIR/config/...). Copied
  # unconditionally so a git pull + rerun updates deployed configs too.
  if [[ -d "$SRC_DIR/config" ]]; then
    run mkdir -p "$INSTALL_DIR/config"
    run cp -r "$SRC_DIR/config/." "$INSTALL_DIR/config/"
    ok "Deployed config/ (litellm + mcpo) to $INSTALL_DIR/config"
  else
    warn "config/ dir not found next to installer — litellm/mcpo units will not start until litellm_config.yaml + mcpo_config.json are placed in $INSTALL_DIR/config"
  fi
else
  warn "jarvis_core.py not found next to installer — clone repo into $INSTALL_DIR manually"
fi

# Mobile UI: jarvis_ui.service ExecStarts $INSTALL_DIR/ui/jarvis_ui.py, which
# was previously never deployed (started by hand on BB as a workaround).
if [[ -f "$SRC_DIR/jarvis_ui.py" ]]; then
  run mkdir -p "$INSTALL_DIR/ui"
  run cp "$SRC_DIR/jarvis_ui.py" "$INSTALL_DIR/ui/"
  [[ -f "$SRC_DIR/index.html" ]] && run cp "$SRC_DIR/index.html" "$INSTALL_DIR/ui/"
  ok "Deployed mobile UI to $INSTALL_DIR/ui"
else
  warn "jarvis_ui.py not found next to installer — UI not deployed"
fi

# Voice listener: jarvis-voice.service ExecStarts $INSTALL_DIR/voice_listener.py.
if [[ -f "$SRC_DIR/voice_listener.py" ]]; then
  run cp "$SRC_DIR/voice_listener.py" "$INSTALL_DIR/"
  ok "Deployed voice_listener.py to $INSTALL_DIR"
fi

run chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR"

# ── (k) SYSTEMD SERVICES ─────────────────────────────────────────────────────
echo; echo "── (k) systemd services"
write_unit() {  # path, content — only writes if changed (idempotent + updatable)
  local path="$1" content="$2"
  if [[ -f "$path" ]] && diff -q <(echo "$content") "$path" &>/dev/null; then
    skip "$(basename "$path") unchanged"
  else
    run bash -c "cat > '$path' <<EOF
$content
EOF"
    ok "Wrote $(basename "$path")"
    UNITS_CHANGED=true
  fi
}
UNITS_CHANGED=false

# Session 4: resolve the actual installed paths of litellm/mcpo console scripts.
# pip --break-system-packages may drop them in /usr/local/bin or /usr/bin
# depending on the box; hardcoding one is fragile. Fall back to a sane default
# (path is only used at unit ExecStart time, so a missing binary surfaces as a
# unit start failure with a clear journalctl message, not a silent install).
# BUG 5: fallback is /usr/local/bin/litellm (where system pip3 installs the
# console script), not the phantom /opt/cofc-itip/venv that never existed.
LITELLM_BIN="$(command -v litellm 2>/dev/null || echo /usr/local/bin/litellm)"
MCPO_BIN="$(command -v mcpo 2>/dev/null || echo /usr/local/bin/mcpo)"

# ── BUG 6: crash-loop detection + alerting ────────────────────────────────────
# Context: jarvis-litellm crash-looped 800+ times silently before anyone
# noticed, because Restart=on-failure with no StartLimit just respawns forever
# and nothing fires when it does. Two-part fix, reusing the EXISTING stack
# (Uptime Kuma + structlog/journald -> Prometheus) — no new monitoring tool:
#
#   (1) Tighten StartLimitIntervalSec / StartLimitBurst on every JARVIS unit so
#       a real crash loop ENTERS the failed state quickly instead of respawning
#       indefinitely. With RestartSec=5 and burst=5 over a 120s window, ~5 fast
#       failures trip the limit and systemd gives up — which then…
#   (2) …triggers OnFailure=jarvis-alert@%n.service. That templated one-shot
#       runs jarvis-crash-alert.sh with the failed unit name, which (a) emits a
#       high-severity structlog JSON line to journald (Prometheus/Promtail can
#       alert on it) and (b) POSTs to an Uptime Kuma PUSH monitor URL if one is
#       configured in config.env (UPTIME_KUMA_PUSH_URL). Best-effort: a missing
#       push URL just logs and exits 0 — alerting must never itself fail-loop.
#
# Deploy the alert script + the alert template unit BEFORE the JARVIS units so
# their OnFailure= target exists when they're written.
run bash -c "cat > '$INSTALL_DIR/jarvis-crash-alert.sh' <<'ALERTEOF'
#!/usr/bin/env bash
# jarvis-crash-alert.sh — fired by jarvis-alert@.service OnFailure for a
# crash-looping JARVIS unit. Arg \$1 = failed unit name (systemd %n). Reuses the
# existing stack: a structlog-shaped JSON line to journald (Prometheus-alertable)
# + an optional Uptime Kuma push. NEVER exits non-zero — alerting must not loop.
set -uo pipefail
UNIT=\"\${1:-unknown}\"
TS=\"\$(date -u +%Y-%m-%dT%H:%M:%SZ)\"
# (a) High-severity structured line to journald (JSON, matches jarvis_core's
# structlog renderer so the same log pipeline picks it up).
echo \"{\\\"timestamp\\\": \\\"\${TS}\\\", \\\"level\\\": \\\"critical\\\", \\\"event\\\": \\\"service.crash_loop\\\", \\\"unit\\\": \\\"\${UNIT}\\\"}\"
# (b) Optional Uptime Kuma push. UPTIME_KUMA_PUSH_URL is sourced from config.env
# by the alert unit's EnvironmentFile; blank-safe.
if [[ -n \"\${UPTIME_KUMA_PUSH_URL:-}\" ]]; then
  curl -fsS --max-time 10 \
    \"\${UPTIME_KUMA_PUSH_URL}?status=down&msg=\$(printf '%s' \"crash-loop:\${UNIT}\" | sed 's/ /%20/g')\" \
    >/dev/null 2>&1 || echo \"{\\\"timestamp\\\": \\\"\${TS}\\\", \\\"level\\\": \\\"warning\\\", \\\"event\\\": \\\"crash_alert.push_failed\\\", \\\"unit\\\": \\\"\${UNIT}\\\"}\"
fi
exit 0
ALERTEOF"
run chmod 755 "$INSTALL_DIR/jarvis-crash-alert.sh"
run chown "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR/jarvis-crash-alert.sh"
ok "Deployed jarvis-crash-alert.sh (crash-loop alerter)"

# Templated one-shot alert unit. %i is the failed unit's name (passed by the
# JARVIS units as OnFailure=jarvis-alert@%n.service). EnvironmentFile makes
# UPTIME_KUMA_PUSH_URL available to the script. Runs as the service account.
write_unit /etc/systemd/system/jarvis-alert@.service "[Unit]
Description=JARVIS crash-loop alert for %i
# This unit is the OnFailure target for the JARVIS services. It must NOT itself
# have an OnFailure (no recursion) and must not be restarted.

[Service]
Type=oneshot
User=$SERVICE_USER
EnvironmentFile=-$CONF_DIR/config.env
ExecStart=$INSTALL_DIR/jarvis-crash-alert.sh %i"


write_unit /etc/systemd/system/jarvis-core.service "[Unit]
Description=JARVIS Core (CofCITIP agent runtime)
After=network-online.target ollama.service jarvis-litellm.service
# Session 4: core's engines call the LiteLLM proxy, so it should come up first.
# Wants (not Requires) — if litellm is briefly down, core still starts and its
# graceful-degrade paths handle the proxy being unreachable.
Wants=network-online.target jarvis-litellm.service
# BUG 6: bound the restart loop. ~5 failures inside 120s trips the limit and
# systemd stops respawning (entering 'failed'), which fires OnFailure below.
StartLimitIntervalSec=120
StartLimitBurst=5
OnFailure=jarvis-alert@%n.service

[Service]
Type=simple
User=$SERVICE_USER
Environment=JARVIS_CONFIG=$CONF_DIR/config.env
WorkingDirectory=$INSTALL_DIR
ExecStart=/usr/bin/python3 $INSTALL_DIR/jarvis_core.py --host 127.0.0.1 --port 8081
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target"

# Session 4: LiteLLM proxy unit. EnvironmentFile pulls OLLAMA_HOST,
# ANTHROPIC_API_KEY, and (optional) LITELLM_MASTER_KEY from config.env so the
# yaml's os.environ/... lookups resolve and no secret is on the command line.
# Starts before jarvis-core (ordering above). Loopback bind keeps the proxy
# on-box; engines reach it at LITELLM_HOST (default http://localhost:4000).
write_unit /etc/systemd/system/jarvis-litellm.service "[Unit]
Description=JARVIS LiteLLM proxy (OpenAI-compatible gateway for Ollama + Claude)
After=network-online.target ollama.service
Wants=network-online.target
# BUG 6: this is the unit that crash-looped 800+ times silently. Bound it.
StartLimitIntervalSec=120
StartLimitBurst=5
OnFailure=jarvis-alert@%n.service

[Service]
Type=simple
User=$SERVICE_USER
WorkingDirectory=$INSTALL_DIR
EnvironmentFile=$CONF_DIR/config.env
ExecStart=$LITELLM_BIN --config $INSTALL_DIR/config/litellm_config.yaml --host 127.0.0.1 --port 4000
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target"

# Session 4: mcpo unit — exposes the READ-ONLY MCP server as OpenAPI REST on
# :8000 (loopback). JARVIS_CONFIG via EnvironmentFile so the wrapped connectors
# honor mock mode / live creds. Bound to 127.0.0.1: this surface carries ops
# data and must not be off-box reachable. --api-key from MCPO_API_KEY if set.
write_unit /etc/systemd/system/jarvis-mcpo.service "[Unit]
Description=JARVIS mcpo (read-only tools as OpenAPI REST)
After=network-online.target jarvis-core.service
Wants=network-online.target
# BUG 6: bound the restart loop + alert on crash-loop (see jarvis-alert@).
StartLimitIntervalSec=120
StartLimitBurst=5
OnFailure=jarvis-alert@%n.service

[Service]
Type=simple
User=$SERVICE_USER
WorkingDirectory=$INSTALL_DIR
EnvironmentFile=$CONF_DIR/config.env
ExecStart=$MCPO_BIN --config $INSTALL_DIR/config/mcpo_config.json --host 127.0.0.1 --port 8000
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target"

write_unit /etc/systemd/system/jarvis-ui.service "[Unit]
Description=JARVIS Mobile UI (LAN-facing, key-authenticated)
After=network-online.target jarvis-core.service
# BUG 6: bound the restart loop + alert on crash-loop (see jarvis-alert@).
StartLimitIntervalSec=120
StartLimitBurst=5
OnFailure=jarvis-alert@%n.service

[Service]
Type=simple
User=$SERVICE_USER
WorkingDirectory=$INSTALL_DIR
EnvironmentFile=$CONF_DIR/config.env
ExecStart=/usr/bin/python3 $INSTALL_DIR/ui/jarvis_ui.py --host 0.0.0.0 --port 8080
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target"

write_unit /etc/systemd/system/jarvis-voice.service "[Unit]
Description=JARVIS Voice Pipeline (OpenWakeWord + faster-whisper)
After=jarvis-core.service sound.target

[Service]
Type=simple
User=$SERVICE_USER
WorkingDirectory=$INSTALL_DIR
EnvironmentFile=$CONF_DIR/config.env
Environment=JARVIS_CORE_URL=http://127.0.0.1:8081
ExecStart=/usr/bin/python3 $INSTALL_DIR/voice_listener.py
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target"

write_unit /etc/systemd/system/prometheus.service "[Unit]
Description=Prometheus
After=network-online.target

[Service]
User=$SERVICE_USER
ExecStart=/opt/prometheus/prometheus --config.file=/etc/prometheus/prometheus.yml --storage.tsdb.path=$DATA_DIR/prometheus
Restart=on-failure

[Install]
WantedBy=multi-user.target"

write_unit /etc/systemd/system/node-exporter.service "[Unit]
Description=Prometheus Node Exporter
After=network-online.target

[Service]
User=$SERVICE_USER
ExecStart=/opt/prometheus/node_exporter
Restart=on-failure

[Install]
WantedBy=multi-user.target"

if [[ "$GPU_DETECTED" == "true" ]]; then
  write_unit /etc/systemd/system/nvidia-gpu-exporter.service "[Unit]
Description=NVIDIA GPU Exporter
After=network-online.target

[Service]
User=$SERVICE_USER
ExecStart=/opt/prometheus/nvidia_gpu_exporter
Restart=on-failure

[Install]
WantedBy=multi-user.target"
fi

run mkdir -p "$DATA_DIR/prometheus"
run chown -R "$SERVICE_USER:$SERVICE_USER" "$DATA_DIR"

# ── (l) ENABLE + START ────────────────────────────────────────────────────────
echo; echo "── (l) Enable and start services"
[[ "$UNITS_CHANGED" == "true" ]] && run systemctl daemon-reload
# Session 4: jarvis-litellm before jarvis-core (ordering handled by unit
# After=), jarvis-mcpo after core. Listed in start order here too.
SERVICES=(jarvis-litellm jarvis-core jarvis-mcpo jarvis-ui prometheus node-exporter grafana-server caddy)
[[ "$GPU_DETECTED" == "true" ]] && SERVICES+=(nvidia-gpu-exporter)
# jarvis-voice is installed but NOT auto-started — it needs working audio
# hardware. Start it on BB with: sudo systemctl enable --now jarvis-voice
for svc in "${SERVICES[@]}"; do
  if systemctl is-active --quiet "$svc" 2>/dev/null; then
    skip "$svc already running"
  else
    run systemctl enable --now "$svc" && ok "$svc enabled + started" \
      || warn "$svc failed to start — check: journalctl -u $svc -n 50"
  fi
done

# ── (m) POST-INSTALL SUMMARY ──────────────────────────────────────────────────
echo; echo "============================================="
echo " POST-INSTALL SUMMARY"
echo "============================================="
echo " GPU:            $GPU_NAME"
if [[ "$DRY_RUN" != "true" ]]; then
  echo " Ollama models:"
  ollama list 2>/dev/null | sed 's/^/   /' || echo "   (ollama not responding)"
  echo " Services:"
  for svc in "${SERVICES[@]}" docker; do
    printf "   %-22s %s\n" "$svc" "$(systemctl is-active "$svc" 2>/dev/null || echo unknown)"
  done
  echo " Endpoints:"
  echo "   JARVIS core    http://127.0.0.1:8081/health"
echo "   JARVIS UI      http://$(hostname -I | awk '{print $1}'):8080  (phone URL)"
  echo "   LiteLLM proxy  http://127.0.0.1:4000/health  (OpenAI-compatible gateway)"
  echo "   mcpo REST      http://127.0.0.1:8000/jarvis-readonly/docs  (read-only tools)"
  echo "   Prometheus     http://127.0.0.1:9090"
  echo "   Grafana        http://127.0.0.1:3000  (admin/admin first login)"
  echo "   Uptime Kuma    http://127.0.0.1:3001"
  echo "   ---- remote access (Session 6) ----"
  echo "   Tailscale IP   $(tailscale ip -4 2>/dev/null | head -1 || echo '(run: sudo tailscale up)')"
  echo "   Caddy proxy    http://<BB-tailscale-ip>/grafana/  (leadership, read-only)"
  echo "                  http://<BB-tailscale-ip>/jarvis/   (techs, full query)"
  echo "                  bind + auth hashes set in /etc/caddy/Caddyfile (manual)"
  echo "   SearXNG        http://127.0.0.1:8888  (opt-in research; needs EGRESS_RESEARCH!=local)"
  echo "   LangFuse       http://127.0.0.1:3000  (observability; bring up via compose — see installer TODO)"
fi
echo
echo " Next steps:"
echo "   1. Edit $CONF_DIR/config.env — add credentials, set JARVIS_MOCK=false when ready"
echo "   2. Point Uptime Kuma at http://127.0.0.1:8081/health (core) and :8080/health (UI)"
echo "   3. Import Grafana dashboards (node exporter ID 1860, GPU ID 14574)"
echo "   4. Session 6 remote access (do these BY HAND, not scripted):"
echo "      a. sudo tailscale up   (browser auth / approve device in admin console)"
echo "      b. note BB's Tailscale IP:  tailscale ip -4"
echo "      c. edit /etc/caddy/Caddyfile: set the bind addr to that 100.x.y.z IP"
echo "      d. caddy hash-password   -> paste real hashes for leadership + techs"
echo "      e. set Grafana org role to Viewer/anonymous (read-only leadership view)"
echo "      f. sudo systemctl reload caddy"
echo "      g. TEST from OFF-LAN (phone hotspot, NOT CofC wifi) before telling Zack/Sasan"
echo "============================================="
ok "Install complete"
