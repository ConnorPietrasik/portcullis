#!/usr/bin/env bash
# panel-hosts.sh — pin every host the panel's Traefik routes to the server's LAN
# IP, in THIS machine's /etc/hosts.
#
# The router doesn't hairpin and can't be controlled, so from the LAN the public
# names can't get back to the server; this pins them to the LAN IP locally
# instead. Only the address changes, so SNI/TLS and Authelia keep working
# (wildcard cert covers everything).
#
# Hosts come from the Host(...) rules in compose/traefik/dynamic/*.yml of THIS
# checkout (working tree — uncommitted edits count). That's the same tree make
# update ships to the server, so: edit a route → run this. No pull/deploy on
# this side; pinning a name before the server routes it is harmless (Traefik
# 404s it until you do).
#
# USAGE (on the client machine, e.g. the main PC):
#   tools/panel-hosts.sh                  sync /etc/hosts (auto-sudo if needed)
#   tools/panel-hosts.sh 10.0.0.99        one-off server-IP override
#   PANEL_SERVER_IP=10.0.0.99 tools/panel-hosts.sh
#   tools/panel-hosts.sh --dry-run        show the managed block; exits 1 if it
#                                         would change (cron/watching friendly)
#   tools/panel-hosts.sh --remove         strip the managed block
#
# Exit codes: 0 = ok / no-op; 1 = --dry-run with pending changes, or a pin
# shadowed by an earlier line in the file (checked on every sync, even when
# the managed block itself is already in sync).
#
# Only the marked BEGIN/END block is managed — replaced in place when present,
# appended at the end when absent; idempotent.
# The rest of /etc/hosts is never touched; a pre-write backup lands at
# <hosts-file>.panel-hosts.bak. Wildcard rules (Host(`*.x.top`)) become wildcard
# hosts lines (glibc supports them). systemd-resolved consults /etc/hosts first,
# so this works with it enabled. The pin is per-machine and LAN-only; public
# DNS is never touched.
#
# PANEL_HOSTS_FILE overrides the target file (default /etc/hosts) — test hook.
set -euo pipefail

C_G=$'\033[1;32m'; C_Y=$'\033[1;33m'; C_R=$'\033[1;31m'; C_0=$'\033[0m'
info() { printf '%s==>%s %s\n' "$C_G" "$C_0" "$*"; }
warn() { printf '%s==>%s %s\n' "$C_Y" "$C_0" "$*"; }
die()  { printf '%s==>%s %s\n' "$C_R" "$*" >&2; exit 1; }

BEGIN_MARK="# BEGIN panel-hosts (managed by tools/panel-hosts.sh — DO NOT EDIT)"
END_MARK="# END panel-hosts"

SERVER_IP="${PANEL_SERVER_IP:-10.0.0.187}"
SERVER_IP_SET=0
HOSTS_FILE="${PANEL_HOSTS_FILE:-/etc/hosts}"
DRY_RUN=0
REMOVE=0

for a in "$@"; do
  case "$a" in
    --dry-run|-n) DRY_RUN=1 ;;
    --remove)     REMOVE=1 ;;
    -h|--help)
      # the header above is the manual — print it without the leading #
      sed -n '2,/^set -euo pipefail/p' "$0" | sed '$d' | sed 's/^# \{0,1\}//'
      exit 0 ;;
    -*) die "unknown flag: $a (see --help)" ;;
    *)  if (( SERVER_IP_SET )); then
          [[ $SERVER_IP == "$a" ]] || die "server IP given twice and differently ($SERVER_IP and $a)"
        fi
        SERVER_IP="$a"; SERVER_IP_SET=1 ;;
  esac
done

