#!/usr/bin/env python3
"""Demo mock of the panel-api — local screenshots of the UI with realistic data.

Stands in for panel-api on 127.0.0.1:8010 (where the Vite dev server proxies
/api). Stdlib only, no deps, no host access: every reading is a bounded random
walk around a snapshot of the live box (Threadripper + 7× AI PRO R9700, four
saturated chat cards, two idle gen slots, one reserved x8 card).

    python3 tools/demo/mock-api.py            # http://127.0.0.1:8010/api/...
    cd compose/panel-frontend && npm run dev  # http://localhost:5173

Nothing here touches auth (there is none) or the privileged path (panel-do is
never invoked) — it only serves the read shapes the UI polls, plus no-op
stubs for the mutation endpoints so clicking buttons doesn't raise errors.
"""

from __future__ import annotations

import json
import hashlib
import random
import re
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST = "127.0.0.1"
PORT = 8010
NOW = time.time()

# ─────────────────────────── demo fleet data ───────────────────────────
# Display names / categories from compose/docker-compose.yml (panel.manage,
# panel.category labels); cards from the gpu-pool ledger. All values mirror a
# snapshot of the real box; the walks below keep the charts alive.

HOSTNAME = "omnissiah"
KERNEL = "7.3.5-2-cachyos"
CPU_MODEL = "AMD Ryzen Threadripper PRO 9965WX 24-Cores"
CORES = 48
UPTIME_BASE = 12 * 3600 + 33 * 60  # 12h 33m at server start

RADIANCE = "stilldeadcode/radiance:latest"

# (display, service, image, state, status, category, private)
CONTAINERS = [
    ("Writer", "gpu-writer", RADIANCE, "running", "Up About an hour (healthy)", "LLM", True),
    ("Qwen 27B IQ6R-S-V4 1", "gpu-27b-1", RADIANCE, "running", "Up 2 hours (healthy)", "LLM", False),
    ("Qwen 27B IQ6R-S-V4", "gpu-27b", RADIANCE, "running", "Up 13 hours (healthy)", "LLM", False),
    ("Qwen Flash-Next", "gpu-flashnext", RADIANCE, "exited", "Exited (0) 2 hours ago", "LLM", False),
    ("Qwen 27B IQ6R-S-V4 2", "gpu-27b-2", RADIANCE, "exited", "Exited (0) 2 hours ago", "LLM", False),
    ("panel-panel-web-1", "panel-panel-web", "panel-panel-web:local", "running", "Up 6 minutes (healthy)", "Panel", False),
    ("edge router", "traefik", "traefik:v3.3.12", "running", "Up 3 hours (healthy)", "Panel", False),
    ("panel-authelia-1", "authelia", "authelia/authelia:4.33.1", "running", "Up 3 hours (healthy)", "Panel", False),
    ("panel-portfolio-1", "portfolio", "nginx:stable-alpine", "running", "Up 13 hours (healthy)", "Web", False),
    ("Gen TP1-B", "gpu-gentp1b", RADIANCE, "running", "Up About an hour", "Gen", True),
    ("Gen TP1-A", "gpu-gentp1a", RADIANCE, "running", "Up About an hour", "Gen", True),
    ("Gen TP2", "gpu-gentp2", RADIANCE, "exited", "Exited (0) About an hour ago", "Gen", True),
    ("SearXNG", "searxng", "searxng/searxng:latest", "running", "Up 3 hours (healthy)", "Search", False),
]
# docker names for containerless rows (compose project name "panel")
CONTAINER_IDS = {
    "gpu-writer": "9f3c1a7d2e0b", "gpu-27b-1": "4b8e2f9a6c1d", "gpu-27b": "7d2a4c8e1f3b",
    "gpu-flashnext": "c5e9f1a3b7d0", "gpu-27b-2": "1a6b3d9e5c2f", "panel-panel-web": "8e4f2a6c1d9b",
    "traefik": "3c7d1e5f9a2b", "authelia": "6f2a8c4e1d7b", "portfolio": "a1d5e9f3c7b0",
    "gpu-gentp1b": "5b9c3e7f2a4d", "gpu-gentp1a": "e7a1f5c9d3b6", "gpu-gentp2": "2c6e0b4a8f1d",
    "searxng": "f4b8d2e6a0c3",
}

# (index, holder, reserved-for, reserved-note) — the pool ledger
POOL = [
    (0, "gpu-27b", None, None),
    (1, "gpu-27b", None, None),
    (2, "gpu-27b-1", None, None),
    (3, "gpu-27b-1", None, None),
    (4, "gpu-writer", "gpu-writer", "PICe5 x8"),
    (5, "gpu-gentp1a", None, None),
    (6, "gpu-gentp1b", None, None),
]

# name, display, cards_needed, assigned, holding, state, autostart
INSTANCES = [
    ("gpu-27b", "Qwen 27B IQ6R-S-V4", 2, [0, 1], [0, 1], "running", True),
    ("gpu-27b-1", "Qwen 27B IQ6R-S-V4 1", 2, [2, 3], [2, 3], "running", True),
    ("gpu-27b-2", "Qwen 27B IQ6R-S-V4 2", 2, [5, 6], None, "exited", False),
    ("gpu-flashnext", "Qwen Flash-Next", 4, [2, 3, 5, 6], None, "exited", False),
    ("gpu-gentp1a", "Gen TP1-A", 1, [5], [5], "running", False),
    ("gpu-gentp1b", "Gen TP1-B", 1, [6], [6], "running", False),
    ("gpu-gentp2", "Gen TP2", 2, [5, 6], None, "exited", False),
    ("gpu-writer", "Writer", 1, [4], [4], "running", True),
]

