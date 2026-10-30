from __future__ import annotations

import hashlib
import json
import os
import socket
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from astrid.core.execution import _child_import_receipt as receipt
from astrid.core.execution.generic_host import GenericPackHost

_AGENT_ROOT = Path(__file__).resolve().parents[3]
_PACK_ROOT = _AGENT_ROOT / "astrid" / "packs"


def _send(channel, packet):
    channel.sendall(len(packet).to_bytes(4, "big") + packet)


@pytest.fixture
def leaf_cleanup(monkeypatch):
    """These offline commands spawn no descendants; reap their real leader.

    Production custody is covered by its own tests. A full Darwin census is
    unavailable in this test sandbox, and is unrelated to import observation.
    """
    from astrid.core.execution import generic_host

    monkeypatch.setattr(generic_host, "_terminate_process_group", lambda process: process.wait(timeout=5))


def _host(tmp_path: Path) -> GenericPackHost:
    host = GenericPackHost(pack_roots=[_PACK_ROOT], attempt_root=tmp_path / "attempts")
    (tmp_path / "support").mkdir()
    host.evidence_status_path = tmp_path / "support" / "evidence-status.json"
    return host


def _invoke(host: GenericPackHost, capability_id: str, inputs: dict[str, object],
            tmp_path: Path, *, bridge=None):
    host.discover()
    if capability_id == "h3_av.transform":
        # H3 is excluded from Runtime executor registration. This projection
        # tests the command worker/runpy mechanism separately from the actual
        # supported local-parent route tested below.
        from astrid.core.pack.loader import load_pack_manifest
        from astrid.core.execution.executor.actions import _action_definition
        from astrid.core.execution.generic_host import CapabilityRecord, _capability_digest

        pack = load_pack_manifest(_PACK_ROOT / "h3_av" / "pack.yaml")
        definition = _action_definition(pack, "transform", pack.actions["transform"])
        host.capabilities[capability_id] = CapabilityRecord(
            definition, _capability_digest(definition.to_dict()), "unit-source", pack.root)
    attempt = tmp_path / "attempt"
    output = attempt / "outputs"
    output.mkdir(parents=True)
    return host._run_command_definition(
        host.capabilities[capability_id], inputs, output, attempt,
        child_bridge=bridge,
        import_binding={
            "task_id": "task-local", "run_id": "run-local", "attempt_id": "attempt-local",
            "fence": 1, "runtime_epoch": 1, "operation_id": "operation-local",
            "submission_id": "submission-local",
        },
    )


class _SocketBridge:
    """Minimal inherited bridge transport; the validator makes no bridge calls."""

    def __init__(self) -> None:
        self.host_socket, self.child_socket = socket.socketpair()

    def inherited_fd(self) -> int:
        return self.child_socket.fileno()

    def launched(self) -> None:
        self.child_socket.close()

    def close(self) -> None:
        self.child_socket.close()
        self.host_socket.close()


def _validate_inputs(tmp_path: Path) -> dict[str, object]:
    workflow = tmp_path / "workflow.json"
    workflow.write_text('{"nodes": [{"id": 1, "type": "Integer", "widgets_values": [7]}], "links": []}', encoding="utf-8")
    return {"workflow": str(workflow)}


def test_direct_validator_subprocess_records_action_entry_and_host_binding(tmp_path: Path, leaf_cleanup, monkeypatch) -> None:
    monkeypatch.setenv("PYTHONPATH", str(tmp_path / "launcher-only-source"))
    host = _host(tmp_path)
    result = _invoke(host, "vibecomfy.validate", _validate_inputs(tmp_path), tmp_path)
    ref = _reference(result.payload["child_import_receipt"])
    assert ref["observation"] == "recorded"
    path = host.evidence_status_path.parent / "child-import-receipts" / f"{ref['launch_id']}.json"
    stored = json.loads(path.read_bytes())
    assert stored["binding"]["task_id"] == "task-local"
    assert stored["binding"]["attempt_id"] == "attempt-local"
    action = next(row for row in stored["observations"] if row["phase"] == "action_entry")
    validate = next(row for row in action["modules"] if row["module_name"] == "__main__")
    assert validate["status"] == "loaded"
    assert Path(validate["resolved_path"]).resolve() == (
        _PACK_ROOT / "vibecomfy" / "actions" / "validate" / "run.py"
    ).resolve()
    assert all(row["status"] == "not_loaded" for row in action["modules"]
               if row["module_name"] in {"astrid.core.execution.generic_host",
                                          "astrid.core.execution._child_bridge",
                                          "astrid.packs.h3_av.actions.transform.run"})


