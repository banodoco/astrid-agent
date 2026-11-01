"""Shared terminal receipt serializer preserves the public result wire shape."""

from __future__ import annotations

import json
from copy import deepcopy

import pytest

from astrid.core.execution.terminal_result import serialize_terminal_result
from astrid.sdk.results import InvocationResult


@pytest.mark.parametrize(
    ("status", "ok", "cost", "settled", "settlement", "effect", "error"),
    [
        ("succeeded", True, 0, True, {"id": "settle-1"}, {"published": True}, None),
        ("partial", False, {"amount": 0.25, "currency": "USD"}, False, None, {"attempted": True},
         {"code": "provider_failed", "message": "one output failed"}),
        ("failed", False, None, False, {"rejected": True}, None,
         {"code": "invalid_input", "message": "missing source"}),
    ],
)
def test_terminal_receipt_has_explicit_stable_wire_fields(
    status, ok, cost, settled, settlement, effect, error,
):
    warnings = ["manifest warning", "publication warning"]
    warning_snapshot = list(warnings)
    resolved_options = {"seed": 17, "nested": ["kept"]}
    options_snapshot = deepcopy(resolved_options)
    artifacts = [] if status == "failed" else [{"id": "video#0", "size": 12}]
    manifest_path = None if status == "failed" else "/run/outputs/manifest.json"
    result = InvocationResult(
        capability_id="rendering.render",
        capability_type="executor",
        native_kind="executor",
        ok=ok,
        error=error,
        manifest_path=manifest_path,
        raw_result={"provider": "fixture", "attempt": 2},
        run_id="run-1",
        run_root="/run",
        outputs={"artifacts": artifacts},
        executor_version="3.4",
        kernel_run_id="run-1",
        kernel_task_id="task-1",
        kernel_attempt_id="attempt-1",
    )

    receipt = serialize_terminal_result(
        result,
        status=status,
        task_id="task-1",
        attempt_id="attempt-1",
        parent_task_id=None,
        parent_attempt_id="parent-attempt",
        request_digest="sha256:abc123",
        resolved_options=resolved_options,
        cost=cost,
        warnings=warnings,
        settled=settled,
        settlement=settlement,
        effect=effect,
    )

    expected = {
        "capability_id": "rendering.render",
        "capability_type": "executor",
        "native_kind": "executor",
        "ok": ok,
        "error": error,
        "manifest_path": manifest_path,
        "run_id": "run-1",
        "run_root": "/run",
        "outputs": {"artifacts": artifacts},
        "executor_version": "3.4",
        "raw_result": {"provider": "fixture", "attempt": 2},
        "kernel_run_id": "run-1",
        "kernel_task_id": "task-1",
        "kernel_attempt_id": "attempt-1",
        "status": status,
        "task_id": "task-1",
        "attempt_id": "attempt-1",
        "parent_task_id": None,
        "parent_attempt_id": "parent-attempt",
        "request_digest": "sha256:abc123",
        "resolved_options": options_snapshot,
        "cost": cost if cost is not None else "unknown",
        "warnings": warning_snapshot,
        "publication": {
            "status": "settled" if settled else "not_settled",
            "receipt": settlement,
            "effect": effect,
        },
    }
    assert receipt == expected
    assert json.dumps(receipt, sort_keys=True, separators=(",", ":")).encode() == json.dumps(
        expected, sort_keys=True, separators=(",", ":")
    ).encode()
    assert warnings == warning_snapshot
    assert resolved_options == options_snapshot


def test_terminal_serializer_accepts_standalone_orchestrator_result():
    result = InvocationResult(
        capability_id="video_editing.hype",
        capability_type="orchestrator",
        native_kind="orchestrator",
        ok=True,
        raw_result={"children": ["rendering.render"]},
        outputs={"artifacts": []},
    )

    receipt = serialize_terminal_result(
        result,
        status="succeeded",
        task_id="orchestrator-task",
        attempt_id=None,
        parent_task_id=None,
        parent_attempt_id=None,
        request_digest="sha256:orchestrator",
        resolved_options={},
        cost=None,
        warnings=[],
        settled=False,
        settlement={"observed": "without publication"},
        effect={"side_effect": "retained"},
    )

    assert receipt["capability_type"] == "orchestrator"
    assert receipt["raw_result"] == {"children": ["rendering.render"]}
    assert receipt["attempt_id"] is None
    assert receipt["parent_task_id"] is None
    assert receipt["parent_attempt_id"] is None
    assert receipt["cost"] == "unknown"
    assert receipt["publication"] == {
        "status": "not_settled",
        "receipt": {"observed": "without publication"},
        "effect": {"side_effect": "retained"},
    }
