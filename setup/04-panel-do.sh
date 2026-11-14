#!/usr/bin/env bash
# Build panel-do (privileged action dispatcher), install it (root-owned,
# 0755), set up sudoers, and run the negative-test suite.
set -euo pipefail
source "$(dirname "$(realpath "$0")")/common.sh"
need go

ensure_panel_user

info "Building panel-do"
(
  cd "$REPO_ROOT/panel-do"
  unformatted="$(gofmt -l .)"
  if [[ -n "$unformatted" ]]; then
    echo "$unformatted"
    die "gofmt check failed for the files above (run: gofmt -w panel-do/)"
  fi
  go vet ./...
  go build -trimpath -ldflags "-s -w" -o panel-do .
)

info "Installing to /usr/local/libexec/panel-do"
# Protection is root ownership + 0755 (panel-do's self-check re-verifies both
# on every run). No immutable flag — btrfs/overlayfs don't support it.
# -D: Arch-based hosts (e.g. CachyOS) don't ship /usr/local/libexec.
sudo install -D -o root -g root -m 0755 "$REPO_ROOT/panel-do/panel-do" /usr/local/libexec/panel-do

info "Installing sudoers rule (/etc/sudoers.d/panel)"
tmp="$(mktemp)"
cat >"$tmp" <<'EOF'
# Managed by panel setup. The 'panel' user may run ONLY the whitelist dispatcher.
Defaults:panel !requiretty
panel ALL=(root) NOPASSWD: /usr/local/libexec/panel-do
EOF
# Validate the candidate BEFORE install: plain visudo -c checks the live tree
# (old order installed first). A broken file drops the panel rule on every
# sudo call — host-wide lockout on sudo < 1.9.3.
sudo visudo -c -f "$tmp" >/dev/null \
  || die "sudoers validation failed — candidate kept at $tmp for inspection"
sudo install -o root -g root -m 0440 "$tmp" /etc/sudoers.d/panel
rm -f "$tmp"

info "Running panel-do self-tests"
pd() { sudo -u panel sudo -n /usr/local/libexec/panel-do "$@" >/dev/null 2>&1; }
expect_exit() { # <expected-code> <args...>
  local want="$1"; shift
  set +e; pd "$@"; local rc=$?; set -e
  if [[ $rc -eq $want ]]; then
    info "  ok: panel-do $* → $rc"
  else
    die "  FAIL: panel-do $* → $rc (want $want)"
  fi
}

if sudo -u panel sudo -n /usr/local/libexec/panel-do --list | python3 -m json.tool >/dev/null 2>&1; then
  info "  ok: --list emits valid registry JSON"
else
  die "  FAIL: --list did not emit valid JSON"
fi
expect_exit 64 definitely-not-an-action          # unknown action
expect_exit 65 restart-service '../../etc/passwd' # path injection
expect_exit 65 restart-service -foo               # flag injection
expect_exit 65 restart-service                    # arity
expect_exit 65 start-container zzz                # not a 12-hex container ID
expect_exit 65 restart-container '../../etc'      # path injection
expect_exit 65 start-service '../../etc/passwd'   # path injection
expect_exit 65 start-service -foo                 # flag injection
expect_exit 65 start-service                      # arity
expect_exit 65 llm-apply                          # arity
expect_exit 65 llm-apply notaninstance            # not an llm-* service name
expect_exit 65 llm-apply '../../etc/passwd'       # path injection
expect_exit 65 llm-apply -foo                     # flag injection
expect_exit 65 llm-stop notaninstance             # not an llm-* service name

if sudo -u panel sudo -n /usr/local/libexec/panel-do list-services | python3 -c 'import json,sys
d = json.load(sys.stdin)
assert isinstance(d, list) and all(isinstance(s.get("name"), str) for s in d)'; then
  info "  ok: list-services emits a valid service JSON array"
else
  die "  FAIL: list-services did not emit a valid service JSON array"
fi

# GPU pool actions (panel's GPU view): the read-only ones must emit valid
# JSON even with an empty fleet; the mutating ones are NOT exercised here
# (llm-apply would (re)create a container) — their arg validation above is
# the negative coverage.
if sudo -u panel sudo -n /usr/local/libexec/panel-do llm-fleet | python3 -c 'import json,sys
d = json.load(sys.stdin)
assert isinstance(d, list) and all(set(x) >= {"name", "display", "cards"} for x in d)'; then
  info "  ok: llm-fleet emits a valid fleet JSON array"
else
  die "  FAIL: llm-fleet did not emit a valid fleet JSON array"
fi
if sudo -u panel sudo -n /usr/local/libexec/panel-do llm-live | python3 -c 'import json,sys
d = json.load(sys.stdin)
assert isinstance(d, list) and all(set(x) >= {"service", "state", "gpus"} for x in d)'; then
  info "  ok: llm-live emits a valid live-state JSON array"
else
  die "  FAIL: llm-live did not emit a valid live-state JSON array"
fi
# Positive cross-check: llm-live must name EXACTLY the llm-* containers the
# host has. The shape check above passes for [] — and a broken label filter
# (the engine matches label VALUES exactly, no regex) yields [] while the
# pool's conflict detection sits silently disabled. Both sides empty on a
# fresh host = still passes.
want_live="$(sudo docker ps -a --format '{{.Names}} {{.Label "com.docker.compose.service"}}' \
  | awk '$2 ~ /^llm-[a-z0-9][a-z0-9-]*$/ {print $1}' | sort)"
got_live="$(sudo -u panel sudo -n /usr/local/libexec/panel-do llm-live \
  | python3 -c 'import json,sys; print("\n".join(sorted(x["container"] for x in json.load(sys.stdin))))')"
if [[ "$want_live" == "$got_live" ]]; then
  info "  ok: llm-live sees the same llm-* containers as the host"
else
  die "  FAIL: llm-live disagrees with the host — host has: [$(echo $want_live | tr '\n' ' ')] llm-live: [$(echo $got_live | tr '\n' ' ')]"
fi

# update-report must emit valid JSON even before the first update (no state
# file yet, no images) — the dashboard polls it unconditionally.
if sudo -u panel sudo -n /usr/local/libexec/panel-do update-report | python3 -c 'import json,sys
d = json.load(sys.stdin)
assert isinstance(d, dict) and "services" in d and "log_tail" in d and "timer_enabled" in d'; then
  info "  ok: update-report emits valid JSON"
else
  die "  FAIL: update-report did not emit valid JSON"
fi

info "panel-do installed. /api/actions in the panel now works."
echo "  registry: sudo -u panel sudo -n /usr/local/libexec/panel-do --list | python3 -m json.tool"