# The action registry, verbatim from panel-do/main.go (sorted, like --list).
ACTIONS = [
    ("autoupdate-toggle", "Enable (on) or disable (off) the daily auto-update timer", [r"^(on|off)$"], 30, True, "updates"),
    ("edge-ips", "edge-network endpoints: containerID → {Name, IPv4Address} (JSON on stdout)", [], 15, False, "containers"),
    ("gpu-apply", "(Re)create + start a gpu-* instance on the override's GPU set", [r"^gpu-[a-z0-9][a-z0-9-]{0,62}$"], 300, True, "gpu"),
    ("gpu-fleet", "Fleet instances + cards needed (JSON on stdout)", [], 20, False, "gpu"),
    ("gpu-live", "Live fleet containers: state + ROCR_VISIBLE_DEVICES holds (JSON on stdout)", [], 20, False, "gpu"),
    ("gpu-stop", "Stop a gpu-* instance (container kept, cards released)", [r"^gpu-[a-z0-9][a-z0-9-]{0,62}$"], 90, True, "gpu"),
    ("list-containers", "List all containers (JSON Lines on stdout)", [], 15, False, "containers"),
    ("list-services", "Deployed compose services with the panel.manage label (JSON on stdout)", [], 15, False, "services"),
    ("prune-docker", "Prune dangling images + build cache (never containers or networks)", [], 300, True, "containers"),
    ("restart-container", "Restart a container (12-hex ID)", [r"^[0-9a-f]{12}$"], 90, True, "containers"),
    ("restart-service", "Restart a systemd service", [r"^[a-zA-Z0-9][a-zA-Z0-9._@-]*\.service$"], 30, True, "services"),
    ("servitor-doctor", "Run the servitor deploy doctor (guest certification + firewall probes + live prompt + live search)", [], 600, False, "servitor"),
    ("servitor-status", "Servitor VM state + guest /healthz probe (read-only)", [], 30, False, "servitor"),
    ("servitor-sync", "Push the host checkout (config/lib/app + key slice) into the servitor VM; never blocks a run", [], 600, True, "servitor"),
    ("servitor-vm-restart", "Reboot the servitor VM (planned; boot reconciliation handles the unclean path)", [], 60, True, "servitor"),
    ("start-container", "Start a stopped container (12-hex ID)", [r"^[0-9a-f]{12}$"], 60, False, "containers"),
    ("start-service", "Create + start a deployed compose service (revives a pruned one)", [r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$"], 300, True, "services"),
    ("stop-container", "Stop a running container (12-hex ID)", [r"^[0-9a-f]{12}$"], 60, True, "containers"),
    ("update-preview", "What the next update would apply: clone git facts + line files (JSON)", [], 30, False, "updates"),
    ("update-report", "Last update's before→after: state + live images + version labels + log tail (JSON)", [], 30, False, "updates"),
    ("update-rollback", "Full rollback to the last update's snapshot (detached; re-gated)", [], 300, True, "updates"),
    ("update-run", "Launch the gated stack update (detached; skips when the clone has uncommitted changes)", [], 300, True, "updates"),
    ("update-run-force", "Launch the gated stack update even with uncommitted clone changes (detached)", [], 300, True, "updates"),
]

# (name, summary, patterns, timeout, confirm, group) — group is the Actions
# view's section (panel-do's registry carries it; the view groups on it).

# 7.3.5-2-cachyos on the real box; repo head of the last update (invented)
REPO_HEAD = "9c41f7a2d0e85b3c6a1f9d2e4b7c8a0d1e3f5b7d"
SNAPSHOT = "20261122T050430Z"


# ─────────────────────────── random walks ───────────────────────────

class Walk:
    """A bounded random walk: keeps a sensor alive without a script."""

    def __init__(self, value: float, step: float, lo: float, hi: float):
        self.v = value
        self.step = step
        self.lo = lo
        self.hi = hi

    def next(self, rnd: random.Random) -> float:
        self.v += rnd.uniform(-self.step, self.step)
        if self.v < self.lo:
            self.v = self.lo + rnd.uniform(0, self.step)
        elif self.v > self.hi:
            self.v = self.hi - rnd.uniform(0, self.step)
        return self.v


def make_walks() -> dict:
    # (key, value, step, lo, hi)
    spec = {
        "cpu_use": (0.8, 0.4, 0.1, 6.0),
        "cpu_temp": (37.0, 0.25, 35.0, 39.0),
        "cpu_freq": (4640.0, 80.0, 4100.0, 5000.0),
        "load1": (0.10, 0.03, 0.02, 0.5),
        "load5": (0.13, 0.02, 0.02, 0.4),
        "load15": (0.09, 0.01, 0.02, 0.3),
        "ram_used": (155424.0, 250.0, 151000.0, 159000.0),
        "ram_cache": (6580.0, 120.0, 5200.0, 8200.0),
        "swap_used": (6.0, 1.0, 2.0, 24.0),
        "net_rx": (25000.0, 9000.0, 2000.0, 120000.0),
        "net_tx": (143000.0, 20000.0, 5000.0, 400000.0),
        "d0_used": (106496.0, 4.0, 106400.0, 106600.0),
        "d0_avail": (96256.0, 4.0, 96100.0, 96400.0),
        "d1_used": (326656.0, 6.0, 326400.0, 326900.0),
        "d1_avail": (1048800.0, 10.0, 1048600.0, 1049300.0),  # stays ≥ 1 TiB in mem()
        "d2_used": (388096.0, 8.0, 387800.0, 388400.0),
        "d2_avail": (1048800.0, 10.0, 1048600.0, 1049300.0),  # stays ≥ 1 TiB in mem()
    }
    w = {k: Walk(v, s, lo, hi) for k, (v, s, lo, hi) in spec.items()}
    # per-card walks: card → field → walk
    cards = {}
    for i in range(7):
        sat = i in (0, 1, 2, 3)
        c = {
            "util": Walk(100 if sat else 3, 1.2 if sat else 0.8, 91 if sat else 0, 100 if sat else 5),
            "clock": Walk({0: 2384, 1: 2405, 2: 2962, 3: 2904}.get(i, 41), 30 if sat else 3, 2000 if sat else 40, 3000 if sat else 60),
            "fan": Walk({0: 1076, 1: 1063, 2: 1067, 3: 1062, 4: 1077, 5: 1062, 6: 1069}[i], 10, 950, 1180),
            "power": Walk({0: 249.0, 1: 249.0, 2: 250.0, 3: 248.0, 4: 14.0, 5: 17.0, 6: 16.0}[i], 1.2 if sat else 0.6, 243 if sat else 12, 250 if sat else 19),
            "junction": Walk({0: 66.0, 1: 68.0, 2: 55.0, 3: 57.0, 4: 32.0, 5: 29.0, 6: 34.0}[i], 0.4, {0: 62, 1: 64, 2: 52, 3: 53, 4: 30, 5: 27, 6: 31}[i], {0: 71, 1: 73, 2: 58, 3: 60, 4: 34, 5: 32, 6: 37}[i]),
            "edge": Walk({0: 64.0, 1: 66.0, 2: 53.0, 3: 55.0, 4: 31.0, 5: 29.0, 6: 33.0}[i], 0.4, {0: 60, 1: 62, 2: 50, 3: 51, 4: 29, 5: 27, 6: 30}[i], {0: 69, 1: 71, 2: 56, 3: 59, 4: 33, 5: 31, 6: 36}[i]),
            "mem": Walk({0: 48.0, 1: 48.0, 2: 46.0, 3: 46.0, 4: 30.0, 5: 30.0, 6: 28.0}[i], 0.3, {0: 45, 1: 45, 2: 43, 3: 43, 4: 28, 5: 28, 6: 26}[i], {0: 50, 1: 50, 2: 48, 3: 48, 4: 32, 5: 32, 6: 30}[i]),
            "vram": Walk({0: 32517.0, 1: 32517.0, 2: 32513.0, 3: 32362.0, 4: 27763.0, 5: 23426.0, 6: 23426.0}[i], 40, {0: 32400, 1: 32400, 2: 32400, 3: 32300, 4: 27700, 5: 23380, 6: 23380}[i], {0: 32560, 1: 32560, 2: 32560, 3: 32420, 4: 27820, 5: 23470, 6: 23470}[i]),
        }
        cards[i] = c
    return {"host": w, "cards": cards}


W = make_walks()
RND = random.Random(42)
NET_SPICKY = 0  # polls until the next 5-min-window net spike (→ "peak 2.2 Mb/s")


def gpu_row(i: int) -> dict:
    c = W["cards"][i]
    sat = i in (0, 1, 2, 3)
    return {
        "card": f"card{i}",
        "rocr_index": i,
        "driver": "amdgpu",
        "pci_id": "1002:7551",
        "pci_slot": ["f3:00.0", "d3:00.0", "03:00.0", "23:00.0", "bc:00.0", "e3:00.0", "13:00.0"][i],
        "temp_edge_c": round(c["edge"].next(RND), 1),
        "temp_junction_c": round(c["junction"].next(RND), 1),
        "temp_mem_c": round(c["mem"].next(RND), 1),
        "util_pct": round(c["util"].next(RND)),
        "vram_used_mib": int(c["vram"].next(RND)),
        "vram_total_mib": 32768,
        "power_w": round(c["power"].next(RND), 1),
        "power_cap_w": 300 if i == 4 else 250,
        "fan_rpm": int(c["fan"].next(RND)),
        "clock_mhz": int(c["clock"].next(RND)),
        "mem_clock_mhz": 1258 if sat else 96,
        "reserved_for": "gpu-writer" if i == 4 else None,
        "reserved_note": "PICe5 x8" if i == 4 else None,
    }


def metrics() -> dict:
    global NET_SPICKY
    h = W["host"]
    rx = h["net_rx"].next(RND)
    tx = h["net_tx"].next(RND)
    # An occasional burst keeps a realistic peak in the 5-minute window.
    NET_SPICKY -= 1
    if NET_SPICKY <= 0:
        NET_SPICKY = 12 + RND.randrange(0, 8)  # ~30 s
        (rx, tx) = (2200000, tx) if RND.random() < 0.5 else (rx, 2200000)
    ram_used = int(h["ram_used"].next(RND))
    ram_cache = int(h["ram_cache"].next(RND))
    ram_total = 257000
    # btrfs-ish: / used + avail < total (reserved)
    d0_used = int(h["d0_used"].next(RND))
    d0_avail = int(h["d0_avail"].next(RND))
    d1_used = int(h["d1_used"].next(RND))
    d1_avail = int(h["d1_avail"].next(RND))
    d2_used = int(h["d2_used"].next(RND))
    d2_avail = int(h["d2_avail"].next(RND))
    dt = int(time.time())
    return {
        "ts": dt,
        "host": {"hostname": HOSTNAME, "kernel": KERNEL},
        "gpus": [gpu_row(i) for i in range(7)],
        "cpu": {
            "temp_c": round(h["cpu_temp"].next(RND), 1),
            "cores": CORES,
            "model": CPU_MODEL,
            "freq_mhz": int(h["cpu_freq"].next(RND)),
            "usage_pct": round(h["cpu_use"].next(RND), 1),
            "procs_running": 1 + RND.randrange(0, 4),
            "procs_blocked": 0,
        },
        "ram": {
            "total_mib": ram_total,
            "used_mib": ram_used,
            "avail_mib": ram_total - ram_used - ram_cache,
            "free_mib": ram_total - ram_used - ram_cache,
            "cache_mib": ram_cache,
        },
        "swap": {"total_mib": 257000, "used_mib": int(h["swap_used"].next(RND))},
        "disks": [
            {
                "mount": "/", "device": "nvme0n1p2", "fstype": "btrfs",
                "total_mib": 204800, "used_mib": d0_used, "avail_mib": d0_avail,
                "read_bytes_s": 0, "write_bytes_s": RND.choice([0, 0, 0, 4096, 12000]),
                "temp_c": 36,
            },
            {
                "mount": "/home", "device": "nvme0n1p3", "fstype": "ext4",
                "total_mib": 1666367, "used_mib": d1_used, "avail_mib": d1_avail,
                "read_bytes_s": 0, "write_bytes_s": 0,
                "temp_c": 36,
            },
            {
                "mount": "/models", "device": "nvme1n1p1", "fstype": "ext4",
                "total_mib": 1878258, "used_mib": d2_used, "avail_mib": d2_avail,
                "read_bytes_s": 0, "write_bytes_s": RND.choice([0, 0, 0, 0, 20480]),
                "temp_c": 37,
            },
        ],
        "load": [round(h["load1"].next(RND), 2), round(h["load5"].next(RND), 2), round(h["load15"].next(RND), 2)],
        "uptime_s": int(UPTIME_BASE + time.time() - NOW),
        "net": {
            "ifaces": ["enp52s0u8u5c2", "enp213s0f0np0", "enp213s0f1np1"],
            "rx_bits_s": int(rx),
            "tx_bits_s": int(tx),
        },
        # k10temp only — the board-sensors strip skips it, like on the real box
        "sensors": [{"chip": "k10temp", "device": "k10temp-isa-0000", "label": "Tctl", "temp_c": round(h["cpu_temp"].v, 1)}],
    }


def containers() -> dict:
    rows = []
    for display, service, image, state, status, category, private in CONTAINERS:
        rows.append({
            "id": CONTAINER_IDS[service],
            "name": display,
            "service": service,
            "category": category,
            "private": private,
            "image": image,
            "state": state,
            "status": status,
        })
    return {"containers": rows}


def stack_services() -> dict:
    rows = [
        {"name": s, "image": img, "display": disp, "restart": "unless-stopped", "category": cat}
        for disp, s, img, _state, _status, cat, _priv in sorted(CONTAINERS, key=lambda r: r[1])
    ]
    return {"services": rows}


def actions() -> dict:
    return {"actions": [
        {"name": n, "summary": s, "group": g, "arity": len(pats), "patterns": pats, "timeout_sec": t, "confirm": c}
        for n, s, pats, t, c, g in ACTIONS
    ]}


def gpus() -> dict:
    return {
        "cards": 7,
        "pool": [
            {"index": i, "holder": holder, "reserved": ({"for": rfor, "note": rnote} if rfor else None)}
            for i, holder, rfor, rnote in POOL
        ],
        "instances": [
            {
                "name": n, "display": d, "cards_needed": cn, "assigned": a, "holding": hld,
                "state": st, "running": st == "running", "autostart": auto,
            }
            for n, d, cn, a, hld, st, auto in INSTANCES
        ],
        "autostart": [n for n, _d, _cn, _a, _h, _st, auto in INSTANCES if auto],
        "warnings": [],
    }


def llm_settings() -> dict:
    return {
        "order": ["gpu-27b", "gpu-27b-1", "gpu-27b-2", "gpu-flashnext"],
        "status": {"gpu-27b": "up", "gpu-27b-1": "up", "gpu-27b-2": "down", "gpu-flashnext": "down"},
        "active": "gpu-27b",
        "fleet": [
            {"name": n, "display": d, "state": st}
            for n, d, _cn, _a, _h, st, _auto in INSTANCES
        ],
        "probe_interval_s": 30,
    }


# ────────────────────── gen: jobs, queue, artifacts ──────────────────────
# A job store with a small state machine: a queued job really does walk
# enhancing → starting → running → completed while you watch the tab, and a
# finished one owns a generated artifact (SVG — the mock has no encoder) served
# under /gen/, the same URL space panel-web serves on the server.

GEN_SETTINGS: dict = {
    "purposes": {"image": "Krea2", "video": "Minimax-H3-T2V"},
    "enhance": {
        "enabled": True,
        # the real prompt pair (compose/gpu-configs + the settings file); the
        # rewrite the simulation produces is canned, not modelled
        "prompts": {
            "Minimax-H3-T2V": "Rewrite as a cinematic video prompt: name the subject, its motion and the environment; add camera move, lens and framing, lighting and time of day, and the soundscape (ambient + foley). Keep the user's intent and subject exactly; expand only detail the 24 fps timeline can show. Plain prose, no lists, no negative phrasing.",
            # a model that was undeployed — the settings view must still show it
            # (a PUT is all-or-nothing, so a stale key blocks the save until the
            # operator copies it somewhere or drops it)
            "SDXL-Turbo": "Expand into a photoreal image prompt: subject, lens, light, grade.",
            "Krea2": "Expand into a photographic image prompt: subject and its material detail, framing and lens, lighting and time of day, colour grade, and one mood word. Keep the user's subject; add craft, not new objects. One sentence, no lists, no negative phrasing.",
        },
        "instance": "gpu-writer",
        "model": "Uncensored",
    },
    "gc_days": 14,
}

VIDEO = "Minimax-H3-T2V"
IMAGE = "Krea2"
# model name → kind, the mock's deployment (the real gen_jobs' known_models)
KNOWN_MODELS = {IMAGE: "image", VIDEO: "video"}
# the real gen_jobs' shape regexes — submission is shape-checked the same way
SIZE_RE = re.compile(r"^\d{2,4}x\d{2,4}$")
MODEL_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,127}$")
GEN_LOCK = threading.Lock()
JOBS: dict[str, dict] = {}
ORDER: list[str] = []
ARTS: dict[str, tuple[bytes, str]] = {}  # filename → (bytes, mime)
STAGE: dict[str, float] = {}  # job id → when it entered its current state
SEQ = 0


def art(seed: int, w: int, h: int) -> bytes:
    """A generated picture: a deterministic landscape in a random palette. A
    placeholder that renders in an <img> at any size, no binary assets here."""
    r = random.Random(seed)
    h1, h2 = r.randrange(360), (r.randrange(360) + r.randrange(40, 150)) % 360
    sx, sy = r.randrange(int(w * 0.15), int(w * 0.85)), r.randrange(int(h * 0.12), int(h * 0.4))
    sr = r.randrange(int(w * 0.04), int(w * 0.11))

    def ridge(base: float, amp: float) -> str:
        pts = [(0, h), (w, h)]
        step = max(1, w // 9)
        for x in range(w, -1, -step):
            pts.append((x, int(h * (base + r.uniform(-amp, amp)))))
        return " ".join(f"{x},{y}" for x, y in pts)

    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}">'
        f'<defs><linearGradient id="s" x1="0" y1="0" x2="0" y2="1">'
        f'<stop offset="0" stop-color="hsl({h1},62%,26%)"/><stop offset="1" stop-color="hsl({h2},58%,62%)"/>'
        f'</linearGradient></defs>'
        f'<rect width="{w}" height="{h}" fill="url(#s)"/>'
        f'<circle cx="{sx}" cy="{sy}" r="{sr}" fill="hsl({(h1 + 40) % 360},88%,78%)" opacity="0.92"/>'
        f'<polygon points="{ridge(0.62, 0.09)}" fill="hsl({h2},34%,24%)" opacity="0.75"/>'
        f'<polygon points="{ridge(0.78, 0.06)}" fill="hsl({(h2 + 20) % 360},30%,14%)"/>'
        f'<rect y="{int(h * 0.92)}" width="{w}" height="{int(h * 0.08)}" fill="hsl({h2},24%,9%)"/>'
        f"</svg>"
    ).encode()


