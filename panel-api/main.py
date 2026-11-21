"""Panel API — host-side FastAPI service behind panel.pietrasik.top/api.

Defense in depth: Traefik runs Authelia forward-auth (admins only) and injects
X-Panel-Token; this service re-verifies both independently. Everything
privileged — container control included — goes through `sudo -n panel-do`
(one code path, one journald audit trail); only sysfs//proc metric reads
bypass it.

Runs as user `panel`, binds 172.17.0.1:8010 (docker0 gateway — reachable from
the edge network, not the internet). PANEL_DEV=1 disables the PANEL auth
checks for local development ONLY (loopback) — not the /v1/* LLM key gate
(see the LLM section); the systemd unit never sets it.
"""

from __future__ import annotations

import asyncio
import base64
import glob
import json
import os
import re
import secrets
import subprocess
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Callable, Optional

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

import gen_jobs
import gpu_pool

PANEL_TOKEN = os.environ.get("PANEL_TOKEN", "")
PANEL_DO = "/usr/local/libexec/panel-do"
DEV_MODE = os.environ.get("PANEL_DEV") == "1"
# Filesystems the Overview shows a storage row for (comma-separated; .env: PANEL_DISK_MOUNTS).
DISK_MOUNTS = [m.strip() for m in os.environ.get("PANEL_DISK_MOUNTS", "/").split(",") if m.strip()]

# LLM fallback router (llm.pietrasik.top → this service): the fleet's shared
# API key is the gate, and the priority order lives in a small JSON file.
# See the LLM section at the bottom of this file.
LLM_API_KEY = os.environ.get("LLM_API_KEY", "")
LLM_SETTINGS_PATH = Path(os.environ.get("LLM_SETTINGS_PATH") or "/opt/panel-api/settings.json")
if DEV_MODE:  # local development: keep the file in the checkout, not /opt
    LLM_SETTINGS_PATH = Path("llm-settings.json")

MANAGE_LABEL = "panel.manage"
CATEGORY_LABEL = "panel.category"  # Services view group (collapsible); absent → "Other"
PRIVATE_LABEL = "gpu.private"  # fleet instance with NO public host (invariant 6; value "true")
CONTAINER_VERBS = frozenset({"start", "stop", "restart"})
CID_RE = re.compile(r"^[0-9a-f]{12}$")  # mirrors panel-do's containerIDRe
ARG_RE = re.compile(r"^[ -~]{1,128}$")  # mirrors panel-do's base arg validation
ACTION_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")
GROUP_SPLIT_RE = re.compile(r"[,\s]+")
LIST_CACHE_TTL = 60.0
CONTAINERS_CACHE_TTL = 3.0  # UI polls ~2s; sudo+docker spawn per miss is fine at this rate

if DEV_MODE:
    print("!!! PANEL_DEV=1 — AUTH CHECKS DISABLED (local development only) !!!", flush=True)

@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Owns the LLM router's shared httpx client + background health probe."""
    global _gen_http, _llm_http
    # GPU pool: seed the state files (ledger + override) before the boot
    # oneshot or a compose render can want them. 02/03 already create them
    # at install time; this is the belt-and-braces for a hand-removed file.
    gpu_pool.ensure_state_files()
    # read=300 s: normal streams emit bytes far more often than every 5 min,
    # but a wedged instance that accepts TCP and then goes silent (ROCm init
    # hang) must not park the client's request forever — the deadline fails
    # it, and the passive cooldown + /health probe steer the next request
    # down the list. connect capped at 5 s.
    _llm_http = httpx.AsyncClient(timeout=httpx.Timeout(300.0, connect=5.0, read=300.0, pool=5.0))
    # Gen section: its own client (a render can sit 30+ min on the read — the
    # LLM client's 300 s ceiling would cut it off), stale-flight reap after a
    # restart, and the engine task (see the Gen section).
    _gen_http = httpx.AsyncClient(timeout=httpx.Timeout(1800.0, connect=5.0, read=1800.0, pool=5.0))
    _gen_reap_stale()
    probe = asyncio.create_task(_llm_probe_loop())
    gen_engine = asyncio.create_task(_gen_engine_loop())
    try:
        yield
    finally:
        probe.cancel()
        gen_engine.cancel()
        for t in (probe, gen_engine):
            try:
                await t  # let the in-flight cycle finish cancelling before closing its client
            except asyncio.CancelledError:
                pass
        await _llm_http.aclose()
        await _gen_http.aclose()


app = FastAPI(title="panel-api", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)


# ───────────────────────────── auth ─────────────────────────────

def require_admin(
    x_panel_token: Optional[str] = Header(default=None),
    remote_user: Optional[str] = Header(default=None),
    remote_groups: Optional[str] = Header(default=None),
) -> Optional[str]:
    """Re-check the Traefik token + Authelia identity; return the username
    (None in dev mode) for the audit line."""
    if DEV_MODE:
        return None
    if not PANEL_TOKEN or not x_panel_token or not secrets.compare_digest(
        x_panel_token.encode(), PANEL_TOKEN.encode()
    ):
        raise HTTPException(401, "bad or missing X-Panel-Token")
    if not remote_user:
        raise HTTPException(401, "missing Remote-User (forward-auth bypass attempt?)")
    if "admins" not in set(GROUP_SPLIT_RE.split(remote_groups or "")):
        raise HTTPException(403, "admins only")
    return remote_user


# ──────────────────────────── metrics ────────────────────────────
#
# /sys + /proc reads only — no privileges, no subprocess. Everything the panel
# cannot know from one sample (CPU %, net and disk throughput) is a difference
# between two polls: the FIRST sample of a counter is null and the UI's 2 s
# poll fills it a tick later. Nothing here raises — a metric that can't be read
# is null (or absent), which the Overview renders as "—", never a 500.

def _rd(path: str | Path, cast: Callable[[str], Any] = str, default: Any = None) -> Any:
    try:
        return cast(Path(path).read_text().strip())
    except (OSError, ValueError, IndexError):
        return default


def _hwrd(hw: Optional[str], name: str, cast: Callable[[str], Any] = str, default: Any = None) -> Any:
    if hw is None:
        return default
    return _rd(f"{hw}/{name}", cast, default)


def _natural_key(s: str) -> list[Any]:
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]


def _temp(path: Optional[str]) -> Optional[float]:
    """Millidegree-Celsius sysfs value → degrees Celsius."""
    if path is None:
        return None
    v = _rd(path, cast=lambda x: int(x) / 1000)
    return v if isinstance(v, float) else None


# Monotonic counters (jiffies, bytes, sectors) for _rate() below. A lock only
# keeps concurrent pollers from reading each other's half-written sample — a
# wrong-instantaneous-value race would be harmless, a KeyError would not.
_counters: dict[str, tuple[float, float]] = {}
_counter_lock = threading.Lock()


def _rate(key: str, value: float) -> Optional[float]:
    """Per-second rate of a monotonic counter; None on a key's first sample or
    after a reset (reboot, interface re-created — the counter went backwards)."""
    now = time.monotonic()
    with _counter_lock:
        prev = _counters.get(key)
        _counters[key] = (value, now)
    if prev is None or value < prev[0]:
        return None
    dt = now - prev[1]
    return (value - prev[0]) / dt if dt > 0 else None


DRM_CLASS_DIR = "/sys/class/drm"
KFD_NODES_DIR = "/sys/class/kfd/kfd/topology/nodes"


def _bdf_of(path: str) -> Optional[str]:
    """A drm sysfs path → its PCI BDF (the component above 'drm/'), None for
    a virtual device or an unresolvable path."""
    parts = os.path.realpath(path).split("/")
    i = parts.index("drm") if "drm" in parts else -1
    return parts[i - 1] if i >= 1 and ":" in parts[i - 1] else None


def _rocr_indexes(cards: list[str]) -> Optional[dict[str, int]]:
    """card name → ROCR device index, from the KFD topology sysfs — or None
    to make the caller fall back to card-number order.

    ROCm enumerates devices in KFD registration order, and the kernel exposes
    exactly that order as /sys/class/kfd/kfd/topology/nodes/<id> — in id
    order, with the HOST CPU occupying a node too (vendor_id 0, always
    first), so ROCR device i is the (i+1)-th GPU node. The ROCm runtime reads
    these same files (its own topology code), so this is the order itself,
    not a proxy — no ROCm install, no /dev/kfd. A node maps to its card via
    unique_id (decimal here, hex in the card's device dir) or
    drm_render_minor (renderD<N> → BDF). None when the topology is absent
    (older kernel), unreadable (the service user lacks kfd access), or a
    node/card can't be placed — a half mapping is worse than the fallback.
    """
    nodes = sorted(
        (p for p in glob.glob(f"{KFD_NODES_DIR}/*") if os.path.basename(p).isdigit()),
        key=lambda p: int(os.path.basename(p)),
    )
    if not nodes:
        return None
    card_by_uid: dict[int, str] = {}
    card_by_bdf: dict[str, str] = {}
    for card in glob.glob(f"{DRM_CLASS_DIR}/card[0-9]*"):
        name = os.path.basename(card)
        if not re.match(r"^card[0-9]+$", name):
            continue  # cardN-DP-1 & friends share the card's BDF/device
        try:
            card_by_uid[int(_rd(f"{card}/device/unique_id", default=""), 16)] = name
        except ValueError:
            pass
        bdf = _bdf_of(card)
        if bdf:
            card_by_bdf[bdf] = name
    mapping: dict[str, int] = {}
    i = -1
    for node in nodes:
        props: dict[str, int] = {}
        for line in _rd(f"{node}/properties", default="").splitlines():
            key, _, val = line.partition(" ")
            try:
                props[key] = int(val)
            except ValueError:
                pass
        if not props.get("vendor_id"):
            continue  # the CPU node (vendor 0) — or unreadable properties
        i += 1
        card = card_by_uid.get(props["unique_id"]) if props.get("unique_id") else None
        if card is None and props.get("drm_render_minor"):
            bdf = _bdf_of(f"{DRM_CLASS_DIR}/renderD{props['drm_render_minor']}")
            card = card_by_bdf.get(bdf) if bdf else None
        if card is None:
            return None  # a GPU node we can't place: don't trust the mapping
        mapping[card] = i
    return mapping if set(mapping) == set(cards) else None


