#!/usr/bin/env bash
# Install the host-side panel API (FastAPI) as a systemd service.
# Runs as user 'panel', binds 172.17.0.1:8010 (docker0 bridge — reachable from
# the edge network, not from the internet).
set -euo pipefail
source "$(dirname "$(realpath "$0")")/common.sh"
need python3
need systemctl
load_env

[[ -f /opt/panel/.env ]] || die "/opt/panel/.env missing — run: make deploy (panel-api.service reads PANEL_TOKEN from it)"

ensure_panel_user

info "Installing panel-api to /opt/panel-api"
sudo mkdir -p /opt/panel-api
sudo cp -a "$REPO_ROOT/panel-api/main.py" "$REPO_ROOT/panel-api/requirements.txt" /opt/panel-api/
sudo chown -R panel:panel /opt/panel-api

if [[ ! -x /opt/panel-api/venv/bin/python ]]; then
  info "Creating venv"
  sudo -u panel python3 -m venv /opt/panel-api/venv
fi
info "Installing python dependencies"
sudo -u panel /opt/panel-api/venv/bin/pip install --quiet --upgrade pip
sudo -u panel /opt/panel-api/venv/bin/pip install --quiet -r "$REPO_ROOT/panel-api/requirements.txt"

info "Installing systemd unit"
sudo cp -a "$REPO_ROOT/panel-api/panel-api.service" /etc/systemd/system/panel-api.service
sudo systemctl daemon-reload
sudo systemctl enable --now panel-api

sleep 1
if systemctl is-active --quiet panel-api; then
  info "panel-api is running"
  echo "  liveness:      curl -s http://172.17.0.1:8010/api/health   # → {\"ok\":true,...} (open by design)"
  echo "  auth negative: curl -s http://172.17.0.1:8010/api/metrics   # → 401 (expected)"
  echo "  auth positive: curl -s -H \"X-Panel-Token: \$(sudo grep '^PANEL_TOKEN=' /opt/panel/.env | cut -d= -f2)\" -H 'Remote-User: connor' -H 'Remote-Groups: admins' http://172.17.0.1:8010/api/metrics"
  echo "  note: /api/actions needs panel-do installed first → make panel-do"
else
  sudo journalctl -u panel-api -n 40 --no-pager || true
  die "panel-api failed to start — journal excerpt above"
fi
