#!/usr/bin/env bash
# Deploy the edge stack (traefik + authelia + panel UI) to /opt/panel.
# Idempotent: re-run freely. Runtime data (acme.json, authelia db) is never touched.
set -euo pipefail
source "$(dirname "$(realpath "$0")")/common.sh"
need docker
load_env

# Preflight: same render check as make update's — a required var .env lacks
# must be caught BEFORE the network is created and the configs copied, not
# at `up -d` after /opt/panel is half-populated.
if ! preflight_out="$(sudo docker compose --env-file "$REPO_ROOT/.env" \
    -f "$REPO_ROOT/compose/docker-compose.yml" config -q 2>&1)"; then
  die "preflight: repo compose does not render with the repo .env — nothing was touched
$preflight_out
add the missing key(s) to .env (see .env.example), then re-run: make deploy"
fi

TARGET=/opt/panel

if [[ -f "$TARGET/docker-compose.yml" ]]; then
  warn "a stack is already deployed at $TARGET — Day-2 changes should go through `make update`
(snapshot + health gate + auto-rollback); this deploy applies them ungated"
fi

# Copy the repo's compose tree + secrets to $TARGET (shared with make update).
stack_sync_configs "$TARGET"

# LLM GPU pool override: the ledger's projection as a compose -f file
# (ROCR_VISIBLE_DEVICES per llm-* instance). panel-api re-renders it on every
# assignment; COMPOSE_STACK (make deploy / update / rollback / panel-do)
# always includes it — the `up -d` below renders through it too — so create
# it BEFORE the first `up -d`: a missing file would break every compose
# render, and a render WITHOUT it would recreate fleet instances on the
# base-file fallback cards instead of their assigned sets. `make api` (03)
# re-derives ownership (panel).
sudo mkdir -p /opt/panel-api
if [[ ! -f /opt/panel-api/llm-gpu-override.yml ]]; then
  printf 'services: {}\n' | sudo tee /opt/panel-api/llm-gpu-override.yml >/dev/null
fi
sudo chmod 644 /opt/panel-api/llm-gpu-override.yml

info "Building + starting stack (first run pulls images and compiles the frontend)"
# Deliberately NO `pull` here: the edge images float on the major line, so a
# pull is an *upgrade* of the public edge — that happens in `make update`
# (snapshot + gate + auto-rollback), not in a routine config deploy. (If a
# manual tag edit references an image not yet local, `up -d` pulls just that
# one.) --remove-orphans drops containers for services deleted from compose.
# stack_compose (not a raw `docker compose`): includes the GPU override, so
# a Day-2 re-run recreates fleet instances on their ASSIGNED card sets.
stack_compose up -d --build --remove-orphans

# Restart services whose start-time-only config content changed (snapshot
# taken by stack_sync_configs). No change → no restart.
stack_restart_if_configs_changed "$TARGET"
stack_compose ps

# Tighten the TOTP store on first boot: authelia creates db.sqlite3 0644,
# AFTER stack_sync_configs' chmod window (the file doesn't exist yet at sync
# time). stack_sync_configs re-tightens it on every later sync.
for _ in 1 2 3 4 5 6 7 8 9 10; do
  [[ -f "$TARGET/authelia/db.sqlite3" ]] && break
  sleep 1
done
if [[ -f "$TARGET/authelia/db.sqlite3" ]]; then
  sudo chmod 600 "$TARGET/authelia/db.sqlite3"
else
  warn "authelia db.sqlite3 not created after ~10s — the TOTP store would stay 0644 (world-readable) until the next deploy/update; check: make logs"
fi

# ── ACME staging → prod auto-promotion ────────────────────────────────────
# First bring-up runs on LE staging (see .env.example) so DNS-01 debugging
# can't hit prod's rate limit. Once the staging cert issues, flip to prod
# right here. Skip the wait with ACME_STAGING_WAIT=0; finish a timed-out wait
# later with `make promote` (or just re-run `make deploy`).
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
