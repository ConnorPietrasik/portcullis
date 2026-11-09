#!/usr/bin/env bash
# Install the host-side panel API (FastAPI) as a systemd service.
# Runs as user 'panel', binds 172.17.0.1:8010 (docker0 bridge — reachable
# from the edge network, not from the internet).
set -euo pipefail
source "$(dirname "$(realpath "$0")")/common.sh"
need python3
need systemctl
load_env

[[ -f /opt/panel/.env ]] || die "/opt/panel/.env missing — run: make deploy (panel-api.service reads PANEL_TOKEN from it, via the derived .panel-api.env)"

# ── UFW gate ─────────────────────────────────────────────────────────────────
# UFW must already scope 8010 to the edge subnet BEFORE this listener exists —
# otherwise the API is WAN-reachable for the window until the firewall lands.
if ! command -v ufw >/dev/null 2>&1; then
  die "ufw missing (pacman -S ufw) — required before panel-api (RUNBOOK → UFW)"
fi
ufw_status="$(sudo ufw status 2>/dev/null || true)"
if ! grep -q "Status: active" <<<"$ufw_status"; then
  die "UFW inactive — enable it (sudo ufw enable) + add the 80,443 + 8010 rules (RUNBOOK → UFW) before panel-api, then re-run"
fi
# The 8010 rule must be edge-scoped: a "From: Anywhere" rule leaves the API
# WAN-reachable. `ufw status` renders the RUNBOOK's destination-scoped rule
# with the destination IP first ("172.17.0.1 8010/tcp  ALLOW 172.18.0.0/16" —
# no /32, and no /tcp suffix unless an explicit proto was given), so match
# "8010" anywhere in the line, never ^8010.
rules_8010="$(grep '8010' <<<"$ufw_status" || true)"
if [[ -z "$rules_8010" ]]; then
  die "UFW active but no 8010 rule — add the edge-scoped rule (RUNBOOK → UFW), then re-run; panel-api must not bind without it"
fi
if grep -q 'Anywhere' <<<"$rules_8010"; then
  die "8010 rule not edge-scoped (From: Anywhere) — the API would be WAN-reachable; re-add it per RUNBOOK → UFW, then re-run"
fi
# A scoped rule exists — but scoped to the LIVE edge subnet? `edge` is
# auto-subnetted at creation (RUNBOOK: derive, don't hardcode — a fresh
# daemon picks 172.18.0.0/16, a busy one something else); a rule for a stale
# subnet binds the API fine but 502s every panel → API call until doctor
# catches it. From column = last field after the trailing "# comment"; a (v6)
# mirror line carries the same IPv4 From. Skipped when the subnet can't be
# derived (docker/network absent — make deploy runs first anyway).
edge_subnet="$(sudo docker network inspect edge -f '{{(index .IPAM.Config 0).Subnet}}' 2>/dev/null || true)"
if [[ -n "$edge_subnet" ]]; then
  rule_sources="$(sed -E 's/#.*$//; s/\(v6\)//' <<<"$rules_8010" | awk 'NF {print $NF}' | sort -u)"
  if ! grep -qxF "$edge_subnet" <<<"$rule_sources"; then
    die "8010 rule scoped to $(paste -sd, <<<"$rule_sources") but the live edge network is $edge_subnet — binding now would 502 every panel → API call; re-add the rule with the live subnet (RUNBOOK → UFW), then re-run"
  fi
fi
info "UFW active with the edge-scoped 8010 rule — safe to bind 172.17.0.1:8010"

ensure_panel_user

info "Installing panel-api to /opt/panel-api"
sudo mkdir -p /opt/panel-api
# llm_gpus.py: the GPU pool's ledger/override/allocation logic (shared with
# the boot oneshot). llm_autostart.py: the boot autostart (below).
sudo cp -a "$REPO_ROOT/panel-api/main.py" "$REPO_ROOT/panel-api/llm_gpus.py" \
  "$REPO_ROOT/panel-api/llm_autostart.py" "$REPO_ROOT/panel-api/requirements.txt" /opt/panel-api/
sudo chown -R panel:panel /opt/panel-api

# LLM GPU pool state: seed the ledger (panel-owned 0600) once; the override
# is its compose projection (0644 — docker compose reads it as root). 02
# already created the override for the first deploy; this keeps a host that
# predates this feature consistent, and panel-api re-seeds both at startup
# if a hand removed them. Never hand-edit either file.
if [[ ! -f /opt/panel-api/llm-gpu.json ]]; then
  printf '{\n  "allocations": {},\n  "autostart": []\n}\n' | sudo tee /opt/panel-api/llm-gpu.json >/dev/null
fi
if [[ ! -f /opt/panel-api/llm-gpu-override.yml ]]; then
  printf 'services: {}\n' | sudo tee /opt/panel-api/llm-gpu-override.yml >/dev/null
fi
sudo chown panel:panel /opt/panel-api/llm-gpu.json /opt/panel-api/llm-gpu-override.yml
sudo chmod 600 /opt/panel-api/llm-gpu.json
sudo chmod 644 /opt/panel-api/llm-gpu-override.yml

if [[ ! -x /opt/panel-api/venv/bin/python ]]; then
  info "Creating venv"
  sudo -u panel python3 -m venv /opt/panel-api/venv
fi
info "Installing python dependencies"
sudo -u panel /opt/panel-api/venv/bin/pip install --quiet --upgrade pip
# Install from the /opt/panel-api copy, not the repo: user 'panel' may not
# be able to traverse the repo's home directory.
sudo -u panel /opt/panel-api/venv/bin/pip install --quiet -r /opt/panel-api/requirements.txt

info "Installing systemd unit"
# (Re)derive the reduced env file NOW: the unit requires it, and doing it here
# (not only in 02) makes `make api` self-contained — it picks up a .env that
# changed since the last deploy.
sync_panel_api_env /opt/panel
sudo cp -a "$REPO_ROOT/panel-api/panel-api.service" /etc/systemd/system/panel-api.service
# Fleet boot autostart: a oneshot that starts the panel-configured set on
# boot (its GPU assignments from the ledger — overlap-safe, see
# RUNBOOK → LLM fleet → GPU pool). Always installed + enabled; a no-op (exit 0) when
# the autostart list is empty. panel-do must be installed first — the
# quickstart runs it (04) before this (03).
sudo cp -a "$REPO_ROOT/panel-api/llm-autostart.service" /etc/systemd/system/llm-autostart.service
sudo systemctl daemon-reload
# enable + explicit restart (not `enable --now`): on a re-run after editing
# main.py the unit is already active, so `start` would be a no-op and keep
# serving the old code. `restart` always picks up the new file.
sudo systemctl enable panel-api
sudo systemctl enable llm-autostart
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
