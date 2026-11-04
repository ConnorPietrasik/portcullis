#!/usr/bin/env bash
# Install the host-side panel API (FastAPI) as a systemd service.
# Runs as user 'panel', binds 172.17.0.1:8010 (docker0 bridge — reachable from
# the edge network, not from the internet).
set -euo pipefail
source "$(dirname "$(realpath "$0")")/common.sh"
need python3
need systemctl
load_env

[[ -f /opt/panel/.env ]] || die "/opt/panel/.env missing — run: make deploy (panel-api.service reads PANEL_TOKEN from it, via the derived .panel-api.env)"

# ── UFW gate ─────────────────────────────────────────────────────────────────
# panel-api binds 172.17.0.1:8010. UFW must already scope 8010 to the edge
# subnet BEFORE this listener exists (RUNBOOK → UFW) — otherwise the API is
# reachable from the WAN for the window until the firewall lands.
if ! command -v ufw >/dev/null 2>&1; then
  die "ufw missing (pacman -S ufw) — required before panel-api (RUNBOOK → UFW)"
fi
ufw_status="$(sudo ufw status 2>/dev/null || true)"
if ! grep -q "Status: active" <<<"$ufw_status"; then
  die "UFW inactive — add the 80,443 + 8010 rules and enable it (RUNBOOK → UFW) before panel-api, then re-run"
fi
# The 8010 rule must be edge-scoped: a "From: Anywhere" rule would leave the
# API WAN-reachable — exactly what the scoping exists to prevent.
# NOTE: `ufw status` renders the RUNBOOK's destination-scoped rule with the
# destination IP FIRST in the To column ("172.17.0.1 8010/tcp  ALLOW
# 172.18.0.0/16" — no /32: ufw normalizes host addresses away), and a rule
# added without an explicit proto has no /tcp suffix at all, so match "8010"
# anywhere in the line, never ^8010.
rules_8010="$(grep '8010' <<<"$ufw_status" || true)"
if [[ -z "$rules_8010" ]]; then
  die "UFW active but no 8010 rule — add the edge-scoped rule (RUNBOOK → UFW), then re-run; panel-api must not bind without it"
fi
if grep -q 'Anywhere' <<<"$rules_8010"; then
  die "8010 rule not edge-scoped (From: Anywhere) — the API would be WAN-reachable; re-add it per RUNBOOK → UFW, then re-run"
fi
info "UFW active with the edge-scoped 8010 rule — safe to bind 172.17.0.1:8010"

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
# Install from the copy in /opt/panel-api, not the repo: user 'panel' may not
# be able to traverse the repo's home directory (permissions).
sudo -u panel /opt/panel-api/venv/bin/pip install --quiet -r /opt/panel-api/requirements.txt

info "Installing systemd unit"
# (Re)derive the reduced env file NOW: the unit requires it, and doing it here
# (not only in 02-deploy-stack.sh) makes `make api` self-contained — it picks
# up a .env that changed since the last deploy.
sync_panel_api_env /opt/panel
sudo cp -a "$REPO_ROOT/panel-api/panel-api.service" /etc/systemd/system/panel-api.service
sudo systemctl daemon-reload
# enable + explicit restart (not `enable --now`): on a re-run after editing
# main.py the unit is already active, so `start` would be a no-op — the running
# uvicorn keeps serving the old code while the is-active check below reports
# success. `restart` always stop-then-starts, picking up the new file (and any
# deps pip just upgraded under it).
sudo systemctl enable panel-api
sudo systemctl restart panel-api

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