def _slug(prompt: str) -> str:
    return prompt.split(",")[0][:44].lower()


def mkjob(
    model: str,
    prompt: str,
    *,
    state: str,
    ago: float,
    enhanced: str | None = None,
    params: dict | None = None,
    slot: str | None = None,
    error: str | None = None,
    progress: int | None = None,
    enhance: bool = True,
    user: str = "piet",
) -> dict:
    global SEQ
    SEQ += 1
    jid = f"job-{SEQ:06d}"
    kind = "video" if model == VIDEO else "image"
    created = NOW - ago
    started = created + 4
    took = 480 if kind == "video" else 14
    j = {
        "id": jid,
        "model": model,
        "kind": kind,
        "prompt": prompt,
        "enhance": enhance,
        "params": params if params is not None else {"size": "1344x768", "seed": 1234},
        "user": user,
        "state": state,
        "slot": slot,
        "enhanced_prompt": enhanced,
        "progress": progress,
        "error": error,
        "artifact": None,
        "radiance_video_id": f"video_{SEQ:08x}" if kind == "video" and state in ("running", "completed", "canceled") else None,
        "created_at": created,
        "started_at": started if state not in ("queued", "enhancing") else None,
        "finished_at": started + took if state in ("completed", "failed", "canceled") else None,
    }
    if state == "completed":
        j["artifact"] = put_artifact(j)
    # a seeded live job keeps its wall-clock position — the progress bar
    # continues where it is, it does not restart at zero on the first poll
    if state in ("starting", "running") and j["started_at"] is not None:
        STAGE[jid] = j["started_at"]
    JOBS[jid] = j
    ORDER.append(jid)
    return j


