#!/usr/bin/env bash
# Deploy the edge stack (traefik + authelia + panel UI) to /opt/panel.
# Idempotent: re-run freely. Runtime data (acme.json, authelia db) is never touched.
set -euo pipefail
source "$(dirname "$(realpath "$0")")/common.sh"
need docker
load_env

TARGET=/opt/panel

if ! sudo docker network inspect edge >/dev/null 2>&1; then
  info "Creating external docker network 'edge'"
  sudo docker network create edge
fi

info "Copying compose stack to $TARGET (skipping node_modules/dist)"
sudo mkdir -p "$TARGET"
tar -C "$REPO_ROOT/compose" --exclude=node_modules --exclude=dist -cf - . \
  | sudo tar -C "$TARGET" -xf -

info "Writing secrets into deployed configs (repo copies keep placeholders)"
sudo sed -i \
  -e "s|__ACME_EMAIL__|${ACME_EMAIL}|g" \
  -e "s|__ACME_CA_SERVER__|${ACME_CA_SERVER:-https://acme-v02.api.letsencrypt.org/directory}|g" \
  -e "s|__PANEL_TOKEN__|${PANEL_TOKEN}|g" \
  "$TARGET/traefik/traefik.yml" \
  "$TARGET/traefik/dynamic/middlewares.yml"

if sudo grep -RqE '__ACME_EMAIL__|__ACME_CA_SERVER__|__PANEL_TOKEN__' "$TARGET/traefik" "$TARGET/docker-compose.yml" 2>/dev/null; then
  die "secret substitution failed — placeholders remain under $TARGET"
fi

sudo install -m 600 "$REPO_ROOT/.env" "$TARGET/.env"
sudo chmod 600 "$TARGET/traefik/dynamic/middlewares.yml"   # contains PANEL_TOKEN

if [[ ! -f "$TARGET/authelia/users_database.yml" ]]; then
  die "authelia/users_database.yml missing (fresh clone? it's gitignored — hashes stay off GitHub).

Create it from the template:
  cp compose/authelia/users_database.yml.example compose/authelia/users_database.yml
…then generate a hash per user (docs/RUNBOOK.md → Phase 1) and re-run this script."
fi

if sudo grep -q 'REPLACE_WITH_ARGON2_HASH' "$TARGET/authelia/users_database.yml"; then
  die "authelia/users_database.yml still has placeholder passwords.

Generate a hash for each user:
  docker run --rm authelia/authelia:4.39 authelia crypto hash generate argon2 --password 'YOUR-PASSWORD'

…then edit compose/authelia/users_database.yml and re-run this script."
fi

info "Pulling patch updates (images are pinned at the minor line; patches ride in on deploy)"
# --ignore-pull-failures: tolerate images that exist only locally (e.g. a
# locally-built llama-rocm) or a transient registry outage — up -d below still
# works from whatever is cached.
sudo docker compose --project-directory "$TARGET" -f "$TARGET/docker-compose.yml" pull --ignore-pull-failures \
  || warn "image pull failed — continuing with cached images"

info "Building + starting stack (first run pulls images and compiles the frontend)"
# --remove-orphans: services deleted from the compose file (e.g. the old
# socket-proxy) must not keep running after a deploy.
sudo docker compose --project-directory "$TARGET" -f "$TARGET/docker-compose.yml" up -d --build --remove-orphans
sudo docker compose --project-directory "$TARGET" -f "$TARGET/docker-compose.yml" ps

# ── ACME staging → prod auto-promotion ────────────────────────────────────
# First bring-up runs on LE staging (see .env.example) so DNS-01 debugging
# can't hit prod's 5-failures/hour rate limit. Once the staging cert issues,
# flip to prod right here. Skip the wait with ACME_STAGING_WAIT=0; finish a
# timed-out wait later with `make promote` (or just re-run `make deploy`).
if [[ "$(acme_mode "$TARGET/traefik/traefik.yml")" == "staging" ]]; then
  wait_secs="${ACME_STAGING_WAIT:-600}"
  if (( wait_secs > 0 )); then
    info "ACME staging mode — waiting up to ${wait_secs}s for the staging cert"
    info "  (NameSilo DNS-01 propagation is the slow part; tail it with: make logs)"
    if acme_wait_for_cert "$TARGET" "$wait_secs"; then
      acme_promote "$TARGET"
    else
      warn "staging cert still not issued after ${wait_secs}s — traefik keeps retrying"
      warn "once 'make logs' shows it, promote to prod with: make promote"
    fi
  else
    info "ACME staging mode (ACME_STAGING_WAIT=0 — not waiting for the cert)"
    info "once the staging cert has issued, promote to prod with: make promote"
  fi
fi

info "Done. Next steps (see docs/RUNBOOK.md):"
echo "  - DNS: apex A self-creates via panel-ddns; *.pietrasik.top is your manual CNAME → pietrasik.top"
echo "  - certs: first run = LE staging; deploy auto-promotes to prod once it issues (manual: make promote)"
echo "  - auth portal:          https://auth.pietrasik.top"
echo "  - panel:                https://panel.pietrasik.top"
