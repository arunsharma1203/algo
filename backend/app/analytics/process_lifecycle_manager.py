import os
import signal
import time
import logging
import atexit
import subprocess
from typing import Dict, List, Optional, Any
from concurrent.futures import ProcessPoolExecutor

logger = logging.getLogger(__name__)

class ProcessLifecycleManager:
    """
    Centralized process and worker lifecycle manager for the platform.
    Guarantees deterministic termination of worker pools, prevents orphaned
    multiprocessing child processes (PPID=1), and provides clean shutdown hooks.
    """
    _active_pools: Dict[str, Dict[str, Any]] = {}
    _registered_handlers = False

    @classmethod
    def register_shutdown_handlers(cls):
        """Registers atexit and cooperative signal handlers once."""
        if cls._registered_handlers:
            return
        
        atexit.register(cls.terminate_all_pools)
        
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                prev_handler = signal.getsignal(sig)
                def _make_handler(prev):
                    def _handler(signum, frame):
                        cls.terminate_all_pools()
                        if callable(prev):
                            prev(signum, frame)
                        elif prev == signal.SIG_DFL:
                            signal.signal(signum, signal.SIG_DFL)
                            os.kill(os.getpid(), signum)
                    return _handler
                
                signal.signal(sig, _make_handler(prev_handler))
            except (ValueError, AttributeError):
                pass  # In non-main thread or unsupported environment

        cls._registered_handlers = True

    @classmethod
    def register_worker_pool(cls, pool_id: str, executor: Optional[ProcessPoolExecutor] = None, pids: Optional[List[int]] = None):
        """Registers an active worker pool and its OS process IDs for lifecycle tracking."""
        cls.register_shutdown_handlers()
        
        tracked_pids = list(pids) if pids else []
        if executor and hasattr(executor, '_processes'):
            tracked_pids.extend([p.pid for p in executor._processes.values() if p and p.pid])

        cls._active_pools[pool_id] = {
            "executor": executor,
            "pids": set(tracked_pids),
            "created_at": time.time()
        }
        logger.info(f"Registered worker pool '{pool_id}' with {len(cls._active_pools[pool_id]['pids'])} tracked PIDs.")

    @classmethod
    def terminate_worker_pool(cls, pool_id: str):
        """Deterministically terminates a specific worker pool via SIGTERM then SIGKILL."""
        pool_info = cls._active_pools.pop(pool_id, None)
        if not pool_info:
            return

        pids = list(pool_info.get("pids", []))
        executor = pool_info.get("executor")

        # 1. Shutdown executor futures
        if executor:
            try:
                executor.shutdown(wait=False, cancel_futures=True)
            except Exception as e:
                logger.warning(f"Error shutting down executor for pool '{pool_id}': {e}")

        # 2. Terminate worker processes
        for pid in pids:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            except Exception as e:
                logger.debug(f"Error sending SIGTERM to PID {pid}: {e}")

        # Brief grace period
        time.sleep(0.2)

        # 3. Force kill any stubborn workers
        for pid in pids:
            try:
                # Check if still alive
                os.kill(pid, 0)
                os.kill(pid, signal.SIGKILL)
                logger.warning(f"Sent SIGKILL to stubborn worker PID {pid} from pool '{pool_id}'.")
            except (ProcessLookupError, OSError):
                pass

        logger.info(f"Worker pool '{pool_id}' terminated successfully.")

    @classmethod
    def terminate_all_pools(cls):
        """Terminates every registered worker pool."""
        pool_ids = list(cls._active_pools.keys())
        for pid_name in pool_ids:
            cls.terminate_worker_pool(pid_name)

    @classmethod
    def cleanup_orphaned_python_workers(cls) -> int:
        """
        Scans the operating system for orphaned multiprocessing worker processes
        launched from the project virtualenv whose parent process died (PPID=1),
        and safely terminates them.
        """
        terminated_count = 0
        try:
            cmd = ["ps", "-eo", "pid,ppid,command"]
            proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True)
            lines = proc.stdout.strip().split("\n")

            current_pid = os.getpid()

            for line in lines[1:]:
                parts = line.strip().split(None, 2)
                if len(parts) < 3:
                    continue
                pid_str, ppid_str, cmd_str = parts[0], parts[1], parts[2]
                
                try:
                    pid = int(pid_str)
                    ppid = int(ppid_str)
                except ValueError:
                    continue

                if pid == current_pid:
                    continue

                # Match orphaned multiprocessing workers created by spawn_main
                if ppid == 1 and ("spawn_main" in cmd_str and "--multiprocessing-fork" in cmd_str):
                    if "swing trade react" in cmd_str or "backend/venv" in cmd_str:
                        logger.warning(f"Reaping orphaned worker PID {pid} (PPID=1, command: {cmd_str[:60]}...)")
                        try:
                            os.kill(pid, signal.SIGTERM)
                            terminated_count += 1
                            time.sleep(0.1)
                            try:
                                os.kill(pid, signal.SIGKILL)
                            except ProcessLookupError:
                                pass
                        except ProcessLookupError:
                            pass
                        except Exception as e:
                            logger.error(f"Failed to kill orphaned worker PID {pid}: {e}")

        except Exception as e:
            logger.error(f"Error during orphaned worker scan: {e}")

        if terminated_count > 0:
            logger.info(f"ProcessLifecycleManager reaped {terminated_count} orphaned Python multiprocessing workers.")
        return terminated_count

