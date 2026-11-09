"""LLM GPU pool — the ledger, the compose override, and the allocation math.

Operator view: docs/RUNBOOK.md → "LLM fleet" → "GPU pool".

- Each fleet instance (compose service ``llm-*``) needs a fixed number of
  cards (its ``llm.cards`` label, else ``--tp`` in its command). The pool is
  the host's whole card set (panel-api counts it from /sys; ROCR index i ==
  drm card i on this host).
- The LEDGER (``llm-gpu.json`` next to this file in production:
  /opt/panel-api/) is the durable state: each instance's current card set
  (``allocations``) + the boot-autostart list (``autostart``). panel-api and
  the boot oneshot (llm_autostart.py) are the only writers; they serialize
  on ``gpu_lock()``.
- The OVERRIDE (``llm-gpu-override.yml``, same directory) is a compose
  ``-f`` file pinning ROCR_VISIBLE_DEVICES per instance, rendered ONLY from
  the ledger (never hand-edited). Every compose invocation that can create
  or recreate a fleet container includes it (panel-do llm-apply/llm-stop/
  start-service, COMPOSE_STACK in setup/common.sh), so a ``make update``
  recreate never silently reverts an instance to the card set committed in the
  base file — which stays as the fallback default for a fully hand-run
  compose (no override -f). Allocations for services no longer in the fleet
  are pruned from the OVERRIDE on every render but kept in the LEDGER:
  inert there, and re-adding a block under the same name re-uses the old
  set instead of silently grabbing new cards.
- LIVE state (which cards are actually held right now) is read from the
  running containers' env via panel-do ``llm-live`` — never from the
  ledger. A stopped instance holds nothing. The API view shows both
  (assigned vs holding) so drift is visible, not silent.

This module is pure logic + these two files: no FastAPI, no /sys reads, no
panel-do calls. Callers pass ``defs`` (panel-do llm-fleet), ``live``
(panel-do llm-live) and ``cards_total`` in.
"""

from __future__ import annotations

import fcntl
import json
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional

DEV_MODE = os.environ.get("PANEL_DEV") == "1"

# Production lives under /opt/panel-api (panel-owned); dev mode keeps the
# files in the checkout, like the LLM order settings (main.py).
LEDGER_PATH = Path(
    os.environ.get("LLM_GPU_LEDGER_PATH")
    or ("llm-gpu.json" if DEV_MODE else "/opt/panel-api/llm-gpu.json")
)
OVERRIDE_PATH = Path(
    os.environ.get("LLM_GPU_OVERRIDE_PATH")
    or ("llm-gpu-override.yml" if DEV_MODE else "/opt/panel-api/llm-gpu-override.yml")
)
LOCK_PATH = Path(
    os.environ.get("LLM_GPU_LOCK_PATH")
    or (".llm-gpu.lock" if DEV_MODE else "/opt/panel-api/.llm-gpu.lock")
)

# Container states that HOLD their pinned cards: a "created" container has
# not started its engine, exited/dead hold nothing.
HOLDING_STATES = frozenset({"running", "restarting", "paused"})


# ──────────────────────────── state files ────────────────────────────


@contextmanager
def gpu_lock() -> Iterator[None]:
    """Serialize the ledger/override writers (panel-api endpoints + the boot
    oneshot). Advisory flock — a crashed writer can't wedge it."""
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    fh = LOCK_PATH.open("a+")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fh, fcntl.LOCK_UN)
        fh.close()


def _empty() -> dict[str, Any]:
    return {"allocations": {}, "autostart": []}


