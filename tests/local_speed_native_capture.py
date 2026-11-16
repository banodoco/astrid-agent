"""Run ONE owned CPU proof under a native observer, preserving raw failures.

This helper never changes product code, credentials, permissions, or another
trace session. Native tracing is instrumentation, not latency measurement.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import platform
import shutil
import signal
import subprocess
import sys
import time

# Offline conversion of a finished ETL is not part of the caller's timer.
# The real Windows capture completed its tests but the old 180-second decode
# stopped at about 46%. Keep conversion bounded without changing proof limits.
TRACE_DECODE_TIMEOUT_SECONDS = 900


def run_bounded(command, *, log: Path, timeout: int) -> int:
    options = {"start_new_session": True} if os.name != "nt" else {
        "creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    with log.open("wb") as handle:
        process = subprocess.Popen(command, stdout=handle, stderr=subprocess.STDOUT, **options)
        try:
            return process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            handle.write(b"\nPLAN55_NATIVE_TIMEOUT: incomplete proof\n")
            if process.poll() is None:
                if os.name == "nt":
                    subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                                   stdout=handle, stderr=subprocess.STDOUT, timeout=30)
                else:
                    os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=30)
            return 124


def decode_saved_trace(output: Path, *, tracerpt: str | None = None,
                       timeout: int = TRACE_DECODE_TIMEOUT_SECONDS) -> dict:
    """Decode an existing completed capture; never start or repeat a proof.

    The caller must separately require successful proof/stop statuses and
    complete trace/loss accounting. A decoded file alone qualifies nothing.
    Original ETL bytes are input-only; existing diagnostic output may be
    replaced by tracerpt's explicit -y option on this owned proof directory.
    """
    if isinstance(timeout, bool) or not isinstance(timeout, int) or not 1 <= timeout <= TRACE_DECODE_TIMEOUT_SECONDS:
        raise ValueError("decode timeout must be an integer between 1 and 900 seconds")
    output = Path(output)
    if not (output / "trace.etl").is_file():
        raise FileNotFoundError("completed input trace.etl is required")
    tracerpt = tracerpt or shutil.which("tracerpt")
    if not tracerpt:
        raise RuntimeError("tracerpt unavailable; native flush count is unknown")
    command = [tracerpt, str(output / "trace.etl"), "-of", "XML", "-o", str(output / "trace.xml"),
               "-summary", str(output / "trace-summary.txt"), "-y"]
    result = {"decode_command": command, "decode_timeout_seconds": timeout,
              "decode_started_epoch_ns": time.time_ns(), "decode_completed": False}
    try:
        result["decode_exit"] = run_bounded(command, log=output / "decode.log", timeout=timeout)
        result["decode_completed"] = result["decode_exit"] == 0
    finally:
        result["decode_finished_epoch_ns"] = time.time_ns()
        (output / "decode.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return result


def capture(output: Path, command: list[str], timeout: int) -> dict:
    if not command:
        raise ValueError("native capture requires an actual proof command")
    output.mkdir(parents=True, exist_ok=False)
    meta = {"schema": "astrid.local-speed-native-capture.v1", "command": command,
            "platform": platform.platform(), "python": sys.version,
            "proof_timeout_seconds": timeout,
            "started_epoch_ns": time.time_ns(), "completed": False}
    try:
        if sys.platform.startswith("linux"):
            tracer = shutil.which("strace")
            if not tracer:
                raise RuntimeError("strace is unavailable; native flush count is unknown")
            traced = [tracer, "-ff", "-ttt", "-T", "-e", "trace=process,fsync,fdatasync",
                      "-o", str(output / "trace"), "--", *command]
            meta["trace_command"] = traced
            meta["proof_exit"] = run_bounded(traced, log=output / "proof.log", timeout=timeout)
            meta["completed"] = meta["proof_exit"] != 124
            meta["observer"] = "strace"
        elif os.name == "nt":
            wpr, tracerpt = shutil.which("wpr"), shutil.which("tracerpt")
            if not wpr or not tracerpt:
                raise RuntimeError("WPR/tracerpt unavailable; native flush count is unknown")
            profile = Path(__file__).with_name("local_speed_native.wprp").resolve()
            start = [wpr, "-start", str(profile) + "!Plan55", "-filemode"]
            meta["start_command"] = start
            meta["start_exit"] = run_bounded(start, log=output / "start.log", timeout=60)
            if meta["start_exit"]:
                raise RuntimeError("WPR start refused or failed; see start.log; not retried")
            try:
                meta["proof_exit"] = run_bounded(command, log=output / "proof.log", timeout=timeout)
            finally:
                stop = [wpr, "-stop", str(output / "trace.etl"), "Astrid Plan 55 owned CPU proof"]
                meta["stop_command"] = stop
                meta["stop_exit"] = run_bounded(stop, log=output / "stop.log", timeout=120)
            if meta["stop_exit"]:
                raise RuntimeError("WPR stop did not produce a completed trace; see stop.log")
            meta.update(decode_saved_trace(output, tracerpt=tracerpt))
            meta["completed"] = meta["decode_exit"] == 0 and meta["proof_exit"] != 124
            meta["observer"] = "WPR FileIO/FileIOInit"
        else:
            raise RuntimeError("native observer is implemented only for Linux and Windows")
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        meta["error"] = f"{type(error).__name__}: {error}"
    finally:
        meta["finished_epoch_ns"] = time.time_ns()
        (output / "capture.json").write_text(json.dumps(meta, indent=2) + "\n")
    return meta


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--timeout", type=int, default=1200)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    meta = capture(args.output.resolve(), command, args.timeout)
    print(json.dumps(meta, indent=2))
    return 0 if meta["completed"] and meta.get("proof_exit") == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
