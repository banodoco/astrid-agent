"""Twenty separate instrumented first jobs per caller; never a D1 pass claim."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import platform
import sys
import xml.etree.ElementTree as ET

from tests.local_speed_meter_run import fresh_pytest_base
from tests.local_speed_native_capture import run_bounded
from tests.ls12_first_job_run import SELECTOR, parse_reports, require_junit
from tests.ls12_phases import NOTES, breakdown, summarize


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--samples", type=int, choices=range(1, 21), default=20)
    parser.add_argument("--timeout", type=int, default=3600)
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    summary = {"schema": "astrid.ls12-phase-proof.v1", "status": "NO-RESULT", "budget_certified": False,
               "platform": platform.platform(), "python": sys.version, "notes": NOTES}
    try:
        command = [sys.executable, "-m", "pytest", "-q", "-s", "-rA", "-m", "integration", SELECTOR,
                   f"--junitxml={output / 'phases.xml'}", f"--basetemp={fresh_pytest_base()}"]
        (output / "command.json").write_text(json.dumps(command, indent=2) + "\n")
        previous = os.environ.copy()
        try:
            os.environ.update(ASTRID_LS12_SAMPLES=str(args.samples), ASTRID_LS12_OBSERVE="1", ASTRID_LS12_PHASES="1")
            code = run_bounded(command, log=output / "phases.log", timeout=args.timeout)
        finally:
            os.environ.clear()
            os.environ.update(previous)
        (output / "phases.exit").write_text(str(code) + "\n")
        if code:
            raise RuntimeError("phase observation failed; original logs, partial spans and exit retained")
        require_junit(output / "phases.xml", args.samples)
        reports = parse_reports(output / "phases.log", instrumented=True, samples=args.samples)
        summary["jobs"] = [breakdown(row) for row in reports]
        summary["distributions"] = summarize(summary["jobs"])
        summary["status"] = "OBSERVED" if args.samples == 20 else "DIAGNOSTIC-SAMPLE-ONLY"
        return 0
    except (ValueError, OSError, RuntimeError, ET.ParseError, KeyError, TypeError) as exc:
        summary["error"] = f"{type(exc).__name__}: {exc}"
        return 1
    finally:
        (output / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(json.dumps({key: value for key, value in summary.items() if key != "jobs"}, indent=2, sort_keys=True))


if __name__ == "__main__":
    raise SystemExit(main())
