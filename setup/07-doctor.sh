#!/usr/bin/env bash
# Doctor — post-deploy smoke test (READ-ONLY: changes nothing). Encodes every
# verification step from docs/RUNBOOK.md as one pass/fail report. Diagnostic
# only — not part of the numbered deploy sequence. Run ON THE SERVER:
#   make doctor
set -uo pipefail  # deliberately NOT -e — every check runs; report everything

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

# ── config ───────────────────────────────────────────────────────────────────
section "config (.env)"
REPO_ROOT="$(cd "$(dirname "$(realpath "$0")")/.." && pwd)"
if [[ -f "$REPO_ROOT/.env" ]]; then
  # shellcheck disable=SC1091
  set -a; source "$REPO_ROOT/.env"; set +a
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

# ── edge stack ───────────────────────────────────────────────────────────────
section "edge stack (docker)"
if [[ -d /opt/panel ]]; then
  running="$(sudo docker compose --project-directory /opt/panel ps --services --status=running 2>/dev/null || true)"
  for svc in traefik authelia panel-web portfolio; do
    if grep -qx "$svc" <<<"$running"; then
      ok "container: $svc running"
    else
      bad "container: $svc not running"; hint "sudo docker compose --project-directory /opt/panel up -d --build"
    fi
  done

  # Health status (only containers defining a compose healthcheck report one;
  # newer compose versions may also omit the field entirely).
  health_out="$(sudo docker compose --project-directory /opt/panel ps --format json 2>/dev/null \
    | python3 -c '
import json, sys
text = sys.stdin.read().strip()
if not text:
    sys.exit(0)
try:
    data = json.loads(text)  # newer compose: one JSON array
    if not isinstance(data, list):
        data = [data]
except json.JSONDecodeError:
    data = [json.loads(line) for line in text.splitlines() if line.strip()]  # JSON lines
for x in data:
    if isinstance(x, dict):
        print(x.get("Service", "?") + "\t" + (x.get("Health") or "none"))
' 2>/dev/null || true)"
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
section "dns"
apex="$(getent hosts pietrasik.top 2>/dev/null | awk '{print $1; exit}')"
if [[ -n "$apex" ]]; then
  ok "apex resolves: pietrasik.top → $apex"
  if command -v dig >/dev/null 2>&1; then
    pub="$(dig +short myip.opendns.com @resolver1.opendns.com 2>/dev/null | grep -E '^[0-9.]+$' | head -1)"
    if [[ -n "$pub" ]]; then
      if [[ "$apex" == "$pub" ]]; then
        ok "apex A matches public IP ($pub)"
      else
        warn "apex A ($apex) != public IP ($pub)"; hint "sudo systemctl start panel-ddns.service — fixed within one tick"
      fi
    else
      skip "public IP detection failed"
    fi
  else
    skip "public-IP compare (dig not installed)"
  fi
else
  bad "pietrasik.top does not resolve"; hint "apex A is created by panel-ddns (make ddns); also check the * CNAME exists (Phase 0)"
fi
for h in auth.pietrasik.top panel.pietrasik.top; do
  if getent hosts "$h" >/dev/null 2>&1; then
    ok "$h resolves (wildcard CNAME chain)"
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

# ── panel-api ────────────────────────────────────────────────────────────────
section "panel-api (host service)"
if systemctl is-active --quiet panel-api 2>/dev/null; then
  ok "systemd: panel-api active"
else
  bad "systemd: panel-api inactive"; hint "sudo journalctl -u panel-api -n 50 (RUNBOOK Phase 4)"
fi
health="$(curl -s --max-time 5 http://172.17.0.1:8010/api/health 2>/dev/null || true)"
if grep -q '"ok":true' <<<"$health"; then
  ok "GET /api/health → ok (open liveness endpoint)"
else
  bad "GET /api/health failed"; hint "systemctl status panel-api"
fi
code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 http://172.17.0.1:8010/api/metrics 2>/dev/null)"
if [[ "$code" == 401 ]]; then
  ok "GET /api/metrics unauthenticated → 401 (auth enforced)"
else
  bad "GET /api/metrics unauthenticated → ${code:-000} (expected 401)"
fi

# ── panel-do ─────────────────────────────────────────────────────────────────
section "panel-do (privileged dispatcher)"
if [[ -x /usr/local/libexec/panel-do ]]; then
  if sudo -u panel sudo -n /usr/local/libexec/panel-do --list 2>/dev/null | python3 -m json.tool >/dev/null 2>&1; then
    ok "--list emits valid registry JSON (sudoers path works)"
  else
    bad "panel-do --list failed"; hint "make panel-do; check /etc/sudoers.d/panel"
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
last="$(sudo journalctl -u panel-ddns.service -n 1 --no-pager -o cat 2>/dev/null | tail -1)"
if [[ -n "$last" ]]; then
  printf '      %slast reconcile: %s%s\n' "$C_D" "$last" "$C_0"
else
  skip "no journal yet (service hasn't run — make ddns)"
fi

# ── firewall ─────────────────────────────────────────────────────────────────
section "firewall (ufw — warn-only)"
if command -v ufw >/dev/null 2>&1 && sudo ufw status 2>/dev/null | grep -q "Status: active"; then
  if sudo ufw status 2>/dev/null | grep -q "80,443/tcp"; then
    ok "80,443/tcp allowed"
  else
    warn "80,443/tcp rule missing"; hint "RUNBOOK → UFW"
  fi
  if sudo ufw status 2>/dev/null | grep -q "8010"; then
    ok "8010 bridge-scoped rule present"
  else
    warn "no 8010 rule — container→host API calls will 502"; hint "RUNBOOK → UFW (this rule is required)"
  fi
else
  skip "ufw not active (docker-published ports bypass it anyway — RUNBOOK → UFW)"
fi

# ── audit trail ──────────────────────────────────────────────────────────────
section "audit trail (journald)"
if [[ -d /var/log/journal ]]; then
  ok "persistent journald (/var/log/journal exists)"
else
  warn "journald is volatile — panel-do's audit trail is lost on reboot"
  hint "sudo mkdir -p /var/log/journal && sudo systemctl restart systemd-journald"
fi

# ── summary ──────────────────────────────────────────────────────────────────
section "summary"
printf '  %d pass, %d fail, %d warn, %d skip\n' "$pass" "$fail" "$warnn" "$skipn"
if (( fail > 0 )); then
  printf '  %sfix the FAILs above, then re-run: make doctor%s\n' "$C_R" "$C_0"
  exit 1
fi
printf '  %sall good%s\n' "$C_G" "$C_0"
