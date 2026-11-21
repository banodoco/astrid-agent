#!/usr/bin/env python3
"""R14-P39-23: read-only native evidence of the original opaque-source refusals.

    python -m scripts.diagnostics.p39_opaque_s10_probe --self-test
    python -m scripts.diagnostics.p39_opaque_s10_probe --output /tmp/p39-s10-rows.json

The self-test exercises the observer with synthetic mechanics only, never
historical film content. Native mode uses each UNMODIFIED opaque_film fixture
variant on the pinned Agent/Runtime tuple. The observer preserves the original
native return object and propagates original exceptions unchanged. No source,
fixture, test assertion, published history or product semantics is modified.
"""
from __future__ import annotations

import argparse
import base64
import copy
import hashlib
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
S10_REFUSAL = "patch previous value does not match"
S13_REFUSAL = "shot item correspondence changed"
ROW_PATH = ["occurrences", "occurrence_id:occurrence-1"]


def _git(root: Path, *argv: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(root), *argv], text=True, capture_output=True,
        check=False,
    )
    if completed.returncode:
        raise RuntimeError(f"git {' '.join(argv)} in {root}: {completed.stderr.strip()}")
    return completed.stdout.strip()


def frozen_sources() -> dict:
    """Refuse a changed test/engine or a different Runtime (never repin)."""
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
            raise RuntimeError(f"frozen source differs at {path}: {actual} != {sha}")
    runtime_head = _git(runtime, "rev-parse", "HEAD")
    if runtime_head != RUNTIME_PIN:
        raise RuntimeError(f"Runtime checkout is {runtime_head}, expected {RUNTIME_PIN}")
    return {"agent_head": head, "agent_base": AGENT_BASE,
            "runtime_head": runtime_head, "test_blob": TEST_BLOB,
            "doc_patch_blob": PATCH_BLOB}


def _occurrence(root: dict, identity: str = "occurrence-1") -> dict | None:
    rows = root.get("occurrences") if isinstance(root, dict) else None
    if not isinstance(rows, list):
        return None
    matches = [row for row in rows if isinstance(row, dict)
               and row.get("occurrence_id") == identity]
    return copy.deepcopy(matches[0]) if len(matches) == 1 else None


def _canonical_row(row: dict) -> dict:
    """Exactly commit._json's finite, sorted, compact UTF-8 byte codec."""
    if not isinstance(row, dict):
        raise ValueError("complete occurrence row is unavailable")
    raw = json.dumps(row, sort_keys=True, separators=(",", ":"),
                     ensure_ascii=False, allow_nan=False).encode("utf-8")
    return {"row": copy.deepcopy(row), "canonical_utf8_base64": base64.b64encode(raw).decode("ascii"),
            "canonical_utf8_sha256": hashlib.sha256(raw).hexdigest(),
            "canonical_utf8_length": len(raw)}


def _native_guard(root, change, physical, patch):
    """Observe the native old-value calculation without changing its result."""
    if change["op"].startswith("list."):
        state = patch.value_at(root, physical, change["path"])
        rows = state.get("value")
        key = patch.identity_key(physical, change["path"])
        order = patch.identities(rows, key, ref=physical, path=change["path"])
        identity = change["id"]
        actual = (copy.deepcopy(patch.ABSENT) if identity not in order
                  else patch.present(rows[order.index(identity)]))
        order_matches = order == change["order_before"]
    else:
        actual = patch.value_at(root, physical, change["path"])
        order_matches = None
    matches = (patch.equal(actual, change["before"])
               and (order_matches is None or order_matches)
               and not (change["op"] in ("remove", "list.remove", "list.move")
                        and actual == patch.ABSENT))
    return {"actual_guard_before": copy.deepcopy(actual), "guard_matches": matches,
            "collection_order_matches": order_matches}