def amd_gpus() -> list[dict[str, Any]]:
    """AMD GPU stats from sysfs — no ROCm / amd-smi needed.

    Each entry carries `rocr_index`: the card's ROCR device number — what
    ROCR_VISIBLE_DEVICES (and the GPU pool's tile indices) count in. The
    kernel exposes the enumeration in the KFD topology sysfs (node id order,
    host CPU node skipped) — the same files the ROCm runtime reads, so
    _rocr_indexes() returns the exact order, not a proxy. The sysfs card NAME
    is NOT that index: a non-GPU drm device can take a card slot (on this
    host card0, so the seven R9700s are card1..card7 — joining the pool on the
    name was off by one: tile 0 had no readings and every tile showed the
    PREVIOUS card's). When the topology is unavailable or can't be fully
    mapped, the index falls back to card-number order (probe order — the same
    sequence KFD registers in). The list itself stays in card-number order
    (the Overview renders it as-is); the pool view joins on rocr_index.
    """
    out: list[dict[str, Any]] = []
    for card in sorted(glob.glob("/sys/class/drm/card[0-9]*"), key=_natural_key):
        dev = f"{card}/device"
        hwmons = sorted(glob.glob(f"{dev}/hwmon/hwmon*"), key=_natural_key)
        # Skip non-GPU drm devices (USB-C display adapters, framebuffers, …):
        # an amdgpu always exposes gpu_busy_percent.
        if not hwmons and _rd(f"{dev}/gpu_busy_percent") is None:
            continue
        hw = hwmons[0] if hwmons else None
        # No product_name on many kernels — the PCI id + slot are what make the
        # two identical cards distinguishable in the UI.
        uevent = _rd(f"{dev}/uevent", default="") or ""
        ue = dict(
            line.split("=", 1) for line in uevent.splitlines() if "=" in line
        )
        out.append(
            {
                "card": os.path.basename(card),
                "rocr_index": None,  # assigned in the KFD-order pass below
                "driver": ue.get("DRIVER", ""),
                "pci_id": ue.get("PCI_ID", ""),
                "pci_slot": ue.get("PCI_SLOT_NAME", ""),
                "temp_edge_c": _temp(f"{hw}/temp1_input" if hw else None),
                "temp_junction_c": _temp(f"{hw}/temp2_input" if hw else None),
                "temp_mem_c": _temp(f"{hw}/temp3_input" if hw else None),
                "util_pct": _rd(f"{dev}/gpu_busy_percent", int),
                "vram_used_mib": _rd(f"{dev}/mem_info_vram_used", lambda v: int(v) // 2**20),
                "vram_total_mib": _rd(f"{dev}/mem_info_vram_total", lambda v: int(v) // 2**20),
                "power_w": _hwrd(hw, "power1_average", lambda v: round(int(v) / 1e6, 1)),
                # The card's own power limit (some firmware only exposes the
                # ceiling): lets the Overview show W as % of the allowed budget.
                "power_cap_w": _hwrd(hw, "power1_cap", lambda v: round(int(v) / 1e6, 1))
                or _hwrd(hw, "power1_cap_max", lambda v: round(int(v) / 1e6, 1)),
                "fan_rpm": _hwrd(hw, "fan1_input", int),
                "clock_mhz": _hwrd(hw, "freq1_input", lambda v: int(v) // 10**6),
                "mem_clock_mhz": _hwrd(hw, "freq2_input", lambda v: int(v) // 10**6),
            }
        )
    # rocr_index: the KFD topology order (exact) when it is readable and
    # maps every card, else card-number order (probe order). See _rocr_indexes.
    rocr = _rocr_indexes([e["card"] for e in out])
    if rocr is None:
        rocr = {e["card"]: i for i, e in enumerate(out)}
    for e in out:
        e["rocr_index"] = rocr[e["card"]]
    return out


def cpu_temp_c() -> Optional[float]:
    for hm in sorted(glob.glob("/sys/class/hwmon/hwmon*"), key=_natural_key):
        if _rd(f"{hm}/name", default="") in ("k10temp", "zenpower", "coretemp", "cpu_thermal"):
            return _temp(f"{hm}/temp1_input")
    return None


def _cpu_cores() -> int:
    try:
        return len(os.sched_getaffinity(0)) or 1
    except (AttributeError, OSError):
        return os.cpu_count() or 1


_cpu_model_cache = ""


def _cpu_model() -> str:
    """/proc/cpuinfo 'model name' — constant for the process's lifetime, so read once."""
    global _cpu_model_cache
    if _cpu_model_cache:
        return _cpu_model_cache
    model = ""
    try:
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            key, _, val = line.partition(":")
            if key.strip() == "model name":
                model = val.strip()
                break
    except OSError:
        pass
    _cpu_model_cache = model
    return model


def _cpu_freq_mhz() -> Optional[float]:
    # amd-pstate exposes the current clock under cpufreq/policyN, intel-pstate
    # under cpuN/cpufreq — whichever the driver is, one of these exists.
    for p in (
        "/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq",
        "/sys/devices/system/cpu/cpufreq/policy0/scaling_cur_freq",
    ):
        v = _rd(p, cast=lambda x: round(int(x) / 1000))  # kHz → MHz
        if v is not None:
            return v
    return None


def cpu_block() -> dict[str, Any]:
    """Everything the CPU itself reports: temp, utilisation, clock, task counts.

    usage_pct is a Δ of /proc/stat's aggregate line (idle+iowait vs total
    jiffies). procs_blocked > 0 means tasks stuck in uninterruptible IO — the
    usual explanation for "everything is slow" and worth its own tile.
    """
    out: dict[str, Any] = {
        "temp_c": cpu_temp_c(),
        "cores": _cpu_cores(),
        "model": _cpu_model(),
        "freq_mhz": _cpu_freq_mhz(),
        "usage_pct": None,
        "procs_running": None,
        "procs_blocked": None,
    }
    try:
        lines = Path("/proc/stat").read_text().splitlines()
    except OSError:
        return out
    for line in lines:
        key, _, val = line.partition(" ")
        if key == "cpu":
            # Fields user..steal only: guest/guest_nice are already counted in
            # user/nice, so summing them twice would understate utilisation.
            f = val.split()
            try:
                nums = [int(x) for x in f[:8]]
            except ValueError:
                continue
            if len(nums) >= 5:
                idle = nums[3] + nums[4]
                dtot = _rate("cpu:total", float(sum(nums)))
                didle = _rate("cpu:idle", float(idle))
                if dtot and didle is not None and dtot > 0:
                    out["usage_pct"] = round(max(0.0, min(100.0, 100.0 * (1 - didle / dtot))), 1)
        elif key in ("procs_running", "procs_blocked"):
            try:
                out[key] = int(val.strip())
            except ValueError:
                pass
    return out


def _meminfo() -> dict[str, int]:
    """/proc/meminfo as key → kB (empty if unreadable)."""
    info: dict[str, int] = {}
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            key, _, val = line.partition(":")
            try:
                info[key] = int(val.strip().split()[0])  # kB
            except (ValueError, IndexError):
                continue
    except OSError:
        pass
    return info


def ram(mi: dict[str, int]) -> dict[str, int]:
    """RAM split so the segments ADD UP TO THE STICK: 'used' is memory the
    kernel can't hand back (total − MemAvailable), 'cache' is reclaimable page
    cache/buffers, 'free' is untouched. Calling page cache 'used' is the classic
    way to make a healthy box look nearly full.
    """
    total = mi.get("MemTotal", 0) // 1024
    if total <= 0:
        return {}
    avail = mi.get("MemAvailable", 0) // 1024
    free = mi.get("MemFree", 0) // 1024
    used = max(0, total - avail)
    return {
        "total_mib": total,
        "used_mib": used,
        "avail_mib": avail,
        "free_mib": free,
        "cache_mib": max(0, total - used - free),
    }


def swap(mi: dict[str, int]) -> dict[str, int]:
    """{} when there is no swap configured (then the Overview shows no tile)."""
    total = mi.get("SwapTotal", 0) // 1024
    if total <= 0:
        return {}
    return {"total_mib": total, "used_mib": max(0, total - mi.get("SwapFree", 0) // 1024)}


# Interfaces the network tile must not count: loopback, and anything whose
# traffic is already counted somewhere else — docker/veth/libvirt/k8s plumbing,
# bridges, bonds, VLANs and tunnels (a stacked device is exactly one that has
# lower_* symlinks next to it in /sys/class/net). Counting them alongside the
# physical NIC double-counts and buries the real link.
NET_VIRTUAL_RE = re.compile(r"^(lo|docker|veth|br|virbr|vnet|tap|tun|wg|zt|kube|flannel|cali|cni|lxc|nerd|dummy|ifb|gre|gretap|ip6gre|sit|ppp)")


def net_rates() -> dict[str, Any]:
    """Bits/s in and out over the physical interfaces (Δ of their byte counters)."""
    names: list[str] = []
    rx = tx = 0
    for path in sorted(glob.glob("/sys/class/net/*"), key=_natural_key):
        name = os.path.basename(path)
        if NET_VIRTUAL_RE.match(name) or glob.glob(f"{path}/lower_*"):
            continue
        r, t = _rd(f"{path}/statistics/rx_bytes", int), _rd(f"{path}/statistics/tx_bytes", int)
        if r is None or t is None:
            continue
        names.append(name)
        rx += r
        tx += t
    if not names:
        return {}
    key = "net:" + ",".join(names)  # interface set changed → restart the delta
    out: dict[str, Any] = {"ifaces": names}
    for field, suffix, total in (("rx_bits_s", "rx", rx), ("tx_bits_s", "tx", tx)):
        v = _rate(f"{key}:{suffix}", float(total))
        if v is not None:
            out[field] = max(0.0, v * 8)
    return out


def _block_name(maj_min: str) -> str:
    """'259:3' → 'nvme0n1p3' — the kernel name, i.e. the /proc/diskstats key."""
    try:
        return os.path.basename(os.readlink(f"/sys/dev/block/{maj_min}"))
    except OSError:
        return ""


def sensors() -> list[dict[str, Any]]:
    """Every hwmon chip and its HOTTEST sensor, reported raw for the UI to curate.

    Deliberately not filtered here: the kernel already tells us what a chip is
    (nvme, spd5118 = DDR5 DIMM sensors, r8169 = NIC, …), and which of those are
    worth a human's attention is a display decision. The API's job is to say
    what the box knows; the Overview decides what it's worth showing.
    """
    out: list[dict[str, Any]] = []
    for hm in sorted(glob.glob("/sys/class/hwmon/hwmon*"), key=_natural_key):
        chip = _rd(f"{hm}/name", default="") or ""
        try:
            # .../nvme/nvme1 → nvme1, .../27-0050 → 27-0050: what identifies the chip.
            device = os.path.basename(os.path.realpath(f"{hm}/device"))
        except OSError:
            device = ""
        readings: list[tuple[float, str]] = []
        for inp in sorted(glob.glob(f"{hm}/temp*_input"), key=_natural_key):
            key = os.path.basename(inp)[: -len("_input")]
            t = _temp(inp)
            if t is not None:
                readings.append((t, _rd(f"{hm}/{key}_label", default="") or ""))
        if not readings:
            continue
        temp, label = max(readings)
        out.append({"chip": chip, "device": device, "label": label, "temp_c": temp})
    return out


def nvme_temps() -> dict[str, float]:
    """nvme controller (nvme1) → drive temperature, for the storage rows."""
    return {s["device"]: s["temp_c"] for s in sensors() if s["chip"] == "nvme" and s["device"].startswith("nvme")}


def _mountinfo() -> dict[str, dict[str, str]]:
    """mountpoint → {device, fstype, block} from /proc/self/mountinfo (the last
    entry for a mountpoint wins, which is the most specific one). Octal-escaped
    spaces in paths aren't decoded — no configured mount uses them."""
    out: dict[str, dict[str, str]] = {}
    try:
        lines = Path("/proc/self/mountinfo").read_text().splitlines()
    except OSError:
        return out
    for line in lines:
        f = line.split()
        try:
            sep = f.index("-")  # fields before it are optional, this one is the divider
        except ValueError:
            continue
        if sep < 6 or len(f) < sep + 3:
            continue
        out[f[4]] = {"device": f[sep + 2], "fstype": f[sep + 1], "block": _block_name(f[2])}
    return out


def _diskstats() -> dict[str, tuple[int, int]]:
    """block device → (bytes read, bytes written); sectors are always 512 bytes."""
    out: dict[str, tuple[int, int]] = {}
    try:
        for line in Path("/proc/diskstats").read_text().splitlines():
            f = line.split()
            if len(f) < 10:
                continue
            try:
                out[f[2]] = (int(f[5]) * 512, int(f[9]) * 512)
            except ValueError:
                continue
    except OSError:
        pass
    return out


def disks() -> list[dict[str, Any]]:
    """Usage per mount in DISK_MOUNTS. Every configured mount ALWAYS gets an
    entry: stats, or an explicit error — an unmounted mountpoint makes statvfs
    report the *containing* filesystem, so its absence must shout, not lie.
    Each entry also says WHAT it is — block device, filesystem, and throughput
    since the previous poll (model loads make a storage tile worth watching).
    """
    try:
        root_dev: Optional[int] = os.stat("/").st_dev
    except OSError:
        root_dev = None
    mounts, io_stats = _mountinfo(), _diskstats()
    temps = nvme_temps()
    out: list[dict[str, Any]] = []
    for mount in DISK_MOUNTS:
        info = mounts.get(mount, {})
        entry: dict[str, Any] = {
            "mount": mount,
            "device": info.get("block") or os.path.basename(info.get("device", "")),
            "fstype": info.get("fstype", ""),
        }
        # An NVMe that cooks throttles and dies young — the drive's own temp
        # belongs next to its mount, not buried in a sensor dump.
        drive = re.match(r"^(nvme\d+)", entry["device"] or "")
        if drive and drive.group(1) in temps:
            entry["temp_c"] = temps[drive.group(1)]
        blk = info.get("block", "")
        if blk in io_stats:
            rd = _rate(f"io:{blk}:r", float(io_stats[blk][0]))
            wr = _rate(f"io:{blk}:w", float(io_stats[blk][1]))
            if rd is not None:
                entry["read_bytes_s"] = max(0.0, rd)
            if wr is not None:
                entry["write_bytes_s"] = max(0.0, wr)
        try:
            st = os.statvfs(mount)
            dev = os.stat(mount).st_dev
        except OSError:
            out.append({**entry, "error": "path does not exist"})
            print(f"panel-api: disk: {mount} unavailable (stat failed)", flush=True)
            continue
        if st.f_blocks == 0:
            out.append({**entry, "error": "not a mountable filesystem (no blocks)"})
            print(f"panel-api: disk: {mount} reports no blocks (pseudo-fs?)", flush=True)
            continue
        if mount != "/" and root_dev is not None and dev == root_dev:
            out.append({**entry, "error": "NOT MOUNTED — partition failed to mount?"})
            print(f"panel-api: disk: {mount} is on /'s device — not mounted", flush=True)
            continue
        total = st.f_blocks * st.f_frsize
        free = st.f_bfree * st.f_frsize
        avail = st.f_bavail * st.f_frsize  # usable by non-root (excludes reserved)
        out.append(
            {
                **entry,
                "total_mib": total // 2**20,
                "used_mib": max(0, total - free) // 2**20,
                "avail_mib": avail // 2**20,
            }
        )
    return out


def load_avg() -> list[float]:
    parts = (_rd("/proc/loadavg") or "").split()
    try:
        return [float(x) for x in parts[:3]]
    except ValueError:
        return []


def uptime_s() -> Optional[float]:
    v = _rd("/proc/uptime")
    if not v:
        return None
    try:
        return float(v.split()[0])
    except (ValueError, IndexError):
        return None


@app.get("/api/metrics")
def metrics(_admin: Optional[str] = Depends(require_admin)) -> dict[str, Any]:
    mi = _meminfo()
    uname = os.uname()
    gpus = amd_gpus()
    # The pool's reservations, joined on rocr_index — the Overview marks the
    # dedicated card at a glance (the GPU view carries the full form + the
    # controls). Read-only under the ledger lock, like every view poll.
    with gpu_pool.gpu_lock():
        reserved = gpu_pool.load_ledger()["reserved"]
    for e in gpus:
        r = reserved.get(e["rocr_index"])
        e["reserved_for"] = r["for"] if r else None
        e["reserved_note"] = r["note"] if r else None
    return {
        "ts": int(time.time()),
        "host": {"hostname": uname.nodename, "kernel": uname.release},
        "gpus": gpus,
        "cpu": cpu_block(),
        "ram": ram(mi),
        "swap": swap(mi),
        "disks": disks(),
        "load": load_avg(),
        "uptime_s": uptime_s(),
        "net": net_rates(),
        "sensors": sensors(),
    }


@app.get("/api/health")
def health() -> dict[str, Any]:
    # Deliberately UNauthenticated (liveness-probe pattern): fixed minimal
    # body, no state, no privileges, and the docker0 bind is only reachable
    # from this host's containers. Verify auth against /api/metrics instead.
    return {"ok": True, "dev": DEV_MODE, "ts": int(time.time())}


# ───────────────────────── containers ────────────────────────────
# Container control is NOT a second privileged path — these are panel-do
# actions (list-containers, start/stop/restart-container): same registry,
# validation, and journald audit as every other action.

_containers_cache: dict[str, Any] = {"data": None, "at": 0.0}


def _sudo_panel_do(action: str, args: list[str], timeout: float) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["sudo", "-n", PANEL_DO, action, *args],
        capture_output=True, text=True, timeout=timeout,
    )


def _cli_label_get(labels: str, key: str) -> Optional[str]:
    """docker ps --format json encodes Labels as a comma-joined string."""
    for part in (labels or "").split(","):
        k, _, v = part.partition("=")
        if k == key:
            return v
    return None


def _container_view(x: dict[str, Any]) -> dict[str, Any]:
    cid = (x.get("ID") or "")[:12]
    raw_names = x.get("Names") or ""
    name = ((raw_names[0] if isinstance(raw_names, list) else raw_names) or "").lstrip("/")
    display = _cli_label_get(x.get("Labels") or "", MANAGE_LABEL)
    return {
        "id": cid,
        "name": display if display and display != "true" else (name or cid),
        # compose service name (container label) — the UI joins this against
        # /api/stack-services to find defined services whose container is gone.
        "service": _cli_label_get(x.get("Labels") or "", "com.docker.compose.service"),
        # Services view group (panel.category label); None/"" render as "Other".
        "category": _cli_label_get(x.get("Labels") or "", CATEGORY_LABEL),
        # gpu.private=true — no public host: the Services view must not render
        # its dashboard link (the <sub>.pietrasik.top/open host does not exist).
        "private": _cli_label_get(x.get("Labels") or "", PRIVATE_LABEL) == "true",
        "image": x.get("Image", ""),
        "state": x.get("State", "unknown"),
        "status": x.get("Status", ""),
    }


def _containers_fetch() -> list[dict[str, Any]]:
    try:
        proc = _sudo_panel_do("list-containers", [], 20.0)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise HTTPException(500, f"panel-do list-containers: {e}") from e
    if proc.returncode != 0:
        raise HTTPException(500, f"list-containers failed: {proc.stderr.strip()[-300:]}")
    out: list[dict[str, Any]] = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError as e:
            raise HTTPException(502, f"list-containers: bad JSON line: {e}") from e
    return out


def containers_cached() -> list[dict[str, Any]]:
    """Full `docker ps -a` objects (panel-do list-containers JSON), 3 s TTL.
    Raw on purpose: /api/containers filters on the Labels field and the LLM
    router's fleet view needs it too — the UI shape is _container_view()."""
    now = time.monotonic()
    if _containers_cache["data"] is not None and now - _containers_cache["at"] < CONTAINERS_CACHE_TTL:
        return _containers_cache["data"]
    data = _containers_fetch()
    _containers_cache["data"] = data
    _containers_cache["at"] = now
    return data


def invalidate_containers_cache() -> None:
    _containers_cache["data"] = None


def _err_detail(proc: subprocess.CompletedProcess) -> str:
    """One-line failure detail: last stderr line, or the exit code."""
    err_lines = proc.stderr.strip().splitlines()
    return err_lines[-1] if err_lines else f"exit {proc.returncode}"


def _audit(user: Optional[str], action: str, args: list[str], result: int | str, err: str = "") -> None:
    """One journal line per privileged invocation (stdout → journald).
    sudo's own log only says *panel* ran the command; this records the
    triggering Authelia user and the outcome."""
    line = (
        f"panel-api: audit user={user or 'dev'} action={action} "
        f"args={args!r} result={result}"
    )
    if err:
        line += f" err={err[:500]}"
    print(line, flush=True)


@app.get("/api/containers")
def containers(_admin: Optional[str] = Depends(require_admin)) -> dict[str, Any]:
    managed = [
        _container_view(x)
        for x in containers_cached()
        if _cli_label_get(x.get("Labels") or "", MANAGE_LABEL) is not None
    ]
    return {"containers": managed}


_STACK_SERVICES_TTL = 15.0  # the defined set only changes via make update / deploy
_stack_services_cache: dict[str, Any] = {"data": None, "at": 0.0}


def _stack_services_fetch() -> list[dict[str, Any]]:
    try:
        proc = _sudo_panel_do("list-services", [], 20.0)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise HTTPException(500, f"panel-do list-services: {e}") from e
    if proc.returncode != 0:
        raise HTTPException(500, f"list-services failed: {proc.stderr.strip()[-300:]}")
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        raise HTTPException(502, f"list-services: bad JSON: {e}") from e
    if not isinstance(data, list):
        raise HTTPException(502, "list-services: expected a JSON array")
    return data


def stack_services_cached() -> list[dict[str, Any]]:
    """Deployed compose services carrying panel.manage (panel-do list-services
    JSON), 15 s TTL. The Services view diffs this against the container list:
    defined-but-container-less services get a create+start button (the
    panel-do start-service action)."""
    now = time.monotonic()
    if (
        _stack_services_cache["data"] is not None
        and now - _stack_services_cache["at"] < _STACK_SERVICES_TTL
    ):
        return _stack_services_cache["data"]
    data = _stack_services_fetch()
    _stack_services_cache["data"] = data
    _stack_services_cache["at"] = now
    return data


@app.get("/api/stack-services")
def stack_services(_admin: Optional[str] = Depends(require_admin)) -> dict[str, Any]:
    return {"services": stack_services_cached()}


@app.post("/api/containers/{cid}/{action}")
def container_action(cid: str, action: str, _admin: Optional[str] = Depends(require_admin)) -> dict[str, Any]:
    if action not in CONTAINER_VERBS:
        raise HTTPException(400, f"action must be one of {sorted(CONTAINER_VERBS)}")
    if not CID_RE.match(cid):
        raise HTTPException(400, "bad container id")
    name = f"{action}-container"
    meta = {a["name"]: a for a in panel_do_actions()}.get(name)
    timeout = min(int(meta.get("timeout_sec", 60)) + 10, 660) if meta else 70.0
    try:
        proc = _sudo_panel_do(name, [cid], timeout)
    except subprocess.TimeoutExpired as e:
        _audit(_admin, name, [cid], "timeout")
        raise HTTPException(504, "action timed out") from e
    except OSError as e:
        _audit(_admin, name, [cid], "spawn-error")
        raise HTTPException(500, f"sudo failed: {e}") from e
    finally:
        invalidate_containers_cache()
    if proc.returncode == 0:
        _audit(_admin, name, [cid], 0)
        return {"ok": True}
    detail = _err_detail(proc)
    _audit(_admin, name, [cid], proc.returncode, detail)
    raise HTTPException(502, detail)


# ──────────────────────── privileged actions ─────────────────────

_actions_cache: dict[str, Any] = {"data": None, "at": 0.0}


def panel_do_actions() -> list[dict[str, Any]]:
    """Action registry, straight from the horse's mouth: `panel-do --list`.

    Cached ~60s so the UI can never drift from what the binary enforces.
    """
    now = time.monotonic()
    if _actions_cache["data"] is not None and now - _actions_cache["at"] < LIST_CACHE_TTL:
        return _actions_cache["data"]
    try:
        proc = _sudo_panel_do("--list", [], 10.0)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise HTTPException(500, f"panel-do --list: {e}") from e
    if proc.returncode != 0:
        raise HTTPException(500, f"panel-do --list failed: {proc.stderr.strip()[:300]}")
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        raise HTTPException(500, f"panel-do --list: bad JSON: {e}") from e
    _actions_cache["data"] = data
    _actions_cache["at"] = now
    return data


@app.get("/api/actions")
def actions(_admin: Optional[str] = Depends(require_admin)) -> dict[str, Any]:
    return {"actions": panel_do_actions()}


class ActionBody(BaseModel):
    args: list[str] = []


@app.post("/api/action/{name}")
def run_action(name: str, body: ActionBody, _admin: Optional[str] = Depends(require_admin)) -> dict[str, Any]:
    # Light sanity checks (panel-do re-validates authoritatively).
    if not ACTION_NAME_RE.match(name):
        raise HTTPException(400, "bad action name")
    registry = {a["name"]: a for a in panel_do_actions()}
    meta = registry.get(name)
    if meta is None:
        raise HTTPException(404, f"unknown action {name!r}")
    for i, arg in enumerate(body.args):
        if not ARG_RE.match(arg) or arg.startswith("-"):
            raise HTTPException(400, f"arg {i}: invalid")

    timeout = min(int(meta.get("timeout_sec", 60)) + 10, 660)
    try:
        proc = _sudo_panel_do(name, body.args, timeout)
    except subprocess.TimeoutExpired as e:
        _audit(_admin, name, body.args, "timeout")
        raise HTTPException(504, "action timed out") from e
    except OSError as e:
        _audit(_admin, name, body.args, "spawn-error")
        raise HTTPException(500, f"sudo failed: {e}") from e
    err = _err_detail(proc) if proc.returncode else ""
    _audit(_admin, name, body.args, proc.returncode, err)
    invalidate_containers_cache()  # e.g. a container action may have changed the fleet
    return {
        "ok": proc.returncode == 0,
        "exit_code": proc.returncode,
        "stdout": proc.stdout[-8000:],
        "stderr": proc.stderr[-8000:],
    }


# ─────────────────────────────── updates ────────────────────────────
# The stack's update lifecycle is panel-do's work (update-run / -force /
# -report / -preview / -rollback — see panel-do/main.go). This section adds
# only the HTTP shape and the MAJOR classification the dashboard badges off.
# The long work is detached INSIDE the update script (its step 6 restarts
# THIS service — the trigger must not be in its process tree), so every call
# here returns in seconds. The update state file (/opt/panel/.update-state,
# root-written) is the control plane: every run flips it to in-progress under
# the lock BEFORE any gate (a pre-snapshot failure is left `failed` — nothing
# applied, nothing to roll back), the snapshot step records repo_root
# (+ repo_head) and the pre-update images, and update-report joins that
# snapshot against the running stack.


def _first_int(s: Optional[str]) -> Optional[int]:
    """First digit run of a version-ish string: '3.7.9'→3, 'v3'→3,
    '22-alpine'→22, '>=0.115,<1'→0. None when there are no digits at all
    (`latest`, `stable-alpine`) — those lines have no major to compare
    (RUNBOOK → Updates, "The lines")."""
    if not s:
        return None
    m = re.search(r"\d+", s)
    return int(m.group(0)) if m else None


def _classify_change(old: str, new: str) -> str:
    """major / minor / changed / unchanged for a version pair: major = the
    leading integer moved, minor = same major, changed = no comparable
    integer on one side."""
    if old == new:
        return "unchanged"
    if not old or not new:
        return "changed"
    po, pn = _first_int(old), _first_int(new)
    if po is None or pn is None:
        return "changed"
    return "major" if po != pn else "minor"


def _image_tag(ref: str) -> str:
    """'traefik:v3' → 'v3' — the comparable part (the registry host carries
    no version)."""
    return ref.rsplit(":", 1)[1] if ":" in ref else ""


def _read_text_or_empty(p: str) -> str:
    try:
        return Path(p).read_text()
    except OSError:
        return ""


def _parse_compose_images(text: str) -> dict[str, str]:
    """service → image ref from a compose file. Line-based (this file is
    hand-kept at 2/4-indent): an explicit `image:` wins, a `<<: *anchor` merge
    resolves to the anchor's own image (the x-gpu-base pattern — gpu-* fleet
    instances carry no image line of their own)."""
    anchors: dict[str, str] = {}
    images: dict[str, str] = {}
    svc_anchor: dict[str, str] = {}
    section = None
    service = None
    for raw in text.splitlines():
        # inline comments stripped (compose allows `value # comment`) — the
        # radiance line depends on it
        line = raw.split(" #", 1)[0].rstrip()
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip(" "))
        key = line.strip()
        if indent == 0:
            section, service = None, None
            m = re.match(r"^x-([A-Za-z0-9_-]*):\s*&([A-Za-z0-9_-]+)\s*$", key)
            if m:
                section = f"anchor:{m.group(2)}"
                anchors[m.group(2)] = ""
            elif key.endswith(":"):
                section = "services" if key.rstrip(":") == "services" else "other"
        elif section == "services" and indent == 2 and key.endswith(":"):
            service = key[:-1].strip()
            svc_anchor.setdefault(service, "")
        elif section and section.startswith("anchor:") and indent == 2:
            if key.startswith("image:"):
                anchors[section[7:]] = key.split(":", 1)[1].strip()
        elif section == "services" and indent == 4 and service is not None:
            if key.startswith("image:"):
                images[service] = key.split(":", 1)[1].strip()
            elif key.startswith("<<:"):
                m = re.search(r"\*([A-Za-z0-9_-]+)", key)
                if m:
                    svc_anchor[service] = m.group(1)
    for service, alias in svc_anchor.items():
        if service not in images and anchors.get(alias):
            images[service] = anchors[alias]
    return images


def _parse_froms(text: str) -> list[str]:
    """FROM bases of a Dockerfile, in stage order."""
    out = []
    for line in text.splitlines():
        m = re.match(r"^\s*FROM\s+(\S+)", line, re.IGNORECASE)
        if m:
            out.append(m.group(1))
    return out


def _parse_ranges(text: str) -> dict[str, str]:
    """name → declared range from a requirements.txt line set."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r"^([A-Za-z0-9._-]+)\s*(\S.*)$", line)
        if m:
            out[m.group(1)] = m.group(2)
    return out


def _npm_deps(text: str) -> dict[str, str]:
    try:
        d = json.loads(text)
    except (ValueError, TypeError):
        return {}
    if not isinstance(d, dict):
        return {}
    return {**d.get("dependencies", {}), **d.get("devDependencies", {})}


def _classify_update_row(s: dict[str, Any], images: dict[str, str]) -> str:
    """One update-report row (images = the deployed compose's service → image
    refs, parsed once by the caller):
      removed    — no container for the service after the update
      unchanged  — running image is the pre-update image (byte-identical)
      rebuilt    — a locally built image (no `image:` in compose): the
                   version IS the build, there is no upstream to compare
      major      — the registry image's leading version integer moved: a
                   deliberate line edit — this is what the notification is for
      minor      — same major, new version: a patch/minor release floated in
      moved      — a registry image without a comparable version label
                   (radiance / searxng `latest` — a new build moved)
    """
    if s.get("post_image") is None:
        return "removed"
    if s["post_image"] == s.get("pre_image"):
        return "unchanged"
    if s["service"] not in images:
        return "rebuilt"
    if s.get("pre_version") is None or s.get("post_version") is None:
        return "moved"
    return _classify_change(s["pre_version"], s["post_version"])


def _preview_changes(data: dict[str, Any]) -> list[dict[str, str]]:
    """What the next update would move: the clone's line files (from panel-do
    — the clone is NOT panel-readable: its users_database.yml holds argon2
    hashes) vs the deployed copies (readable here: the sync keeps them 0644).
    One row per changed line; the dashboard highlights every `major`."""
    files = data.get("files") or {}
    changes: list[dict[str, str]] = []

    dep = _parse_compose_images(_read_text_or_empty("/opt/panel/docker-compose.yml"))
    new = _parse_compose_images(files.get("compose") or "")
    for svc in sorted(set(dep) | set(new)):
        old, nw = dep.get(svc, ""), new.get(svc, "")
        if old != nw:
            changes.append({"kind": "image", "name": svc, "old": old, "new": nw,
                            "change": _classify_change(_image_tag(old), _image_tag(nw))})

    depf = _parse_froms(_read_text_or_empty("/opt/panel/panel-frontend/Dockerfile"))
    newf = _parse_froms(files.get("dockerfile") or "")
    for i in range(max(len(depf), len(newf))):
        old = depf[i] if i < len(depf) else ""
        nw = newf[i] if i < len(newf) else ""
        if old != nw:
            changes.append({"kind": "base", "name": f"panel-web FROM #{i + 1}", "old": old, "new": nw,
                            "change": _classify_change(_image_tag(old), _image_tag(nw))})

    depn = _npm_deps(_read_text_or_empty("/opt/panel/panel-frontend/package.json"))
    newn = _npm_deps(files.get("package_json") or "")
    for pkg in sorted(set(depn) | set(newn)):
        old, nw = depn.get(pkg, ""), newn.get(pkg, "")
        if old != nw:
            changes.append({"kind": "npm", "name": pkg, "old": old, "new": nw,
                            "change": _classify_change(old, nw)})

    depp = _parse_ranges(_read_text_or_empty("/opt/panel-api/requirements.txt"))
    newp = _parse_ranges(files.get("requirements") or "")
    for pkg in sorted(set(depp) | set(newp)):
        old, nw = depp.get(pkg, ""), newp.get(pkg, "")
        if old != nw:
            changes.append({"kind": "pip", "name": pkg, "old": old, "new": nw,
                            "change": _classify_change(old, nw)})
    return changes


def _update_fetch(name: str) -> dict[str, Any]:
    try:
        proc = _sudo_panel_do(name, [], 60.0)
    except (subprocess.TimeoutExpired, OSError) as e:
        raise HTTPException(500, f"panel-do {name}: {e}") from e
    if proc.returncode != 0:
        raise HTTPException(500, f"{name} failed: {proc.stderr.strip()[-300:]}")
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        raise HTTPException(502, f"{name}: bad JSON: {e}") from e
    if not isinstance(data, dict):
        raise HTTPException(502, f"{name}: expected a JSON object")
    return data


def _update_action(name: str, user: Optional[str], timeout: float) -> dict[str, Any]:
    """Run a fast (detached-launch) update action. Expected outcomes —
    skipped-dirty, already running (panel-do exit 75, EX_TEMPFAIL) — come
    back as ok:false with the skip message, not a 5xx: the UI renders them,
    they are not server faults. (The timer's unit maps 75 → clean exit via
    SuccessExitStatus; the reason stays in its journal.)"""
    try:
        proc = _sudo_panel_do(name, [], timeout)
    except subprocess.TimeoutExpired as e:
        _audit(user, name, [], "timeout")
        raise HTTPException(504, f"{name} timed out") from e
    except OSError as e:
        _audit(user, name, [], "spawn-error")
        raise HTTPException(500, f"sudo failed: {e}") from e
    err = _err_detail(proc) if proc.returncode else ""
    _audit(user, name, [], proc.returncode, err)
    return {
        "ok": proc.returncode == 0,
        "exit_code": proc.returncode,
        "stdout": proc.stdout[-4000:],
        "stderr": proc.stderr[-4000:],
    }


class UpdateRunBody(BaseModel):
    # force = the panel's "run anyway" (the clone has uncommitted changes):
    # panel-do's update-run-force (same gates, no dirty-skip).
    force: bool = False


@app.post("/api/update")
def update_run(body: UpdateRunBody, _admin: Optional[str] = Depends(require_admin)) -> dict[str, Any]:
    # Returns in seconds: the panel-do action launches the update script
    # DETACHED (it re-execs itself in a new session — the update's step 6
    # restarts this very service, so the trigger cannot be in its tree).
    name = "update-run-force" if body.force else "update-run"
    return _update_action(name, _admin, 120.0)


@app.post("/api/update/rollback")
def update_rollback(_admin: Optional[str] = Depends(require_admin)) -> dict[str, Any]:
    return _update_action("update-rollback", _admin, 120.0)


@app.get("/api/update")
def update_status(_admin: Optional[str] = Depends(require_admin)) -> dict[str, Any]:
    # The dashboard's control-plane poll: the last update's before→after,
    # classified here (the presentation layer — panel-do emits raw facts).
    data = _update_fetch("update-report")
    images = _parse_compose_images(_read_text_or_empty("/opt/panel/docker-compose.yml"))
    for row in data.get("services", []):
        row["change"] = _classify_update_row(row, images)
    return data


@app.get("/api/update/preview")
def update_preview(_admin: Optional[str] = Depends(require_admin)) -> dict[str, Any]:
    data = _update_fetch("update-preview")
    data["changes"] = _preview_changes(data)
    # `files` is the raw line files panel-do read from the clone (the diff
    # input just consumed, ~20 KB) — not dashboard data. Stripping it keeps
    # the payload from riding every poll to a phone on metered data.
    data.pop("files", None)
    return data


# ─────────────────────────── LLM fallback router ─────────────────────────────
# llm.pietrasik.top → this service (router in traefik/dynamic/llm.yml). Routes
# OpenAI-style /v1/* to the FIRST instance that's up in the admin-configured
# priority order (panel Settings → "llm.pietrasik.top"). Strictly single-active
# — the point is "the best model I'm serving right now", never load-split.
#
# Auth is the same gate as the per-instance subdomains: Authorization: Bearer
# with LLM_API_KEY. No Authelia here — API clients speak bearer tokens, and
# forward-auth is cookie-based (see the gpu.yml header). /v1/models needs no
# special handling: it simply proxies to the active instance. The gate is
# NOT bypassed by PANEL_DEV=1 (unlike the panel endpoints): local dev of
# the router needs LLM_API_KEY set to any non-empty value.
#
# Security: the order holds compose SERVICE names (gpu-<name> — what the
# traefik rules and `make rollback` use), validated against the live gpu-*
# containers and resolved to edge-network IPs server-side — the config
# (and its admin) can never name an arbitrary host address, so there is no
# SSRF surface beyond the fleet the admin already fully controls.

LLM_PROBE_INTERVAL_S = 30.0  # background /health refresh per instance
LLM_PROBE_TIMEOUT_S = 3.0  # /health budget (connect+read)
LLM_STALE_AFTER_S = 90.0  # a cached "up" older than this forces an on-demand re-probe
LLM_COOLDOWN_S = 30.0  # passive: a failed proxied request parks the instance this long
LLM_MAX_ORDER = 8
# Hard cap on /v1 request bodies (bytes). Buffering is intrinsic to the
# fallback router: the serving instance is picked per request, and a 5xx
# from the first choice must fall through to the next — so the whole body
# sits in panel-api's RAM before any forward. That makes the cap a
# memory-exhaustion guard on this internet-facing, bearer-gated endpoint:
# without it, a leaked key or a buggy client could trickle gigabytes in
# and OOM the host service that also runs the whole admin surface. Sized
# for coding-agent workloads: base64 images inflate ~33%, and one request
# can carry several multi-MB crops (screenshot/OCR-style work).
LLM_MAX_BODY = 64 * 1024 * 1024
GPU_NAME_RE = re.compile(r"^gpu-[a-z0-9][a-z0-9-]{0,62}$")

# Stripped in both directions: RFC 7230 §6.1 hop-by-hop + content-length
# (recomputed by starlette) + accept-encoding (forced identity, so upstream
# bytes pass through untransmuted).
HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
        "host",
        "content-length",
        "accept-encoding",
    }
)

_llm_http: Optional[httpx.AsyncClient] = None  # created in lifespan
_llm_health: dict[str, dict[str, Any]] = {}  # name → {"up": bool, "at": float, "err": str}
_llm_down_until: dict[str, float] = {}  # name → monotonic cooldown end (passive failures)
_edge_ips_cache: dict[str, Any] = {"data": None, "at": 0.0}
EDGE_IPS_TTL = 5.0  # IPs only change when a container is (re)created


def _llm_load() -> list[str]:
    """The priority order from disk (small file; re-read per use, no lock)."""
    try:
        data = json.loads(LLM_SETTINGS_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return []
    order = data.get("order")
    return [n for n in order if isinstance(n, str)] if isinstance(order, list) else []


def _llm_save(order: list[str]) -> None:
    LLM_SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = LLM_SETTINGS_PATH.with_name(LLM_SETTINGS_PATH.name + ".tmp")
    tmp.write_text(json.dumps({"order": order}, indent=2) + "\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, LLM_SETTINGS_PATH)


def _edge_ips() -> dict[str, str]:
    """Container name → edge-network IPv4 (panel-do edge-ips, 5 s cache).
    Empty on failure (e.g. the edge network was deleted) — instances then
    simply report down, never a wrong IP."""
    now = time.monotonic()
    if _edge_ips_cache["data"] is not None and now - _edge_ips_cache["at"] < EDGE_IPS_TTL:
        return _edge_ips_cache["data"]
    out: dict[str, str] = {}
    try:
        proc = _sudo_panel_do("edge-ips", [], 20.0)
        if proc.returncode == 0:
            data = json.loads(proc.stdout or "{}")
            # docker renders an EMPTY .Containers map as `null` on some
            # versions — an empty fleet, not a parse error.
            if isinstance(data, dict):
                for ep in data.values():
                    name = (ep.get("Name") or "").lstrip("/")
                    ip = (ep.get("IPv4Address") or "").partition("/")[0]
                    if name and ip:
                        out[name] = ip
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        pass
    _edge_ips_cache["data"] = out
    _edge_ips_cache["at"] = now
    return out


def _llm_mark(name: str, up: bool, err: str = "") -> None:
    _llm_health[name] = {"up": up, "at": time.monotonic(), "err": err}


def _llm_fresh_down(name: str) -> bool:
    """Knows it's down, recently — skip it (avoids 5 s connect hangs on a
    wedged instance). Once stale, the proxy tries it anyway (connection-
    refused fails in ms), so the probe loop dying can't wedge failover."""
    h = _llm_health.get(name)
    now = time.monotonic()
    return bool(h and not h["up"] and now - h["at"] < LLM_PROBE_INTERVAL_S)


async def _llm_probe(client: httpx.AsyncClient, name: str, ip: str) -> None:
    try:
        r = await client.get(f"http://{ip}:8000/health", timeout=LLM_PROBE_TIMEOUT_S)
        _llm_mark(name, r.status_code == 200, "" if r.status_code == 200 else f"/health → {r.status_code}")
    except httpx.HTTPError as e:
        _llm_mark(name, False, type(e).__name__)


async def _llm_probe_loop() -> None:
    while True:
        await asyncio.sleep(LLM_PROBE_INTERVAL_S)
        if _llm_http is None:
            continue
        try:
            order = await asyncio.to_thread(_llm_load)
            ips = await asyncio.to_thread(_edge_ips)
            fleet = await asyncio.to_thread(_gpu_fleet)
        except Exception:  # noqa: BLE001 — a probe-cycle hiccup is not fatal
            continue
        ip_by_name = _gpu_ip_by_name(fleet, ips)

        async def one(name: str) -> None:
            ip = ip_by_name.get(name, "")
            if ip:
                await _llm_probe(_llm_http, name, ip)
            else:
                _llm_mark(name, False, "container not on the edge network")

        await asyncio.gather(*(one(n) for n in order), return_exceptions=True)
        # forget names removed from the order (keeps the status map clean)
        for name in [n for n in _llm_health if n not in order]:
            del _llm_health[name]
            _llm_down_until.pop(name, None)


def _gpu_fleet() -> list[dict[str, str]]:
    """All gpu-* containers (running or stopped) as {name, container, display, state}.

    `name` is the identifier the order holds: the compose SERVICE name
    (gpu-27b — the same name the traefik rules, the RUNBOOK and `make
    rollback WHAT=…` use) when the container carries the compose service
    label, else the bare container name (a hand-`docker run`-ed gpu-*).
    NOT the docker container name — compose prefixes it (panel-gpu-27b),
    so keying on it would find nothing on the deployed stack.
    `container` is that docker name: the key _edge_ips() maps to an IP.
    """
    out: list[dict[str, str]] = []
    for x in containers_cached():
        raw_names = x.get("Names") or ""
        container = str(raw_names[0] if isinstance(raw_names, list) else raw_names or "").lstrip("/")
        svc = _cli_label_get(x.get("Labels") or "", "com.docker.compose.service")
        name = svc if svc and GPU_NAME_RE.match(svc) else container
        if not name.startswith("gpu-"):
            continue
        state = x.get("State", "unknown")
        # a recreated service leaves its old container in `ps -a` — two rows
        # may claim one name; keep the running one
        for i, f in enumerate(out):
            if f["name"] == name:
                if not (f["state"] == "running" and state != "running"):
                    out.pop(i)
                break
        # Same display fallback as _container_view: a bare `panel.manage=true`
        # label is a marker, not a name.
        display = _cli_label_get(x.get("Labels") or "", MANAGE_LABEL)
        out.append(
            {
                "name": name,
                "container": container,
                "display": display if display and display != "true" else name,
                "state": state,
            }
        )
    return sorted(out, key=lambda f: f["name"])


def _gpu_fleet_view() -> list[dict[str, str]]:
    """All gpu-* containers (running or stopped): the UI's add-list."""
    return [{"name": f["name"], "display": f["display"], "state": f["state"]} for f in _gpu_fleet()]


def _gpu_ip_by_name(fleet: list[dict[str, str]], ips: dict[str, str]) -> dict[str, str]:
    """Order name → edge-network IP ("" = no container row or no IP)."""
    return {f["name"]: ips.get(f["container"], "") for f in fleet}


def _llm_status_view(order: list[str]) -> tuple[dict[str, str], Optional[str]]:
    """(status per name, active name) — active = what the next request hits now."""
    status: dict[str, str] = {}
    active: Optional[str] = None
    for name in order:
        h = _llm_health.get(name)
        if h is not None and time.monotonic() - h["at"] < LLM_STALE_AFTER_S:
            status[name] = "up" if h["up"] else "down"
        else:
            status[name] = "unknown"
        # Mirror the proxy's first CANDIDATE — anything not freshly parked —
        # not just fresh-up: within a probe cycle the proxy also tries
        # unknown/stale names, so "active" should say what the next request
        # hits first (steady state: identical to the first fresh-up).
        if active is None and not _llm_fresh_down(name):
            active = name
    return status, active


async def _llm_stream(upstream: httpx.Response) -> StreamingResponse:
    # location is stripped too: a 3xx (which the LLM servers practically
    # never return) would carry an internal http://<edge-ip>:8000 Location —
    # never forward an internal address to the client.
    headers = {
        k: v
        for k, v in upstream.headers.items()
        if k.lower() not in HOP_BY_HOP and k.lower() != "location"
    }

    async def body():
        try:
            async for chunk in upstream.aiter_raw():
                yield chunk
        finally:
            await upstream.aclose()  # client disconnect → finally → close upstream

    return StreamingResponse(body(), status_code=upstream.status_code, headers=headers)


def require_forward_auth_admin(
    remote_user: Optional[str] = Header(default=None),
    remote_groups: Optional[str] = Header(default=None),
) -> Optional[str]:
    """Like require_admin minus X-Panel-Token: the fleet-open router carries
    auth-chain only (a browser can't hold the panel token, and /open must
    not be reachable WITH one off-panel). On the routed path Traefik strips
    client-supplied Remote-* headers, so their presence proves the request
    passed forward-auth. The 172.17.0.1 bind itself is reachable from any
    edge container, and there the headers are NOT stripped: a bare hit
    401s, but one that spoofs Remote-User/Remote-Groups gets the key. That
    is the price of a path with no secret gate (a browser can't hold the
    panel token) — accepted, since such an attacker already sits on the
    fleet's own network. The Authelia wildcard admits any user to the path,
    so admins-only is enforced HERE — same boundary as the panel."""
    if DEV_MODE:
        return None
    if not remote_user:
        raise HTTPException(401, "missing Remote-User (direct bind hit?)")
    if "admins" not in set(GROUP_SPLIT_RE.split(remote_groups or "")):
        raise HTTPException(403, "admins only")
    return remote_user


@app.get("/open", include_in_schema=False)
def llm_dashboard_open(_admin: Optional[str] = Depends(require_forward_auth_admin)):
    """Key bootstrap for the radiance dashboards: https://<sub>.pietrasik.top/open
    (panel Services view → the dashboard link per instance). The dashboard
    HTML is public on every instance host, but its data calls need the
    bearer key and the page only reads it from its OWN origin's localStorage
    — no URL param, and a panel page can't write cross-origin. This one-line
    page, served on the instance's own origin by this service (which holds
    LLM_API_KEY), writes it and bounces to /. no-store: the key must never
    sit in a browser cache (it lands in localStorage — exactly where typing
    it into the header field would put it)."""
    if not LLM_API_KEY:
        raise HTTPException(503, "LLM_API_KEY not configured (fleet not set up)")
    # .replace: a key containing </script> would close the inline script early
    # in the browser — \u003c decodes to the same character in the JS string.
    key_json = json.dumps(LLM_API_KEY).replace("<", "\\u003c")
    html = (
        "<!doctype html><meta charset=utf-8><title>opening radiance…</title>"
        f"<script>try{{localStorage.setItem('radiance.key',{key_json});location.replace('/')}}"
        "catch(e){document.title='radiance: key not set — open / and use the API key field in the header'}</script>"
        "<noscript>JavaScript is required. Open <code>/</code> and type the API key into the "
        "field in the header.</noscript>"
    )
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})