def load_ledger() -> dict[str, Any]:
    """The ledger, tolerating a missing/corrupt file (→ empty, re-seeded by
    the next save). Shape is coerced, never trusted."""
    try:
        data = json.loads(LEDGER_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return _empty()
    if not isinstance(data, dict):
        return _empty()
    alloc = data.get("allocations")
    auto = data.get("autostart")
    out = _empty()
    if isinstance(alloc, dict):
        for name, cards in alloc.items():
            if not isinstance(name, str) or not isinstance(cards, list):
                continue
            clean = [c for c in cards if isinstance(c, int) and not isinstance(c, bool)]
            if clean:
                out["allocations"][name] = clean
    if isinstance(auto, list):
        out["autostart"] = [s for s in auto if isinstance(s, str)]
    return out


def save_ledger(ledger: dict[str, Any]) -> None:
    """Atomic 0600 write (tmp + rename). Call inside gpu_lock()."""
    LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = LEDGER_PATH.with_name(LEDGER_PATH.name + ".tmp")
    tmp.write_text(json.dumps(ledger, indent=2) + "\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, LEDGER_PATH)


def render_override(allocations: dict[str, list[int]], fleet: set[str]) -> str:
    """The override as YAML: ROCR_VISIBLE_DEVICES per instance.

    Only services that exist in the CURRENT fleet are written — an entry for
    a removed service would make compose merge a phantom service (no image)
    into every render. Empty allocation → an empty services map, which is a
    valid no-op override.
    """
    lines = [
        "# Generated by panel-api (LLM GPU pool) — do not edit by hand.",
        "# Card sets live in the ledger (llm-gpu.json); this file is its",
        "# compose projection. ROCR_VISIBLE_DEVICES merges key-by-key over",
        "# the base block, so the key/model flags in the base are kept.",
        "services:",
    ]
    written = 0
    for name in sorted(allocations):
        if name not in fleet:
            continue
        cards = ",".join(str(c) for c in sorted(allocations[name]))
        lines.append(f"  {name}:")
        lines.append("    environment:")
        lines.append(f'      ROCR_VISIBLE_DEVICES: "{cards}"')
        written += 1
    if written == 0:
        lines.append("  {}")
    return "\n".join(lines) + "\n"


def sync_override(ledger: dict[str, Any], fleet: set[str]) -> bool:
    """Re-render the override from the ledger (pruned to the fleet); writes
    only when the content changed. Returns True when it wrote. Call inside
    gpu_lock(). The GET endpoint calls this too — self-healing prune."""
    content = render_override(ledger["allocations"], fleet)
    try:
        if OVERRIDE_PATH.read_text() == content:
            return False
    except OSError:
        pass
    OVERRIDE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = OVERRIDE_PATH.with_name(OVERRIDE_PATH.name + ".tmp")
    tmp.write_text(content)
    os.chmod(tmp, 0o644)
    os.replace(tmp, OVERRIDE_PATH)
    return True


def ensure_state_files() -> None:
    """Seed missing state files (panel-api lifespan + setup scripts). The
    ledger is created once; the override is (re)rendered from the ledger."""
    with gpu_lock():
        if not LEDGER_PATH.exists():
            save_ledger(load_ledger())
        if not OVERRIDE_PATH.exists():
            sync_override(load_ledger(), set())


# ─────────────────────────── live state ──────────────────────────────


def parse_cards(spec: str) -> list[int]:
    """'0,1' → [0,1]; ValueError (user-ready) on anything else."""
    out: list[int] = []
    for part in spec.split(","):
        part = part.strip()
        if not part or not part.isdigit():
            raise ValueError(f"bad card {part!r} (want digits like 0,1,2)")
        n = int(part)
        if n in out:
            raise ValueError(f"card {n} is listed twice")
        out.append(n)
    return sorted(out)


def live_holds(live: list[dict[str, Any]]) -> dict[int, str]:
    """card index → holding service, from the containers that are actually
    up. A recreate leaves the old container in `ps -a`, so only holding
    states count; the first holding row wins a card (in practice there is
    one holding container per service)."""
    holds: dict[int, str] = {}
    for row in live:
        if row.get("state") not in HOLDING_STATES:
            continue
        svc = row.get("service")
        if not isinstance(svc, str):
            continue
        for card in row.get("gpus") or []:
            if isinstance(card, int) and not isinstance(card, bool):
                holds.setdefault(card, svc)
    return holds


def is_running(live: list[dict[str, Any]], service: str) -> bool:
    return any(
        r.get("service") == service and r.get("state") in HOLDING_STATES for r in live
    )


def holding_of(live: list[dict[str, Any]], service: str) -> list[int]:
    """The cards this service's up-container(s) actually pin right now."""
    out: set[int] = set()
    for row in live:
        if row.get("service") != service or row.get("state") not in HOLDING_STATES:
            continue
        for card in row.get("gpus") or []:
            if isinstance(card, int) and not isinstance(card, bool):
                out.add(card)
    return sorted(out)


def cards_needed(defs: list[dict[str, Any]], service: str) -> Optional[int]:
    """The instance's card count (llm.cards label, else --tp, else 1); None
    when the service isn't a fleet instance in the deployed compose."""
    for d in defs:
        if d.get("name") == service:
            return int(d.get("cards") or 1)
    return None


def free_cards(cards_total: int, holds: dict[int, str]) -> list[int]:
    return [c for c in range(cards_total) if c not in holds]


def _cards_str(xs: list[int]) -> str:
    return ",".join(str(c) for c in xs)


def _valid_set(cards: Any, n: int, cards_total: int) -> bool:
    return (
        isinstance(cards, list)
        and len(cards) == n
        and len(set(cards)) == n
        and all(isinstance(c, int) and not isinstance(c, bool) and 0 <= c < cards_total for c in cards)
    )


def _free_or_raise(cards_total: int, holds: dict[int, str], service: str, n: int) -> list[int]:
    free = free_cards(cards_total, holds)
    if len(free) < n:
        raise ValueError(
            f"not enough free cards: {service} needs {n}, only {len(free)} free"
            + (f" ({_cards_str(free)})" if free else " (none)")
        )
    return free[:n]


# ───────────────────────── allocation math ───────────────────────────


def allocate(
    ledger: dict[str, Any],
    defs: list[dict[str, Any]],
    live: list[dict[str, Any]],
    cards_total: int,
    service: str,
    spec: str,
) -> tuple[dict[str, Any], list[int]]:
    """Plan one explicit/auto assignment. ``spec`` is "auto" or "0,1,2".

    Raises ValueError with a user-ready message. Returns (new_ledger,
    gpus); the caller owns the lock, save, override, and the apply.
    """
    n = cards_needed(defs, service)
    if n is None:
        raise ValueError(f"{service} is not an llm-* instance in the deployed compose")
    holds = live_holds(live)
    if spec == "auto":
        gpus = _free_or_raise(cards_total, holds, service, n)
    else:
        gpus = parse_cards(spec)
        if len(gpus) != n:
            raise ValueError(
                f"{service} needs exactly {n} card(s) (its --tp), got {len(gpus)}: {_cards_str(gpus)}"
            )
        if any(c >= cards_total for c in gpus):
            bad = max(c for c in gpus if c >= cards_total)
            raise ValueError(f"card {bad} is out of range (the pool is 0-{cards_total - 1})")
        # the service's OWN current cards are allowed — the (re)create frees
        # and re-takes them; a card held by ANYONE ELSE is a conflict.
        conflicts = sorted(c for c in gpus if holds.get(c) not in (None, service))
        if conflicts:
            who = ", ".join(f"{c} (held by {holds[c]})" for c in conflicts)
            raise ValueError(f"card(s) in use: {who}")
    return {"allocations": {**ledger["allocations"], service: gpus}, "autostart": list(ledger["autostart"])}, gpus


def plan_start(
    ledger: dict[str, Any],
    defs: list[dict[str, Any]],
    live: list[dict[str, Any]],
    cards_total: int,
    service: str,
) -> tuple[dict[str, Any], list[int]]:
    """Plan a start (the GPU-aware start / "create + start"): honor the saved
    set when it is free, else grab the lowest N free cards and record them.
    """
    if is_running(live, service):
        raise ValueError(f"{service} is already running")
    n = cards_needed(defs, service)
    if n is None:
        raise ValueError(f"{service} is not an llm-* instance in the deployed compose")
    holds = live_holds(live)
    saved = ledger["allocations"].get(service)
    if _valid_set(saved, n, cards_total) and all(holds.get(c) in (None, service) for c in saved):
        gpus = sorted(saved)
    else:
        gpus = _free_or_raise(cards_total, holds, service, n)
    return {"allocations": {**ledger["allocations"], service: gpus}, "autostart": list(ledger["autostart"])}, gpus


def plan_boot(
    ledger: dict[str, Any],
    defs: list[dict[str, Any]],
    live: list[dict[str, Any]],
    cards_total: int,
) -> tuple[dict[str, Any], list[tuple[str, list[int]]], list[str]]:
    """The boot sequence for the autostart set, in list order.

    Returns (new_ledger, plan [(service, gpus)], skipped ["service: reason"]).
    Honors a saved set when it is free, otherwise grabs the lowest N free
    cards and records them (the ledger converges on what actually starts) —
    so the boot set is conflict-free by construction: whatever FITS comes
    up. A container already holding cards is left alone.
    """
    n_by_service = {d["name"]: (int(d.get("cards") or 1)) for d in defs}
    holds = live_holds(live)
    plan: list[tuple[str, list[int]]] = []
    skipped: list[str] = []
    new_alloc = {**ledger["allocations"]}
    for svc in ledger["autostart"]:
        n = n_by_service.get(svc)
        if n is None:
            skipped.append(f"{svc}: no longer in the deployed compose")
            continue
        if is_running(live, svc):
            continue  # already up — keep whatever it holds
        saved = new_alloc.get(svc)
        if _valid_set(saved, n, cards_total) and all(holds.get(c) in (None, svc) for c in saved):
            gpus = sorted(saved)
        else:
            free = free_cards(cards_total, holds)
            if len(free) < n:
                skipped.append(f"{svc}: needs {n} card(s), only {len(free)} free")
                continue
            gpus = free[:n]
            new_alloc[svc] = gpus
        plan.append((svc, gpus))
        for c in gpus:
            holds[c] = svc
    return {"allocations": new_alloc, "autostart": list(ledger["autostart"])}, plan, skipped


def autostart_warnings(ledger: dict[str, Any]) -> list[str]:
    """Human-readable warnings about the boot-autostart set (never fatal —
    the boot planner resolves overlaps by re-grabbing; this is the
    visibility)."""
    out: list[str] = []
    alloc = ledger["allocations"]
    auto = ledger["autostart"]
    for i, a in enumerate(auto):
        sa = alloc.get(a)
        if not isinstance(sa, list) or not sa:
            continue  # no saved set: boot auto-grabs — nothing to overlap
        sa = [c for c in sa if isinstance(c, int) and not isinstance(c, bool)]
        for b in auto[i + 1 :]:
            sb = alloc.get(b)
            if not isinstance(sb, list):
                continue
            overlap = set(sa) & {c for c in sb if isinstance(c, int) and not isinstance(c, bool)}
            if overlap:
                out.append(
                    f"autostart overlap: {a} ({_cards_str(sorted(sa))}) and {b} "
                    f"({_cards_str(sorted(set(sb)))}) claim card(s) {_cards_str(sorted(overlap))} — at boot "
                    f"the later one is moved to other free cards"
                )
    return out


def assignment_warnings(defs: list[dict[str, Any]], ledger: dict[str, Any]) -> list[str]:
    """Human-readable warnings for SAVED sets whose count no longer matches
    the instance's card need (its llm.cards / --tp) — e.g. after a flag edit
    without a re-assign. A GPU-aware start self-heals (plan_start re-grabs
    the right count and records it), but a hand-run compose would pin the
    old count and the engine would fail to load. Never fatal — reassign
    (auto) or fix the flag."""
    out: list[str] = []
    alloc = ledger["allocations"]
    for d in defs:
        name = d.get("name")
        need = d.get("cards")
        saved = alloc.get(name)
        if (
            not isinstance(name, str)
            or not isinstance(need, int)
            or isinstance(need, bool)
            or not isinstance(saved, list)
            or not saved
        ):
            continue
        if len(saved) != need:
            out.append(
                f"{name}: saved set {_cards_str(sorted(saved))} has {len(saved)} card(s) "
                f"but the instance needs {need} (llm.cards / --tp) — reassign (auto) or fix the flag"
            )
    return out