def mp4_bytes(seed: int) -> bytes:
    """A short clip's worth of inert bytes (not decodable — the demo's <video>
    takes the documented error→poster path, like an artifact the age GC took),
    sized like a real render so the record matches the file."""
    return random.Random(seed * 7919 + 1).randbytes(4_200_000 + seed % 3_000_000)


def _pad_svg(svg: bytes, size: int) -> bytes:
    """Grow a placeholder to its recorded size (a dot comment before the root
    close) — the file is what the record says it is, so the download link and
    the archive pack exactly the selection's MiB total."""
    if len(svg) >= size:
        return svg
    return svg[:-6] + b"<!--" + b"." * (size - len(svg) - 7) + b"--></svg>"


def put_artifact(j: dict) -> dict:
    """Write the job's artifact (image, or MP4 + first-frame thumb) into the
    served map — same names, same URLs the real panel uses. The bytes are
    placeholders grown to their recorded size (see mp4_bytes / _pad_svg), so
    the download link and the archive pack exactly what the record says."""
    w, h = (int(x) for x in str(j["params"].get("size", "1024x1024")).split("x"))
    seed = abs(hash(j["id"] + j["prompt"])) % 100000
    if j["kind"] == "video":
        blob = mp4_bytes(seed)
        ARTS[f"{j['id']}.mp4"] = (blob, "video/mp4")
        thumb = None
        if "nothumb" not in j["prompt"]:  # one row keeps the no-thumbnail path visible
            ARTS[f"{j['id']}-thumb.webp"] = (art(seed, w, h), "image/svg+xml")
            thumb = f"/gen/{j['id']}-thumb.webp"
        return {"url": f"/gen/{j['id']}.mp4", "mime": "video/mp4", "size": len(blob), "thumb": thumb}
    svg = _pad_svg(art(seed, w, h), 380_000 + seed % 900_000)
    ext, mime = (".jpg", "image/jpeg") if seed % 3 == 0 else (".png", "image/png")
    ARTS[f"{j['id']}{ext}"] = (svg, "image/svg+xml")
    return {"url": f"/gen/{j['id']}{ext}", "mime": mime, "size": len(svg), "thumb": None}


FOX = "a red fox in fresh snow, morning light"
PINE = "a red fox trotting through a snowy pine forest, snow crunching underfoot"


def gen_models() -> dict:
    """model → {kind, slots} — the real known_models for the mock's fleet."""
    return {
        IMAGE: {"kind": "image", "slots": ["gpu-gentp1a", "gpu-gentp1b"]},
        VIDEO: {"kind": "video", "slots": ["gpu-gentp2"]},
    }


def mock_resolve_model(model: str) -> tuple[str, str]:
    """The real gen_jobs.resolve_model against the mock's two deployed models
    — same purpose-key / exact-name semantics, same error shapes."""
    model = (model or "").strip()
    if model in ("image", "video"):
        target = (GEN_SETTINGS.get("purposes") or {}).get(model)
        if not target:
            raise ValueError(f"purpose {model!r} is not configured (Gen settings)")
        if target not in KNOWN_MODELS:
            raise ValueError(f"purpose {model!r} points at unknown model {target!r}")
        return target, KNOWN_MODELS[target]
    if not model:
        raise ValueError("model is required (a purpose key like 'image' or an exact model name)")
    if model not in KNOWN_MODELS:
        avail = ", ".join(sorted(KNOWN_MODELS))
        raise ValueError(f"unknown model {model!r} (known: {avail})")
    return model, KNOWN_MODELS[model]