# ─────────────────────────── search gate (forwardAuth) ──────────────────────
# /gate/search — Traefik forwardAuth target for search.pietrasik.top.
# Answers EXACTLY 200 / 401 / 503, never 3xx: forwardAuth forwards the
# request to searxng only when the answer is in [200, 300) and copies every
# other status — 3xx included — + body to the client verbatim (a 3xx would
# arrive as a redirect the client follows). 503 means "LLM_API_KEY not
# configured" (fleet never set up) — with the key present the gate answers
# 200/401 only, so a direct probe failing on 503 is attributable (RUNBOOK →
# Web search → status table).
#
# Key reuse is an owner decision: the gate authenticates with the SAME
# LLM_API_KEY as the /v1 router (trade-offs recorded in
# docs/ARCHITECTURE.md: no search-only rotation — rotating the key re-
# creates the gpu-* containers; a leaked key grants search too). Bearer AND
# Basic (username ignored, constant-time on the key bytes) — Basic exists
# for mcp-searxng, which is Basic-only. open_metrics must never be set to
# this key (see compose/searxng/settings.yml) — the doctor's /metrics→404
# probe makes that structural.
#
# Placement: ROOT, never /api. This handler deliberately carries NO
# require_admin (it is a key checker) — under /api it would ride the
# panel-api router, whose panel-secret middleware auto-injects the token for
# whatever Authelia session arrives (admins only — and the key's own holder
# — but a machine-facing key endpoint should have no browser-routable path
# at all, and one future panel.yml edit changes that). At ROOT no router on
# the panel host forwards to it: the panel-web router matches the host but
# serves the SPA, and only PathPrefix(/api) hits panel-api — the gate is
# internet-unreachable except via search.'s forwardAuth. /open is the
# root-level include_in_schema=False precedent. The doctor pins the negative
# reachability (panel-host /gate/search must answer Authelia/panel-web, never
# gate JSON).
#
# Reads ONLY Authorization; never consults Remote-*; NOT bypassed by
# PANEL_DEV=1 (the /v1 precedent). The forwardAuth subrequest is GET by
# default (preserveRequestMethod is unset repo-wide); the all-methods
# declaration is hardening for the direct probe, not a router requirement.


