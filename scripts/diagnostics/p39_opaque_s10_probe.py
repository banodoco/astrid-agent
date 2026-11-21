#!/usr/bin/env python3
"""R14-P39-23: native, read-only evidence of the original opaque-source refusal.

Run from the Agent checkout on the frozen Plan 39 parent, with a sibling
Runtime checkout at the pinned revision:

    python -m scripts.diagnostics.p39_opaque_s10_probe --output /tmp/p39-s10-rows.json

The probe invokes the UNMODIFIED opaque_film fixture with its original
item_id, both, and id parameters. It wraps only the in-process doc.patch
observer and records the actual checked row at the refusal; all underlying
operations, assertions and refusals execute unchanged. Nothing is committed.
The output contains complete synthetic fixture rows and submitted operations.
"""
from __future__ import annotations

import argparse
import copy
import inspect
import json
import subprocess
import tempfile
from pathlib import Path
from types import SimpleNamespace

AGENT_BASE = "6076344855410066a01f09b54d8ef7101700a66d"
RUNTIME_PIN = "2537fed24385ec1c24f48a812e09af4bb63fb794"
TEST_BLOB = "339bf63752f509c1c0cb6b1fce30cda385dd2065"
PATCH_BLOB = "e55d400c00ed727a244aecdaef95a67205e623fa"


def _git(root: Path, *argv: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(root), *argv], text=True, capture_output=True,
        check=False,
    )
    if completed.returncode:
        raise RuntimeError(f"git {' '.join(argv)} in {root}: {completed.stderr.strip()}")
    return completed.stdout.strip()


def frozen_sources() -> dict:
    """Reject a mismatched source rather than certifying an unrelated tuple."""
    from tests.helpers.runtime import runtime_checkout

    agent = Path(__file__).resolve().parents[2]
    runtime = runtime_checkout()
    head = _git(agent, "rev-parse", "HEAD")
    _git(agent, "merge-base", "--is-ancestor", AGENT_BASE, head)
    for path, sha in (
        ("tests/editing_conformance/test_commit_runtime_opaque_items.py", TEST_BLOB),
        ("astrid/sdk/timeline/_doc_patch.py", PATCH_BLOB),
    ):
        actual = _git(agent, "hash-object", path)
        if actual != sha:
            raise RuntimeError(f"frozen test/patch blob mismatch at {path}: {actual} != {sha}")
    runtime_head = _git(runtime, "rev-parse", "HEAD")
    if runtime_head != RUNTIME_PIN:
        raise RuntimeError(f"Runtime checkout is {runtime_head}, expected {RUNTIME_PIN}")
    return {"agent_head": head, "agent_base": AGENT_BASE, "runtime_head": runtime_head,
            "test_blob": TEST_BLOB, "doc_patch_blob": PATCH_BLOB}


def _occurrence(root: dict, identity: str = "occurrence-1") -> dict | None:
    for row in root.get("occurrences", []):
        if row.get("occurrence_id") == identity:
            return copy.deepcopy(row)
    return None


