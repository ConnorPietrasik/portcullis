#!/usr/bin/env bash
# make update — the ONE Day-2 command: apply the repo's configs to the
# deployed stack AND float everything that has a line to float.
# (make deploy is the first-boot path — network, secrets, ACME staging→prod,
# first build; a Day-2 re-run of it applies config ungated and warns.)
#
#   - stack configs — the same repo → /opt/panel sync as make deploy:
#     compose/traefik/authelia/portfolio + .env, secrets substituted, stale
#     route files pruned, the panel-uid services' reduced env files re-derived
#   - container images on the major line (traefik:v3, authelia/authelia:4,
#     nginx:stable-alpine — compose/docker-compose.yml)
#   - panel-web's frontend: build context synced from the repo, npm deps
#     bumped within the declared ranges, base images pulled (node:22-alpine,
#     nginx:stable-alpine — compose/panel-frontend/Dockerfile)
#   - panel-api: main.py + requirements.txt + unit re-deployed, pip deps
#     upgraded within the declared ranges, restarted
# What this adds over a bare pull+up:
#   0. precondition: `make doctor` green (UPDATE_DOCTOR=0 skips it) — a
#      pre-existing doctor failure would otherwise auto-revert the update
#   1. snapshot EVERYTHING about to change — image IDs re-tagged
#      portcullis-rollback/<svc>:<ts>, the deployed config tree (panel.tar,
#      runtime data excluded), an exact copy of /opt/panel-api (venv
#      included), the npm files — under /opt/panel/.update-snapshot-<ts>,
#      state in /opt/panel/.update-state
#   2. npm bump + config sync + pull — a failure stops the run before any
#      container is touched
#   3. rebuild (bases pulled) + converge the services that should be running
#      (the pre-update running set + what this update added; deliberately
#      stopped services stay down, removed ones' containers are dropped) +
#      restart on start-time-only config change, update panel-api
#   4. gate: every service that was running before the update healthy (a
#      deliberately stopped — or removed — service can't fail the gate) +
#      panel-api serving + a full `make doctor`
#   5. gate fails → automatic rollback of all of the above (re-gated)
#
# After a successful update, `make rollback` reverts to the pre-update snapshot
# (one generation); the next `make update` replaces it. Env:
#   UPDATE_HEALTH_TIMEOUT=600   health-gate budget (seconds)
#   UPDATE_ROLLBACK_TIMEOUT=300 rollback re-gate budget (seconds)
#   UPDATE_DOCTOR=1             run the doctor gate (0 = skip). A green `make
#                               doctor` before updating is the precondition —
#                               a pre-existing doctor failure would auto-revert
#                               every update.
set -euo pipefail
source "$(dirname "$(realpath "$0")")/common.sh"
need docker
need flock
need npm
need systemctl
need curl
need python3
[[ -f /opt/panel/docker-compose.yml ]] || die "no deployed stack at /opt/panel — run: make deploy"
[[ -d /opt/panel-api && -x /opt/panel-api/venv/bin/pip ]] \
  || die "panel-api not installed (or its venv is missing) — run: make api"
load_env

sudo -v
update_acquire_lock
update_gc_old_snapshots

if [[ "${UPDATE_DOCTOR:-1}" != "0" ]]; then
  info "precondition: doctor must be green BEFORE updating (a pre-existing failure would auto-revert the whole update)"
  if ! bash "$REPO_ROOT/setup/07-doctor.sh"; then
    die "doctor is not green — the failures above are pre-existing (this run touched nothing); fix them, then re-run: make update"
  fi
fi

info "1/7 snapshotting the running stack (rollback anchor: images + configs + panel-api + npm files)"
snapshot_stack
# The pre-update running set (the gate's expectation list, step 7).
snap_services="$(python3 -c 'import json,sys
for s in json.load(open(sys.argv[1])).get("services", {}):
    print(s)' "$STACK_STATE_FILE")"
# Every service with a container in ANY state, pre-update: this is what tells
# a service ADDED by this update (no container yet → `up` must start it, step 5)
# from one the operator deliberately stopped (a stopped container exists →
# leave it down). Captured before the config sync below, which replaces the
# compose file this listing is read against.
pre_containers="$(stack_compose ps --all --format '{{.Service}}' 2>/dev/null | sort -u || true)"
snapshot_dir="$(update_snapshot_dir "$SNAPSHOT_TS")"
snapshot_host_stack "$snapshot_dir"
info "snapshot $SNAPSHOT_TS recorded in $STACK_STATE_FILE (undo: make rollback)"

info "2/7 bumping the panel frontend's npm deps (within the declared ranges — a major is a deliberate package.json edit)"
# BEFORE the config sync (step 3): the sync ships the repo's panel-frontend —
# including the just-bumped lockfile — to /opt/panel, and the build below uses
# that copy. (A fresh clone has no lockfile; this step generates it.)
npm_bump_panel_frontend

