"""Run repeated unobserved timing separately from the standing counters.

The two runs never contribute times to one combined distribution. Output/task
checks live in the existing integration test; this collector retains them and
requires complete report coverage before publishing counts. No budget is
certified until the design budget exists. A reference baseline gates regressions
in today's path. Native Windows tracing is opt-in LS-13 evidence, not an LS-2
prerequisite; the absence of a trace stays unknown in every report.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from pathlib import Path

from tests._local_speed_baseline import approve_reference, compare_baseline, make_baseline
from tests._local_speed_native import attribute_writes, count_interval, trace_etw, trace_strace
from tests.local_speed_native_capture import capture, run_bounded

REPORT_PREFIX = "LOCAL_SPEED_OBSERVATION "
CALLERS = {"resident_sdk", "fresh_python_cli"}
SELECTOR = "tests/integrations/test_local_speed_meter.py"


def parse_reports(log: Path, *, instrumented: bool, samples: int = 1, require_gate: bool = False) -> list[dict]:
    reports = []
    for line in log.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith(REPORT_PREFIX):
            try:
                row = json.loads(line[len(REPORT_PREFIX) :])
            except (ValueError, TypeError) as error:
                raise ValueError("malformed attached meter report") from error
            reports.append(row)
    expected = {(caller, sample) for caller in CALLERS for sample in range(samples)}
    if (
        len(reports) != 2 * samples
        or {(row.get("caller"), row.get("sample_index", 0)) for row in reports} != expected
    ):
        raise ValueError("both distinct caller reports are required exactly once per sample")
    for row in reports:
        if row.get("instrumented") is not instrumented:
            raise ValueError("latency/counter report mode mismatch")
        if row.get("schema") != "astrid.local-speed-observation.v1":
            raise ValueError("wrong observation schema")
        if require_gate and (row.get("gate_version") != 3 or row.get("harness_settings") != {
            "host_poll_seconds": "product_default", "machine_poll_seconds": "product_default",
            "sdk_poll_seconds": "product_default",
        }):
            raise ValueError("standing gate requires the product-default cell")
        duration = row.get("caller_seconds")
        execution = row.get("standin_execution_seconds")
        if not isinstance(duration, (float, int)) or duration <= 0:
            raise ValueError("missing positive caller duration")
        if (
            not isinstance(execution, list)
            or len(execution) != 1
            or not 0 <= execution[0] <= duration
        ):
            raise ValueError("missing bounded Comfy execution span")
        proof = row.get("proof", {})
        if (
            not proof.get("warmup_task_id")
            or not proof.get("measured_task_id")
            or proof["warmup_task_id"] == proof["measured_task_id"]
            or proof.get("verified_states") != ["succeeded", "succeeded"]
        ):
            raise ValueError("meter lacks two distinct succeeded task records")
        outputs = proof.get("outputs", [])
        if {item.get("phase") for item in outputs} != {"warmup", "delivered"}:
            raise ValueError("meter lacks verified warmup and measured outputs")
        for item in outputs:
            if (
                not isinstance(item.get("bytes"), int)
                or item["bytes"] <= 0
                or len(item.get("sha256", "")) != 64
                or not item.get("dimensions")
            ):
                raise ValueError("incomplete output identity")
        if row.get("attached_http_paths", {}).get("POST /prompt") != 1:
            raise ValueError("measured job must submit exactly once")
        if instrumented:
            if row.get("observer_gaps") != [] or not row.get("process_observations"):
                raise ValueError("Python counter coverage is incomplete")
            if row.get("output_hash_streams", 0) <= 0:
                raise ValueError("output hashing was not observed")
            starts = row.get("process_start_counts", {})
            worker_starts = starts.get("pack_worker")
            if type(worker_starts) is not int or worker_starts < 0:
                raise ValueError("pack worker start count must be observed")
            if worker_starts == 0:
                interval = row.get("interval", {})
                expected_interval = {key: interval.get(key) for key in (
                    "monotonic_start_ns", "monotonic_reply_ns")}
                residents = [child for child in row["process_observations"] if child.get("role") == "pack_worker"
                             and child.get("interval") == expected_interval
                             and type(child.get("start_ns")) is int
                             and child["start_ns"] < interval.get("monotonic_start_ns", 0)
                             and not child.get("coverage_gaps") and "counts" in child]
                if not residents:
                    raise ValueError("zero starts requires interval evidence from the resident pack worker")
            if starts.get("cli", 0) != (1 if row["caller"] == "fresh_python_cli" else 0):
                raise ValueError("baseline CLI start count mismatch")
    return reports


def require_junit(path: Path, *, samples: int = 1) -> dict:
    root = ET.parse(path).getroot()
    suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
    counts = {
        name: sum(int(s.get(name, "0")) for s in suites)
        for name in ("tests", "failures", "errors", "skipped")
    }
    if counts != {"tests": 2 * samples, "failures": 0, "errors": 0, "skipped": 0}:
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
    return [
        sys.executable,
        "-m",
        "pytest",
        "-q",
        "-s",
        "-rA",
        "-m",
        "integration",
        SELECTOR,
        f"--junitxml={output / (phase + '.xml')}",
        f"--basetemp={base}",
    ]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--timeout", type=int, default=1800)
    parser.add_argument("--samples", type=int, choices=range(1, 21), default=5)
    parser.add_argument("--counter-samples", type=int, choices=range(1, 21), default=2)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--paired-reference", type=Path)
    parser.add_argument("--reference-output", type=Path)
    parser.add_argument("--counter-vocabulary", type=Path,
                        help="retain explicit observed zeroes for reference request paths")
    parser.add_argument(
        "--native",
        choices=("linux", "all", "none"),
        default="linux",
        help="all retains strict native Windows tracing for LS-13",
    )
    args = parser.parse_args()
    if args.reference_output and not args.baseline:
        parser.error("a replacement reference requires the old --baseline")
    if args.baseline and platform.system() == "Windows" and not args.paired_reference:
        parser.error("Windows gating requires a reference measured on this runner")
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    summary = {
        "schema": "astrid.local-speed-meter-proof.v1",
        "status": "NO-RESULT",
        "platform": platform.platform(),
        "python": sys.version,
        "budget_certified": False,
        "reference_eligible": False,
        "latency": None,
        "counters": None,
        "scope": "attached stand-in through real SDK/CLI, Runtime, host and worker; not GPU proof",
    }
    original_mode = os.environ.pop("ASTRID_LS2_OBSERVE", None)
    original_samples = os.environ.get("ASTRID_LS2_SAMPLES")
    try:
        os.environ["ASTRID_LS2_SAMPLES"] = str(args.samples)
        latency_command = meter_command(output, "latency")
        (output / "latency-command.json").write_text(json.dumps(latency_command, indent=2) + "\n")
        latency_exit = run_bounded(
            latency_command, log=output / "latency.log", timeout=args.timeout
        )
        (output / "latency.exit").write_text(str(latency_exit) + "\n")
        if latency_exit:
            raise RuntimeError("uninstrumented attached meter failed; retained latency.log")
        summary["latency_junit"] = require_junit(output / "latency.xml", samples=args.samples)
        summary["latency"] = parse_reports(
            output / "latency.log", instrumented=False, samples=args.samples, require_gate=True
        )
        for row in summary["latency"]:
            row["overhead_seconds"] = row["caller_seconds"] - row["standin_execution_seconds"][0]
            row["queue_time_subtracted"] = False
        os.environ["ASTRID_LS2_OBSERVE"] = "1"
        os.environ["ASTRID_LS2_SAMPLES"] = str(args.counter_samples)
        counter_command = meter_command(output, "counters")
        (output / "counter-command.json").write_text(json.dumps(counter_command, indent=2) + "\n")
        native_enabled = args.native == "all" or (
            args.native == "linux" and sys.platform.startswith("linux")
        )
        counter_log = output / "counters.log"
        if native_enabled:
            captured = capture(output / "native", counter_command, args.timeout)
            summary["counter_capture"] = captured
            counter_log = output / "native/proof.log"
            if not captured["completed"] or captured.get("proof_exit") != 0:
                raise RuntimeError(
                    "instrumented attached meter/capture failed; retained native/proof.log"
                )
        else:
            counter_exit = run_bounded(counter_command, log=counter_log, timeout=args.timeout)
            summary["counter_capture"] = {
                "observer": "Python process observer",
                "proof_exit": counter_exit,
                "completed": counter_exit != 124,
            }
            if counter_exit:
                raise RuntimeError("instrumented attached meter failed; retained counters.log")
        summary["counter_junit"] = require_junit(
            output / "counters.xml", samples=args.counter_samples
        )
        summary["counters"] = parse_reports(
            counter_log, instrumented=True, samples=args.counter_samples, require_gate=True
        )
        if native_enabled:
            trace = (
                trace_etw(output / "native/trace.xml", output / "native/trace-summary.txt")
                if os.name == "nt"
                else trace_strace(output / "native")
            )
            (output / "native-events.json").write_text(json.dumps(trace, indent=2) + "\n")
            for row in summary["counters"]:
                native = count_interval(trace, row["interval"])
                row["native_observation"] = native
                row["native_flushes"] = native["native_flushes"]
                if native["native_flushes"] is None or native["observed_failed_flushes"]:
                    raise RuntimeError("native counter is incomplete or records failed flushes")
                row["native_attribution"] = attribute_writes(native, row)
                if row["native_attribution"]["status"] != "PASS":
                    raise RuntimeError("native write attribution is incomplete; retained events")
        summary["native_flush_coverage"] = (
            "completed OS operations"
            if native_enabled
            else (
                "unobserved; Windows native trace belongs to LS-13"
                if os.name == "nt"
                else "unobserved; native capture disabled"
            )
        )
        source_paths = {
            "agent": ".",
            "runtime": os.environ.get("ASTRID_RUNTIME_CHECKOUT"),
            "vibecomfy": os.environ.get("ASTRID_VIBECOMFY_CHECKOUT"),
            "lifecycle": os.environ.get("ASTRID_TEST_LIFECYCLE_CHECKOUT"),
        }
        identities = {
            name: subprocess.check_output(
                ["git", "-C", path, "rev-parse", "HEAD"], text=True
            ).strip()
            for name, path in source_paths.items()
            if path
        }
        baseline = make_baseline(
            summary["latency"], summary["counters"], system=platform.system(), sources=identities,
            counter_vocabulary=json.loads(args.counter_vocabulary.read_text(encoding="utf-8")) if args.counter_vocabulary else None,
        )
        (output / "baseline.json").write_text(
            json.dumps(baseline, indent=2) + "\n", encoding="utf-8"
        )
        summary["baseline"] = baseline
        summary["baseline_status"] = "RECORDED; reference comparison not requested"
        if args.baseline:
            reference = json.loads(args.baseline.read_text(encoding="utf-8"))
            paired = json.loads(args.paired_reference.read_text(encoding="utf-8")) if args.paired_reference else None
            summary["baseline_regressions"] = compare_baseline(baseline, reference, coverage_reference=paired)
            summary["baseline_reference"] = str(args.baseline)
            if summary["baseline_regressions"]:
                raise RuntimeError("standing baseline regression; see baseline_regressions")
            summary["baseline_status"] = "PASS"
            approve_reference(baseline, reference, coverage_reference=paired)
            summary["reference_eligible"] = True
            if args.reference_output:
                with args.reference_output.open("x", encoding="utf-8") as handle:
                    handle.write(json.dumps(baseline, indent=2) + "\n")
        summary["status"] = "PASS"
    except (OSError, ValueError, RuntimeError, ET.ParseError) as error:
        summary["error"] = f"{type(error).__name__}: {error}"
    finally:
        if original_mode is None:
            os.environ.pop("ASTRID_LS2_OBSERVE", None)
        else:
            os.environ["ASTRID_LS2_OBSERVE"] = original_mode
        if original_samples is None:
            os.environ.pop("ASTRID_LS2_SAMPLES", None)
        else:
            os.environ["ASTRID_LS2_SAMPLES"] = original_samples
        summary["finished_epoch_ns"] = time.time_ns()
        (output / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(
        json.dumps({k: v for k, v in summary.items() if k not in {"latency", "counters"}}, indent=2)
    )
    return 0 if summary["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
