"""Panel API — host-side FastAPI service behind panel.pietrasik.top/api.

Trust model (defense in depth):
  * Traefik terminates TLS, runs Authelia forward-auth (admins only) and
    injects X-Panel-Token from /opt/panel/.env (never client-visible; Traefik
    overwrites any client-supplied value).
  * This service independently re-verifies X-Panel-Token and the Remote-User /
    Remote-Groups headers Authelia set.
  * EVERYTHING privileged — container list/start/stop/restart included — goes
    through `sudo -n panel-do`, a compiled whitelist dispatcher: one code path,
    one audit trail (journald). Only sysfs//proc metric reads bypass it.

Runs as systemd unit `panel-api`: user `panel`, binds 172.17.0.1:8010
(docker0 bridge gateway — reachable from the edge network, not the internet).

PANEL_DEV=1 disables auth checks for local development ONLY (pair it with
uvicorn on loopback). The systemd unit never sets it; never set it in prod.
"""

from __future__ import annotations

import glob
import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any, Callable, Optional

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel

PANEL_TOKEN = os.environ.get("PANEL_TOKEN", "")
PANEL_DO = "/usr/local/libexec/panel-do"
DEV_MODE = os.environ.get("PANEL_DEV") == "1"
# Filesystems the Overview shows dials for (comma-separated; .env: PANEL_DISK_MOUNTS).
DISK_MOUNTS = [m.strip() for m in os.environ.get("PANEL_DISK_MOUNTS", "/").split(",") if m.strip()]

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

app = FastAPI(title="panel-api", docs_url=None, redoc_url=None, openapi_url=None)


# ───────────────────────────── auth ─────────────────────────────

def require_admin(
    x_panel_token: Optional[str] = Header(default=None),
    remote_user: Optional[str] = Header(default=None),
    remote_groups: Optional[str] = Header(default=None),
) -> None:
    """FastAPI dependency: Traefik token + Authelia identity, both re-checked."""
    if DEV_MODE:
        return
    if not PANEL_TOKEN or not x_panel_token or x_panel_token != PANEL_TOKEN:
        raise HTTPException(401, "bad or missing X-Panel-Token")
    if not remote_user:
        raise HTTPException(401, "missing Remote-User (forward-auth bypass attempt?)")
    if "admins" not in set(GROUP_SPLIT_RE.split(remote_groups or "")):
        raise HTTPException(403, "admins only")


