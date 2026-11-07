#!/usr/bin/env bash
# Install panel-ddns: a stdlib-python systemd timer (user 'panel', no root,
# outbound only) that keeps the NameSilo apex A record pointed at the current
# public IPv4. Reuses NAMESILO_API_KEY from /opt/panel/.env via the reduced
# /opt/panel/.panel-ddns.env — no new secrets. Creates the apex A if missing.
set -euo pipefail
source "$(dirname "$(realpath "$0")")/common.sh"
need python3
need systemctl
load_env
ensure_panel_user

# The unit reads NAMESILO_API_KEY via EnvironmentFile=/opt/panel/.panel-ddns.env,
# derived from /opt/panel/.env below and only present after make deploy —
# fail loudly rather than with systemd's opaque "Failed with result 'resources'".
[[ -f /opt/panel/.env ]] || die "/opt/panel/.env missing — run make deploy first (the source for the derived /opt/panel/.panel-ddns.env)"

TARGET=/opt/panel-ddns

info "Installing panel-ddns to $TARGET"
# Root-owned, read-only for the service: the script writes nothing (no state).
sudo install -D -o root -g root -m 0755 "$REPO_ROOT/ddns/ddns.py" "$TARGET/ddns.py"

info "Installing systemd units"
sudo install -o root -g root -m 0644 \
  "$REPO_ROOT/ddns/panel-ddns.service" "$REPO_ROOT/ddns/panel-ddns.timer" /etc/systemd/system/
sudo systemctl daemon-reload
# (Re)derive the reduced env file NOW: makes `make ddns` self-contained — it
# picks up a .env that changed since the last deploy (e.g. a rotated key).
sync_ddns_env /opt/panel
# restart the timer (not `enable --now`): `start` alone is a no-op on an
# active timer and doesn't recover a timer broken by a bad schedule edit
# (systemd#16578) — only stop + start does.
sudo systemctl enable panel-ddns.timer
sudo systemctl restart panel-ddns.timer

info "Running one reconcile now (creates the apex A record if missing)"
if sudo systemctl start panel-ddns.service; then # Type=oneshot: start blocks until done
  info "reconcile ok — apex A record points at the current public IP"
else
  warn "reconcile FAILED — journal excerpt:"
  sudo journalctl -u panel-ddns -n 20 --no-pager || true
  die "fix the issue above and re-run: make ddns (idempotent)"
fi

info "panel-ddns installed (timer: every 10 min + 2 min after boot)"
echo "  status: systemctl status panel-ddns.timer"
echo "  log:    journalctl -u panel-ddns -f"
echo "  manual run: sudo systemctl start panel-ddns.service"
