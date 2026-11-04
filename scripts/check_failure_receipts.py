#!/usr/bin/env python3
"""Fail closed over the nine Release-1 failure-mode receipt slots."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Mapping

if __package__:
    from .journey_failure_modes import (
        RECEIPT_NAME,
        REQUIRED_SLOT_IDS,
        candidate_errors,
        read_json,
        validate_receipt,
    )
else:
    try:
        from journey_failure_modes import (
            RECEIPT_NAME,
            REQUIRED_SLOT_IDS,
            candidate_errors,
            read_json,
            validate_receipt,
        )
    except ModuleNotFoundError:  # importlib-loaded tests run from the repository root
        from scripts.journey_failure_modes import (
            RECEIPT_NAME,
            REQUIRED_SLOT_IDS,
            candidate_errors,
            read_json,
            validate_receipt,
        )


def _mapping(value: object) -> Mapping:
    return value if isinstance(value, Mapping) else {}


def evaluate_needs(needs: Mapping) -> list[str]:
    """Validate GitHub's ``toJSON(needs)`` without inferring receipt success."""
    errors = []
    for slot_id in REQUIRED_SLOT_IDS:
        if slot_id not in needs:
            errors.append(f"{slot_id}: missing needs dependency")
            continue
        dependency = _mapping(needs.get(slot_id))
        result = dependency.get("result")
        if result != "success":
            errors.append(f"{slot_id}: needs result {result!r} is not success")
    return errors


def collect_receipts(results: Path) -> tuple[dict[str, tuple[dict, Path]], list[str]]:
    found = {}
    errors = []
    for path in sorted(results.rglob(RECEIPT_NAME)):
        try:
            row = read_json(path)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            errors.append(str(exc))
            continue
        slot_id = row.get("slot_id")
        if not isinstance(slot_id, str) or slot_id not in REQUIRED_SLOT_IDS:
            errors.append(f"{path}: unknown or absent failure-mode slot id")
        elif slot_id in found:
            errors.append(f"{path}: duplicate receipt for {slot_id}")
        else:
            found[slot_id] = (row, path)
    return found, errors


def verify_evidence_files(receipt: Mapping, receipt_path: Path) -> list[str]:
    """Bind receipt digests to run-owned files in the downloaded artifact."""
    errors = []
    receipt_root = receipt_path.parent.resolve()
    evidence = receipt.get("evidence_files")
    if not isinstance(evidence, list):
        return ["evidence_files: machine evidence list is required"]
    for index, item in enumerate(evidence):
        row = _mapping(item)
        raw_path = row.get("path")
        if not isinstance(raw_path, str):
            continue  # The schema validator reports this precisely.
        evidence_path = receipt_root / raw_path
        try:
            resolved = evidence_path.resolve(strict=True)
        except OSError as exc:
            errors.append(f"evidence_files[{index}]: missing evidence file {raw_path!r}: {exc}")
            continue
        if not resolved.is_relative_to(receipt_root) or evidence_path.is_symlink():
            errors.append(f"evidence_files[{index}]: evidence path escapes the receipt artifact")
            continue
        if not resolved.is_file():
            errors.append(f"evidence_files[{index}]: evidence path is not a regular file")
            continue
        observed = hashlib.sha256(resolved.read_bytes()).hexdigest()
        if observed != row.get("sha256"):
            errors.append(f"evidence_files[{index}]: evidence SHA-256 does not match {raw_path!r}")
    return errors


def evaluate(candidate: Mapping, results: Path, needs: Mapping) -> list[str]:
    """Require a successful dependency and verified receipt for every slot."""
    errors = candidate_errors(candidate)
    errors.extend(evaluate_needs(needs))
    found, collection_errors = collect_receipts(results)
    errors.extend(collection_errors)
    for slot_id in REQUIRED_SLOT_IDS:
        found_receipt = found.get(slot_id)
        if found_receipt is None:
            errors.append(f"{slot_id}: missing receipt")
            continue
        receipt, receipt_path = found_receipt
        for error in validate_receipt(receipt, candidate, slot_id):
            errors.append(f"{slot_id}: {error}")
        for error in verify_evidence_files(receipt, receipt_path):
            errors.append(f"{slot_id}: {error}")
    return errors


def report(errors: list[str]) -> str:
    heading = "## Failure-mode aggregate: " + ("FAIL" if errors else "PASS")
    body = (
        "\n".join(f"- {error}" for error in errors)
        if errors
        else "All nine OS/mode dependencies and machine-verified receipts passed."
    )
    return f"{heading}\n\n{body}\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", required=True, type=Path)
    parser.add_argument("--results", required=True, type=Path)
    parser.add_argument(
        "--needs",
        required=True,
        type=Path,
        help="JSON object from the aggregate job's toJSON(needs) context",
    )
    args = parser.parse_args(argv)
    errors = []
    try:
        candidate = read_json(args.candidate)
        needs = read_json(args.needs)
        if not args.results.is_dir():
            raise ValueError(f"{args.results}: results directory is required")
        errors.extend(evaluate(candidate, args.results, needs))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        errors.append(str(exc))
    output = report(errors)
    print(output, end="")
    summary_path = os.getenv("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as summary:
            summary.write(output)
    return int(bool(errors))


if __name__ == "__main__":
    sys.exit(main())
