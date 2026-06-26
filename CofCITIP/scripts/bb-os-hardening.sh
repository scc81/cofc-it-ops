#!/usr/bin/env bash
# =============================================================================
# bb-os-hardening.sh — CofCITIP / BB host hardening + performance tuning
# =============================================================================
# OS-LEVEL work on the Ubuntu host itself, separate from the JARVIS app stack.
# Idempotent: safe to re-run. Nothing here can lock Steven out of BB on the
# default (no-argument) path.
#
# Usage:
#   sudo bash scripts/bb-os-hardening.sh              # safe default path
#   sudo bash scripts/bb-os-hardening.sh --lock-ssh   # SEPARATE, opt-in:
#                                                       disables SSH password
#                                                       auth — see SSH section
#
# HARD SAFETY INVARIANT:
#   The default path NEVER disables SSH password authentication. Disabling it
#   is a distinct, manually-invoked step (--lock-ssh) that refuses to run
#   unless a key is present and Steven has confirmed key login works. Locking
#   the only admin out of a box with no other access path is unacceptable.
#
# ENVIRONMENT (for reference, not asserted by the script):
#   Dell Precision 3660, Ubuntu Server 24.04 LTS (installer produced 26.04 —
#   see bb_install_lessons_learned.md), i7-12700K, 64GB RAM, RTX A2000 6GB,
#   single ext4 partition on NVMe, no LVM. Service account: cofc-itip.
#   Human admin: steven (SSH).
# =============================================================================

set -euo pipefail

# ── PARAMETERS ────────────────────────────────────────────────────────────────
LOCK_SSH=false
ADMIN_USER="${ADMIN_USER:-steven}"      # human admin whose authorized_keys we check
SERVICE_USER="${SERVICE_USER:-cofc-itip}"
JOURNALD_MAX_USE="${JOURNALD_MAX_USE:-3G}"   # log cap for single-partition box
SWAPPINESS_TARGET="${SWAPPINESS_TARGET:-10}"
NOFILE_LIMIT="${NOFILE_LIMIT:-65536}"
# BUG 4: campus LAN subnet for the mobile-UI (8080) UFW rule. The JARVIS mobile
# UI binds 0.0.0.0:8080 (LAN-facing, key-authenticated) and is the ONE JARVIS
# port that must be reachable off-loopback — but only from CofC's network, never
# Anywhere. FILL THIS IN with the real campus/IT VLAN CIDR before relying on the
# rule (e.g. 10.0.0.0/8 or the specific IT VLAN like 172.16.40.0/24). Left as a
# placeholder rather than a guessed value so a wrong subnet can't silently open
# or block the UI. Blank = the 8080 rule is SKIPPED (loopback-only, as before).
CAMPUS_LAN_CIDR="${CAMPUS_LAN_CIDR:-}"   # e.g. 10.0.0.0/8  — REPLACE, do not guess

while [[ $# -gt 0 ]]; do
  case "$1" in
    --lock-ssh) LOCK_SSH=true; shift ;;
    *) echo "Unknown flag: $1"; echo "Usage: sudo bash $0 [--lock-ssh]"; exit 2 ;;
  esac
done