def _observe_apply(root, change, *, physical, original_apply, patch, evidence, tracking):
    """Observe both outcomes; return exactly what native returned or re-raise.

    All observation exceptions are retained as incomplete evidence and NEVER
    replace a native success or a native exception. A failure to collect a
    complete row is rejected later by _diagnose, not by the native adapter.
    """
    if not tracking["active"]:
        return original_apply(root, change, physical=physical)

    resolved = physical or change["ref"]
    index = len(evidence["ordered_native_operations"])
    event = {"ordinal": index, "ref": change["ref"], "physical": resolved,
             "op": change["op"], "path": copy.deepcopy(change["path"]),
             "submitted_before": copy.deepcopy(change["before"]),
             "submitted_value": copy.deepcopy(change.get("value")),
             "observer_errors": []}
    try:
        submitted = evidence.get("ordered_submitted_operations", [])
        if index >= len(submitted):
            event["observer_errors"].append("native operation has no matching submitted ordinal")
        else:
            claim = submitted[index]
            event["command_ordinal"] = claim["command_ordinal"]
            event["change_ordinal"] = claim["change_ordinal"]
            actual = json.dumps(change, sort_keys=True, separators=(",", ":"),
                                ensure_ascii=False, allow_nan=False)
            expected = json.dumps(claim["change"], sort_keys=True, separators=(",", ":"),
                                  ensure_ascii=False, allow_nan=False)
            event["submitted_change_matches"] = actual.encode() == expected.encode()
            if not event["submitted_change_matches"]:
                event["observer_errors"].append("native change differs from claimed operation ordinal")
        if resolved == "parent":
            event["row_before"] = _occurrence(root)
            event.update(_native_guard(root, change, resolved, patch))
    except Exception as error:
        event["observer_errors"].append(f"{type(error).__name__}: {error}")
    evidence["ordered_native_operations"].append(event)

    try:
        original_result = original_apply(root, change, physical=physical)
    except BaseException as error:
        event["native_exception"] = {"type": type(error).__name__, "message": str(error),
                                     "kind": getattr(error, "kind", None)}
        raise
    if resolved == "parent":
        try:
            event["row_after"] = _occurrence(original_result[0])
        except Exception as error:
            event["observer_errors"].append(f"after: {type(error).__name__}: {error}")
    return original_result  # Preserve the original (root, before) tuple by identity.


def _diagnose(evidence: dict) -> dict:
    """Fail closed unless the first S10 mismatch is unique and byte-complete."""
    mode = evidence["mode"]
    events = evidence.get("ordered_native_operations", [])
    claims = evidence.get("ordered_submitted_operations", [])
    outcome = evidence.get("fixture_outcome", {})
    problems = []
    if "capture_error" in evidence:
        problems.append(f"capture error: {evidence['capture_error']}")
    if not claims:
        problems.append("ordered submitted operations not captured")
    if not events:
        problems.append("no original native operations observed")
    if len(events) > len(claims):
        problems.append("more native operations than original submitted changes")
    for event in events:
        if event.get("observer_errors"):
            problems.append(f"operation {event['ordinal']} observer incomplete: {event['observer_errors']}")
        if event.get("submitted_change_matches") is not True:
            problems.append(f"operation {event['ordinal']} has no exact matched submitted claim")
    if mode == "id":
        if outcome.get("message") != S13_REFUSAL:
            problems.append(f"original S13 refusal not reproduced: {outcome}")
        return {"status": "INCOMPLETE" if problems else "ORIGINAL_ID_ONLY_REFUSAL",
                "historical_id_only_bytes_authenticated": False,
                "no_compatibility_conclusion": True, "incomplete_reasons": problems}

    if mode not in ("item_id", "both"):
        problems.append(f"unexpected mode {mode}")
    if outcome.get("message") != S10_REFUSAL:
        problems.append(f"original S10 refusal not reproduced: {outcome}")
    failures = [event for event in events if event.get("native_exception")]
    if len(failures) != 1 or (events and failures and failures[0] is not events[-1]):
        problems.append(f"native failure count/position ambiguous: {len(failures)}")
    guarded = [e for e in failures
               if e.get("native_exception", {}).get("kind") == "previous_value_mismatch"
               and e.get("ref") == "parent" and e.get("path") == ROW_PATH
               and e.get("op") == "set"]
    if len(guarded) != 1:
        problems.append(f"unique parent occurrence-1 previous-value refusal missing: {len(guarded)}")
    if events and guarded and any(e.get("guard_matches") is False for e in events[:guarded[0]["ordinal"]]):
        problems.append("earlier observed guarded mismatch; first divergence is ambiguous")

    result = {"status": "INCOMPLETE", "incomplete_reasons": problems}
    if len(guarded) == 1:
        first = guarded[0]
        result["first_mismatch"] = {
            "ordinal": first["ordinal"],
            "command_ordinal": first.get("command_ordinal"),
            "change_ordinal": first.get("change_ordinal"),
            "ref": first["ref"], "path": first["path"],
            "native_exception": first["native_exception"],
        }
        sources = {
            "frozen_staged": evidence.get("staged_occurrence_1"),
            "submitted_expected_old": first.get("submitted_before", {}).get("value"),
            "actual_current": first.get("actual_guard_before", {}).get("value"),
            "complete_intended": first.get("submitted_value"),
            "actual_root_row_before": first.get("row_before"),
        }
        canonical = {}
        for name, row in sources.items():
            try:
                canonical[name] = _canonical_row(row)
            except (ValueError, TypeError) as error:
                problems.append(f"{name} missing/noncanonical: {type(error).__name__}: {error}")
        result["complete_rows_and_canonical_bytes"] = canonical
        if len(canonical) == len(sources):
            digests = {name: value["canonical_utf8_base64"] for name, value in canonical.items()}
            result["canonical_byte_comparisons"] = {
                "submitted_old_eq_current": digests["submitted_expected_old"] == digests["actual_current"],
                "current_eq_intended": digests["actual_current"] == digests["complete_intended"],
                "root_current_eq_guard": digests["actual_root_row_before"] == digests["actual_current"],
                "staged_eq_submitted_old": digests["frozen_staged"] == digests["submitted_expected_old"],
            }
            if first.get("guard_matches") is not False:
                problems.append("native rejected but observer did not capture its guard mismatch")
            if not result["canonical_byte_comparisons"]["root_current_eq_guard"]:
                problems.append("root occurrence row and native guard value differ")
            if result["canonical_byte_comparisons"]["submitted_old_eq_current"]:
                problems.append("old/current canonical bytes match despite native mismatch")
            if not problems:
                result["status"] = "STALE_FIXTURE_BEFORE_CAPTURED"
                result["remove_appended_write_safe_if_reviewed"] = result[
                    "canonical_byte_comparisons"]["current_eq_intended"]
    result["incomplete_reasons"] = problems
    return result


