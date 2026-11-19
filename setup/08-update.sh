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
# The snapshot records repo_root (this checkout's path) + repo_head (git HEAD,
# when it is a repo) in the state file: that registration is how the
# auto-update (panel-do update-run — dashboard button or daily timer) re-runs
# THIS script, detached, via update_maybe_detach (common.sh). The update never
# touches git: the auto path floats the lines; the clone only moves when the
# admin edits it.
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
#   SERVITOR_UPDATE=1           run the servitor (agent VM) update as a
#                               best-effort tail AFTER the host gates pass
#                               (0 = skip). Non-gating: a servitor failure
#                               never rolls back the host stack — the servitor
#                               world rolls back on its own (shape AND image).
#                               It inherits UPDATE_DOCTOR (0 skips its
#                               precheck + verify doctors too).
#                               INVARIANT 9: the scripts this tail
#                               root-execs live in an admin-editable
#                               checkout OUTSIDE panel-do — the bash ones
#                               (deploy/update.sh + its siblings
#                               sync/doctor.sh + deploy/guest/build.sh)
#                               pass managed_script_check (the bash mirror
#                               of checkManagedScript), and the executed
#                               python (deploy/resolve_target.py — run
#                               under python3 by update.sh, its shebang
#                               never the exec path) the file-level
#                               managed_data_check — right here, before the
#                               exec. A failed check skips its update
#                               loudly; it never rolls back the host.
set -euo pipefail
source "$(dirname "$(realpath "$0")")/common.sh"
# panel-do's update-run/-force launch this script with --detach: re-exec in a
# new session and return to the caller in seconds (update_maybe_detach).
update_maybe_detach "$@"
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
# Record the attempt BEFORE any gate (under the lock): from here on the panel
# shows "update in progress" + the run-log tail, and a run that dies at a
# pre-snapshot gate is left `failed` by the gate's die — visible, never
# invisible. snapshot_stack rewrites the state with the full snapshot once it
# has actually captured one.
update_begin_run
# Preflight: the repo's compose must render with the repo's .env BEFORE any
# change is applied. A required var a new block needs but .env lacks (e.g. a
# ${LLM_API_KEY:?...} in a freshly pulled llm-* block) would otherwise
# surface at step 4 — AFTER the snapshot, the npm bump, and the config sync
# of step 3 already replaced the deployed configs: /opt/panel left
# half-synced (new compose, old .env). Read-only; aborts before the doctor,
# the snapshot, and the GC — the run is left `failed` in the state file.
# `make doctor` runs the same render check.
if ! preflight_out="$(sudo docker compose --env-file "$REPO_ROOT/.env" \
    -f "$REPO_ROOT/compose/docker-compose.yml" config -q 2>&1)"; then
  update_record_failure "preflight (repo compose render)" "$preflight_out"
  update_set_status failed
  die "preflight: repo compose does not render with the repo .env — nothing was touched (state: failed)
$preflight_out
add the missing key(s) to .env (see .env.example), then re-run: make update"
fi

if [[ "${UPDATE_DOCTOR:-1}" != "0" ]]; then
  info "precondition: doctor must be green BEFORE updating (a pre-existing failure would auto-revert the whole update)"
  # tee (not plain capture): the detached run's log keeps streaming live, and
  # the file feeds update_record_failure — the banner shows the FAILING
  # checks (a bare tail of doctor's report is mostly PASSes after the failure).
  doc_tmp="$(mktemp)"
  if ! bash "$REPO_ROOT/setup/07-doctor.sh" 2>&1 | tee "$doc_tmp"; then
    update_record_failure "doctor (precondition)" "$(cat "$doc_tmp")"
    update_set_status failed
    rm -f "$doc_tmp"
    die "doctor is not green — the failures above are pre-existing (this run touched nothing — state: failed); fix them, then re-run: make update"
  fi
  rm -f "$doc_tmp"
fi

info "1/7 snapshotting the running stack (rollback anchor: images + configs + panel-api + npm files)"
snapshot_stack
# GC runs AFTER the snapshot is recorded: it protects the live rollback tags
# read from the state file's services, and the attempt record (update_begin_run)
# has no services yet — pruning before the snapshot could delete the LIVE
# snapshot's tags once they age past ROLLBACK_RETAIN_DAYS.
update_gc_old_snapshots
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

info "3/7 syncing the repo's configs to /opt/panel (compose + traefik + authelia + portfolio + searxng + .env)"
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
  # tee — the log keeps streaming live; the file feeds update_rollback_or_die,
  # which records the FAILING checks in the state file's failure field (the
  # banner shows them; the log tail is mostly passes after the failure).
  doc_tmp="$(mktemp)"
  if ! bash "$REPO_ROOT/setup/07-doctor.sh" 2>&1 | tee "$doc_tmp"; then
    update_rollback_or_die "doctor (post-update)" "$(cat "$doc_tmp")"
  fi
  rm -f "$doc_tmp"
fi

