#!/usr/bin/env bash
# Build and start AI Worker on this machine. Run after deploy/oracle_setup.sh.
#
#   bash deploy/deploy.sh
#
# Re-runnable. The named volume holds the databases, so a redeploy preserves
# every bill the agent has entered.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_FILE="${REPO_ROOT}/deploy/docker-compose.yml"
ENV_FILE="${REPO_ROOT}/deploy/.env"

log() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
die() { printf '\033[31merror: %s\033[0m\n' "$*" >&2; exit 1; }

command -v docker >/dev/null 2>&1 || die "docker is not installed; run deploy/oracle_setup.sh first"
docker compose version >/dev/null 2>&1 || die "the docker compose plugin is missing; run deploy/oracle_setup.sh"

[[ -f $ENV_FILE ]] || die "deploy/.env not found: cp deploy/.env.example deploy/.env"

# Fail here with a useful message rather than letting Caddy die in a loop with
# a parse error nobody reads.
if grep -q 'PASTE_THE_HASH_HERE' "$ENV_FILE"; then
  die "CADDY_HASH is still the placeholder in deploy/.env — run deploy/oracle_setup.sh, or paste a real bcrypt hash"
fi
for var in DOMAIN CADDY_USER CADDY_HASH ACME_EMAIL; do
  grep -qE "^${var}=.+" "$ENV_FILE" || die "${var} is empty in deploy/.env"
done

# A --reset anywhere in .env would wipe the databases on every restart.
grep -qE '^RESET=true' "$ENV_FILE" && die "do not set RESET=true in deploy/.env"

cd "$REPO_ROOT"

# Validate the Caddyfile with the real environment BEFORE building or starting
# anything. Without this, a Caddyfile mistake shows up as a container that
# restarts every few seconds logging nothing useful, long after the deploy
# appeared to succeed. This check is not theoretical: an empty ACME_EMAIL
# collapses the `email` line to a bare directive and Caddy exits 1 with
# "wrong argument count" and no other output.
log "Validating the Caddyfile"
validate_args=()
for var in DOMAIN ACME_EMAIL CADDY_USER CADDY_HASH; do
  validate_args+=(-e "$var=$(grep -E "^${var}=" "$ENV_FILE" | cut -d= -f2-)")
done
# The caddy image ships no ENTRYPOINT, so the first argument is exec'd directly.
# Passing a bare `validate` therefore fails with "executable file not found",
# and passing both --entrypoint caddy *and* a leading `caddy` fails with
# "unknown command". Spelling the binary out is the form that works.
if ! validate_out="$(docker run --rm "${validate_args[@]}" \
      -v "$REPO_ROOT/deploy/Caddyfile:/etc/caddy/Caddyfile:ro" \
      caddy:2 caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile 2>&1)"; then
  printf '%s\n' "$validate_out" | sed 's/^/  /'
  die "the Caddyfile is invalid (see above)"
fi
echo "  Caddyfile OK"

# One machine, always on. A run parks for QUESTION_TIMEOUT_SECONDS waiting for a
# human while the browser holds an SSE connection open; letting Docker restart
# or stop the container mid-run discards that state. `restart: unless-stopped`
# in the compose file covers crashes; this is the manual path.
log "Building the image (this pulls ~2GB of Chromium on first run)"
docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" build --pull

log "Starting"
docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" up -d

DOMAIN="$(grep -E '^DOMAIN=' "$ENV_FILE" | cut -d= -f2-)"

log "Waiting for the agent to report healthy (Chromium's first launch is slow)"
for attempt in $(seq 1 40); do
  status="$(docker inspect --format '{{.State.Health.Status}}' \
    "$(docker compose -f "$COMPOSE_FILE" ps -q aiworker)" 2>/dev/null || echo unknown)"
  case "$status" in
    healthy)
      printf '\n\033[32mReady: https://%s\033[0m\n' "$DOMAIN"
      printf '  user: %s\n' "$(grep -E '^CADDY_USER=' "$ENV_FILE" | cut -d= -f2-)"
      exit 0
      ;;
    unhealthy)
      echo "container reports unhealthy. Last 40 log lines:"
      docker compose -f "$COMPOSE_FILE" logs --tail 40 aiworker
      exit 1
      ;;
  esac
  sleep 5
done

echo "Did not become healthy in 200s. Last 40 log lines:"
docker compose -f "$COMPOSE_FILE" logs --tail 40 aiworker
echo
echo "Common causes:"
echo "  - DNS A record for $DOMAIN does not resolve here yet, so Caddy cannot get a certificate"
echo "  - out of memory; Chromium needs roughly 1.5GB"
exit 1