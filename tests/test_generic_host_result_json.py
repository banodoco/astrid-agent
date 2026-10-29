"""Terminal run receipts use the existing host lifecycle and artifact contract."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from astrid.core._shared.result_manifest import build_manifest, write_manifest
from astrid.core.contracts.errors import AstridError
from astrid.core.execution import generic_host
from astrid.core.execution.generic_host import GenericPackHost, HostCancelled, HostError
from astrid.sdk.results import InvocationResult
from tests.test_generic_host import FakeRuntime, _write_manifest


class ReceiptRuntime(FakeRuntime):
    def upload_object(self, path, *, project_id, media_type, filename=None, **_binding):
        return super().upload_object(path, project_id=project_id, media_type=media_type, filename=filename)


@pytest.fixture
def host_case(tmp_path, monkeypatch):
    """Keep execution at its child boundary while exercising real run_task paths."""
    path = _write_manifest(tmp_path / "pack")
    definition = json.loads(path.read_text(encoding="utf-8"))
    definition["metadata"]["output_result_manifest"] = True
    definition["inputs"] = [{"name": "quality", "type": "string", "required": False, "default": "standard"}]
    path.write_text(json.dumps(definition), encoding="utf-8")
    runtime = ReceiptRuntime()
    root = tmp_path / "run"
    host = GenericPackHost(pack_roots=[tmp_path / "pack"], client=runtime, attempt_root=root)
    host.discover()
    # These tests do not launch a child; the host's binding still needs identity.
    monkeypatch.setattr(generic_host, "process_birth_identity", lambda: "fixture-host-birth")
    task = {"task": {
        "id": "task-result", "attempt_id": "attempt-result", "fence": 3,
        "capability": "test.echo", "project_id": "demo", "run_id": "run-result",
        "parent_task_id": "task-parent", "parent_attempt_id": "attempt-parent",
        "spec": {"spec": {"inputs": {"mode": "precise", "seed": 17}}},
    }}
    runtime.tasks["task-result"] = task
    return SimpleNamespace(host=host, runtime=runtime, root=root, task=task, legacy_bytes=None)


def _legacy_output(case, output_root, *, warnings=None, **extras):
    output_root.mkdir(parents=True, exist_ok=True)
    (output_root / "answer.txt").write_bytes(b"complete output\n")
    write_manifest(output_root / "manifest.json", build_manifest(
        kind="test.echo", inputs={"mode": "precise", "seed": 17},
        outputs=[{"name": "answer", "path": "answer.txt", "ordinal": 0,
                  "media_type": "text/plain", "role": "result", "is_primary": True}],
        created="2026-10-29T00:00:00Z", warnings=warnings or [], **extras,
    ))
    case.legacy_bytes = (output_root / "manifest.json").read_bytes()


def _success_boundary(case, monkeypatch, *, payload=None, manifest_extras=None):
    def execute(_record, _inputs, output_root, _attempt, **_kwargs):
        _legacy_output(case, output_root, **(manifest_extras or {}))
        return SimpleNamespace(payload=payload or {}, process_id=12345, returncode=0)

    monkeypatch.setattr(case.host, "_run_command_definition", execute)


def _read_result(case, status, *, root=None):
    root = root or case.root
    receipt = json.loads((root / "result.json").read_text(encoding="utf-8"))
    serialized_keys = set(InvocationResult(
        capability_id="test.echo", capability_type="executor", native_kind="external", ok=False,
    ).to_dict())
    assert serialized_keys <= receipt.keys()
    assert receipt["status"] == status
    assert receipt["task_id"] == receipt["kernel_task_id"] == "task-result"
    assert receipt["attempt_id"] == receipt["kernel_attempt_id"] == "attempt-result"
    assert receipt["parent_task_id"] == "task-parent"
    assert receipt["parent_attempt_id"] == "attempt-parent"
    assert receipt["run_root"] == str(root)
    assert receipt["capability_id"] == "test.echo"
    assert receipt["capability_type"] == "executor"
    assert receipt["resolved_options"]["mode"] == "precise"
    assert receipt["resolved_options"]["seed"] == 17
    assert receipt["resolved_options"]["quality"] == "standard"
    assert isinstance(receipt["request_digest"], str)
    digest = receipt["request_digest"].removeprefix("sha256:")
    assert len(digest) == 64 and all(char in "0123456789abcdef" for char in digest)
    assert isinstance(receipt["warnings"], list)
    assert isinstance(receipt["outputs"], dict)
    assert isinstance(receipt["outputs"]["artifacts"], list)
    assert {"cost", "error", "publication"} <= receipt.keys()
    return receipt


def _assert_artifact(receipt):
    rows = receipt["outputs"]["artifacts"]
    assert len(rows) == 1
    output = rows[0]
    assert output["id"] == "answer#0"
    assert output["digest"].removeprefix("sha256:") == hashlib.sha256(b"complete output\n").hexdigest()
    assert output["media_type"] == "text/plain"
    assert output["size"] == len(b"complete output\n")


@pytest.mark.parametrize("cost_source,cost", [
    ("absent", "unknown"), ("child", 0), ("manifest", {"amount": 0.25, "currency": "USD"}),
])
def test_run_task_success_writes_serialized_result_and_preserves_legacy_manifest(
    host_case, monkeypatch, cost_source, cost,
):
    case = host_case
    _success_boundary(
        case, monkeypatch,
        payload={"warnings": ["child warning"], **({"cost": cost} if cost_source == "child" else {})},
        manifest_extras={"warnings": ["manifest warning"], **({"cost": cost} if cost_source == "manifest" else {})},
    )

    settlement = case.host.run_task(case.task, lease_token="lease-result")

    receipt = _read_result(case, "succeeded")
    assert receipt["ok"] is True
    assert receipt["error"] is None
    assert receipt["cost"] == cost
    assert {"child warning", "manifest warning"} <= set(receipt["warnings"])
    assert receipt["raw_result"] == case.runtime.settlements[0][2]["result"]
    assert receipt["publication"] == {"status": "settled", "receipt": settlement, "effect": None}
    _assert_artifact(receipt)
    assert (case.root / "outputs" / "manifest.json").read_bytes() == case.legacy_bytes
    assert len(list(case.root.glob("result.json"))) == 1
    assert not (case.root / "outputs" / "result.json").exists()


def test_run_task_partial_failure_preserves_verified_outputs_and_coded_error(host_case, monkeypatch):
    case = host_case
    error = AstridError("provider stopped after one output", code="provider_failed", field="prompt", fix="retry remaining output")

    def execute(_record, _inputs, output_root, _attempt, **_kwargs):
        _legacy_output(case, output_root, warnings=["only one output finished"], cost=0)
        raise error

    monkeypatch.setattr(case.host, "_run_command_definition", execute)
    with pytest.raises(AstridError) as raised:
        case.host.run_task(case.task, lease_token="lease-result")

    assert raised.value is error
    receipt = _read_result(case, "partial")
    assert receipt["ok"] is False
    assert receipt["cost"] == 0
    assert receipt["error"] == {"code": "provider_failed", "message": str(error), "field": "prompt", "fix": "retry remaining output"}
    assert "only one output finished" in receipt["warnings"]
    assert receipt["publication"] == {"status": "not_settled", "receipt": None, "effect": None}
    _assert_artifact(receipt)
    assert (case.root / "outputs" / "manifest.json").read_bytes() == case.legacy_bytes
    assert not case.runtime.settlements
    assert len(case.runtime.failures) == 1


def test_run_task_zero_output_failure_writes_empty_result_and_coded_error(host_case, monkeypatch):
    case = host_case
    error = AstridError("missing source", code="invalid_input", field="source", fix="provide a source")

    def execute(*_args, **_kwargs):
        raise error

    monkeypatch.setattr(case.host, "_run_command_definition", execute)
    with pytest.raises(AstridError) as raised:
        case.host.run_task(case.task, lease_token="lease-result")

    assert raised.value is error
    receipt = _read_result(case, "failed")
    assert receipt["ok"] is False
    assert receipt["outputs"]["artifacts"] == []
    assert receipt["cost"] == "unknown"
    assert receipt["error"] == {"code": "invalid_input", "message": "missing source", "field": "source", "fix": "provide a source"}
    assert receipt["publication"]["status"] == "not_settled"
    assert len(case.runtime.failures) == 1


def test_run_task_host_cancelled_writes_cancelled_result(host_case, monkeypatch):
    case = host_case

    def execute(*_args, **_kwargs):
        raise HostCancelled("fixture cancellation")

    monkeypatch.setattr(case.host, "_run_command_definition", execute)
    returned = case.host.run_task(case.task, lease_token="lease-result")

    assert returned["cancelled"] is True
    receipt = _read_result(case, "cancelled")
    assert receipt["ok"] is False
    assert receipt["outputs"]["artifacts"] == []
    assert receipt["cost"] == "unknown"
    assert set(receipt["error"]) == {"code", "message", "field", "fix"}
    assert receipt["error"]["message"]
    assert receipt["publication"]["status"] == "not_settled"
    assert not case.runtime.failures
    assert not case.runtime.settlements


def test_run_task_deadline_writes_timeout_result_and_terminalizes_exact_attempt(host_case, monkeypatch):
    case = host_case
    expired = False
    monkeypatch.setattr(generic_host._AttemptLifetime, "expired", lambda _self: expired)

    def execute(*_args, **kwargs):
        nonlocal expired
        expired = True
        assert kwargs["cancelled"]() is True
        raise HostCancelled("deadline interrupted child")

    monkeypatch.setattr(case.host, "_run_command_definition", execute)
    returned = case.host.run_task(case.task, lease_token="lease-result")

    assert returned["deadline_exceeded"] is True
    receipt = _read_result(case, "timeout")
    assert receipt["ok"] is False
    assert receipt["outputs"]["artifacts"] == []
    assert receipt["cost"] == "unknown"
    assert "deadline" in receipt["error"]["message"]
    assert set(receipt["error"]) == {"code", "message", "field", "fix"}
    assert receipt["publication"]["status"] == "not_settled"
    assert len(case.runtime.failures) == 1
    task_id, lease_id, message, kwargs = case.runtime.failures[0]
    assert (task_id, lease_id, message) == ("task-result", "lease-result", "execution deadline exceeded")
    assert kwargs["attempt_id"] == "attempt-result" and kwargs["fence"] == 3
    assert not case.runtime.settlements


def test_run_task_early_admission_failure_writes_structured_result(host_case, monkeypatch):
    case = host_case
    error = AstridError("invalid admission", code="invalid_input", field="mode", fix="select a supported mode")

    def reject(_metadata):
        raise error

    monkeypatch.setattr(generic_host, "_storage_input_limits", reject)
    with pytest.raises(HostError, match="invalid admission"):
        case.host.run_task(case.task, lease_token="lease-result")

    receipt = _read_result(case, "failed")
    assert receipt["error"] == {"code": "invalid_input", "message": "invalid admission", "field": "mode", "fix": "select a supported mode"}
    assert receipt["outputs"]["artifacts"] == []
    assert receipt["cost"] == "unknown"
    assert not case.runtime.heartbeats
    assert len(case.runtime.failures) == 1


def test_run_task_initial_heartbeat_failure_writes_result_before_child_launch(host_case, monkeypatch):
    case = host_case

    def lost_heartbeat(*_args, **_kwargs):
        raise RuntimeError("lease heartbeat unavailable")

    monkeypatch.setattr(case.runtime, "heartbeat", lost_heartbeat)
    with pytest.raises(RuntimeError, match="lease heartbeat unavailable"):
        case.host.run_task(case.task, lease_token="lease-result")

    receipt = _read_result(case, "failed")
    assert receipt["error"] == {"code": "executor_failed", "message": "lease heartbeat unavailable", "field": None, "fix": ""}
    assert receipt["outputs"]["artifacts"] == []
    assert receipt["cost"] == "unknown"
    assert not case.runtime.settlements


def test_run_task_writes_result_before_normal_ephemeral_cleanup(host_case, monkeypatch):
    case = host_case
    case.host.attempt_root = None
    _success_boundary(case, monkeypatch)
    observed = []
    cleanup = case.host._cleanup_ephemeral_attempt_or_latch

    def inspect_and_cleanup(root):
        observed.append(_read_result(case, "succeeded", root=root))
        cleanup(root)
        assert not root.exists()

    monkeypatch.setattr(case.host, "_cleanup_ephemeral_attempt_or_latch", inspect_and_cleanup)
    case.host.run_task(case.task, lease_token="lease-result")

    assert len(observed) == 1
    assert observed[0]["ok"] is True
    _assert_artifact(observed[0])


def test_run_task_tampered_partial_output_is_not_described_as_verified(host_case, monkeypatch):
    case = host_case

    def execute(_record, _inputs, output_root, _attempt, **_kwargs):
        _legacy_output(case, output_root, cost=99)
        (output_root / "answer.txt").write_bytes(b"tampered after receipt")
        raise AstridError("child failed", code="provider_failed", fix="retry")

    monkeypatch.setattr(case.host, "_run_command_definition", execute)
    with pytest.raises(AstridError, match="child failed"):
        case.host.run_task(case.task, lease_token="lease-result")

    receipt = _read_result(case, "failed")
    assert receipt["outputs"]["artifacts"] == []
    assert receipt["cost"] == "unknown"
    assert any("hash" in warning for warning in receipt["warnings"])
    assert (case.root / "outputs" / "manifest.json").read_bytes() == case.legacy_bytes


def test_request_digest_does_not_depend_on_attempt_or_materialized_paths(host_case, monkeypatch):
    case = host_case
    materialize = case.host._materialize_inputs

    def materialize_with_local_path(spec, root, **kwargs):
        return {**materialize(spec, root, **kwargs), "snapshot": str(root / "inputs" / "snapshot.json")}

    monkeypatch.setattr(case.host, "_materialize_inputs", materialize_with_local_path)
    _success_boundary(case, monkeypatch)
    case.host.run_task(case.task, lease_token="lease-result")
    first = _read_result(case, "succeeded")
    case.root = case.root.with_name("another-run")
    case.host.attempt_root = case.root

    case.host.run_task(case.task, lease_token="lease-result")
    second = _read_result(case, "succeeded")

    assert first["request_digest"] == second["request_digest"]
    assert first["resolved_options"]["snapshot"] != second["resolved_options"]["snapshot"]
