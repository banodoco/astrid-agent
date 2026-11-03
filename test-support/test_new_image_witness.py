"""Offline checks for the retained A4 image witness boundary."""
from __future__ import annotations

import copy
import importlib.util
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = Path(
    os.environ.get(
        "A4_IMAGE_EVIDENCE",
        "/tmp/claude-0/-home-user-repo/7dc61be1-7066-52e0-a5d0-94f90e61430a"
        "/scratchpad/orch/workers/agt-A4-image-live-0554/evidence",
    )
)
SPEC = importlib.util.spec_from_file_location(
    "new_image_witness", ROOT / "test-support" / "new_image_witness.py"
)
assert SPEC is not None and SPEC.loader is not None
WITNESS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(WITNESS)


def retained_response() -> tuple[dict, dict, dict, dict]:
    output = json.loads((EVIDENCE / "app-output.json").read_text())
    accepted = json.loads((EVIDENCE / "accepted-response.json").read_text())
    request = output["request"]
    return (
        request["commands"][0],
        request["steps"][0],
        accepted["data"]["commands"][0],
        accepted["data"]["steps"][0],
    )


def assert_fails(callable_, *args) -> None:
    try:
        callable_(*args)
    except AssertionError:
        return
    raise AssertionError("mutated authoritative witness was accepted")


def test_retained_authoritative_boundary_and_negative_controls() -> None:
    submitted_command, submitted_step, authoritative_command, authoritative_step = retained_response()
    assert authoritative_command["op"] == "clip.add-media"
    assert authoritative_step["patch"]
    WITNESS._assert_authoritative_command(
        submitted_command, authoritative_command, authoritative_command
    )
    WITNESS._assert_authoritative_step(submitted_step, authoritative_step, authoritative_step)

    altered_submitted = copy.deepcopy(submitted_command)
    altered_submitted["args"]["row"]["at"] = 3
    assert_fails(
        WITNESS._assert_authoritative_command,
        altered_submitted,
        authoritative_command,
        authoritative_command,
    )

    altered_created = copy.deepcopy(authoritative_command)
    altered_created["created"][0] = "parent/asset:forged"
    assert_fails(
        WITNESS._assert_authoritative_command,
        submitted_command,
        altered_created,
        authoritative_command,
    )

    missing_binding = copy.deepcopy(authoritative_command)
    missing_binding["bindings"].pop("media.track")
    assert_fails(
        WITNESS._assert_authoritative_command,
        submitted_command,
        missing_binding,
        authoritative_command,
    )

    altered_effect = copy.deepcopy(authoritative_step)
    altered_effect["patch"][0]["value"][-1]["at"] = 3
    assert_fails(
        WITNESS._assert_authoritative_step,
        submitted_step,
        altered_effect,
        authoritative_step,
    )

    altered_step_metadata = copy.deepcopy(authoritative_step)
    altered_step_metadata["dependencies"].pop()
    assert_fails(
        WITNESS._assert_authoritative_step,
        submitted_step,
        altered_step_metadata,
        authoritative_step,
    )
