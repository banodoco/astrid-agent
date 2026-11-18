"""First-job D1 proof using LS-2's caller clock and process/hash observer.

The standing warm-path collector and its stored references remain unchanged.
Every row here belongs to the first generation after a fresh host admission.
Counter-run times never enter the uninstrumented latency distribution.
"""
from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import platform
import statistics
import sys
import xml.etree.ElementTree as ET

from tests.local_speed_meter_run import fresh_pytest_base
from tests.local_speed_native_capture import run_bounded

PREFIX = "LS12_FIRST_JOB_OBSERVATION "
CALLERS = {"resident_sdk", "fresh_python_cli"}
SELECTOR = "tests/integrations/test_ls12_first_job.py"


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def parse_reports(log: Path, *, instrumented: bool, samples: int) -> list[dict]:
    reports = [json.loads(line[len(PREFIX):])
               for line in log.read_text(encoding="utf-8", errors="replace").splitlines()
               if line.startswith(PREFIX)]
    expected = {(caller, sample) for caller in CALLERS for sample in range(samples)}
    if len(reports) != len(expected) or {
        (row.get("caller"), row.get("sample_index")) for row in reports
    } != expected:
        raise ValueError("every first-job caller/sample is required exactly once")
    for row in reports:
        if row.get("schema") != "astrid.local-speed-observation.v1" or row.get("instrumented") is not instrumented:
            raise ValueError("invalid first-job observation identity/mode")
        duration = row.get("caller_seconds")
        execution = row.get("standin_execution_seconds")
        queue = row.get("standin_queue_seconds")
        if not _finite(duration) or duration <= 0:
            raise ValueError("missing finite positive caller duration")
        for spans in (execution, queue):
            if not isinstance(spans, list) or len(spans) != 1 or not _finite(spans[0]) or not 0 <= spans[0] <= duration:
                raise ValueError("missing bounded observed Comfy interval")
        proof = row.get("proof", {})
        if not proof.get("measured_task_id") or proof.get("verified_states") != ["succeeded"] or "warmup_task_id" in proof:
            raise ValueError("requires the first succeeded task without generation warmup")
        outputs = proof.get("outputs")
        if not isinstance(outputs, list) or not outputs:
            raise ValueError("missing delivered output")
        for item in outputs:
            if (type(item.get("bytes")) is not int or item["bytes"] <= 0
                    or len(item.get("sha256", "")) != 64
                    or any(c not in "0123456789abcdef" for c in item["sha256"])
                    or item.get("dimensions") != [16, 16]):
                raise ValueError("incomplete verified output identity")
        if row.get("attached_http_paths", {}).get("POST /prompt") != 1:
            raise ValueError("first job must submit exactly once")
        if instrumented:
            if row.get("observer_gaps") != [] or not row.get("process_observations"):
                raise ValueError("incomplete process/hash observation")
            if row.get("output_hash_streams", 0) <= 0 or row.get("output_hash_bytes", 0) <= 0:
                raise ValueError("missing observed output hash passes")
            starts = row.get("process_start_counts", {})
            if starts.get("pack_worker", 0) != 1 or starts.get("cli", 0) != (row["caller"] == "fresh_python_cli"):
                raise ValueError("unexpected first-job worker/CLI count")
        row["overhead_seconds"] = duration - execution[0]
        row["queue_time_subtracted"] = False
    return reports


def require_junit(path: Path, samples: int):
    cases = list(ET.parse(path).getroot().iter("testcase"))
    names = {f"test_first_attached_job_observation[{sample}-{caller}]"
             for sample in range(samples) for caller in CALLERS}
    if len(cases) != len(names) or {case.attrib.get("name") for case in cases} != names:
        raise ValueError("missing/duplicate first-job test IDs")
    if any(case.find(tag) is not None for case in cases for tag in ("failure", "error", "skipped")):
        raise ValueError("not every first-job test passed")


def distribution(values: list[float], limit: float) -> dict:
    if not values or any(not _finite(x) or x < 0 for x in values):
        raise ValueError("invalid first-job latency samples")
    ordered = sorted(values)
    # Nearest-rank p99; the slowest value is also reported for the twenty runs.
    return {"samples": len(values), "median": statistics.median(values),
            "p99": ordered[math.ceil(0.99 * len(ordered)) - 1], "slowest": ordered[-1],
            "share_at_or_over_limit": sum(x >= limit for x in values) / len(values),
            "limit_seconds": limit, "median_under_limit": statistics.median(values) < limit,
            "p99_under_limit": ordered[math.ceil(0.99 * len(ordered)) - 1] < limit}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--samples", type=int, choices=range(1, 21), default=20)
    parser.add_argument("--counter-samples", type=int, choices=range(1, 21), default=2)
    parser.add_argument("--timeout", type=int, default=3600)
    parser.add_argument("--require-d1", action="store_true")
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    summary = {"schema": "astrid.ls12-first-job-proof.v1", "status": "NO-RESULT",
               "platform": platform.platform(), "python": sys.version, "budget_certified": False,
               "scope": "attached CPU stand-in; first generation after Runtime/host admission; setup outside caller clock",
               "native_flushes": None, "latency": None, "counters": None}
    try:
        for phase, count in (("latency", args.samples), ("counters", args.counter_samples)):
            command = [sys.executable, "-m", "pytest", "-q", "-s", "-rA", "-m", "integration", SELECTOR,
                       f"--junitxml={output / (phase + '.xml')}", f"--basetemp={fresh_pytest_base()}"]
            environment = dict(os.environ, ASTRID_LS12_SAMPLES=str(count))
            environment.pop("ASTRID_LS12_OBSERVE", None)
            if phase == "counters":
                environment["ASTRID_LS12_OBSERVE"] = "1"
            (output / (phase + '-command.json')).write_text(json.dumps(command, indent=2) + "\n")
            # The existing bounded process runner inherits the caller environment.
            previous = os.environ.copy()
            try:
                os.environ.clear()
                os.environ.update(environment)
                code = run_bounded(command, log=output / (phase + '.log'), timeout=args.timeout)
            finally:
                os.environ.clear()
                os.environ.update(previous)
            (output / (phase + '.exit')).write_text(str(code) + "\n")
            if code:
                raise RuntimeError(f"first-job {phase} failed; original log and exit retained")
            require_junit(output / (phase + '.xml'), count)
            summary[phase] = parse_reports(output / (phase + '.log'), instrumented=phase == "counters", samples=count)
        limit = 5.0 if platform.system() == "Windows" else 3.0
        summary["distributions"] = {caller: distribution(
            [row["overhead_seconds"] for row in summary["latency"] if row["caller"] == caller], limit)
            for caller in sorted(CALLERS)}
        qualified = args.samples == 20 and all(row["median_under_limit"] and row["p99_under_limit"]
                                             for row in summary["distributions"].values())
        summary["budget_certified"] = qualified
        summary["status"] = "PASS" if qualified else "D1-NOT-MET"
        return 0 if qualified or not args.require_d1 else 1
    except (ValueError, OSError, RuntimeError, ET.ParseError) as exc:
        summary["error"] = f"{type(exc).__name__}: {exc}"
        return 1
    finally:
        (output / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    raise SystemExit(main())
