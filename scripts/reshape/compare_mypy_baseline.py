from __future__ import annotations

import collections
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

from scripts.reshape import count_baseline

FILE_CODE_BASELINE_PATH = Path("scripts/reshape/baselines/mypy_file_code.json")
COMMAND = [
    sys.executable,
    "-m",
    "mypy",
    "astrid",
    "scripts/reshape",
    "--hide-error-context",
    "--no-color-output",
    "--no-pretty",
    "--show-error-codes",
]
ERROR_RE = re.compile(r"^(.*?):(\d+):(?:(\d+):)? error: (.*?)\s+\[(.*?)\]$")


def _run() -> dict[str, Any]:
    try:
        proc = subprocess.run(COMMAND, capture_output=True, text=True, check=False)
    except OSError as exc:
        raise RuntimeError(f"mypy failed to execute: {exc}") from exc
    findings = []
    for line in (proc.stdout.splitlines() + proc.stderr.splitlines()):
        match = ERROR_RE.match(line)
        if not match:
            continue
        findings.append(
            {
                "path": match.group(1),
                "line": int(match.group(2)),
                "column": int(match.group(3) or 0),
                "message": match.group(4),
                "code": match.group(5),
            }
        )
    if proc.returncode not in (0, 1) or (proc.returncode == 1 and not findings):
        detail = (proc.stderr or proc.stdout).strip() or "no diagnostic output"
        raise RuntimeError(f"mypy failed to execute (exit {proc.returncode}): {detail}")
    return {
        "file_code_counts": count_baseline.file_code_counts(findings),
        "tool": "mypy",
        "scope": COMMAND[3:5],
        "command": COMMAND,
        "finding_count": len(findings),
        "code_counts": dict(
            sorted(collections.Counter(item["code"] for item in findings).items())
        ),
    }


def main() -> int:
    return count_baseline.main(
        "Mypy", None, FILE_CODE_BASELINE_PATH, _run, compare_total=False
    )


if __name__ == "__main__":
    raise SystemExit(main())
