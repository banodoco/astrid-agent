"""Summarize only reported outcomes and compare actual runner inventories."""
import hashlib
import json
import os
from pathlib import Path


TEST = "tests/integrations/test_dry_run_equals_live_run.py::test_dry_run_equals_live_run"
IDS = [f"{TEST}[{cell}]" for cell in ("image/t2i-local-managed", "video/h3-local-managed")]


def summarize(cell):
    root = Path(f"functional-{cell}")
    manifest = root / "inventory.json"
    digest = hashlib.sha256(manifest.read_bytes()).hexdigest() if manifest.exists() else None
    drift = json.loads(manifest.read_text())["original_version_differences"] if digest else None
    receipt = root / "pytest.jsonl"
    events = [json.loads(line) for line in receipt.read_text().splitlines()] if receipt.exists() else []
    selected = [(e["nodeid"], e["timeout"]) for e in events if e["event"] == "selected"]
    reports = [(e["nodeid"], e["phase"], e["outcome"]) for e in events if e["event"] == "phase_end"]
    exit_file = root / "pytest-exit.txt"
    exit_code = int(exit_file.read_text()) if exit_file.exists() else None
    expected = [(nodeid, phase, "passed") for nodeid in IDS for phase in ("setup", "call", "teardown")]
    passed = (selected == [(nodeid, [900]) for nodeid in IDS] and reports == expected and
              any(e["event"] == "session_finish" and e["exitstatus"] == 0 for e in events) and exit_code == 0)
    return {"inventory_sha256": digest, "original_version_differences": drift,
            "pytest_exit": exit_code, "reported_phases": reports, "all_phases_passed": passed}


def main():
    cells = {cell: summarize(cell) for cell in ("reference", "candidate")}
    digests = [row["inventory_sha256"] for row in cells.values()]
    equal = digests[0] is not None and digests[0] == digests[1]
    matched = all(row["original_version_differences"] == {} for row in cells.values())
    report = {"paired_environment": "matched recorded inventories" if equal else "equality unproven",
              "original_logged_versions_matched": matched, "cells": cells}
    text = json.dumps(report, indent=2)
    print(text)
    with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a") as summary:
        summary.write(f"\n```json\n{text}\n```\n")
    return 0 if equal and matched and all(row["all_phases_passed"] for row in cells.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
