"""Gen job queue — the panel's image/video generation section (panel tab "Gen").

Operator view: docs/RUNBOOK.md → "Gen".

A GEN SLOT is a ``gpu-*`` fleet instance whose radiance config lists at least
one model under ``/models/image/`` or ``/models/video/`` — the model's path
gives its kind (the /models/{llm,image,video} layout is the contract). Slots
are discovered from the deployed configs (``/opt/panel/gpu-configs/``, the
repo copy in dev) — no second model registry to drift. A slot's config is
GEN-ONLY: its gen model is the sole entry and stays resident — prompt
enhancement runs OFF-SLOT, on the settings' ENHANCE TARGET (by default the
gpu-writer instance's "writer" model — a public chat instance the engine
reaches by container IP over the edge network, like the slots), so a slot's
VRAM never serves the enhancer.

Two state files (next to this file; /opt/panel-api/ in production), the
gpu_pool.py pattern:

- ``gen-settings.json`` (0600) — admin-editable from the Gen tab:
  ``purposes`` (purpose key → model name), ``enhance`` (global toggle +
  per-gen-model prompt — a model with no prompt skips enhancement — plus the
  enhance target: ``instance`` + ``model``), ``gc_days``.
- ``gen-jobs.json`` (0600) — the FIFO: ``seq``, ``jobs`` (id → record),
  ``order`` (oldest first).

Job lifecycle (states are the API's vocabulary):
    queued → enhancing → queued → starting → loading → running → completed
                        (no enhance, or an already-enhanced job, skips
                        straight from the first queued to starting)
  ``queued``      — awaiting a phase; ``enhanced_prompt`` set ⇒ the
                    enhancement is done and only the gen phase remains.
  ``enhancing``   — the enhance target is rewriting the prompt (one call;
                    bringing the target up is part of this phase). OFF-SLOT:
                    no slot is claimed or started — the gen lane is not
                    involved, and the global enhance cap (not a slot's)
                    gates it (see the admission rules below).
  ``starting``    — claimed on a slot; the slot is being (re)started on its
                    lane cards.
  ``loading``     — slot up, waiting for the gen phase to be admitted (the
                    slot is at its gen-phase cap — see below).
  ``running``     — the gen request is in flight (image POST / video render;
                    ``progress`` 0-100 for videos).

Admission — two independent caps, each checked against a fresh state read
before every claim (the gen-phase one is re-checked before the gen request
itself):

- GEN PHASE (per slot, GEN_CAP): at most GEN_CAP jobs whose gen request is
  on the slot (LOADING: admitted, about to be sent; RUNNING: in radiance) —
  one in progress + one queued in radiance, which sequences the model's
  work itself (the video model runs one denoise + one conditioning at a
time). A STARTING flight (the slot booting) has no gen request yet and
  consumes no gen-phase headroom. Enhancement runs off-slot (the enhance
  target), so it never contends for the slot's VRAM either — nothing on the
  slot blocks anything else (the pre-writer "no enhance while a gen request
  is in flight" rule existed only because the in-slot LLM and the gen model
  couldn't co-reside; with the in-slot LLM entry gone, it is moot).
- ENHANCEMENT (system-wide, GEN_ENHANCE_CAP): at most GEN_ENHANCE_CAP jobs
  in ENHANCING — their rewrites all land on ONE entry (the settings'
  enhance target), so the cap is global, not per slot. Enhancement runs
  BEFORE the slot phase and off-slot (a different card): a queued job
  enhances as soon as there is enhance headroom, without a slot being
  claimed or started — the gen phase never waits for the enhancement, and
  the enhancement never waits for the lane. The target's max-num-seqs
  carries the cap (radiance queues excess sequences before 429ing).

This module is pure logic + these files: no FastAPI, no panel-do, no HTTP —
callers pass ``defs`` (panel-do gpu-fleet), ``live`` (panel-do gpu-live),
``ledger`` (gpu_pool) and ``ips`` (edge) in, and run the privileged parts
themselves.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
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
# The radiance configs (one per gpu-* service, named after it) — the deployed
# copy is the running truth; dev reads the repo's.
CONFIG_DIR = Path(
    os.environ.get("GEN_CONFIG_DIR")
    or ("/opt/panel/gpu-configs" if not DEV_MODE else "compose/gpu-configs")
)

# ── the contracts ──────────────────────────────────────────────────────────
# Purpose keys: what the UI/servitor say; the admin maps them to models in
# the settings. RESERVED — a model literally named one of these could never
# be addressed by name, so the key set is closed over them.
PURPOSE_KEYS = ("image", "video")  # + "edit" when an edit model lands
RESERVED_MODEL_NAMES = frozenset(PURPOSE_KEYS) | {"edit"}

# /models layout: the 2nd path component of a model path is its class.
KIND_DIRS = frozenset({"llm", "image", "video"})

MODEL_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,127}$")
# No length caps on the user prompt or the enhancement prompts: the real cap
# is per-model and in TOKENS (a video model's max_text — MiniMax-H3's is
# 8192, its kMaxText — an image model's conditioning budget), and radiance
# enforces it at SUBMIT with a 400 that lands in the job's error. That
# failure IS the "too big" mark; the panel does not second-guess it in chars
# (a video model exposes its cap as video_max_text on /v1/models).
MAX_JOBS_KEPT = 500  # history kept in the state file (newer survive a trim)

# Concurrency caps (see the module docstring's admission rules).
GEN_CAP = 2  # gen-phase flights per slot: one in progress + one queued in
             # radiance (the model's own sequencing). Counts LOADING +
             # RUNNING only — a STARTING flight has no gen request yet.
GEN_ENHANCE_CAP = 8  # enhancement calls in flight on the enhance target
                     # (system-wide — the target is a single entry, and its
                     # max-num-seqs carries this).

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
# The DELETE endpoint's cancel branch: everything not yet terminal. A queued
# job has no task to interrupt — the mark IS the cancel; its record stands
# (canceled) like an in-flight one's. Terminal records get deleted instead.
CANCELLABLE_STATES = IN_FLIGHT_STATES | frozenset({QUEUED})

# Param SHAPE guards only — never "too big": the model's own length/size
# caps are radiance's to enforce, and its submit-time 400 (e.g. "seconds: ...
# between X and Y") is the failure the job records.
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
        # Enhance target: where prompt enhancement runs. Defaults match the
        # shipped writer instance (the 7th card) — a deployment without it
        # shows the misconfig in the UI (target not ok) and can repoint the
        # target at any chat-capable instance in the Gen settings.
        "enhance": {"enabled": True, "prompts": {}, "instance": "gpu-writer", "model": "writer"},
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
    """Append a queued job; returns it. Call inside gen_lock()."""
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
        if jid not in state["jobs"]:
            return False
        del state["jobs"][jid]
        state["order"] = [j for j in state["order"] if j != jid]
        save_jobs(state)
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
    """The ``models:`` section of a radiance config → name → {path, kind, tp}.

    Strict line-based (the repo owns this format and doctor checks it — no
    YAML dependency in the host service). Unknown shapes are skipped, not
    fatal: a config the engine can't read simply exposes no models.
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
            out[current] = {"path": None, "kind": None, "tp": None}
            continue
        if indent >= 4 and current and ":" in code:
            key, _, val = code.strip().partition(":")
            key, val = key.strip(), val.strip()
            if key == "model" and val.startswith("/"):
                out[current]["path"] = val
            elif key == "tp" and val.isdigit():
                out[current]["tp"] = int(val)
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
    """The Gen section's view of the WHOLE fleet: every gpu-* instance with
    its models (name → kind, from the config), its llm entries (the enhance-
    target candidates) and its card state. ``live``/``ips`` use the
    gpu_pool/main.py row shapes. The gen SLOTS are the subset hosting an
    image/video model (build_slots).

    NOTE: this reads the on-disk configs (the running truth is the deployed
    /opt/panel copy — panel-api runs as `panel`, which reads 0644 fine).
    """
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
            "llm": row["llms"][0] if row["llms"] else None,
        }
    return slots


