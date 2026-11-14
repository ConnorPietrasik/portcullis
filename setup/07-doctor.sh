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
  # panel-api service (no compose service to pair), but its host list must
  # still name EVERY instance. A missing host leaves that instance's
  # dashboard link dead: /open falls through to the instance's bare-Host
  # router (no forward-auth there), and radiance's API-key gate — which
  # covers every path but GET / /health /dashboard /ping — answers the
  # browser with 401 {"error":{"message":"invalid api key",…}}: that JSON
  # IS the page the user sees. A stale host is harmless clutter (warn
  # only). Empty fleet → nothing to compare.
  if (( ${#svcs[@]} )); then
    local open_hosts inst_hosts missing stale add h badhost
    # Syntax canary: Traefik v3's Host() takes ONE value. The v2 multi-value
    # form is a rule parse error — and traefik SWALLOWS it (logs the error,
    # drops just that router, keeps serving the rest of the file): the
    # host-list check below would still PASS on the dead rule, so flag the
    # syntax itself.
    badhost="$(grep -nE '^[[:space:]]*rule:.*Host\([^)]*,[^)]*\)' "$routes" | cut -d: -f1 | tr '\n' ' ')"
    if [[ -n $badhost ]]; then
      bad "$label: multi-value Host() at line(s) $badhost in $routes — Traefik v3 takes ONE host per Host() and SWALLOWS the parse error (the router is dropped, the rest of the file still loads), so every dashboard link falls through to the instance's bare-Host router and 401s 'invalid api key'"
      hint "OR the hosts: (Host(\`a.pietrasik.top\`) || Host(\`b.pietrasik.top\`)) && PathPrefix(...)"
    fi
    open_hosts="$(awk '/^  routers:/{f=1;next} f && /^  [a-z]/{f=0} f && /^    fleet-open:$/{g=1;next} f && g && /^    [a-z]/{g=0;next} f && g{print}' "$routes" \
      | sed 's/#.*$//' \
      | { grep -oE 'Host\([^)]*\)' || true; } | { grep -oE '`[^`]+`' || true; } | tr -d '`' | LC_ALL=C sort -u)"
    inst_hosts="$(for svc in "${svcs[@]}"; do printf '%s\n' "${svc#llm-}.pietrasik.top"; done | LC_ALL=C sort -u)"
    if [[ -z $open_hosts ]]; then
      bad "$label: no hosts parseable from the fleet-open rule in llm.yml — every dashboard link 401s 'invalid api key'"
      hint "restore the fleet-open router block (key bootstrap → panel-api; see its comment in llm.yml)"
    else
      missing="$(LC_ALL=C comm -13 <(printf '%s\n' "$open_hosts") <(printf '%s\n' "$inst_hosts") || true)"
      stale="$(LC_ALL=C comm -23 <(printf '%s\n' "$open_hosts") <(printf '%s\n' "$inst_hosts") || true)"
      if [[ -n $missing ]]; then
        add=""
        while IFS= read -r h; do
          [[ -n $h ]] || continue
          add+="${add:+ || }Host(\`${h}\`)"
        done <<<"$missing"
        bad "$label: fleet-open rule missing $(tr '\n' ' ' <<<"$missing" | sed 's/ *$//') — its dashboard link(s) 401 'invalid api key'"
        hint "add ${add} to the fleet-open rule (between the parens): llm.yml → fleet-open → rule"
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
https_check "https://www.pietrasik.top"    200 "portfolio via www (public)"
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

# ── search (searxng) ─────────────────────────────────────────────────────────
# SearXNG agent API + private browser UI (docs/RUNBOOK.md → "Web search (SearXNG)").
#
# Tri-state gate mirroring the fleet's deadlock protection: the repo-side NEW
# doctor runs as the precondition of the update that introduces the feature,
# so feature-absent = SKIP, never FAIL. Defined-but-not-running = FAIL
# (searxng is always-on — restart: unless-stopped, not on-demand: a WARN
# would green-light the "defined, no container" state). Running = the full
# probe matrix.
#
# Never `docker compose config` for expectations (interpolation makes healthy
# containers look dead — the 4x phantom-FAIL incident); the deployed files
# are grepped raw and container state comes from compose LABELS
# (config-independent).
section "search (searxng)"
# The searxng service's environment: keys. The block must carry ONLY the
# allow-list — additions are a deliberate diff, and a guard key fails
# LOUDLY: a guard key in env silently overrides the deployed settings.yml
# (env > file) and every deployed-file check would pass on it. GRANIAN_HOST
# is NOT a member: the 2026-11-13 spike forced no host pin (both loopback
# families answered) — a future spike that does forces a deliberate edit here.
SEARXNG_ENV_ALLOW=(SEARXNG_PORT FORCE_OWNERSHIP GRANIAN_BLOCKING_THREADS)
SEARXNG_ENV_GUARDS=(SEARXNG_SECRET SEARXNG_BASE_URL SEARXNG_LIMITER SEARXNG_PUBLIC_INSTANCE SEARXNG_IMAGE_PROXY)
searxng_env_keys() { # <compose-file> → key, one per line
  awk '/^  searxng:/{f=1;next} f && /^  [a-z]/{f=0}
       f && /^    environment:/{e=1;next} f && e && /^    [a-z]/{e=0}
       f && e && /^[[:space:]]+[A-Za-z_][A-Za-z0-9_]*:/{print}' "$1" \
    | sed -E 's/^[[:space:]]+([A-Za-z_][A-Za-z0-9_]*):.*/\1/' | LC_ALL=C sort -u
}
searxng_env_check() { # <label> <compose-file>
  local label="$1" file="$2" k g keys allowed clean=1 is_guard
  keys="$(searxng_env_keys "$file")"
  allowed="$(printf '%s\n' "${SEARXNG_ENV_ALLOW[@]}" | LC_ALL=C sort -u)"
  for k in $SEARXNG_ENV_GUARDS; do
    if grep -qx "$k" <<<"$keys"; then
      bad "$label: guard key $k in the searxng env block — env > settings.yml, the deployed file is overridden (the secret path's premise is 'the env name is absent')"
      hint "remove $k from the compose environment: block — the named allow-list is the control (RUNBOOK → Web search)"
      clean=0
    fi
  done
  for k in $keys; do
    is_guard=0
    for g in ${SEARXNG_ENV_GUARDS[@]}; do [[ "$k" == "$g" ]] && is_guard=1; done
    if (( is_guard )); then continue; fi   # already reported
    if ! grep -qx "$k" <<<"$allowed"; then
      bad "$label: env member $k outside the searxng allow-list (${SEARXNG_ENV_ALLOW[*]})"
      hint "additions are a deliberate diff — add it to SEARXNG_ENV_ALLOW in this script with a reason, or remove it from compose"
      clean=0
    fi
  done
  (( clean )) && ok "$label: searxng env block carries only allow-list members"
  return 0
}
# One search-matrix probe. Sets SX_CODE / SX_BODY / SX_INSECURE. NEVER -L:
# following redirects would turn the API host's 302 auth-chain drift into a
# 200 from the Authelia portal. Trusted cert first; a transport failure
# (000) is retried with -k to classify "staging cert" from "actually down".
SX_CODE=""; SX_BODY=""; SX_INSECURE=0
sx_get() { # <url> <curl args...>
  local url="$1" resp
  shift
  resp="$(curl -s --max-time 10 -w '|%{http_code}' "$@" "$url" 2>/dev/null)"
  SX_CODE="${resp##*|}"; SX_BODY="${resp%|*}"
  [[ -z "$SX_CODE" ]] && SX_CODE="000"
  if [[ "$SX_CODE" == "000" ]]; then
    SX_INSECURE=1
    resp="$(curl -sk --max-time 10 -w '|%{http_code}' "$@" "$url" 2>/dev/null)"
    SX_CODE="${resp##*|}"; SX_BODY="${resp%|*}"
    [[ -z "$SX_CODE" ]] && SX_CODE="000"
  fi
}

repo_has_searxng=0; deployed_has_searxng=0
[[ -f "$REPO_ROOT/compose/docker-compose.yml" ]] && grep -q '^  searxng:' "$REPO_ROOT/compose/docker-compose.yml" && repo_has_searxng=1
[[ -f /opt/panel/docker-compose.yml ]] && grep -q '^  searxng:' /opt/panel/docker-compose.yml && deployed_has_searxng=1

if (( ! repo_has_searxng )); then
  skip "searxng not in the repo compose (feature not in this checkout)"
else
  # Repo-side pairing — a half-landed feature is the failure mode: route
  # without service, service without settings, settings without the json
  # surface.
  if [[ ! -f "$REPO_ROOT/compose/traefik/dynamic/search.yml" ]]; then
    bad "repo compose defines searxng but traefik/dynamic/search.yml is missing"
    hint "the container would run with no route — restore the route file"
  else
    ok "repo: search.yml ↔ compose searxng block (route pair present)"
  fi
  if [[ ! -f "$REPO_ROOT/compose/searxng/settings.yml" ]]; then
    bad "repo compose defines searxng but compose/searxng/settings.yml is missing"
    hint "restore the settings file (the deployed dir is a config ROOT — the sync prunes stale files from it)"
  else
    s="$REPO_ROOT/compose/searxng/settings.yml"
    if grep -qE '^[[:space:]]*formats:.*json' "$s"; then
      ok "repo settings.yml: json in formats (the agent API surface — 403 without it)"
    else
      bad "repo settings.yml: json NOT in formats — every keyed /search?format=json would 403 (the single most likely silent misconfiguration)"
    fi
    if grep -qE '^[[:space:]]*lock:.*image_proxy' "$s"; then
      ok "repo settings.yml: preferences.lock carries image_proxy (the per-request re-arm is locked)"
    else
      bad "repo settings.yml: preferences.lock missing image_proxy — a bare &image_proxy=1 on a keyed /search would re-arm the image proxy"
    fi
    if grep -qF 'secret_key: "__SEARXNG_SECRET__"' "$s"; then
      ok "repo settings.yml: secret_key placeholder intact (the deployed-file sed owns it)"
    else
      bad "repo settings.yml: secret_key is not the __SEARXNG_SECRET__ placeholder — the secret path (01-secrets → sync sed) is broken"
    fi
  fi
  searxng_env_check "repo" "$REPO_ROOT/compose/docker-compose.yml"
  if [[ ! -f "$REPO_ROOT/.env" ]]; then
    skip "searxng .env checks (no .env — make secrets, RUNBOOK Phase 1)"
  else
    if [[ -n "${SEARXNG_SECRET:-}" && "${SEARXNG_SECRET}" != __*__ ]]; then
      ok ".env: SEARXNG_SECRET set (non-placeholder)"
    else
      bad ".env: SEARXNG_SECRET missing or still a placeholder"
      hint "make secrets (fresh install) or add SEARXNG_SECRET=\$(openssl rand -hex 32) to the repo .env, then make update"
    fi
    if [[ -n "${LLM_API_KEY:-}" ]]; then
      ok ".env: LLM_API_KEY set (the search GATE key — reused from the fleet, owner decision)"
    else
      bad ".env: LLM_API_KEY missing — the gate answers 503 until it exists (search reuses the fleet key)"
      hint "set LLM_API_KEY in .env (fleet + search share it), then make update"
    fi
  fi
  # Placeholder-set equality: every __X__ token in the route/config trees
  # must be owned by a sed line in stack_sync_configs, and vice versa — a
  # token with no sed line is a public string running live. Scoped: unscoped
  # it is permanently red on node_modules. Mirrors the deployed-side
  # survival grep (which covers docker-compose.yml too).
  owned="$(grep -oE 's\|__[A-Z_]+__\|' "$REPO_ROOT/setup/common.sh" | grep -oE '__[A-Z_]+__' | LC_ALL=C sort -u || true)"
  found="$(grep -rohE '__[A-Z_]+__' "$REPO_ROOT/compose/traefik/" "$REPO_ROOT/compose/searxng/" "$REPO_ROOT/compose/docker-compose.yml" 2>/dev/null | LC_ALL=C sort -u || true)"
  if [[ -z "$owned" ]]; then
    bad "placeholder-set derivation is empty (the sed lines in setup/common.sh changed shape — this check is blind)"
  elif [[ "$owned" == "$found" ]]; then
    ok "placeholder sets equal ($(tr '\n' ' ' <<<"$owned" | sed 's/ $//')) — every __X__ token in compose/traefik+searxng+docker-compose.yml is owned by a sed line"
  else
    bad "placeholder sets differ — owned by the sync's seds: [$(tr '\n' ' ' <<<"$owned" | sed 's/ $//')] vs in the trees: [$(tr '\n' ' ' <<<"$found" | sed 's/ $//')]"
    hint "a token with no sed line is a public string running live; a sed line with no token is a dead fill"
  fi
  # Placement: set equality is blind to WHERE a token lives — a token in a
  # file the sync's sed never rewrites (the 2026-11-13 bug: a comment in
  # docker-compose.yml named the searxng one) survives the sync verbatim,
  # and the deployed survival grep then red-lights every update. Every
  # token-carrying file must be a sed target of stack_sync_configs.
  token_files="$(grep -rlE '__[A-Z_]+__' "$REPO_ROOT/compose/traefik/" "$REPO_ROOT/compose/searxng/" "$REPO_ROOT/compose/docker-compose.yml" 2>/dev/null || true)"
  misplaced=""
  while IFS= read -r tf; do
    [[ -n "$tf" ]] || continue
    case "$tf" in
      "$REPO_ROOT/compose/traefik/traefik.yml"|\
      "$REPO_ROOT/compose/traefik/dynamic/middlewares.yml"|\
      "$REPO_ROOT/compose/searxng/settings.yml") : ;;   # the sync's sed targets
      *) misplaced+="$tf"$'\n' ;;
    esac
  done <<<"$token_files"
  if [[ -n "$misplaced" ]]; then
    bad "placeholder token(s) in file(s) the sync's sed does not rewrite:
$(sed "s|^${REPO_ROOT}/|  |" <<<"$misplaced")"
    hint "tokens belong only in the sed targets (traefik.yml, dynamic/middlewares.yml, searxng/settings.yml) — name them descriptively in prose"
  else
    ok "every __X__ token lives in a sed-targeted file (the sync rewrites each one)"
  fi
fi

if (( ! deployed_has_searxng )); then
  if (( repo_has_searxng )); then
    warn "deployed compose lacks searxng (repo has it) — lands on the next make update (WARN only: a cross-tree FAIL would red-light the very update that introduces the feature)"
  else
    skip "searxng not in the deployed compose (feature not landed)"
  fi
else
  # Deployed-file checks — run whenever the deployed compose defines searxng,
  # running or not.
  f=/opt/panel/searxng/settings.yml
  if [[ ! -f "$f" ]]; then
    bad "deployed searxng/settings.yml missing"
    hint "make update (the sync ships compose/searxng/ + fills the secret)"
  else
    og="$(stat -c '%u:%g' "$f" 2>/dev/null || true)"   # numeric — %U:%G would print a NAME if the host has a local uid-977 user
    mo="$(stat -c '%a' "$f" 2>/dev/null || true)"
    if [[ "$og" == "977:977" && "$mo" == "600" ]]; then
      ok "deployed settings.yml 977:977/600 (the app reads it as uid 977 at import; the sync re-applies both after every tar)"
    elif [[ "$og" != "977:977" ]]; then
      bad "deployed settings.yml owner ${og:-unreadable} (want 977:977) — the next boot crashloops on EACCES (root:root 0600 is unreadable to the app)"
      hint "sudo chown 977:977 $f (make update re-applies it)"
    else
      bad "deployed settings.yml mode ${mo:-unreadable} (want 600) — the instance secret is over-exposed"
      hint "sudo chmod 600 $f (make update re-applies it)"
    fi
    if sudo grep -qE '__[A-Z_]+__' "$f"; then
      bad "placeholder(s) survived in the deployed settings.yml — the sed line was skipped; the instance would run on a literal public string"
      hint "make update (re-runs the substitution + the survival check)"
    else
      ok "no __*__ placeholder survives in the deployed settings.yml"
    fi
  fi
  searxng_env_check "deployed" /opt/panel/docker-compose.yml

  running="$(stack_running_services)"
  if ! grep -qx searxng <<<"$running"; then
    bad "searxng defined but not running (always-on service — restart: unless-stopped, not on-demand)"
    hint "sudo docker compose --project-directory /opt/panel up -d searxng (a WARN would green-light the defined-no-container state)"
  else
    cid="$(sudo docker ps --filter "label=com.docker.compose.project=$STACK_PROJECT" \
      --filter 'label=com.docker.compose.service=searxng' --format '{{.ID}}' 2>/dev/null | head -1 || true)"
    # Mount check: a dropped/renamed bind hands out an ANONYMOUS volume (it
    # carries NO inspect flag — grep the bind string) and the entrypoint
    # template-copies (the template's image_proxy: true + formats [html] —
    # agents 403, healthcheck green, compose diff clean).
    mounts="$(sudo docker inspect --format '{{range .Mounts}}{{.Type}} {{.Source}} {{.Destination}} {{.Mode}}{{"\n"}}{{end}}' "$cid" 2>/dev/null || true)"
    if grep -qx 'bind /opt/panel/searxng /etc/searxng ro' <<<"$mounts"; then
      ok "config bind intact: /opt/panel/searxng → /etc/searxng (ro)"
    else
      bad "config bind missing/renamed — an anonymous volume would carry the entrypoint template and boot healthy"
      hint "make update (re-syncs the volume line); check: sudo docker inspect --format '{{json .Mounts}}' \$(docker compose --project-directory /opt/panel ps -q searxng)"
    fi
    # Effective-secret probe — the POST-INIT searx.settings value (env
    # overrides included), compared against the deployed file INSIDE the
    # container: verdict only, the secret never leaves (the landing spike's
    # stage-0 check, at day-2 cadence).
    verdict="$(sudo docker exec -w /usr/local/searxng "$cid" /usr/local/searxng/.venv/bin/python -c 'from searx import settings
file_val = ""
for line in open("/etc/searxng/settings.yml"):
    if line.strip().startswith("secret_key:"):
        file_val = line.split(":", 1)[1].strip().strip(chr(34)).strip(chr(39))
eff = settings["server"]["secret_key"]
print("OK" if eff == file_val and eff not in ("ultrasecretkey", "__SEARXNG_SECRET__") and len(eff) >= 32 else "FAIL")' 2>/dev/null || true)"
    if [[ "$verdict" == "OK" ]]; then
      ok "effective secret == deployed file value (post-init — an env override would show up here)"
    else
      bad "effective-secret probe FAIL (override mode: the image set SEARXNG_* in its own ENV, or an env line slipped the allow-list; sed-skip mode: file and effective both bad)"
      hint "check the env allow-list + owner/mode checks above; re-run: make update"
    fi

    # ── probe matrix ── Exact status codes + body shape. `code != 401`-style
    # assertions are forbidden (403/404/502/503 would all pass one and hide
    # the drift). Never a live search: the no-q 400 and the /healthz +
    # /metrics probes all run before any engine contact — the matrix must
    # stay engine-free (twice per update, from the home IP, against engines
    # that throttle).
    if [[ -n "${LLM_API_KEY:-}" ]]; then
      sx_get "https://search.pietrasik.top/search?format=json"
      if [[ "$SX_CODE" == "401" ]]; then
        ok "unkeyed search. /search?format=json → 401 (the key gate answers)"
      else
        bad "unkeyed search. /search?format=json → ${SX_CODE} (want exactly 401)"
        hint "400 = the gate middleware fell off — WIDENED ROUTE, incident class (this probe carries no q, so SearXNG answering directly is 400 'No query', never 200): restore search-gate in search.yml; 200 = not SearXNG answering (it cannot 200 a no-q json request) — the router's service drifted; 302 = auth-chain drift; 404 = router/file-tree missing; 500 = the gate (panel-api) unreachable; 429 = the shared search-rate bucket is busy (live traffic — RUNBOOK → Web search, shared-bucket note): re-run in a few seconds"
        (( SX_INSECURE )) && hint "over an UNTRUSTED cert — staging? make promote"
      fi
      sx_get "https://search.pietrasik.top/search?format=json" -H "Authorization: Bearer $LLM_API_KEY"
      if [[ "$SX_CODE" == "400" && "$SX_BODY" == *"No query"* ]]; then
        ok "keyed /search (no q) → 400 'No query' (credential + chain + json format, zero engine queries)"
      else
        bad "keyed /search (no q) → ${SX_CODE} (want exactly 400 with JSON containing 'No query')"
        hint "401 = key mismatch (repo .env vs deployed); 403 = json missing from the deployed formats (the silent misconfiguration — re-run: make update); 500 = the gate unreachable (panel-api down); 429 = the shared search-rate bucket is busy (live traffic — RUNBOOK → Web search, shared-bucket note): re-run in a few seconds"
        (( SX_INSECURE )) && hint "over an UNTRUSTED cert — staging? make promote"
      fi
      sx_get "https://search.pietrasik.top/healthz" -H "Authorization: Bearer $LLM_API_KEY"
      if [[ "$SX_CODE" == "200" && "$SX_BODY" == "OK" ]]; then
        ok "keyed /healthz → 200 OK"
      else
        bad "keyed /healthz → ${SX_CODE} (want 200, body OK)"
        hint "liveness disambiguator: 503 = the gate answered key-unset OR no healthy searxng server; 500 = the gate unreachable"
      fi
      sx_get "https://search.pietrasik.top/metrics" -H "Authorization: Bearer $LLM_API_KEY"
      if [[ "$SX_CODE" == "404" ]]; then
        ok "keyed /metrics → 404 (open_metrics still empty — the fleet key does not open Prometheus)"
      else
        bad "keyed /metrics → ${SX_CODE} (want exactly 404 — open_metrics was SET)"
        hint "if it was set to the LLM key, the fleet key now opens Prometheus: clear open_metrics in compose/searxng/settings.yml + make update"
      fi
    else
      skip "keyed probes (no LLM_API_KEY in the repo .env — the gate answers 503 until the fleet key exists)"
    fi
    # The direct gate probe — the CONFIGURED check, independent of traefik.
    gcode="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 http://172.17.0.1:8010/gate/search 2>/dev/null || true)"
    if [[ "$gcode" == "401" ]]; then
      ok "direct gate probe → 401 (gate configured; 503 would mean LLM_API_KEY missing from the reduced env)"
    else
      bad "direct gate probe → ${gcode:-000} (want exactly 401, never 503)"
      hint "503 = LLM_API_KEY missing from /opt/panel/.panel-api.env (make api re-derives it); 404 = stale panel-api code (make api); 000 = panel-api down (systemctl status panel-api)"
    fi
    # Negative reachability: the gate must NOT answer on the panel host — an
    # unauth curl sees the Authelia redirect; gate JSON there means a router
    # edit turned /gate/search into a browser-routable key oracle.
    px_code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "https://panel.pietrasik.top/gate/search" 2>/dev/null || true)"
    px_insecure=0
    if [[ "$px_code" == "000" || -z "$px_code" ]]; then
      px_code="$(curl -sk -o /dev/null -w '%{http_code}' --max-time 10 "https://panel.pietrasik.top/gate/search" 2>/dev/null || true)"; px_insecure=1
    fi
    if [[ "$px_code" =~ ^30 ]]; then
      ok "panel host /gate/search → $px_code (Authelia answers, never the gate${px_insecure:+ — over an UNTRUSTED cert, staging? make promote})"
    elif [[ "$px_code" == "401" || "$px_code" == "503" ]]; then
      bad "panel host /gate/search → $px_code — the GATE is answering on the panel host (route drifted to panel-api)"
      hint "root-level placement is one panel.yml edit from becoming a key oracle — audit compose/traefik/dynamic/panel.yml"
    else
      bad "panel host /gate/search → ${px_code:-000} (want 30x — the Authelia redirect for an unauth curl)"
      hint "200 = an authed session or the auth-chain fell off; 000 = unreachable (DNS checks above)"
    fi
  fi
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

# ── auto-update ──────────────────────────────────────────────────────────────
# The update state file (the dashboard's control plane) + the clone's
# auto-exec safety when the timer is enabled. Read-only — the lock probe
# takes and releases the flock immediately (it only READS who holds it).
# A held lock means either a live run (status in-progress, or this doctor's
# own run via PANEL_UPDATE_INSIDE) or a stale leftover (flagged).
section "auto-update (update state + timer)"
USTATE=/opt/panel/.update-state
if [[ -f $USTATE ]]; then
  if python3 -c 'import json,sys; json.load(open(sys.argv[1]))' "$USTATE" 2>/dev/null; then
    ok "update state file parses"
    # Stuck in-progress: the update process died without finishing (power
    # loss, kill). Snapshot + log survive; the fix is manual.
    stuck="$(python3 -c '
import calendar, json, sys, time
try:
    d = json.load(open(sys.argv[1]))
    if d.get("status") != "in-progress":
        sys.exit(0)
    ts = (d.get("snapshot") or "")[:15]  # 20250107T012345 — UTC
    print(max(0, int(time.time() - calendar.timegm(time.strptime(ts, "%Y%m%dT%H%M%S")))))
except Exception:
    pass' "$USTATE" 2>/dev/null)"
    if [[ -n ${stuck:-} && $stuck -gt 7200 ]]; then
      bad "update stuck in-progress for ${stuck}s"
      hint "the update process died mid-run: sudo tail /opt/panel/.update-run.log, pgrep -af '08-update.sh|09-rollback.sh' — kill the leftover, then: make rollback (or re-run make update)"
    elif [[ ${stuck:-0} -gt 0 ]]; then
      ok "update in progress (${stuck}s in) — the gates + auto-rollback finish it"
    fi
  else
    bad "update state file is corrupt"
    hint "sudo rm /opt/panel/.update-state and re-run: make update (it re-snapshots)"
  fi
  # A held lock with no in-progress status means a leftover process keeps the
  # fd open (flock releases on process exit). The probe only runs on the real
  # file — a fallback open (e.g. /dev/null) would make flock fail and
  # false-positive. ONE exception: this doctor is a gate of the run ITSELF
  # (PANEL_UPDATE_INSIDE — exported by update_acquire_lock): 08 locks BEFORE
  # its pre-doctor, and the snapshot that flips status to in-progress runs
  # AFTER it, so its own held lock is expected and must not red-light the very
  # update it is gating. (The flock, not this probe, is the exclusion.)
  if [[ -f /opt/panel/.update.lock ]] && exec 8</opt/panel/.update.lock 2>/dev/null; then
    if ! flock -n 8 2>/dev/null; then
      exec 8>&-
      if grep -q '"status": "in-progress"' "$USTATE" 2>/dev/null; then
        : # a live run — the in-progress line above reports it
      elif [[ -n "${PANEL_UPDATE_INSIDE:-}" ]]; then
        ok "update lock held by this run itself (pre-snapshot doctor — expected)"
      else
        bad "update lock held with no in-progress update (leftover process?)"
        hint "holder: sudo fuser -v /opt/panel/.update.lock — kill it if it is a leftover from an interrupted run, then: make doctor"
      fi
    else
      exec 8>&-
    fi
  fi
  if systemctl is-enabled --quiet panel-autoupdate.timer 2>/dev/null; then
    repo="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("repo_root",""))' "$USTATE" 2>/dev/null)"
    if [[ -z $repo ]]; then
      bad "auto-update timer enabled but no repo_root registered in the state"
      hint "run: make update (from the server clone) once to register it"
    elif [[ $repo != /* ]]; then
      # panel-do's validUpdatePath rejects a non-absolute repo_root at runtime;
      # mirror it HERE too — the ancestor walk below would otherwise loop on a
      # relative path (dirname "x" → "." → "." …) if the dir exists. Only a
      # hand-edited state file can hold one (snapshot_stack always writes the
      # absolute REPO_ROOT).
      bad "update state holds a non-absolute repo_root ($repo) — the state was hand-edited"
      hint "sudo rm /opt/panel/.update-state, then: make update (it re-registers the clone)"
    elif [[ ! -d $repo ]]; then
      bad "registered clone $repo is gone"
      hint "re-clone, copy .env + compose/authelia/users_database.yml, run: make update (RUNBOOK → Auto-updates)"
    else
      # The SAME checks panel-do's update-run makes before EVERY auto-exec:
      # the script, its setup/ dir, and every ancestor directory up to / must
      # not be group/other-writable (a writable dir ANYWHERE in the chain =
      # delete-and-replace the clone or the script) and not be owned by the
      # panel user; 08/09 must be executable with the repo's bash shebang.
      #
      # NOTE: bash's file tests do NOT mean group/other-writable — [[ -g ]]
      # is the setgid bit and [[ -o ]]/"-o" means "owned by the effective
      # user" — so the writable check reads the octal mode bits directly
      # (stat -c %a), exactly like panel-do's Mode()&0o022.
      mode_gw_or_ow() { # arg: octal mode; true if the group- or other-write bit is set
        local m="$1"
        (( (8#${m: -2:1} & 2) || (8#${m: -1:1} & 2) ))
      }
      mode_any_exec() { # arg: octal mode; true if ANY execute bit is set
        local m="$1"
        (( (8#${m: -3:1} | 8#${m: -2:1} | 8#${m: -1:1}) & 1 ))
      }
      panel_uid="$(id -u panel 2>/dev/null)"
      clone_ok=1
      d="$repo"
      while :; do
        m="$(stat -c %a "$d" 2>/dev/null)"
        if [[ ! -d $d || -z $m ]] || mode_gw_or_ow "$m" \
           || [[ -n $panel_uid && "$(stat -c %u "$d" 2>/dev/null)" == "$panel_uid" ]]; then
          clone_ok=0; break
        fi
        [[ $d == / ]] && break
        d="$(dirname "$d")"
      done
      script_ok=1
      for s in 08-update.sh 09-rollback.sh; do
        p="$repo/setup/$s"
        m="$(stat -c %a "$p" 2>/dev/null)"
        if [[ ! -f $p || -z $m ]] || mode_gw_or_ow "$m" || ! mode_any_exec "$m" \
           || [[ -n $panel_uid && "$(stat -c %u "$p" 2>/dev/null)" == "$panel_uid" ]] \
           || [[ "$(head -n1 "$p" 2>/dev/null)" != "#!/usr/bin/env bash" ]]; then
          script_ok=0; break
        fi
      done
      if (( clone_ok )) && (( script_ok )); then
        ok "registered clone $repo is auto-update safe (private directory chain, scripts executable with the bash shebang)"
      else
        bad "registered clone $repo fails panel-do's auto-exec check"
        hint "the clone, its setup/, and every directory above them must be writable only by you (and root) and not owned by the panel user; 08/09-update.sh must be executable with the '#!/usr/bin/env bash' first line (both committed — re-pull the repo if missing)"
      fi
    fi
  else
    skip "auto-update timer not enabled (install: make autoupdate, then the panel's Settings or systemctl enable --now panel-autoupdate.timer)"
  fi
else
  skip "no update state yet (make update has never run on this host — nothing to check)"
fi

# ── summary ──────────────────────────────────────────────────────────────────
section "summary"
printf '  %d pass, %d fail, %d warn, %d skip\n' "$pass" "$fail" "$warnn" "$skipn"
if (( fail > 0 )); then
  printf '  %sfix the FAILs above, then re-run: make doctor%s\n' "$C_R" "$C_0"
  exit 1
fi
printf '  %sall good%s\n' "$C_G" "$C_0"
