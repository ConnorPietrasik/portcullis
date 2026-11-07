#!/usr/bin/env bash
# Preflight: checks the tooling this repo's scripts need. Safe to re-run.
set -euo pipefail
source "$(dirname "$(realpath "$0")")/common.sh"

info "Panel preflight — checking host tooling"
fail=0

# Sudo first: cache credentials up front so the ufw check below doesn't
# prompt mid-list.
sudo_ok=0
if sudo -v 2>/dev/null; then
  info "  ok: sudo available"
  sudo_ok=1
else
  warn "  MISSING: sudo  (pacman -S sudo) — required; every deploy/doctor step calls sudo"
  fail=1
fi

for c in docker openssl curl python3 git flock; do
  if command -v "$c" >/dev/null 2>&1; then info "  ok: $c"; else warn "  MISSING: $c"; fail=1; fi
done

# Test a real venv creation (ensurepip included), not just module presence.
if command -v python3 >/dev/null 2>&1; then
  venv_test="$(mktemp -d)"
  if python3 -m venv "$venv_test" >/dev/null 2>&1; then
    info "  ok: python3 -m venv (panel-api's venv step)"
  else
    warn "  MISSING: python3-venv (pacman -S python3-venv) — panel-api install will fail at the venv step"
    fail=1
  fi
  rm -rf "$venv_test"
fi

if docker compose version >/dev/null 2>&1; then
  info "  ok: docker compose plugin"
else
  warn "  MISSING: docker compose plugin  (pacman -S docker-compose)"
  fail=1
fi

if command -v go >/dev/null 2>&1; then
  info "  ok: go ($(go version))"
else
  warn "  MISSING: go  (pacman -S go) — panel-do can't build (make panel-do)"
  fail=1
fi

if command -v ufw >/dev/null 2>&1; then
  info "  ok: ufw (required firewall)"
  ufw_state=""
  if (( sudo_ok )); then
    ufw_state="$(sudo ufw status 2>/dev/null | grep '^Status:' || true)"
  fi
  if [[ "$ufw_state" == "Status: active" ]]; then
    info "  ok: ufw active"
  elif [[ -n "$ufw_state" ]]; then
    info "  ufw not active — the deploy needs it active: enable it (sudo ufw enable) before Phase 2 (RUNBOOK → UFW)"
  else
    info "  ufw state unknown (needs working sudo) — re-check before Phase 2 (RUNBOOK → UFW)"
  fi
else
  warn "  MISSING: ufw  (pacman -S ufw) — required; the deploy depends on it"
  fail=1
fi

if command -v dig >/dev/null 2>&1; then
  info "  ok: dig (panel-ddns detects the public IP with it)"
else
  info "  dig not found (optional — panel-ddns falls back to HTTPS; install bind-tools for parity)"
fi

# node/npm are needed for `make update`'s npm range bump only — the deploy
# flow builds the frontend in Docker — so warn, don't fail.
if command -v node >/dev/null 2>&1 && command -v npm >/dev/null 2>&1; then
  info "  ok: node $(node --version) + npm $(npm --version) (make update's npm range bump)"
else
  warn "  MISSING: node/npm (pacman -S nodejs npm) — needed for make update (the deploy flow works without them)"
fi

if [[ $EUID -eq 0 ]]; then
  warn "Running as root. Scripts use sudo internally; prefer a regular sudo-capable user."
fi

if command -v systemctl >/dev/null 2>&1; then
  info "  ok: systemd (needed for panel-api.service)"
else
  warn "  systemctl not found — panel-api install step will not work"
fi

# TOTP and ACME both need a correct clock; skew breaks both.
if command -v timedatectl >/dev/null 2>&1 && timedatectl show -p NTPSynchronized --value >/dev/null 2>&1; then
  if [[ "$(timedatectl show -p NTPSynchronized --value)" == "yes" ]]; then
    info "  ok: clock NTP-synchronized (TOTP 2FA + cert issuance need this)"
  else
    warn "  clock NOT NTP-synchronized — TOTP logins/cert issuance break on skew"
    echo "      fix: sudo timedatectl set-ntp true"
  fi
else
  info "  clock-sync check skipped (timedatectl/systemd unavailable)"
fi

# Traefik needs 80/443. Don't fail here, just flag it early.
if command -v ss >/dev/null 2>&1; then
  listeners="$(ss -tln 2>/dev/null | awk '{print $4}' | grep -E ':(80|443)$' || true)"
  if [[ -n "$listeners" ]]; then
    warn "  something already listens on :80/:443 — Traefik needs both free:"
    echo "$listeners" | sed 's/^/      /'
  else
    info "  ok: ports 80/443 free"
  fi
fi

# panel.yml (traefik route) + panel-api.service (bind) hardcode 172.17.0.1 —
# the docker0 gateway. A non-default bridge = a deploy whose /api 502s forever.
if command -v ip >/dev/null 2>&1; then
  if ip -4 addr show docker0 >/dev/null 2>&1; then
    if ip -4 -o addr show docker0 | awk '{print $4}' | grep -q '^172\.17\.0\.1/'; then
      info "  ok: docker0 is 172.17.0.1 (panel API bind/route IP)"
    else
      warn "  WRONG: docker0 is not 172.17.0.1 — panel.yml + panel-api.service hardcode it:"
      ip -4 -o addr show docker0 | awk '{print "      " $4}'
      echo "      fix: set daemon.json 'bip' to 172.17.0.0/16 (+ restart docker), or update every hardcoded 172.17.0.1 ref together (panel.yml, panel-api.service, 07-doctor.sh, RUNBOOK)"
      fail=1
    fi
  else
    info "  docker0 absent (docker not started?) — it must be 172.17.0.1 (panel API IP); re-check once docker is up"
  fi
fi

if [[ -d /opt/panel ]]; then
  warn "  /opt/panel already exists — deploy will overwrite managed configs (runtime data is untouched)"
fi

if [[ $fail -ne 0 ]]; then
  die "preflight found missing required tools (see warnings above)"
fi

info "preflight passed — next: make secrets"