def _search_key_ok(authorization: str | None) -> bool:
    """True iff authorization carries LLM_API_KEY (Bearer or Basic password)."""
    scheme, _, token = (authorization or "").partition(" ")
    if not LLM_API_KEY:
        return False
    t = token.strip()
    if scheme.lower() == "bearer":
        # .encode() like require_admin: compare_digest(str, str) raises
        # TypeError on non-ASCII — a 500 where a 401 belongs.
        return secrets.compare_digest(t.encode(), LLM_API_KEY.encode())
    if scheme.lower() == "basic":
        try:
            user_pass = base64.b64decode(t).decode()
        except Exception:
            return False
        password = user_pass.partition(":")[2]
        return secrets.compare_digest(password.encode(), LLM_API_KEY.encode())
    return False


@app.api_route(
    "/gate/search",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"],
    include_in_schema=False,
)
def search_gate(request: Request):
    if not LLM_API_KEY:
        return JSONResponse({"error": "LLM_API_KEY not configured"}, status_code=503)
    if _search_key_ok(request.headers.get("authorization")):
        return Response(status_code=200)
    return JSONResponse({"error": "invalid api key"}, status_code=401)


@app.get("/")
def root() -> dict[str, str]:
    # Direct bind only: the llm.pietrasik.top router is scoped to /v1 and
    # panel.pietrasik.top serves / from panel-web. Handy for a sanity curl.
    return {
        "service": "panel-api",
        "llm": "https://llm.pietrasik.top/v1",
        "panel": "https://panel.pietrasik.top/api (Authelia admins)",
    }


@app.api_route("/v1/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"])
async def llm_proxy(path: str, request: Request):
    # Same gate as the per-instance subdomains — the key, not Authelia.
    scheme, _, token = (request.headers.get("authorization") or "").partition(" ")
    if not (
        LLM_API_KEY
        and scheme.lower() == "bearer"
        # .encode() like require_admin: compare_digest(str, str) raises
        # TypeError on non-ASCII — a 500 where a 401 belongs.
        and secrets.compare_digest(token.strip().encode(), LLM_API_KEY.encode())
    ):
        return JSONResponse(
            {"error": {"message": "Invalid API key.", "type": "invalid_request_error", "code": "invalid_api_key"}},
            status_code=401,
        )
    # Stream-cap: stop reading at LLM_MAX_BODY instead of buffering the whole
    # upload first (a full buffer makes the cap decorative).
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > LLM_MAX_BODY:
            return JSONResponse(
                {"error": {"message": "Request body too large.", "type": "invalid_request_error", "code": "body_too_large"}},
                status_code=413,
            )
    body = bytes(body)
    headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP_BY_HOP}
    headers["accept-encoding"] = "identity"

    order = await asyncio.to_thread(_llm_load)
    ips = await asyncio.to_thread(_edge_ips)
    try:
        fleet = await asyncio.to_thread(_gpu_fleet)
    except HTTPException:
        fleet = []  # panel-do down: degrade to 503-with-reasons, not a 500
    ip_by_name = _gpu_ip_by_name(fleet, ips)
    names_in_fleet = {f["name"] for f in fleet}
    reasons = []
    for name in order:
        if _llm_fresh_down(name):
            reasons.append(f"{name}: down ({_llm_health[name]['err']})")
            continue
        if name not in names_in_fleet:
            reasons.append(f"{name}: container not found (stale order entry?)")
            continue
        ip = ip_by_name.get(name, "")
        if not ip:
            reasons.append(f"{name}: not on the edge network")
            continue
        try:
            upstream = await _llm_http.send(
                _llm_http.build_request(
                    request.method, f"http://{ip}:8000/v1/{path}", headers=headers, content=body
                ),
                stream=True,
            )
        except httpx.HTTPError as e:
            _llm_mark(name, False, type(e).__name__)
            _llm_down_until[name] = time.monotonic() + LLM_COOLDOWN_S
            reasons.append(f"{name}: {type(e).__name__}")
            continue
        if upstream.status_code >= 500:
            # The instance answered but can't serve (model loading, OOM, …):
            # a 5xx means it didn't process the request, so park it and fall
            # through to the next rather than bouncing the client.
            await upstream.aclose()
            _llm_mark(name, False, f"HTTP {upstream.status_code}")
            _llm_down_until[name] = time.monotonic() + LLM_COOLDOWN_S
            reasons.append(f"{name}: HTTP {upstream.status_code}")
            continue
        return await _llm_stream(upstream)
    msg = (
        f"No LLM instance is up — {'; '.join(reasons)}"
        if reasons
        else "No LLM instance configured (set the order in the panel Settings)"
    )
    return JSONResponse(
        {"error": {"message": msg, "type": "server_error", "code": "no_instance_up"}},
        status_code=503,
    )


class LLMSettingsBody(BaseModel):
    order: list[str] = []


@app.get("/api/settings/llm")
def llm_settings_get(_admin: Optional[str] = Depends(require_admin)) -> dict[str, Any]:
    order = _llm_load()
    status, active = _llm_status_view(order)
    return {
        "order": order,
        "status": status,
        "active": active,
        "fleet": _gpu_fleet_view(),
        "probe_interval_s": LLM_PROBE_INTERVAL_S,
    }


