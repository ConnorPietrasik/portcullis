"""GPU pool — the ledger, the compose override, and the allocation math.

State split:
- LEDGER (gpu-pool.json): intent — each instance's card set (allocations),
  the boot-autostart list, and card RESERVATIONS (card index → {for, note,
  bdf}). Writers: panel-api + the boot oneshot, serialized on gpu_lock().
- OVERRIDE (gpu-pool-override.yml): the ledger's compose projection
  (ROCR_VISIBLE_DEVICES per instance), rendered ONLY from the ledger. Every
  compose render that can create/recreate a fleet container includes it
  (invariant 7); the base file's card set stays the fallback for a fully
  hand-run compose. Allocations for services no longer in the fleet are
  pruned from the override on every render but kept in the ledger: inert
  there, and re-adding a block under the same name re-uses the old set
  instead of silently grabbing new cards.
- LIVE (panel-do gpu-live): which cards are held right now, read from the
  running containers' env — never from the ledger. A stopped instance holds
  nothing; the API view shows both (assigned vs holding) so drift is visible.

Pool indices are ROCR indices — what ROCR_VISIBLE_DEVICES counts in — NOT
sysfs drm card numbers (a non-GPU drm device can take a card slot; see
amd_gpus() in main.py for the join).

A RESERVATION dedicates a card to one instance (e.g. the physically-odd x8
card): every auto-grab path (allocate "auto", plan_start, plan_boot,
start_preference) treats a card reserved for ANOTHER service as NOT free —
plan_start/plan_boot's saved-set branch is fenced the same way
(_saved_set_usable): a saved set containing such a card is stale w.r.t. the
reservation (it predates the dedication) and the start/boot falls back to
the reservation-aware grab, so a card add that renumbers the tiles under a
saved set can't silently re-point an instance at the dedicated card. An
explicit assign of the card to another service is refused; the owner's own
grab lands on its reserved cards FIRST (a dedication, not just a fence). A
reservation holds no cards itself — a CONSTRAINT, never written to the
override — and a stale one keeps fencing the card (safer than opening it)
until an admin unreserves. Each reservation records the card's BDF at
reserve time; the view warns when the tile's BDF later moves (a card
add/remove renumbers the tiles, so an un-warned reservation could point at
the wrong physical card).

Pure logic + the two state files: no FastAPI, no /sys reads, no panel-do
calls — callers pass defs (gpu-fleet), live (gpu-live), cards_total.
Operator view: docs/RUNBOOK.md → "GPU fleet" → "GPU pool".
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
# files in the checkout, like the LLM settings (main.py).
LEDGER_PATH = Path(
    os.environ.get("GPU_POOL_LEDGER_PATH")
    or ("gpu-pool.json" if DEV_MODE else "/opt/panel-api/gpu-pool.json")
)
OVERRIDE_PATH = Path(
    os.environ.get("GPU_POOL_OVERRIDE_PATH")
    or ("gpu-pool-override.yml" if DEV_MODE else "/opt/panel-api/gpu-pool-override.yml")
)
LOCK_PATH = Path(
    os.environ.get("GPU_POOL_LOCK_PATH")
    or (".gpu-pool.lock" if DEV_MODE else "/opt/panel-api/.gpu-pool.lock")
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
    return {"allocations": {}, "autostart": [], "reserved": {}}


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
    res = data.get("reserved")
    if isinstance(res, dict):
        for key, val in res.items():
            try:
                idx = int(key)
            except (TypeError, ValueError):
                continue
            if not isinstance(val, dict):
                continue
            owner = val.get("for")
            if not isinstance(owner, str) or not owner:
                continue
            note = val.get("note")
            bdf = val.get("bdf")
            out["reserved"][idx] = {
                "for": owner,
                "note": note if isinstance(note, str) else "",
                "bdf": bdf if isinstance(bdf, str) and bdf else None,
            }
    return out


def save_ledger(ledger: dict[str, Any]) -> None:
    """Atomic 0600 write (tmp + rename). Call inside gpu_lock()."""
    LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = LEDGER_PATH.with_name(LEDGER_PATH.name + ".tmp")
    tmp.write_text(json.dumps(ledger, indent=2) + "\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, LEDGER_PATH)


def render_override(allocations: dict[str, list[int]], fleet: set[str]) -> str:
    """ROCR_VISIBLE_DEVICES per instance, for services in the CURRENT fleet
    only — an entry for a removed service would make compose merge a phantom
    service (no image) into every render. Empty allocation → a no-op
    `services: {}`."""
    lines = [
        "# Generated by panel-api (GPU pool) — do not edit by hand.",
        "# Card sets live in the ledger (gpu-pool.json); this file is its",
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
    gpu_lock()."""
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
    states count; the first holding row wins a card."""
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
    """The instance's card count (its gpu.cards label); None when the
    service isn't a fleet instance in the deployed compose."""
    for d in defs:
        if d.get("name") == service:
            return int(d.get("cards") or 1)
    return None