# ──────────────────────────── metrics ────────────────────────────

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
        out.append(
            {
                "card": os.path.basename(card),
                "name": _rd(f"{dev}/product_name") or _rd(f"{dev}/label") or os.path.basename(card),
                "temp_edge_c": _temp(f"{hw}/temp1_input" if hw else None),
                "temp_junction_c": _temp(f"{hw}/temp2_input" if hw else None),
                "temp_mem_c": _temp(f"{hw}/temp3_input" if hw else None),
                "util_pct": _rd(f"{dev}/gpu_busy_percent", int),
                "vram_used_mib": _rd(f"{dev}/mem_info_vram_used", lambda v: int(v) // 2**20),
                "vram_total_mib": _rd(f"{dev}/mem_info_vram_total", lambda v: int(v) // 2**20),
                "power_w": _hwrd(hw, "power1_average", lambda v: round(int(v) / 1e6, 1)),
                "fan_rpm": _hwrd(hw, "fan1_input", int),
            }
        )
    return out


def cpu_temp_c() -> Optional[float]:
    for hm in sorted(glob.glob("/sys/class/hwmon/hwmon*"), key=_natural_key):
        if _rd(f"{hm}/name", default="") in ("k10temp", "zenpower", "coretemp", "cpu_thermal"):
            return _temp(f"{hm}/temp1_input")
    return None


def ram() -> dict[str, int]:
    info: dict[str, int] = {}
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            key, _, val = line.partition(":")
            info[key] = int(val.strip().split()[0])  # kB
    except (OSError, ValueError, IndexError):
        return {}
    total = info.get("MemTotal", 0) // 1024
    avail = info.get("MemAvailable", 0) // 1024
    return {"total_mib": total, "used_mib": max(0, total - avail), "avail_mib": avail}


def disks() -> list[dict[str, Any]]:
    """Usage per mount in DISK_MOUNTS — the thing that kills home servers quietly.

    Every configured mount ALWAYS gets an entry: stats, or an explicit error.
    If a mountpoint isn't actually mounted (a partition failed at boot),
    statvfs silently reports the *containing* filesystem — a confidently
    wrong number under the right label. That case becomes an error entry so
    the dashboard shouts instead of showing a lie or a silent gap: you listed
    it in PANEL_DISK_MOUNTS, so its absence is itself the alert.
    """
    try:
        root_dev: Optional[int] = os.stat("/").st_dev
    except OSError:
        root_dev = None
    out: list[dict[str, Any]] = []
    for mount in DISK_MOUNTS:
        try:
            st = os.statvfs(mount)
            dev = os.stat(mount).st_dev
        except OSError:
            out.append({"mount": mount, "error": "path does not exist"})
            print(f"panel-api: disk: {mount} unavailable (stat failed)", flush=True)
            continue
        if st.f_blocks == 0:
            out.append({"mount": mount, "error": "not a mountable filesystem (no blocks)"})
            print(f"panel-api: disk: {mount} reports no blocks (pseudo-fs?)", flush=True)
            continue
        if mount != "/" and root_dev is not None and dev == root_dev:
            out.append({"mount": mount, "error": "NOT MOUNTED — partition failed to mount?"})
            print(f"panel-api: disk: {mount} is on /'s device — not mounted", flush=True)
            continue
        total = st.f_blocks * st.f_frsize
        free = st.f_bfree * st.f_frsize
        avail = st.f_bavail * st.f_frsize  # usable by non-root (excludes reserved)
        out.append(
            {
                "mount": mount,
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
def metrics(_admin: None = Depends(require_admin)) -> dict[str, Any]:
    return {
        "ts": int(time.time()),
        "gpus": amd_gpus(),
        "cpu_temp_c": cpu_temp_c(),
        "ram": ram(),
        "disks": disks(),
        "load": load_avg(),
        "uptime_s": uptime_s(),
    }


@app.get("/api/health")
def health() -> dict[str, Any]:
    # Deliberately UNauthenticated (standard liveness-probe pattern): fixed
    # minimal body, no state, no privileges — and the bind address (docker0
    # gateway) is only reachable from this host's containers. Verify auth
    # enforcement against /api/metrics instead; everything that touches state
    # or privileges sits behind require_admin.
    return {"ok": True, "dev": DEV_MODE, "ts": int(time.time())}


# ───────────────────────── containers ────────────────────────────
# Container control is NOT a second privileged path — these are panel-do
# actions (list-containers, start/stop/restart-container): same registry,
# same arg validation, same journald audit as every other action.

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
    now = time.monotonic()
    if _containers_cache["data"] is not None and now - _containers_cache["at"] < CONTAINERS_CACHE_TTL:
        return _containers_cache["data"]
    data = _containers_fetch()
    _containers_cache["data"] = data
    _containers_cache["at"] = now
    return data


def invalidate_containers_cache() -> None:
    _containers_cache["data"] = None


@app.get("/api/containers")
def containers(_admin: None = Depends(require_admin)) -> dict[str, Any]:
    managed = [
        _container_view(x)
        for x in containers_cached()
        if _cli_label_get(x.get("Labels") or "", MANAGE_LABEL) is not None
    ]
    return {"containers": managed}


@app.post("/api/containers/{cid}/{action}")
def container_action(cid: str, action: str, _admin: None = Depends(require_admin)) -> dict[str, Any]:
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
        raise HTTPException(504, "action timed out") from e
    except OSError as e:
        raise HTTPException(500, f"sudo failed: {e}") from e
    finally:
        invalidate_containers_cache()
    if proc.returncode == 0:
        return {"ok": True}
    err_lines = proc.stderr.strip().splitlines()
    detail = err_lines[-1] if err_lines else f"exit {proc.returncode}"
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
def actions(_admin: None = Depends(require_admin)) -> dict[str, Any]:
    return {"actions": panel_do_actions()}


class ActionBody(BaseModel):
    args: list[str] = []


@app.post("/api/action/{name}")
def run_action(name: str, body: ActionBody, _admin: None = Depends(require_admin)) -> dict[str, Any]:
    # Light sanity checks here (panel-do re-validates authoritatively).
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
        raise HTTPException(504, "action timed out") from e
    except OSError as e:
        raise HTTPException(500, f"sudo failed: {e}") from e
    invalidate_containers_cache()  # e.g. prune-docker removes stopped containers
    return {
        "ok": proc.returncode == 0,
        "exit_code": proc.returncode,
        "stdout": proc.stdout[-8000:],
        "stderr": proc.stderr[-8000:],
    }
