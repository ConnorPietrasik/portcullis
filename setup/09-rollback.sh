#!/usr/bin/env bash
# make rollback — restore the edge stack to the snapshot taken at the last
# `make update` (state: /opt/panel/.update-state). Restores the deployed
# configs, container images, panel-api (exact copy incl. venv), and the npm
# files — all from LOCAL snapshots, so no registry access is needed: it works
# on a dead connection.
# The state file always holds the *pre-update* snapshot, so this undoes the
# last update — including one that `make update` already auto-reverted (then it
# is a no-op re-create). Each component rolls back best-effort; a failure is
# reported as PARTIAL and the remaining snapshots stay intact for a re-run.
set -euo pipefail
source "$(dirname "$(realpath "$0")")/common.sh"
need docker
need flock
need python3
[[ -f "$STACK_STATE_FILE" ]] \
  || die "no snapshot at $STACK_STATE_FILE — nothing to roll back to (run: make update)"

ts="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("snapshot","?"))' "$STACK_STATE_FILE" 2>/dev/null)" \
  || die "state file $STACK_STATE_FILE is unreadable — it is corrupted; delete it and re-run: make update"
n="$(python3 -c 'import json,sys; print(len(json.load(open(sys.argv[1])).get("services",{})))' "$STACK_STATE_FILE")"
(( n > 0 )) || die "snapshot $ts records no services — nothing to roll back to"
dir="$(update_snapshot_dir "$ts")"

info "Rolling back to snapshot $ts ($n container service(s) + configs + panel-api + npm files, if snapshotted)"
sudo -v
update_acquire_lock
failed=()
# Configs first: the image recreate below must read the PRE-UPDATE compose
# file + .env (the restored ones).
rollback_stack_configs "$dir" || failed+=("configs")
# Subshell: apply_snapshot's die (e.g. a rollback image was pruned) must not
# abort the remaining rollbacks — record it as a failed component instead.
( apply_snapshot "$STACK_STATE_FILE" "${UPDATE_ROLLBACK_TIMEOUT:-300}" ) || failed+=("container images")
rollback_panel_api "$dir" || failed+=("panel-api")
rollback_npm "$dir" || failed+=("npm files")
if (( ${#failed[@]} > 0 )); then
  die "rollback was PARTIAL — not reverted: ${failed[*]}
The remaining snapshots are intact (snapshot dir: $dir, state file: $STACK_STATE_FILE):
fix the problem and re-run: make rollback
  make logs     # the failing service's logs are the place to start"
fi
update_set_status rolled-back
info "rollback complete — verify end-to-end with: make doctor"