def mock_validate_params(kind: str, params: dict) -> dict:
    """The real _gen_validate_params, shape-only: radiance stays the authority
    on ranges (an out-of-range seconds lands in the job record, not here)."""
    out: dict = {}
    allowed = {"size", "seed"} if kind == "image" else {"seconds", "size", "seed"}
    for k, v in (params or {}).items():
        if k not in allowed:
            raise ValueError(f"params.{k}: not a field for a {kind} job (allowed: {', '.join(sorted(allowed))})")
        if k == "size":
            if not isinstance(v, str) or not SIZE_RE.match(v):
                raise ValueError('params.size: want "WIDTHxHEIGHT" like 1344x768')
            out["size"] = v
        elif k == "seed":
            if not isinstance(v, int) or isinstance(v, bool) or v < -1:
                raise ValueError("params.seed: want an integer ≥ -1 (-1 = random)")
            out["seed"] = v
        elif k == "seconds":
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not float(v) > 0:
                raise ValueError("params.seconds: want a positive number of seconds")
            out["seconds"] = float(v)
    return out


def mock_validate_settings(s: dict) -> tuple[dict, list[str]]:
    """The real gen_jobs.validate_settings against the mock fleet — atomic
    (errors non-empty → nothing applied) and rebuild-from-body (a PUT is the
    whole file, not a patch — the UI's one-draft-two-tabs saves depend on
    that)."""
    errors: list[str] = []
    out = {
        "purposes": {},
        "enhance": {"enabled": True, "prompts": {}, "instance": "gpu-writer", "model": "Writer"},
        "gc_days": 14,
    }
    if not isinstance(s, dict):
        return out, ["settings must be a JSON object"]
    purp_in = s.get("purposes", {})
    if not isinstance(purp_in, dict):
        errors.append("purposes must be an object")
    else:
        for k, v in purp_in.items():
            if k not in ("image", "video"):
                errors.append(f"unknown purpose {k!r} (want one of: image, video)")
                continue
            if not isinstance(v, str) or not MODEL_NAME_RE.match(v):
                errors.append(f"purposes.{k}: bad model name {v!r}")
                continue
            if v in ("image", "video"):
                errors.append(f"purposes.{k}: {v!r} is a reserved purpose key, not a model name")
                continue
            if v not in KNOWN_MODELS:
                errors.append(f"purposes.{k}: unknown model {v!r} (not hosted by any gen slot)")
                continue
            if KNOWN_MODELS[v] != k:
                errors.append(f"purposes.{k}: {v!r} is a {KNOWN_MODELS[v]} model, not a {k} model")
                continue
            out["purposes"][k] = v
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
                if k not in KNOWN_MODELS:
                    errors.append(
                        f"enhance.prompts: unknown gen model {k!r} (matches no deployed gen model — copy or delete it in the Gen tab's enhancement prompts)"
                    )
                    continue
                if not isinstance(v, str):
                    errors.append(f"enhance.prompts.{k}: must be a string")
                    continue
                out["enhance"]["prompts"][k] = v
        inst = enh_in.get("instance", out["enhance"]["instance"])
        mdl = enh_in.get("model", out["enhance"]["model"])
        # the mock's one instance (the real thing: any deployed gpu-* with the named llm entry)
        if not isinstance(inst, str) or inst != "gpu-writer":
            errors.append(f"enhance.instance: {inst!r} is not a deployed gpu-* instance")
        elif not isinstance(mdl, str) or mdl not in ("Writer", "Uncensored"):
            errors.append(
                f"enhance.model: {inst!r} hosts no llm model {mdl!r} (its llm entries: Writer, Uncensored)"
            )
        else:
            out["enhance"]["instance"] = inst
            out["enhance"]["model"] = mdl
    gc = s.get("gc_days", out["gc_days"])
    if not isinstance(gc, int) or isinstance(gc, bool) or not 1 <= gc <= 365:
        errors.append("gc_days must be an integer 1-365")
    else:
        out["gc_days"] = gc
    return out, errors


def gen_seed() -> None:
    mkjob(IMAGE, "a lighthouse at dusk, oil painting", state="completed", ago=86400 * 3 + 900,
          enhanced="a lighthouse at dusk seen from the seawall, oil painting, impasto waves, warm lamp cutting cold blue haze, 35 mm, golden hour",
          params={"size": "1024x1024", "seed": 7})
    mkjob(VIDEO, PINE, state="completed", ago=86400 * 2 + 120,
          enhanced="a red fox trots through a snowy pine forest, low morning sun raking the trunks, snow spraying under its paws, handheld tracking shot at fox height, 35 mm, shallow depth of field; soundscape: wind in the branches, snow crunching, distant jays",
          params={"size": "1344x768", "seconds": 5, "seed": 21})
    mkjob(IMAGE, FOX, state="completed", ago=86400 * 2 + 3000, params={"size": "1024x1024", "seed": -1})
    mkjob(VIDEO, "a lighthouse beam sweeping over a storm sea, nothumb", state="completed", ago=86400 + 400,
          enhanced="a lighthouse beam sweeps a storm sea at night, spray tearing off the breakwater, locked-off wide shot, 24 mm, cold moonlight, the lamp warm; soundscape: surf, wind, gulls",
          params={"size": "1344x768", "seconds": 8, "seed": 404})
    mkjob(IMAGE, "a cubist guitar on a velvet table", state="failed", ago=86400 + 60,
          error="image generation failed: HTTP 400: size 2400x2400 exceeds the model's 2048x2048 (16384 latent tokens)",
          params={"size": "2400x2400", "seed": 9}, enhance=False)
    mkjob(IMAGE, "a copper teapot, product shot, softbox light", state="completed", ago=3600 * 20,
          params={"size": "1536x1024", "seed": 500}, enhance=False)
    mkjob(VIDEO, PINE + " (vertical)", state="canceled", ago=3600 * 6,
          error="canceled", params={"size": "768x1344", "seconds": 10, "seed": 12})
    mkjob(IMAGE, "a workshop bench at 6 am, dust in the light", state="completed", ago=3600 * 5,
          enhanced="a carpenter's workshop bench at 6 am, hand tools laid out, dust motes in a low window shaft of light, 50 mm, natural light, muted warm grade, quiet",
          params={"size": "1536x1024", "seed": 88})
    mkjob(IMAGE, "an aurora over a frozen lake, reflections", state="completed", ago=3600 * 4,
          enhanced="an aurora over a frozen lake, green and violet curtains mirrored in clear ice, foreground of cracked ice, 14 mm, long exposure, deep blue night, crisp",
          params={"size": "1344x768", "seed": 31337})
    mkjob(IMAGE, "a robot barista mid-pour, cinematic", state="completed", ago=3600 * 3,
          enhanced="a robot barista mid-pour in a small café, steam curling over the cup, 85 mm macro, rim light, shallow depth of field, cinematic teal grade, cosy",
          params={"size": "1024x1024", "seed": 2024})
    mkjob(VIDEO, "a paper boat drifting down a rain-filled gutter", state="completed", ago=3600 * 2,
          enhanced="a paper boat drifts down a rain-filled gutter past fallen leaves, low water-level follow shot, 50 mm, overcast light, grey city bokeh; soundscape: steady rain, traffic hum, a passing bicycle",
          params={"size": "1344x768", "seconds": 5, "seed": 6})
    # — the live end: one rendering, one enhancing, two queued
    mkjob(VIDEO, "a train of cargo containers climbing a mountain pass at dawn", state="running", ago=90,
          enhanced="a freight train of stacked cargo containers climbs a mountain pass at dawn, slow aerial parallel tracking shot, 70 mm, low golden sun along the viaduct, mist in the valley; soundscape: diesel rhythm, rail joints, wind",
          params={"size": "1344x768", "seconds": 8, "seed": 77},
          slot="gpu-gentp2", progress=57)
    mkjob(IMAGE, "a bowl of blood oranges on marble", state="queued", ago=70,
          params={"size": "1024x1024", "seed": 900})
    enh = mkjob(IMAGE, "a crow on a snow-covered fence, winter morning", state="enhancing", ago=25,
                params={"size": "1344x768", "seed": -1})
    STAGE[enh["id"]] = time.time() - 1.0  # mid-rewrite when the first poll lands
    mkjob(VIDEO, "waves breaking on a black-sand beach from behind", state="queued", ago=6,
          params={"size": "1344x768", "seconds": 5, "seed": -1})
    mkjob(IMAGE, FOX + ", 35 mm", state="queued", ago=2, params={"size": "1024x1024", "seed": -1})


