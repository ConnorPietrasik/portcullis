# shellcheck shell=bash
# Shared env-file parser. Source this, don't execute it (sets no shell
# options — safe to source from anywhere, including 07-doctor.sh, which must
# not pick up `set -e`).
#
# env_file_load <file> — load a KEY=VALUE env file into the environment
# (export) WITHOUT executing it, and refuse anything the file's other readers
# (docker compose, the systemd units' EnvironmentFile) would parse
# differently: $ or backslash in unquoted/double-quoted values, ' #' in
# unquoted values, keys outside [A-Za-z_][A-Za-z0-9_]*, an `export ` prefix
# on the reduced-env keys, unterminated quotes, stray lines. If this parser
# accepts the file, the deploy scripts see EXACTLY the value the containers
# and systemd units receive. Single-quoted values are literal in every reader
# and unrestricted.
#
# Returns 0 if every line parsed, 1 if any line was refused (each offender
# warned), 2 if the file is missing. Requires warn() from the caller (falls
# back to stderr).

# The keys each panel-uid reduced env file may carry — the SINGLE SOURCE of
# truth, shared by the derivation (sync_*_env, setup/common.sh) and the
# doctor's whitelist check (setup/07-doctor.sh — it verifies every line of
# the deployed files is one of these, so a leaked key is caught even if it
# is a secret added to .env later). Keep in sync with what the services
# actually read: panel-api/main.py and ddns/ddns.py.
panel_api_env_keys=(PANEL_TOKEN PANEL_DEV PANEL_DISK_MOUNTS)
panel_ddns_env_keys=(NAMESILO_API_KEY DDNS_HOSTS)

# Keys whose line must not carry an `export ` prefix (refused, not merely
# warned): exactly the reduced-env keys above — systemd's EnvironmentFile
# drops an `export `-prefixed line SILENTLY, and derive_reduced_env's ^KEY=
# pattern would simply not match it.
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
    # Optional `export ` prefix: compose strips it, but systemd would drop
    # the variable silently. Warn here; refusal for the reduced-env keys
    # happens at key extraction below.
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
      # Constructs the readers reinterpret (the containers would receive a
      # different value than the deploy scripts) — refuse. Single-quoted
      # values are literal in every reader and exempt.
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
      # environment" form) is ignored by both readers — everything else
      # diverges (compose refuses it at up time or parses yaml-style
      # KEY:value, which systemd ignores).
      trimmed="${line#"${line%%[![:space:]]*}"}"
      trimmed="${trimmed%"${trimmed##*[![:space:]]}"}"
      if [[ "$trimmed" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]]; then
        __envload_warn "$file: bare line without '=' (systemd ignores it; docker compose inherits it only if the key is already set in its own environment — skipping): $line"
      else
        __envload_warn "$file: stray line without '=' — docker compose would reject the file (or parse it as yaml-style KEY:value) at up time; write KEY=VALUE: $line"
        bad=1
      fi
    else
      # '=' present but the key the readers can't share — name the offender
      # instead of a generic "unparseable".
      keypart="${line%%=*}"
      keypart="${keypart#"${keypart%%[![:space:]]*}"}"
      keypart="${keypart%"${keypart##*[![:space:]]}"}"
      __envload_warn "$file: invalid key '$keypart' — keys must match [A-Za-z_][A-Za-z0-9_]* (systemd drops such names silently; docker compose may keep or reject them): $line"
      bad=1
    fi
  done <"$file"
  return "$bad"
}
