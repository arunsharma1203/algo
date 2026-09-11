#!/usr/bin/env python3
"""
CONCURRENT RUNTIME LIFECYCLE SUPERVISOR
=======================================
Manages independent background execution of:
1. LEGACY TRADING BACKEND (Port 8000, PID: backend/run/legacy.pid)
2. QLIB TRADING BACKEND   (Port 8001, PID: backend/run/qlib.pid)

Invariants:
- Absolute process isolation: Stopping or restarting Qlib NEVER touches Legacy.
- Absolute failure isolation: Legacy failure or restart NEVER touches Qlib.
- No destructive `killall` commands: All process control is strictly PID-addressed.
- Authentic health verification: Probes live HTTP endpoints before reporting RUNNING.
"""

import os
import sys
import time
import signal
import subprocess
import urllib.request
import json
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
RUN_DIR = BACKEND_DIR / "run"
LOGS_DIR = BACKEND_DIR / "logs"

LEGACY_PID_FILE = RUN_DIR / "legacy.pid"
QLIB_PID_FILE = RUN_DIR / "qlib.pid"

LEGACY_LOG_FILE = LOGS_DIR / "legacy.log"
QLIB_LOG_FILE = LOGS_DIR / "qlib.log"

LEGACY_PORT = int(os.environ.get("LEGACY_BACKEND_PORT", 8000))
QLIB_PORT = int(os.environ.get("QLIB_BACKEND_PORT", 8001))

VENV_PYTHON = BACKEND_DIR / "venv" / "bin" / "python"
if not VENV_PYTHON.exists():
    VENV_PYTHON = Path(sys.executable)

RUN_DIR.mkdir(parents=True, exist_ok=True)
LOGS_DIR.mkdir(parents=True, exist_ok=True)


def is_pid_alive(pid: int) -> bool:
    """Check if process with PID exists."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def get_pid(pid_file: Path) -> int:
    """Reads PID from file if alive."""
    if pid_file.exists():
        try:
            pid = int(pid_file.read_text().strip())
            if is_pid_alive(pid):
                return pid
        except (ValueError, OSError):
            pass
    return 0


def probe_http(url: str, timeout: float = 2.0) -> dict:
    """Probes an HTTP endpoint, returning (is_ok, data)."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "ServiceSupervisor/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status == 200:
                try:
                    return {"ok": True, "data": json.loads(resp.read().decode("utf-8"))}
                except Exception:
                    return {"ok": True, "data": {}}
    except Exception as e:
        return {"ok": False, "error": str(e)}
    return {"ok": False}


def check_legacy_status() -> dict:
    """Verifies Legacy process state and health."""
    pid = get_pid(LEGACY_PID_FILE)
    probe = probe_http(f"http://127.0.0.1:{LEGACY_PORT}/api/scheduler/status")
    
    # Fallback to root if scheduler route is pending
    if not probe.get("ok"):
        probe = probe_http(f"http://127.0.0.1:{LEGACY_PORT}/")

    is_running = probe.get("ok", False)
    return {
        "engine": "LEGACY",
        "status": "RUNNING" if is_running else ("STALE_PID" if pid else "OFFLINE"),
        "pid": pid if pid else None,
        "port": LEGACY_PORT,
        "details": probe.get("data", {})
    }


def check_qlib_status() -> dict:
    """Verifies Qlib process state and health."""
    pid = get_pid(QLIB_PID_FILE)
    probe = probe_http(f"http://127.0.0.1:{QLIB_PORT}/api/qlib/runtime-status")
    
    if not probe.get("ok"):
        probe = probe_http(f"http://127.0.0.1:{QLIB_PORT}/")

    is_running = probe.get("ok", False)
    return {
        "engine": "QLIB",
        "status": "RUNNING" if is_running else ("STALE_PID" if pid else "OFFLINE"),
        "pid": pid if pid else None,
        "port": QLIB_PORT,
        "details": probe.get("data", {})
    }


def start_legacy():
    """Starts Legacy backend on port 8000."""
    status = check_legacy_status()
    if status["status"] == "RUNNING":
        print(f"✅ Legacy backend is ALREADY RUNNING (PID: {status['pid']}, Port: {LEGACY_PORT})")
        return True

    print(f"🚀 Starting Legacy backend on port {LEGACY_PORT}...")
    log_fp = open(LEGACY_LOG_FILE, "a")
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"

    cmd = [
        str(VENV_PYTHON), "-m", "uvicorn",
        "app.main:app",
        "--host", "0.0.0.0",
        "--port", str(LEGACY_PORT)
    ]

    proc = subprocess.Popen(
        cmd,
        cwd=str(BACKEND_DIR),
        stdout=log_fp,
        stderr=subprocess.STDOUT,
        env=env,
        start_new_session=True
    )
    LEGACY_PID_FILE.write_text(str(proc.pid))
    print(f"   Spawned Legacy process (PID: {proc.pid})")

    # Poll for liveness (up to 15s)
    for _ in range(15):
        time.sleep(1)
        st = check_legacy_status()
        if st["status"] == "RUNNING":
            print(f"✅ Legacy backend ONLINE (PID: {proc.pid}, Port: {LEGACY_PORT})")
            return True

    print(f"⚠️ Legacy backend failed to respond within 15s. Check log: {LEGACY_LOG_FILE}")
    return False