def enhance_of(j: dict) -> str:
    """The canned rewrite — the shape is what the tab needs, not the words."""
    if j["kind"] == "video":
        return (
            f"{j['prompt'].rstrip('.')}, low tracking shot at ground level, 35 mm, shallow depth of field, "
            "golden-hour light raking across the frame; soundscape: ambient wind, foley of every footfall, "
            "distant birdsong, no music"
        )
    return (
        f"{j['prompt'].rstrip('.')}, editorial product photography, 90 mm macro, single soft window light, "
        "shallow depth of field, muted natural palette"
    )


def _claim(j: dict) -> None:
    j["slot"] = "gpu-gentp2" if j["kind"] == "video" else ("gpu-gentp1a" if int(j["id"][4:]) % 2 else "gpu-gentp1b")
    j["state"] = "starting"
    STAGE[j["id"]] = time.time()


def gen_advance() -> None:
    """Advance every non-terminal job by wall-clock time (the tab's poll drives
    it — no threads, and the mock never needs an idle tick)."""
    now = time.time()
    for j in list(JOBS.values()):
        st = j["state"]
        t = STAGE.get(j["id"], now)
        if st in ("completed", "failed", "canceled"):
            continue
        if st == "queued":
            if now - j["created_at"] < 4:
                continue
            if j["enhance"] and not j["enhanced_prompt"]:
                j["state"] = "enhancing"
                STAGE[j["id"]] = now
            else:
                _claim(j)
        elif st == "enhancing":
            if now - t < 6:
                continue
            j["enhanced_prompt"] = enhance_of(j)
            _claim(j)
        elif st == "starting":
            if now - t < 4:
                continue
            j["state"] = "running"
            j["started_at"] = now
            j["progress"] = 0 if j["kind"] == "video" else None
            STAGE[j["id"]] = now
        elif st == "running":
            span = 150.0 if j["kind"] == "video" else 25.0
            p = (now - t) / span * 100
            if p >= 100:
                j["progress"] = None
                j["state"] = "completed"
                j["finished_at"] = now
                j["error"] = None
                j["artifact"] = put_artifact(j)
                STAGE.pop(j["id"], None)
            elif j["kind"] == "video":
                j["progress"] = max(0, int(p))


def gen_view() -> dict:
    with GEN_LOCK:
        gen_advance()
        jobs = [JOBS[i] for i in ORDER if i in JOBS]
        active = [j for j in jobs if j["state"] not in ("completed", "failed", "canceled")]
        counts = {
            "all": len(jobs),
            "active": len(active),
            "image": sum(1 for j in jobs if j["kind"] == "image"),
            "video": sum(1 for j in jobs if j["kind"] == "video"),
            "failed": sum(1 for j in jobs if j["state"] in ("failed", "canceled")),
        }
        busy = {j["slot"] for j in JOBS.values() if j["state"] in ("starting", "loading", "running")}
    # a down instance has no live container — the real instance rows carry
    # container=None + ip="" in that case (the ip comes from the container)
    t2_up = "gpu-gentp2" in busy
    slots = [
        {
            "name": "gpu-gentp1a", "display": "Gen TP1-A", "cards": 1,
            "assigned": [5], "holding": [5], "running": True,
            "container": CONTAINER_IDS["gpu-gentp1a"], "ip": "172.18.0.7",
            "models": {IMAGE: "image"}, "llm": None,
            "status": {"state": "running", "stops": [], "blockers": [], "busy": "gpu-gentp1a" in busy},
        },
        {
            "name": "gpu-gentp1b", "display": "Gen TP1-B", "cards": 1,
            "assigned": [6], "holding": [6], "running": True,
            "container": CONTAINER_IDS["gpu-gentp1b"], "ip": "172.18.0.8",
            "models": {IMAGE: "image"}, "llm": None,
            "status": {"state": "running", "stops": [], "blockers": [], "busy": "gpu-gentp1b" in busy},
        },
        {
            "name": "gpu-gentp2", "display": "Gen TP2", "cards": 2,
            "assigned": [5, 6], "holding": [5, 6] if t2_up else None,
            "running": t2_up,
            "container": CONTAINER_IDS["gpu-gentp2"] if t2_up else None,
            "ip": "172.18.0.9" if t2_up else "",
            "models": {VIDEO: "video"}, "llm": None,
            "status": {
                "state": "running" if t2_up else "down-idle-stops",
                "stops": [] if t2_up else ["gpu-gentp1a", "gpu-gentp1b"],
                "blockers": [],
                "busy": t2_up,
            },
        },
    ]
    return {
        "slots": slots,
        "instances": [
            # the writer's instance row, the real build_instances shape
            {"name": "gpu-writer", "display": "Writer", "cards": 1,
             "assigned": [4], "holding": [4], "running": True,
             "container": CONTAINER_IDS["gpu-writer"], "ip": "172.18.0.6",
             "models": {}, "llms": ["Writer", "Uncensored"]},
        ],
        "enhance_target": {
            "instance": GEN_SETTINGS["enhance"]["instance"], "model": GEN_SETTINGS["enhance"]["model"],
            "running": True, "ip": "172.18.0.6", "ok": True, "reason": "",
        },
        "active": active,
        "counts": counts,
        "models": gen_models(),
        "purposes": GEN_SETTINGS["purposes"],
        "enhance": GEN_SETTINGS["enhance"],
        "gc_days": GEN_SETTINGS["gc_days"],
    }


def gen_create(body: dict) -> dict:
    """Like the real POST /api/gen/jobs: prompt/model/enhance/params all
    validated before anything is queued (same 400s, same messages)."""
    prompt = (body.get("prompt") or "").strip()
    if not prompt:
        raise ValueError("prompt is required")
    model, kind = mock_resolve_model(body.get("model"))
    enhance_in = body.get("enhance")
    enhance = bool(enhance_in) if enhance_in is not None else bool(GEN_SETTINGS["enhance"]["enabled"])
    if enhance and not (GEN_SETTINGS["enhance"].get("prompts") or {}).get(model, ""):
        if enhance_in is True:
            raise ValueError(f"enhancement on but no prompt configured for {model!r} (Gen settings)")
        enhance = False  # the settings' default is on, no prompt: skip (the plan's rule)
    params = mock_validate_params(kind, body.get("params") or {})
    with GEN_LOCK:
        j = mkjob(
            model,
            prompt,
            state="queued",
            ago=0,
            params=params,
            enhance=enhance,
        )
        j["created_at"] = time.time()
        STAGE[j["id"]] = time.time()
        return j


