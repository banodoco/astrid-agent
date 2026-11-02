#!/usr/bin/env python3
"""Fail-closed admission for the one-purpose R-24 push venue."""
from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path
from typing import Mapping

REPOSITORY = "banodoco/astrid-agent"
WORKFLOW_PATH = ".github/workflows/r24-linux-live.yml"
VENUE_REF = "refs/heads/venue/M-PIN-R-24-PUSH-ROUTE-R1-E53B11D1"
PRODUCER_REF = "70636d1833164dad84a3551606b61ba2634565ba"
CONFIRMATION = "R24_LIVE_PIN_MANIFEST_CANDIDATE"
HEX40 = re.compile(r"[0-9a-f]{40}\Z")


class AdmissionError(RuntimeError):
    pass


def _required(environment: Mapping[str, str], name: str) -> str:
    value = environment.get(name)
    if not isinstance(value, str) or not value:
        raise AdmissionError(f"{name} is required")
    return value


def _equal(actual: str, expected: str, label: str) -> None:
    if actual != expected:
        raise AdmissionError(f"{label} must equal {expected!r}")


def validate_admission(environment: Mapping[str, str], *, runner_ref: str,
                       producer_ref: str, confirmation: str) -> dict[str, str]:
    """Validate shared exact-head inputs and the stricter push-route identity."""
    if not isinstance(runner_ref, str) or not HEX40.fullmatch(runner_ref):
        raise AdmissionError("runner_ref must be a lowercase immutable 40-character SHA")
    _equal(producer_ref, PRODUCER_REF, "producer_ref")
    _equal(confirmation, CONFIRMATION, "confirmation")
    _equal(_required(environment, "GITHUB_REPOSITORY"), REPOSITORY, "GITHUB_REPOSITORY")
    sha = _required(environment, "GITHUB_SHA")
    workflow_sha = _required(environment, "GITHUB_WORKFLOW_SHA")
    if not HEX40.fullmatch(sha) or not HEX40.fullmatch(workflow_sha):
        raise AdmissionError("GitHub SHA bindings must be lowercase immutable 40-character SHAs")
    _equal(runner_ref, sha, "runner_ref/GITHUB_SHA")
    _equal(workflow_sha, sha, "GITHUB_WORKFLOW_SHA/GITHUB_SHA")

    event = _required(environment, "GITHUB_EVENT_NAME")
    ref = _required(environment, "GITHUB_REF")
    workflow_ref = _required(environment, "GITHUB_WORKFLOW_REF")
    if event == "push":
        _equal(ref, VENUE_REF, "GITHUB_REF")
        _equal(workflow_ref, f"{REPOSITORY}/{WORKFLOW_PATH}@{VENUE_REF}",
               "GITHUB_WORKFLOW_REF")
    elif event != "workflow_dispatch":
        raise AdmissionError("GitHub event must be workflow_dispatch or the dedicated venue push")
    return {
        "schema": "astrid.r24.push-route-request.v1",
        "event": event,
        "ref": ref,
        "workflow_ref": workflow_ref,
        "workflow_sha": workflow_sha,
        "runner_ref": runner_ref,
        "producer_ref": producer_ref,
        "confirmation": confirmation,
    }


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("--runner-ref", required=True)
    value.add_argument("--producer-ref", required=True)
    value.add_argument("--confirmation", required=True)
    value.add_argument("--output", type=Path, required=True)
    return value


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    request = validate_admission(
        os.environ, runner_ref=args.runner_ref, producer_ref=args.producer_ref,
        confirmation=args.confirmation,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(request, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
