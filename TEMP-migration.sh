#!/usr/bin/env bash
# One-time, ON THE SERVER: state migration for the llm-* -> gpu-* fleet rename.
# Precondition: every instance STOPPED (old GPUs view), panel UI closed
# (a live poll hitting the old API is harmless but pointless). Run as root
# from a checkout of the renamed tree. Refuses to re-run; backups in
# /root/gpu-rename-backup-<ts>/.
set -euo pipefail

API=/opt/panel-api
die() { printf 'FATAL: %s\n' "$*" >&2; exit 1; }
[[ $EUID -eq 0 ]] || die "run as root"
[[ -f "$API/llm-gpu.json" ]] || die "$API/llm-gpu.json missing — already migrated (or wrong host)"
[[ -e "$API/gpu-pool.json" || -e "$API/gpu-pool-override.yml" ]] && die "gpu-pool.* already exist — refusing to re-run"

ts=$(date +%Y%m%d-%H%M%S); bak=/root/gpu-rename-backup-$ts; mkdir -p "$bak"
cp -p "$API/llm-gpu.json" "$bak/llm-gpu.json"
[[ -f "$API/settings.json" ]] && cp -p "$API/settings.json" "$bak/settings.json"
[[ -f "$API/llm-gpu-override.yml" ]] && cp -p "$API/llm-gpu-override.yml" "$bak/llm-gpu-override.yml"

# sed to tmp, cat back over the original — preserves owner (panel) + mode (0600)
rewrite() { sed -E "$2" "$1" > "$1.tmp" && cat "$1.tmp" > "$1" && rm -f "$1.tmp"; }

# 1. ledger: rename + rewrite llm-<name> -> gpu-<name> (allocation keys + autostart list).
#    "llm-" is only ever a JSON string prefix here, so the rewrite is exact.
mv "$API/llm-gpu.json" "$API/gpu-pool.json"
rewrite "$API/gpu-pool.json" 's/"llm-/"gpu-/g'
python3 - "$API/gpu-pool.json" <<'EOF' || die "ledger rewrite failed validation"
import json, sys
d = json.load(open(sys.argv[1]))
bad = [k for k in d.get("allocations", {}) if k.startswith("llm-")] \
    + [s for s in d.get("autostart", []) if s.startswith("llm-")]
sys.exit(f"llm- names remain: {bad}" if bad else 0)
EOF

# 2. LLM fallback order (settings.json): same rewrite, or it names containers
#    that no longer exist and the fallback router reports everything down.
#    Missing file = empty order = nothing to do.
[[ -f "$API/settings.json" ]] && rewrite "$API/settings.json" 's/"llm-/"gpu-/g'

# 3. override: EMPTY on purpose — do NOT rewrite the old one. A gpu-* service
#    that exists only in the override has no image in the OLD deployed compose,
#    so a rewritten file breaks the doctor's pre-update render ("service has
#    neither an image nor a build") and make update dies at the precondition.
#    The file must merely EXIST (every new render -f's it); its content is the
#    ledger's projection and panel-api re-renders it on the first GPUs-view
#    touch — and always before any gpu-apply.
printf 'services: {}\n' > "$API/gpu-pool-override.yml"
chown panel:panel "$API/gpu-pool-override.yml"; chmod 644 "$API/gpu-pool-override.yml"

cat <<DONE
State migrated (backups: $bak). NOW, from this checkout:
  make panel-do && make update
  # the update lands the rename; gpu-* services come up DOWN; the old
  # panel-llm-* containers become orphans
  systemctl disable --now llm-autostart
  rm /etc/systemd/system/llm-autostart.service && systemctl daemon-reload
  rm -f $API/llm_gpus.py $API/llm_autostart.py $API/llm-gpu-override.yml
  rm -rf /opt/panel/llm-configs
  docker rm \$(docker ps -aq --filter name=panel-llm-)   # orphaned old containers
  # open the panel's GPUs view (re-renders the override from the ledger),
  # start the fleet, make doctor
If the update auto-rolls-back: the old code comes back and seeds a fresh EMPTY
llm-gpu.json — card assignments are lost (containers come back via the
snapshot's image tags); re-pin from the GPUs view. $bak restores the old
state if you need to go further back.
DONE
