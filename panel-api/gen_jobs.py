"""Gen job queue — the panel's image/video generation section (panel tab "Gen").

A GEN SLOT is a ``gpu-*`` fleet instance whose radiance config lists at least
one model under ``/models/image/`` or ``/models/video/`` (the /models/{llm,
image,video} layout is the contract) — the gpu.private instances, discovered
from the deployed configs (``/opt/panel/gpu-configs/``, the repo copy in dev)
— no second model registry to drift. A slot's config is GEN-ONLY (no LLM
shares it); its entries are PARKING partners, never concurrent ones —
admission keeps ONE model per slot in flight (see ``admission``). Prompt
enhancement runs OFF-SLOT, on the settings' ENHANCE TARGET (default: the
gpu-writer instance's "Writer" model — a public chat instance the engine
reaches by container IP, like the slots), so a slot's VRAM never serves the
enhancer.

Three state files (next to this file; /opt/panel-api/ in production), the
gpu_pool.py pattern:
- ``gen-settings.json`` (0600) — admin-editable from the Gen tab: purposes
  (purpose key → model name), enhance (global toggle + per-gen-model prompt
  + the enhance target: instance + model), gc_days.
- ``gen-jobs.json`` (0600) — the FIFO: seq, jobs (id → record), order
  (oldest first).
- ``gen-capabilities.json`` (0600) — per-model capabilities DISCOVERED from
  radiance (``/v1/models`` → ``capabilities.image_edit``), cached by model
  name; the stored container path is the row's validity (a file swapped under
  a name re-probes). Never probed = ``edits: null`` = unverified, never
  "cannot" — the panel keeps no hand-written model table. See ``known_models``.

Job lifecycle: queued → enhancing → queued → starting → loading → running →
completed (a job without enhance skips straight from the first queued to
starting). `enhanced_prompt` set ⇒ the enhancement is done and only the gen
phase remains. `enhancing` is OFF-SLOT: no slot is claimed or started, and
the global enhance cap (not a slot's) gates it.

Admission — two independent caps, each checked against a fresh state read
before every claim (the gen-phase one is re-checked before the gen request
itself):
- GEN PHASE (per slot, GEN_CAP): at most GEN_CAP jobs whose gen request is
  on the slot (LOADING: admitted, about to be sent; RUNNING: in radiance)
  — one in progress + one queued in radiance, which sequences the model's
  work itself. A STARTING flight (the slot booting) has no gen request yet
  and consumes no headroom. Enhancement runs off-slot, so it never contends
  for the slot's VRAM either. Plus ONE MODEL AT A TIME per slot: a flight on
  a different model than one in flight (or starting) on that slot waits — the
  entries park for each other (see admission).
- ENHANCEMENT (system-wide, GEN_ENHANCE_CAP): at most GEN_ENHANCE_CAP jobs
  in ENHANCING — their rewrites all land on ONE entry (the enhance target),
  so the cap is global, not per slot. Enhancement runs BEFORE the slot
  phase and off-slot (a different card): a queued job enhances as soon as
  there is enhance headroom, without a slot being claimed or started. The
  target's max-num-seqs carries the cap (radiance queues excess sequences
  before 429ing).

Pure logic + these files: no FastAPI, no panel-do, no HTTP — callers pass
defs (gpu-fleet), live (gpu-live), ledger (gpu_pool) and ips (edge) in, and
run the privileged parts themselves. Operator view: docs/RUNBOOK.md → "Gen".
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import struct
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

DEV_MODE = os.environ.get("PANEL_DEV") == "1"

# Production lives under /opt/panel-api (panel-owned); dev mode keeps the
# files in the checkout, like the LLM settings (main.py).
SETTINGS_PATH = Path(
    os.environ.get("GEN_SETTINGS_PATH")
    or ("gen-settings.json" if DEV_MODE else "/opt/panel-api/gen-settings.json")
)
JOBS_PATH = Path(
    os.environ.get("GEN_JOBS_PATH")
    or ("gen-jobs.json" if DEV_MODE else "/opt/panel-api/gen-jobs.json")
)
LOCK_PATH = Path(
    os.environ.get("GEN_LOCK_PATH")
    or (".gen-jobs.lock" if DEV_MODE else "/opt/panel-api/.gen-jobs.lock")
)
# Discovered per-model capabilities (radiance's answer — see the module
# docstring). Keyed by model name (a name maps to one model file); the stored
# path is the row's validity: a file swapped under the name re-probes.
CAPS_PATH = Path(
    os.environ.get("GEN_CAPS_PATH")
    or ("gen-capabilities.json" if DEV_MODE else "/opt/panel-api/gen-capabilities.json")
)
# The radiance configs (one per gpu-* service, named after it) — the deployed
# copy is the running truth; dev reads the repo's.
CONFIG_DIR = Path(
    os.environ.get("GEN_CONFIG_DIR")
    or ("/opt/panel/gpu-configs" if not DEV_MODE else "compose/gpu-configs")
)

# ── the contracts ──────────────────────────────────────────────────────────
# Purpose keys: what the UI/servitor say; the admin maps them to models in
# the settings. RESERVED — a model literally named one of these could never
# be addressed by name, so the key set is closed over them. "edit" names the
# picture-reading path (/v1/images/edits): its job needs at least one source
# picture and an edit-capable model (validate_settings).
PURPOSE_KEYS = ("image", "video", "edit")
RESERVED_MODEL_NAMES = frozenset(PURPOSE_KEYS)

# /models layout: the 2nd path component of a model path is its class.
KIND_DIRS = frozenset({"llm", "image", "video"})

MODEL_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,127}$")  # entry names may carry case (Krea2, Minimax-H3-T2V)

# An EDIT's source pictures ride the create request as ``data:`` URLs (the
# shape radiance's /v1/images/edits takes) and are stored next to the
# artifact (before/after view, replay).
EDIT_INPUT_MIMES = frozenset({"image/png", "image/jpeg", "image/webp"})  # radiance's edit formats
# A mask is the alpha-having subset: radiance decodes a jpeg mask fully
# opaque, so nothing would change — a silent no-op, so the door refuses it.
EDIT_MASK_MIMES = frozenset({"image/png", "image/webp"})
# radiance's cap on an edit's pictures (up to ten); a model with a smaller
# own cap 400s at submit naming its number.
EDIT_MAX_IMAGES = 10
# Per-picture cap. The one size guard besides radiance's submit 400: the
# request is buffered whole with no transport cap, and a 30 MB upload would
# otherwise cost a slot start to fail (bound: 10 × 16 MiB + a mask).
EDIT_INPUT_MAX_BYTES = 16 * 1024 * 1024
# radiance's decoded-source budget (64 MP) on every picture and the mask: a
# 10000x10000 photo is a legal 16-MiB file that would fail after the slot
# started, so the door checks the header's dimensions (image_size).
EDIT_MAX_SOURCE_PIXELS = 1 << 26


def image_size(data: bytes) -> Optional[tuple[int, int]]:
    """(w, h) from the file's header — png (IHDR), webp (VP8/VP8L/VP8X), jpeg
    (first SOFn) — or None when unreadable (radiance's own decode answers
    then). Header-only: needed for the mask-size door and the 64 MP cap."""
    try:
        if data[:8] == b"\x89PNG\r\n\x1a\n":
            w, h = struct.unpack(">II", data[16:24])
            return (w, h) if w and h else None
        if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            if len(data) < 30:  # shorter than any chunk's header + dimension bytes
                return None
            chunk = data[12:16]
            if chunk == b"VP8 ":  # lossy: 3-byte frame tag + 3-byte start code (9D 01 2A), then 14-bit w/h
                w = int.from_bytes(data[26:28], "little") & 0x3FFF
                h = int.from_bytes(data[28:30], "little") & 0x3FFF
                return (w, h) if w and h else None
            if chunk == b"VP8L":  # lossless: 0x2f, then 14-bit (w-1)/(h-1)
                b0 = int.from_bytes(data[21:25], "little")
                return ((b0 & 0x3FFF) + 1, ((b0 >> 14) & 0x3FFF) + 1)
            if chunk == b"VP8X":  # extended: 1 flag byte, then 24-bit (w-1) / 24-bit (h-1)
                w = int.from_bytes(data[21:24], "little") + 1
                h = int.from_bytes(data[24:27], "little") + 1
                return (w, h) if w and h else None
            return None
        if data[:2] == b"\xff\xd8":  # jpeg: walk markers to the first SOFn
            i, n = 2, len(data)
            while i + 4 <= n:
                while i < n and data[i] == 0xFF:
                    i += 1
                if i >= n:
                    return None
                marker = data[i]
                i += 1
                if marker == 0xD9 or marker == 0x01 or 0xD0 <= marker <= 0xD8:
                    continue  # EOI / TEM / RSTn / SOI — no length segment
                seglen = int.from_bytes(data[i : i + 2], "big")
                if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC9, 0xCB, 0xCC, 0xCD, 0xCE, 0xCF):
                    if i + 7 > n:
                        return None
                    h = int.from_bytes(data[i + 3 : i + 5], "big")
                    w = int.from_bytes(data[i + 5 : i + 7], "big")
                    return (w, h) if w and h else None
                if seglen < 2:
                    return None
                i += seglen
            return None
    except (IndexError, struct.error, ValueError):
        return None
    return None

# ───────────────────── the row model of one pass (routing) ─────────────────────
# Radiance sizes an image model's context in LATENT ROWS (16x16 px blocks); the
# entry's ``max-model-len`` is the rows ONE pass gets — prompt, every reference
# picture and the drawn picture all pay out of it. A model can be bound
# differently per slot (NoctQ-V4: 8192 on tp1, unbounded on tp2), so a job's
# row cost picks its slot (job_candidates). REF_ROWS is upstream's sizing
# figure (MULTIMODEL §6.3; the KV cost is 4096, GUIDE §5.6 — RUNBOOK → Editing
# pictures); PROMPT_ROWS is an allowance, not a measurement. Radiance stays the
# authority: a pass it cannot hold 400s naming the rows needed.
LATENT_ROW_PX = 16
EDIT_REF_ROWS = 4600
PROMPT_ROWS = 256
# No length caps on the user prompt or the enhancement prompts: the real cap
# is per-model and in TOKENS (a video model's max_text, an image model's
# conditioning budget), and radiance enforces it at SUBMIT with a 400 that
# lands in the job's error. That failure IS the "too big" mark; the panel
# does not second-guess it in chars.
MAX_JOBS_KEPT = 500  # history kept in the state file (newer survive a trim)
GEN_CAPS_PROBE_S = 60.0  # the capability pass's clock (its work list is usually empty)
GEN_CAPS_REFRESH_S = 3600.0  # re-ask an answer this old — the path check only sees a re-pointed entry

GEN_CAP = 2  # gen-phase flights per slot (LOADING + RUNNING; STARTING has no
             # request yet — see the module docstring's admission rules)
GEN_ENHANCE_CAP = 8  # enhancements in flight on the (single) enhance target
                     # (system-wide — the target's max-num-seqs carries it)

# Job states.
QUEUED = "queued"
STARTING = "starting"
ENHANCING = "enhancing"
LOADING = "loading"
RUNNING = "running"
COMPLETED = "completed"
FAILED = "failed"
CANCELED = "canceled"
IN_FLIGHT_STATES = frozenset({STARTING, ENHANCING, LOADING, RUNNING})
GEN_PHASE_STATES = frozenset({LOADING, RUNNING})  # the gen request is on the slot — admission's count
TERMINAL_STATES = frozenset({COMPLETED, FAILED, CANCELED})
# The DELETE endpoint's cancel branch: everything not yet terminal (a queued
# job has no task to interrupt — the mark IS the cancel; terminal records
# get deleted instead).
CANCELLABLE_STATES = IN_FLIGHT_STATES | frozenset({QUEUED})

# Param SHAPE guards only — never "too big": the model's own length/size
# caps are radiance's, and its submit-time 400 is the failure the job records.
SIZE_RE = re.compile(r"^\d{2,4}x\d{2,4}$")


# ──────────────────────────── state files ────────────────────────────


@contextmanager
def gen_lock() -> Iterator[None]:
    """Serialize the settings/jobs writers (endpoints + the engine task).
    Advisory flock — a crashed writer can't wedge it."""
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    fh = LOCK_PATH.open("a+")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fh, fcntl.LOCK_UN)
        fh.close()


