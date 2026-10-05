#!/usr/bin/env bash
# Prepare a fresh Oracle Cloud Always Free VM to run AI Worker.
#
# Tested by inspection against Ubuntu 22.04/24.04 and Oracle Linux 9 (the two
# images Oracle offers for the free Ampere A1 shape). Safe to re-run: every step
# checks before it acts.
#
#   sudo bash deploy/oracle_setup.sh
#
# What it does, in order:
#   1. install Docker Engine + the compose plugin from Docker's own repo
#   2. make sure ports 80/443 are open in the host firewall
#   3. generate a bcrypt password hash for Caddy if you have not got one
#
# It does NOT deploy anything. Run deploy/deploy.sh once this finishes.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${REPO_ROOT}/deploy/.env"
ENV_EXAMPLE="${REPO_ROOT}/deploy/.env.example"

log()  { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[33mwarning: %s\033[0m\n' "$*" >&2; }
die()  { printf '\033[31merror: %s\033[0m\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "run as root: sudo bash deploy/oracle_setup.sh"

# ── 1. Docker ────────────────────────────────────────────────────────────
install_docker() {
  if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
    log "Docker already present: $(docker --version)"
    return
  fi

  # Ubuntu and Oracle Linux 9 need slightly different package names.
  local os_id os_version codename
  os_id="$(. /etc/os-release && printf '%s' "${ID:-unknown}")"
  os_version="$(. /etc/os-release && printf '%s' "${VERSION_ID:-}")"
  codename="$(. /etc/os-release && printf '%s' "${VERSION_CODENAME:-}")"

  log "Installing Docker (${os_id} ${os_version})"

  if command -v dnf >/dev/null 2>&1; then
    # Oracle Linux. dnf's python3-docker dependency needs the appstream repo on
    # a minimal image; without it `dnf install docker` fails confusingly.
    dnf install -y dnf-plugins-core || true
    if ! dnf repolist --enabled 2>/dev/null | grep -q ol9_appstream; then
      dnf config-manager --set-enabled ol9_appstream || \
        warn "could not enable ol9_appstream; the Docker CE repo below may still work"
    fi
    dnf install -y dnf-utils
    dnf config-manager --add-repo https://download.docker.com/linux/centos/docker-ce.repo
    dnf install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
  elif command -v apt-get >/dev/null 2>&1; then
    apt-get update -qq
    apt-get install -y ca-certificates curl
    install -m 0755 -d /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/$(. /etc/os-release && printf '%s' "$ID")/gpg \
      | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
    chmod a+r /etc/apt/keyrings/docker.gpg
    # shellcheck disable=SC1091
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/$(. /etc/os-release && printf '%s' "$ID") $(. /etc/os-release && printf '%s' "$VERSION_CODENAME") stable" \
      > /etc/apt/sources.list.d/docker.list
    apt-get update -qq
    apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
  else
    die "no supported package manager found (need apt-get or dnf)"
  fi

  systemctl enable --now docker
  log "Docker installed: $(docker --version)"
}

# ── 2. Firewall ──────────────────────────────────────────────────────────
open_ports() {
  if ! command -v ufw >/dev/null 2>&1; then
    warn "ufw not present. Open 80/443 in your VCN security list instead:"
    warn "  Networking > Virtual Cloud Networks > VCN > Subnets > Security Lists"
    warn "  add ingress rules for TCP 80 and TCP 443 from 0.0.0.0/0"
    return
  fi
  log "Opening 80/443 in ufw"
  ufw allow 80/tcp  >/dev/null
  ufw allow 443/tcp >/dev/null
  ufw allow 443/udp >/dev/null   # HTTP/3
  # ufw ships disabled by default on Oracle images. Enabling it without SSH
  # would lock you out of the machine, so refuse rather than risk it.
  if ufw status | grep -q "Status: active"; then
    ufw status verbose
  else
    warn "ufw is installed but inactive — leaving it alone."
    warn "Enable it with 'ufw allow OpenSSH && ufw enable' or you can skip it and"
    warn "rely on the VCN security list alone."
  fi
}

# ── 3. Password hash ─────────────────────────────────────────────────────
seed_password() {
  [[ -f $ENV_FILE ]] || cp "$ENV_EXAMPLE" "$ENV_FILE"

  if grep -qE '^CADDY_HASH=.+' "$ENV_FILE" && \
     ! grep -q 'PASTE_THE_HASH_HERE' "$ENV_FILE"; then
    log "CADDY_HASH already set in deploy/.env, leaving it alone"
    return
  fi

  read -r -s -p "Password for the web UI (Caddy basic auth): " PASSWORD
  echo
  [[ -n $PASSWORD ]] || die "empty password; the agent must not be published unauthenticated"

  log "Hashing the password"
  local hash
  hash="$(docker run --rm caddy:2 caddy hash-password --plaintext "$PASSWORD")"
  [[ $hash == \$2* ]] || die "unexpected hash output: $hash"

  # Portable in-place edit: BSD sed (macOS) and GNU sed disagree about -i.
  local tmp
  tmp="$(mktemp)"
  awk -v h="$hash" '/^CADDY_HASH=/{print "CADDY_HASH=" h; next} {print}' \
      "$ENV_FILE" > "$tmp" && mv "$tmp" "$ENV_FILE"

  unset PASSWORD
  log "CADDY_HASH written to deploy/.env"
}

# ── 4. Sanity report ─────────────────────────────────────────────────────
report() {
  log "Environment"
  printf '  OS        : %s\n' "$(. /etc/os-release && printf '%s %s' "$PRETTY_NAME" "$VERSION_ID")"
  printf '  arch      : %s\n' "$(uname -m)"
  printf '  CPUs      : %s\n' "$(nproc)"
  printf '  memory    : %s\n' "$(free -h | awk '/^Mem:/{print $2}')"
  printf '  Docker    : %s\n' "$(docker --version)"
  printf '  disk free : %s\n' "$(df -h / | awk 'NR==2{print $4}')"

  if [[ "$(uname -m)" == "aarch64" ]]; then
    log "arm64 host detected — the Dockerfile builds the arm64 Playwright image"
  fi

  local mem_gb
  mem_gb="$(free -g | awk '/^Mem:/{print $2}')"
  if [[ ${mem_gb:-0} -lt 2 ]]; then
    warn "less than 2GB RAM. Chromium plus three Python servers needs about 1.5GB."
  fi

  if [[ -f $ENV_FILE ]]; then
    local domain
    domain="$(grep -E '^DOMAIN=' "$ENV_FILE" | cut -d= -f2- || true)"
    if [[ -n $domain ]]; then
      log "Before deploying"
      printf '  Point the DNS A record for %s at this machine:\n' "$domain"
      printf '    your public IPv4: %s\n' "$(curl -4 -fsS --max-time 10 https://ifconfig.me || echo '<could not determine>')"
      printf '  Caddy cannot get a certificate until that resolves, or it will\n'
      printf '  retry forever and the site stays on the HTTP redirect.\n'
    fi
  fi
}

install_docker
open_ports
seed_password
report

cat <<'EOF'

Next:
  1. Point your DNS A record at this machine's public IPv4.
  2. Edit deploy/.env — at minimum DOMAIN.
  3. bash deploy/deploy.sh
EOF