# ───────────────────────── model resolution ─────────────────────────


def _article(word: str) -> str:
    return "an" if word[:1].lower() in "aeiou" else "a"


def known_models(slots: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """model name → {kind, slots} across the gen slots (the UI's model list
    and the settings validation's address space)."""
    out: dict[str, dict[str, Any]] = {}
    for slot in slots.values():
        for name, kind in slot["models"].items():
            if kind not in ("image", "video"):
                continue
            e = out.setdefault(name, {"kind": kind, "slots": []})
            if slot["name"] not in e["slots"]:
                e["slots"].append(slot["name"])
    for e in out.values():
        e["slots"].sort()
    return out


def resolve_model(model: str, settings: dict[str, Any], slots: dict[str, dict[str, Any]]) -> tuple[str, str]:
    """A job's ``model`` field → (model name, kind).

    A purpose key (image/video) is translated through the admin's settings;
    anything else must be an exact model name. Raises ValueError (user-ready).
    """
    model = (model or "").strip()
    known = known_models(slots)
    if model in RESERVED_MODEL_NAMES:
        target = (settings.get("purposes") or {}).get(model)
        if not target:
            raise ValueError(f"purpose {model!r} is not configured (Gen settings)")
        if target not in known:
            raise ValueError(f"purpose {model!r} points at unknown model {target!r}")
        return target, known[target]["kind"]
    if not model:
        raise ValueError("model is required (a purpose key like 'image' or an exact model name)")
    if model not in known:
        avail = ", ".join(sorted(known)) or "(none — no gen slot is up in compose)"
        raise ValueError(f"unknown model {model!r} (known: {avail})")
    return model, known[model]["kind"]


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
            if known[v]["kind"] != k:
                kind = known[v]["kind"]
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
                    errors.append(f"enhance.prompts: unknown gen model {k!r}")
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
    """May a flight on ``slot`` take its GEN PHASE? The per-slot cap counts
    GEN_PHASE_STATES only (GEN_CAP of them — one in progress + one queued in
    radiance, the model's own sequencing): a STARTING flight (the slot
    booting) has no gen request yet and consumes no headroom, and
    enhancement runs off-slot (the enhance target, its own global cap), so
    it never contends for the slot's VRAM either. ``self_jid`` excludes a
    re-checking flight from its own count."""
    fl = [
        j
        for j in jobs.values()
        if j.get("slot") == slot and j.get("state") in GEN_PHASE_STATES and j.get("id") != self_jid
    ]
    if len(fl) >= GEN_CAP:
        return False, f"slot {slot} is at its gen-phase cap ({GEN_CAP})"
    return True, ""


def enhance_admission(jobs: dict[str, Any]) -> tuple[bool, str]:
    """May another job start its ENHANCEMENT? The system-wide cap on the
    ENHANCE TARGET's load: at most GEN_ENHANCE_CAP jobs in ENHANCING (their
    rewrites all land on one entry, so the cap is global, not per slot).
    The target is a different card from every slot — enhancement neither
    waits for a lane nor holds one."""
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
    Call OUTSIDE gen_lock()."""
    def _m(job: dict[str, Any]) -> None:
        if job.get("state") != QUEUED:
            raise RuntimeError(f"{jid} is {job.get('state')}, not queued")
        job["slot"] = slot
        job["state"] = state
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
    own position. One critical section, like claim_job. Call OUTSIDE
    gen_lock()."""

    def _m(job: dict[str, Any]) -> None:
        if job.get("state") != QUEUED:
            raise RuntimeError(f"{jid} is {job.get('state')}, not queued")
        job["state"] = ENHANCING
        job["slot"] = None  # off-slot — the gen phase re-picks its slot later

    try:
        return update_job(jid, _m)
    except RuntimeError:
        return None


def job_candidates(
    jobs: dict[str, Any],
    slots: dict[str, dict[str, Any]],
    model: str,
) -> list[tuple[str, str]]:
    """Slots that host ``model``, most-attractive first: running before down,
    then LEAST-LOADED (in-flight flights on the slot) — a model hosted by
    several slots SPREADS across them (two identical image slots take one
    job each, not two on the first up to the per-slot cap). The caller
    reloads ``jobs`` per claim, so a claim made earlier in the same tick
    already counts toward its slot's load. Name is the final tiebreak
    (deterministic). Returns [(slot, reason-if-not-running)]."""
    out = []
    for name in sorted(slots):
        slot = slots[name]
        if model not in slot["models"]:
            continue
        load = len(flights_on_slot(jobs, name))
        out.append((name, "" if slot["running"] and slot["ip"] else "down", load))
    out.sort(key=lambda x: (x[1] != "", x[2], x[0]))
    return [(name, why) for name, why, _load in out]


def queue_wait_reason(job: dict[str, Any], slots: dict[str, dict[str, Any]]) -> Optional[str]:
    """Why a QUEUED, gen-phase-bound job can never be claimed under the
    CURRENT configs — the structural reasons only (the transient ones —
    gen-phase cap, a blocked lane, a stop that didn't land — are the normal
    queue and need no say-so). The engine marks the result on the record's
    error column, so a config drift since the job was queued (its model is
    no longer hosted) shows up in the UI instead of a job that quietly waits
    forever. The enhance target's structural reasons are marked UP FRONT by
    the tick's enhance branch (a pending-enhance job never reaches the slot
    loop). None when the wait is a usual lane condition (the caller then
    leaves the record's error alone — a re-queue's reason may be riding
    it)."""
    model = job.get("model") or ""
    hosting = [s for s in slots.values() if model in s["models"]]
    if not hosting:
        return f"no gen slot hosts {model!r} (a config changed since the job was queued)"
    return None


def slot_status(
    slots: dict[str, dict[str, Any]],
    jobs: dict[str, Any],
    holds: dict[int, str],
) -> dict[str, dict[str, Any]]:
    """Per slot: the UI state + the IDLE gen slots that would have to stop
    to free its lane + the blockers otherwise. Pure — ``holds`` is
    gpu_pool.live_holds(live) (card index → holding service).

    states: running | down | down-idle-stops | blocked.
    Only a gen slot (a fleet instance with gen models) is ever stoppable by
    the engine; an LLM instance or a busy gen slot is a blocker, full stop.
    """
    out: dict[str, dict[str, Any]] = {}
    for name, slot in slots.items():
        busy = bool(flights_on_slot(jobs, name))
        if slot["running"]:
            out[name] = {"state": "running", "stops": [], "blockers": [], "busy": busy}
            continue
        cards = set(slot["assigned"] or [])
        if not cards:
            out[name] = {
                "state": "down",
                "stops": [],
                "blockers": ["unassigned — assign its lane cards in the GPU view (a start auto-grabs the lowest free)"],
                "busy": False,
            }
            continue
        stops: list[str] = []
        blockers: list[str] = []
        for c in sorted(cards):
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
        if blockers:
            st = "blocked"
        elif stops:
            st = "down-idle-stops"
        else:
            st = "down"
        out[name] = {"state": st, "stops": sorted(stops), "blockers": blockers, "busy": False}
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
    """The job id an artifact FILENAME belongs to: the name minus its
    extension and a trailing ``-thumb`` (video thumbs are named after their
    MP4)."""
    stem = name.rsplit(".", 1)[0]
    if stem.endswith("-thumb"):
        stem = stem[: -len("-thumb")]
    return stem


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
    the count cap orphans its file — which THIS pass then takes on the first
    sweep after the file ages past ``days``. No size caps by design. Returns
    the removed names (the caller audits)."""
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
