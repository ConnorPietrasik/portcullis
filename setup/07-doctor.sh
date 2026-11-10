#!/usr/bin/env bash
# Doctor — post-deploy smoke test (READ-ONLY: changes nothing). Encodes every
# verification step from docs/RUNBOOK.md as one pass/fail report.
# Run ON THE SERVER: make doctor
set -uo pipefail  # deliberately NOT -e — every check runs; report everything
# shellcheck disable=SC1091
source "$(dirname "$(realpath "$0")")/envload.sh"

C_G=$'\033[1;32m'; C_Y=$'\033[1;33m'; C_R=$'\033[1;31m'; C_D=$'\033[2m'; C_0=$'\033[0m'
pass=0; fail=0; warnn=0; skipn=0
ok()   { pass=$((pass+1)); printf '  %sPASS%s %s\n' "$C_G" "$C_0" "$*"; }
bad()  { fail=$((fail+1)); printf '  %sFAIL%s %s\n' "$C_R" "$C_0" "$*"; }
warn() { warnn=$((warnn+1)); printf '  %sWARN%s %s\n' "$C_Y" "$C_0" "$*"; }
skip() { skipn=$((skipn+1)); printf '  %sSKIP%s %s\n' "$C_D" "$C_0" "$*"; }
hint() { printf '      %s↳ %s%s\n' "$C_D" "$*" "$C_0"; }

code_matches() { # <code> <want:200|30x> — exact match, or any 30x for redirects
  local c="$1" want="$2"
  [[ "$c" == "$want" ]] && return 0
  [[ "$want" == 30x && "$c" =~ ^30 ]] && return 0
  return 1
}

# <url> <want:200|30x> <desc> — trusted curl first; on mismatch, retry with -k
# to classify "staging cert" vs "actually down".
https_check() {
  local url="$1" want="$2" desc="$3" code kcode
  code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "$url" 2>/dev/null)"
  [[ -z "$code" ]] && code="000"
  if code_matches "$code" "$want"; then
    ok "$desc → $code"; return
  fi
  kcode="$(curl -sk -o /dev/null -w '%{http_code}' --max-time 10 "$url" 2>/dev/null)"
  [[ -z "$kcode" ]] && kcode="000"
  if code_matches "$kcode" "$want"; then
    warn "$desc → $kcode but over an UNTRUSTED cert"; hint "staging cert still in place — make promote (RUNBOOK Phase 2)"
  elif [[ "$kcode" == "000" ]]; then
    bad "$desc unreachable"; hint "DNS checks above; is traefik up? sudo docker compose --project-directory /opt/panel ps"
  else
    bad "$desc → $kcode (wanted $want)"; hint "check the router in compose/traefik/dynamic/ + make logs"
  fi
}

section() { printf '\n%s\n' "$*"; }

# Project name of the deployed stack (compose derives it from the project
# directory /opt/panel) — compose sets it as a label on every container it
# creates. The running/health checks below read those LABELS instead of
# `docker compose ps`: that must interpolate the deployed compose file first,
# so a .env missing a required var makes the whole command fail and every
# healthy container masquerades as "not running" (the 4x phantom FAIL after a
# half-synced update). `docker ps` needs no config at all.
STACK_PROJECT=panel
stack_running_services_ids() { # service|id, one per running container
  sudo docker ps --filter "label=com.docker.compose.project=$STACK_PROJECT" \
    --format '{{.Label "com.docker.compose.service"}}|{{.ID}}' 2>/dev/null || true
}
stack_running_services() { stack_running_services_ids | cut -d'|' -f1; }

# ── config ───────────────────────────────────────────────────────────────────
section "config (.env)"
REPO_ROOT="$(cd "$(dirname "$(realpath "$0")")/.." && pwd)"
if [[ -f "$REPO_ROOT/.env" ]]; then
  # Literal parse, never `source` (setup/envload.sh): the parser refuses values
  # compose or systemd would reinterpret, so the checks below see what the
  # containers actually receive.
  if ! env_file_load "$REPO_ROOT/.env"; then
    bad ".env has unparseable or unsafe lines (see warnings above)"
    hint "every line must be KEY=VALUE; values: no \$, no backslash, no ' #' (setup/envload.sh)"
  fi
  if [[ -n "${NAMESILO_API_KEY:-}" && "${NAMESILO_API_KEY:-}" != REPLACE_ME ]]; then
    ok "NAMESILO_API_KEY set"
  else
    bad "NAMESILO_API_KEY missing/placeholder"; hint "make secrets, then edit .env (RUNBOOK Phase 1)"
  fi
  if [[ -n "${ACME_EMAIL:-}" && "${ACME_EMAIL:-}" != __ACME_EMAIL__ ]]; then
    ok "ACME_EMAIL set"
  else
    bad "ACME_EMAIL missing"; hint "edit .env (RUNBOOK Phase 1)"
  fi
else
  skip ".env not found"; hint "make secrets (RUNBOOK Phase 1)"
fi