def stop_legacy():
    """Gracefully stops Legacy backend via SIGTERM."""
    pid = get_pid(LEGACY_PID_FILE)
    if not pid:
        print("ℹ️ Legacy backend is not running.")
        if LEGACY_PID_FILE.exists():
            LEGACY_PID_FILE.unlink()
        return True

    print(f"🛑 Stopping Legacy backend (PID: {pid})...")
    try:
        os.kill(pid, signal.SIGTERM)
        for _ in range(25):
            time.sleep(0.2)
            if not is_pid_alive(pid):
                break
        if is_pid_alive(pid):
            print(f"⚠️ Process {pid} did not exit cleanly. Sending SIGKILL...")
            os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass

    if LEGACY_PID_FILE.exists():
        LEGACY_PID_FILE.unlink()
    print("✅ Legacy backend stopped.")
    return True


def start_qlib():
    """Starts Qlib standalone backend on port 8001."""
    status = check_qlib_status()
    if status["status"] == "RUNNING":
        print(f"✅ Qlib backend is ALREADY RUNNING (PID: {status['pid']}, Port: {QLIB_PORT})")
        return True

    print(f"🚀 Starting Qlib backend on port {QLIB_PORT}...")
    log_fp = open(QLIB_LOG_FILE, "a")
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    env["QLIB_BACKEND_PORT"] = str(QLIB_PORT)
    env["MLFLOW_ALLOW_FILE_STORE"] = "true"
    env["MLFLOW_DISABLE_AGENT_HINT"] = "1"

    cmd = [
        str(VENV_PYTHON), "-m", "uvicorn",
        "app.qlib_server:app",
        "--host", "0.0.0.0",
        "--port", str(QLIB_PORT)
    ]

    proc = subprocess.Popen(
        cmd,
        cwd=str(BACKEND_DIR),
        stdout=log_fp,
        stderr=subprocess.STDOUT,
        env=env,
        start_new_session=True
    )
    QLIB_PID_FILE.write_text(str(proc.pid))
    print(f"   Spawned Qlib process (PID: {proc.pid})")

    # Poll for liveness (up to 15s)
    for _ in range(15):
        time.sleep(1)
        st = check_qlib_status()
        if st["status"] == "RUNNING":
            print(f"✅ Qlib backend ONLINE (PID: {proc.pid}, Port: {QLIB_PORT})")
            return True

    print(f"⚠️ Qlib backend failed to respond within 15s. Check log: {QLIB_LOG_FILE}")
    return False


def stop_qlib():
    """Gracefully stops Qlib backend via SIGTERM."""
    pid = get_pid(QLIB_PID_FILE)
    if not pid:
        print("ℹ️ Qlib backend is not running.")
        if QLIB_PID_FILE.exists():
            QLIB_PID_FILE.unlink()
        return True

    print(f"🛑 Stopping Qlib backend (PID: {pid})...")
    try:
        os.kill(pid, signal.SIGTERM)
        for _ in range(25):
            time.sleep(0.2)
            if not is_pid_alive(pid):
                break
        if is_pid_alive(pid):
            print(f"⚠️ Process {pid} did not exit cleanly. Sending SIGKILL...")
            os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass

    if QLIB_PID_FILE.exists():
        QLIB_PID_FILE.unlink()
    print("✅ Qlib backend stopped.")
    return True


def print_status():
    """Prints live status table for both engines."""
    leg = check_legacy_status()
    qlb = check_qlib_status()

    print("\n" + "=" * 60)
    print(" DUAL-SYSTEM RUNTIME STATUS")
    print("=" * 60)
    print(f"LEGACY BACKEND : [{leg['status']}] (PID: {leg.get('pid') or 'N/A'}, Port: {leg['port']})")
    if leg["status"] == "RUNNING":
        sched_st = leg.get("details", {}).get("status", "ONLINE")
        job_cnt = leg.get("details", {}).get("job_count", "N/A")
        print(f"   Scheduler: {sched_st} | Active Jobs: {job_cnt}")

    print(f"QLIB BACKEND   : [{qlb['status']}] (PID: {qlb.get('pid') or 'N/A'}, Port: {qlb['port']})")
    if qlb["status"] == "RUNNING":
        dt = qlb.get("details", {})
        ver = dt.get("qlib_version", "unknown")
        mod_cnt = dt.get("active_models_count", 0)
        print(f"   Qlib Ver: {ver} | Active Models: {mod_cnt} | Sched Jobs: {dt.get('scheduler_job_count', 0)}")
    print("=" * 60 + "\n")


def main():
    action = sys.argv[1] if len(sys.argv) > 1 else "status"

    if action == "status":
        print_status()
    elif action == "start-legacy":
        start_legacy()
    elif action == "stop-legacy":
        stop_legacy()
    elif action == "restart-legacy":
        stop_legacy()
        time.sleep(1)
        start_legacy()
    elif action == "start-qlib":
        start_qlib()
    elif action == "stop-qlib":
        stop_qlib()
    elif action == "restart-qlib":
        stop_qlib()
        time.sleep(1)
        start_qlib()
    elif action == "start-all":
        print("Starting both Legacy and Qlib runtimes...")
        start_legacy()
        start_qlib()
        print_status()
    elif action == "stop-all":
        print("Stopping both runtimes...")
        stop_qlib()
        stop_legacy()
        print_status()
    elif action == "restart-all":
        stop_qlib()
        stop_legacy()
        time.sleep(1)
        start_legacy()
        start_qlib()
        print_status()
    else:
        print(f"Usage: {sys.argv[0]} [status|start-all|stop-all|restart-all|start-legacy|stop-legacy|restart-legacy|start-qlib|stop-qlib|restart-qlib]")
        sys.exit(1)


if __name__ == "__main__":
    main()

