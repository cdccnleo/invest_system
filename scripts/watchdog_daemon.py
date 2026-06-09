#!/usr/bin/env python3
"""
Watchdog daemon for schedule_runner.py and streamlit dashboard.

Spawns both services as child processes, monitors them every 30 seconds,
and restarts any that crash within 5 seconds.

Designed for WSL environments without systemd.
"""

from __future__ import annotations

import fcntl
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
def _is_schedule_runner_already_running() -> bool:
    """
    检测是否已有 schedule_runner 进程在跑 (孤儿 + 持锁).
    防止 watchdog 启新 schedule_runner 抢锁失败导致死循环.
    使用 /proc/locks 查真正持锁的 PID (不依赖锁文件内容).
    """
    lock_path = WORK_DIR / "logs" / ".schedule_runner.lock"
    try:
        lock_inode = lock_path.stat().st_ino
    except (FileNotFoundError, OSError):
        return False

    # 1. 扫 /proc/locks 找 FLOCK WRITE 持本 inode 的 PID
    holder_pid = None
    try:
        with open("/proc/locks") as f:
            for line in f:
                if "FLOCK" not in line:
                    continue
                parts = line.split()
                # 格式: "10: FLOCK  ADVISORY  WRITE 26873 08:30:292751 0 EOF"
                #       parts[0]=num:  parts[1]=FLOCK  parts[2]=ADVISORY  parts[3]=WRITE  parts[4]=PID  parts[5]=dev:ino  parts[6]=start  parts[7]=end
                if len(parts) < 8:
                    continue
                if "WRITE" not in parts:
                    continue
                try:
                    pid = int(parts[4])
                    inode_str = parts[5].split(":", 2)[-1]  # "08:30:292751" -> "292751"
                    inode = int(inode_str)
                except (ValueError, IndexError):
                    continue
                if inode == lock_inode:
                    holder_pid = pid
                    break
    except OSError:
        return False

    if not holder_pid:
        return False

    # 2. 检查持锁 PID 是不是真的 schedule_runner
    proc_dir = Path(f"/proc/{holder_pid}")
    if not proc_dir.exists():
        # 持锁 PID 死了, 锁应自动释放. 强删 lock.
        try:
            lock_path.unlink()
            log.warning("[watchdog] 持锁 PID=%s 已死, 强删 lock", holder_pid)
        except OSError:
            pass
        return False

    try:
        cmdline = (proc_dir / "cmdline").read_bytes().decode("utf-8", "replace").replace("\0", " ")
    except OSError:
        return False

    if "schedule_runner.py" in cmdline:
        log.info("[watchdog] 持锁 PID=%s 是孤儿 schedule_runner, 跳过启新", holder_pid)
        return True
    return False


def _is_streamlit_already_running(port: int = 8501) -> bool:
    """
    检测端口 port 是否被已有 streamlit 进程占用.
    防止 watchdog 启新 streamlit 端口冲突导致死循环.
    通过 /proc/net/tcp 解析监听端口 + /proc/<pid>/cmdline 验证.
    """
    # 1. 解析 /proc/net/tcp 找监听端口
    port_hex = f"{port:04X}".upper()
    listening_pids = set()
    try:
        with open("/proc/net/tcp") as f:
            next(f)  # skip header
            for line in f:
                parts = line.split()
                if len(parts) < 10:
                    continue
                local_addr = parts[1]
                state = parts[3]
                if state != "0A":  # 0A = LISTEN
                    continue
                if ":" + port_hex not in local_addr:
                    continue
                # 拿到 inode (parts[9])
                try:
                    sock_inode = int(parts[9])
                except (ValueError, IndexError):
                    continue
                # 2. 遍历 /proc/*/fd/* 找谁持这个 socket inode
                import glob
                for fd_path in glob.glob("/proc/[0-9]*/fd/*"):
                    try:
                        target = os.readlink(fd_path)
                    except OSError:
                        continue
                    if f"socket:[{sock_inode}]" in target:
                        pid = int(fd_path.split("/")[2])
                        listening_pids.add((pid, sock_inode))
    except OSError:
        return False

    # 3. 检查 pid 是不是 streamlit
    for pid, _ in listening_pids:
        try:
            cmdline = (Path(f"/proc/{pid}") / "cmdline").read_bytes().decode("utf-8", "replace").replace("\0", " ")
            if "streamlit run" in cmdline:
                log.info("[watchdog] 端口 %d 被孤儿 streamlit PID=%s 占用, 跳过启新", port, pid)
                return True
        except OSError:
            continue
    return False


class ServiceProcess:
    """Wraps a child Popen and its metadata."""

    def __init__(self, name: str, cmd: list[str]):
        self.name = name
        self.cmd = cmd
        self.proc: Optional[subprocess.Popen] = None
        self.restart_count = 0
        self.skipped_count = 0  # schedule_runner 跳过启动的次数
        self.stdout_thread = None
        self.stderr_thread = None

    def _stream_to_log(self, pipe, label: str):
        """后台线程: 把子进程 stdout/stderr 实时重定向到 watchdog.log, 防止 PIPE 缓冲满 (64KB) 导致子进程阻塞."""
        import threading as _threading
        for line in iter(pipe.readline, b""):
            try:
                line_s = line.decode("utf-8", "replace").rstrip()
                if line_s:
                    log.info("[%s] %s", label, line_s)
            except Exception:
                pass
        pipe.close()

    def start(self) -> subprocess.Popen:
        log.info("Starting %s: %s", self.name, " ".join(self.cmd))
        # 用 DEVNULL 替代 PIPE: 子进程 stdout/stderr 直通 /dev/null, 永不阻塞.
        # 完整输出应走 FileHandler, 不是 Popen 的 pipe (与原版 stdin=PIPE / stderr=PIPE 的 64KB 死锁隐患不同).
        proc = subprocess.Popen(
            self.cmd,
            cwd=str(WORK_DIR),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        self.proc = proc
        return proc

    def is_alive(self) -> bool:
        if self.proc is None:
            return False
        return self.proc.poll() is None

    def restart(self) -> None:
        # 先 wait() 收尸旧子进程, 避免变 zombie 占资源
        if self.proc is not None and self.proc.poll() is None:
            try:
                self.proc.kill()
            except Exception:
                pass
        if self.proc is not None:
            try:
                self.proc.wait(timeout=5)
            except Exception:
                pass
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
                # 启新前先检测是否有孤儿在跑
                skip = False
                if svc.name == "schedule_runner" and _is_schedule_runner_already_running():
                    skip = True
                elif svc.name == "streamlit" and _is_streamlit_already_running(8501):
                    skip = True
                if skip:
                    svc.skipped_count += 1
                    if svc.skipped_count % 10 == 1:
                        log.info("[watchdog] %s 跳过启动 #%d (孤儿已存活)", svc.name, svc.skipped_count)
                    time.sleep(RESTART_DELAY)
                    continue
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
    # 单实例锁：防止多个 watchdog 同时跑导致 streamlit/schedule_runner 被 spawn 多次
    lock_path = WORK_DIR / "logs" / ".watchdog.lock"
    try:
        lock_fd = open(lock_path, "w")
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        lock_fd.write(str(os.getpid()))
        lock_fd.flush()
    except (BlockingIOError, OSError):
        log.error("Watchdog 已在运行（lock 被占），当前进程退出。")
        sys.exit(0)

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
