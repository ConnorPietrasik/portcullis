#!/usr/bin/env bash
# Install the auto-update timer (panel-autoupdate.{service,timer}) — DISABLED
# by design: the first run should be deliberate (the panel's Settings card, or
# `systemctl enable --now panel-autoupdate.timer`). Idempotent; re-run after an
# update that changes the units. The oneshot calls panel-do update-run (root
# into the one privileged path); the gated 08-update.sh does all the work.
set -euo pipefail
source "$(dirname "$(realpath "$0")")/common.sh"
need systemctl

for u in panel-autoupdate.service panel-autoupdate.timer; do
  sudo install -m 644 "$REPO_ROOT/setup/$u" "/etc/systemd/system/$u"
done
sudo systemctl daemon-reload

# Known state every time: a missing unit installs disabled; an already-enabled
# one is left running (disabling it here would surprise a deliberate setup).
if systemctl is-enabled --quiet panel-autoupdate.timer 2>/dev/null; then
  info "panel-autoupdate.timer already enabled — leaving it enabled (disable: panel Settings → auto-update, or systemctl disable --now panel-autoupdate.timer)"
else
  info "panel-autoupdate.timer installed (DISABLED — enable from the panel's Settings, or: systemctl enable --now panel-autoupdate.timer)"
fi
