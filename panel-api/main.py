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
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

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
    global _llm_http
    # read=300 s: normal streams emit bytes far more often than every 5 min,
    # but a wedged instance that accepts TCP and then goes silent (ROCm init
    # hang) must not park the client's request forever — the deadline fails
    # it, and the passive cooldown + /health probe steer the next request
    # down the list. connect capped at 5 s.
    _llm_http = httpx.AsyncClient(timeout=httpx.Timeout(300.0, connect=5.0, read=300.0, pool=5.0))
    probe = asyncio.create_task(_llm_probe_loop())
    try:
        yield
    finally:
        probe.cancel()
        try:
            await probe  # let the in-flight cycle finish cancelling before closing its client
        except asyncio.CancelledError:
            pass
        await _llm_http.aclose()


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


def amd_gpus() -> list[dict[str, Any]]:
    """AMD GPU stats from sysfs — no ROCm / amd-smi needed."""
    out: list[dict[str, Any]] = []
    for card in sorted(glob.glob("/sys/class/drm/card[0-9]*"), key=_natural_key):
        dev = f"{card}/device"
        hwmons = sorted(glob.glob(f"{dev}/hwmon/hwmon*"), key=_natural_key)
        # Skip non-GPU drm devices (USB-C display adapters, framebuffers, …):
        # an amdgpu always exposes gpu_busy_percent.
        if not hwmons and _rd(f"{dev}/gpu_busy_percent") is None:
            continue
        hw = hwmons[0] if hwmons else None
        # No product_name on many kernels — the PCI id + slot at least make the
        # two identical cards distinguishable in the UI (name falls back to cardN).
        uevent = _rd(f"{dev}/uevent", default="") or ""
        ue = dict(
            line.split("=", 1) for line in uevent.splitlines() if "=" in line
        )
        out.append(
            {
                "card": os.path.basename(card),
                "name": _rd(f"{dev}/product_name") or _rd(f"{dev}/label") or os.path.basename(card),
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
    return {
        "ts": int(time.time()),
        "host": {"hostname": uname.nodename, "kernel": uname.release},
        "gpus": amd_gpus(),
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


# ─────────────────────────── LLM fallback router ─────────────────────────────
# llm.pietrasik.top → this service (router in traefik/dynamic/llm.yml). Routes
# OpenAI-style /v1/* to the FIRST instance that's up in the admin-configured
# priority order (panel Settings → "llm.pietrasik.top"). Strictly single-active
# — the point is "the best model I'm serving right now", never load-split.
#
# Auth is the same gate as the per-instance subdomains: Authorization: Bearer
# with LLM_API_KEY. No Authelia here — API clients speak bearer tokens, and
# forward-auth is cookie-based (see the llm.yml header). /v1/models needs no
# special handling: it simply proxies to the active instance. The gate is
# NOT bypassed by PANEL_DEV=1 (unlike the panel endpoints): local dev of
# the router needs LLM_API_KEY set to any non-empty value.
#
# Security: the order holds compose SERVICE names (llm-<name> — what the
# traefik rules and `make rollback` use), validated against the live llm-*
# containers and resolved to edge-network IPs server-side — the config
# (and its admin) can never name an arbitrary host address, so there is no
# SSRF surface beyond the fleet the admin already fully controls.

LLM_PROBE_INTERVAL_S = 30.0  # background /health refresh per instance
LLM_PROBE_TIMEOUT_S = 3.0  # /health budget (connect+read)
LLM_STALE_AFTER_S = 90.0  # a cached "up" older than this forces an on-demand re-probe
LLM_COOLDOWN_S = 30.0  # passive: a failed proxied request parks the instance this long
LLM_MAX_ORDER = 8
LLM_MAX_BODY = 10 * 1024 * 1024
LLM_NAME_RE = re.compile(r"^llm-[a-z0-9][a-z0-9-]{0,62}$")

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
            fleet = await asyncio.to_thread(_llm_fleet)
        except Exception:  # noqa: BLE001 — a probe-cycle hiccup is not fatal
            continue
        ip_by_name = _llm_ip_by_name(fleet, ips)

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


def _llm_fleet() -> list[dict[str, str]]:
    """All llm-* containers (running or stopped) as {name, container, display, state}.

    `name` is the identifier the order holds: the compose SERVICE name
    (llm-27b — the same name the traefik rules, the RUNBOOK and `make
    rollback WHAT=…` use) when the container carries the compose service
    label, else the bare container name (a hand-`docker run`-ed llm-*).
    NOT the docker container name — compose prefixes it (panel-llm-27b),
    so keying on it would find nothing on the deployed stack.
    `container` is that docker name: the key _edge_ips() maps to an IP.
    """
    out: list[dict[str, str]] = []
    for x in containers_cached():
        raw_names = x.get("Names") or ""
        container = str(raw_names[0] if isinstance(raw_names, list) else raw_names or "").lstrip("/")
        svc = _cli_label_get(x.get("Labels") or "", "com.docker.compose.service")
        name = svc if svc and LLM_NAME_RE.match(svc) else container
        if not name.startswith("llm-"):
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


def _llm_fleet_view() -> list[dict[str, str]]:
    """All llm-* containers (running or stopped): the UI's add-list."""
    return [{"name": f["name"], "display": f["display"], "state": f["state"]} for f in _llm_fleet()]


def _llm_ip_by_name(fleet: list[dict[str, str]], ips: dict[str, str]) -> dict[str, str]:
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
        fleet = await asyncio.to_thread(_llm_fleet)
    except HTTPException:
        fleet = []  # panel-do down: degrade to 503-with-reasons, not a 500
    ip_by_name = _llm_ip_by_name(fleet, ips)
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
        "fleet": _llm_fleet_view(),
        "probe_interval_s": LLM_PROBE_INTERVAL_S,
    }


@app.put("/api/settings/llm")
def llm_settings_put(body: LLMSettingsBody, _admin: Optional[str] = Depends(require_admin)) -> dict[str, Any]:
    known = {f["name"] for f in _llm_fleet_view()}
    order: list[str] = []
    for name in body.order:
        if not LLM_NAME_RE.match(name):
            raise HTTPException(400, f"bad instance name {name!r} (want llm-<name>)")
        if name not in known:
            raise HTTPException(400, f"unknown instance {name!r} (not a llm-* container)")
        if name not in order:
            order.append(name)
    if len(order) > LLM_MAX_ORDER:
        raise HTTPException(400, f"at most {LLM_MAX_ORDER} instances in the order")
    _llm_save(order)
    _audit(_admin, "llm-settings-put", order, 0)
    status, active = _llm_status_view(order)
    return {"order": order, "status": status, "active": active}