def test_bridge_wrapped_validator_records_worker_and_action_entry(tmp_path: Path, leaf_cleanup) -> None:
    host = _host(tmp_path)
    bridge = _SocketBridge()
    try:
        result = _invoke(
            host, "vibecomfy.validate", _validate_inputs(tmp_path), tmp_path, bridge=bridge,
        )
    finally:
        bridge.close()
    ref = _reference(result.payload["child_import_receipt"])
    assert ref["observation"] == "recorded"
    path = host.evidence_status_path.parent / "child-import-receipts" / f"{ref['launch_id']}.json"
    stored = json.loads(path.read_bytes())
    assert [row["phase"] for row in stored["observations"]] == ["worker_entry", "action_entry"]
    assert stored["child"]["pid"] != os.getpid()
    worker = stored["observations"][0]
    assert worker["modules"][0]["spec_name"] == "astrid.core.execution.generic_host_worker"
    assert worker["modules"][0]["sha256"] == hashlib.sha256(Path(worker["modules"][0]["resolved_path"]).read_bytes()).hexdigest()
    assert worker["pid"] == stored["child"]["pid"]
    assert worker["process_birth_id"] == stored["child"]["process_birth_id"]


def test_h3_transform_dry_run_subprocess_records_runpy_action_entry(tmp_path: Path, leaf_cleanup) -> None:
    host = _host(tmp_path)
    request = tmp_path / "request.json"
    asset_map = tmp_path / "asset-map.json"
    request.write_text("{}", encoding="utf-8")
    asset_map.write_text("{}", encoding="utf-8")
    bridge = _SocketBridge()
    try:
        _invoke(host, "h3_av.transform", {
            "request": str(request), "asset_map": str(asset_map), "dry_run": True,
        }, tmp_path, bridge=bridge)
    except Exception as exc:
        ref = getattr(exc, "child_import_receipt", None)
        assert ref is not None, "H3 dry-run failure should retain its action-entry receipt"
        ref = _reference(ref)
    else:
        pytest.fail("empty H3 request should fail validation after action entry")
    finally:
        bridge.close()
    assert ref["observation"] == "recorded"
    path = host.evidence_status_path.parent / "child-import-receipts" / f"{ref['launch_id']}.json"
    stored = json.loads(path.read_bytes())
    action = next(row for row in stored["observations"] if row["phase"] == "action_entry")
    entry = next(row for row in action["modules"] if row["module_name"] == "__main__")
    assert entry["status"] == "loaded"
    assert entry["spec_name"] == "astrid.packs.h3_av.actions.transform.run"
    assert next(row for row in action["modules"] if row["module_name"] == entry["spec_name"])["status"] == "not_loaded"


def _reference(value: object) -> dict[str, object]:
    assert isinstance(value, dict)
    assert set(value) == {
        "schema_version", "launch_id", "sha256", "observation", "persistence",
    }
    assert value["schema_version"] == 1
    assert isinstance(value["launch_id"], str) and len(value["launch_id"]) == 32
    assert value["observation"] in {"recorded", "unavailable", "invalid"}
    assert value["persistence"] in {"recorded", "failed"}
    digest = value["sha256"]
    assert digest == "unavailable" or (
        isinstance(digest, str) and len(digest) == 64
        and all(char in "0123456789abcdef" for char in digest)
    )
    return value