def _atomic_write(path: Path, text: str, mode: int) -> None:
    """Atomic 0600 write (tmp + rename). Call inside gen_lock()."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text)
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def _empty_settings() -> dict[str, Any]:
    return {
        "purposes": {},
        # Enhance target: where prompt enhancement runs (defaults to the
        # shipped writer instance; repointable in Gen settings).
        "enhance": {"enabled": True, "prompts": {}, "instance": "gpu-writer", "model": "Writer"},
        "gc_days": 14,
    }


def load_settings() -> dict[str, Any]:
    """Settings, tolerating a missing/corrupt file (→ defaults). Shape is
    coerced, never trusted."""
    try:
        data = json.loads(SETTINGS_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return _empty_settings()
    out = _empty_settings()
    if not isinstance(data, dict):
        return out
    purp = data.get("purposes")
    if isinstance(purp, dict):
        for k, v in purp.items():
            if isinstance(k, str) and isinstance(v, str) and v:
                out["purposes"][k] = v
    enh = data.get("enhance")
    if isinstance(enh, dict):
        if isinstance(enh.get("enabled"), bool):
            out["enhance"]["enabled"] = enh["enabled"]
        prompts = enh.get("prompts")
        if isinstance(prompts, dict):
            for k, v in prompts.items():
                if isinstance(k, str) and isinstance(v, str):
                    out["enhance"]["prompts"][k] = v
        if isinstance(enh.get("instance"), str) and enh["instance"]:
            out["enhance"]["instance"] = enh["instance"]
        if isinstance(enh.get("model"), str) and enh["model"]:
            out["enhance"]["model"] = enh["model"]
    if isinstance(data.get("gc_days"), int) and not isinstance(data.get("gc_days"), bool):
        out["gc_days"] = max(1, min(365, data["gc_days"]))
    return out


def save_settings(s: dict[str, Any]) -> None:
    _atomic_write(SETTINGS_PATH, json.dumps(s, indent=2) + "\n", 0o600)


# ─────────────────── discovered capabilities (radiance's answer) ───────────────────


def _empty_caps() -> dict[str, Any]:
    return {"models": {}}


def load_caps() -> dict[str, Any]:
    """Per-model capabilities {name: {edits, path, at}}; shape-coerced like
    the other state files (a corrupt file is no capabilities, never a
    crash)."""
    try:
        data = json.loads(CAPS_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return _empty_caps()
    out = _empty_caps()
    if not isinstance(data, dict) or not isinstance(data.get("models"), dict):
        return out
    for name, row in data["models"].items():
        if not isinstance(name, str) or not isinstance(row, dict):
            continue
        if not isinstance(row.get("edits"), bool):
            continue
        out["models"][name] = {
            "edits": row["edits"],
            "path": row.get("path") if isinstance(row.get("path"), str) else "",
            "at": row["at"] if isinstance(row.get("at"), (int, float)) else 0.0,
        }
    return out


def caps_of(caps: dict[str, Any], name: str, path: str) -> Optional[bool]:
    """Does ``name`` read pictures (radiance's ``capabilities.image_edit``)?
    None = never probed, or the cached answer belongs to a different container
    path — both read as unknown, never "cannot". A file swap under the same
    path is re-asked by age, not here."""
    row = caps["models"].get(name)
    if not row or (row["path"] and row["path"] != path):
        return None
    return row["edits"]


def caps_refresh(name: str, path: str, edits: bool) -> bool:
    """Record one probed answer. Call OUTSIDE gen_lock(); returns True when it
    WROTE. A fresh confirm writes nothing (no per-pass writes on a steady
    fleet); a stale or changed answer re-stamps the age clock."""
    with gen_lock():
        caps = load_caps()
        row = caps["models"].get(name)
        if (
            row
            and row["edits"] == edits
            and row["path"] == path
            and time.time() - float(row["at"]) <= GEN_CAPS_REFRESH_S
        ):
            return False  # same answer on the same file, still fresh: nothing to record
        caps["models"][name] = {"edits": edits, "path": path, "at": time.time()}
        _atomic_write(CAPS_PATH, json.dumps(caps, indent=2) + "\n", 0o600)
        return True


def _empty_jobs() -> dict[str, Any]:
    return {"seq": 0, "jobs": {}, "order": []}


def load_jobs() -> dict[str, Any]:
    """The job store, tolerating a missing/corrupt file (→ empty)."""
    try:
        data = json.loads(JOBS_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return _empty_jobs()
    out = _empty_jobs()
    if not isinstance(data, dict):
        return out
    if isinstance(data.get("seq"), int) and not isinstance(data.get("seq"), bool):
        out["seq"] = max(0, data["seq"])
    jobs = data.get("jobs")
    if isinstance(jobs, dict):
        for jid, job in jobs.items():
            if isinstance(jid, str) and isinstance(job, dict) and isinstance(job.get("state"), str):
                out["jobs"][jid] = job
    order = data.get("order")
    if isinstance(order, list):
        out["order"] = [j for j in order if isinstance(j, str) and j in out["jobs"]]
        # re-order against the jobs that survived coercion: keep file order,
        # append any job missing from it (corrupt half-write), oldest first
        seen = set(out["order"])
        out["order"] += [j for j in out["jobs"] if j not in seen]
    else:
        out["order"] = list(out["jobs"])
    return out


def save_jobs(state: dict[str, Any]) -> None:
    _atomic_write(JOBS_PATH, json.dumps(state, indent=2) + "\n", 0o600)


def _next_id(state: dict[str, Any]) -> str:
    state["seq"] = int(state.get("seq", 0)) + 1
    return f"job-{state['seq']:06d}"


def create_job(
    model: str,
    kind: str,
    prompt: str,
    enhance: bool,
    params: dict[str, Any],
    user: Optional[str],
) -> dict[str, Any]:
    """Append a queued job; returns it. Call inside gen_lock(). ``kind`` is
    the artifact class ("image" for an edit too); the edit block is set by the
    create path in the same critical section (its files are named after the
    job)."""
    state = load_jobs()
    job: dict[str, Any] = {
        "id": _next_id(state),
        "model": model,
        "kind": kind,
        "prompt": prompt,
        "enhance": enhance,
        "params": params,
        "user": user,
        "state": QUEUED,
        "slot": None,
        "enhanced_prompt": None,
        "progress": None,
        "error": None,
        "artifact": None,
        "edit": None,
        "radiance_video_id": None,
        "created_at": time.time(),
        "started_at": None,
        "finished_at": None,
    }
    state["jobs"][job["id"]] = job
    state["order"].append(job["id"])
    # trim the history (newest MAX_JOBS_KEPT survive; in-flight jobs never are)
    if len(state["order"]) > MAX_JOBS_KEPT:
        drop = [j for j in state["order"][: -MAX_JOBS_KEPT] if state["jobs"][j]["state"] in TERMINAL_STATES]
        for j in drop:
            del state["jobs"][j]
        state["order"] = [j for j in state["order"] if j in state["jobs"]]
    save_jobs(state)
    return job


def update_job(jid: str, mutate: Callable[[dict[str, Any]], None]) -> Optional[dict[str, Any]]:
    """Load, apply ``mutate(job)``, save — one critical section. Returns the
    job (or None). Call OUTSIDE gen_lock() (it takes it)."""
    with gen_lock():
        state = load_jobs()
        job = state["jobs"].get(jid)
        if job is None:
            return None
        mutate(job)
        save_jobs(state)
        return dict(job)


def remove_job(jid: str) -> bool:
    """Drop a job (and its slot claim, if any). Call OUTSIDE gen_lock()."""
    with gen_lock():
        state = load_jobs()
        if not remove_job_locked(state, jid):
            return False
        save_jobs(state)
        return True


def remove_job_locked(state: dict[str, Any], jid: str) -> bool:
    """Drop a job from an already-loaded store (caller holds the lock and
    saves) — the create path rolls a job back when its pictures fail to land,
    in the same critical section (no nested lock)."""
    if jid not in state["jobs"]:
        return False
    del state["jobs"][jid]
    state["order"] = [j for j in state["order"] if j != jid]
    return True


def get_job(jid: str) -> Optional[dict[str, Any]]:
    return load_jobs()["jobs"].get(jid)


def has_work(jobs: dict[str, Any]) -> bool:
    """Any job needing the engine's attention — a QUEUED one (claimable) or an
    in-flight one (its task may need the tick's re-attach pass after a
    restart). The engine loop's idle fast-path: False means a tick would do
    nothing, so the caller skips the fleet world build (two panel-do spawns)
    and pays only the local state-file read."""
    return any(j.get("state") in IN_FLIGHT_STATES or j.get("state") == QUEUED for j in jobs.values())


# ─────────────────────── slot discovery (configs) ───────────────────────


def parse_slot_config(text: str) -> dict[str, dict[str, Any]]:
    """The ``models:`` section of a radiance config → name → {path, kind, tp,
    mlen}.

    Strict line-based (the repo owns this format and doctor checks it — no
    YAML dependency in the host service); unknown shapes are skipped, not
    fatal: a config the engine can't read simply exposes no models.

    ``mlen`` is the entry's ``max-model-len``: the row budget of one pass
    (None = the model's own length).
    """
    out: dict[str, dict[str, Any]] = {}
    in_models = False
    current: Optional[str] = None
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        code = line.split("#", 1)[0].rstrip()  # comments end the code part
        if not code:
            continue
        indent = len(code) - len(code.lstrip())
        if indent == 0:
            in_models = code == "models:"
            current = None
            continue
        if not in_models:
            continue
        if indent == 2 and code.endswith(":"):
            current = code.strip()[:-1]
            out[current] = {"path": None, "kind": None, "tp": None, "mlen": None}
            continue
        if indent >= 4 and current and ":" in code:
            key, _, val = code.strip().partition(":")
            key, val = key.strip(), val.strip()
            if key == "model" and val.startswith("/"):
                out[current]["path"] = val
            elif key == "tp" and val.isdigit():
                out[current]["tp"] = int(val)
            elif key == "max-model-len" and val.isdigit():
                out[current]["mlen"] = int(val)
    # kind from the /models layout (the 2nd path component)
    for m in out.values():
        p = m["path"]
        if p:
            parts = Path(p).parts  # ('/', 'models', 'image', 'x.rad')
            if len(parts) >= 3 and parts[1] == "models" and parts[2] in KIND_DIRS:
                m["kind"] = parts[2]
    return {n: m for n, m in out.items() if m["path"]}


def slot_models(service: str) -> dict[str, dict[str, Any]]:
    """A service's models from its config file ({} when absent/unreadable)."""
    try:
        return parse_slot_config((CONFIG_DIR / f"{service}.yaml").read_text())
    except OSError:
        return {}


def build_instances(
    defs: list[dict[str, Any]],
    live: list[dict[str, Any]],
    ledger: dict[str, Any],
    ips: dict[str, str],
) -> dict[str, dict[str, Any]]:
    """The Gen section's view of the whole fleet: every gpu-* instance with
    its models (name → kind, from the config), its llm entries (the enhance-
    target candidates) and its card state. The gen SLOTS are the subset
    hosting an image/video model (build_slots). Reads the on-disk configs
    (the running truth is the deployed /opt/panel copy — panel reads 0644
    fine)."""
    import gpu_pool  # local: keep the module importable without the fleet helpers

    out: dict[str, dict[str, Any]] = {}
    for d in defs:
        name = d.get("name")
        if not isinstance(name, str) or not name.startswith("gpu-"):
            continue
        models = slot_models(name)
        running = any(
            r.get("service") == name and r.get("state") in gpu_pool.HOLDING_STATES for r in live
        )
        container = next(
            (
                (r.get("container") or "")
                for r in live
                if r.get("service") == name and r.get("state") in gpu_pool.HOLDING_STATES
            ),
            None,
        )
        out[name] = {
            "name": name,
            "display": d.get("display") or name,
            "cards": int(d.get("cards") or 1),
            "assigned": sorted(ledger.get("allocations", {}).get(name) or []) or None,
            "holding": sorted(gpu_pool.holding_of(live, name)) or None,
            "running": running,
            "container": container,
            "ip": (ips.get(container, "") if container else ""),
            "models": {n: m["kind"] for n, m in models.items()},
            "mlens": {n: int(m["mlen"] or 0) for n, m in models.items()},  # 0 = no bound set
            "paths": {n: (m["path"] or "") for n, m in models.items()},  # the capability cache's identity
            "llms": [n for n, m in models.items() if m["kind"] == "llm"],
        }
    return out


def build_slots(instances: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """The gen slots: the instances hosting at least one image or video model
    (that IS the slot — an LLM-only instance, like the writer, is not one)."""
    slots: dict[str, dict[str, Any]] = {}
    for name, row in instances.items():
        if not any(k in ("image", "video") for k in row["models"].values()):
            continue
        slots[name] = {
            "name": name,
            "display": row["display"],
            "cards": row["cards"],
            "assigned": row["assigned"],
            "holding": row["holding"],
            "running": row["running"],
            "container": row["container"],
            "ip": row["ip"],
            "models": dict(row["models"]),
            "mlens": dict(row["mlens"]),
            "paths": dict(row["paths"]),
            "llm": row["llms"][0] if row["llms"] else None,
        }
    return slots


# ───────────────────────── model resolution ─────────────────────────


def _article(word: str) -> str:
    return "an" if word[:1].lower() in "aeiou" else "a"


def known_models(slots: dict[str, dict[str, Any]], caps: Optional[dict[str, Any]] = None) -> dict[str, dict[str, Any]]:
    """model name → {kind, slots, edits, mlen, mlen_min} across the gen slots
    (the UI's model list and the settings validation's address space).

    ``edits`` is radiance's answer from the capability cache (None = never
    probed = unverified, never "cannot"); ``mlen``/``mlen_min`` are the
    longest/shortest BOUNDED pass any slot gives the model (0 = no slot
    bounds it) — the line the tab prices frames against and job_candidates
    routes on.
    """
    caps = caps if caps is not None else load_caps()
    out: dict[str, dict[str, Any]] = {}
    for slot in slots.values():
        for name, kind in slot["models"].items():
            if kind not in ("image", "video"):
                continue
            path = (slot.get("paths") or {}).get(name) or ""
            bound = int((slot.get("mlens") or {}).get(name) or 0)
            e = out.setdefault(
                name,
                {"kind": kind, "slots": [], "edits": None, "mlen": 0, "mlen_min": 0, "_edits": [], "_bounds": []},
            )
            if slot["name"] not in e["slots"]:
                e["slots"].append(slot["name"])
            e["mlen"] = max(e["mlen"], bound)
            e["_bounds"].append(bound)
            e["_edits"].append(caps_of(caps, name, path))
    for e in out.values():
        bounded = [b for b in e.pop("_bounds") if b > 0]
        e["mlen_min"] = min(bounded) if bounded else 0
        seen = [v for v in e.pop("_edits") if v is not None]
        # True on any slot beats False on another
        e["edits"] = True if True in seen else (False if seen else None)
        e["slots"].sort()
    return out


def resolve_model(
    model: str, settings: dict[str, Any], slots: dict[str, dict[str, Any]]
) -> tuple[str, str, Optional[str]]:
    """A job's ``model`` field → (model name, kind, purpose key or None). A
    purpose key (image/video/edit) is translated through the settings; anything
    else must be an exact model name. ``kind`` is the artifact class (an
    edit's is still "image"); the third value says the request was asked for
    the picture-reading path. Raises ValueError (user-ready)."""
    model = (model or "").strip()
    known = known_models(slots)
    if model in RESERVED_MODEL_NAMES:
        target = (settings.get("purposes") or {}).get(model)
        if not target:
            raise ValueError(f"purpose {model!r} is not configured (Gen settings)")
        if target not in known:
            raise ValueError(f"purpose {model!r} points at unknown model {target!r}")
        return target, known[target]["kind"], model
    if not model:
        raise ValueError("model is required (a purpose key like 'image' or an exact model name)")
    if model not in known:
        avail = ", ".join(sorted(known)) or "(none — no gen slot is up in compose)"
        raise ValueError(f"unknown model {model!r} (known: {avail})")
    return model, known[model]["kind"], None


def validate_settings(
    s: dict[str, Any],
    slots: dict[str, dict[str, Any]],
    instances: dict[str, dict[str, Any]],
) -> tuple[dict[str, Any], list[str]]:
    """Full validation of an incoming settings PUT. Returns (clean, errors);
    errors non-empty → nothing is applied (atomic). ``instances`` is the
    whole-fleet view (build_instances) — the enhance target must name a
    deployed instance + one of its llm models."""
    errors: list[str] = []
    known = known_models(slots)
    out = _empty_settings()
    if not isinstance(s, dict):
        return out, ["settings must be a JSON object"]
    # purposes: purpose key → model of the matching kind
    purp_in = s.get("purposes", {})
    if not isinstance(purp_in, dict):
        errors.append("purposes must be an object")
    else:
        for k, v in purp_in.items():
            if k not in PURPOSE_KEYS:
                errors.append(f"unknown purpose {k!r} (want one of: {', '.join(PURPOSE_KEYS)})")
                continue
            if not isinstance(v, str) or not MODEL_NAME_RE.match(v):
                errors.append(f"purposes.{k}: bad model name {v!r}")
                continue
            if v in RESERVED_MODEL_NAMES:
                errors.append(f"purposes.{k}: {v!r} is a reserved purpose key, not a model name")
                continue
            if v not in known:
                errors.append(f"purposes.{k}: unknown model {v!r} (not hosted by any gen slot)")
                continue
            kind = known[v]["kind"]
            if k == "edit":
                # An unprobed model (edits None) is allowed — radiance answers
                # at submit — so a fresh deploy stays configurable.
                if kind != "image":
                    errors.append(f"purposes.edit: {v!r} is {_article(kind)} {kind} model, not an image model")
                    continue
                if known[v]["edits"] is False:
                    errors.append(
                        f"purposes.edit: {v!r} refuses /v1/images/edits (radiance says it reads no "
                        "pictures) — pick an edit-capable model, or start a slot that hosts it so its "
                        "capabilities can be read"
                    )
                    continue
            elif kind != k:
                errors.append(f"purposes.{k}: {v!r} is {_article(kind)} {kind} model, not {_article(k)} {k} model")
                continue
            out["purposes"][k] = v
    # enhance: global toggle + per-gen-model prompts + the target
    # (instance + llm model the engine runs prompts through)
    enh_in = s.get("enhance", {})
    if not isinstance(enh_in, dict):
        errors.append("enhance must be an object")
    else:
        out["enhance"]["enabled"] = bool(enh_in.get("enabled", out["enhance"]["enabled"]))
        prompts_in = enh_in.get("prompts", {})
        if not isinstance(prompts_in, dict):
            errors.append("enhance.prompts must be an object (model name → instruction)")
        else:
            for k, v in prompts_in.items():
                if k not in known:
                    errors.append(f"enhance.prompts: unknown gen model {k!r} (matches no deployed gen model — copy or delete it in the Gen tab's enhancement prompts)")
                    continue
                if not isinstance(v, str):
                    errors.append(f"enhance.prompts.{k}: must be a string")
                    continue
                out["enhance"]["prompts"][k] = v
        inst = enh_in.get("instance", out["enhance"]["instance"])
        mdl = enh_in.get("model", out["enhance"]["model"])
        if not isinstance(inst, str) or inst not in instances:
            errors.append(f"enhance.instance: {inst!r} is not a deployed gpu-* instance")
        elif not isinstance(mdl, str) or mdl not in instances[inst]["llms"]:
            hosted = ", ".join(instances[inst]["llms"]) or "none"
            errors.append(
                f"enhance.model: {inst!r} hosts no llm model {mdl!r} (its llm entries: {hosted})"
            )
        else:
            out["enhance"]["instance"] = inst
            out["enhance"]["model"] = mdl
    # gc_days
    gc = s.get("gc_days", out["gc_days"])
    if not isinstance(gc, int) or isinstance(gc, bool) or not 1 <= gc <= 365:
        errors.append("gc_days must be an integer 1-365")
    else:
        out["gc_days"] = gc
    return out, errors


# ─────────────────────── admission (the lane) ───────────────────────


def flights_on_slot(jobs: dict[str, Any], slot: str) -> list[dict[str, Any]]:
    return [j for j in jobs.values() if j.get("slot") == slot and j.get("state") in IN_FLIGHT_STATES]


def admission(
    jobs: dict[str, Any], slot: str, self_jid: Optional[str] = None
) -> tuple[bool, str]:
    """May a flight on ``slot`` take its GEN PHASE? Two rules.

    THE CAP counts GEN_PHASE_STATES only: a STARTING flight has no gen request
    yet, and enhancement runs off-slot, so neither contends for the slot's
    VRAM.

    ONE MODEL AT A TIME: a flight on a different model blocks this job, in any
    in-flight state (a STARTING one is about to ask for its model) — the
    slot's entries park for each other, so two models in flight would hand
    the card back and forth every step. Behind it is bounded work: the
    flights drain, the model switches once, the job runs.

    ``self_jid`` excludes a re-checking flight from its own count (and names
    the model to compare against).
    """
    mine = (jobs.get(self_jid) or {}).get("model") if self_jid else None
    same = 0
    for j in jobs.values():
        if j.get("slot") != slot or j.get("id") == self_jid:
            continue
        st = j.get("state")
        if st not in IN_FLIGHT_STATES:
            continue
        if mine and j.get("model") != mine:
            return False, f"slot {slot} is on {j.get('model')} (one model per slot at a time)"
        if st in GEN_PHASE_STATES:
            same += 1
    if same >= GEN_CAP:
        return False, f"slot {slot} is at its gen-phase cap ({GEN_CAP})"
    return True, ""


def enhance_admission(jobs: dict[str, Any]) -> tuple[bool, str]:
    """May another job start its ENHANCEMENT? The system-wide cap on the
    ENHANCE TARGET's load: at most GEN_ENHANCE_CAP jobs in ENHANCING (their
    rewrites all land on one entry, so the cap is global, not per slot). The
    target is a different card from every slot — enhancement neither waits
    for a lane nor holds one."""
    n = sum(1 for j in jobs.values() if j.get("state") == ENHANCING)
    if n >= GEN_ENHANCE_CAP:
        return False, f"the enhance target is at its cap ({GEN_ENHANCE_CAP})"
    return True, ""


def claim_job(
    jid: str,
    slot: str,
    state: str,
    started: bool = False,
) -> Optional[dict[str, Any]]:
    """queued → ``state`` on ``slot`` — the claim (one critical section).
    A QUEUED job's error is a wait reason by construction (the tick's mark,
    a re-queue, the startup reap) — the claim clears it, so the lane wait
    shows until the job is admitted, never into the start. Call OUTSIDE
    gen_lock()."""
    def _m(job: dict[str, Any]) -> None:
        if job.get("state") != QUEUED:
            raise RuntimeError(f"{jid} is {job.get('state')}, not queued")
        job["slot"] = slot
        job["state"] = state
        job["error"] = None  # stale the moment the job is admitted
        if started:
            job["started_at"] = job.get("started_at") or time.time()

    try:
        return update_job(jid, _m)
    except RuntimeError:
        return None


def claim_enhance(jid: str) -> Optional[dict[str, Any]]:
    """queued → ENHANCING with NO slot claim — the enhance phase is off-slot
    (gated by enhance_admission's global cap, not a lane). The job's FIFO
    position is untouched (it rides the order list): on success it goes back
    to QUEUED with enhanced_prompt set, and the gen phase claims it in its
    own position. Like claim_job, the claim clears the error (a QUEUED job's
    is a wait reason by construction). One critical section, like claim_job.
    Call OUTSIDE gen_lock()."""

    def _m(job: dict[str, Any]) -> None:
        if job.get("state") != QUEUED:
            raise RuntimeError(f"{jid} is {job.get('state')}, not queued")
        job["state"] = ENHANCING
        job["slot"] = None  # off-slot — the gen phase re-picks its slot later
        job["error"] = None  # the wait reason is stale the moment the phase is admitted

    try:
        return update_job(jid, _m)
    except RuntimeError:
        return None


def slot_pass(slot: dict[str, Any], model: str) -> int:
    """The rows this slot's config gives one pass of ``model`` (0 = no bound)."""
    return int((slot.get("mlens") or {}).get(model) or 0)


def slot_covers(slot: dict[str, Any], model: str, rows: Optional[int]) -> bool:
    """Can one pass of ``model`` on ``slot`` hold ``rows``? Unknown rows route
    as an unknown cost — radiance answers for them."""
    bound = slot_pass(slot, model)
    return bound <= 0 or rows is None or rows <= bound


def size_rows(size: Any) -> Optional[int]:
    """WxH → the latent rows a frame that size costs. None when it is not two
    numbers: the model then draws its own default, and the panel keeps no
    model table — an unknown size is an unknown cost, and radiance says no."""
    if not isinstance(size, str):
        return None
    m = re.match(r"^\s*(\d+)\s*[x*]\s*(\d+)\s*$", size, re.I)
    if not m:
        return None
    w, h = int(m.group(1)), int(m.group(2))
    if w <= 0 or h <= 0:
        return None
    return (w // LATENT_ROW_PX) * (h // LATENT_ROW_PX)


def pass_rows(kind: str, params: dict[str, Any], refs: int = 0) -> Optional[int]:
    """The rows one gen pass needs: the frame + its reference pictures + the
    prompt allowance. None for a video job (its bound is its own conditioning)
    or an image job whose size cannot be costed."""
    if kind != "image":
        return None
    rows = size_rows((params or {}).get("size"))
    if rows is None:
        return None
    return rows + max(0, refs) * EDIT_REF_ROWS + PROMPT_ROWS


def job_rows(job: dict[str, Any]) -> Optional[int]:
    """``pass_rows`` for a stored job record (its edit block says how many
    reference pictures it carries)."""
    refs = len(((job.get("edit") or {}).get("images")) or [])
    return pass_rows(job.get("kind") or "", job.get("params") or {}, refs)


def longest_pass(slots: dict[str, dict[str, Any]], model: str) -> tuple[int, list[str]]:
    """(the longest pass any slot gives ``model``, the slots that host it) —
    0 means some slot runs it unbounded."""
    host = [s for s in slots.values() if model in s["models"]]
    return max((slot_pass(s, model) for s in host), default=0), [s["name"] for s in host]


def job_candidates(
    jobs: dict[str, Any],
    slots: dict[str, dict[str, Any]],
    job: dict[str, Any],
) -> list[tuple[str, str]]:
    """Slots that can serve ``job``, most-attractive first: running before
    down, then the CHEAPEST PASS THAT FITS (a model bound at 8192 on tp1 and
    unbounded on tp2 is one model; a 2048x2048 request only fits the second),
    then LEAST-LOADED (spreads a multi-slot model across its slots), then
    name. Cost comes after "running" because a cold slot costs a start + a
    load, dearer than the card-minutes it saves. The caller reloads ``jobs``
    per claim, so a claim earlier in the same tick already counts. Returns
    [(slot, reason-if-not-running)]."""
    model = job.get("model") or ""
    rows = job_rows(job)
    out = []
    UNBOUNDED = 1 << 40  # an unbounded entry is the most expensive seat, not the free one
    for name in sorted(slots):
        slot = slots[name]
        if model not in slot["models"] or not slot_covers(slot, model, rows):
            continue
        up = bool(slot["running"] and slot["ip"])
        bound = slot_pass(slot, model)
        out.append(
            (name, "" if up else "down", 0 if up else 1, bound if bound > 0 else UNBOUNDED,
             len(flights_on_slot(jobs, name)), name)
        )
    out.sort(key=lambda x: (x[2], x[3], x[4], x[5]))
    return [(name, why) for name, why, *_rest in out]


def queue_wait_reason(
    job: dict[str, Any],
    slots: dict[str, dict[str, Any]],
    status: Optional[dict[str, dict[str, Any]]] = None,
) -> Optional[str]:
    """Why a QUEUED, gen-phase-bound job can never be claimed under the
    CURRENT configs: a config drift since the job was queued (its model is no
    longer hosted, or no slot's pass covers it), or a lane EVERY hosting slot
    is blocked on. The normal queue (caps, free/yieldable lanes, a stop that
    didn't land, a slot parked on another model) gets None. The result is
    marked on the record's error column (the enhance target's structural
    reasons up front in the tick's enhance branch); a later claim clears it.
    None otherwise (a re-queue's reason may be riding it)."""
    model = job.get("model") or ""
    hosting = [s for s in slots.values() if model in s["models"]]
    if not hosting:
        return f"no gen slot hosts {model!r} (a config changed since the job was queued)"
    # Only the slots whose pass covers this job count: a 2048x2048 edit of a
    # model bound at 8192 on tp1 is not "waiting for tp1 to drain", it is a
    # pass tp1 can never take. Create refuses that shape, so reaching here means
    # the configs shrank (or the store was edited) since the job was queued.
    rows = job_rows(job)
    covers = [s for s in hosting if slot_covers(s, model, rows)]
    if not covers:
        longest, _names = longest_pass(slots, model)
        return (
            f"no slot hosting {model!r} takes a {rows}-row pass (the longest bound is {longest}) "
            f"— a smaller frame or one fewer reference picture fits (the configs changed since this was queued)"
        )
    hosting = covers
    # Every hosting slot blocked (a lane the engine can't free: held by an
    # LLM instance, the pool can't cover it even after the idle yield —
    # structural, only an admin clears it — or a busy gen holder — transient,
    # it drains on its own), so the job row says why instead of waiting in
    # silence. The mark rides until the claim, which clears it (claim_job).
    if status is not None:
        sts = [status.get(s["name"]) for s in sorted(hosting, key=lambda s: s["name"])]
        if sts and all(st is not None and st.get("state") == "blocked" for st in sts):
            for st in sts:
                for b in st.get("blockers") or []:
                    return f"waiting for the lane: {b}"
    return None


def slot_status(
    slots: dict[str, dict[str, Any]],
    jobs: dict[str, Any],
    holds: dict[int, str],
    cards_total: int = 0,
    reserved: Optional[dict[int, dict[str, Any]]] = None,
) -> dict[str, dict[str, Any]]:
    """Per slot: the UI state + the IDLE gen slots that would have to stop
    to free its lane + the blockers otherwise. Pure — ``holds`` is
    gpu_pool.live_holds(live) (card index → holding service), ``reserved``
    the ledger's fence (card → {for, note, bdf}), ``cards_total`` the pool
    size (0 in dev, where the engine is off).

    The LANE is the card set a start would actually take — gpu_pool.plan_start's
    two branches: the SAVED set when that branch would take it (shape-valid
    for the slot's card need and not fenced by a reservation for another
    service) — the yield reasons over ITS cards — else the FREE-GRAB: the
    slot's OWN reserved cards first, then the lowest unheld, then (only if
    still short) the LOWEST cards the IDLE gen slots hold, until the need is
    met (a card reserved for ANOTHER service is fenced — the grab skips it).
    free_cards' own-first ordering is mirrored EXACTLY, not approximated: an
    idle neighbour on a card the grab would skip must not be stopped for
    nothing. A slot with NO usable saved set (a fresh deploy never assigned
    it, a reservation fence, a shape drift) rides this branch — the idle
    neighbours holding the cards the grab would take are the yield set, and
    a lane the pool can't cover even after the yield is BLOCKED — the
    blocker names what holds the gap (a busy gen slot that drains on its
    own, an LLM occupant to stop, a fenced card to unreserve) — instead of
    the engine claiming the job and flapping a start it can't win.

    states: running | down | down-idle-stops | blocked. ``stops`` is
    non-empty ONLY in down-idle-stops — a blocked lane is not stopped into
    by any consumer (the tick, the engine's _gen_free_slot, the admin-start
    path and the UI all gate on that state). Only a gen slot is ever
    stoppable by the engine; an LLM instance or a busy gen slot is a
    blocker, full stop.
    """
    reserved = reserved or {}
    out: dict[str, dict[str, Any]] = {}
    for name, slot in slots.items():
        busy = bool(flights_on_slot(jobs, name))
        if slot["running"]:
            out[name] = {"state": "running", "stops": [], "blockers": [], "busy": busy}
            continue
        n = int(slot.get("cards") or 1)
        saved = slot["assigned"]
        # The plan's saved-set branch would take it (gpu_pool._saved_set_usable
        # + in-range): the lane is the saved cards. Anything else (no saved
        # set, a shape drift, a reservation fence) lands in the free-grab lane.
        saved_lanes = (
            isinstance(saved, list)
            and len(saved) == n
            and len(set(saved)) == n
            and all(isinstance(c, int) and not isinstance(c, bool) and 0 <= c < cards_total for c in saved)
            and all(reserved.get(c, {}).get("for") in (None, name) for c in saved)
        )
        stops: list[str] = []
        blockers: list[str] = []
        if saved_lanes:
            for c in sorted(set(saved)):
                holder = holds.get(c)
                if holder is None:
                    continue
                if holder in slots:
                    if flights_on_slot(jobs, holder):
                        blockers.append(f"card {c} held by {holder} (busy — its jobs finish first)")
                    elif holder not in stops:
                        stops.append(holder)
                else:
                    blockers.append(f"card {c} held by {holder} (not a gen slot — stop it in the GPU view)")
            st = "down-idle-stops" if stops else "down"
        else:
            # Free-grab lane: free_cards' own-first ordering (the slot's
            # reserved cards first, then the lowest unheld), topped up with
            # the LOWEST idle-gen-held cards until the need is met. A card
            # reserved for ANOTHER service is fenced (the grab skips it), and
            # the top-up skips it for the same reason: a card an idle slot
            # holds that is reserved for that slot ITSELF frees nothing the
            # grab can take. The slot's OWN reserved cards sort first, exactly
            # like the grab.
            own = [c for c in range(cards_total) if c not in holds and reserved.get(c, {}).get("for") == name]
            rest = [c for c in range(cards_total) if c not in holds and reserved.get(c, {}).get("for") is None]
            idle = {
                c: svc
                for c, svc in holds.items()
                if svc in slots and reserved.get(c, {}).get("for") in (None, name) and not flights_on_slot(jobs, svc)
            }
            lane = (sorted(own) + sorted(rest))[:n]
            for c in sorted(idle):  # lowest idle first: the grab takes the lowest it needs
                if len(lane) >= n:
                    break
                lane.append(c)
            stops = sorted({idle[c] for c in lane if c in idle})
            if len(lane) < n:
                st = "blocked"
                who = ", ".join(stops) or "none"
                # Name what holds the gap — the advice differs by kind: a
                # busy gen slot drains on its own (no admin needed); an LLM
                # occupant needs one stopped in the GPU view; a card
                # reserved for ANOTHER service is fenced (unreserve it).
                lane_set = set(lane)
                gap_busy: dict[str, list[int]] = {}
                gap_llm: dict[str, list[int]] = {}
                gap_fenced: list[tuple[int, str]] = []
                for c in range(cards_total):
                    if c in lane_set:
                        continue
                    owner = reserved.get(c, {}).get("for")
                    holder = holds.get(c)
                    if owner is not None and owner != name:
                        gap_fenced.append((c, owner))
                    elif holder is not None:
                        (gap_busy if holder in slots else gap_llm).setdefault(holder, []).append(c)
                detail: list[str] = []
                for svc in sorted(gap_busy):
                    detail.append(
                        f"card(s) {', '.join(map(str, sorted(gap_busy[svc])))} held by {svc} "
                        "(busy — it drains on its own)"
                    )
                for svc in sorted(gap_llm):
                    detail.append(
                        f"card(s) {', '.join(map(str, sorted(gap_llm[svc])))} held by {svc} "
                        "(stop it in the GPU view)"
                    )
                for c, owner in sorted(gap_fenced):
                    detail.append(f"card {c} reserved for {owner} (fenced — unreserve it in the GPU view)")
                if not detail:
                    detail.append("the pool has no more cards")
                blockers.append(
                    f"lane short: {name} needs {n} card(s), only {len(lane)} free even after "
                    f"stopping the idle gen slots ({who}) — {'; '.join(detail)} — "
                    + (
                        f"its saved set {sorted(saved)} is unusable (fenced or drifted): reassign it (auto)"
                        if saved
                        else "unassigned: assign its lane cards in the GPU view"
                    )
                )
            else:
                st = "down-idle-stops" if stops else "down"
        if blockers:
            st = "blocked"
            # A blocked lane is never stopped into (see the states contract
            # above) — the reported field stays strict (the would-be stops
            # already ride in the lane-short blocker's "who" text).
            stops = []
        out[name] = {"state": st, "stops": sorted(stops), "blockers": blockers, "busy": busy}
    return out


# ─────────────────────── artifacts + GC ───────────────────────


def sniff(data: bytes) -> tuple[str, str]:
    """(ext, mime) from magic bytes; falls back to PNG (krea-2's output)."""
    if data[:4] == b"\x89PNG":
        return ".png", "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return ".jpg", "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp", "image/webp"
    if data[:4] == b"\x1a\x45\xdf\xa3":
        return ".webm", "video/webm"
    if len(data) >= 12 and data[4:8] == b"ftyp":
        return ".mp4", "video/mp4"
    return ".png", "image/png"


def _artifact_job_id(name: str) -> str:
    """The job id an artifact FILENAME belongs to: the name minus its extension
    and a trailing ``-thumb`` / ``-mask`` / ``-in<N>`` — all job-named files age
    GC with their record."""
    stem = name.rsplit(".", 1)[0]
    for suffix in ("-thumb", "-mask"):
        if stem.endswith(suffix):
            stem = stem[: -len(suffix)]
    if "-in" in stem:
        head, _, tail = stem.rpartition("-in")
        if head.startswith("job-") and tail.isdigit():
            stem = head
    return stem


def edit_input_names(jid: str, images: int, mask: bool) -> tuple[list[str], Optional[str]]:
    """An edit's input file names: ``job-<n>-in<i>.<ext>`` +
    ``job-<n>-mask.<ext>``. Named like the artifact so the nginx allowlist,
    the age GC and the record's delete all reach them by the same rules."""
    return (
        [f"{jid}-in{i}" for i in range(images)],
        f"{jid}-mask" if mask else None,
    )


def gc_old_artifacts(
    directory: Path,
    days: int,
    keep_ids: Optional[frozenset[str]] = None,
    now: Optional[float] = None,
) -> list[str]:
    """Age-based GC: delete artifact files older than ``days`` (mtime),
    EXCEPT the files of jobs still in the store (``keep_ids``) — a record the
    UI still shows must not point at a deleted file, so a stored record's
    artifact outlives ``days``. The record's other two exits: the DELETE
    endpoint removes its files with it (immediate), and a record trimmed by
    the count cap orphans its file — which this pass takes on the first sweep
    after the file ages past ``days``. No size caps by design. Returns the
    removed names (the caller audits)."""
    now = now if now is not None else time.time()
    cutoff = now - days * 86400.0
    keep = keep_ids or frozenset()
    removed: list[str] = []
    try:
        entries = list(directory.iterdir())
    except OSError:
        return removed
    for p in entries:
        if not p.is_file() or not p.name.startswith("job-"):
            continue  # never touch anything we didn't name
        if _artifact_job_id(p.name) in keep:
            continue  # the record is still in the store — its artifact stands
        try:
            if p.stat().st_mtime < cutoff:
                p.unlink()
                removed.append(p.name)
        except OSError:
            continue
    return removed
