#!/usr/bin/env bash
# Shared helpers for setup scripts. Source this, don't execute it.
set -euo pipefail

C_G=$'\033[1;32m'; C_Y=$'\033[1;33m'; C_R=$'\033[1;31m'; C_0=$'\033[0m'
info() { printf '%s==>%s %s\n' "$C_G" "$C_0" "$*"; }
warn() { printf '%s==>%s %s\n' "$C_Y" "$C_0" "$*"; }
die()  { printf '%s==>%s %s\n' "$C_R" "$C_0" "$*" >&2; exit 1; }

# Repo root = parent of the directory this script was sourced from.
REPO_ROOT="$(cd "$(dirname "$(realpath "${BASH_SOURCE[1]}")")/.." && pwd)"

need() { command -v "$1" >/dev/null 2>&1 || die "missing required command: $1 (see setup/00-preflight.sh)"; }

# Load .env into the environment and sanity-check the required values.
load_env() {
  [[ -f "$REPO_ROOT/.env" ]] || die ".env not found — run: make secrets"
  # shellcheck disable=SC1091
  set -a; source "$REPO_ROOT/.env"; set +a
  [[ -n "${NAMESILO_API_KEY:-}" && "${NAMESILO_API_KEY:-}" != "REPLACE_ME" ]] \
    || die "Set NAMESILO_API_KEY in .env (NameSilo → Domain Manager → API)"
  [[ -n "${ACME_EMAIL:-}" && "${ACME_EMAIL:-}" != "__ACME_EMAIL__" ]] \
    || die "Set ACME_EMAIL in .env"
  [[ -n "${PANEL_TOKEN:-}" && "${PANEL_TOKEN:-}" != "__PANEL_TOKEN__" ]] \
    || die "PANEL_TOKEN missing in .env — run: make secrets"
}

# Idempotently ensure the non-root 'panel' user exists (runs the host API).
ensure_panel_user() {
  if ! id panel >/dev/null 2>&1; then
    info "Creating system user 'panel'"
    sudo useradd -r -s /usr/sbin/nologin -d /opt/panel-api panel
  fi
}

# ── ACME staging → production promotion ─────────────────────────────────────
# Used by 02-deploy-stack.sh (auto, after the stack is up) and
# 06-acme-promote.sh (manual, `make promote`). First bring-up runs on LE
# staging so DNS-01 debugging can't hit prod's 5-failures/hour rate limit.

ACME_STAGING_URL="https://acme-staging-v02.api.letsencrypt.org/directory"
ACME_PROD_URL="https://acme-v02.api.letsencrypt.org/directory"

acme_mode() { # <deployed-traefik.yml> → echo: staging | prod | unknown
  local f="$1"
  if [[ ! -f "$f" ]]; then
    echo unknown
  elif sudo grep -q "$ACME_STAGING_URL" "$f"; then
    echo staging
  elif sudo grep -q "$ACME_PROD_URL" "$f"; then
    echo prod
  else
    echo unknown
  fi
}

acme_cert_issued() { # <target-dir> → exit 0 if the ACME store already holds a cert
  sudo docker compose --project-directory "$1" -f "$1/docker-compose.yml" \
    exec -T traefik cat /letsencrypt/acme.json 2>/dev/null \
    | python3 -c 'import json,sys
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(1)  # no store yet (or traefik down) — nothing issued
sys.exit(0 if (d.get("letsencrypt") or {}).get("certificates") else 1)'
}

acme_wait_for_cert() { # <target-dir> <timeout-seconds> — poll until a cert lands
  local deadline=$(( $(date +%s) + $2 ))
  while (( $(date +%s) < deadline )); do
    acme_cert_issued "$1" && return 0
    sleep 10
  done
  acme_cert_issued "$1"
}

acme_promote() { # <target-dir> — flip staging→prod. Idempotent; wipes the ACME store.
  local target="$1"
  local mode="$(acme_mode "$target/traefik/traefik.yml")"
  if [[ "$mode" == prod ]]; then
    info "ACME already on production — nothing to promote"
    return 0
  fi
  [[ "$mode" == staging ]] || die "cannot determine ACME mode from $target/traefik/traefik.yml"

  info "Checking the staging cert actually issued before promoting"
  acme_cert_issued "$target" \
    || die "no certificate in the ACME store yet — wait for issuance (make logs), then re-run"

  info "Staging cert issued ✓ — switching ACME to production"
  # Flip configs first: the running traefik only re-reads static config at
  # start, so this is inert until the restart below. Keeps the downtime window
  # (stop → wipe → up) down to two compose commands.
  sudo sed -i "s|$ACME_STAGING_URL|$ACME_PROD_URL|g" "$target/traefik/traefik.yml"
  if [[ -f "$REPO_ROOT/.env" ]]; then
    sed -i "s|$ACME_STAGING_URL|$ACME_PROD_URL|g" "$REPO_ROOT/.env"
    sudo install -m 600 "$REPO_ROOT/.env" "$target/.env"
  fi

  local compose=(sudo docker compose --project-directory "$target" -f "$target/docker-compose.yml")
  # The ACME store is staging-CA-specific: its account URI doesn't exist on
  # prod, and reusing it fails JWS verification. Wipe it (staging certs are
  # worthless) while traefik is down; prod registers a fresh account.
  "${compose[@]}" stop traefik
  "${compose[@]}" run --rm -T --entrypoint /bin/sh traefik -c 'rm -f /letsencrypt/acme.json' \
    || warn "could not wipe the ACME store — if the prod cert never issues, remove it:\n         sudo docker run --rm -v panel_acme:/letsencrypt alpine rm -f /letsencrypt/acme.json"
  "${compose[@]}" up -d traefik

  info "Waiting for the production cert (usually a couple of minutes)"
  if acme_wait_for_cert "$target" "${ACME_PROMOTE_WAIT:-300}"; then
    info "production cert issued — https is trusted now (hard-refresh the browser)"
  else
    warn "no production cert after ${ACME_PROMOTE_WAIT:-300}s — traefik keeps retrying; check: make logs"
  fi
}
