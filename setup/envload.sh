# shellcheck shell=bash
# Shared env-file parser. Source this, don't execute it. Deliberately sets no
# shell options — safe to source from anywhere, including 07-doctor.sh, which
# must not pick up `set -e`.
#
# env_file_load <file> — load a KEY=VALUE env file into the environment
# (export) WITHOUT executing it, and refuse anything the file's other readers
# would parse differently:
#   - docker compose auto-loads the deployed .env for the containers
#   - the systemd units read REDUCED files derived from that same .env by
#     raw-line grep (.panel-api.env for panel-api, .panel-ddns.env for
#     panel-ddns — see derive_reduced_env, setup/common.sh)
#   - this parser feeds the deploy scripts' own checks and substitutions
# If this parser accepts a file, the value the deploy scripts see is EXACTLY
# the value the containers and the systemd units receive.
#
# Never `source` an env file: the file would run as bash, so a hand-pasted
# value diverges from what the running processes get:
#   KEY=abc def   → "def: command not found" (deploy aborts, rc=127)
#   KEY=abc$UNSET → "UNSET: unbound variable" under set -u (deploy aborts)
#   KEY=a`id`b    → the command executes silently at deploy time (rc=0)
#
# Accepted lines (all readers agree on the resulting value):
#   - blank lines; lines whose first non-space char is #
#   - KEY=VALUE — KEY is [A-Za-z_][A-Za-z0-9_]*, whitespace around KEY and =
#     is ok; VALUE is trimmed and one pair of matching enclosing quotes is
#     stripped (all readers do this)
#   - an optional leading `export ` — compose strips it (and the old `source`
#     flow ran it as a builtin), but systemd's EnvironmentFile does not know
#     the prefix: the key becomes "export KEY", an invalid name, and systemd
#     drops the variable SILENTLY (journal notice only). Accepted with a
#     WARNING for container-only keys, REFUSED for the keys in
#     __envload_no_export_keys — silently losing NAMESILO_API_KEY stops the
#     apex A record tracking with no deploy error, and losing PANEL_TOKEN
#     rejects every panel-api request
#   - a bare KEY line without `=` (the "inherit from environment" form) —
#     systemd always ignores it (it wants KEY=VALUE) and so does this
#     parser; docker compose inherits it from its OWN process environment
#     when the key is set there (compose >= 2.33 feeds os.Environ() into
#     the .env lookup). `sudo docker compose` runs with sudo's sanitized
#     environment (PATH/HOME/USER/TERM/...), not the deployer's shell, and
#     none of this file's secret keys ever sit there — so a bare line here
#     cannot diverge in practice: warned about and skipped, not a failure
#
# Refused lines (one clear message per line, rc=1):
#   - `=` lines whose key the readers can't share (empty, space in it, `;`,
#     digit-led, `-`/`.`/brackets in it) — current compose ACCEPTS `-`/`.`
#     and digit-led keys (the container would get a variable systemd can't
#     set) but REFUSES empty/space keys; systemd drops all of them as
#     invalid names (journal error only). No shared value exists, so refuse
#   - `export KEY=...` where KEY is in __envload_no_export_keys (see above)
#     — compose keeps it, systemd drops it
#   - an unterminated enclosing quote (compose refuses the file), or content
#     after the closing quote (compose drops it / re-parses it; systemd
#     glues it into the value — `'abc' # c` becomes `abc# c`)
#   - `$` in an unquoted or double-quoted value — compose interpolates it
#     from the host environment (plus earlier .env lines), systemd sees it
#     literally
#   - `\` in an unquoted or double-quoted value — systemd eats the backslash
#     (a trailing one continues the line); compose keeps unquoted values
#     literal and expands escape sequences in double-quoted ones
#   - a space-`#` sequence in an unquoted value — compose cuts the value
#     there (inline comment), systemd keeps it
#   - any other stray line without `=` (multi-word text, `;` comments,
#     `KEY:value` yaml-style) — compose refuses the file at `up` time
#     ("key cannot contain a space" / "unexpected character in variable
#     name") or parses it as an assignment systemd would ignore
# Single-quoted values are literal in every reader, so their content is
# unrestricted.
#
# Returns 0 if every line parsed, 1 if any line was refused (each offender
# warned), 2 if the file is missing. Requires warn() from the caller (falls
# back to stderr if it is not defined).

# The keys each panel-uid reduced env file may carry — the SINGLE SOURCE of
# truth, shared by the derivation (sync_*_env, setup/common.sh) and the
# doctor's whitelist check (setup/07-doctor.sh — it verifies every line of the
# deployed files is one of these, so a leaked key is caught even if it is a
# secret added to .env later). Keep in sync with what the services actually
# read: panel-api/main.py (PANEL_TOKEN, PANEL_DEV, PANEL_DISK_MOUNTS) and
# ddns/ddns.py (NAMESILO_API_KEY, optional DDNS_HOSTS).
panel_api_env_keys=(PANEL_TOKEN PANEL_DEV PANEL_DISK_MOUNTS)
panel_ddns_env_keys=(NAMESILO_API_KEY DDNS_HOSTS)

# Keys whose line must not carry an `export ` prefix (refused, not merely
# warned): exactly the keys the REDUCED ENV FILES carry above (union of the
# two lists) — .panel-ddns.env (panel-ddns.service's EnvironmentFile=, ddns.py's
# os.environ.get) and .panel-api.env (panel-api.service's EnvironmentFile=),
# both derived by raw-line grep (derive_reduced_env), whose ^KEY= pattern
# requires the line to start with the key; systemd would drop an `export `-
# prefixed copy SILENTLY (journal error only), so an `export `-prefixed line
# here breaks DNS tracking / panel-api with no deploy error.
__envload_no_export_keys=" ${panel_api_env_keys[*]} ${panel_ddns_env_keys[*]} "