# ── servitor (agent VM) update — a best-effort tail, NOT a gate ─────────────
# The host stack is fully updated and green here, and the snapshot above is
# its rollback anchor. The servitor world rolls back on its own (shape AND
# image, its .env re-floated to what is deployed) — a servitor failure must
# therefore neither roll back the host stack nor mark this run failed (a
# "failed" host update would point the operator at make rollback for a host
# that is fine; the VM's state is visible in the panel's servitor section).
# It runs BEFORE update_set_status updated on purpose: the panel keeps showing
# "in progress" for the whole tail (a rebuild is ~40 min — the VM is
# only down for the swap itself), and the host update lock stays held, so a
# second update cannot start into the middle of it. Detection mirrors the
# doctor's tri-state: a route in the repo or deployed tree = installed.
# The servitor update floats pi/extension to the newest release in the same
# major and rebuilds + swaps the guest image when its userland outgrows the
# freshness floor (its .env: IMAGE_MAX_AGE_DAYS) — RUNBOOK → Servitor (agent
# VM). Its own failure message says what its rollback did.
if [[ "${SERVITOR_UPDATE:-1}" == "1" ]]; then
  servitor_defined=0
  grep -rqs "servitor\.pietrasik\.top" "$REPO_ROOT/compose/traefik/dynamic/" 2>/dev/null && servitor_defined=1
  grep -rqs "servitor\.pietrasik\.top" /opt/panel/traefik/dynamic/ 2>/dev/null && servitor_defined=1
  if (( ! servitor_defined )); then
    info "servitor: no route in the repo or deployed tree (not installed) — skipping its update"
  else
    # Re-warm BEFORE any sudo use in this tail — the domstate query below
    # and the update exec: the health gate + doctor above can outlive the
    # sudo timestamp, and a prompt mid-tail would hang a detached
    # auto-update tick. (The managed checks between here and the exec are
    # plain stat/read — no second re-warm needed.)
    sudo -v
    if [[ ! -x /opt/servitor-repo/deploy/update.sh ]]; then
      warn "servitor: route defined but /opt/servitor-repo/deploy/update.sh is missing — skipping its update (first install: servitor INSTALL.md; the doctor's servitor section covers the gap)"
    elif [[ "$(sudo virsh domstate servitor 2>/dev/null || true)" == "shut off" ]]; then
      # Planned downtime is NOT an update trigger: the doctor's tri-state
      # treats a shut-off VM as fine, and an automated tick must not WAKE a
      # VM the operator stopped on purpose (update.sh's image swap ends in
      # `virsh start` — with the precheck doctor skipped via
      # UPDATE_DOCTOR=0 nothing else would stop it). The float lands on the
      # next tick after the VM is back, or manually (sudo make update in
      # /opt/servitor-repo). sudo (as the doctor's own virsh query): the
      # admin need not be in the libvirt group — a bare virsh would
      # silently return empty and this guard would never fire. A query
      # that cannot be made at all (libvirtd down) returns empty and we
      # proceed — update.sh then fails loudly on its own virsh/doctor
      # steps (non-gating, as designed).
      info "servitor: the VM is shut off (planned downtime) — leaving it asleep: its update would wake it via the image swap (start it when you want the float: sudo virsh start servitor — the next tick then applies the update)"
    else
      # INVARIANT 9 on this path: every script the tail root-execs — the
      # bash ones directly (update.sh) or through it (guest/build.sh,
      # sync.sh, doctor.sh) — passes managed_script_check (the bash mirror
      # of panel-do's checkManagedScript) before any exec; the check's
      # detail line(s) are already on the log. The tail is non-gating, so a
      # failed check skips loudly — it never rolls back the host stack (the
      # panel's servitor-* actions fail the same check at their own exec,
      # so a writable checkout is visible from both sides). resolve_target.py
      # is executed code too (update.sh runs it under python3 as root — its
      # shebang is never the exec path): the chain check above covers its
      # replaceability, managed_data_check its file-level writability (an
      # in-place modification of a writable file, which the chain alone does
      # not catch).
      managed_ok=1
      for s in update.sh sync.sh doctor.sh guest/build.sh; do
        if ! managed_script_check "/opt/servitor-repo/deploy/$s"; then
          managed_ok=0
          break
        fi
      done
      if (( managed_ok )) && ! managed_data_check /opt/servitor-repo/deploy/resolve_target.py; then
        managed_ok=0
      fi
      if (( ! managed_ok )); then
        warn "servitor: a managed script in /opt/servitor-repo/deploy (update/sync/doctor/guest-build.sh, or the executed resolve_target.py) failed the managed-script check — a writable path in the checkout chain is a replaceable root script (chmod go-w the offending path, never chown panel; RUNBOOK → Servitor, day-2 note) — skipping its update"
      else
        info "servitor: updating the agent VM (float pi/extension + userland freshness; non-gating — its failure never rolls back the host stack)"
        # VAR=value on sudo's command line: honored when the matched rule is
        # ALL/SETENV (root, and any standard admin rule — %wheel ALL=(ALL) ALL
        # matches the command ALL). A restricted sudoers does not silently
        # drop the variable: sudo REFUSES THE COMMAND ("sorry, you are not
        # allowed to set the following environment variables") — the tail
        # then fails loudly below and skips (non-gating, as designed), and
        # the update stays reachable via `sudo make update` in the checkout
        # (root, no sudo indirection). Wherever update.sh DOES run, its own
        # default keeps UPDATE_DOCTOR=1: the doctor only ever skips on an
        # explicit 0.
        set +e
        sudo UPDATE_DOCTOR="${UPDATE_DOCTOR:-1}" /opt/servitor-repo/deploy/update.sh
        serv_rc=$?
        set -e
        if [[ "$serv_rc" -ne 0 ]]; then
          warn "servitor update failed (rc=$serv_rc) — the host stack is updated and healthy; its output above says what its own rollback did (or: manual attention — panel servitor-status / servitor-doctor, RUNBOOK → Servitor)"
        fi
      fi
    fi
  fi
fi

update_set_status updated
info "summary (before → after):"
report_stack_update "$STACK_STATE_FILE" "$after_ids"
rm -f "$after_ids"
info "panel-api: updated (code + pip within declared ranges)"
info "update complete — snapshot $SNAPSHOT_TS is now the rollback point (make rollback)"
