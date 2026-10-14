#!/usr/bin/env bash
# Preflight: checks the tooling this repo's scripts need. Safe to re-run.
set -euo pipefail
source "$(dirname "$(realpath "$0")")/common.sh"

info "Panel preflight — checking host tooling"
fail=0

for c in docker openssl curl python3 git; do
  if command -v "$c" >/dev/null 2>&1; then info "  ok: $c"; else warn "  MISSING: $c"; fail=1; fi
done

if docker compose version >/dev/null 2>&1; then
  info "  ok: docker compose plugin"
else
  warn "  MISSING: docker compose plugin  (pacman -S docker-compose)"
  fail=1
fi

if command -v go >/dev/null 2>&1; then
  info "  ok: go ($(go version))"
else
  warn "  go not found — needed for panel-do  (pacman -S go)"
fi

if command -v dig >/dev/null 2>&1; then
  info "  ok: dig (panel-ddns detects the public IP with it)"
else
  info "  dig not found (optional — panel-ddns falls back to HTTPS; install bind-tools for parity)"
fi

if command -v node >/dev/null 2>&1; then
  info "  ok: node $(node --version) (optional — only for frontend dev; prod builds run in Docker)"
else
  info "  node not found (optional — prod frontend builds run inside Docker)"
fi

if [[ $EUID -eq 0 ]]; then
  warn "Running as root. Scripts use sudo internally; prefer a regular sudo-capable user."
fi

if sudo -v 2>/dev/null; then
  info "  ok: sudo available"
else
  warn "  sudo unavailable — setup scripts need it"
fi

if command -v systemctl >/dev/null 2>&1; then
  info "  ok: systemd (needed for panel-api.service)"
else
  warn "  systemctl not found — panel-api install step will not work"
fi

# Authelia TOTP and the ACME dance both need a correct clock; skew breaks both
# (browser 401 loop, failed cert issuance).
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

if [[ -d /opt/panel ]]; then
  warn "  /opt/panel already exists — deploy will overwrite managed configs (runtime data is untouched)"
fi

if [[ $fail -ne 0 ]]; then
  die "preflight found missing required tools (see warnings above)"
fi

info "preflight passed — next: make secrets"
