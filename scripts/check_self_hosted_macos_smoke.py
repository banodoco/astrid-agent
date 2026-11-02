#!/usr/bin/env python3
"""Validate a self-hosted smoke separately; it is never release qualification."""
import json
import sys
from pathlib import Path

from check_journey_evidence import RELEASE1_MATRIX, _release1_row_errors


def main(candidate_path, receipt_path):
    candidate = json.loads(Path(candidate_path).read_text(encoding="utf-8"))
    receipt = json.loads(Path(receipt_path).read_text(encoding="utf-8"))
    contract = next(row for row in RELEASE1_MATRIX if row["job_id"] == "direct-macos")
    errors = _release1_row_errors(candidate, contract, receipt, self_hosted_smoke=True)
    if errors:
        raise SystemExit("Self-hosted smoke failed:\n" + "\n".join(errors))
    print("Self-hosted macOS smoke passed; this receipt is not release qualification.")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit("usage: check_self_hosted_macos_smoke.py CANDIDATE RECEIPT")
    main(*sys.argv[1:])