def gen_remove(jid: str) -> str:
    with GEN_LOCK:
        j = JOBS.get(jid)
        if not j:
            raise ValueError("no such job")
        if j["state"] in ("completed", "failed", "canceled"):
            del JOBS[jid]
            if jid in ORDER:
                ORDER.remove(jid)
            for name in list(ARTS):
                if name.startswith(jid):
                    del ARTS[name]
            return "deleted"
        j.update({"state": "canceled", "error": "canceled", "finished_at": time.time(), "progress": None})
        STAGE.pop(jid, None)
        return "canceled"


def gen_archive(ids: list[str]) -> bytes:
    import io
    import zipfile

    buf = io.BytesIO()
    manifest = []
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
        for jid in ids:
            j = JOBS.get(jid) or {}
            url = ((j.get("artifact") or {}).get("url")) or ""
            name = url.rsplit("/", 1)[-1]
            if name in ARTS:
                zf.writestr(name, ARTS[name][0])
                # the real manifest, line for line (the prompts.txt a folder of
                # pictures carries with it)
                when = time.strftime("%Y-%m-%d %H:%M", time.localtime(j.get("created_at") or 0))
                manifest.append(
                    f"[{jid}] {j.get('model')} ({j.get('kind')}) — {when}\n"
                    f"  prompt:   {j.get('prompt')}\n"
                    + (f"  enhanced: {j['enhanced_prompt']}\n" if j.get("enhanced_prompt") else "")
                    + f"  params:   {json.dumps(j.get('params') or {}, sort_keys=True)}\n"
                )
        if not manifest:
            raise ValueError("no artifacts to download (nothing selected has one on disk)")
        zf.writestr("prompts.txt", "\n".join(manifest))
    return buf.getvalue()


GEN_CARD_TEXT_CHARS = 200
GEN_CARD_DROP = ("params", "radiance_video_id", "enhanced_prompt")


def gen_card(j: dict) -> dict:
    """The page projection — mirrors the real _gen_card: the modal-only fields
    out (params, the radiance video id, the enhanced prompt — the card
    previews the human prompt only), the remaining long text clipped. The
    modal and the remix fetch the full record by id for a terminal job (the
    single-job GET stays full here, like there).
    """
    c = {k: v for k, v in j.items() if k not in GEN_CARD_DROP}
    for k in ("prompt", "error"):
        v = c.get(k)
        if isinstance(v, str) and len(v) > GEN_CARD_TEXT_CHARS:
            c[k] = v[: GEN_CARD_TEXT_CHARS - 1] + "…"
    return c


def gen_page(limit: int, cursor: int, q: str, flt: str) -> dict:
    """One window of the history (newest first) — mirrors /api/gen/jobs: same
    cursor (seq of the record to start BEFORE), the same `q`/`filter` semantics
    (the search matches the FULL text, before the projection), and the same
    `next` (the seq of the last job on the page when it is full — resume
    before it — or None at the end). The records are card projections.
    """
    needle = q.strip().lower()[:200]

    def matches(j: dict) -> bool:
        if flt == "active" and j["state"] in ("completed", "failed", "canceled"):
            return False
        if flt == "image" and j["kind"] != "image":
            return False
        if flt == "video" and j["kind"] != "video":
            return False
        if flt == "failed" and j["state"] not in ("failed", "canceled"):
            return False
        if needle:
            hay = f"{j['id']} {j['model']} {j['prompt']} {j.get('enhanced_prompt') or ''}".lower()
            if needle not in hay:
                return False
        return True

    with GEN_LOCK:
        gen_advance()
        rows: list[dict] = []
        last: int | None = None  # seq of the last job on this page
        for jid in reversed(ORDER):
            j = JOBS.get(jid)
            if not j:
                continue
            s = int(jid.rsplit("-", 1)[1])
            if cursor > 0 and s >= cursor:
                continue
            if not matches(j):
                continue
            if len(rows) == limit:
                # a full page with an older match: resume BEFORE the last job
                # shown — never the first match past the page (the `>=` skip
                # would drop it and it would never surface)
                return {"jobs": rows, "next": last}
            rows.append(gen_card(j))
            last = s
    return {"jobs": rows, "next": None}


def gen() -> dict:
    return gen_view()


# The Actions console reads better with real shapes than with a stub, so the
# read-only actions answer with their own JSON (the write ones stay no-ops).
def action_output(name: str, args: list[str]) -> str:
    if name == "gpu-fleet":
        return json.dumps([{"service": n, "cards": cn} for n, _d, cn, _a, _h, _st, _x in INSTANCES], indent=2)
    if name == "gpu-live":
        return json.dumps(
            [{"service": n, "state": st, "holds": h} for n, _d, _cn, _a, h, st, _x in INSTANCES if h], indent=2
        )
    if name == "edge-ips":
        return json.dumps({CONTAINER_IDS[s]: {"Name": f"panel-{s}-1", "IPv4Address": f"172.18.0.{i + 4}"}
                           for i, s in enumerate(CONTAINER_IDS)}, indent=2)
    if name == "list-services":
        return json.dumps([{"name": s, "image": img, "display": d} for d, s, img, _1, _2, _3, _4 in sorted(CONTAINERS, key=lambda r: r[1])], indent=2)
    if name == "list-containers":
        return "\n".join(json.dumps(r) for r in containers()["containers"])
    if name == "update-preview":
        return json.dumps(update_preview(), indent=2)
    if name == "update-report":
        return json.dumps(update_report(), indent=2)
    if name == "servitor-status":
        return json.dumps({"domain": "servitor", "state": "running", "healthz": {"ok": True, "uptime_s": 5210},
                           "gpu_pick": "gpu-27b"}, indent=2)
    return f"(demo) {name}{' ' + ' '.join(args) if args else ''}: no-op — mock API\n"


def update_report() -> dict:
    svc = (
        ("authelia", "authelia/authelia:4.32.0", "authelia/authelia:4.33.1", "running", "minor"),
        ("panel-panel-web", "panel-panel-web:local", "panel-panel-web:local", "running", "rebuilt"),
        ("portfolio", "nginx:stable-alpine", "nginx:stable-alpine", "running", "unchanged"),
        ("searxng", "searxng/searxng:latest", "searxng/searxng:latest", "running", "moved"),
        ("traefik", "traefik:v3.3.11", "traefik:v3.3.12", "running", "minor"),
    )
    return {
        "state": {
            "snapshot": SNAPSHOT,
            "status": "updated",
            "repo_root": "/opt/panel",
            "repo_head": REPO_HEAD,
            "services": {n: {"image_id": f"sha256:{i:064x}", "rollback_image": pre, "image_tags": post} for i, (n, pre, post, _s, _c) in enumerate(svc)},
        },
        "timer_enabled": True,
        "services": [
            {
                "service": n, "pre_image": pre, "pre_tags": pre.split(":")[-1], "pre_version": pre.rsplit(":", 1)[-1],
                "post_image": post, "post_version": post.rsplit(":", 1)[-1], "state": s, "change": c,
            }
            for n, pre, post, s, c in svc
        ],
        "log_tail": (
            "==> update: clone clean (head " + REPO_HEAD[:7] + ") — applying\n"
            "==> update: docker compose up -d (no-pull for pinned lines)\n"
            "==> health: 13/13 containers healthy (96 s)\n"
            "==> doctor: PASS (37 checks, 0 warn)\n"
            "==> update: done — 2 changed, 3 unchanged, 0 failed\n"
        ),
    }


