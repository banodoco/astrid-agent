"""Uncatchable test exits and cleanup of subprocesses owned by the caller.

SIGKILL is the Linux crash oracle. Native samples use os._exit so the same
durable boundary can be exercised on Windows without exception unwinding.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys

EXIT_CODE = 86


def crash() -> None:
    if os.environ.get("ASTRID_TEST_CRASH_METHOD", "sigkill") == "exit":
        os._exit(EXIT_CODE)
    os.kill(os.getpid(), signal.SIGKILL)


def crashed(returncode: int) -> bool:
    return returncode == (EXIT_CODE if os.environ.get("ASTRID_TEST_CRASH_METHOD") == "exit"
                          else -signal.SIGKILL)


def terminate_owned_tree(process: subprocess.Popen) -> None:
    """Accept a live Popen handle, never an arbitrary PID or process name."""
    if process.poll() is not None:
        return
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                       capture_output=True, check=False, timeout=10)
    else:
        # run_owned creates this session; no other caller may supply a group.
        if os.getpgid(process.pid) != process.pid:
            raise ValueError("crash subprocess does not own its process group")
        os.killpg(process.pid, signal.SIGKILL)
    process.wait(timeout=10)


def run_owned(args, *, timeout: float, **kwargs) -> subprocess.CompletedProcess:
    """Bound the child and its descendants without touching ambient workers."""
    options = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if sys.platform == "win32"
               else {"start_new_session": True})
    with subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          text=True, **options, **kwargs) as process:
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except BaseException:
            terminate_owned_tree(process)
            raise
        return subprocess.CompletedProcess(args, process.returncode, stdout, stderr)