# ── OUTPUT HELPERS (restated standalone — this script is independent of ───────
#    jarvis-install.sh and must run on its own with no shared sourcing) ────────
GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; BLUE='\033[0;34m'; NC='\033[0m'
ok()   { echo -e "${GREEN}[ OK ]${NC} $*"; }
skip() { echo -e "${YELLOW}[SKIP]${NC} $*"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $*"; }
info() { echo -e "${BLUE}[INFO]${NC} $*"; }
fail() { echo -e "${RED}[FAIL]${NC} $*"; exit 1; }
hdr()  { echo; echo -e "${BLUE}── $*${NC}"; }

# ── PRE-FLIGHT ────────────────────────────────────────────────────────────────
[[ $EUID -eq 0 ]] || fail "Run as root (sudo)."
grep -qi "ubuntu" /etc/os-release || warn "Not Ubuntu — tested on Ubuntu Server 24.04 only."

echo "============================================="
echo " BB host hardening + performance tuning"
echo " ADMIN_USER=$ADMIN_USER  SERVICE_USER=$SERVICE_USER  LOCK_SSH=$LOCK_SSH"
echo "============================================="

# =============================================================================
# --lock-ssh PATH: this is the ONLY thing --lock-ssh does. It is deliberately
# isolated from the default hardening path so it can never run unattended as a
# side effect of routine re-runs. It performs its own key check and refuses
# unless satisfied, then exits — it does not run sections 1-9.
# =============================================================================
if [[ "$LOCK_SSH" == "true" ]]; then
  hdr "SSH LOCKDOWN (--lock-ssh) — disabling PasswordAuthentication"

  AUTH_KEYS="$(eval echo "~${ADMIN_USER}")/.ssh/authorized_keys"
  if [[ ! -s "$AUTH_KEYS" ]]; then
    fail "No authorized_keys for '$ADMIN_USER' at $AUTH_KEYS. REFUSING to disable
          password auth — doing so now would lock you out. Add a key, verify key
          login from a SEPARATE terminal, then re-run with --lock-ssh."
  fi
  KEYCOUNT="$(grep -cvE '^\s*($|#)' "$AUTH_KEYS" || true)"
  info "Found $KEYCOUNT key line(s) in $AUTH_KEYS"

  echo
  warn "FINAL CONFIRMATION REQUIRED."
  warn "Before this proceeds you MUST already have an open, WORKING key-based SSH"
  warn "session to BB in a SEPARATE terminal. If key login does not actually work"
  warn "and you continue, you will lose remote access and need console/physical"
  warn "recovery."
  echo
  read -r -p "Type EXACTLY 'I VERIFIED KEY LOGIN' to continue: " CONFIRM
  if [[ "$CONFIRM" != "I VERIFIED KEY LOGIN" ]]; then
    fail "Confirmation not given. No changes made to sshd_config."
  fi

  # Idempotent edit via a drop-in (don't mangle the main sshd_config).
  SSHD_DROPIN="/etc/ssh/sshd_config.d/99-cofc-hardening.conf"
  DESIRED="$(cat <<'EOF'
# Managed by bb-os-hardening.sh --lock-ssh
PasswordAuthentication no
PermitRootLogin no
KbdInteractiveAuthentication no
EOF
)"
  if [[ -f "$SSHD_DROPIN" ]] && diff -q <(printf '%s\n' "$DESIRED") "$SSHD_DROPIN" >/dev/null 2>&1; then
    skip "$SSHD_DROPIN already set as desired"
  else
    printf '%s\n' "$DESIRED" > "$SSHD_DROPIN"
    chmod 644 "$SSHD_DROPIN"
    ok "Wrote $SSHD_DROPIN"
  fi

  # Validate config before reloading — a bad config + reload can drop sshd.
  if sshd -t; then
    ok "sshd config validates"
    systemctl reload ssh 2>/dev/null || systemctl reload sshd 2>/dev/null || \
      warn "Could not reload ssh service automatically — reload it manually."
    ok "SSH password authentication disabled. Keep your current session open and"
    ok "test a NEW key login before closing it."
  else
    rm -f "$SSHD_DROPIN"
    fail "sshd -t failed — removed drop-in, made NO change to running sshd."
  fi

  echo; echo "============================================="
  ok "--lock-ssh complete (no other sections run in this mode)"
  exit 0
fi

# =============================================================================
# DEFAULT PATH (sections 1-9). None of this disables password auth.
# =============================================================================

# ── 1. APT MAINTENANCE ────────────────────────────────────────────────────────
hdr "1. APT maintenance"
apt-get update -qq
ok "apt index updated"

echo; info "Upgradable packages (before any change):"
apt list --upgradable 2>/dev/null | tail -n +2 || true
echo

# Identify kernel / nvidia packages in the upgradable set and HOLD them for this
# run so `apt upgrade` can't pull them. Rationale: a kernel or nvidia-* bump
# risks breaking the confirmed-working CUDA/Ollama stack. We never auto-upgrade
# those here. We record what we held and (below) leave them held so a later
# unattended re-run stays safe too; Steven unholds + upgrades them deliberately.
UPGRADABLE="$(apt list --upgradable 2>/dev/null | tail -n +2 | cut -d/ -f1 || true)"
RISKY=()
while IFS= read -r pkg; do
  [[ -z "$pkg" ]] && continue
  case "$pkg" in
    linux-image-*|linux-headers-*|linux-generic*|linux-modules-*|nvidia-*|libnvidia-*|cuda-*)
      RISKY+=("$pkg") ;;
  esac
