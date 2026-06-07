#!/usr/bin/env python3
"""
Watchdog daemon for schedule_runner.py and streamlit dashboard.

Spawns both services as child processes, monitors them every 30 seconds,
and restarts any that crash within 5 seconds.

Designed for WSL environments without systemd.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

# ── paths ──────────────────────────────────────────────────────────────────
WORK_DIR = Path("/home/aileo/invest_system")
VENV_PY = WORK_DIR / ".venv" / "bin" / "python3.11"
LOG_FILE = WORK_DIR / "logs" / "watchdog.log"

# ── service definitions ─────────────────────────────────────────────────────
SERVICES = [
    (
        "schedule_runner",
        [str(VENV_PY), "scripts/schedule_runner.py"],
    ),
    (
        "streamlit",
        [
            str(VENV_PY),
            "-m",
            "streamlit",
            "run",
            "scripts/dashboard_views/__main__.py",
            "--server.port",
            "8501",
        ],
    ),
]

# ── logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("watchdog")


# ── process management ───────────────────────────────────────────────────────
class ServiceProcess:
    """Wraps a child Popen and its metadata."""

    def __init__(self, name: str, cmd: list[str]):
        self.name = name
        self.cmd = cmd
        self.proc: Optional[subprocess.Popen] = None
        self.restart_count = 0

    def start(self) -> subprocess.Popen:
        log.info("Starting %s: %s", self.name, " ".join(self.cmd))
        proc = subprocess.Popen(
            self.cmd,
            cwd=str(WORK_DIR),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        self.proc = proc
        return proc

    def is_alive(self) -> bool:
        if self.proc is None:
            return False
        return self.proc.poll() is None

    def restart(self) -> None:
        self.restart_count += 1
        log.warning(
            "Restarting %s (restart #%d) — previous exit code: %s",
            self.name,
            self.restart_count,
            self.proc.poll() if self.proc else "N/A",
        )
        self.start()


# ── sigterm graceful shutdown ─────────────────────────────────────────────────
shutdown_requested = False

def _sigterm_handler(signum, frame):
    global shutdown_requested
    log.info("SIGTERM received — initiating graceful shutdown")
    shutdown_requested = True


# ── monitor loop ─────────────────────────────────────────────────────────────
MONITOR_INTERVAL = 30  # seconds
RESTART_DELAY = 5     # seconds

def monitor_loop(services: list[ServiceProcess]) -> None:
    while not shutdown_requested:
        time.sleep(MONITOR_INTERVAL)

        for svc in services:
            if shutdown_requested:
                break
            if not svc.is_alive():
                log.error("%s died (exit code %s) — restarting in %ds",
                          svc.name,
                          svc.proc.poll() if svc.proc else "N/A",
                          RESTART_DELAY)
                time.sleep(RESTART_DELAY)
                if not shutdown_requested:
                    svc.restart()


def shutdown_services(services: list[ServiceProcess]) -> None:
    for svc in services:
        if svc.proc is not None and svc.is_alive():
            log.info("Terminating %s (PID %d)", svc.name, svc.proc.pid)
            try:
                svc.proc.terminate()
                svc.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                log.warning("Force-killing %s", svc.name)
                svc.proc.kill()
            except Exception as e:
                log.error("Error shutting down %s: %s", svc.name, e)


# ── main ─────────────────────────────────────────────────────────────────────
def main() -> None:
    log.info("Watchdog daemon starting — PID %d", os.getpid())
    log.info("Working directory: %s", WORK_DIR)

    # ensure logs directory exists
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

    # register SIGTERM handler
    signal.signal(signal.SIGTERM, _sigterm_handler)
    signal.signal(signal.SIGINT, _sigterm_handler)

    # start all services
    service_instances: list[ServiceProcess] = []
    for name, cmd in SERVICES:
        svc = ServiceProcess(name, cmd)
        svc.start()
        service_instances.append(svc)

    log.info("All services started — entering monitor loop")

    try:
        monitor_loop(service_instances)
    finally:
        log.info("Shutting down services...")
        shutdown_services(service_instances)
        log.info("Watchdog daemon exit")


if __name__ == "__main__":
    main()
