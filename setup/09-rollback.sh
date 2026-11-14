#!/usr/bin/env bash
# make rollback [WHAT=...] — restore the edge stack to the snapshot taken at
# the last `make update` (state: /opt/panel/.update-state). With NO WHAT it
# restores the deployed configs, container images, panel-api (exact copy incl.
# venv), and the npm files — all from LOCAL snapshots, so it works on a dead
# connection. Each component rolls back best-effort; a failure is reported as
# PARTIAL and the remaining snapshots stay intact for a re-run.
#
# With WHAT it rolls back IMAGES ONLY: the matching services are re-created on
# their pre-update images (the same compose-override machinery) while configs,
# panel-api, npm files, and every non-matching service stay in place. The value
# is matched against each snapshot service by NAME (exact, or as a prefix) and
# by the pre-update IMAGE name (substring):
#   make rollback WHAT=radiance   every instance of that image
#   make rollback WHAT=llm        the llm- prefix → the whole radiance fleet
#   make rollback WHAT=llm-27b    that one instance
# That is the fix for an image update that passed the health + doctor gates
# but changed BEHAVIOR (the gates only see health — a broken new `latest`
# build is green to both). The next `make update` re-snapshots the reverted
# build as the new anchor.
#
# Like 08-update.sh, it accepts --detach as the first arg (panel-do's
# update-rollback action): re-exec in a new session, log to
# /opt/panel/.update-run.log, return to the caller in seconds.
set -euo pipefail
source "$(dirname "$(realpath "$0")")/common.sh"
update_maybe_detach "$@"
need docker
need flock
need python3
[[ -f "$STACK_STATE_FILE" ]] \
  || die "no snapshot at $STACK_STATE_FILE — nothing to roll back to (run: make update)"

ts="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("snapshot","?"))' "$STACK_STATE_FILE" 2>/dev/null)" \
  || die "state file $STACK_STATE_FILE is unreadable — it is corrupted; delete it and re-run: make update"
n="$(python3 -c 'import json,sys; print(len(json.load(open(sys.argv[1])).get("services",{})))' "$STACK_STATE_FILE")"
(( n > 0 )) || die "snapshot $ts records no services — nothing to roll back to"

# ── targeted image rollback: make rollback WHAT=<name|prefix|image-name> ────
# Only the matching services' images are reverted; nothing else is touched, so
# nothing else is restored either — a config-level change is a FULL rollback's
# job.
target="${1:-}"
if [[ -n "$target" ]]; then
  selected="$(python3 - "$STACK_STATE_FILE" "$target" <<'PYEOF'
import json, sys
arg = sys.argv[2]
d = json.load(open(sys.argv[1]))
for s, i in sorted(d.get("services", {}).items()):
    if s == arg or s.startswith(arg) or arg in i.get("image_tags", ""):
        print(s)
PYEOF
)"
  if [[ -z "$selected" ]]; then
    avail="$(python3 -c '
import json, sys
d = json.load(open(sys.argv[1]))
for s, i in sorted(d.get("services", {}).items()):
    tags = i.get("image_tags", "").strip()
    if not tags:
        tags = "(no repo:tag recorded — service names only)"
    print(f"  {s}   {tags}")
' "$STACK_STATE_FILE")"
    die "no snapshot services match '$target' — it is matched against the service name (exact or prefix) and the pre-update image name (substring). Snapshot $ts holds:
$avail"
  fi
  # A selected service the CURRENT compose no longer defines cannot be
  # re-created — warn + skip (a full `make rollback` restores it with its
  # configs).
  defined="$(stack_services | sort)"
  keep="" skipped=""
  while IFS= read -r s; do
    if grep -qx -- "$s" <<<"$defined"; then
      keep+="$s "
    else
      skipped+="$s "
    fi
  done <<<"$selected"
  for s in $skipped; do
    warn "$s: was running pre-update but is no longer in the current compose — skipped (a full \`make rollback\` restores it with its configs)"
  done
  [[ -n "$keep" ]] \
    || die "every match is gone from the current compose — nothing to roll back (a full \`make rollback\` would restore them with their configs)"
  info "Targeted rollback to snapshot $ts: images of ${keep% } only — configs, panel-api, npm files, and every other service stay in place"
  sudo -v
  update_acquire_lock
  # Subshell: apply_snapshot's die (e.g. a rollback image was pruned) must not
  # abort before the message below — record the failure instead.
  ( apply_snapshot "$STACK_STATE_FILE" "${UPDATE_HEALTH_TIMEOUT:-600}" $keep ) \
    || die "targeted rollback FAILED — the snapshot is intact, fix the problem and re-run: make rollback WHAT=$target
  make logs     # the failing service's logs are the place to start"
  update_set_status partially-rolled-back
  info "targeted rollback complete — check the behavior, then: make doctor"
  exit 0
fi

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
( apply_snapshot "$STACK_STATE_FILE" "${UPDATE_HEALTH_TIMEOUT:-600}" ) || failed+=("container images")
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
