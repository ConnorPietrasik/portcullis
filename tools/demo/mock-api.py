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
import random
import re
import sys
import time
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
    ("autoupdate-toggle", "Enable (on) or disable (off) the daily auto-update timer", [r"^(on|off)$"], 30, True),
    ("edge-ips", "edge-network endpoints: containerID → {Name, IPv4Address} (JSON on stdout)", [], 15, False),
    ("gpu-apply", "(Re)create + start a gpu-* instance on the override's GPU set", [r"^gpu-[a-z0-9][a-z0-9-]{0,62}$"], 300, True),
    ("gpu-fleet", "Fleet instances + cards needed (JSON on stdout)", [], 20, False),
    ("gpu-live", "Live fleet containers: state + ROCR_VISIBLE_DEVICES holds (JSON on stdout)", [], 20, False),
    ("gpu-stop", "Stop a gpu-* instance (container kept, cards released)", [r"^gpu-[a-z0-9][a-z0-9-]{0,62}$"], 90, True),
    ("list-containers", "List all containers (JSON Lines on stdout)", [], 15, False),
    ("list-services", "Deployed compose services with the panel.manage label (JSON on stdout)", [], 15, False),
    ("prune-docker", "Prune dangling images + build cache (never containers or networks)", [], 300, True),
    ("restart-container", "Restart a container (12-hex ID)", [r"^[0-9a-f]{12}$"], 90, True),
    ("restart-service", "Restart a systemd service", [r"^[a-zA-Z0-9][a-zA-Z0-9._@-]*\.service$"], 30, True),
    ("servitor-doctor", "Run the servitor deploy doctor (guest certification + firewall probes + live prompt + live search)", [], 600, False),
    ("servitor-status", "Servitor VM state + guest /healthz probe (read-only)", [], 30, False),
    ("servitor-sync", "Push the host checkout (config/lib/app + key slice) into the servitor VM; never blocks a run", [], 600, True),
    ("servitor-vm-restart", "Reboot the servitor VM (planned; boot reconciliation handles the unclean path)", [], 60, True),
    ("start-container", "Start a stopped container (12-hex ID)", [r"^[0-9a-f]{12}$"], 60, False),
    ("start-service", "Create + start a deployed compose service (revives a pruned one)", [r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$"], 300, True),
    ("stop-container", "Stop a running container (12-hex ID)", [r"^[0-9a-f]{12}$"], 60, True),
    ("update-preview", "What the next update would apply: clone git facts + line files (JSON)", [], 30, False),
    ("update-report", "Last update's before→after: state + live images + version labels + log tail (JSON)", [], 30, False),
    ("update-rollback", "Full rollback to the last update's snapshot (detached; re-gated)", [], 300, True),
    ("update-run", "Launch the gated stack update (detached; skips when the clone has uncommitted changes)", [], 300, True),
    ("update-run-force", "Launch the gated stack update even with uncommitted clone changes (detached)", [], 300, True),
]

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
        {"name": n, "summary": s, "arity": len(pats), "patterns": pats, "timeout_sec": t, "confirm": c}
        for n, s, pats, t, c in ACTIONS
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


def gen() -> dict:
    ip = {"gpu-gentp1a": "172.18.0.7", "gpu-gentp1b": "172.18.0.8", "gpu-gentp2": "172.18.0.9", "gpu-writer": "172.18.0.6"}
    return {
        "slots": [
            {
                "name": "gpu-gentp1a", "display": "Gen TP1-A", "cards": 1,
                "assigned": [5], "holding": [5], "running": True, "ip": ip["gpu-gentp1a"],
                "models": {"Krea2": "image"}, "llm": None,
                "status": {"state": "running", "stops": [], "blockers": [], "busy": False},
            },
            {
                "name": "gpu-gentp1b", "display": "Gen TP1-B", "cards": 1,
                "assigned": [6], "holding": [6], "running": True, "ip": ip["gpu-gentp1b"],
                "models": {"Krea2": "image"}, "llm": None,
                "status": {"state": "running", "stops": [], "blockers": [], "busy": False},
            },
            {
                "name": "gpu-gentp2", "display": "Gen TP2", "cards": 2,
                "assigned": [5, 6], "holding": None, "running": False, "ip": ip["gpu-gentp2"],
                "models": {"Minimax-H3-T2V": "video"}, "llm": None,
                "status": {"state": "down-idle-stops", "stops": ["gpu-gentp1a", "gpu-gentp1b"], "blockers": [], "busy": False},
            },
        ],
        "instances": [
            {"name": "gpu-writer", "display": "Writer", "cards": 1, "running": True, "ip": ip["gpu-writer"], "llms": ["Writer", "Uncensored"]},
        ],
        "enhance_target": {
            "instance": "gpu-writer", "model": "Uncensored", "running": True,
            "ip": ip["gpu-writer"], "ok": True, "reason": "",
        },
        "queue": [],
        "jobs": [],
        "models": {
            "Krea2": {"kind": "image", "slots": ["gpu-gentp1a", "gpu-gentp1b"]},
            "Minimax-H3-T2V": {"kind": "video", "slots": ["gpu-gentp2"]},
        },
        "purposes": {"image": "Krea2", "video": "Minimax-H3-T2V"},
        "enhance": {
            "enabled": True,
            "prompts": {
                # the video prompt is set (the settings card reads "1/2 set");
                # the image one is empty (the composer shows "off (no prompt set)")
                "Minimax-H3-T2V": "Rewrite as a cinematic video prompt: name the subject, its motion and the environment; add camera move, lens and framing, lighting and time of day, and the soundscape (ambient + foley). Keep the user's intent and subject exactly; expand only detail the 24 fps timeline can show. Plain prose, no lists, no negative phrasing.",
            },
            "instance": "gpu-writer",
            "model": "Uncensored",
        },
        "gc_days": 14,
    }


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

    def _json(self, obj, code: int = 200) -> None:
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        p = self.path.split("?", 1)[0]
        routes = {
            "/api/metrics": metrics,
            "/api/health": lambda: {"ok": True, "dev": False, "ts": int(time.time())},
            "/api/containers": containers,
            "/api/stack-services": stack_services,
            "/api/actions": actions,
            "/api/gpus": gpus,
            "/api/gen": gen,
            "/api/settings/llm": llm_settings,
            "/api/update": update_report,
            "/api/update/preview": update_preview,
        }
        if p in routes:
            self._json(routes[p]())
        else:
            self._json({"detail": f"not found (demo): {p}"}, 404)

    def do_POST(self) -> None:
        p = self.path.split("?", 1)[0]
        self._read_body()
        if p == "/api/update" or p == "/api/update/rollback":
            self._json({"ok": True, "exit_code": 0, "stdout": "(demo) no-op — mock API", "stderr": ""})
        elif p == "/api/gen/jobs":
            self._json({"id": "job-" + f"{int(time.time() * 1000) % 10**12:012x}", "state": "queued"}, 201)
        elif p.startswith("/api/containers/") and p.count("/") >= 4:
            self._json({"ok": True})
        elif p in ("/api/gpus/start", "/api/gpus/stop", "/api/gpus/assign",
                   "/api/gpus/autostart", "/api/gpus/reserve", "/api/gpus/unreserve"):
            self._json(gpus())
        elif p.startswith("/api/action/"):
            self._json({"ok": True, "exit_code": 0, "stdout": "(demo) no-op — mock API", "stderr": ""})
        else:
            self._json({"ok": True})

    def do_PUT(self) -> None:
        self._read_body()
        self._json({"ok": True})

    def do_DELETE(self) -> None:
        self._read_body()
        self._json({"ok": True})

    def _read_body(self) -> None:
        n = int(self.headers.get("Content-Length") or 0)
        if n:
            self.rfile.read(n)


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else PORT
    print(f"demo mock api on http://{HOST}:{port} (serves the panel-api read shapes)")
    ThreadingHTTPServer((HOST, port), Handler).serve_forever()