# dotted quad, octets ≤ 255
valid_ip() {
  [[ $1 =~ ^([0-9]{1,3})\.([0-9]{1,3})\.([0-9]{1,3})\.([0-9]{1,3})$ ]] || return 1
  local o
  for o in "${BASH_REMATCH[@]:1:4}"; do (( 10#$o <= 255 )) || return 1; done
}
valid_ip "$SERVER_IP" || die "not an IPv4 address: $SERVER_IP"

REPO_ROOT="$(cd "$(dirname "$(realpath "$0")")/.." && pwd)"
DYNAMIC_DIR="$REPO_ROOT/compose/traefik/dynamic"
[[ -d $DYNAMIC_DIR ]] || die "no $DYNAMIC_DIR — run this from the portcullis repo"

# ── extract the routed hosts ─────────────────────────────────────────────────
# Router rules only (rule: lines), comments stripped, every Host(...) group
# pulled out (handles Host(`a`,`b`) and Host(`a`) && Host(`b`)), backticks
# dropped, deduped + sorted.
# grep exits 1 on no match — || true keeps pipefail from killing the pipeline
# (an empty result is caught below, with a better message).
hosts_raw="$(
  { for f in "$DYNAMIC_DIR"/*.yml; do
      sed -e 's/#.*$//' "$f" | grep -E '^[[:space:]]*rule:[[:space:]]' || true
    done; } \
  | { grep -oE 'Host\([^)]*\)' || true; } \
  | { grep -oE '`[^`]+`' || true; } \
  | tr -d '`' \
  | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' \
  | { grep -v '^$' || true; } \
  | LC_ALL=C sort -u
)"

HOSTS=()
while IFS= read -r h; do
  [[ -n $h ]] || continue
  h="${h,,}" # DNS is case-insensitive; /etc/hosts is not
  # hostname labels, at least two of them; a single * per label allows
  # wildcard rules (Host(`*.x.top`)), which glibc /etc/hosts supports too.
  if [[ $h =~ ^[a-z0-9*]([a-z0-9*-]*[a-z0-9*])?(\.[a-z0-9*]([a-z0-9*-]*[a-z0-9*])?)+$ ]]; then
    HOSTS+=("$h")
  else
    die "a route in $DYNAMIC_DIR doesn't look like a hostname: '$h' — fix the rule (or extend this validator)"
  fi
done <<< "$hosts_raw"
(( ${#HOSTS[@]} )) || die "no Host(...) rules found in $DYNAMIC_DIR — nothing to pin"

commit="$(git -C "$REPO_ROOT" rev-parse --short HEAD 2>/dev/null || echo nogit)"

# The block is deterministic per (repo commit, server IP): no timestamps, so an
# unchanged repo → byte-identical /etc/hosts → no write, no backup churn.
tmp_block="$(mktemp)"
proposal="$(mktemp)"
tmp=""
trap 'rm -f "$tmp_block" "$proposal" "$tmp"' EXIT
{
  printf '%s\n' "$BEGIN_MARK"
  printf '# Routed hosts from this checkout (git %s), pinned to the server LAN IP.\n' "$commit"
  for h in "${HOSTS[@]}"; do printf '%s  %s\n' "$SERVER_IP" "$h"; done
  printf '%s\n' "$END_MARK"
} > "$tmp_block"

# Print everything outside the managed block (conservative: an unterminated
# BEGIN swallows the rest — the backup always covers a corrupted file).
strip_block() {
  awk -v begin="$BEGIN_MARK" -v end="$END_MARK" '
    index($0, begin) { skip = 1; next }
    index($0, end)   { skip = 0; next }
    !skip            { print }
  '
}

[[ -f $HOSTS_FILE ]] || die "no such file: $HOSTS_FILE"

# Build the proposed file, then compare byte-for-byte: identical → no write,
# no backup churn.
if (( REMOVE )); then
  if ! grep -qF "$BEGIN_MARK" "$HOSTS_FILE"; then
    info "no managed block in $HOSTS_FILE — nothing to remove"
    exit 0
  fi
  # Strip the block where it sits, then drop the trailing blank lines the
  # separator left where the block used to be.
  strip_block < "$HOSTS_FILE" \
    | awk '{ l[NR] = $0 } END { n = NR; while (n > 0 && l[n] == "") n--; for (i = 1; i <= n; i++) print l[i] }' \
    > "$proposal"
  if (( DRY_RUN )); then
    info "would remove the managed block from $HOSTS_FILE:"
    cat "$tmp_block"
    exit 1
  fi
else
  if grep -qF "$BEGIN_MARK" "$HOSTS_FILE"; then
    # Replace the existing block where it sits — lines before/after pass
    # through untouched. (Conservative like strip_block: an unterminated BEGIN
    # swallows the rest — the backup always covers a corrupted file.)
    awk -v begin="$BEGIN_MARK" -v end="$END_MARK" -v blockfile="$tmp_block" '
      BEGIN { while ((getline l < blockfile) > 0) bl[++n] = l; close(blockfile) }
      index($0, begin) { for (i = 1; i <= n; i++) print bl[i]; skip = 1; next }
      skip             { if (index($0, end)) skip = 0; next }
      { print }
    ' "$HOSTS_FILE" > "$proposal"
  else
    stripped="$(strip_block < "$HOSTS_FILE")"
    if [[ -n $stripped ]]; then
      printf '%s\n\n%s\n' "$stripped" "$(cat "$tmp_block")" > "$proposal"
    else
      cat "$tmp_block" > "$proposal"
    fi
  fi
  if (( DRY_RUN )); then
    if cmp -s "$proposal" "$HOSTS_FILE"; then
      info "already in sync — $HOSTS_FILE untouched"
      exit 0
    fi
    info "managed block as it would be written (server $SERVER_IP, git $commit):"
    cat "$tmp_block"
    warn "dry run — $HOSTS_FILE was not modified"
    exit 1
  fi
fi

# ── write (root) ─────────────────────────────────────────────────────────────
# Only when there is something to write: remove always changes the file, and a
# sync whose proposal is byte-identical is skipped (no write, no backup churn).
# Auto-privilege only when it's actually needed: root or a user-writable target
# (the PANEL_HOSTS_FILE test hook) needs no sudo.
if (( REMOVE )) || ! cmp -s "$proposal" "$HOSTS_FILE"; then
  if (( EUID != 0 )) && [[ ! -w $HOSTS_FILE ]]; then
    command -v sudo >/dev/null 2>&1 || die "cannot write $HOSTS_FILE — re-run as root"
    # Carry the IP as a positional so it survives the sudo env scrub.
    args=()
    if (( REMOVE )); then args+=(--remove); fi
    exec env -u PANEL_SERVER_IP sudo -- "$0" "$SERVER_IP" "${args[@]}"
  fi

  backup="${HOSTS_FILE}.panel-hosts.bak"
  cp -p "$HOSTS_FILE" "$backup"

  # Same dir as the target so the final rename is an atomic same-filesystem mv.
  tmp="$(mktemp "$(dirname "$HOSTS_FILE")/.panel-hosts.XXXXXX")"
  cp "$proposal" "$tmp"
  chmod --reference="$HOSTS_FILE" "$tmp" 2>/dev/null || chmod 644 "$tmp"
  mv -f "$tmp" "$HOSTS_FILE"
  info "wrote $HOSTS_FILE (backup: $backup)"
else
  info "already in sync — $HOSTS_FILE untouched"
fi

if (( REMOVE )); then
  info "done — the panel names now resolve via normal DNS again"
  exit 0
fi

# ── verify ───────────────────────────────────────────────────────────────────
# Runs on every sync — including the in-sync no-op — so a pin shadowed by an
# earlier line is caught either way and the exit code is always trustworthy.
# Resolve the way glibc would: first matching line in the file wins, `*` is a
# one-or-more wildcard. (getent isn't usable here — for a missing name it falls
# back to DNS and would report the PUBLIC answer as a pass.)
resolve_in_hosts() { # <file> <name> → IP of the first matching line, or nothing
  local line ip name pat
  while IFS= read -r line || [[ -n $line ]]; do
    line="${line%%#*}" # inline comment
    [[ -z ${line//[[:space:]]/} ]] && continue
    read -r ip rest <<< "$line"
    [[ $ip =~ ^[0-9a-fA-F:.]+$ ]] || continue # not an address line
    for name in $rest; do
      if [[ $name == "$2" ]]; then
        printf '%s' "$ip"; return 0
      elif [[ $name == *'*'* ]]; then
        pat="^${name//./\\.}" # escape the dots
        pat="${pat//\*/.+}$"  # glibc: * matches one or more chars
        [[ $2 =~ $pat ]] && { printf '%s' "$ip"; return 0; }
      fi
    done
  done < "$1"
  return 0
}
ok=1
for h in "${HOSTS[@]}"; do
  ip="$(resolve_in_hosts "$HOSTS_FILE" "$h")"
  if [[ $ip == "$SERVER_IP" ]]; then
    info "  $h → $ip  ✓"
  else
    ok=0
    warn "  $h resolves to '${ip:-nothing}' in $HOSTS_FILE (expected $SERVER_IP) — a line earlier in the file wins: remove it, then re-run"
  fi
done
if (( ok )); then
  info "all pinned hosts resolve to $SERVER_IP in $HOSTS_FILE"
else
  warn "verification FAILED — the shadowed pins above don't take effect: remove the earlier lines, then re-run"
  exit 1
fi