def capture_mode(mode: str) -> dict:
    import pytest
    from astrid.sdk.timeline import _doc_patch
    from astrid.sdk.timeline._command_state import equal
    from tests.editing_conformance import test_commit_runtime_opaque_items as test_module

    evidence: dict = {"mode": mode, "ordered_native_operations": []}
    original_builder = test_module.body_with_ordered_patch
    original_apply = _doc_patch.apply_change
    tracking = {"active": False}

    def observe_builder(edit, commands):
        frame = inspect.currentframe().f_back
        # Exactly the original opaque-source batch, before its native commit.
        if (frame is not None and frame.f_code.co_name == "opaque_film"
                and "staged_bundle" in frame.f_locals and not tracking["active"]):
            values = frame.f_locals
            staged = values["staged"]
            evidence["frozen_staged_parent"] = {
                "revision_id": staged["parent_revision"]["revision_id"],
                "content_digest": staged["parent_revision"]["content_digest"],
            }
            evidence["staged_occurrence_1"] = copy.deepcopy(
                next(row for row in staged["parent_revision"]["payload"]["occurrences"]
                     if row["occurrence_id"] == "occurrence-1"))
            evidence["ordered_submitted_operations"] = [
                {"command_ordinal": ci, "change_ordinal": oi, "change": copy.deepcopy(change)}
                for ci, command in enumerate(commands)
                for oi, change in enumerate(command["args"]["changes"])
            ]
            evidence["original_command_count"] = len(commands)
            tracking["active"] = True
        return original_builder(edit, commands)

    def observe_apply(root, change, *, physical=None):
        if not tracking["active"]:
            return original_apply(root, change, physical=physical)
        resolved = physical or change["ref"]
        event = {
            "ordinal": len(evidence["ordered_native_operations"]),
            "ref": change["ref"], "physical": resolved,
            "op": change["op"], "path": copy.deepcopy(change["path"]),
            "submitted_before": copy.deepcopy(change["before"]),
            "submitted_value": copy.deepcopy(change.get("value")),
        }
        if resolved == "parent":
            event["row_before"] = _occurrence(root)
            try:
                event["actual_guard_before"] = copy.deepcopy(
                    _doc_patch.value_at(root, resolved, change["path"]))
                event["guard_matches"] = equal(event["submitted_before"],
                                               event["actual_guard_before"])
            except Exception as error:
                event["read_error"] = f"{type(error).__name__}: {error}"
        evidence["ordered_native_operations"].append(event)
        try:
            new_root, before = original_apply(root, change, physical=physical)
        except Exception as error:
            event["refusal"] = {"type": type(error).__name__, "message": str(error),
                                "kind": getattr(error, "kind", None)}
            raise
        if resolved == "parent":
            event["row_after"] = _occurrence(new_root)
        return new_root, before

    patcher = pytest.MonkeyPatch()
    generator = None
    try:
        patcher.setattr(test_module, "body_with_ordered_patch", observe_builder)
        patcher.setattr(_doc_patch, "apply_change", observe_apply)
        with tempfile.TemporaryDirectory(prefix=f"p39-opaque-{mode}-") as directory:
            generator = test_module.opaque_film.__wrapped__(
                SimpleNamespace(param=mode), Path(directory), patcher)
            try:
                next(generator)
            except Exception as error:
                evidence["fixture_outcome"] = {
                    "type": type(error).__name__, "message": str(error),
                    "status": getattr(error, "status", None),
                    "code": getattr(error, "code", None),
                }
            else:
                evidence["fixture_outcome"] = {"type": "ACCEPTED", "message": "fixture yielded"}
            finally:
                if generator is not None:
                    generator.close()
    finally:
        patcher.undo()

    if mode in ("item_id", "both"):
        matching = [event for event in evidence["ordered_native_operations"]
                    if event["ref"] == "parent" and event["op"] == "set"
                    and event["path"] == ["occurrences", "occurrence_id:occurrence-1"]]
        if len(matching) != 1:
            evidence["diagnosis"] = {"status": "UNKNOWN", "reason": "expected one whole-row set",
                                     "observed": len(matching)}
        else:
            failing = matching[0]
            old = failing["submitted_before"].get("value")
            current = failing.get("actual_guard_before", {}).get("value")
            intended = failing["submitted_value"]
            evidence["diagnosis"] = {
                "status": "STALE_FIXTURE_BEFORE" if current is not None and old != current
                          and failing.get("refusal", {}).get("kind") == "previous_value_mismatch"
                          else "UNPROVEN",
                "expected_old": old, "current_at_guard": current,
                "complete_intended_row": intended,
                "old_equals_current": old == current,
                "current_equals_complete_intended": current == intended,
                "remove_appended_write_safe": current == intended,
                "first_divergence_operation": failing["ordinal"],
            }
    return evidence


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True,
                        help="JSON evidence output outside the repository")
    args = parser.parse_args()
    report: dict = {"packet": "R14-P39-23", "scope": "evidence-only",
                    "cases": ["item_id", "both", "id"]}
    try:
        report["verified_sources"] = frozen_sources()
        report["modes"] = [capture_mode(mode) for mode in ("item_id", "both", "id")]
    except Exception as error:
        report["probe_failure"] = {"type": type(error).__name__, "message": str(error)}
    if "probe_failure" not in report:
        expected = {"item_id": "patch previous value does not match",
                    "both": "patch previous value does not match",
                    "id": "shot item correspondence changed"}
        for mode in report["modes"]:
            found = mode.get("fixture_outcome", {}).get("message", "")
            if expected[mode["mode"]] not in found:
                report["probe_failure"] = {
                    "type": "OriginalRefusalChanged",
                    "message": f"{mode['mode']}: expected {expected[mode['mode']]!r}; got {found!r}",
                }
                break
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"Evidence written to {args.output}")
    for mode in report.get("modes", []):
        print(mode["mode"], mode.get("fixture_outcome"), mode.get("diagnosis", {}).get("status"))
    if report.get("probe_failure"):
        print(f"BLOCKED: {report['probe_failure']}")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
