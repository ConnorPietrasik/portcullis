#!/usr/bin/env bash
# Build panel-do (privileged action dispatcher), install it immutable, set up sudoers,
# and run the negative-test suite from the design doc.
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
sudo chattr -i /usr/local/libexec/panel-do 2>/dev/null || true
sudo install -o root -g root -m 0755 "$REPO_ROOT/panel-do/panel-do" /usr/local/libexec/panel-do
sudo chattr +i /usr/local/libexec/panel-do

info "Installing sudoers rule (/etc/sudoers.d/panel)"
tmp="$(mktemp)"
cat >"$tmp" <<'EOF'
# Managed by panel setup. The 'panel' user may run ONLY the whitelist dispatcher.
Defaults:panel !requiretty
panel ALL=(root) NOPASSWD: /usr/local/libexec/panel-do
EOF
sudo install -o root -g root -m 0440 "$tmp" /etc/sudoers.d/panel
rm -f "$tmp"
sudo visudo -c >/dev/null || die "sudoers validation failed"

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

info "panel-do installed. /api/actions in the panel now works."
echo "  registry: sudo -u panel sudo -n /usr/local/libexec/panel-do --list | python3 -m json.tool"