# ── configs render ───────────────────────────────────────────────────────────
# Both compose trees must interpolate with their .env BEFORE anything runs —
# a required var a new block needs but .env lacks (a ${LLM_API_KEY:?...} added
# with a new llm-* block after git pull) would otherwise surface only at
# pull/up time, after the configs are already half-synced. `config -q`
# renders the model without touching any container.
section "configs render"
if [[ -f "$REPO_ROOT/.env" && -f "$REPO_ROOT/compose/docker-compose.yml" ]]; then
  if render_out="$(sudo docker compose --env-file "$REPO_ROOT/.env" \
      -f "$REPO_ROOT/compose/docker-compose.yml" config -q 2>&1)"; then
    ok "repo compose renders with repo .env (make update preflight)"
  else
    bad "repo compose does NOT render with repo .env — make update will be refused"
    hint "$(head -1 <<<"$render_out") → add the missing key(s) to .env (see .env.example), then re-run"
  fi
else
  skip "repo compose render (.env or compose/ missing — fresh clone: make secrets)"
fi
if [[ -f /opt/panel/docker-compose.yml ]]; then
  # the deployed render includes the GPU pool override — that is exactly what
  # COMPOSE_STACK (make update / rollback / panel-do) renders, so a stale or
  # broken override surfaces HERE, not mid-update.
  if [[ ! -f /opt/panel-api/llm-gpu-override.yml ]]; then
    bad "llm-gpu-override.yml missing — every compose render on this host is broken"
    hint "sudo mkdir -p /opt/panel-api && printf 'services: {}\\n' | sudo tee /opt/panel-api/llm-gpu-override.yml && sudo chmod 644 /opt/panel-api/llm-gpu-override.yml — or re-run: make api (re-creates it)"
  elif render_out="$(sudo docker compose --project-directory /opt/panel \
      -f /opt/panel/docker-compose.yml -f /opt/panel-api/llm-gpu-override.yml config -q 2>&1)"; then
    ok "deployed compose (+ GPU override) renders with /opt/panel/.env"
  else
    bad "deployed compose does NOT render with /opt/panel/.env (half-synced state?)"
    hint "$(head -1 <<<"$render_out") → re-run: make deploy (re-syncs the repo .env; ungated — make update refuses while this check is red) — or make rollback if the last snapshot was known-good"
  fi
else
  skip "deployed compose render (no /opt/panel — make deploy, RUNBOOK Phase 2)"
fi

