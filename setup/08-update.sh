#!/usr/bin/env bash
# make update — the one Day-2 command: apply the repo's configs to the
# deployed stack AND float everything that has a line to float (container
# images on the major line, panel-web's base images, npm deps within
# package.json's ranges, panel-api's pip deps within requirements.txt').
# Safety net: doctor precondition → snapshot (images re-tagged, config tree,
# exact panel-api copy, npm files) → npm bump + config sync + pull → rebuild
# + converge → gate (post-update services healthy + panel-api serving + full
# doctor) → automatic rollback of all of it on failure.
# make deploy remains the FIRST-BOOT path; a Day-2 re-run of it applies config
# ungated and warns. Details: docs/RUNBOOK.md → Updates.
#
# Env:
#   UPDATE_HEALTH_TIMEOUT=600   health-gate budget (seconds) — one knob for the
#                               update's final gate and EVERY rollback re-gate:
#                               a recreated radiance instance can't read healthy
#                               until its 5m healthcheck start_period elapses
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
# Preflight: the repo's compose must render with the repo's .env BEFORE any
# state is touched. A required var a new block needs but .env lacks (e.g. a
# ${LLM_API_KEY:?...} in a freshly pulled llm-* block) would otherwise
# surface at step 4 — AFTER the snapshot, the npm bump, and the config sync
# of step 3 already replaced the deployed configs: /opt/panel left
# half-synced (new compose, old .env). Read-only; aborts before the lock, the
# snapshot GC, and the doctor. `make doctor` runs the same render check.
if ! preflight_out="$(sudo docker compose --env-file "$REPO_ROOT/.env" \
    -f "$REPO_ROOT/compose/docker-compose.yml" config -q 2>&1)"; then
  die "preflight: repo compose does not render with the repo .env — nothing was touched
$preflight_out
add the missing key(s) to .env (see .env.example), then re-run: make update"
fi
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
# The pre-update running set — feeds $targets (the gate's expectation list)
# in step 5 and the "removed from compose" note in step 7.
snap_services="$(python3 -c 'import json,sys
for s in json.load(open(sys.argv[1])).get("services", {}):
    print(s)' "$STACK_STATE_FILE")"
# Every service with a container in ANY state, pre-update: distinguishes a
# service ADDED by this update (no container yet → `up` must start it) from
# one the operator deliberately stopped (a stopped container exists → leave
# it down). Captured before the config sync below, which replaces the compose
# file this listing is read against.
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
# The same sync make deploy performs (stack_sync_configs): file content only —
# nothing is recreated yet, and a sync failure aborts with every container
# untouched.
stack_sync_configs /opt/panel
# The router doesn't hairpin: doctor's endpoint checks curl the public names
# from this server and must resolve them locally. The pin (tools/panel-hosts.sh)
# is derived from this checkout's traefik rules, so this step — the only place
# the route set changes — is where it can go stale. Idempotent: no write when
# the managed block already matches. Aborts if a pin is shadowed (set -e).
bash "$REPO_ROOT/tools/panel-hosts.sh"

info "4/7 pulling new images (nothing is touched until every pull succeeds)"
pull_err=""
while IFS= read -r svc; do
  if pull_err="$(stack_compose pull "$svc" 2>&1)"; then
    summary="$(grep -m1 -E 'Digest:|up to date|Pulled' <<<"$pull_err" || echo "pulled")"
    info "  $svc: $summary"
    continue
  fi
  # Local-only images (built on the server rather than pulled) 404 on the
  # registry — skip those, but a real registry/network failure stops the run.
  if grep -qiE 'not found|manifest unknown|pull access denied|denied: requested access' <<<"$pull_err"; then
    warn "  $svc: not on a public registry (local image?) — reusing the local image"
  else
    warn "$pull_err"
    die "pull of $svc failed — the running stack is untouched; fix the registry/network issue and re-run"
  fi
done < <(stack_pull_services)

info "5/7 rebuilding panel-web (bases pulled), converging the services that should be running"
# build --pull re-pulls the FROM bases (node:22-alpine, nginx:stable-alpine):
# a plain build reuses the local bases from the first build. The subsequent
# up --build reuses the fresh build (cache hit); a failure aborts with every
# container untouched.
sudo -v  # re-warm for the `up -d` below: a cold-cache build can outlive the sudo timestamp
stack_compose build --pull
# Converge ONLY the services that should be running after this update: what
# was running before (the snapshot) + services added by this update
# (no container before → first start). Deliberately stopped services stay
# down — an update must not resurrect them. --remove-orphans drops containers
# for services REMOVED from compose (stopped-but-defined services keep
# theirs).
# The "no container before" test tells a NEW service from a deliberately
# stopped one — but a pruned stopped container (raw `docker container prune`)
# destroys that evidence and reads as NEW. On-demand services (restart: "no"
# — the LLM fleet) therefore never auto-start from a missing container: the
# restart policy is the durable signal, and starting the whole fleet at once
# is invalid, not just rude, when instances share cards (ROCR_VISIBLE_DEVICES).
# They still get their image pulled (step 4) and their configs synced (step 3);
# a genuinely NEW instance is deployed the same way and started once with
# `docker compose up -d <svc>` (the panel's Services view then manages it).
defined_services="$(stack_services)"
on_demand="$(stack_compose config --format json | python3 -c 'import json,sys
svcs = json.load(sys.stdin).get("services") or {}
[print(s) for s, v in svcs.items() if isinstance(v, dict) and v.get("restart") == "no"]')" \
  || die "cannot read the deployed compose config (missing env vars? re-run: make deploy)"
targets=""
for s in $defined_services; do
  if grep -qx -- "$s" <<<"$snap_services"; then
    targets+="$s " # was running — must be running after the update
  elif grep -qx -- "$s" <<<"$pre_containers"; then
    : # stopped with a container present — deliberately stopped, leave it down
  elif grep -qx -- "$s" <<<"$on_demand"; then
    if [[ "$s" == llm-* ]]; then
      # llm-* instances must NOT be revived by hand: a raw `up -d` (even with
      # the override) skips the GPU-aware path — no ledger record, no conflict
      # check — and an unassigned instance would land on the base-file fallback
      # cards. The panel's GPUs view is the only sanctioned start.
      info "  $s: no container and restart: \"no\" — left down (a stopped container was pruned, or it is brand-new). Once the update passes, start it from the panel's GPUs view (start picks its cards — see GPU pool)"
    else
      info "  $s: no container and restart: \"no\" — left down (a stopped container was pruned, or it is brand-new). Once the update passes, start it from the panel's Services view (\"defined, no container\") or: cd /opt/panel && docker compose up -d $s"
    fi
  else
    targets+="$s " # no container before and not on-demand — new service added by this update
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

info "7/7 verifying: every service that should be running after the update is healthy again (budget ${UPDATE_HEALTH_TIMEOUT:-600}s)"
# Gate on $targets — the pre-update running set PLUS services this update
# added: a broken NEW app must fail the update (auto-revert) instead of
# passing green. A deliberately stopped service is not in $targets (it must
# not time out the gate), and neither is one REMOVED from compose (its
# container was already dropped; waiting for it would auto-revert the
# removal — a rollback restores it).
for s in $snap_services; do
  if ! grep -qx -- "$s" <<<"$defined_services"; then
    info "  $s: removed from compose — excluded from the gate (a rollback would restore it)"
  fi
done
if ! wait_stack_healthy "${UPDATE_HEALTH_TIMEOUT:-600}" $targets; then
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
