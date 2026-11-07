#!/usr/bin/env bash
# Deploy the edge stack (traefik + authelia + panel UI) to /opt/panel.
# Idempotent: re-run freely. Runtime data (acme.json, authelia db) is never touched.
set -euo pipefail
source "$(dirname "$(realpath "$0")")/common.sh"
need docker
load_env

TARGET=/opt/panel

if [[ -f "$TARGET/docker-compose.yml" ]]; then
  warn "a stack is already deployed at $TARGET — Day-2 changes should go through `make update`
(snapshot + health gate + auto-rollback); this deploy applies them ungated"
fi

# Copy the repo's compose tree + secrets to $TARGET (shared with make update).
stack_sync_configs "$TARGET"

info "Building + starting stack (first run pulls images and compiles the frontend)"
# Deliberately NO `pull` here: the edge images float on the major line, so a
# pull is an *upgrade* of the public edge — that happens in `make update`
# (snapshot + gate + auto-rollback), not in a routine config deploy. (If a
# manual tag edit references an image not yet local, `up -d` pulls just that
# one.) --remove-orphans drops containers for services deleted from compose.
sudo docker compose --project-directory "$TARGET" -f "$TARGET/docker-compose.yml" up -d --build --remove-orphans

# Restart services whose start-time-only config content changed (snapshot
# taken by stack_sync_configs). No change → no restart.
stack_restart_if_configs_changed "$TARGET"
sudo docker compose --project-directory "$TARGET" -f "$TARGET/docker-compose.yml" ps

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
