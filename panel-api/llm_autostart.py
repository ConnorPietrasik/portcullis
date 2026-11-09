"""Boot autostart for the LLM fleet — run by llm-autostart.service (oneshot,
User=panel, right after docker) on every boot.

Starts the panel-configured autostart set (GPU view → autostart column):
honors each instance's saved card set when it is free, otherwise grabs the
lowest free cards (and records that in the ledger) — in list order, so the
set that comes up is conflict-free by construction. The panel's GPU view is
the source of truth afterwards (the ledger converges on what actually
started; skipped instances show down, with the reason in the journal).

stdout/stderr go to the journal (journalctl -u llm-autostart). Exits 0 even
when nothing (or some instances) could not start — a fleet partial at boot
must never wedge or mask the rest of the boot sequence.
"""

from __future__ import annotations

import glob
import json
import os
import subprocess
import sys

import llm_gpus  # same directory (WorkingDirectory=/opt/panel-api in the unit)

PANEL_DO = "/usr/local/libexec/panel-do"
APPLY_TIMEOUT_S = 330.0  # panel-do's own budget is 5m; cover a first-run pull


def log(msg: str) -> None:
    print(f"llm-autostart: {msg}", flush=True)


def panel_do(*args: str, timeout: float = 30.0) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["sudo", "-n", PANEL_DO, *args],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def cards_total() -> int:
    """The host's GPU count — the SAME rule as panel-api's metrics
    (amd_gpus in main.py): a /sys drm card that exposes an hwmon OR
    gpu_busy_percent (skips iGPUs/framebuffers). Kept in lockstep on
    purpose — the boot planner's card range must be the pool the panel
    shows."""
    total = 0
    for d in glob.glob("/sys/class/drm/card[0-9]*"):
        dev = f"{d}/device"
        if os.path.exists(f"{dev}/gpu_busy_percent") or glob.glob(f"{dev}/hwmon/hwmon*"):
            total += 1
    return total


def main() -> int:
    try:
        with llm_gpus.gpu_lock():
            ledger = llm_gpus.load_ledger()
        if not ledger["autostart"]:
            log("no autostart instances configured — nothing to do")
            return 0
        log(f"autostart set: {', '.join(ledger['autostart'])}")

        fleet_p = panel_do("llm-fleet")
        if fleet_p.returncode != 0:
            log(f"FATAL: llm-fleet failed: {fleet_p.stderr.strip()[-300:] or f'exit {fleet_p.returncode}'}")
            return 1
        fleet = json.loads(fleet_p.stdout)
        live_p = panel_do("llm-live")
        if live_p.returncode != 0:
            log(f"FATAL: llm-live failed: {live_p.stderr.strip()[-300:] or f'exit {live_p.returncode}'}")
            return 1
        live = json.loads(live_p.stdout)
        total = cards_total()
        if total == 0:
            log("FATAL: no GPUs visible in /sys — nothing to allocate to")
            return 1

        with llm_gpus.gpu_lock():
            ledger, plan, skipped = llm_gpus.plan_boot(ledger, fleet, live, total)
            for reason in skipped:
                log(f"skip: {reason}")
            if plan:
                llm_gpus.save_ledger(ledger)
                llm_gpus.sync_override(ledger, {d["name"] for d in fleet})
        if not plan:
            log("no autostart instance could be started — nothing applied")
            return 0

        failures = 0
        for svc, gpus in plan:
            log(f"starting {svc} on card(s) {','.join(str(c) for c in gpus)}")
            p = panel_do("llm-apply", svc, timeout=APPLY_TIMEOUT_S)
            if p.returncode != 0:
                failures += 1
                log(f"FAIL {svc}: {p.stderr.strip()[-300:] or f'exit {p.returncode}'}")
            else:
                log(f"ok {svc} (model load continues in the background — check the GPU view)")
        return 1 if failures else 0
    except subprocess.TimeoutExpired:
        log("FATAL: a panel-do call timed out — docker may be wedged; check: systemctl status docker")
        return 1
    except Exception as e:  # noqa: BLE001 — an oneshot must never wedge boot
        log(f"FATAL: {type(e).__name__}: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
