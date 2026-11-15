from __future__ import annotations

import collections
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from scripts.reshape import count_baseline

BASELINE_PATH = Path("scripts/reshape/baselines/ruff_astrid.json")
FILE_CODE_BASELINE_PATH = Path("scripts/reshape/baselines/ruff_file_code.json")
COMMAND = [
    sys.executable,
    "-m",
    "ruff",
    "check",
    ".",
    "--output-format",
    "json",
]


def _run() -> dict[str, Any]:
    try:
        proc = subprocess.run(COMMAND, capture_output=True, text=True, check=False)
    except OSError as exc:
        raise RuntimeError(f"ruff failed to execute: {exc}") from exc
    if proc.returncode not in (0, 1):
        detail = (proc.stderr or proc.stdout).strip() or "no diagnostic output"
        raise RuntimeError(f"ruff failed to execute (exit {proc.returncode}): {detail}")
    try:
        findings = json.loads(proc.stdout or "[]")
    except json.JSONDecodeError as exc:
        detail = (proc.stderr or proc.stdout).strip() or "invalid JSON output"
        raise RuntimeError(f"ruff failed to execute: {detail}") from exc
    if not isinstance(findings, list):
        raise RuntimeError("ruff failed to execute: expected a JSON findings list")
    if proc.returncode == 1 and not findings:
        detail = (proc.stderr or proc.stdout).strip() or "no diagnostic output"
        raise RuntimeError(f"ruff failed to execute (exit 1): {detail}")
    return {
        "file_code_counts": count_baseline.file_code_counts(
            [{"path": Path(item["filename"]).relative_to(Path.cwd()).as_posix(), "code": item["code"]} for item in findings]
        ),
        "tool": "ruff",
        "scope": ["pyproject.toml [tool.ruff].include"],
        "command": COMMAND,
        "finding_count": len(findings),
        "code_counts": dict(collections.Counter(item["code"] for item in findings)),
    }


def main() -> int:
    return count_baseline.main("Ruff", BASELINE_PATH, FILE_CODE_BASELINE_PATH, _run)


if __name__ == "__main__":
    raise SystemExit(main())