def free_cards(
    cards_total: int,
    holds: dict[int, str],
    reserved: Optional[dict[int, dict[str, Any]]] = None,
    for_service: Optional[str] = None,
) -> list[int]:
    """Cards no one is holding — the for_service's OWN reserved cards first,
    then the rest. A card reserved for ANOTHER service is not free (see the
    module docstring for the full semantics)."""
    reserved = reserved or {}
    own: list[int] = []
    rest: list[int] = []
    for c in range(cards_total):
        if c in holds:
            continue
        owner = reserved.get(c, {}).get("for")
        if owner == for_service:
            own.append(c)
        elif owner is None:
            rest.append(c)
    return sorted(own) + sorted(rest)


def _new_ledger(ledger: dict[str, Any], allocations: dict[str, list[int]]) -> dict[str, Any]:
    """A FULL copy of the ledger for a plan's return: the caller save_ledger()
    s the return verbatim, so EVERY key must ride along. Dropping one
    silently rewrites the ledger without it (the next load coerces the gap to
    {}), and a 'reserved' fence lost this way reopens its card to every
    auto-grab."""
    return {
        "allocations": allocations,
        "autostart": list(ledger["autostart"]),
        "reserved": dict(ledger["reserved"]),
    }


def _cards_str(xs: list[int]) -> str:
    return ",".join(str(c) for c in xs)


def _valid_set(cards: Any, n: int, cards_total: int) -> bool:
    return (
        isinstance(cards, list)
        and len(cards) == n
        and len(set(cards)) == n
        and all(isinstance(c, int) and not isinstance(c, bool) and 0 <= c < cards_total for c in cards)
    )


def _saved_set_usable(
    ledger: dict[str, Any],
    saved: Any,
    n: int,
    cards_total: int,
    service: str,
) -> bool:
    """``saved`` is a set plan_start/plan_boot's saved-set branch may take:
    shape-valid AND free of cards reserved for ANOTHER service. A
    reservation made AFTER the set was saved fences it — taking it would put
    the instance on a card the fence exists to keep off it; the start/boot
    falls back to the reservation-aware grab instead. The owner's OWN
    reserved cards are fine (that is the dedication)."""
    if not _valid_set(saved, n, cards_total):
        return False
    reserved = ledger["reserved"]
    return all(reserved.get(c, {}).get("for") in (None, service) for c in saved)


def _free_or_raise(
    cards_total: int,
    holds: dict[int, str],
    reserved: dict[int, dict[str, Any]],
    service: str,
    n: int,
) -> list[int]:
    free = free_cards(cards_total, holds, reserved, service)
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
    Raises ValueError (user-ready). Returns (new_ledger, gpus); the caller
    owns the lock, save, override, and the apply."""
    n = cards_needed(defs, service)
    if n is None:
        raise ValueError(f"{service} is not a gpu-* instance in the deployed compose")
    holds = live_holds(live)
    if spec == "auto":
        gpus = _free_or_raise(cards_total, holds, ledger["reserved"], service, n)
    else:
        gpus = parse_cards(spec)
        if len(gpus) != n:
            raise ValueError(
                f"{service} needs exactly {n} card(s) (its gpu.cards), got {len(gpus)}: {_cards_str(gpus)}"
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
        # a card reserved for ANOTHER service is fenced even while idle —
        # that is the whole point of the reservation.
        fenced = sorted(
            c for c in gpus if (r := ledger["reserved"].get(c)) and r["for"] != service
        )
        if fenced:
            who = ", ".join(
                f"{c} (reserved for {ledger['reserved'][c]['for']}"
                + (f" — {ledger['reserved'][c]['note']}" if ledger["reserved"][c]["note"] else "")
                + ")"
                for c in fenced
            )
            raise ValueError(f"card(s) reserved: {who}")
    return _new_ledger(ledger, {**ledger["allocations"], service: gpus}), gpus


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
        raise ValueError(f"{service} is not a gpu-* instance in the deployed compose")
    holds = live_holds(live)
    saved = ledger["allocations"].get(service)
    if _saved_set_usable(ledger, saved, n, cards_total, service) and all(
        holds.get(c) in (None, service) for c in saved
    ):
        gpus = sorted(saved)
    else:
        gpus = _free_or_raise(cards_total, holds, ledger["reserved"], service, n)
    return _new_ledger(ledger, {**ledger["allocations"], service: gpus}), gpus


def start_preference(
    ledger: dict[str, Any],
    defs: list[dict[str, Any]],
    live: list[dict[str, Any]],
    cards_total: int,
    service: str,
) -> list[int]:
    """The lane set a start of ``service`` PREFERS — the set a re-plan picks
    once its stoppable blockers drain: the saved set when it is usable
    (shape-valid AND free of other services' reservations — see
    _saved_set_usable), EVEN while other instances still hold some of it
    (stopping those holders is exactly what makes plan_start take the saved
    set back); else plan_start's fallback — the lowest free set, possibly
    short when the pool can't cover the need (a short target has no holders
    to stop).

    plan_start itself can't use this: it must not WAIT on a held saved set
    (it takes the fallback now or raises). The Gen section's idle-stop
    (main.py _gen_idle_conflict_stops) targets THIS set's holders, because
    the fallback set's cards are free by construction and hold nothing
    stoppable — only the saved lane's holders, drained, can turn a
    conflicting start into a successful one. If plan_start's saved-set rule
    changes, this must change with it: a preference no re-plan would pick
    stops warm slots for nothing. The fallback is reservation-aware like
    plan_start's."""
    n = cards_needed(defs, service)
    if n is None:
        raise ValueError(f"{service} is not a gpu-* instance in the deployed compose")
    saved = ledger["allocations"].get(service)
    if _saved_set_usable(ledger, saved, n, cards_total, service):
        return sorted(saved)
    return free_cards(cards_total, live_holds(live), ledger["reserved"], service)[:n]


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
        if _saved_set_usable(ledger, saved, n, cards_total, svc) and all(
            holds.get(c) in (None, svc) for c in saved
        ):
            gpus = sorted(saved)
        else:
            free = free_cards(cards_total, holds, ledger["reserved"], svc)
            if len(free) < n:
                skipped.append(f"{svc}: needs {n} card(s), only {len(free)} free")
                continue
            gpus = free[:n]
            new_alloc[svc] = gpus
        plan.append((svc, gpus))
        for c in gpus:
            holds[c] = svc
    return _new_ledger(ledger, new_alloc), plan, skipped