# ── fleet routes ───────────────────────────────────────────────────────────────
# A fleet instance is reachable only through its router pair in
# traefik/dynamic/llm.yml: router <name> (rule Host(<sub>.pietrasik.top)) +
# service <name> (url http://<name>:8000). Those four name references come
# from a hand-copied block, and one wrong one leaves the container up but the
# subdomain dead — silently. Both trees are checked: the repo's (make update
# refuses to run while this is red — doctor is its precondition) and the
# deployed one (what actually routes). The llm-* prefix IS the instance
# marker: the orphan check below assumes any llm-* router pairs with a
# compose service, so shared (non-instance) routers carry the fleet-*
# prefix instead (fleet-fallback, fleet-open — both route to the panel-api
# service in panel.yml) and need no exemption; a shared router named llm-*
# reports as stale on purpose (rename it). fleet-open's Host() list is
# checked separately: it must name every instance host (end of
# fleet_routes).
fleet_routes() { # <compose.yml> <llm.yml> <label>
  local comp="$1" routes="$2" label="$3" svc sub orph
  local -a svcs
  mapfile -t svcs < <(grep -oE '^  llm-[a-z0-9][a-z0-9-]*:' "$comp" | sed 's/^  //; s/:$//' | sort -u)
  for svc in "${svcs[@]}"; do
    sub="${svc#llm-}"
    if ! grep -q "^    ${svc}:" "$routes"; then
      bad "$label: $svc (compose) has no router entry in llm.yml"
      hint "the container would run but ${sub}.pietrasik.top would 404 — add the router + service pair (one block each)"
    elif ! grep -qF "Host(\`${sub}.pietrasik.top\`)" "$routes"; then
      bad "$label: $svc — Host(\`${sub}.pietrasik.top\`) not found in llm.yml"
      hint "the subdomain would not reach this instance — fix the router's rule"
    elif ! grep -qF "http://${svc}:8000" "$routes"; then
      bad "$label: $svc — no service url http://${svc}:8000 in llm.yml"
      hint "the subdomain would 503 — add the service block (loadBalancer → servers → url)"
    else
      ok "$label: $svc ↔ ${sub}.pietrasik.top (router + Host + url)"
    fi
  done
  while IFS= read -r orph; do
    if ! grep -q "^  ${orph}:" "$comp"; then
      bad "$label: $orph router in llm.yml but no compose service"
      hint "stale route (instance deleted?) — remove the router + service pair"
    fi
  done < <(awk '/^  routers:/{f=1;next} f && /^  [a-z]/{f=0} f && /^    llm-[a-z0-9][a-z0-9-]*:/{print}' "$routes" \
    | sed 's/^    //; s/:$//' | sort -u)
  [[ ${#svcs[@]} -eq 0 ]] && warn "$label: no llm-* services in compose (fleet not configured?)"
  # The fleet-open list — the one copy of instance names that is not a
  # router/service pair: the shared key-bootstrap router routes to the
  # panel-api service (no compose service to pair), but its Host() list
  # must still name EVERY instance. A missing host leaves that instance's
  # dashboard link dead: /open falls through to the instance's bare-Host
  # router, and radiance 404s it after the Authelia login. A stale host is
  # harmless clutter (warn only). Empty fleet → nothing to compare.
  if (( ${#svcs[@]} )); then
    local open_hosts inst_hosts missing stale add h
    open_hosts="$(awk '/^  routers:/{f=1;next} f && /^  [a-z]/{f=0} f && /^    fleet-open:$/{g=1;next} f && g && /^    [a-z]/{g=0;next} f && g{print}' "$routes" \
      | sed 's/#.*$//' \
      | { grep -oE 'Host\([^)]*\)' || true; } | { grep -oE '`[^`]+`' || true; } | tr -d '`' | LC_ALL=C sort -u)"
    inst_hosts="$(for svc in "${svcs[@]}"; do printf '%s\n' "${svc#llm-}.pietrasik.top"; done | LC_ALL=C sort -u)"
    if [[ -z $open_hosts ]]; then
      bad "$label: no hosts parseable from the fleet-open rule in llm.yml — every dashboard link 404s"
      hint "restore the fleet-open router block (key bootstrap → panel-api; see its comment in llm.yml)"
    else
      missing="$(LC_ALL=C comm -13 <(printf '%s\n' "$open_hosts") <(printf '%s\n' "$inst_hosts") || true)"
      stale="$(LC_ALL=C comm -23 <(printf '%s\n' "$open_hosts") <(printf '%s\n' "$inst_hosts") || true)"
      if [[ -n $missing ]]; then
        add=""
        while IFS= read -r h; do
          [[ -n $h ]] || continue
          add+="${add:+, }\`${h}\`"
        done <<<"$missing"
        bad "$label: fleet-open rule missing $(tr '\n' ' ' <<<"$missing" | sed 's/ *$//') — its dashboard link(s) 404"
        hint "add ${add} to the Host() list: llm.yml → fleet-open → rule"
      fi
      [[ -n $stale ]] && warn "$label: fleet-open rule lists $(tr '\n' ' ' <<<"$stale" | sed 's/ *$//') with no compose service (instance deleted?) — trim the list"
      [[ -z $missing && -z $stale ]] && ok "$label: fleet-open Host() list ↔ ${#svcs[@]} instance host(s)"
    fi
  fi
  return 0
}
section "fleet routes"
if [[ -f "$REPO_ROOT/compose/docker-compose.yml" && -f "$REPO_ROOT/compose/traefik/dynamic/llm.yml" ]]; then
  fleet_routes "$REPO_ROOT/compose/docker-compose.yml" "$REPO_ROOT/compose/traefik/dynamic/llm.yml" "repo"
else
  skip "repo fleet routes (compose/ tree missing — fresh clone: make secrets/deploy)"
fi
if [[ -f /opt/panel/docker-compose.yml && -f /opt/panel/traefik/dynamic/llm.yml ]]; then
  fleet_routes /opt/panel/docker-compose.yml /opt/panel/traefik/dynamic/llm.yml "deployed"
else
  skip "deployed fleet routes (no /opt/panel — make deploy, RUNBOOK Phase 2)"
fi

# ── edge stack ───────────────────────────────────────────────────────────────
section "edge stack (docker)"
if [[ -d /opt/panel ]]; then
  # `edge` is EXTERNAL to compose — only make deploy / make stack creates it.
  # If the edge containers are stopped (or removed) and unused networks are
  # pruned, `edge` is deleted and `up -d` fails with "network edge declared as
  # external, but could not be found".
  if sudo docker network inspect edge >/dev/null 2>&1; then
    ok "network: edge present"
  else
    bad "network: edge missing — `docker compose up -d` will refuse to start"; hint "sudo docker network create edge (make deploy / make stack recreate it automatically)"
  fi
  running="$(stack_running_services)"
  for svc in traefik authelia panel-web portfolio; do
    if grep -qx "$svc" <<<"$running"; then
      ok "container: $svc running"
    else
      bad "container: $svc not running"; hint "sudo docker compose --project-directory /opt/panel up -d --build"
    fi
  done

  # Health status per running container via `docker inspect` (same
  # config-independence as the running check above — `compose ps` would need
  # to interpolate the deployed file). Only containers defining a healthcheck
  # report one; the rest render as "none" → skipped below.
  health_out="$(stack_running_services_ids | while IFS='|' read -r svc id; do
    [[ -n "$svc" ]] || continue
    h="$(sudo docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$id" 2>/dev/null || echo none)"
    printf '%s\t%s\n' "$svc" "$h"
  done || true)"
  if [[ -n "$health_out" ]]; then
    while IFS=$'\t' read -r svc h; do
      case "$h" in
        healthy)   ok "health: $svc healthy" ;;
        unhealthy) bad "health: $svc UNHEALTHY"; hint "sudo docker compose --project-directory /opt/panel logs $svc --tail 50" ;;
        starting)  warn "health: $svc still starting"; hint "re-check in a couple of minutes" ;;
        *)         skip "health: $svc (no healthcheck defined)" ;;
      esac
    done <<<"$health_out"
  fi
else
  skip "/opt/panel missing"; hint "make deploy (RUNBOOK Phase 2)"
fi

# ── dns ──────────────────────────────────────────────────────────────────────
# Two different questions: do the names resolve ON THIS MACHINE (what the
# https checks below depend on — with the /etc/hosts pins from
# tools/panel-hosts.sh, that resolution IS the pin), and is the zone at the
# registrar healthy (panel-ddns keeps the apex A current; the * CNAME is a
# one-time manual record). The zone is asked of NameSilo's AUTHORITATIVE name
# servers: a recursive resolver serves a cached answer that can lag a healthy
# ddns tick by the record TTL (1h) — "fixed within one tick" must be
# observable in that tick — and through /etc/hosts the pins would hide
# everything local anyway.
section "dns"
# ns_query <name> — answer from the first reachable authoritative NS (ns1→2→3).
# Reachability is proven with a SOA probe (always present in an authoritative
# zone): an empty answer alone can't say whether the NS said "no record" or
# was simply unreachable, and that distinction is FAIL vs skip. rc 0 = an
# authoritative NS answered (an empty result is a definite no-record);
# rc 9 = no NS reachable.
ns_query() {
  local out ns
  for ns in ns1.dnsowl.com ns2.dnsowl.com ns3.dnsowl.com; do
    dig -4 +short +time=3 +tries=1 SOA pietrasik.top "@$ns" >/dev/null 2>&1 || continue
    out="$(dig -4 +short +time=3 +tries=1 "$1" "@$ns" 2>/dev/null)"
    printf '%s' "$out"
    return 0
  done
  return 9
}
have_dig=0
command -v dig >/dev/null 2>&1 && have_dig=1
apex="$(getent hosts pietrasik.top 2>/dev/null | awk '{print $1; exit}')"
if [[ -n "$apex" ]]; then
  ok "apex resolves locally: pietrasik.top → $apex"
  if (( have_dig )); then
    apex_ans="$(ns_query pietrasik.top)"; ns_ok=$?
    # OpenDNS is kept only for the public-IP echo (our egress address) — not
    # part of the zone.
    pub="$(dig -4 +short myip.opendns.com @resolver1.opendns.com 2>/dev/null | grep -E '^[0-9.]+$' | head -1)"
    pub_apex="$(grep -m1 -E '^[0-9.]+$' <<<"$apex_ans" || true)"
    if (( ns_ok )); then
      skip "published-zone check skipped (none of ns1..ns3.dnsowl.com reachable)"
    elif [[ -z "$pub_apex" ]]; then
      bad "no published apex A for pietrasik.top (authoritative NS)"; hint "apex A is created by panel-ddns (make ddns)"
    elif [[ -n "$pub" ]]; then
      if [[ "$pub_apex" == "$pub" ]]; then
        ok "apex A matches public IP ($pub)"
      else
        warn "apex A ($pub_apex) != public IP ($pub)"; hint "sudo systemctl start panel-ddns.service — fixed within one tick"
      fi
    else
      ok "published apex A: pietrasik.top → $pub_apex"
      skip "public IP detection failed"
    fi
  else
    skip "public-IP compare (dig not installed)"
  fi
else
  bad "pietrasik.top does not resolve"; hint "apex A is created by panel-ddns (make ddns); also check the * CNAME exists (Phase 0)"
fi
for h in auth.pietrasik.top panel.pietrasik.top; do
  if (( have_dig )); then
    ans="$(ns_query "$h")"; rc=$?
    if (( ! rc )); then
      # +short prints only the Answer section, and dig does NOT follow CNAMEs.
      # A recursive resolver chases the chain server-side (why local `dig`
      # shows the final A), but an authoritative NS answers "A auth" with the
      # CNAME RR alone — so walk the chain ourselves: re-query the last line
      # until it is an IP (bounded, so a CNAME cycle can't spin). A line with
      # a digit in it is an IP (a CNAME target like 'pietrasik.top.' has none).
      chained=0  # 1 once we actually had to re-query a CNAME target
      for _ in 1 2 3 4 5 6 7 8; do
        grep -qE '^[0-9a-fA-F:]*[0-9][0-9a-fA-F:.]*$' <<<"$ans" && break
        chained=1
        next="$(tail -1 <<<"$ans")"
        [[ -z "$next" ]] && break
        more="$(ns_query "${next%.}")" || break
        [[ -z "$more" ]] && break
        ans+=$'\n'"$more"
      done
      if grep -qE '^[0-9a-fA-F:]*[0-9][0-9a-fA-F:.]*$' <<<"$ans"; then
        if (( chained )); then
          ok "$h resolves publicly (wildcard CNAME chain)"
        else
          ok "$h resolves publicly (direct A record)"
        fi
      else
        bad "$h does not resolve publicly"; hint "check the * CNAME → pietrasik.top at NameSilo (Phase 0)"
      fi
      continue
    fi
  fi
  # no dig, or no authoritative NS reachable — local check only.
  if getent hosts "$h" >/dev/null 2>&1; then
    ok "$h resolves locally (weak: an /etc/hosts pin passes this even if the public record is gone)"
  else
    bad "$h does not resolve"; hint "check the * CNAME → pietrasik.top at NameSilo (Phase 0)"
  fi
done

# ── tls ──────────────────────────────────────────────────────────────────────
section "tls"
leaf="$(echo | timeout 15 openssl s_client -connect pietrasik.top:443 -servername pietrasik.top 2>/dev/null)"
if [[ -z "$leaf" ]]; then
  skip "no TLS handshake on pietrasik.top:443 (stack down, or DNS checks above failed)"
else
  issuer="$(openssl x509 -noout -issuer <<<"$leaf" 2>/dev/null || true)"
  if grep -qi "staging\|fake" <<<"$issuer"; then
    warn "cert is STAGING (untrusted)"; hint "make promote (RUNBOOK Phase 2)"
  elif grep -qi "let.s encrypt" <<<"$issuer"; then
    ok "production Let's Encrypt cert"
  else
    warn "unrecognized issuer: $issuer"
  fi
  if openssl x509 -noout -checkend 604800 <<<"$leaf" >/dev/null 2>&1; then
    ok "cert valid for >7 days"
  else
    warn "cert expires within 7 days — traefik renews at 30; if stuck: make logs"
  fi
  if grep -q "DNS:\*\.pietrasik\.top" <(openssl x509 -noout -ext subjectAltName <<<"$leaf" 2>/dev/null); then
    ok "cert covers *.pietrasik.top"
  else
    warn "wildcard missing from cert SANs"
  fi
fi

# ── https endpoints ──────────────────────────────────────────────────────────
section "https endpoints"
https_check "https://auth.pietrasik.top"   200 "auth portal"
https_check "https://pietrasik.top"        200 "portfolio (public)"
https_check "https://panel.pietrasik.top"  30x "panel (unauth → Authelia redirect)"
# LLM fallback router: 200 (some instance up) or 503 (all down) is both
# HEALTHY — the router is answering. 401 = key mismatch, 404/502 = broken
# route or panel-api. Only meaningful with a configured fleet.
if [[ -n "${LLM_API_KEY:-}" ]]; then
  llm_code="$(curl -sk -o /dev/null -w '%{http_code}' --max-time 10 \
    -H "Authorization: Bearer $LLM_API_KEY" \
    https://llm.pietrasik.top/v1/models 2>/dev/null || true)"
  if [[ "$llm_code" == 200 || "$llm_code" == 503 ]]; then
    ok "llm.pietrasik.top/v1/models → $llm_code (router answers; 503 = all instances down)"
  else
    bad "llm.pietrasik.top/v1/models → ${llm_code:-000} (want 200 or 503)"
    hint "401 = LLM_API_KEY mismatch; 404/502 = fleet-fallback router (traefik/dynamic/llm.yml) or panel-api down (make logs)"
  fi
else
  skip "llm.pietrasik.top (no LLM_API_KEY in .env — fleet not configured)"
fi

# ── panel-api ────────────────────────────────────────────────────────────────
section "panel-api (host service)"
if systemctl is-active --quiet panel-api 2>/dev/null; then
  ok "systemd: panel-api active"
else
  bad "systemd: panel-api inactive"; hint "sudo journalctl -u panel-api -n 50 (RUNBOOK Phase 4)"
fi
# The reduced env file: must hold the token and ONLY the keys its service
# reads (whitelist, shared with the derivation — envload.sh). A regression
# here = the full secret set sitting in a panel-uid /proc environ.
if [[ -f /opt/panel/.panel-api.env ]]; then
  env_mode="$(stat -c %a /opt/panel/.panel-api.env 2>/dev/null || true)"
  if [[ "$env_mode" == 600 ]]; then ok ".panel-api.env mode 600"; else
    warn ".panel-api.env mode ${env_mode:-unreadable} (want 600)"; hint "make api re-derives it root-owned"
  fi
  if sudo grep -qE '^[[:space:]]*PANEL_TOKEN[[:space:]]*=' /opt/panel/.panel-api.env; then
    ok ".panel-api.env holds PANEL_TOKEN"
  else
    bad ".panel-api.env missing PANEL_TOKEN"; hint "make api (re-derives from /opt/panel/.env)"
  fi
  # PANEL_DEV=1 disables the API's own auth checks (local development only).
  # It is a legal member of the allowed set, so the whitelist check below
  # would pass a production deployment with auth off — catch it here. The
  # value must be exactly 1 (optionally quoted), so the pattern is anchored.
  if sudo grep -qE "^[[:space:]]*PANEL_DEV[[:space:]]*=[[:space:]]*[\"']?1[\"']?[[:space:]]*$" /opt/panel/.panel-api.env; then
    bad "panel-api runs with PANEL_DEV=1 — API auth checks disabled"; hint "remove PANEL_DEV from /opt/panel/.env (and the repo .env — local dev only), then re-run: make api"
  else
    ok "PANEL_DEV not enabled in .panel-api.env"
  fi
  api_allow="${panel_api_env_keys[*]}"; api_allow="${api_allow// /|}"
  if sudo grep -qEv "^[[:space:]]*(${api_allow})[[:space:]]*=" /opt/panel/.panel-api.env; then
    bad ".panel-api.env holds a key outside its allowed set (${panel_api_env_keys[*]})"
    hint "make api (re-derives it from /opt/panel/.env — never hand-edit)"
  else
    ok ".panel-api.env holds only its allowed keys"
  fi
else
  bad "/opt/panel/.panel-api.env missing"; hint "make api (derives it from /opt/panel/.env)"
fi
health="$(curl -s --max-time 5 http://172.17.0.1:8010/api/health 2>/dev/null || true)"
if grep -q '"ok":true' <<<"$health"; then
  ok "GET /api/health → ok (open liveness endpoint)"
else
  bad "GET /api/health failed"; hint "systemctl status panel-api"
fi
# GPU pool state (the ledger is intent — panel-owned 0600; the override is
# its compose projection — 0644, read by docker compose as root). Both are
# seeded by make deploy / make api and re-seeded by panel-api at startup.
if [[ -f /opt/panel-api/llm-gpu.json ]]; then
  m="$(stat -c %a /opt/panel-api/llm-gpu.json 2>/dev/null || true)"
  if [[ "$m" == 600 ]]; then ok "llm-gpu.json (GPU ledger) mode 600"; else
    warn "llm-gpu.json mode ${m:-unreadable} (want 600)"; hint "sudo chown panel:panel && sudo chmod 600 /opt/panel-api/llm-gpu.json"
  fi
else
  warn "llm-gpu.json missing — panel-api seeds it at startup"; hint "systemctl restart panel-api (or make api)"
fi
if [[ -f /opt/panel-api/llm-gpu-override.yml ]]; then
  ok "llm-gpu-override.yml present (GPU pool compose projection)"
else
  bad "llm-gpu-override.yml missing — compose renders (make update) will fail"; hint "make api (re-creates it)"
fi
if systemctl is-enabled --quiet llm-autostart 2>/dev/null; then
  ok "llm-autostart.service enabled (fleet boot autostart)"
else
  warn "llm-autostart.service not enabled — the fleet won't auto-start on boot"; hint "make api (installs + enables it)"
fi
code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 http://172.17.0.1:8010/api/metrics 2>/dev/null)"
if [[ "$code" == 401 ]]; then
  ok "GET /api/metrics unauthenticated → 401 (auth enforced)"
else
  bad "GET /api/metrics unauthenticated → ${code:-000} (expected 401)"
fi
code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 http://172.17.0.1:8010/v1/models 2>/dev/null)"
if [[ "$code" == 401 ]]; then
  ok "GET /v1/models unauthenticated → 401 (LLM router key enforced)"
else
  bad "GET /v1/models unauthenticated → ${code:-000} (expected 401)"
fi
# The REAL data path: edge container → host API. The host curls above pass
# even when traefik's route 502s (wrong bind IP or missing UFW rule).
panel_web_id="$(sudo docker ps --filter "label=com.docker.compose.project=$STACK_PROJECT" \
  --filter 'label=com.docker.compose.service=panel-web' --format '{{.ID}}' 2>/dev/null | head -1 || true)"
if [[ -d /opt/panel && -n "$panel_web_id" ]]; then
  # Probe with busybox-wget-compatible options ONLY: panel-web is
  # nginx:stable-alpine, whose wget has no --timeout (GNU-only). Passing it
  # makes the probe abort at argument parsing (usage on stderr, no request
  # ever made, stdout empty) — a permanent false FAIL with the network
  # perfectly healthy. Busybox wget also has no connect timeout (wget's -T
  # only caps reads), so a genuinely DROPped port would hang the doctor
  # forever: cap the whole probe with the timeout applet. 2>&1 so a real
  # failure (refused/no route) surfaces in the hint instead of masquerading
  # as an empty response. No -T on exec: redundant without -t, and removed
  # in Docker 29.
  edge_health="$(sudo docker exec "$panel_web_id" \
    timeout 5 wget -q -O - http://172.17.0.1:8010/api/health 2>&1 || true)"
  if grep -q '"ok":true' <<<"$edge_health"; then
    ok "edge container → 172.17.0.1:8010 reachable (real API path)"
  else
    bad "edge container cannot reach 172.17.0.1:8010 — panel /api/* will 502"
    probe_err="$(head -1 <<<"$edge_health")"
    hint "${probe_err:+$probe_err | }compare the 8010 rule's From with the live subnet: sudo docker network inspect edge -f '{{(index .IPAM.Config 0).Subnet}}' (stale scope → RUNBOOK → UFW); bind: ss -tln | grep 8010"
  fi
else
  skip "edge → API path (panel-web not running)"
fi

# ── secret file modes ────────────────────────────────────────────────────────
# tar restores the repo's 0644 on every sync, so the deploy re-tightens the
# credential files — verify that actually happened (and catch manual edits).
section "secret file modes"
if [[ -d /opt/panel ]]; then
  for f in /opt/panel/.env /opt/panel/traefik/dynamic/middlewares.yml /opt/panel/authelia/users_database.yml; do
    if [[ -f "$f" ]]; then
      m="$(stat -c %a "$f" 2>/dev/null || true)"
      if [[ "$m" == 600 ]]; then ok "$f mode 600"
      else warn "$f mode ${m:-unreadable} (want 600)"; hint "sudo chmod 600 $f (make deploy/update re-tightens it)"
      fi
    else
      bad "$f missing"; hint "make deploy"
    fi
  done
  if [[ -f /opt/panel/authelia/db.sqlite3 ]]; then
    m="$(stat -c %a /opt/panel/authelia/db.sqlite3 2>/dev/null || true)"
    if [[ "$m" == 600 ]]; then ok "authelia db.sqlite3 mode 600 (TOTP seeds)"
    else warn "authelia db.sqlite3 mode ${m:-unreadable} (want 600 — TOTP seeds)"; hint "sudo chmod 600 /opt/panel/authelia/db.sqlite3"
    fi
  else
    warn "authelia db.sqlite3 missing (authelia hasn't booted yet?)"
  fi
else
  skip "secret file modes (no /opt/panel)"
fi

# ── panel-do ─────────────────────────────────────────────────────────────────
section "panel-do (privileged dispatcher)"
if [[ -x /usr/local/libexec/panel-do ]]; then
  if sudo -u panel sudo -n /usr/local/libexec/panel-do --list 2>/dev/null | python3 -m json.tool >/dev/null 2>&1; then
    ok "--list emits valid registry JSON (sudoers path works)"
  else
    bad "panel-do --list failed"; hint "make panel-do; check /etc/sudoers.d/panel"
  fi
  # The LLM fallback router resolves instance names via edge-ips. A stale
  # binary (pre-edge-ips) makes llm.pietrasik.top report every instance
  # "not on the edge network" — a 503 the route check above still calls
  # healthy — so the action's presence must be checked explicitly.
  if sudo -u panel sudo -n /usr/local/libexec/panel-do --list 2>/dev/null | grep -q '"name":"edge-ips"'; then
    ok "edge-ips action present (LLM router name→IP map)"
  else
    bad "panel-do registry missing edge-ips — llm.pietrasik.top will report every instance down"
    hint "binary is stale — make panel-do (rebuilds + reinstalls it)"
  fi
  # The GPU view (panel tab "GPUs") rides on llm-fleet/llm-live/llm-apply/
  # llm-stop; a stale binary makes the view error instead of erroring
  # silently, so presence is a hard check like the others above.
  if sudo -u panel sudo -n /usr/local/libexec/panel-do --list 2>/dev/null | grep -q '"name":"llm-apply"'; then
    ok "llm-apply action present (GPU pool: (re)create an instance on its card set)"
  else
    bad "panel-do registry missing llm-apply — the GPU view cannot assign cards"
    hint "binary is stale — make panel-do (rebuilds + reinstalls it)"
  fi
  # The Services view's "defined, no container" → create+start button rides on
  # start-service/list-services; a stale binary makes that section silently
  # vanish (and /api/stack-services 500), so check presence explicitly too.
  if sudo -u panel sudo -n /usr/local/libexec/panel-do --list 2>/dev/null | grep -q '"name":"start-service"'; then
    ok "start-service action present (Services view create+start)"
  else
    bad "panel-do registry missing start-service — the panel can't bring up container-less services"
    hint "binary is stale — make panel-do (rebuilds + reinstalls it)"
  fi
else
  skip "not installed"; hint "make panel-do (RUNBOOK Phase 3)"
fi

# ── panel-ddns ───────────────────────────────────────────────────────────────
section "panel-ddns (dynamic IP)"
if systemctl is-active --quiet panel-ddns.timer 2>/dev/null; then
  ok "systemd timer active"
else
  bad "panel-ddns.timer not active"; hint "make ddns (RUNBOOK → Dynamic IP)"
fi
# The reduced env file: must hold the NameSilo key and ONLY the keys its
# script reads (same discipline as .panel-api.env).
if [[ -f /opt/panel/.panel-ddns.env ]]; then
  env_mode="$(stat -c %a /opt/panel/.panel-ddns.env 2>/dev/null || true)"
  if [[ "$env_mode" == 600 ]]; then ok ".panel-ddns.env mode 600"; else
    warn ".panel-ddns.env mode ${env_mode:-unreadable} (want 600)"; hint "make ddns re-derives it root-owned"
  fi
  if sudo grep -qE '^[[:space:]]*NAMESILO_API_KEY[[:space:]]*=' /opt/panel/.panel-ddns.env; then
    ok ".panel-ddns.env holds NAMESILO_API_KEY"
  else
    bad ".panel-ddns.env missing NAMESILO_API_KEY"; hint "make ddns (re-derives from /opt/panel/.env)"
  fi
  ddns_allow="${panel_ddns_env_keys[*]}"; ddns_allow="${ddns_allow// /|}"
  if sudo grep -qEv "^[[:space:]]*(${ddns_allow})[[:space:]]*=" /opt/panel/.panel-ddns.env; then
    bad ".panel-ddns.env holds a key outside its allowed set (${panel_ddns_env_keys[*]})"
    hint "make ddns (re-derives it from /opt/panel/.env — never hand-edit)"
  else
    ok ".panel-ddns.env holds only its allowed keys"
  fi
else
  bad "/opt/panel/.panel-ddns.env missing"; hint "make ddns (derives it from /opt/panel/.env)"
fi
last="$(sudo journalctl -u panel-ddns.service -n 1 --no-pager -o cat 2>/dev/null | tail -1)"
if [[ -n "$last" ]]; then
  printf '      %slast reconcile: %s%s\n' "$C_D" "$last" "$C_0"
else
  skip "no journal yet (service hasn't run — make ddns)"
fi

# ── firewall ─────────────────────────────────────────────────────────────────
# UFW is REQUIRED on this host (no firewalld/iptables fallback).
section "firewall (ufw — required)"
if command -v ufw >/dev/null 2>&1; then
  ufw_status="$(sudo ufw status 2>/dev/null || true)"
  if grep -q "Status: active" <<<"$ufw_status"; then
    # ufw renders the multi-port rule as "80,443/tcp" (comma, no space); also
    # match a spaced form defensively.
    if grep -qE '80, ?443/tcp' <<<"$ufw_status"; then
      ok "80,443/tcp allowed"
    else
      warn "80,443/tcp rule missing"; hint "RUNBOOK → UFW (docker-published ports bypass ufw — belt-and-suspenders)"
    fi
    # `ufw status` renders the destination-scoped rule as "172.17.0.1 8010/tcp"
    # (no /32; no /tcp suffix without an explicit proto), so match "8010"
    # anywhere in the line, never ^8010.
    rules_8010="$(grep '8010' <<<"$ufw_status" || true)"
    if [[ -z "$rules_8010" ]]; then
      bad "no 8010 rule — container→host API calls will 502"; hint "RUNBOOK → UFW: derive the edge subnet and add the rule (required)"
    elif grep -q 'Anywhere' <<<"$rules_8010"; then
      bad "8010 rule not edge-scoped (From: Anywhere) — API is WAN-reachable"; hint "RUNBOOK → UFW: re-add it scoped to the edge subnet"
    elif edge_subnet="$(sudo docker network inspect edge -f '{{(index .IPAM.Config 0).Subnet}}' 2>/dev/null || true)" && [[ -n "$edge_subnet" ]]; then
      # A scoped rule exists — but scoped to WHICH subnet? `edge` is auto-subnetted
      # at creation (RUNBOOK: derive, don't hardcode — a fresh daemon picks
      # 172.18.0.0/16, a busy one something else), and a rule for a STALE subnet
      # reads "present and edge-scoped" while silently dropping every container→
      # API call (host curls stay green: local delivery bypasses INPUT). The
      # From column is the last field after the trailing "# comment"; a (v6)
      # mirror line carries the same IPv4 From, so strip the marker too.
      rule_sources="$(sed -E 's/#.*$//; s/\(v6\)//' <<<"$rules_8010" | awk 'NF {print $NF}' | sort -u)"
      if grep -qxF "$edge_subnet" <<<"$rule_sources"; then
        ok "8010 rule present and scoped to the live edge subnet ($edge_subnet)"
      else
        bad "8010 rule is scoped to $(paste -sd, <<<"$rule_sources") but the live edge network is $edge_subnet — container→API traffic is dropped (panel /api/* 502s)"
        hint "delete the stale rule (sudo ufw status numbered, sudo ufw delete <n>) and re-add it: sudo ufw allow from $edge_subnet to 172.17.0.1 port 8010 proto tcp (RUNBOOK → UFW)"
      fi
    else
      ok "8010 rule present and edge-scoped"
    fi
    # Break-glass: with UFW active and no SSH rule, standard-port SSH is
    # closed and break-glass is console-only. Non-verbose status renders the
    # app profile as "OpenSSH" (not 22/tcp), so accept both; the anchor is the
    # To column, so a source-scoped rule counts too. Only warned when
    # something actually listens on :22 (a custom SSH port legitimately has
    # no 22 rule). The deploy never adds SSH rules, so the hint points at the
    # operator.
    if ! grep -qE '^[[:space:]]*(22/tcp|OpenSSH)' <<<"$ufw_status" \
       && [[ $(ss -tln 'sport = :22' 2>/dev/null | tail -n +2 | grep -c .) -gt 0 ]]; then
      warn "no 22/tcp rule — something listens on :22 but SSH is closed while UFW is active (break-glass is console-only)"
      hint "allow your SSH port manually (the deploy never touches SSH rules): sudo ufw allow 22/tcp"
    fi
  else
    bad "UFW not active"; hint "enable it (sudo ufw enable), then RUNBOOK → UFW for the two rules"
  fi
else
  bad "UFW not installed"; hint "required on this host — pacman -S ufw && sudo ufw enable, then RUNBOOK → UFW for the two rules"
fi

# ── audit trail ──────────────────────────────────────────────────────────────
section "audit trail (journald)"
if [[ -d /var/log/journal ]]; then
  ok "persistent journald (/var/log/journal exists)"
else
  warn "journald is volatile — panel-do's audit trail is lost on reboot"
  hint "sudo mkdir -p /var/log/journal && sudo systemctl restart systemd-journald"
fi

# ── host maintenance ─────────────────────────────────────────────────────────
# CachyOS ships fstrim.timer enabled — this is the cross-distro catch-all.
section "host maintenance"
if systemctl is-active --quiet fstrim.timer 2>/dev/null; then
  ok "fstrim.timer active (weekly SSD trim)"
else
  warn "fstrim.timer not active — SSDs skip weekly TRIM"; hint "sudo systemctl enable --now fstrim.timer (RUNBOOK → Hardening)"
fi

# ── summary ──────────────────────────────────────────────────────────────────
section "summary"
printf '  %d pass, %d fail, %d warn, %d skip\n' "$pass" "$fail" "$warnn" "$skipn"
if (( fail > 0 )); then
  printf '  %sfix the FAILs above, then re-run: make doctor%s\n' "$C_R" "$C_0"
  exit 1
fi
printf '  %sall good%s\n' "$C_G" "$C_0"
