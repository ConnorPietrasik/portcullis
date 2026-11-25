#!/usr/bin/env bash
# gpu-mlen-test.sh — trial a radiance max-model-len (or a model's whole size) and read the startup log.
#
# Runs a THROWAWAY radiance container — the same image the fleet runs — on a NEW config this
# script generates in /tmp, and streams the startup log at radiance's DEFAULT verbosity
# (info; the fleet's RADIANCE_LOG=warn is NOT set). The real configs in compose/gpu-configs/
# are read at most, never touched. Use it to decide an entry's max-model-len — or whether
# "no bound at all" admits — by varying one value and reading what the cards say:
#
#   refused (the figure you are sizing against):
#     warm-up: NoctQ-V4 stays cold: it needs 34288 MiB on card(s) 5,6 and that much cannot
#     be made free there; staying: Minimax-H3-T2V
#   admitted:
#     model NoctQ-V4: loaded on card(s) 5,6 in 41.3 s, holding 24.3 GiB a card
#
# Vary the value until the line flips; the refused figure is the exact number the pair cannot
# make free, so it tells you where the bound has to land. An on-demand load (a request while
# it is cold) fails with the same why at ERROR:  model NoctQ-V4: not loaded: <why>
#
# Usage (on the server; needs docker):
#   tools/gpu-mlen-test.sh --base FILE --set-mlen NAME=ROWS [--set-mlen NAME=ROWS ...]
#                          [--unbounded NAME]... [--drop-entry NAME]... [options]
#   tools/gpu-mlen-test.sh --model PATH [--entry NAME] [--tp N] [--mlen ROWS]
#                          [--with PATH[:NAME[:TP]]]... [options]
#
#   --base FILE           start from a real config (the repo's compose/gpu-configs/... or the
#                         deployed /opt/panel/... — read-only), rewriting only the named
#                         entries in the generated copy
#   --set-mlen NAME=ROWS  set that entry's max-model-len (replaces any existing line)
#   --unbounded NAME      remove the entry's max-model-len line — the model's own length
#   --drop-entry NAME     remove that entry from the generated copy (test a model alone)
#   --model PATH          minimal config: the model under test (host path under /models/)
#   --entry NAME          its name in the generated config (default: the file's base name)
#   --tp N                its tp (minimal mode; default: the number of cards)
#   --mlen ROWS           its max-model-len (minimal mode; default: none — own length)
#   --with PATH[:NAME[:TP]]
#                         another entry in the same file (minimal mode, repeatable) — pass
#                         the video model to test the pair as deployed; a path matching
#                         *minimax* gets vision-tower-placement: ram, like the fleet's
#   --cards 5,6           the physical cards (ROCR_VISIBLE_DEVICES; default: the cards of the
#                         running gpu-gentp2, else 5,6)
#   --image IMG           the radiance image (default: the running gpu-* container's image,
#                         else stilldeadcode/radiance:latest)
#   --timeout S           how long to watch the startup (default 600 s)
#   --host-mib N          pinned host memory for the minimal config (default 24576)
#   --keep                leave the container, the generated config and the log behind
#   --force               run even though a fleet container is running on the same cards
#
# The probe is a SEPARATE process: the running fleet sees none of its VRAM and it sees none
# of the fleet's, so on shared cards the two would OOM-fight each other and the figure in
# the log would be a lie. The script refuses to start over a running gpu-* container that
# shares a card with --cards (stop it first, or --force knowingly).
set -euo pipefail

BASE=""
MODEL="" ENTRY="" TP="" MLEN=""
CARDS="" IMAGE="" TIMEOUT=600 HOST_MIB=24576
KEEP=0 FORCE=0
SET_MLENS=() DROP_ENTRIES=() WITHS=()

die() { printf 'gpu-mlen-test: %s\n' "$*" >&2; exit 2; }

while (( $# )); do
  case "$1" in
    --base)      BASE=${2:?--base needs a file}; shift 2 ;;
    --model)     MODEL=${2:?--model needs a path}; shift 2 ;;
    --entry)     ENTRY=${2:?--entry needs a name}; shift 2 ;;
    --tp)        TP=${2:?--tp needs a number}; shift 2 ;;
    --mlen)      MLEN=${2:?--mlen needs a row count}; shift 2 ;;
    --set-mlen)  SET_MLENS+=("${2:?--set-mlen needs NAME=ROWS}"); shift 2 ;;
    --unbounded) SET_MLENS+=("${2:?--unbounded needs a name}=0"); shift 2 ;;
    --drop-entry) DROP_ENTRIES+=("${2:?--drop-entry needs a name}"); shift 2 ;;
    --with)      WITHS+=("${2:?--with needs a path}"); shift 2 ;;
    --cards)     CARDS=${2:?--cards needs a list like 5,6}; shift 2 ;;
    --image)     IMAGE=${2:?--image needs an image ref}; shift 2 ;;
    --timeout)   TIMEOUT=${2:?--timeout needs seconds}; shift 2 ;;
    --host-mib)  HOST_MIB=${2:?--host-mib needs a MiB count}; shift 2 ;;
    --keep)      KEEP=1; shift ;;
    --force)     FORCE=1; shift ;;
    # the help IS the leading comment block — end at the first code line, so
    # editing the header never breaks this again
    -h|--help)   sed -n "2,$(( $(grep -n -m1 -E '^[[:space:]]*[^#[:space:]]' "$0" | cut -d: -f1) - 1 ))p" "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) die "unknown argument: $1 (see --help)" ;;
  esac