done <<< "$UPGRADABLE"

if [[ ${#RISKY[@]} -gt 0 ]]; then
  warn "Kernel/NVIDIA/CUDA upgrades available — HOLDING these (NOT auto-upgrading):"
  for pkg in "${RISKY[@]}"; do
    if apt-mark showhold | grep -qx "$pkg"; then
      skip "  $pkg already held"
    else
      apt-mark hold "$pkg" >/dev/null && warn "  held: $pkg"
    fi
  done
  warn "Upgrade these MANUALLY when you can validate CUDA/Ollama after a reboot:"
  warn "  sudo apt-mark unhold <pkg> && sudo apt install --only-upgrade <pkg>"
else
  ok "No kernel/NVIDIA/CUDA packages in the upgradable set"
fi

# Plain `upgrade` only — NEVER dist-upgrade/full-upgrade (those can pull in new
# kernels / drivers and change the running stack). Held packages above are
# skipped automatically by apt.
DEBIAN_FRONTEND=noninteractive apt-get upgrade -y -qq
ok "apt upgrade complete (held packages skipped, no dist-upgrade)"

apt-get autoremove -y -qq && ok "autoremove done"
apt-get autoclean -qq && ok "autoclean done"

echo; info "Currently held packages (apt-mark showhold):"
apt-mark showhold | sed 's/^/   /' || echo "   (none)"

if [[ -f /var/run/reboot-required ]]; then
  echo
  warn "REBOOT REQUIRED — a package update needs a reboot to take effect."
  warn "This box runs live services; reboot is NOT automatic. Schedule it:"
  [[ -f /var/run/reboot-required.pkgs ]] && \
    warn "  triggered by: $(tr '\n' ' ' < /var/run/reboot-required.pkgs)"
else
  ok "No reboot pending"
fi

# ── 2. UFW FIREWALL ───────────────────────────────────────────────────────────
hdr "2. UFW firewall"
if ! command -v ufw &>/dev/null; then
  DEBIAN_FRONTEND=noninteractive apt-get install -y -qq ufw && ok "ufw installed"
else
  skip "ufw already installed"
fi

# Default posture. Setting these is idempotent (ufw just restates them).
ufw default deny incoming  >/dev/null && ok "default deny incoming"
ufw default allow outgoing >/dev/null && ok "default allow outgoing"

# SSH. NOTE: scoping 22 to a specific source IP/VLAN is a MANUAL follow-up —
# Steven does it from his actual network, e.g.:
#   sudo ufw allow from <IT_VLAN_CIDR> to any port 22 proto tcp
#   sudo ufw delete allow 22
# We don't guess his subnet here; an open-to-22 rule keeps him reachable now.
if ufw status | grep -qE '(^|[[:space:]])22(/tcp)?[[:space:]]+ALLOW'; then
  skip "SSH (22) already allowed"
else
  ufw allow 22/tcp >/dev/null && ok "allowed SSH (22/tcp) — scope to your VLAN manually"
fi

# Most JARVIS service ports (8081/4000/8000/9090/3000/3001/11434/etc.) are
# loopback-only by design — NO ufw rules added for them. The ONE exception is
# the mobile UI on 8080, which binds 0.0.0.0 (jarvis-ui.service) so phones on
# campus can reach it. BUG 4: open 8080 — but SCOPED to CAMPUS_LAN_CIDR, never
# Anywhere — matching the manual-scoping pattern documented for SSH above. If
# CAMPUS_LAN_CIDR is unset we SKIP the rule (keeping 8080 effectively loopback-
# only / unreachable off-box) rather than guessing a subnet.
#
# If Session 6 (Tailscale + Caddy) is deployed, the off-LAN path is Caddy on the
# tailscale0 interface (e.g. `ufw allow in on tailscale0 to any port 443`) — a
# separate, deployment-gated manual step, not opened here.
if [[ -n "$CAMPUS_LAN_CIDR" ]]; then
  if ufw status | grep -qE "8080(/tcp)?[[:space:]]+ALLOW[[:space:]]+IN[[:space:]]+${CAMPUS_LAN_CIDR//./\\.}"; then
    skip "JARVIS UI (8080) already allowed from $CAMPUS_LAN_CIDR"
  else
    ufw allow from "$CAMPUS_LAN_CIDR" to any port 8080 proto tcp >/dev/null \
      && ok "allowed JARVIS mobile UI (8080/tcp) from $CAMPUS_LAN_CIDR"
  fi
else
  warn "CAMPUS_LAN_CIDR unset — SKIPPING the 8080 (mobile UI) rule. The UI will"
  warn "  be unreachable off-loopback until you set CAMPUS_LAN_CIDR (top of this"
  warn "  script) to the real campus/IT VLAN subnet and re-run."
fi

if ufw status | grep -q "Status: active"; then
  skip "ufw already active"
else
  # --force avoids the interactive y/n prompt; default-deny + SSH rule are set.
  ufw --force enable >/dev/null && ok "ufw enabled"
fi
echo; info "ufw status:"; ufw status verbose | sed 's/^/   /'

# ── 3. FAIL2BAN ───────────────────────────────────────────────────────────────
hdr "3. fail2ban"
if ! command -v fail2ban-server &>/dev/null; then
  DEBIAN_FRONTEND=noninteractive apt-get install -y -qq fail2ban && ok "fail2ban installed"
else
  skip "fail2ban already installed"
fi

# Enable the sshd jail via a local drop-in. Don't rewrite if already present.
F2B_JAIL="/etc/fail2ban/jail.d/cofc-sshd.local"
if [[ -f "$F2B_JAIL" ]]; then
  skip "$F2B_JAIL already configured"
else
  cat > "$F2B_JAIL" <<'EOF'
# Managed by bb-os-hardening.sh
[sshd]
enabled  = true
backend  = systemd
maxretry = 5
bantime  = 1h
findtime = 10m
EOF
  ok "Wrote $F2B_JAIL (sshd jail enabled)"
fi
systemctl enable fail2ban >/dev/null 2>&1 || true
if systemctl is-active --quiet fail2ban; then
  systemctl reload fail2ban 2>/dev/null || systemctl restart fail2ban
  ok "fail2ban active"
else
  systemctl start fail2ban && ok "fail2ban started"
fi

# ── 4. SSH HARDENING (key-presence check only — NO auto-disable) ──────────────
hdr "4. SSH hardening (key check only)"
# This section NEVER disables password auth. It only reports key status and
# tells Steven how to complete the two-step lockdown. The actual disable is the
# separate --lock-ssh path at the top of this script.
AUTH_KEYS="$(eval echo "~${ADMIN_USER}")/.ssh/authorized_keys"
if [[ -s "$AUTH_KEYS" ]]; then
  KEYCOUNT="$(grep -cvE '^\s*($|#)' "$AUTH_KEYS" || true)"
  ok "$KEYCOUNT SSH key(s) present for '$ADMIN_USER' ($AUTH_KEYS)"
  echo
  info "TWO-STEP SSH LOCKDOWN (do NOT skip the verify step):"
  info "  1. From a SEPARATE terminal, confirm key login works:"
  info "       ssh ${ADMIN_USER}@<BB-ip>      (must NOT prompt for a password)"
  info "  2. ONLY after that succeeds, run the explicit, separate step:"
  info "       sudo bash scripts/bb-os-hardening.sh --lock-ssh"
  warn "This default run did NOT change password auth. That is by design."
else
  warn "No SSH key found for '$ADMIN_USER' at $AUTH_KEYS."
  warn "SKIPPING all SSH-auth changes. Add a key first:"
  warn "  (from your laptop)  ssh-copy-id ${ADMIN_USER}@<BB-ip>"
  warn "Until a key exists and is verified, do NOT disable password auth."
fi

# ── 5. NVIDIA PERSISTENCE MODE ────────────────────────────────────────────────
hdr "5. NVIDIA persistence mode"
# WHY: without persistence mode, the driver unloads when no client holds the
# GPU, adding latency/instability when Ollama next touches it. We use the
# nvidia-persistenced SYSTEMD DAEMON (the current NVIDIA-recommended mechanism)
# rather than the legacy `nvidia-smi -pm 1`, because -pm 1 does NOT survive
# reboot on its own — the daemon does. We still call -pm 1 once for immediate
# effect this session, then ensure the daemon is enabled for persistence.
if ! command -v nvidia-smi &>/dev/null; then
  warn "nvidia-smi not found — skipping (no GPU / driver). Re-run on the GPU box."
elif ! nvidia-smi &>/dev/null; then
  warn "nvidia-smi present but GPU not responding — skipping persistence config."
else
  PSTATE="$(nvidia-smi -q -d PERSISTENCE_MODE 2>/dev/null | grep -m1 -i 'Persistence Mode' | awk -F: '{gsub(/ /,"",$2); print $2}')"
  info "Current persistence mode: ${PSTATE:-unknown}"

  # Immediate enable for this boot (idempotent — harmless if already on).
  nvidia-smi -pm 1 >/dev/null 2>&1 && ok "persistence mode enabled for current boot (-pm 1)" \
    || warn "nvidia-smi -pm 1 failed (non-fatal)"

  # Persist across reboots via nvidia-persistenced.
  if systemctl list-unit-files 2>/dev/null | grep -q '^nvidia-persistenced'; then
    if systemctl is-enabled --quiet nvidia-persistenced 2>/dev/null; then
      skip "nvidia-persistenced already enabled"
    else
      systemctl enable --now nvidia-persistenced >/dev/null 2>&1 \
        && ok "nvidia-persistenced enabled + started (survives reboot)" \
        || warn "could not enable nvidia-persistenced — check: systemctl status nvidia-persistenced"
    fi
  else
    # Package not present on some driver installs. Install the helper package
    # if available; otherwise leave a clear note rather than fabricating a unit.
    if DEBIAN_FRONTEND=noninteractive apt-get install -y -qq nvidia-persistenced 2>/dev/null; then
      systemctl enable --now nvidia-persistenced >/dev/null 2>&1 \
        && ok "installed + enabled nvidia-persistenced" \
        || warn "installed nvidia-persistenced but enable failed — check manually"
    else
      warn "nvidia-persistenced unit/package not found. -pm 1 set for THIS boot only."
      warn "Persistence will NOT survive reboot until nvidia-persistenced is in place."
    fi
  fi
fi

# ── 6. SYSCTL TUNING (vm.swappiness) ──────────────────────────────────────────
hdr "6. sysctl tuning"
# Lower swappiness so the box prefers keeping working set (Ollama/Chroma) in
# its 64GB RAM rather than swapping. Drop-in only — never edit /etc/sysctl.conf.
SYSCTL_DROPIN="/etc/sysctl.d/99-cofc-tuning.conf"
CURRENT_SWAP="$(sysctl -n vm.swappiness 2>/dev/null || echo unknown)"
info "Current vm.swappiness: $CURRENT_SWAP (target: $SWAPPINESS_TARGET)"
DESIRED_SYSCTL="# Managed by bb-os-hardening.sh
vm.swappiness = $SWAPPINESS_TARGET"
if [[ -f "$SYSCTL_DROPIN" ]] && diff -q <(printf '%s\n' "$DESIRED_SYSCTL") "$SYSCTL_DROPIN" >/dev/null 2>&1; then
  skip "$SYSCTL_DROPIN already set"
else
  printf '%s\n' "$DESIRED_SYSCTL" > "$SYSCTL_DROPIN"
  ok "Wrote $SYSCTL_DROPIN"
fi
# Apply now without a reboot.
sysctl --system >/dev/null 2>&1 && ok "sysctl applied (vm.swappiness=$(sysctl -n vm.swappiness))" \
  || warn "sysctl --system reported an issue"

# ── 7. ULIMITS FOR cofc-itip ──────────────────────────────────────────────────
hdr "7. ulimit (nofile) for $SERVICE_USER"
# Default 1024 FDs is tight with jarvis-core + ChromaDB + LiteLLM + mcpo +
# exporters all running. Raise nofile for THIS user only (not system-wide).
# NOTE: limits.d applies to PAM logins; systemd services get FD limits from
# LimitNOFILE= in their units, not from here. This covers interactive/su
# sessions as $SERVICE_USER; the install script's units set their own limits.
LIMITS_DROPIN="/etc/security/limits.d/99-cofc-itip.conf"
DESIRED_LIMITS="# Managed by bb-os-hardening.sh — open-file limits for $SERVICE_USER
$SERVICE_USER soft nofile $NOFILE_LIMIT
$SERVICE_USER hard nofile $NOFILE_LIMIT"
if [[ -f "$LIMITS_DROPIN" ]] && diff -q <(printf '%s\n' "$DESIRED_LIMITS") "$LIMITS_DROPIN" >/dev/null 2>&1; then
  skip "$LIMITS_DROPIN already set"
else
  printf '%s\n' "$DESIRED_LIMITS" > "$LIMITS_DROPIN"
  ok "Wrote $LIMITS_DROPIN (nofile=$NOFILE_LIMIT for $SERVICE_USER)"
fi
info "Applies to NEW $SERVICE_USER login sessions. For systemd services, set"
info "  LimitNOFILE=$NOFILE_LIMIT in the unit files (install script territory)."

# ── 8. JOURNALD LOG CAPS ──────────────────────────────────────────────────────
hdr "8. journald log cap"
# Single ext4 partition, no LVM — unbounded journald can eventually fill the
# disk. Cap via a drop-in (don't edit journald.conf directly).
JOURNALD_DROPIN_DIR="/etc/systemd/journald.conf.d"
JOURNALD_DROPIN="$JOURNALD_DROPIN_DIR/99-cofc-cap.conf"
mkdir -p "$JOURNALD_DROPIN_DIR"
DESIRED_JOURNALD="# Managed by bb-os-hardening.sh
[Journal]
SystemMaxUse=$JOURNALD_MAX_USE"
if [[ -f "$JOURNALD_DROPIN" ]] && diff -q <(printf '%s\n' "$DESIRED_JOURNALD") "$JOURNALD_DROPIN" >/dev/null 2>&1; then
  skip "$JOURNALD_DROPIN already set ($JOURNALD_MAX_USE)"
else
  printf '%s\n' "$DESIRED_JOURNALD" > "$JOURNALD_DROPIN"
  ok "Wrote $JOURNALD_DROPIN (SystemMaxUse=$JOURNALD_MAX_USE)"
  systemctl restart systemd-journald && ok "systemd-journald restarted (cap applied)"
fi
info "Current journal disk usage:"; journalctl --disk-usage 2>/dev/null | sed 's/^/   /' || true

# ── 9. VERIFICATION CHECKS (warn only — no auto-fix) ──────────────────────────
hdr "9. Verification checks (report only)"

# fstrim.timer — Ubuntu 24.04+ ships this on by default. Report, don't touch.
if systemctl is-enabled --quiet fstrim.timer 2>/dev/null; then
  ok "fstrim.timer enabled ($(systemctl is-active fstrim.timer 2>/dev/null))"
else
  warn "fstrim.timer NOT enabled. SSD TRIM may not run on schedule."
  warn "  Enable yourself if intended: sudo systemctl enable --now fstrim.timer"
fi

# Time sync — matters for audit log + JARVIS approval-token timestamps.
if command -v timedatectl &>/dev/null; then
  NTP_SYNC="$(timedatectl show -p NTPSynchronized --value 2>/dev/null || echo unknown)"
  if [[ "$NTP_SYNC" == "yes" ]]; then
    ok "Time is NTP-synchronized"
  else
    warn "System clock is NOT NTP-synchronized (NTPSynchronized=$NTP_SYNC)."
    warn "  Audit/approval-token timestamps may be untrustworthy. Check:"
    warn "    timedatectl status ; systemctl status systemd-timesyncd (or chrony)"
  fi
  info "timedatectl summary:"; timedatectl 2>/dev/null | sed 's/^/   /'
else
  warn "timedatectl not available — cannot verify time sync."
fi

# ── DONE ──────────────────────────────────────────────────────────────────────
echo; echo "============================================="
ok "Host hardening default path complete."
info "Reminder: SSH password auth is UNCHANGED. To disable it, verify key login"
info "from a separate terminal, then run:  sudo bash $0 --lock-ssh"
echo "============================================="
