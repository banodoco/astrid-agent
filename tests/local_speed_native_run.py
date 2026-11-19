"""Calibrate actual native visibility before using the LS-2 counter on a job."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

from tests._local_speed_native import count_interval, trace_etw, trace_strace
from tests.local_speed_native_capture import capture


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    command = [sys.executable, "-m", "tests.local_speed_native_calibration",
               "--output", str(output / "calibration")]
    meta = capture(output / "trace", command, timeout=120)
    summary = {"capture": meta, "status": "NO-RESULT", "native_count": None,
               "scope": "native reader calibration, not generation speed or LS-2 completion"}
    interval_path = output / "calibration/calibration-interval.json"
    if meta["completed"] and meta.get("proof_exit") == 0 and interval_path.is_file():
        trace = (trace_etw(output / "trace/trace.xml", output / "trace/trace-summary.txt")
                 if os.name == "nt" else trace_strace(output / "trace"))
        counts = count_interval(trace, json.loads(interval_path.read_text()))
        summary["native_count"] = counts
        # The child has exactly one observed Python fsync and real FULL SQLite
        # transactions. More than one native flush proves the native observer
        # sees work the Python fsync wrapper cannot see.
        if counts["native_flushes"] is not None:
            summary["status"] = "PASS" if counts["native_flushes"] > 1 and not counts["observed_failed_flushes"] else "FAIL"
        (output / "decoded-events.json").write_text(json.dumps(trace, indent=2) + "\n")
        wal = json.loads((output / "calibration/wal-calibration.json").read_text())
        summary["wal_calibration"] = wal
        if os.name != "nt":
            observed = count_interval(trace, wal["interval"])
            wal_events = [row for row in observed["events"] if row.get("path") == wal["database"] + "-wal"]
            checkpoint_events = [row for row in observed["events"] if row.get("path") == wal["database"]]
            qualified = (observed["native_flushes"] is not None and not observed["observed_failed_flushes"]
                         and wal_events and checkpoint_events)
            summary["wal_native"] = {"counts": observed, "wal_syncs": len(wal_events),
                                      "checkpoint_database_syncs": len(checkpoint_events)}
            if not qualified:
                summary["status"] = "FAIL"
    (output / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary, indent=2))
    return 0 if summary["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