done

command -v docker >/dev/null || die "docker is not on PATH (run this on the server, as a user with docker)"
[[ -n $BASE || -n $MODEL ]] || die "nothing to test: --base FILE (a real config) or --model PATH (minimal)"
[[ -z $BASE || -z $MODEL ]] || die "choose one: --base FILE (override a real config) or --model PATH (minimal)"
[[ $TIMEOUT =~ ^[0-9]+$ ]] || die "--timeout must be whole seconds"

re_hdr='^  ([A-Za-z0-9._-]+):[[:space:]]*$'   # an entry header: two spaces, name, colon
re_attr='^    '                                # an entry attribute: four spaces
re_mlen='^[[:space:]]+max-model-len:[[:space:]]'
re_apik='^[[:space:]]+api-key(-env)?[[:space:]]*:'

# Rewrite one entry's max-model-len in a config copy. $1=file $2=entry name
# $3=rows ("" or 0 = remove the line). The repo owns this format — the same shape
# gen_jobs.parse_slot_config reads — so line surgery is sound.
transform_config() {
  local file=$1 name=$2 mlen=$3
  local -a lines=()
  mapfile -t lines <"$file"
  local -a out=()
  local n=${#lines[@]} i=0 in_models=0 line l2 b entry
  while (( i < n )); do
    line=${lines[i]}
    if (( ! in_models )); then
      out+=("$line")
      [[ $line == "models:" ]] && in_models=1
      i=$((i+1)); continue
    fi
    if [[ -z ${line//[[:space:]]/} || $line =~ ^[[:space:]]*# ]]; then
      out+=("$line"); i=$((i+1)); continue
    fi
    if [[ $line =~ ^[^[:space:]] ]]; then      # a new top-level key ends models:
      in_models=0; out+=("$line"); i=$((i+1)); continue
    fi
    if [[ $line =~ $re_hdr ]]; then            # an entry: header + its attribute block
      entry=${BASH_REMATCH[1]}
      local -a block=("$line")
      i=$((i+1))
      while (( i < n )); do
        l2=${lines[i]}
        [[ -z ${l2//[[:space:]]/} || $l2 =~ ^[[:space:]]*# || $l2 =~ $re_attr ]] || break
        block+=("$l2"); i=$((i+1))
      done
      if [[ $entry == "$name" && $mlen == __DROP_ENTRY__ ]]; then
        :                                       # dropped: the block is not printed
      elif [[ $entry == "$name" ]]; then
        local -a kept=()
        for b in "${block[@]}"; do
          [[ $b =~ $re_mlen ]] && continue      # the old line goes either way
          kept+=("$b")
        done
        out+=("${kept[@]}")
        [[ -n $mlen && $mlen != 0 ]] && out+=("    max-model-len: $mlen")
      else
        out+=("${block[@]}")
      fi
      continue
    fi
    out+=("$line"); i=$((i+1))
  done
  printf '%s\n' "${out[@]}"
}

# The probe is network-less and short-lived: it needs no API key, and the base config's
# api-key(-env) line would ask for one (and pull the real key into the probe). Drop it.
strip_api_key() {
  grep -vE "$re_apik" || true
}

# The minimal config: the model under test + its --with co-residents, the production
# memory/scheduler shape. Reads the globals MODEL TP MLEN HOST_MIB CARDS WITHS; ENTRY is
# derived from the path when unset.
gen_minimal_config() {
  local path=$MODEL ncards w wpath wname wtp
  [[ -n ${ENTRY:-} ]] || { ENTRY=${MODEL##*/}; ENTRY=${ENTRY%%.*}; }
  ncards=$(awk -F, '{print NF}' <<<"${CARDS:-}")
  [[ $ncards =~ ^[0-9]+$ && $ncards -ge 1 ]] || ncards=1
  [[ -n ${TP:-} ]] || TP=$ncards
  {
    echo "server:"
    echo "  host: 0.0.0.0"
    echo "  port: 8000"
    echo "memory:"
    echo "  gpu-headroom-mib: 96"
    echo "  host-mib: $HOST_MIB"
    echo "scheduler:"
    echo "  load-timeout: 600s"
    echo "  drain-timeout: 30s"
    echo "  warm-up: true"
    echo "models:"
    echo "  $ENTRY:"
    echo "    model: $path"
    echo "    tp: $TP"
    [[ -n ${MLEN:-} ]] && echo "    max-model-len: $MLEN"
    echo "    park: {ram: never}"
    for w in ${WITHS[@]+"${WITHS[@]}"}; do
      IFS=':' read -r wpath wname wtp <<<"$w"
      [[ -n ${wname:-} ]] || { wname=${wpath##*/}; wname=${wname%%.*}; }
      [[ -n ${wtp:-} ]] || wtp=$TP
      echo "  $wname:"
      echo "    model: $wpath"
      echo "    tp: $wtp"
      [[ $wpath == *minimax* ]] && echo "    vision-tower-placement: ram"
      echo "    park: {ram: never}"
    done
  } | strip_api_key
}

WORK=$(mktemp -d /tmp/radiance-mlen-test.XXXXXX)
CFG=$WORK/probe.yaml
LOG=$WORK/startup.log
CNAME="" TEE=""

cleanup() {
  [[ -n ${TEE:-} ]] && kill "$TEE" 2>/dev/null
  if [[ ${KEEP:-0} != 1 ]]; then
    [[ -n ${CNAME:-} ]] && docker rm -f "$CNAME" >/dev/null 2>&1
    [[ -n ${WORK:-} ]] && rm -rf "$WORK"
  fi
}
trap cleanup EXIT

# ── build the config ────────────────────────────────────────────────────────────
if [[ -n $BASE ]]; then
  [[ -f $BASE ]] || die "--base: no such file: $BASE"
  strip_api_key <"$BASE" >"$CFG"
  for spec in ${SET_MLENS[@]+"${SET_MLENS[@]}"}; do
    name=${spec%%=*}; rows=${spec#*=}
    [[ $name =~ ^[A-Za-z0-9._-]+$ ]] || die "--set-mlen: bad entry name: $name"
    [[ $rows =~ ^[0-9]+$ ]] || die "--set-mlen: bad row count: $rows"
    grep -qE "^  ${name}:[[:space:]]*$" "$CFG" || die "--set-mlen: no entry named '$name' in $BASE"
    transform_config "$CFG" "$name" "$rows" >"$CFG.new" && mv "$CFG.new" "$CFG"
  done
  for name in ${DROP_ENTRIES[@]+"${DROP_ENTRIES[@]}"}; do
    [[ $name =~ ^[A-Za-z0-9._-]+$ ]] || die "--drop-entry: bad entry name: $name"
    grep -qE "^  ${name}:[[:space:]]*$" "$CFG" || die "--drop-entry: no entry named '$name' in $BASE"
    transform_config "$CFG" "$name" __DROP_ENTRY__ >"$CFG.new" && mv "$CFG.new" "$CFG"
  done
  [[ ${#SET_MLENS[@]} -gt 0 || ${#DROP_ENTRIES[@]} -gt 0 ]] \
    || die "override mode: nothing to change — give --set-mlen / --unbounded / --drop-entry"
else
  gen_minimal_config >"$CFG"
fi

# the probe config's entries — the wait loop below stops when each has had its
# say in the log (a load line, a stays-cold, or a not-loaded error)
ENTRIES=()
mapfile -t ENTRIES < <(
  awk '/^models:/{m=1; next} m && /^[^[:space:]]/{m=0} m && /^  [A-Za-z0-9._-]+:[[:space:]]*$/{sub(/^ +/, ""); sub(/:.*$/, ""); print}' "$CFG"
)

# every model path in the generated config is a HOST path — its parent must exist, and it
# is what the probe container mounts (read-only), the way the fleet blocks mount their class
MODEL_DIRS=()
while IFS= read -r mp; do
  parent=$(dirname "$mp")
  [[ -e $parent ]] || die "model path not on this host: $mp (the config's /models/... paths are host paths)"
  for d in ${MODEL_DIRS[@]+"${MODEL_DIRS[@]}"}; do [[ $d == "$parent" ]] && continue 2; done
  MODEL_DIRS+=("$parent")
done < <(grep -E '^[[:space:]]+model:' "$CFG" | sed -E 's/^[[:space:]]+model:[[:space:]]*//')

echo "── the probe config ($CFG) ──"
cat "$CFG"
echo

# ── the launch shape: x-gpu-base, minus the network and the API key ───────────
[[ -n $IMAGE ]] || IMAGE=$(docker inspect -f '{{.Config.Image}}' gpu-gentp2 2>/dev/null) || true
if [[ -z ${IMAGE:-} ]]; then
  for c in $(docker ps --format '{{.Names}}' | grep -E '^gpu-' || true); do
    IMAGE=$(docker inspect -f '{{.Config.Image}}' "$c" 2>/dev/null) && break
  done
fi
IMAGE=${IMAGE:-stilldeadcode/radiance:latest}

[[ -n $CARDS ]] || CARDS=$(docker inspect -f '{{json .Config.Env}}' gpu-gentp2 2>/dev/null \
  | grep -oE 'ROCR_VISIBLE_DEVICES=[^"]*' | cut -d= -f2) || true
CARDS=${CARDS:-5,6}

if (( ! FORCE )); then
  for c in $(docker ps --format '{{.Names}}' | grep -E '^gpu-' || true); do
    rcr=$(docker inspect -f '{{json .Config.Env}}' "$c" 2>/dev/null \
      | grep -oE 'ROCR_VISIBLE_DEVICES=[^"]*' | cut -d= -f2)
    [[ -n $rcr ]] || continue
    IFS=',' read -ra pa <<<"$CARDS"
    IFS=',' read -ra pb <<<"$rcr"
    for x in "${pa[@]}"; do
      for y in "${pb[@]}"; do
        if [[ $x == "$y" ]]; then
          die "container $c is running on card(s) $rcr — it shares a card with --cards $CARDS.
The probe sees none of the fleet's VRAM and it sees none of the probe's: on shared cards
the two OOM-fight and the figure in the log is a lie. Stop it first (docker stop $c, or
the GPUs view), or pass --force knowingly."
        fi
      done
    done
  done
fi

CNAME=mlen-probe-$(date +%H%M%S)
VOL_ARGS=()
for d in ${MODEL_DIRS[@]+"${MODEL_DIRS[@]}"}; do VOL_ARGS+=(-v "$d:$d:ro"); done

echo "── running: image $IMAGE · cards $CARDS · name $CNAME · network none · default (info) log ──"
CID=$(docker run -d \
  --name "$CNAME" \
  --network none \
  --user 1000:1000 \
  --init \
  --device /dev/kfd \
  --device /dev/dri \
  --group-add 987 \
  --group-add 983 \
  --security-opt seccomp=unconfined \
  --ulimit memlock=-1 \
  -e "ROCR_VISIBLE_DEVICES=$CARDS" \
  -v "$CFG:/etc/radiance/radiance.yaml:ro" \
  "${VOL_ARGS[@]}" \
  "$IMAGE" \
  --config /etc/radiance/radiance.yaml)

# the startup log, live — this is what you are here for
docker logs -f "$CID" 2>&1 | tee "$LOG" &
TEE=$!

# until every entry has had its say in the log, the container dies, or the
# timeout. A multi-entry fleet's warm-up says it all with its terminal line
# ("every model has been loaded once" / "warm-up: stops:"); a SINGLE entry
# has no warm-up thread (radiance warms fleets of two or more, ref/radiance
# core/fleet/fleet.cpp) — its say is its own line, and without it this loop
# would watch to the timeout an answer that is already on screen.
deadline=$(( $(date +%s) + TIMEOUT ))
while :; do
  if grep -qE 'every model has been loaded once|warm-up: stops:' "$LOG" 2>/dev/null; then
    break
  fi
  quiet=0
  if [[ ${#ENTRIES[@]} -gt 0 ]]; then
    quiet=1
    for en in "${ENTRIES[@]}"; do
      if ! grep -qF "model $en: loaded on card(s)" "$LOG" 2>/dev/null \
        && ! grep -qF "model $en: not loaded:" "$LOG" 2>/dev/null \
        && ! grep -qF "warm-up: $en stays cold:" "$LOG" 2>/dev/null; then
        quiet=0
        break
      fi
    done
    if (( quiet )); then break; fi
  fi
  running=$(docker inspect -f '{{.State.Running}}' "$CID" 2>/dev/null) || running=false
  [[ $running == true ]] || break
  (( $(date +%s) < deadline )) || break
  sleep 2
done

echo
echo "── what the startup said (full log: $LOG) ──"
grep -nE 'loaded on card|stays cold|not loaded|waiting for room|warm-up:' "$LOG" \
  || echo "(no load/warm-up lines found — the log above is the whole story)"

if (( KEEP )); then
  echo
  echo "kept: container $CNAME (docker logs -f $CNAME · docker rm -f $CNAME to end it),"
  echo "      config $CFG, log $LOG"
else
  [[ -n ${TEE:-} ]] && kill "$TEE" 2>/dev/null || true
  docker rm -f "$CNAME" >/dev/null 2>&1
  rm -rf "$WORK"
  TEE="" CNAME="" WORK=""
  echo
  echo "(probe removed; the log is gone with it — re-run with --keep to keep it)"
fi