def test_action_entry_snapshot_uses_only_current_modules_and_hashes_loaded_files() -> None:
    observed = receipt._snapshot("action_entry")
    assert set(observed) == {"pid", "process_birth_id", "observed_at_utc", "phase", "modules"}
    assert observed["pid"] == os.getpid()
    assert observed["process_birth_id"] == receipt._birth(os.getpid())
    assert observed["phase"] == "action_entry"
    assert isinstance(observed["observed_at_utc"], str) and observed["observed_at_utc"]
    modules = observed["modules"]
    assert isinstance(modules, list) and len(modules) == len(receipt._MODULES)
    names = {row["module_name"] for row in modules}
    assert "__main__" in names
    for row in modules:
        assert set(row) == {
            "module_name", "status", "actual_file", "resolved_path", "sha256",
            "spec_name", "import_mode",
        }
        assert row["status"] in {"loaded", "not_loaded", "unavailable"}
        if row["status"] == "loaded":
            path = Path(row["resolved_path"])
            assert path.is_absolute()
            assert row["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
        else:
            assert row["sha256"] == "unavailable"


def test_host_binds_receipt_to_owned_child_and_persists_private_bounded_record(tmp_path: Path) -> None:
    host = _host(tmp_path)
    binding = {
        "task_id": "task-1", "run_id": "run-1", "attempt_id": "attempt-1",
        "fence": 3, "runtime_epoch": 4, "parent_task_id": "parent-task",
        "parent_attempt_id": "parent-attempt", "operation_id": "op-1",
        "submission_id": "submission-1",
    }
    receipt_writer = receipt.ChildImportReceipt(
        host, capability_id="vibecomfy.validate", binding=binding,
    )
    try:
        env: dict[str, str] = {}
        (fd,) = receipt_writer.prepare(env)
        assert env == {"_ASTRID_CHILD_IMPORT_FD": str(fd)}
        child_socket = socket.socket(fileno=os.dup(fd))
        main = sys.modules["__main__"]
        old_spec = main.__spec__
        main.__spec__ = SimpleNamespace(name="astrid.packs.vibecomfy.actions.validate.run")
        try:
            _send(child_socket, json.dumps(receipt._snapshot("action_entry"), separators=(",", ":")).encode())
        finally:
            main.__spec__ = old_spec
        child_socket.close()

        process = SimpleNamespace(pid=os.getpid())
        receipt_writer.launched(process)
        ref = _reference(receipt_writer.finish())
    finally:
        receipt_writer.close()

    assert ref["observation"] == "recorded"
    assert ref["persistence"] == "recorded"
    path = host.evidence_status_path.parent / "child-import-receipts" / f"{ref['launch_id']}.json"
    assert path.exists()
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700
    raw = path.read_bytes()
    assert len(raw) <= 64 * 1024
    assert hashlib.sha256(raw).hexdigest() == ref["sha256"]
    stored = json.loads(raw)
    assert stored["binding"] == binding
    assert stored["capability_id"] == "vibecomfy.validate"
    assert stored["child"] == {
        "pid": os.getpid(), "process_birth_id": receipt._birth(os.getpid()),
    }


def test_receipt_does_not_accept_child_supplied_host_or_task_identity(tmp_path: Path) -> None:
    host = _host(tmp_path)
    writer = receipt.ChildImportReceipt(
        host, capability_id="h3_av.transform",
        binding={"task_id": "host-task", "attempt_id": "host-attempt", "fence": 2},
    )
    try:
        env: dict[str, str] = {}
        (fd,) = writer.prepare(env)
        child_socket = socket.socket(fileno=os.dup(fd))
        _send(child_socket, json.dumps({
            "host": {"pid": -1, "birth_id": "forged"},
            "binding": {"task_id": "forged-task", "attempt_id": "forged-attempt", "fence": 99},
            "observations": [],
        }, separators=(",", ":")).encode())
        child_socket.close()
        writer.launched(SimpleNamespace(pid=os.getpid()))
        ref = _reference(writer.finish())
    finally:
        writer.close()

    assert ref["observation"] in {"invalid", "unavailable"}
    assert ref["persistence"] == "recorded"
    path = host.evidence_status_path.parent / "child-import-receipts" / f"{ref['launch_id']}.json"
    stored = json.loads(path.read_bytes())
    assert stored["binding"]["task_id"] == "host-task"
    assert stored["binding"]["attempt_id"] == "host-attempt"
    assert stored["host"]["pid"] == os.getpid()
    assert stored["host"]["process_birth_id"] == receipt._birth(os.getpid())


def test_receipt_marks_missing_child_observation_and_persistence_failure(tmp_path: Path, monkeypatch) -> None:
    host = _host(tmp_path)
    writer = receipt.ChildImportReceipt(host, capability_id="vibecomfy.validate")
    try:
        env: dict[str, str] = {}
        writer.prepare(env)
        writer.launched(SimpleNamespace(pid=os.getpid()))
        monkeypatch.setattr(receipt.Path, "mkdir", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("disk unavailable")))
        ref = _reference(writer.finish())
    finally:
        writer.close()
    assert ref["observation"] == "unavailable"
    assert ref["persistence"] == "failed"
    assert ref["sha256"] == "unavailable"


def test_child_receipt_rejects_oversized_packet(tmp_path: Path) -> None:
    host = _host(tmp_path)
    writer = receipt.ChildImportReceipt(host, capability_id="vibecomfy.validate")
    try:
        env: dict[str, str] = {}
        (fd,) = writer.prepare(env)
        child_socket = socket.socket(fileno=os.dup(fd))
        _send(child_socket, b"x" * 30000)
        child_socket.close()
        writer.launched(SimpleNamespace(pid=os.getpid()))
        ref = _reference(writer.finish())
    finally:
        writer.close()
    assert ref["observation"] == "invalid"