__envload_warn() {
  if declare -F warn >/dev/null 2>&1; then
    warn "$@"
  else
    printf 'WARN %s\n' "$*" >&2
  fi
}

env_file_load() {
  local file="$1" line key value rest trail q keypart line_bad bad=0 exported trimmed
  [[ -f "$file" ]] || return 2
  while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line%$'\r'}"
    # Optional `export ` prefix: compose strips it (the old `source` flow
    # ran it as a builtin), but systemd would drop the variable silently.
    # Warn here; the refusal for keys in __envload_no_export_keys is below,
    # at key extraction.
    exported=0
    if [[ "$line" =~ ^[[:space:]]*export[[:space:]]+(.*)$ ]]; then
      line="${BASH_REMATCH[1]}"
      exported=1
      if [[ -n "${line//[[:space:]]/}" ]]; then
        __envload_warn "$file: 'export ' prefix — docker compose strips it, but systemd's EnvironmentFile does NOT and would drop the variable silently: $line"
      fi
    fi
    [[ "$line" =~ ^[[:space:]]*$ ]] && continue
    [[ "$line" =~ ^[[:space:]]*# ]] && continue
    if [[ "$line" =~ ^[[:space:]]*([A-Za-z_][A-Za-z0-9_]*)[[:space:]]*=(.*)$ ]]; then
      key="${BASH_REMATCH[1]}"; value="${BASH_REMATCH[2]}"
      if (( exported )) && [[ "$__envload_no_export_keys" == *" $key "* ]]; then
        __envload_warn "$file: 'export ' prefix on $key — docker compose strips it, but systemd does NOT: it would drop $key silently (journal error only). Remove the 'export ' prefix: $line"
        bad=1
        continue
      fi
      value="${value#"${value%%[![:space:]]*}"}"   # ltrim
      value="${value%"${value##*[![:space:]]}"}"   # rtrim
      line_bad=0
      q=""
      case "$value" in
        \"*) q='"' ;;
        \'*) q="'" ;;
      esac
      if [[ -n "$q" ]]; then
        rest="${value:1}"
        if [[ "$rest" != *"$q"* ]]; then
          __envload_warn "$file: unterminated $q-quoted value (docker compose refuses the file): $line"
          line_bad=1
        else
          value="${rest%%"$q"*}"
          trail="${rest#*"$q"}"
          if [[ ! "$trail" =~ ^[[:space:]]*$ ]]; then
            __envload_warn "$file: content after the closing $q quote (docker compose drops it silently): $line"
            line_bad=1
          fi
        fi
      fi
      # Constructs the readers reinterpret (so the containers would receive
      # a different value than the deploy scripts) — refuse. Single-quoted
      # values are literal in every reader and are exempt.
      if (( line_bad )); then
        :
      elif [[ "$q" == '"' ]]; then
        if [[ "$value" == *'$'* ]]; then
          __envload_warn "$file: value contains \$ (docker compose interpolates it from the host environment; systemd sees it literally): $line"
          line_bad=1
        elif [[ "$value" == *\\* ]]; then
          __envload_warn "$file: value contains a backslash (escape/continuation rules differ between readers): $line"
          line_bad=1
        fi
      elif [[ -z "$q" ]]; then
        if [[ "$value" == *'$'* ]]; then
          __envload_warn "$file: value contains \$ (docker compose interpolates it from the host environment; systemd sees it literally): $line"
          line_bad=1
        elif [[ "$value" == *\\* ]]; then
          __envload_warn "$file: value contains a backslash (escape/continuation rules differ between readers): $line"
          line_bad=1
        elif [[ "$value" == *" #"* ]]; then
          __envload_warn "$file: value contains ' #' (docker compose treats the rest as a comment; systemd keeps it): $line"
          line_bad=1
        fi
      fi
      if (( line_bad )); then
        bad=1
      else
        export "$key=$value"
      fi
    elif [[ "$line" != *=* ]]; then
      # Stray line without '='. Only a bare KEY (the "inherit from the
      # environment" form) is ignored by BOTH readers — everything else
      # diverges: compose refuses "FOO BAR" ("key cannot contain a space")
      # and "; note" ("unexpected character ';' in variable name") at up
      # time, and parses "KEY:value" (yaml-style) as an assignment that
      # systemd ignores.
      trimmed="${line#"${line%%[![:space:]]*}"}"
      trimmed="${trimmed%"${trimmed##*[![:space:]]}"}"
      if [[ "$trimmed" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]]; then
        __envload_warn "$file: bare line without '=' (systemd ignores it; docker compose inherits it only if the key is already set in its own environment — skipping): $line"
      else
        __envload_warn "$file: stray line without '=' — docker compose would reject the file (or parse it as yaml-style KEY:value) at up time; write KEY=VALUE: $line"
        bad=1
      fi
    else
      # '=' present but the key the readers can't share (empty, digit-led,
      # or a char outside [A-Za-z0-9_] like '-', '.', space, ';') — name the
      # offender instead of a generic "unparseable".
      keypart="${line%%=*}"
      keypart="${keypart#"${keypart%%[![:space:]]*}"}"
      keypart="${keypart%"${keypart##*[![:space:]]}"}"
      __envload_warn "$file: invalid key '$keypart' — keys must match [A-Za-z_][A-Za-z0-9_]* (systemd drops such names silently; docker compose may keep or reject them): $line"
      bad=1
    fi
  done <"$file"
  return "$bad"
}
