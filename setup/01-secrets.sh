#!/usr/bin/env bash
# Generate .env with fresh secrets. Existing .env is left untouched (delete it to regenerate).
set -euo pipefail
source "$(dirname "$(realpath "$0")")/common.sh"
need openssl

if [[ -f "$REPO_ROOT/.env" ]]; then
  warn ".env already exists — leaving it alone (delete it first if you really want to regenerate)"
  exit 0
fi

info "Generating .env from .env.example"
cp "$REPO_ROOT/.env.example" "$REPO_ROOT/.env"

panel_token="$(openssl rand -hex 32)"
jwt="$(openssl rand -hex 32)"
sess="$(openssl rand -hex 32)"
enc="$(openssl rand -hex 32)"

sed -i \
  -e "s|__PANEL_TOKEN__|${panel_token}|g" \
  -e "s|__AUTHELIA_JWT_SECRET__|${jwt}|g" \
  -e "s|__AUTHELIA_SESSION_SECRET__|${sess}|g" \
  -e "s|__AUTHELIA_STORAGE_ENCRYPTION_KEY__|${enc}|g" \
  "$REPO_ROOT/.env"

chmod 600 "$REPO_ROOT/.env"

warn "Now edit .env:"
echo "    NAMESILO_API_KEY=...   # NameSilo → Domain Manager → API → generate"
info "ACME_EMAIL is pre-filled (Let's Encrypt notices) — edit if you prefer another inbox"
info "Then: make deploy"