def test_actual_local_h3_launcher_observes_child_without_inventing_runtime_identity(tmp_path, monkeypatch):
    import subprocess
    from astrid.core.execution import process_group
    from astrid.core.execution.executor.actions import action_orchestrator_definition
    from astrid.core.execution.orchestrator.registry import OrchestratorRegistry
    from astrid.core.execution.orchestrator.runner import OrchestratorRunRequest, run_orchestrator
    from astrid.core.pack.loader import load_pack_manifest

    # Real leaf interpreter at the runner's owned launch seam. Custody's native
    # census is independently tested and unavailable in this sandbox.
    monkeypatch.setattr(process_group, "popen_owned_process", subprocess.Popen)
    monkeypatch.setattr(process_group, "verify_tree_absent", lambda process: process.wait(timeout=5))
    support = tmp_path / "support"
    support.mkdir()
    monkeypatch.setenv("ASTRID_RUNTIME_DATA_ROOT", str(support))
    monkeypatch.setenv("PYTHONPATH", str(_AGENT_ROOT))
    pack = load_pack_manifest(_PACK_ROOT / "h3_av" / "pack.yaml")
    definition = action_orchestrator_definition(pack, "transform", pack.actions["transform"])
    request = tmp_path / "request.json"
    assets = tmp_path / "assets.json"
    request.write_text("{}")
    assets.write_text("{}")
    result = run_orchestrator(OrchestratorRunRequest(
        "h3_av.transform", out=tmp_path / "out", project="unit-project",
        project_was_auto_resolved=True, inputs={"request": str(request), "asset_map": str(assets), "dry_run": True},
    ), OrchestratorRegistry([definition]))
    assert result.returncode == 2  # H3's original request-contract error code.
    ref = _reference(result.to_dict()["child_import_receipt"])
    assert ref["observation"] == "recorded"
    path = support / "child-import-receipts" / (ref["launch_id"] + ".json")
    stored = json.loads(path.read_bytes())
    assert stored["child"]["pid"] != os.getpid()
    assert stored["binding"]["task_id"] == stored["binding"]["attempt_id"] == "unavailable"
    assert stored["host"]["execution_profile"] == "local_orchestrator"
    main = stored["observations"][0]["modules"][0]
    assert main["spec_name"] == "astrid.packs.h3_av.actions.transform.run"
    assert main["sha256"] == hashlib.sha256(Path(main["resolved_path"]).read_bytes()).hexdigest()


@pytest.mark.parametrize("file_value", [None, "/missing/module.py", "x" * 1025])
def test_loaded_module_with_unavailable_file_is_explicit(tmp_path, monkeypatch, file_value):
    from types import ModuleType

    name = "astrid.packs.h3_av.actions.transform.run"
    monkeypatch.delitem(sys.modules, name, raising=False)
    absent = receipt._module(name)
    assert absent["status"] == "not_loaded"
    assert name not in sys.modules
    module = ModuleType(name)
    module.__file__ = file_value
    monkeypatch.setitem(sys.modules, name, module)
    observed = receipt._module(name)
    assert observed["status"] == "unavailable"
    assert observed["sha256"] == "unavailable"
    assert observed["import_mode"] == "already_loaded"


def test_launcher_configuration_and_host_hashes_cannot_replace_child_observation(tmp_path):
    host = _host(tmp_path)
    host._child_import_host_context = {"source_checkout": "/launcher/candidate", "source_closure_digest": "f" * 64}
    writer = receipt.ChildImportReceipt(host, "vibecomfy.validate", {"task_id": "owned-task"})
    env = {"PYTHONPATH": "/launcher/candidate", "_ASTRID_CHILD_IMPORT_FD": "123456"}
    writer.prepare(env)
    writer.launched(SimpleNamespace(pid=os.getpid()))
    reference = writer.finish()
    assert reference["observation"] == "unavailable"
    path = host.evidence_status_path.parent / "child-import-receipts" / (reference["launch_id"] + ".json")
    assert json.loads(path.read_bytes())["observations"] == []
    assert "/launcher" not in json.dumps(reference)


@pytest.mark.parametrize("workload_fails", [False, True])
def test_receipt_write_failure_preserves_actual_child_outcome(tmp_path, monkeypatch, leaf_cleanup, caplog, workload_fails):
    from astrid.core.execution.generic_host import HostError

    host = _host(tmp_path)
    inputs = _validate_inputs(tmp_path)
    if workload_fails:
        Path(inputs["workflow"]).write_text('{"nodes":[],"links":[]}')

    def fail_write(*args):
        raise OSError("EVIDENCE_SECRET_EXCEPTION_TEXT")

    monkeypatch.setattr(receipt.ChildImportReceipt, "_persist", fail_write)
    if workload_fails:
        with pytest.raises(HostError, match="workflow validation reported errors") as failure:
            _invoke(host, "vibecomfy.validate", inputs, tmp_path)
        reference = failure.value.child_import_receipt
        assert "EVIDENCE_SECRET" not in str(failure.value)
    else:
        result = _invoke(host, "vibecomfy.validate", inputs, tmp_path)
        assert result.returncode == 0
        reference = result.payload["child_import_receipt"]
    assert _reference(reference)["persistence"] == "failed"
    assert reference["observation"] == "recorded"
    assert "child import receipt persistence failed" in caplog.text
    assert "EVIDENCE_SECRET" not in caplog.text + json.dumps(reference)
