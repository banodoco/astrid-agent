"""Shared exact-count gate for the Ruff and mypy baselines (testing plan, Owned debt).

Both gates compare a finding count with the committed baseline. The baseline is exact, like
`compare_structure_baseline`: a higher count is a regression, and a lower count fails until the
baseline is rewritten with `--write-baseline`, so deleted code cannot leave slack for a later
regression to fill.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any


def verdict(label: str, current: int, baseline: int) -> tuple[int, str]:
    """Return (exit code, message) for a current finding count against its baseline."""
    summary = f"{label} findings: current={current} baseline={baseline}"
    if current > baseline:
        return 1, f"{summary}\nERROR: {label} regressed by {current - baseline}; fix the new findings."
    if current < baseline:
        return 1, (
            f"{summary}\nERROR: {label} improved by {baseline - current}; lock it in with "
            f"`python -m scripts.reshape.compare_{label.lower()}_baseline --write-baseline`."
        )
    return 0, summary


def file_code_verdict(current: list[dict[str, Any]], baseline: list[dict[str, Any]]) -> tuple[int, str]:
    """Compare counts for every path and code, reporting increases and available reductions."""
    current_counts = {(row["path"], row["code"]): row["count"] for row in current}
    baseline_counts = {(row["path"], row["code"]): row["count"] for row in baseline}
    increases, decreases = [], []
    for path, code in sorted(current_counts.keys() | baseline_counts.keys()):
        now = current_counts.get((path, code), 0)
        before = baseline_counts.get((path, code), 0)
        row = f"{path} | {code}: baseline={before} current={now}"
        if now > before:
            increases.append(row)
        elif now < before:
            decreases.append(row)
    lines = [f"ERROR: file/code regression: {row}" for row in increases]
    lines += [f"Ratchet down available: {row}" for row in decreases]
    return int(bool(increases)), "\n".join(lines)


def file_code_counts(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    from collections import Counter

    counts = Counter((item["path"], item["code"]) for item in findings)
    return [{"path": path, "code": code, "count": count} for (path, code), count in sorted(counts.items())]


def main(label: str, default_baseline: Path, file_code_baseline: Path, run: Callable[[], dict[str, Any]]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", type=Path, default=default_baseline)
    parser.add_argument("--write-baseline", action="store_true")
    parser.add_argument("--file-code-baseline", type=Path, default=file_code_baseline)
    parser.add_argument("--write-file-code-baseline", action="store_true")
    args = parser.parse_args()

    try:
        current = run()
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    file_code = current.pop("file_code_counts")
    if args.write_file_code_baseline:
        args.file_code_baseline.write_text(json.dumps(file_code, indent=2) + "\n")
        print(f"Wrote {label} file/code baseline to {args.file_code_baseline} ({len(file_code)} rows)")
        return 0
    if args.write_baseline:
        args.baseline.write_text(json.dumps(current, indent=2) + "\n")
        print(f"Wrote {label} baseline to {args.baseline} ({current['finding_count']} findings)")
        return 0

    baseline = json.loads(args.baseline.read_text())
    code, message = verdict(label, int(current["finding_count"]), int(baseline["finding_count"]))
    print(message, file=sys.stderr if code else sys.stdout)
    file_code_exit, file_code_message = file_code_verdict(
        file_code, json.loads(args.file_code_baseline.read_text())
    )
    if file_code_message:
        print(file_code_message)
    return max(code, file_code_exit)
