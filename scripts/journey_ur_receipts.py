"""Update/rollback leg: writing the step receipts and reading them back for the fail-closed check."""
from __future__ import annotations

import json
import sys
from pathlib import Path

if __package__:
    from . import ci_journey as journey
    from .journey_ur_evidence import RESULT_NAME, STEPS, evaluate
else:
    import ci_journey as journey
    from journey_ur_evidence import RESULT_NAME, STEPS, evaluate

EVIDENCE_DIR = "update-rollback"


def receipt(evidence: Path, name: str, checks: dict, **fields) -> dict:
    required = STEPS[name]
    body = {"schema_version": 1, "receipt_kind": "r15-update-rollback-step", "step": name,
            "checks": {key: checks.get(key, "not-run") for key in required}, **fields}
    body["status"] = "pass" if all(value == "pass" for value in body["checks"].values()) else "fail"
    journey.write_json(evidence / name, body)  # the journey's own JSON writer, as every other leg uses it
    return body


def load_receipts(directory: Path) -> dict:
    found = {}
    for name in (*STEPS, RESULT_NAME):
        path = Path(directory) / name
        try:
            found[name] = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
    return found


def report_problems(directory: Path) -> list[str]:
    """Evaluate the receipts in ``directory`` and print each problem; the caller turns the list into an exit code."""
    problems = evaluate(load_receipts(directory))
    for problem in problems:
        print("PROBLEM:", problem, file=sys.stderr)
    return problems
