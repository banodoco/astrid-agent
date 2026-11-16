"""Run the actual attached meter twice: unobserved timing and native/Python counts.

The two runs never contribute times to one combined distribution. Output/task
checks live in the existing integration test; this collector retains them and
requires complete report coverage before publishing counts. No budget is
certified until a separately recorded baseline and design budget exist.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET

from tests._local_speed_native import count_interval, trace_etw, trace_strace
from tests.local_speed_native_capture import capture, run_bounded

REPORT_PREFIX = "LOCAL_SPEED_OBSERVATION "
CALLERS = {"resident_sdk", "fresh_python_cli"}
SELECTOR = "tests/integrations/test_local_speed_meter.py"


def parse_reports(log: Path, *, instrumented: bool) -> list[dict]:
    reports = []
    for line in log.read_text(errors="replace").splitlines():
        if line.startswith(REPORT_PREFIX):
            try:
                row = json.loads(line[len(REPORT_PREFIX):])
            except (ValueError, TypeError) as error:
                raise ValueError("malformed attached meter report") from error
            reports.append(row)
    if len(reports) != 2 or {row.get("caller") for row in reports} != CALLERS:
        raise ValueError("both distinct caller reports are required exactly once")
    for row in reports:
        if row.get("instrumented") is not instrumented:
            raise ValueError("latency/counter report mode mismatch")
        if row.get("schema") != "astrid.local-speed-observation.v1":
            raise ValueError("wrong observation schema")
        duration = row.get("caller_seconds")
        execution = row.get("standin_execution_seconds")
        if not isinstance(duration, (float, int)) or duration <= 0:
            raise ValueError("missing positive caller duration")
        if not isinstance(execution, list) or len(execution) != 1 or not 0 <= execution[0] <= duration:
            raise ValueError("missing bounded Comfy execution span")
        proof = row.get("proof", {})
        if (not proof.get("warmup_task_id") or not proof.get("measured_task_id")
                or proof["warmup_task_id"] == proof["measured_task_id"]
                or proof.get("verified_states") != ["succeeded", "succeeded"]):
            raise ValueError("meter lacks two distinct succeeded task records")
        outputs = proof.get("outputs", [])
        if {item.get("phase") for item in outputs} != {"warmup", "delivered"}:
            raise ValueError("meter lacks verified warmup and measured outputs")
        for item in outputs:
            if (not isinstance(item.get("bytes"), int) or item["bytes"] <= 0
                    or len(item.get("sha256", "")) != 64 or not item.get("dimensions")):
                raise ValueError("incomplete output identity")
        if row.get("attached_http_paths", {}).get("POST /prompt") != 1:
            raise ValueError("measured job must submit exactly once")
        if instrumented:
            if row.get("observer_gaps") != [] or not row.get("process_observations"):
                raise ValueError("Python counter coverage is incomplete")
            if row.get("output_hash_streams", 0) <= 0:
                raise ValueError("output hashing was not observed")
            starts = row.get("process_start_counts", {})
            if starts.get("pack_worker", 0) != 1:
                raise ValueError("baseline must observe exactly one pack worker start")
            if starts.get("cli", 0) != (1 if row["caller"] == "fresh_python_cli" else 0):
                raise ValueError("baseline CLI start count mismatch")
    return reports


def require_junit(path: Path) -> dict:
    root = ET.parse(path).getroot()
    suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
    counts = {name: sum(int(s.get(name, "0")) for s in suites)
              for name in ("tests", "failures", "errors", "skipped")}
    if counts != {"tests": 2, "failures": 0, "errors": 0, "skipped": 0}:
        raise ValueError(f"actual meter tests did not both pass: {counts}")
    return counts


def fresh_pytest_base() -> Path:
    """Own a short empty path; never point pytest's reset at an existing tree."""
    parent = Path(os.environ.get("RUNNER_TEMP") or tempfile.gettempdir()).resolve()
    return Path(tempfile.mkdtemp(prefix="p55-", dir=parent))


def meter_command(output: Path, phase: str) -> list[str]:
    if phase not in {"latency", "counters"}:
        raise ValueError("unknown meter phase")
    base = fresh_pytest_base()
    return [sys.executable, "-m", "pytest", "-q", "-s", "-rA", "-m", "integration",
            SELECTOR, f"--junitxml={output / (phase + '.xml')}", f"--basetemp={base}"]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--timeout", type=int, default=1800)
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    summary = {"schema": "astrid.local-speed-meter-proof.v1", "status": "NO-RESULT",
               "platform": platform.platform(), "python": sys.version,
               "budget_certified": False, "latency": None, "counters": None,
               "scope": "attached stand-in through real SDK/CLI, Runtime, host and worker; not GPU proof"}
    original_mode = os.environ.pop("ASTRID_LS2_OBSERVE", None)
    try:
        latency_command = meter_command(output, "latency")
        (output / "latency-command.json").write_text(json.dumps(latency_command, indent=2) + "\n")
        latency_exit = run_bounded(latency_command, log=output / "latency.log", timeout=args.timeout)
        (output / "latency.exit").write_text(str(latency_exit) + "\n")
        if latency_exit:
            raise RuntimeError("uninstrumented attached meter failed; retained latency.log")
        summary["latency_junit"] = require_junit(output / "latency.xml")
        summary["latency"] = parse_reports(output / "latency.log", instrumented=False)
        for row in summary["latency"]:
            row["overhead_seconds"] = row["caller_seconds"] - row["standin_execution_seconds"][0]
            row["queue_time_subtracted"] = False
        os.environ["ASTRID_LS2_OBSERVE"] = "1"
        counter_command = meter_command(output, "counters")
        (output / "counter-command.json").write_text(json.dumps(counter_command, indent=2) + "\n")
        captured = capture(output / "native", counter_command, args.timeout)
        summary["counter_capture"] = captured
        if not captured["completed"] or captured.get("proof_exit") != 0:
            raise RuntimeError("instrumented attached meter/capture failed; retained native/proof.log")
        summary["counter_junit"] = require_junit(output / "counters.xml")
        summary["counters"] = parse_reports(output / "native/proof.log", instrumented=True)
        trace = (trace_etw(output / "native/trace.xml", output / "native/trace-summary.txt")
                 if os.name == "nt" else trace_strace(output / "native"))
        (output / "native-events.json").write_text(json.dumps(trace, indent=2) + "\n")
        for row in summary["counters"]:
            native = count_interval(trace, row["interval"])
            row["native_observation"] = native
            row["native_flushes"] = native["native_flushes"]
            if native["native_flushes"] is None or native["observed_failed_flushes"]:
                raise RuntimeError("native counter is incomplete or records failed flushes")
        summary["status"] = "PASS"
    except (OSError, ValueError, RuntimeError, ET.ParseError) as error:
        summary["error"] = f"{type(error).__name__}: {error}"
    finally:
        if original_mode is None:
            os.environ.pop("ASTRID_LS2_OBSERVE", None)
        else:
            os.environ["ASTRID_LS2_OBSERVE"] = original_mode
        summary["finished_epoch_ns"] = time.time_ns()
        (output / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps({k: v for k, v in summary.items() if k not in {"latency", "counters"}}, indent=2))
    return 0 if summary["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
