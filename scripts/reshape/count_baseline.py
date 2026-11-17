"""Shared count gate for the Ruff and mypy baselines.

A count above its committed baseline is a regression. A lower count is an improvement: it passes
and is reported so the manager can lock it into the baseline once the merged batch is ready.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

# Owner decision, 12 Nov: only these per-file/code increases block Release 13.
BLOCKING_CODES = {
    "Ruff": frozenset({"F821", "BLE001"}),
    "Mypy": frozenset({"name-defined", "attr-defined", "call-arg"}),
}


def verdict(label: str, current: int, baseline: int) -> tuple[int, str]:
    """Return (exit code, message) for a current finding count against its baseline."""
    summary = f"{label} findings: current={current} baseline={baseline}"
    if current > baseline:
        return 1, f"{summary}\nERROR: {label} regressed by {current - baseline}; fix the new findings."
    if current < baseline:
        return 0, f"{summary}\nIMPROVEMENT: {label} findings decreased by {baseline - current}."
    return 0, summary


def file_code_verdict(
    current: list[dict[str, Any]], baseline: list[dict[str, Any]], blocking_codes: frozenset[str]
) -> tuple[int, str]:
    """Compare counts for every path and code, reporting increases and available reductions."""
    current_counts = {(row["path"], row["code"]): row["count"] for row in current}
    baseline_counts = {(row["path"], row["code"]): row["count"] for row in baseline}
    increases, reported, decreases = [], [], []
    for path, code in sorted(current_counts.keys() | baseline_counts.keys()):
        now = current_counts.get((path, code), 0)
        before = baseline_counts.get((path, code), 0)
        row = f"{path} | {code}: baseline={before} current={now}"
        if now > before:
            (increases if code in blocking_codes else reported).append(row)
        elif now < before:
            decreases.append(row)
    lines = [f"ERROR: file/code regression: {row}" for row in increases]
    if reported:
        lines.append("Reported, not blocking:")
        lines += [f"  {row}" for row in reported]
    lines += [f"IMPROVEMENT (ratchet down available): {row}" for row in decreases]
    return int(bool(increases)), "\n".join(lines)


def file_code_counts(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    from collections import Counter

    counts = Counter((item["path"], item["code"]) for item in findings)
    return [{"path": path, "code": code, "count": count} for (path, code), count in sorted(counts.items())]


def main(
    label: str,
    default_baseline: Path | None,
    file_code_baseline: Path,
    run: Callable[[], dict[str, Any]],
    *,
    compare_total: bool = True,
) -> int:
    parser = argparse.ArgumentParser()
    if compare_total:
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
    if args.write_baseline and compare_total:
        args.baseline.write_text(json.dumps(current, indent=2) + "\n")
        print(f"Wrote {label} baseline to {args.baseline} ({current['finding_count']} findings)")
        return 0

    code = 0
    if compare_total:
        baseline = json.loads(args.baseline.read_text())
        code, message = verdict(label, int(current["finding_count"]), int(baseline["finding_count"]))
        print(message, file=sys.stderr if code else sys.stdout)
    file_code_exit, file_code_message = file_code_verdict(
        file_code, json.loads(args.file_code_baseline.read_text()), BLOCKING_CODES[label]
    )
    if file_code_message:
        print(file_code_message)
    return max(code, file_code_exit)
