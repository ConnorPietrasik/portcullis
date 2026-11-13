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
searxng_secret="$(openssl rand -hex 32)"

sed -i \
  -e "s|__PANEL_TOKEN__|${panel_token}|g" \
  -e "s|__AUTHELIA_JWT_SECRET__|${jwt}|g" \
  -e "s|__AUTHELIA_SESSION_SECRET__|${sess}|g" \
  -e "s|__AUTHELIA_STORAGE_ENCRYPTION_KEY__|${enc}|g" \
  -e "s|__SEARXNG_SECRET__|${searxng_secret}|g" \
  "$REPO_ROOT/.env"

# Placeholder-survival assert: the fill list above is hardcoded, so a
# placeholder added to .env.example WITHOUT a sed line here survives verbatim
# into a fresh .env and every other check passes on it (the value is a public
# string — a fill regression fails THIS assert, not the deploy, because the
# token's own line is owned by its sed line). Derive the expected token set
# from the sed list itself; fail when the derivation is empty (the assert is
# then checking nothing) or when the generated .env still carries any __*__
# token (owned → its sed line didn't match; unowned → a token with no sed
# line). Covers __*__ tokens ONLY, not REPLACE_ME: NAMESILO_API_KEY=REPLACE_ME
# is the manual step the warn below points at.
owned_tokens="$(grep -oE 's\|__[A-Z_]+__\|' "$0" | grep -oE '__[A-Z_]+__' | LC_ALL=C sort -u || true)"
if [[ -z "$owned_tokens" ]]; then
  die "placeholder derivation is empty — the sed fill lines above changed shape and this assert now derives nothing from them"
fi
survivors="$(grep -oE '__[A-Z_]+__' "$REPO_ROOT/.env" | LC_ALL=C sort -u || true)"
if [[ -n "$survivors" ]]; then
  die "placeholders survived generation: $(tr '\n' ' ' <<<"$survivors" | sed 's/ $//') — every __*__ token must be filled by a sed line in this script (owned set: $(tr '\n' ' ' <<<"$owned_tokens" | sed 's/ $//'))"
fi

chmod 600 "$REPO_ROOT/.env"

warn "Now edit .env:"
echo "    NAMESILO_API_KEY=...   # NameSilo → Domain Manager → API → generate"
info "ACME_EMAIL is pre-filled (Let's Encrypt notices) — edit if you prefer another inbox"
info "Then: make deploy"