@app.put("/api/settings/llm")
def llm_settings_put(body: LLMSettingsBody, _admin: Optional[str] = Depends(require_admin)) -> dict[str, Any]:
    known = {f["name"] for f in _gpu_fleet_view()}
    order: list[str] = []
    for name in body.order:
        if not GPU_NAME_RE.match(name):
            raise HTTPException(400, f"bad instance name {name!r} (want gpu-<name>)")
        if name not in known:
            raise HTTPException(400, f"unknown instance {name!r} (not a gpu-* container)")
        if name not in order:
            order.append(name)
    if len(order) > LLM_MAX_ORDER:
        raise HTTPException(400, f"at most {LLM_MAX_ORDER} instances in the order")
    _llm_save(order)
    _audit(_admin, "llm-settings-put", order, 0)
    status, active = _llm_status_view(order)
    return {"order": order, "status": status, "active": active}


# ─────────────────────────── GPU pool ────────────────────────────────────────
# The GPU view (panel tab "GPUs"): which cards each fleet instance runs on,
# a pool with free/held per card, auto-grab (lowest free set, sized by the
# instance's gpu.cards label — radiance 2.x has no --tp left to read), and
# the boot-autostart list.
#
# State split: the LEDGER (gpu_pool.py / gpu-pool.json)
# is intent — panel-api + the boot oneshot write it, under the flock in
# gpu_pool.gpu_lock(). The OVERRIDE (gpu-pool-override.yml) is its compose
# projection (ROCR_VISIBLE_DEVICES per instance); every compose render that
# can create/recreate a fleet container includes it (panel-do gpu-apply / gpu-stop,
# COMPOSE_STACK in setup/common.sh) so `make update` recreates keep the
# assignment. LIVE holds come from the containers themselves (panel-do
# gpu-live) — the ledger is never consulted for "what is up".
# All privileged work below is panel-do (gpu-fleet / gpu-live / gpu-apply /
# gpu-stop); this section only computes + persists state.