info "3/7 syncing the repo's configs to /opt/panel (compose + traefik + authelia + portfolio + .env)"
# The same sync make deploy performs (stack_sync_configs, common.sh): file
# content only — nothing is recreated yet, and a sync failure aborts with
# every container untouched. .env is re-copied, so an .env edit is applied
# here (and re-derives both panel-uid reduced env files).
stack_sync_configs /opt/panel

info "4/7 pulling new images (nothing is touched until every pull succeeds)"
pull_err=""
while IFS= read -r svc; do
  if pull_err="$(stack_compose pull "$svc" 2>&1)"; then
    summary="$(grep -m1 -E 'Digest:|up to date|Pulled' <<<"$pull_err" || echo "pulled")"
    info "  $svc: $summary"
    continue
  fi
  # Local-only images (e.g. a self-built llama-rocm) 404 on the registry —
  # skip those, but a real registry/network failure still stops the run.
  if grep -qiE 'not found|manifest unknown|pull access denied|denied: requested access' <<<"$pull_err"; then
    warn "  $svc: not on a public registry (local image?) — reusing the local image"
  else
    warn "$pull_err"
    die "pull of $svc failed — the running stack is untouched; fix the registry/network issue and re-run"
  fi
done < <(stack_pull_services)

info "5/7 rebuilding panel-web (bases pulled), converging the services that should be running"
# build --pull re-pulls the FROM bases (node:22-alpine, nginx:stable-alpine for
# panel-web): a plain build reuses the local bases from the first build and lets
# the panel's base images go stale. The subsequent up --build reuses the fresh
# build (cache hit); a build/base-pull failure aborts with every container
# untouched.
sudo -v  # re-warm for the `up -d` below: a cold-cache build can outlive the sudo timestamp
stack_compose build --pull
# Converge ONLY the services that should be running after this update:
#   - what was running before (the snapshot) — compose recreates the ones whose
#     definition/image changed, keeps the rest
#   - services added by this update (no container before → first start; this is
#     the "add an app → make update" flow)
# Deliberately stopped services (a container exists, but they weren't running
# at the snapshot) are left down — an update must not resurrect what the
# operator stopped. (A crashed service lands in the same bucket: it stays down
# here, and the doctor precondition/gate is what catches it — a bare `make
# deploy` still starts everything, ungated.)
# --remove-orphans drops containers for services REMOVED from compose. compose
# defines "orphan" as "service not in the compose file" (independent of the
# explicit selection above), so stopped-but-defined services keep their
# containers.
defined_services="$(stack_services)"
targets=""
for s in $defined_services; do
  if grep -qx -- "$s" <<<"$snap_services" || ! grep -qx -- "$s" <<<"$pre_containers"; then
    targets+="$s "
  fi
done
if [[ -n "$targets" ]]; then
  stack_compose up -d --build --remove-orphans $targets
else
  info "no services to start (the whole stack is deliberately stopped) — images + configs updated, nothing started"
fi
# authelia + traefik read some files only at container start: restart them if
# their content changed in the sync above (no change → no downtime).
stack_restart_if_configs_changed /opt/panel

# AFTER the rebuild: the rebuilt panel-web image is what the report should
# show (captured pre-build it would always print "unchanged" for it).
after_ids="$(mktemp)"
stack_image_ids >"$after_ids"

info "6/7 updating panel-api (code + unit + pip within the declared ranges + restart)"
if ! update_panel_api; then
  update_rollback_or_die "panel-api update — $PANEL_API_REASON"
fi

info "7/7 verifying: every service that was running before the update is healthy again (budget ${UPDATE_HEALTH_TIMEOUT:-600}s)"
# Gate on the snapshot's services (what was running), not every service in the
# compose file: a deliberately stopped service must not time out the gate and
# auto-revert a healthy update (the doctor gate still covers the core stack).
# A service REMOVED from compose by this update is intentionally gone — its
# container was already dropped by --remove-orphans above, so waiting for it
# would burn the whole budget and auto-revert the removal. (A rollback
# restores it.)
gate_services=""
for s in $snap_services; do
  if grep -qx -- "$s" <<<"$defined_services"; then
    gate_services+="$s "
  else
    info "  $s: removed from compose — excluded from the gate (a rollback would restore it)"
  fi
done
if ! wait_stack_healthy "${UPDATE_HEALTH_TIMEOUT:-600}" $gate_services; then
  update_rollback_or_die "health gate — $HEALTH_GATE_REASON"
fi

if [[ "${UPDATE_DOCTOR:-1}" != "0" ]]; then
  info "verifying: full doctor pass (every RUNBOOK check, including panel-api)"
  if ! bash "$REPO_ROOT/setup/07-doctor.sh"; then
    update_rollback_or_die "doctor gate"
  fi
fi

update_set_status updated
info "summary (before → after):"
report_stack_update "$STACK_STATE_FILE" "$after_ids"
rm -f "$after_ids"
info "panel-api: updated (code + pip within declared ranges)"
info "update complete — snapshot $SNAPSHOT_TS is now the rollback point (make rollback)"