def autostart_warnings(ledger: dict[str, Any]) -> list[str]:
    """Warnings about the boot-autostart set (never fatal — the boot planner
    resolves overlaps by re-grabbing; this is the visibility)."""
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
    """Warnings for SAVED sets whose count no longer matches the instance's
    card need (its gpu.cards) — e.g. after a config edit without a
    re-assign. A GPU-aware start self-heals (plan_start re-grabs the right
    count and records it), but a hand-run compose would pin the old count
    and the engine would fail to load. Never fatal — reassign (auto) or fix
    the label."""
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
                f"but the instance needs {need} (its gpu.cards) — reassign (auto) or fix the label/config"
            )
    return out


def reservation_warnings(
    defs: list[dict[str, Any]],
    ledger: dict[str, Any],
    cards_total: int,
    holds: dict[int, str],
    bdf_by_index: dict[int, str],
) -> list[str]:
    """Warnings for RESERVATIONS that have gone stale — the card left the
    pool, the tile's BDF moved (a card add/remove renumbered the tiles — the
    dedication may point at the wrong physical card), the owner left the
    fleet, a hand-run compose pinned a foreign instance on the card, or
    another instance's saved set still names it (its next start falls back
    to the reservation-aware grab — the fence wins). Never fatal: the
    reservation KEEPS fencing the card (safer than opening it to the
    auto-grab) until an admin unreserves. Called on every GPU-view poll, so
    a hardware change can't silently re-point a dedication."""
    out: list[str] = []
    names = {d.get("name") for d in defs}
    for card in sorted(ledger["reserved"]):
        r = ledger["reserved"][card]
        if card >= cards_total:
            out.append(
                f"card {card} is reserved for {r['for']} but the pool is 0-{cards_total - 1} — "
                f"a card was removed and the tiles renumbered: check the reservation still points "
                f"at the right physical card (recorded BDF {r['bdf'] or '?'}), then re-reserve or unreserve"
            )
            continue
        cur = bdf_by_index.get(card)
        if r["bdf"] and cur and cur != r["bdf"]:
            out.append(
                f"card {card} was reserved for {r['for']} at BDF {r['bdf']} but tile {card} is now "
                f"{cur} — the tile→BDF map moved (a card was added/removed): check the reservation "
                f"points at the right physical card, then re-reserve to re-record the BDF"
            )
        if r["for"] not in names:
            out.append(
                f"card {card} is reserved for {r['for']}, which is not in the deployed compose — "
                f"the card stays fenced from every auto-grab until you unreserve it (or re-add the instance)"
            )
        holder = holds.get(card)
        if holder and holder != r["for"]:
            out.append(
                f"card {card} is reserved for {r['for']} but held by {holder} — a hand-run compose "
                f"pinned it there: stop the holder (or unreserve) so the reservation is not fiction"
            )
        # A SAVED set of another instance that still names the card:
        # _saved_set_usable makes its next start/boot fall back to a free
        # set — this names the drift until it is re-assigned (auto).
        for name, saved in ledger.get("allocations", {}).items():
            if name != r["for"] and isinstance(saved, list) and card in saved:
                out.append(
                    f"card {card} is reserved for {r['for']} but {name}'s saved set "
                    f"{sorted(saved)} still contains it — {name}'s next start falls back to a free "
                    f"set (the fence wins): reassign {name} (auto) to clean it up"
                )
    return out