def capture_mode(mode: str) -> dict:
    import pytest
    from astrid.sdk.timeline import _doc_patch
    from tests.editing_conformance import test_commit_runtime_opaque_items as test_module

    evidence: dict = {"mode": mode, "ordered_native_operations": []}
    original_builder = test_module.body_with_ordered_patch
    original_apply = _doc_patch.apply_change
    tracking = {"active": False}

    def observe_builder(edit, commands):
        frame = inspect.currentframe().f_back
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
        return _observe_apply(root, change, physical=physical,
                              original_apply=original_apply, patch=_doc_patch,
                              evidence=evidence, tracking=tracking)

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
    except Exception as error:
        evidence["capture_error"] = f"{type(error).__name__}: {error}"
    finally:
        patcher.undo()
    evidence["diagnosis"] = _diagnose(evidence)
    return evidence


def _self_test() -> None:
    """Exercise both instrumented native paths without importing Agent or Runtime."""
    class NativeRefusal(Exception):
        kind = "previous_value_mismatch"

    initial = {"occurrence_id": "occurrence-1", "duration_ms": 1800,
               "data": {"unicode": "é", "order": [1, 2]}}
    older = {**initial, "duration_ms": 1200}
    intended = {**initial, "opaque": {"v": [None, "item-1"]}}
    change = {"op": "set", "ref": "parent", "path": list(ROW_PATH),
              "before": {"state": "present", "value": copy.deepcopy(older)},
              "value": copy.deepcopy(intended)}
    patch = SimpleNamespace(
        value_at=lambda root, ref, path: {"state": "present", "value": copy.deepcopy(root["occurrences"][0])},
        equal=lambda a, b: a == b,
        ABSENT={"state": "absent"}, present=lambda row: {"state": "present", "value": copy.deepcopy(row)},
        identity_key=lambda ref, path: "occurrence_id",
        identities=lambda rows, key, **kw: [row[key] for row in rows],
    )

    def record():
        return {"mode": "item_id", "staged_occurrence_1": copy.deepcopy(older),
                "ordered_submitted_operations": [{"command_ordinal": 0, "change_ordinal": 0,
                                                  "change": copy.deepcopy(change)}],
                "ordered_native_operations": [],
                "fixture_outcome": {"message": S10_REFUSAL}}

    success_value = ({"occurrences": [copy.deepcopy(intended)]},
                     {"state": "present", "value": copy.deepcopy(initial)})
    success_report = record()
    def native_success(root, change, *, physical=None):
        return success_value
    observed = _observe_apply({"occurrences": [copy.deepcopy(initial)]}, change, physical="parent",
                              original_apply=native_success, patch=patch, evidence=success_report,
                              tracking={"active": True})
    assert observed is success_value and observed[1] is success_value[1]
    assert success_report["ordered_native_operations"][0]["row_after"] == intended
    assert not success_report["ordered_native_operations"][0].get("native_exception")

    inactive_report = record()
    inactive = _observe_apply({"occurrences": [copy.deepcopy(initial)]}, change, physical="parent",
                              original_apply=native_success, patch=patch, evidence=inactive_report,
                              tracking={"active": False})
    assert inactive is success_value and inactive_report["ordered_native_operations"] == []

    failure = NativeRefusal(S10_REFUSAL)
    failure_report = record()
    def native_failure(root, change, *, physical=None):
        raise failure
    try:
        _observe_apply({"occurrences": [copy.deepcopy(initial)]}, change, physical="parent",
                       original_apply=native_failure, patch=patch, evidence=failure_report,
                       tracking={"active": True})
        raise AssertionError("native refusal was swallowed")
    except NativeRefusal as error:
        assert error is failure
    event = failure_report["ordered_native_operations"][0]
    assert event["native_exception"]["kind"] == "previous_value_mismatch"
    diagnosis = _diagnose(failure_report)
    assert diagnosis["status"] == "STALE_FIXTURE_BEFORE_CAPTURED", diagnosis
    assert diagnosis["canonical_byte_comparisons"]["submitted_old_eq_current"] is False
    assert diagnosis["canonical_byte_comparisons"]["root_current_eq_guard"] is True
    assert diagnosis["remove_appended_write_safe_if_reviewed"] is False
    raw = base64.b64decode(diagnosis["complete_rows_and_canonical_bytes"]["actual_current"][
        "canonical_utf8_base64"])
    assert raw == json.dumps(initial, sort_keys=True, separators=(",", ":"),
                             ensure_ascii=False, allow_nan=False).encode("utf-8")
    failure_report.pop("staged_occurrence_1")
    assert _diagnose(failure_report)["status"] == "INCOMPLETE"
    print("PASS: tracked success returned original root/before object; tracked exception preserved")
    print("PASS: inactive forwarding; unique first mismatch; canonical UTF-8 byte comparison")
    print("PASS: missing staged row is INCOMPLETE, not a claimed fixture fix")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="JSON evidence outside the repository")
    parser.add_argument("--self-test", action="store_true", help="pure observer mechanics, no native Runtime")
    args = parser.parse_args()
    if args.self_test:
        _self_test()
        return 0
    if args.output is None:
        parser.error("--output is required for native capture")
    report: dict = {"packet": "R14-P39-23", "scope": "evidence-only",
                    "cases": ["item_id", "both", "id"], "modes": []}
    try:
        report["verified_sources"] = frozen_sources()
    except Exception as error:
        report["probe_failure"] = {"type": type(error).__name__, "message": str(error)}
    if "probe_failure" not in report:
        for mode in ("item_id", "both", "id"):
            try:
                report["modes"].append(capture_mode(mode))
            except Exception as error:
                report["modes"].append({"mode": mode, "capture_error": f"{type(error).__name__}: {error}",
                                        "diagnosis": {"status": "INCOMPLETE", "incomplete_reasons": [str(error)]}})
        incomplete = [mode["mode"] for mode in report["modes"]
                      if mode["diagnosis"]["status"] == "INCOMPLETE"]
        if incomplete:
            report["probe_failure"] = {"type": "IncompleteEvidence", "modes": incomplete,
                                       "message": "native evidence incomplete or original refusal changed"}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
                           encoding="utf-8")
    print(f"Evidence written to {args.output}")
    for mode in report["modes"]:
        print(mode["mode"], mode.get("fixture_outcome"), mode["diagnosis"]["status"])
    if report.get("probe_failure"):
        print("BLOCKED:", report["probe_failure"])
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