def _panel_do_json(action: str, timeout: float) -> Any:
    """panel-do action → parsed JSON (500/502 on the way, like list-services)."""
    try:
        proc = _sudo_panel_do(action, [], timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise HTTPException(500, f"panel-do {action}: {e}") from e
    if proc.returncode != 0:
        raise HTTPException(500, f"{action} failed: {proc.stderr.strip()[-300:]}")
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        raise HTTPException(502, f"{action}: bad JSON: {e}") from e


def _fleet_defs() -> list[dict[str, Any]]:
    """The fleet's static half (panel-do gpu-fleet): {name, display, cards}.
    Fresh, not cached — it feeds the override PRUNE, and a stale fleet set
    would let a removed service's entry linger in the override (a phantom
    service with no image would then break every compose render)."""
    data = _panel_do_json("gpu-fleet", 25.0)
    if not isinstance(data, list):
        raise HTTPException(502, "gpu-fleet: expected a JSON array")
    return data


def _live_fleet() -> list[dict[str, Any]]:
    """The fleet's live half (panel-do gpu-live): {service, container, state,
    gpus}. Empty on failure → the pool simply shows nothing held."""
    data = _panel_do_json("gpu-live", 25.0)
    return data if isinstance(data, list) else []


def _gpu_pool_view(
    gpus: list[dict[str, Any]],
    defs: list[dict[str, Any]],
    live: list[dict[str, Any]],
    ledger: dict[str, Any],
) -> dict[str, Any]:
    """The GPU view's shape: pool (live holds + reservations) + instances
    (ledger ∪ live). `assigned` (ledger) and `holding` (live) are shown
    separately — drift between them means an apply failed or a hand-run
    compose diverged. ``gpus`` is amd_gpus(): its len is the pool size and
    its per-tile BDFs feed the reservation drift warning."""
    cards_total = len(gpus)
    bdf_by_index = {
        e["rocr_index"]: e["pci_slot"]
        for e in gpus
        if e.get("rocr_index") is not None and e.get("pci_slot")
    }
    holds = gpu_pool.live_holds(live)
    state_by_service: dict[str, str] = {}
    for row in live:
        svc, st = row.get("service"), row.get("state", "unknown")
        if not isinstance(svc, str) or not isinstance(st, str):
            continue
        # a recreated service leaves its old container in `ps -a` — prefer the
        # holding state, same rule as _gpu_fleet()
        cur = state_by_service.get(svc)
        if cur is None or (st in gpu_pool.HOLDING_STATES and cur not in gpu_pool.HOLDING_STATES):
            state_by_service[svc] = st
    instances: list[dict[str, Any]] = []
    for d in defs:
        name = d.get("name")
        if not isinstance(name, str):
            continue
        st = state_by_service.get(name, "absent")
        assigned = ledger["allocations"].get(name)
        instances.append(
            {
                "name": name,
                "display": d.get("display") or name,
                "cards_needed": int(d.get("cards") or 1),
                "assigned": sorted(assigned) if assigned else None,
                "holding": gpu_pool.holding_of(live, name) or None,
                "state": st,
                "running": st in gpu_pool.HOLDING_STATES,
                "autostart": name in ledger["autostart"],
            }
        )
    reserved = ledger["reserved"]
    return {
        "cards": cards_total,
        "pool": [
            {
                "index": c,
                "holder": holds.get(c),
                "reserved": (
                    {"for": r["for"], "note": r["note"]} if (r := reserved.get(c)) else None
                ),
            }
            for c in range(cards_total)
        ],
        "instances": instances,
        "autostart": list(ledger["autostart"]),
        "warnings": (
            gpu_pool.autostart_warnings(ledger)
            + gpu_pool.assignment_warnings(defs, ledger)
            + gpu_pool.reservation_warnings(defs, ledger, cards_total, holds, bdf_by_index)
        ),
    }


def _gpu_pool_now() -> dict[str, Any]:
    """Fresh view after a mutation (the UI polls anyway; this makes the
    response self-consistent without a second round-trip)."""
    defs = _fleet_defs()
    live = _live_fleet()
    with gpu_pool.gpu_lock():
        ledger = gpu_pool.load_ledger()
        gpu_pool.sync_override(ledger, {d["name"] for d in defs if isinstance(d.get("name"), str)})
    return _gpu_pool_view(amd_gpus(), defs, live, ledger)


def _panel_do_checked(name: str, args: list[str], user: Optional[str]) -> None:
    """Run a panel-do action; audit + raise on failure (the GPU endpoints'
    shared privileged tail — same shape as the container actions)."""
    meta = {a["name"]: a for a in panel_do_actions()}.get(name)
    timeout = min(int(meta.get("timeout_sec", 60)) + 10, 660) if meta else 330
    try:
        proc = _sudo_panel_do(name, args, timeout)
    except subprocess.TimeoutExpired:
        _audit(user, name, args, "timeout")
        raise HTTPException(504, f"{name} timed out") from None
    except OSError as e:
        _audit(user, name, args, "spawn-error")
        raise HTTPException(500, f"sudo failed: {e}") from e
    if proc.returncode != 0:
        detail = _err_detail(proc)
        _audit(user, name, args, proc.returncode, detail)
        raise HTTPException(502, f"{name} failed: {detail}")
    _audit(user, name, args, 0)
    invalidate_containers_cache()  # the fleet just changed


def _check_gpu_name(service: str) -> None:
    if not GPU_NAME_RE.match(service):
        raise HTTPException(400, f"bad instance name {service!r} (want gpu-<name>)")


def _gpus_plan_start_checked(service: str) -> tuple[dict[str, Any], list[int]]:
    """The locked plan + save + override-sync of a GPU-aware start (the
    shared core of gpus_start and its idle-stop retry). Raises ValueError
    (user-ready) on conflict, like gpu_pool.plan_start."""
    with gpu_pool.gpu_lock():
        ledger = gpu_pool.load_ledger()
        defs = _fleet_defs()
        live = _live_fleet()
        ledger, gpus = gpu_pool.plan_start(ledger, defs, live, len(amd_gpus()), service)
        gpu_pool.save_ledger(ledger)
        gpu_pool.sync_override(ledger, {d["name"] for d in defs if isinstance(d.get("name"), str)})
    return ledger, gpus


def _gen_idle_conflict_stops(
    defs: list[dict[str, Any]],
    live: list[dict[str, Any]],
    ledger: dict[str, Any],
    cards_total: int,
    service: str,
) -> list[str]:
    """The IDLE gen slots whose cards would block this start — the Gen
    section's lazy-stop rule applied to an admin's explicit start: a drained
    gen slot yields, a busy gen slot or any LLM instance never does (the
    start fails as before — the shortage named, the holders in the GPUs view)."""
    if gpu_pool.is_running(live, service):
        return []
    try:
        # The stop target is plan_start's PREFERENCE, not its fallback — the
        # saved set when it is shape-valid, even while held; the coupling to
        # plan_start's selection rule is documented in start_preference.
        target = gpu_pool.start_preference(ledger, defs, live, cards_total, service)
    except ValueError:
        return []  # not a fleet instance — nothing to stop (plan_start said so)
    holds = gpu_pool.live_holds(live)
    slots = gen_jobs.build_slots(gen_jobs.build_instances(defs, live, ledger, {}))
    jobs = gen_jobs.load_jobs()["jobs"]
    out: list[str] = []
    for c in target:
        holder = holds.get(c)
        if holder and holder in slots and not gen_jobs.flights_on_slot(jobs, holder) and holder not in out:
            out.append(holder)
    return sorted(out)


@app.get("/api/gpus")
def gpus_view(_admin: Optional[str] = Depends(require_admin)) -> dict[str, Any]:
    defs = _fleet_defs()
    live = _live_fleet()
    with gpu_pool.gpu_lock():
        # reload under the lock (a writer can't slip in between) and
        # re-render the override — self-healing prune of removed services,
        # a no-op write when already in sync.
        ledger = gpu_pool.load_ledger()
        gpu_pool.sync_override(ledger, {d["name"] for d in defs if isinstance(d.get("name"), str)})
    return _gpu_pool_view(amd_gpus(), defs, live, ledger)


class GpuAssignBody(BaseModel):
    service: str
    gpus: str = "auto"  # "auto" (lowest free set) or "0,1" (explicit)


@app.post("/api/gpus/assign")
def gpus_assign(body: GpuAssignBody, _admin: Optional[str] = Depends(require_admin)) -> dict[str, Any]:
    """Assign cards to an instance and (re)create it on them — GPU env only
    exists at container creation, so "set cards" and "run on them" are one
    operation. Running instance → brief downtime (the model reloads)."""
    _check_gpu_name(body.service)
    spec = body.gpus.strip()
    if spec != "auto":
        try:
            gpu_pool.parse_cards(spec)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
    user = _admin
    with gpu_pool.gpu_lock():
        ledger = gpu_pool.load_ledger()
        defs = _fleet_defs()
        live = _live_fleet()
        try:
            ledger, gpus = gpu_pool.allocate(ledger, defs, live, len(amd_gpus()), body.service, spec)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        gpu_pool.save_ledger(ledger)
        gpu_pool.sync_override(ledger, {d["name"] for d in defs if isinstance(d.get("name"), str)})
    _panel_do_checked("gpu-apply", [body.service], user)
    _audit(user, "gpu-assign", [body.service, spec, ",".join(map(str, gpus))], 0)
    return _gpu_pool_now()


class GpuServiceBody(BaseModel):
    service: str


@app.post("/api/gpus/start")
def gpus_start(body: GpuServiceBody, _admin: Optional[str] = Depends(require_admin)) -> dict[str, Any]:
    """GPU-aware start (also the "create + start" path for a
    container-less instance): the saved set when it is free, else the lowest
    free set — never a raw `docker start`, which would reuse the create-time
    env and could resurrect cards another instance now owns. A conflict with
    an IDLE gen slot yields to the start (that slot is stopped — it is idle,
    no job is in flight on it, nothing is lost); a busy gen slot or an LLM
    instance holds as before and the start fails with the shortage named (the
    GPUs view names the holders)."""
    _check_gpu_name(body.service)
    user = _admin
    stopped: list[str] = []
    for attempt in range(2):  # at most one idle-gen-stop + re-plan
        try:
            _ledger, gpus = _gpus_plan_start_checked(body.service)
            break
        except ValueError as e:
            # Conflict path: are the conflicting cards held ONLY by idle gen
            # slots? Then the Gen section's lazy-stop rule applies to this
            # explicit start — stop them and re-plan once. A SECOND conflict
            # (e.g. a set held by an idle gen slot AND an LLM instance) is
            # not stopped into: fail as before (the shortage is named; the
            # GPUs view names the holders).
            with gpu_pool.gpu_lock():
                ledger = gpu_pool.load_ledger()
                defs = _fleet_defs()
                live = _live_fleet()
            stops = _gen_idle_conflict_stops(defs, live, ledger, len(amd_gpus()), body.service)
            if not stops or attempt == 1:
                raise HTTPException(400, str(e)) from e
            for st in stops:
                _panel_do_checked("gpu-stop", [st], user)  # audited as the admin's start
            stopped += [s for s in stops if s not in stopped]
    _panel_do_checked("gpu-apply", [body.service], user)
    _audit(user, "gpu-start", [body.service, ",".join(map(str, gpus))] + [f"stopped-idle-gen:{s}" for s in stopped], 0)
    return _gpu_pool_now()


@app.post("/api/gpus/stop")
def gpus_stop(body: GpuServiceBody, _admin: Optional[str] = Depends(require_admin)) -> dict[str, Any]:
    """Stop (container kept — the panel starts it again; cards free on exit).
    The ledger's assignment is untouched: a later start re-uses it when free."""
    _check_gpu_name(body.service)
    _panel_do_checked("gpu-stop", [body.service], _admin)
    return _gpu_pool_now()


class GpuAutostartBody(BaseModel):
    service: str
    enabled: bool


@app.post("/api/gpus/autostart")
def gpus_autostart(body: GpuAutostartBody, _admin: Optional[str] = Depends(require_admin)) -> dict[str, Any]:
    """Toggle the boot-autostart list (applied by gpu-autostart.service on
    boot, in list order; overlaps are resolved there by re-grabbing — see
    the warnings in the response)."""
    _check_gpu_name(body.service)
    with gpu_pool.gpu_lock():
        ledger = gpu_pool.load_ledger()
        auto = ledger["autostart"]
        if body.enabled and body.service not in auto:
            auto.append(body.service)
        elif not body.enabled:
            auto[:] = [s for s in auto if s != body.service]
        ledger["autostart"] = auto
        gpu_pool.save_ledger(ledger)
    _audit(_admin, "gpu-autostart", [body.service, "on" if body.enabled else "off"], 0)
    return _gpu_pool_now()


class GpuReserveBody(BaseModel):
    """Dedicate a card to one instance (the physically-odd card — e.g. the
    PCIe5 x8 one — that must never be mixed into the others' pool): the
    auto-grab never hands it to another instance, an explicit assign of it
    by another is refused, and the owner's own grab lands on it first.
    ``for`` is an alias (reserved word)."""

    model_config = ConfigDict(populate_by_name=True)

    card: int
    for_service: str = Field(alias="for")
    note: str = ""


@app.post("/api/gpus/reserve")
def gpus_reserve(body: GpuReserveBody, _admin: Optional[str] = Depends(require_admin)) -> dict[str, Any]:
    """Reserve a card for one instance (GPU view). The BDF is captured at
    reserve time: the view WARNS when the tile's BDF later moves — a card
    add/remove renumbers the tiles, so an un-warned reservation could point
    at the wrong physical card. Re-reserving the same owner re-records it."""
    _check_gpu_name(body.for_service)
    note = body.note.strip()[:200]
    gpus = amd_gpus()
    if not (0 <= body.card < len(gpus)):
        raise HTTPException(400, f"card {body.card} is out of range (the pool is 0-{len(gpus) - 1})")
    with gpu_pool.gpu_lock():
        ledger = gpu_pool.load_ledger()
        holder = gpu_pool.live_holds(_live_fleet()).get(body.card)
        # reserving the card the owner already runs on is the natural
        # "make it official" case; a card held by ANYONE ELSE is not
        # reserved out from under it — stop the holder first.
        if holder and holder != body.for_service:
            raise HTTPException(400, f"card {body.card} is held by {holder} — stop it first")
        cur = ledger["reserved"].get(body.card)
        if cur and cur["for"] != body.for_service:
            raise HTTPException(400, f"card {body.card} is reserved for {cur['for']} — unreserve it first")
        bdf = next(
            (e["pci_slot"] for e in gpus if e.get("rocr_index") == body.card and e.get("pci_slot")),
            None,
        )
        ledger["reserved"][body.card] = {"for": body.for_service, "note": note, "bdf": bdf}
        gpu_pool.save_ledger(ledger)
    _audit(_admin, "gpu-reserve", [str(body.card), body.for_service, note], 0)
    return _gpu_pool_now()


class GpuUnreserveBody(BaseModel):
    card: int


@app.post("/api/gpus/unreserve")
def gpus_unreserve(body: GpuUnreserveBody, _admin: Optional[str] = Depends(require_admin)) -> dict[str, Any]:
    """Remove a reservation — the card rejoins the auto-grab pool for everyone."""
    with gpu_pool.gpu_lock():
        ledger = gpu_pool.load_ledger()
        cur = ledger["reserved"].get(body.card)
        if cur is None:
            raise HTTPException(400, f"card {body.card} is not reserved")
        del ledger["reserved"][body.card]
        gpu_pool.save_ledger(ledger)
    _audit(_admin, "gpu-unreserve", [str(body.card), cur["for"]], 0)
    return _gpu_pool_now()


# ───────────────────────────── Gen (image/video) ─────────────────────────────
# The panel's own generation section (panel tab "Gen"): one FIFO of jobs,
# served by the GEN SLOTS — gpu-* instances whose radiance config hosts an
# image/video model (gen_jobs.py discovers them from the deployed configs;
# they are the gpu.private instances: no router, reachable by container IP
# only, like the LLM router talks to the fleet). Each slot is gen-ONLY: its
# gen model is the sole config entry and stays resident — prompt ENHANCEMENT
# runs off-slot, on the settings' enhance target (default: the gpu-writer
# instance's "writer" model — a PUBLIC chat instance on the 7th card; the
# engine reaches it by container IP over the edge network, exactly like the
# slots, and starts it via the same panel-do gpu-apply when needed).
#
# Internal-only by construction for the SLOTS: they have no public host
# (make doctor asserts it), and every endpoint here rides the same admin
# gate as the rest of the panel. V2 exposes the SAME job API to the servitor
# agent on /machine/gen/* behind the LLM bearer key — no second queue.
#
# The engine is one background task (a ~2 s tick, the single scheduler) plus
# one asyncio task per in-flight job. Every state transition goes through
# gen_jobs (flock + atomic file), so a panel-api restart loses nothing: a
# video with a radiance id re-attaches to its poll (radiance kept the job);
# a pre-request flight (starting/enhancing/loading — the gen request never
# reached the slot) is re-queued and resumes; only a RUNNING request in
# flight (an image, or a video whose id was never recorded) fails.
#
# Radiance contract (upstream, ref/radiance): images — synchronous
# POST /v1/images/generations, response_format b64_json ONLY (url is refused);
# videos — POST /v1/videos → {id, status}, GET /v1/videos/{id} →
# status(queued|in_progress|completed|failed)+progress(0-100),
# GET /v1/videos/{id}/content → the MP4 (?variant=thumbnail → first frame),
# DELETE /v1/videos/{id} → cancel. Cancel has a second, connection-level path:
# an ABNORMALLY CLOSED response sets the ticket's cancel flag (upstream's
# on_close handler — the same flag the video DELETE sets), so dropping an
# in-flight request retires the render at the next pass boundary;
# _gen_await_request uses this for the synchronous calls.
# Auth: Bearer LLM_API_KEY — the slots' and the writer's RADIANCE_API_KEY is
# the same fleet key (one rotation, zero plumbing).
#
# Admission (two caps — see gen_jobs): per slot, at most GEN_CAP jobs in
# the GEN PHASE (one in progress + one queued in radiance — the model's own
# sequencing; a starting flight consumes no headroom); system-wide, at most
# GEN_ENHANCE_CAP enhancements in flight on the enhance target. Enhancement
# runs BEFORE the slot phase and OFF-SLOT (a different card): a queued job
# enhances as soon as there is enhance headroom, without a slot being
# claimed or started.

GEN_ARTIFACT_DIR = Path(
    os.environ.get("GEN_ARTIFACT_DIR")
    or ("/models/gen-output" if not DEV_MODE else "gen-output")
)
GEN_GC_INTERVAL_S = 6 * 3600  # artifact age-GC pass (the age itself: gc_days)
GEN_SLOT_START_TIMEOUT_S = 600.0  # container start + engine boot budget per job
GEN_VIDEO_POLL_S = 5.0
GEN_CANCEL_POLL_S = 2.0  # how fast an in-flight radiance request notices a cancel (drop → the slot's ticket cancel)
# Request budgets vs the slot's LOAD: radiance's /health is 200 from PROCESS
# start (a stuck device is the only 503), so a request to a freshly
# (re)started slot can sit out its entry load — warm-up is still loading from
# disk and the request is held until its entry is resident, up to the slot's
# scheduler.load-timeout (300 s on the tp1 slots, 600 s on gpu-gentp2, the
# fleet's largest). Every budget below is that load wait + headroom, never a
# cap on the work itself (the image call's 1800 s is load + a 30+ min render).
GEN_SLOT_LOAD_WAIT_S = 600.0  # the fleet's largest scheduler.load-timeout (gpu-gentp2)
GEN_ENHANCE_TIMEOUT_S = GEN_SLOT_LOAD_WAIT_S + 300.0
GEN_VIDEO_SUBMIT_TIMEOUT_S = GEN_SLOT_LOAD_WAIT_S + 300.0

_gen_http: Optional[httpx.AsyncClient] = None  # created in lifespan
_gen_tasks: dict[str, asyncio.Task] = {}  # job id → in-flight task
_gen_gc_at: float = 0.0
_gen_dev_logged: bool = False


class _GenError(Exception):
    """User-ready job failure — the message lands in the job record."""


class _JobCanceled(Exception):
    """The job was canceled (or deleted) while its radiance request was in
    flight — the request task was dropped. Not a failure: the record is
    already canceled (terminal-sticky), and the video tail — the only caller
    that holds a radiance id to retire — does its own cleanup."""


def _gen_auth() -> dict[str, str]:
    return {"authorization": f"Bearer {LLM_API_KEY}"} if LLM_API_KEY else {}


def _gen_world() -> dict[str, Any]:
    """defs + live + ledger + ips + instances + slots + jobs + holds +
    per-slot status + the enhance target, in one pass. Sync (panel-do
    spawns) — endpoints run in the threadpool, the engine awaits it via
    asyncio.to_thread.

    DEV_MODE has no panel-do: defs come from the repo's compose file (the
    same gpu.cards/labels panel-do gpu-fleet reads on the server), live and
    ips are empty — slots then show down/unreachable, and the engine stays
    off, so the endpoints + state files are fully exercisable locally."""
    if DEV_MODE:
        defs, live, ips = _fleet_defs_dev(), [], {}
    else:
        defs = _fleet_defs()
        live = _live_fleet()
        ips = _edge_ips()
    with gpu_pool.gpu_lock():
        ledger = gpu_pool.load_ledger()
    instances = gen_jobs.build_instances(defs, live, ledger, ips)
    slots = gen_jobs.build_slots(instances)
    store = gen_jobs.load_jobs()
    holds = gpu_pool.live_holds(live)
    return {
        "defs": defs,
        "live": live,
        "ledger": ledger,
        "ips": ips,
        "instances": instances,
        "slots": slots,
        "store": store,
        "jobs": store["jobs"],
        "holds": holds,
        "status": gen_jobs.slot_status(slots, store["jobs"], holds),
        "enhance_target": _gen_enhance_target(gen_jobs.load_settings(), instances),
    }


def _gen_enhance_target(settings: dict[str, Any], instances: dict[str, Any]) -> dict[str, Any]:
    """The settings' enhance target resolved against the current fleet: the
    (instance, model) the engine runs prompts through (default: the
    gpu-writer instance's "writer" entry). ``ok`` False carries a user-ready
    reason — create 400s on it, and a queued enhance job rides it as its
    structural wait reason (a config drift, like a vanished model)."""
    e = settings.get("enhance") or {}
    inst = e.get("instance") or "gpu-writer"
    model = e.get("model") or "writer"
    out: dict[str, Any] = {"instance": inst, "model": model, "running": False, "ip": "", "ok": False, "reason": ""}
    row = instances.get(inst)
    if row is None:
        out["reason"] = f"enhance target {inst!r} is not a deployed gpu-* instance (Gen settings)"
        return out
    out["running"], out["ip"] = row["running"], row["ip"]
    if model not in row["llms"]:
        hosted = ", ".join(row["llms"]) or "none"
        out["reason"] = f"{inst} hosts no llm model {model!r} (its llm entries: {hosted} — Gen settings)"
        return out
    out["ok"] = True
    return out


def _fleet_defs_dev() -> list[dict[str, Any]]:
    """DEV_MODE fleet defs: the repo's compose gpu-* blocks (name +
    gpu.cards + panel.manage) — the local stand-in for panel-do gpu-fleet,
    which needs the server's sudo."""
    import re as _re

    try:
        text = Path("compose/docker-compose.yml").read_text()
    except OSError:
        return []
    out: list[dict[str, Any]] = []
    for m in _re.finditer(r"^  (gpu-[a-z0-9][a-z0-9-]*):\n((?:^    .*(?:\n|$))*)", text, _re.M):
        name, block = m.group(1), m.group(2)
        cards = _re.search(r"gpu\.cards=(\d+)", block)
        manage = _re.search(r"panel\.manage=([^\n#]+)", block)
        out.append(
            {
                "name": name,
                "cards": int(cards.group(1)) if cards else 1,
                "display": manage.group(1).strip() if manage else name,
            }
        )
    return out


async def _gen_mark(jid: str, **fields: Any) -> Optional[dict[str, Any]]:
    """Set fields on a job. Terminal states are sticky — a late task can't
    overwrite canceled/completed/failed (a cancel racing a finish keeps the
    cancel only if it landed first; the UI polls either way). Async: the
    state file is sync file IO — the engine's tasks never touch it on the
    event loop (the endpoints do, running in the threadpool)."""

    def _m(job: dict[str, Any]) -> None:
        if job["state"] in gen_jobs.TERMINAL_STATES:
            return
        job.update(fields)

    return await asyncio.to_thread(gen_jobs.update_job, jid, _m)


def _gen_requeue(jid: str, reason: str) -> None:
    """Back to QUEUED without its slot claim — the slot start conflicted (a
    card got held between the tick's claim and this attempt), which is a
    lane condition, not a job fault: the next tick re-evaluates (the slot's
    status then says blocked — wait for the holder — or the lane is free).
    A permanent conflict (docker down, cards short) re-queues with the same
    reason every tick; the reason rides the record's error field so the job
    row says why it waits. Terminal-sticky like _gen_mark (a cancel that
    landed first wins; the record stays)."""

    def _m(job: dict[str, Any]) -> None:
        if job["state"] in gen_jobs.TERMINAL_STATES:
            return
        job["state"] = gen_jobs.QUEUED
        job["slot"] = None  # the next tick re-picks a candidate (another slot may host the model)
        job["started_at"] = None
        job["error"] = reason

    gen_jobs.update_job(jid, _m)


def _gen_reap_stale() -> None:
    """Startup: no in-flight tasks exist in this process. A RUNNING job with
    a radiance id survives (the tick re-attaches its poll — radiance kept
    the video); the pre-request in-flight states (STARTING/ENHANCING/
    LOADING — the gen request never reached the slot) are lossless to re-run
    → back to QUEUED, slot claim released (the slot start is idempotent, the
    enhancement simply runs again); only a RUNNING request that was in flight
    in the dead process (an image, or a video whose submit had not recorded
    its id — an orphan on the slot) → failed, with a say-so."""
    now = time.time()
    with gen_jobs.gen_lock():
        store = gen_jobs.load_jobs()
        changed = False
        for job in store["jobs"].values():
            state = job.get("state")
            if state not in gen_jobs.IN_FLIGHT_STATES:
                continue
            if state == gen_jobs.RUNNING:
                if job.get("radiance_video_id"):
                    continue  # the tick re-attaches its poll (radiance kept the video)
                job["state"] = gen_jobs.FAILED
                job["error"] = "panel-api restarted mid-job"
                job["finished_at"] = now
            else:
                # STARTING / ENHANCING / LOADING: nothing irrevocable was
                # sent (the enhance is off-slot and simply runs again; the
                # gen request never reached the slot) — re-run it, don't
                # fail it.
                job["state"] = gen_jobs.QUEUED
                job["slot"] = None  # the next tick re-picks a candidate
                job["started_at"] = None
                job["error"] = "panel-api restarted before the gen request — re-queued"
            changed = True
        if changed:
            gen_jobs.save_jobs(store)


# ── instance start (engine-driven, the GPU view's start without the admin) ──


def _gen_start_instance(service: str) -> None:
    """GPU-aware start of ANY gpu-* instance the engine needs — a gen slot
    (its lane cards) or the enhance target (its own card): the saved set
    when free, else the lowest free — then gpu-apply (idempotent: a no-op
    on an already-up container, so the admin/engine race can't
    double-start). Raises RuntimeError (user-ready) on conflict — the job
    records it."""
    try:
        _gpus_plan_start_checked(service)
    except ValueError as e:
        raise RuntimeError(str(e)) from e
    try:
        _panel_do_checked("gpu-apply", [service], "gen-engine")  # audited as the engine
    except HTTPException as e:
        raise RuntimeError(str(e.detail)) from e


def _gen_instance_up(name: str) -> tuple[bool, str]:
    """(running, edge IP) for ONE instance — the health-wait's poll. gpu-live
    (one panel-do spawn) + the cached edge-ips, not the full world (fleet
    defs + every slot's config): a cold-load wait polls this every 3 s, and
    N concurrent waiters would otherwise N× the tick's two-spawn world
    build."""
    row = next(
        (
            r
            for r in _live_fleet()
            if r.get("service") == name and r.get("state") in gpu_pool.HOLDING_STATES
        ),
        None,
    )
    if row is None:
        return False, ""
    return True, _edge_ips().get(row.get("container") or "", "")


async def _gen_wait_instance_health(name: str, jid: str, budget_s: float) -> str:
    """Poll a fleet instance's /health until up; returns its edge IP.
    RuntimeError (user-ready) on budget exhaustion — the job fails with it.
    Returns "" when the job left in-flight while waiting
    (canceled/deleted) — the caller re-checks the record and drops out; a
    canceled job shouldn't pay out the full 10 min for an instance that no
    longer needs to be up."""
    deadline = time.monotonic() + budget_s
    last = "waiting for the container"
    while time.monotonic() < deadline:
        running, ip = await asyncio.to_thread(_gen_instance_up, name)
        job = await asyncio.to_thread(gen_jobs.get_job, jid)
        if job is None or job["state"] in gen_jobs.TERMINAL_STATES:
            return ""
        if running and ip:
            try:
                r = await _gen_http.get(f"http://{ip}:8000/health", timeout=5.0)
                if r.status_code == 200:
                    return ip
                last = f"/health → {r.status_code}"
            except httpx.HTTPError as e:
                last = type(e).__name__
        await asyncio.sleep(3.0)
    raise RuntimeError(f"{name} did not become healthy in {budget_s:.0f}s ({last})")


async def _gen_free_slot(sname: str) -> bool:
    """Stop the IDLE gen slots holding ``sname``'s lane cards (the lazy-stop
    rule in action — the holder is idle, nothing in flight is lost) and
    re-verify the slot is then startable. The stop is the engine's one
    preemption, and it only ever touches gen slots with no in-flight job —
    re-checked against a FRESH read at stop time, because the caller's tick
    world is a snapshot taken before this tick's own claims: an earlier claim
    in the same tick may have just landed a job on a holder that looked idle,
    and stopping it would kill that job (the admin-start path does the same
    fresh check).
    """
    w = await asyncio.to_thread(_gen_world)
    st = w["status"].get(sname)
    if st is None:
        return False
    if st["state"] == "down-idle-stops":
        for holder in st["stops"]:
            if gen_jobs.flights_on_slot((await asyncio.to_thread(gen_jobs.load_jobs))["jobs"], holder):
                return False  # busy now — the next tick picks it up once drained
            try:
                await asyncio.to_thread(_panel_do_checked, "gpu-stop", [holder], "gen-engine")
            except HTTPException:
                return False  # failed — the next tick retries
    # a fresh world again: the stops (or an admin) may have moved the lane
    w2 = await asyncio.to_thread(_gen_world)
    row = w2["slots"].get(sname)
    st2 = w2["status"].get(sname)
    if row is None or st2 is None:
        return False
    return bool(row["running"] and row["ip"]) or st2["state"] == "down"


# ── radiance calls (the slot's HTTP surface, bearer LLM_API_KEY) ──


async def _gen_await_request(coro, jid: str) -> Any:
    """Await a long radiance request, dropping it when the job is canceled or
    deleted in the meantime (re-checked every GEN_CANCEL_POLL_S — the record
    read hops to a thread, like the poll loops). The drop closes the connection, and
    radiance treats an abnormally closed response as a cancel (the ticket's
    flag), so the slot retires the render at its next pass boundary instead of
    burning it out. Raises _JobCanceled on the drop; the request's own errors
    propagate unchanged (the caller's terminal-sticky mark makes them no-ops
    after a racing cancel).

    NOT used for the video submit: the tail's poll loop DELETEs the radiance
    side within GEN_VIDEO_POLL_S of a cancel — dropping the submit itself
    would orphan a video radiance registered before the connection dropped
    (its id would never be known). A canceled submit simply lands, records
    its id, and the next poll tick retires it (its timeout carries the same
    cold-load wait as the other budgets — it is quick once the entry is
    resident)."""
    task = asyncio.ensure_future(coro)
    try:
        while True:
            await asyncio.wait({task}, timeout=GEN_CANCEL_POLL_S)
            if task.done():
                return task.result()
            job = await asyncio.to_thread(gen_jobs.get_job, jid)
            if job is None or job["state"] == gen_jobs.CANCELED:
                break  # dropped below; the raise after the finally is the drop's exit
    finally:
        if not task.done():
            task.cancel()
            try:
                await task  # let the connection teardown finish (no "destroyed pending" tasks)
            except BaseException:  # noqa: B902 — the drop (or our own cancel) IS the point
                pass
        elif not task.cancelled():
            task.exception()  # the done-with-error race: retrieve, so it isn't reported un-retrieved
    raise _JobCanceled()


async def _gen_enhance_call(base: str, model: str, user_prompt: str, instruction: str) -> str:
    """One non-streaming chat completion against the enhance target (the
    settings' instance + model — default: the writer's "writer" entry). The
    per-gen-model ``instruction`` is the admin's enhancement prompt (Gen
    settings); the result replaces the user prompt for the gen request."""
    if not model:
        raise _GenError("no enhance model configured (Gen settings)")
    r = await _gen_http.post(
        f"{base}/v1/chat/completions",
        headers=_gen_auth(),
        json={
            "model": model,
            "messages": [
                {"role": "system", "content": instruction},
                {"role": "user", "content": user_prompt},
            ],
        },
        timeout=GEN_ENHANCE_TIMEOUT_S,
    )
    if r.status_code >= 400:
        raise _GenError(f"enhancement failed: HTTP {r.status_code}: {r.text[:300]}")
    try:
        out = r.json()["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError, ValueError):
        raise _GenError("enhancement: unexpected response shape") from None
    if not out:
        raise _GenError("enhancement: empty response")
    return out


async def _gen_image_call(base: str, job: dict[str, Any], final_prompt: str) -> bytes:
    """The synchronous image POST — b64_json only (radiance refuses url)."""
    body: dict[str, Any] = {
        "model": job["model"],
        "prompt": final_prompt,
        "response_format": "b64_json",
    }
    if job["params"].get("size"):
        body["size"] = job["params"]["size"]
    if job["params"].get("seed") is not None:
        body["seed"] = job["params"]["seed"]
    r = await _gen_http.post(f"{base}/v1/images/generations", headers=_gen_auth(), json=body, timeout=1800.0)
    if r.status_code >= 400:
        raise _GenError(f"image generation failed: HTTP {r.status_code}: {r.text[:300]}")
    try:
        b64 = r.json()["data"][0]["b64_json"]
    except (KeyError, IndexError, ValueError):
        raise _GenError("image generation: unexpected response shape") from None
    try:
        return base64.b64decode(b64)
    except Exception as e:  # noqa: BLE001
        raise _GenError(f"image generation: bad b64 payload ({type(e).__name__})") from e


async def _gen_video_submit(base: str, job: dict[str, Any], final_prompt: str) -> str:
    body: dict[str, Any] = {"model": job["model"], "prompt": final_prompt}
    p = job["params"]
    if p.get("seconds") is not None:
        body["seconds"] = str(p["seconds"])
    if p.get("size"):
        body["size"] = p["size"]
    if p.get("seed") is not None:
        body["seed"] = p["seed"]
    r = await _gen_http.post(f"{base}/v1/videos", headers=_gen_auth(), json=body, timeout=GEN_VIDEO_SUBMIT_TIMEOUT_S)
    if r.status_code >= 400:
        raise _GenError(f"video submit failed: HTTP {r.status_code}: {r.text[:300]}")
    try:
        return r.json()["id"]
    except (KeyError, ValueError):
        raise _GenError("video submit: unexpected response shape") from None


async def _gen_video_status(base: str, vid: str) -> dict[str, Any]:
    r = await _gen_http.get(f"{base}/v1/videos/{vid}", headers=_gen_auth(), timeout=30.0)
    if r.status_code >= 400:
        why = "radiance lost the job (slot restarted?)" if r.status_code == 404 else f"HTTP {r.status_code}"
        raise _GenError(f"video poll: {why}")
    d = r.json()
    return {"status": str(d.get("status", "unknown")), "progress": d.get("progress"), "error": d.get("error")}


async def _gen_video_content(base: str, vid: str) -> tuple[bytes, Optional[bytes]]:
    """(MP4, thumbnail) — the MP4 is required, the thumbnail best-effort
    (a failed thumb never fails the job)."""
    r = await _gen_http.get(f"{base}/v1/videos/{vid}/content", headers=_gen_auth(), timeout=1800.0)
    if r.status_code >= 400:
        raise _GenError(f"video download: HTTP {r.status_code}")
    mp4 = r.content
    thumb: Optional[bytes] = None
    try:
        t = await _gen_http.get(
            f"{base}/v1/videos/{vid}/content?variant=thumbnail", headers=_gen_auth(), timeout=60.0
        )
        if t.status_code == 200:
            thumb = t.content
    except httpx.HTTPError:
        pass
    return mp4, thumb


async def _gen_video_cancel(base: str, vid: str) -> None:
    try:
        await _gen_http.delete(f"{base}/v1/videos/{vid}", headers=_gen_auth(), timeout=30.0)
    except httpx.HTTPError:
        pass  # best effort — the slot may already be gone


def _gen_write_artifact(job: dict[str, Any], data: bytes, thumb: Optional[bytes]) -> dict[str, Any]:
    """Write under GEN_ARTIFACT_DIR (this host service's artifact dir, user
    panel; the same dir is ro on panel-web at /gen — the UI's static URLs).
    Sync (file IO) — callers await it via asyncio.to_thread. Files are pinned
    0644: panel-web's nginx reads them as a NON-panel user, so the mode must
    not ride panel's umask (a 077 umask would 403 every artifact in the UI)."""

    def put(path: Path, payload: bytes) -> None:
        path.write_bytes(payload)
        path.chmod(0o644)

    GEN_ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    if job["kind"] == "video":
        put(GEN_ARTIFACT_DIR / f"{job['id']}.mp4", data)
        art: dict[str, Any] = {"url": f"/gen/{job['id']}.mp4", "mime": "video/mp4", "size": len(data), "thumb": None}
        if thumb:
            put(GEN_ARTIFACT_DIR / f"{job['id']}-thumb.webp", thumb)
            art["thumb"] = f"/gen/{job['id']}-thumb.webp"
    else:
        ext, mime = gen_jobs.sniff(data)
        put(GEN_ARTIFACT_DIR / f"{job['id']}{ext}", data)
        art = {"url": f"/gen/{job['id']}{ext}", "mime": mime, "size": len(data), "thumb": None}
    return art


def _gen_delete_artifacts(job: dict[str, Any]) -> None:
    art = job.get("artifact") or {}
    for key in ("url", "thumb"):
        u = art.get(key)
        if not u:
            continue
        try:
            (GEN_ARTIFACT_DIR / u.rsplit("/", 1)[-1]).unlink()
        except OSError:
            pass


# ── the job runner (one asyncio task per in-flight job) ──


async def _gen_video_tail(jid: str, base: str, final_prompt: str) -> None:
    """Submit (if not done) → poll → download → complete. Also the re-attach
    path after a restart (radiance_video_id already set → straight to the
    poll)."""
    job = await asyncio.to_thread(gen_jobs.get_job, jid)
    if job is None:
        return
    vid = job.get("radiance_video_id")
    if not vid:
        vid = await _gen_video_submit(base, job, final_prompt)
        await _gen_mark(jid, radiance_video_id=vid)
    while True:
        job_now = await asyncio.to_thread(gen_jobs.get_job, jid)
        if job_now is None or job_now["state"] == gen_jobs.CANCELED:
            await _gen_video_cancel(base, vid)
            return
        try:
            v = await _gen_video_status(base, vid)
        except _GenError as e:
            await _gen_mark(jid, state=gen_jobs.FAILED, error=str(e)[:500], finished_at=time.time())
            return
        except httpx.HTTPError as e:
            # the slot itself died (connection refused/reset): the video lived
            # in radiance's memory — it's gone, and the job says so plainly
            await _gen_mark(
                jid,
                state=gen_jobs.FAILED,
                error=f"lost the slot mid-render ({type(e).__name__}) — the video was lost",
                finished_at=time.time(),
            )
            return
        if v["status"] == "completed":
            break
        if v["status"] == "failed":
            await _gen_mark(
                jid,
                state=gen_jobs.FAILED,
                error=f"render failed: {v.get('error') or 'radiance reported failure'}"[:500],
                finished_at=time.time(),
            )
            return
        if isinstance(v["progress"], (int, float)):
            await _gen_mark(jid, progress=v["progress"])
        await asyncio.sleep(GEN_VIDEO_POLL_S)
    try:
        mp4, thumb = await _gen_await_request(_gen_video_content(base, vid), jid)
    except _JobCanceled:
        await _gen_video_cancel(base, vid)  # the render is done — retire it from radiance's store
        return
    job_now = await asyncio.to_thread(gen_jobs.get_job, jid)
    if job_now is None or job_now["state"] == gen_jobs.CANCELED:
        await _gen_video_cancel(base, vid)
        return
    art = await asyncio.to_thread(_gen_write_artifact, job_now, mp4, thumb)
    await _gen_mark(jid, state=gen_jobs.COMPLETED, artifact=art, finished_at=time.time())


async def _gen_enhance_phase(jid: str) -> None:
    """One job's ENHANCEMENT — off-slot, on the settings' enhance target:
    bring the target up if needed (part of this phase) → the rewrite → back
    to QUEUED with enhanced_prompt set (the gen phase then claims its slot
    in the next tick, in the job's own FIFO position). No slot is claimed or
    started: the target is a different card, so the enhancement neither
    waits for the lane nor holds one. A target lane condition (its card
    held, docker down) or a settings drift re-queues with the reason (the
    tick's structural check marks it again until fixed); a cancel while here
    is already terminal (the marks below are no-ops)."""
    try:
        job = await asyncio.to_thread(gen_jobs.get_job, jid)
        if job is None or job["state"] != gen_jobs.ENHANCING:
            return
        settings = await asyncio.to_thread(gen_jobs.load_settings)
        instruction = (settings["enhance"].get("prompts") or {}).get(job["model"], "")
        if not instruction:
            # The door refuses it at create; this is a settings drift since
            # the claim — re-queue (the tick's structural check marks the
            # reason again until the prompt is back).
            await asyncio.to_thread(
                _gen_requeue, jid, f"no enhancement prompt configured for {job['model']!r} (Gen settings)"
            )
            return
        w = await asyncio.to_thread(_gen_world)
        et = w["enhance_target"]
        if not et["ok"]:
            # Structural (the target drifted out of the fleet since the
            # claim) — re-queue with the reason; the tick's structural
            # check marks it again until the settings are fixed.
            await asyncio.to_thread(_gen_requeue, jid, f"enhance target unavailable: {et['reason']}")
            return
        if not (et["running"] and et["ip"]):
            try:
                await asyncio.to_thread(_gen_start_instance, et["instance"])
            except RuntimeError as e:
                # Its card is held / docker down — a lane condition, not a
                # job fault: re-queue; the next tick retries.
                await asyncio.to_thread(_gen_requeue, jid, f"waiting for the enhance target: {e}")
                return
        et_ip = await _gen_wait_instance_health(et["instance"], jid, GEN_SLOT_START_TIMEOUT_S)
        j = await asyncio.to_thread(gen_jobs.get_job, jid)
        if j is None or j["state"] in gen_jobs.TERMINAL_STATES:
            return  # canceled while the target came up — nothing to send
        if not et_ip:
            return
        final_prompt = await _gen_await_request(
            _gen_enhance_call(f"http://{et_ip}:8000", et["model"], job["prompt"], instruction), jid
        )

        def _m(job2: dict[str, Any]) -> None:
            if job2["state"] in gen_jobs.TERMINAL_STATES:
                raise RuntimeError("terminal")
            job2["state"] = gen_jobs.QUEUED
            job2["slot"] = None  # the gen phase re-picks its slot
            job2["enhanced_prompt"] = final_prompt
            job2["error"] = None  # any wait reason (a re-queue, a structural mark) is stale now

        try:
            await asyncio.to_thread(gen_jobs.update_job, jid, _m)
        except RuntimeError:
            return  # went terminal (a racing cancel) — the prompt is moot
    except _JobCanceled:
        return  # the record is already canceled — the dropped call is the point
    except _GenError as e:
        await _gen_mark(jid, state=gen_jobs.FAILED, error=str(e)[:500], finished_at=time.time())
    except Exception as e:  # noqa: BLE001 — a job failure, never a loop crash
        await _gen_mark(jid, state=gen_jobs.FAILED, error=f"internal: {type(e).__name__}: {e}"[:500], finished_at=time.time())
    finally:
        _gen_tasks.pop(jid, None)


async def _gen_run_job(jid: str, slot: str) -> None:
    """One job's gen-phase flight on ``slot``: (re)start the slot if needed
    → the gen request (with the enhanced prompt, if the enhance phase ran
    it — that ran OFF-SLOT, on the settings' enhance target, BEFORE this
    phase) → the artifact. State is durable (gen_jobs); a failure lands in
    the record, never in this task's exit."""
    try:
        job = await asyncio.to_thread(gen_jobs.get_job, jid)
        if job is None:
            return
        # 1. the slot up + healthy (it may have come up since the claim — an
        #    admin start — in which case the start below is a no-op)
        w = await asyncio.to_thread(_gen_world)
        row = w["slots"].get(slot) or {}
        if not (row.get("running") and row.get("ip")):
            try:
                await asyncio.to_thread(_gen_start_instance, slot)
            except RuntimeError as e:
                w = await asyncio.to_thread(_gen_world)
                row = w["slots"].get(slot) or {}
                if not (row.get("running") and row.get("ip")):
                    # Start conflict (a card got held between the tick's claim
                    # and this attempt, or the lane is short) — not a job
                    # fault: re-queue it. The next tick re-evaluates through
                    # the slot status (blocked → wait; idle holder → it gets
                    # stopped; free → it starts). A transient docker failure
                    # rides the same path and simply retries.
                    await asyncio.to_thread(_gen_requeue, jid, f"waiting for the lane: {e}")
                    return
        base_ip = await _gen_wait_instance_health(slot, jid, GEN_SLOT_START_TIMEOUT_S)
        if (j := await asyncio.to_thread(gen_jobs.get_job, jid)) is None or j["state"] in gen_jobs.TERMINAL_STATES:
            return  # canceled while the slot came up — no request to send
        if not base_ip:
            return
        base = f"http://{base_ip}:8000"
        await _gen_mark(jid, error=None)  # a re-queue's wait reason is stale now
        # 2. the prompt — the enhanced one, if the enhance phase ran it
        #    (off-slot, on the settings' enhance target, BEFORE this phase:
        #    a different card, so neither phase waited on the other).
        final_prompt = job.get("enhanced_prompt") or job["prompt"]
        # 3. gen-phase admission (the per-slot cap on the GEN PHASE — a
        #    second gen flight on the same slot is admitted and gens in
        #    parallel; radiance sequences the model's work)
        while True:
            j = await asyncio.to_thread(gen_jobs.get_job, jid)
            if j is None or j["state"] == gen_jobs.CANCELED:
                return
            ok, _why = gen_jobs.admission((await asyncio.to_thread(gen_jobs.load_jobs))["jobs"], slot, jid)
            if ok:
                break
            if j["state"] != gen_jobs.LOADING:
                await _gen_mark(jid, state=gen_jobs.LOADING)
            await asyncio.sleep(2.0)
        _started = (await asyncio.to_thread(gen_jobs.get_job, jid) or {}).get("started_at")
        await _gen_mark(jid, state=gen_jobs.RUNNING, started_at=_started or time.time())
        # 4. the request + the artifact
        if job["kind"] == "image":
            try:
                data = await _gen_await_request(_gen_image_call(base, job, final_prompt), jid)
            except httpx.HTTPError as e:
                # the slot itself died (connection refused/reset): the image
                # lived in radiance's memory — it's gone, and the job says so
                # plainly (the video tail's poll has its own copy of this)
                raise _GenError(f"lost the slot mid-render ({type(e).__name__}) — the image was lost") from e
            if (j := await asyncio.to_thread(gen_jobs.get_job, jid)) is None or j["state"] == gen_jobs.CANCELED:
                return
            art = await asyncio.to_thread(_gen_write_artifact, j, data, None)
            await _gen_mark(jid, state=gen_jobs.COMPLETED, artifact=art, finished_at=time.time())
        else:
            await _gen_video_tail(jid, base, final_prompt)
    except _JobCanceled:
        return  # the record is already canceled — nothing to mark, no artifact to drop
    except _GenError as e:
        await _gen_mark(jid, state=gen_jobs.FAILED, error=str(e)[:500], finished_at=time.time())
    except Exception as e:  # noqa: BLE001 — a job failure, never a loop crash
        await _gen_mark(jid, state=gen_jobs.FAILED, error=f"internal: {type(e).__name__}: {e}"[:500], finished_at=time.time())
    finally:
        _gen_tasks.pop(jid, None)


async def _gen_resume_video(jid: str) -> None:
    """Re-attach to a video poll after a panel restart (radiance kept the
    job — its id is in the record). The slot must still be up: if it went
    down, the in-memory video is gone and the job says so."""
    try:
        job = await asyncio.to_thread(gen_jobs.get_job, jid)
        if job is None or not job.get("radiance_video_id"):
            return
        slot = job.get("slot") or ""
        w = await asyncio.to_thread(_gen_world)
        row = w["slots"].get(slot) or {}
        if not (row.get("running") and row.get("ip")):
            await _gen_mark(
                jid,
                state=gen_jobs.FAILED,
                error="panel-api restarted and the slot is down — the in-progress video was lost",
                finished_at=time.time(),
            )
            return
        await _gen_video_tail(jid, f"http://{row['ip']}:8000", job.get("enhanced_prompt") or job["prompt"])
    except Exception as e:  # noqa: BLE001
        await _gen_mark(jid, state=gen_jobs.FAILED, error=f"internal: {type(e).__name__}: {e}"[:500], finished_at=time.time())
    finally:
        _gen_tasks.pop(jid, None)


# ── the engine (the single scheduler) ──


async def _gen_tick() -> None:
    w = await asyncio.to_thread(_gen_world)
    slots, status = w["slots"], w["status"]
    # 1. re-attach orphaned video polls (a restart kept the jobs; radiance
    #    kept the videos)
    for jid, job in w["jobs"].items():
        if job.get("state") == gen_jobs.RUNNING and job.get("radiance_video_id") and jid not in _gen_tasks:
            _gen_tasks[jid] = asyncio.create_task(_gen_resume_video(jid))
    # 2. claim queued jobs (FIFO — the order list is oldest first). The
    # state view is reloaded PER JOB: a claim made earlier in this tick must
    # be visible to the next job's admission check, or a cap would be
    # over-admitted within one tick. Two phases per job: the enhancement
    # first (off-slot, the global enhance cap — no slot claimed or started;
    # a different card, so the gen lane neither waits for it nor blocks
    # it), then the gen phase (the slot's gen-phase cap).
    prompts = (await asyncio.to_thread(gen_jobs.load_settings))["enhance"].get("prompts") or {}
    for jid in w["store"]["order"]:
        jobs = (await asyncio.to_thread(gen_jobs.load_jobs))["jobs"]
        job = jobs.get(jid)
        if not job or job["state"] != gen_jobs.QUEUED or jid in _gen_tasks:
            continue
        # PHASE 1 — the enhancement (off-slot, before any slot is claimed or
        # started). A job that can't be enhanced right now (target broken,
        # or its prompt cleared) can't run at all — claiming it would just
        # re-queue in a flap: mark the structural reason up front and wait
        # for the settings.
        if job["enhance"] and not job.get("enhanced_prompt"):
            et = w["enhance_target"]
            reason = None
            if not et["ok"]:
                reason = (
                    f"the enhance target is unavailable: {et['reason'] or 'unknown'} "
                    f"(fix the enhance target in Gen settings, or queue the job without enhance)"
                )
            elif not prompts.get(job["model"]):
                reason = (
                    f"no enhancement prompt configured for {job['model']!r} "
                    f"(Gen settings — set it, or queue the job without enhance)"
                )
            else:
                ok, _why = gen_jobs.enhance_admission(jobs)
                if not ok:
                    continue  # the target is at its cap — the normal queue
                if (await asyncio.to_thread(gen_jobs.claim_enhance, jid)) is not None:
                    _gen_tasks[jid] = asyncio.create_task(_gen_enhance_phase(jid))
                continue  # claimed — or lost the claim (canceled in the window)
            _r = reason

            def _m(j, _r=_r):
                if j.get("state") == gen_jobs.QUEUED:
                    j["error"] = _r

            await asyncio.to_thread(gen_jobs.update_job, jid, _m)
            continue
        # PHASE 2 — the gen phase (the per-slot gen-phase cap).
        for sname, _why in gen_jobs.job_candidates(jobs, slots, job["model"]):
            srow = slots[sname]
            ok, _why = gen_jobs.admission(jobs, sname, jid)
            if not ok:
                continue
            st = status[sname]
            if not (srow["running"] and srow["ip"]):
                if st["state"] not in ("down", "down-idle-stops"):
                    continue  # blocked by something the engine must not touch
                if st["state"] == "down-idle-stops" and not await _gen_free_slot(sname):
                    continue  # a stop failed or the world moved — next tick
                ok, _why = gen_jobs.admission((await asyncio.to_thread(gen_jobs.load_jobs))["jobs"], sname, jid)
                if not ok:
                    continue  # a stop freed nothing the admission wanted
            claimed = await asyncio.to_thread(gen_jobs.claim_job, jid, sname, gen_jobs.STARTING, True)
            if claimed is None:
                continue  # lost the claim (canceled between load and claim)
            _gen_tasks[jid] = asyncio.create_task(_gen_run_job(jid, sname))
            break
        else:
            # no candidate admitted it this tick. The transient reasons (the
            # gen-phase cap, a blocked lane, a stop that didn't land) are the
            # normal queue — no say-so. The structural one (a config drift
            # since the job was queued: its model is no longer hosted) would
            # otherwise wait forever in silence: mark it on the record's
            # error column (queued-only — a cancel that landed in the window
            # wins; a later claim clears the error on progress). The enhance
            # target's structural reasons are marked up front in the enhance
            # branch above — a pending-enhance job never gets here.
            reason = gen_jobs.queue_wait_reason(job, slots)
            if reason:
                def _m(j, _r=reason):
                    if j.get("state") == gen_jobs.QUEUED:
                        j["error"] = _r

                await asyncio.to_thread(gen_jobs.update_job, jid, _m)


async def _gen_engine_loop() -> None:
    """~2 s tick + the age-GC. Never dies — a bad tick is logged, not raised
    (a dead engine wedges every queued job)."""
    global _gen_gc_at, _gen_dev_logged
    while True:
        await asyncio.sleep(2.0)
        if DEV_MODE:
            if not _gen_dev_logged:
                _gen_dev_logged = True
                print(
                    "gen engine: disabled (PANEL_DEV=1 — no panel-do): endpoints + state files work, jobs stay queued",
                    flush=True,
                )
            continue
        try:
            # Idle fast-path: a tick builds the fleet world (two panel-do
            # spawns) — with the queue empty and nothing in flight there is
            # nothing to claim or re-attach, so a local state-file read skips
            # the tick. In-flight counts even with an empty queue: the
            # re-attach pass (a restart's orphans) runs only in a tick.
            # Deliberately NOT a `continue` — the age-GC below runs on idle
            # ticks too: a drained queue is exactly when stale artifacts
            # would otherwise sit un-GC'd until the next job (or forever
            # once gen goes unused).
            if gen_jobs.has_work((await asyncio.to_thread(gen_jobs.load_jobs))["jobs"]):
                await _gen_tick()
        except Exception as e:  # noqa: BLE001
            print(f"gen engine tick failed: {type(e).__name__}: {e}", flush=True)
        now = time.monotonic()
        if now >= _gen_gc_at:
            _gen_gc_at = now + GEN_GC_INTERVAL_S
            try:
                days = (await asyncio.to_thread(gen_jobs.load_settings))["gc_days"]
                # Artifacts of jobs still in the store are KEPT: a record the
                # UI still shows must not point at a deleted file — those go
                # when the record is trimmed (the count cap) or deleted.
                keep = frozenset((await asyncio.to_thread(gen_jobs.load_jobs))["jobs"])
                removed = await asyncio.to_thread(gen_jobs.gc_old_artifacts, GEN_ARTIFACT_DIR, days, keep)
                if removed:
                    _audit(None, "gen-gc", [f"{len(removed)} file(s) older than {days}d"], 0, " ".join(removed)[:200])
            except Exception as e:  # noqa: BLE001
                print(f"gen artifact GC failed: {type(e).__name__}: {e}", flush=True)


# ── endpoints (admin-gated like the rest of the panel) ──


class GenJobBody(BaseModel):
    model: str  # a purpose key ("image"/"video") or an exact model name
    prompt: str
    enhance: Optional[bool] = None  # None → the settings' global toggle
    params: dict[str, Any] = {}  # image: size, seed · video: seconds, size, seed


def _gen_validate_params(kind: str, params: dict[str, Any]) -> dict[str, Any]:
    """Param SHAPE checks only — radiance is the authority on what the model
    takes (its 400, e.g. an out-of-range seconds, lands in the job record).
    Malformed values are refused HERE, at submission, because a job that can
    never be valid would still cost a slot start the user didn't expect."""
    out: dict[str, Any] = {}
    allowed = {"size", "seed"} if kind == "image" else {"seconds", "size", "seed"}
    for k, v in (params or {}).items():
        if k not in allowed:
            raise HTTPException(
                400, f"params.{k}: not a field for a {kind} job (allowed: {', '.join(sorted(allowed))})"
            )
        if k == "size":
            if not isinstance(v, str) or not gen_jobs.SIZE_RE.match(v):
                raise HTTPException(400, 'params.size: want "WIDTHxHEIGHT" like 1344x768')
            out["size"] = v
        elif k == "seed":
            if not isinstance(v, int) or isinstance(v, bool) or v < -1:
                raise HTTPException(400, "params.seed: want an integer ≥ -1 (-1 = random)")
            out["seed"] = v
        elif k == "seconds":
            # Shape only (a positive number) — the model's own range (its
            # chunks) is radiance's: its 400 "between X and Y seconds" is the
            # failure the job records.
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not float(v) > 0:
                raise HTTPException(400, "params.seconds: want a positive number of seconds")
            out["seconds"] = float(v)
    return out


@app.get("/api/gen")
def gen_overview(_admin: Optional[str] = Depends(require_admin)) -> dict[str, Any]:
    w = _gen_world()
    settings = gen_jobs.load_settings()
    queued = [w["jobs"][i] for i in w["store"]["order"] if i in w["jobs"] and w["jobs"][i]["state"] == gen_jobs.QUEUED]
    recent = [w["jobs"][i] for i in reversed(w["store"]["order"]) if i in w["jobs"]][:30]
    return {
        "slots": [{**row, "status": w["status"][row["name"]]} for row in sorted(w["slots"].values(), key=lambda r: r["name"])],
        # the whole fleet (the settings' enhance-target picks an instance +
        # one of its llm models from here)
        "instances": [row for row in w["instances"].values() if row["llms"]],
        "enhance_target": w["enhance_target"],
        "queue": queued,
        "jobs": recent,
        "models": gen_jobs.known_models(w["slots"]),
        "purposes": settings["purposes"],
        "enhance": settings["enhance"],
        "gc_days": settings["gc_days"],
    }


@app.post("/api/gen/jobs", status_code=201)
def gen_job_create(body: GenJobBody, _admin: Optional[str] = Depends(require_admin)) -> dict[str, Any]:
    prompt = (body.prompt or "").strip()
    if not prompt:
        raise HTTPException(400, "prompt is required")
    # No length cap: the per-model token cap is radiance's, and its
    # submit-time 400 is the "too big" mark the job records.
    w = _gen_world()
    slots = w["slots"]
    settings = gen_jobs.load_settings()
    try:
        model, kind = gen_jobs.resolve_model(body.model, settings, slots)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    enhance = body.enhance if body.enhance is not None else settings["enhance"]["enabled"]
    if enhance and not (settings["enhance"].get("prompts") or {}).get(model, ""):
        if body.enhance is True:
            raise HTTPException(400, f"enhancement on but no prompt configured for {model!r} (Gen settings)")
        enhance = False  # the settings' default is on, no prompt: skip (the plan's rule)
    et = w["enhance_target"]
    if enhance and not et["ok"]:
        raise HTTPException(400, et["reason"])
    params = _gen_validate_params(kind, body.params)
    with gen_jobs.gen_lock():
        job = gen_jobs.create_job(model, kind, prompt, enhance, params, _admin)
    _audit(_admin, "gen-job-create", [job["id"], model, kind], 0)
    return job


@app.get("/api/gen/jobs")
def gen_jobs_list(limit: int = 50, _admin: Optional[str] = Depends(require_admin)) -> dict[str, Any]:
    store = gen_jobs.load_jobs()
    limit = max(1, min(limit, 200))
    out = [store["jobs"][i] for i in reversed(store["order"]) if i in store["jobs"]][:limit]
    return {"jobs": out}


@app.get("/api/gen/jobs/{jid}")
def gen_job_get(jid: str, _admin: Optional[str] = Depends(require_admin)) -> dict[str, Any]:
    job = gen_jobs.get_job(jid)
    if not job:
        raise HTTPException(404, "no such job")
    return job


@app.delete("/api/gen/jobs/{jid}")
def gen_job_delete(jid: str, _admin: Optional[str] = Depends(require_admin)) -> dict[str, Any]:
    job = gen_jobs.get_job(jid)
    if not job:
        raise HTTPException(404, "no such job")
    if job["state"] in gen_jobs.CANCELLABLE_STATES:
        # Queued or in flight: mark canceled — the task (when one exists)
        # checks the state at every checkpoint (≤5 s between poll ticks) and
        # cancels the radiance video when it holds an id; a queued job has no
        # task — the mark is the whole cancel. Either way the record stays
        # (history) until deleted.
        # The check lives INSIDE the mutation (one critical section): a
        # terminal transition (a finish, or a first cancel) that lands between
        # the read above and this mark wins — the job is simply done (its
        # artifact, if any, stands) and the UI's re-poll shows it; a raw
        # overwrite would turn a completed job into a canceled one. A
        # re-queue in that window does NOT drop the cancel — queued is still
        # cancellable, so the mark applies.
        def _m(j: dict[str, Any]) -> None:
            if j["state"] in gen_jobs.TERMINAL_STATES:
                raise RuntimeError(f"{jid} is {j['state']}, terminal")
            j.update({"state": gen_jobs.CANCELED, "error": "canceled", "finished_at": time.time()})

        try:
            gen_jobs.update_job(jid, _m)
        except RuntimeError:
            return {"ok": True}  # went terminal while canceling — nothing to cancel
        _audit(_admin, "gen-job-cancel", [jid], 0)
    else:
        # Terminal: delete the record + its artifact files (the GC would take
        # them by age anyway; this is the "clear from history" path).
        gen_jobs.remove_job(jid)
        _gen_delete_artifacts(job)
        _audit(_admin, "gen-job-delete", [jid], 0)
    return {"ok": True}


@app.get("/api/gen/jobs/{jid}/content")
def gen_job_content(jid: str, _admin: Optional[str] = Depends(require_admin)):
    """The artifact bytes (image or video — one endpoint, as settled). V2
    re-exposes this under /machine/gen/* behind the bearer key. Served inline
    (no Content-Disposition): the UI displays via the /gen/ static twin, and
    a machine consumer takes the bytes regardless of the header."""
    job = gen_jobs.get_job(jid)
    if not job or not job.get("artifact"):
        raise HTTPException(404, "no artifact for this job")
    name = job["artifact"]["url"].rsplit("/", 1)[-1]
    path = GEN_ARTIFACT_DIR / name
    if not path.is_file():
        raise HTTPException(410, "artifact removed (GC)")
    return FileResponse(path, media_type=job["artifact"].get("mime") or "application/octet-stream")


class GenSettingsBody(BaseModel):
    purposes: dict[str, str] = {}
    enhance: dict[str, Any] = {}
    gc_days: int = 14


@app.get("/api/gen/settings")
def gen_settings_get(_admin: Optional[str] = Depends(require_admin)) -> dict[str, Any]:
    w = _gen_world()
    return {"settings": gen_jobs.load_settings(), "models": gen_jobs.known_models(w["slots"])}


@app.put("/api/gen/settings")
def gen_settings_put(body: GenSettingsBody, _admin: Optional[str] = Depends(require_admin)) -> dict[str, Any]:
    w = _gen_world()
    clean, errors = gen_jobs.validate_settings(body.model_dump(), w["slots"], w["instances"])
    if errors:
        raise HTTPException(400, "; ".join(errors[:5]))
    with gen_jobs.gen_lock():
        gen_jobs.save_settings(clean)
    _audit(_admin, "gen-settings-put", [json.dumps(clean, sort_keys=True)[:200]], 0)
    return {"settings": clean}