def update_preview() -> dict:
    return {
        "repo_root": "/opt/panel",
        "deployed_head": REPO_HEAD,
        "clone_head": REPO_HEAD,
        "deployed_at": "2026-11-22T05:04:30Z",
        "status": "clean",
        "commits": [],
        "commit_count": 0,
        "dirty": [],
        "git_error": None,
        "changes": [],
    }


# ─────────────────────────── HTTP ───────────────────────────

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args) -> None:  # quiet
        pass

    def _json(self, obj, code: int = 200, headers: dict | None = None) -> None:
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json_conditional(self, obj) -> None:
        """The gen polls' ETag/304 — mirrors the real _gen_conditional: the
        etag is the body hash, so an unchanged store (the idle ticks — the
        walk only moves while a job is live) answers 304, headers only. The
        304 carries no body (RFC 7232) and echoes the etag so the client can
        refresh its copy; Cache-Control: no-cache forces the revalidation
        every poll, never a stale body.
        """
        body = json.dumps(obj, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()
        etag = '"' + hashlib.sha256(body).hexdigest() + '"'
        inm = (self.headers.get("If-None-Match") or "").split(",")
        # strong comparison (RFC 9110 §13.2.1): the tag is strong, so a
        # W/-prefixed candidate never matches — and neither does `*` (its
        # precondition is "no current representation", which this always
        # has) — mirrors the real endpoint
        if any(c.strip() == etag for c in inm):
            self.send_response(304)
            self.send_header("ETag", etag)
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("ETag", etag)
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        p, _q, query = self.path.partition("?")
        params = dict(urllib.parse.parse_qsl(query))
        routes = {
            "/api/metrics": metrics,
            "/api/health": lambda: {"ok": True, "dev": False, "ts": int(time.time())},
            "/api/containers": containers,
            "/api/stack-services": stack_services,
            "/api/actions": actions,
            "/api/gpus": gpus,
            "/api/gen/settings": lambda: {"settings": GEN_SETTINGS, "models": gen_models()},
            "/api/settings/llm": llm_settings,
            "/api/update": update_report,
            "/api/update/preview": update_preview,
        }
        if p.startswith("/gen/"):
            self._artifact(p[len("/gen/"):])
            return
        if p == "/api/gen":
            # the tab's overview poll — ETag/304'd, like the real endpoint
            self._json_conditional(gen())
            return
        if p == "/api/gen/jobs":
            # the gallery's page — same params + semantics as the real endpoint
            # (card records, ETag/304'd)
            flt = params.get("filter", "all")
            if flt not in ("all", "active", "image", "video", "failed"):
                self._json({"detail": "filter: one of all, active, image, video, failed"}, 400)
                return
            try:
                limit = max(1, min(int(params.get("limit", "50")), 200))
                cursor = max(0, int(params.get("cursor", "0")))
            except ValueError:
                self._json({"detail": "bad limit/cursor"}, 400)
                return
            self._json_conditional(gen_page(limit, cursor, params.get("q", ""), flt))
            return
        if p.startswith("/api/gen/jobs/"):
            # the single-job GET (the real endpoint 404s on an unknown id) —
            # advances the walk like the other gen routes, so polling ONLY
            # this route still drives the state machine. Like the real
            # endpoint: not conditional (no ETag), but no-cache (a stale
            # copy of a deleted record must not be served from cache)
            with GEN_LOCK:
                gen_advance()
                j = JOBS.get(p[len("/api/gen/jobs/"):])
            if not j:
                self._json({"detail": "no such job"}, 404)
                return
            self._json(j, headers={"Cache-Control": "no-cache"})
            return
        if p in routes:
            self._json(routes[p]())
        else:
            self._json({"detail": f"not found (demo): {p}"}, 404)

    def _artifact(self, name: str) -> None:
        """The artifacts the mock generated (a real deploy serves the files from
        /models/gen-output through panel-web's nginx). The MP4 is inert bytes —
        undecodable, so the UI's <video> falls back to its poster, like it does
        for an artifact the age GC took; the download link and the archive pack
        the bytes for real (see mp4_bytes)."""
        found = ARTS.get(name)
        if not found:
            self._json({"detail": f"no artifact {name} (demo)"}, 404)
            return
        body, mime = found
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        p = self.path.split("?", 1)[0]
        body = self._read_body()
        try:
            if p == "/api/gen/jobs":
                self._json(gen_create(body), 201)
            elif p == "/api/gen/jobs/remove":
                # like the real endpoint: every id gets an answer (unknown
                # ones fail per id) — nothing is dropped silently
                ids = list(dict.fromkeys(body.get("ids", [])))
                if not ids:
                    raise ValueError("no job ids")
                out = []
                for jid in ids:
                    try:
                        out.append({"id": jid, "ok": True, "action": gen_remove(jid)})
                    except ValueError as e:
                        out.append({"id": jid, "ok": False, "error": str(e)})
                self._json({"results": out})
            elif p == "/api/gen/jobs/archive":
                ids = list(dict.fromkeys(body.get("ids", [])))
                if not ids:
                    raise ValueError("no job ids")
                try:
                    blob = gen_archive(ids)
                except ValueError as e:
                    self._json({"detail": str(e)}, 404)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/zip")
                self.send_header("Content-Disposition", 'attachment; filename="panel-gen-demo.zip"')
                self.send_header("Content-Length", str(len(blob)))
                self.end_headers()
                self.wfile.write(blob)
            elif p in ("/api/update", "/api/update/rollback"):
                self._json({"ok": True, "exit_code": 0, "stdout": "(demo) no-op — mock API", "stderr": ""})
            elif p.startswith("/api/action/"):
                name = p[len("/api/action/"):]
                self._json({"ok": True, "exit_code": 0, "stdout": action_output(name, body.get("args", [])), "stderr": ""})
            elif p.startswith("/api/containers/") and p.count("/") >= 4:
                self._json({"ok": True})
            elif p in ("/api/gpus/start", "/api/gpus/stop", "/api/gpus/assign",
                       "/api/gpus/autostart", "/api/gpus/reserve", "/api/gpus/unreserve"):
                self._json(gpus())
            else:
                self._json({"ok": True})
        except ValueError as e:
            self._json({"detail": str(e)}, 400)

    def do_PUT(self) -> None:
        body = self._read_body()
        if self.path.split("?", 1)[0] == "/api/gen/settings":
            # like the real endpoint: full validation, atomic (an error applies
            # nothing), rebuild-from-body (a PUT is the whole file, not a patch)
            clean, errors = mock_validate_settings(body)
            if errors:
                self._json({"detail": "; ".join(errors[:5])}, 400)
                return
            with GEN_LOCK:
                GEN_SETTINGS.clear()
                GEN_SETTINGS.update(clean)
            self._json({"settings": GEN_SETTINGS})
            return
        self._json({"ok": True})

    def do_DELETE(self) -> None:
        self._read_body()
        p = self.path.split("?", 1)[0]
        if p.startswith("/api/gen/jobs/"):
            try:
                self._json({"ok": True, "action": gen_remove(p[len("/api/gen/jobs/"):])})
            except ValueError as e:
                self._json({"detail": str(e)}, 404)
            return
        self._json({"ok": True})

    def _read_body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""
        if not raw:
            return {}
        try:
            data = json.loads(raw)
            return data if isinstance(data, dict) else {}
        except json.JSONDecodeError:
            return {}


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else PORT
    gen_seed()
    print(f"demo mock api on http://{HOST}:{port} (serves the panel-api read shapes + {len(JOBS)} gen jobs)")
    ThreadingHTTPServer((HOST, port), Handler).serve_forever()
