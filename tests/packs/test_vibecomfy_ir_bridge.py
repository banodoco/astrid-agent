from __future__ import annotations

import hashlib
import importlib
import json
import os
import subprocess
import sys
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType, ModuleType, SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from astrid.core.execution.executor.registry import load_default_registry
from astrid.core.gateway.dispatch import _top_level_commands
from astrid.packs.vibecomfy.shared.bundle_inputs import (
    CanonicalBundleInputError,
    staged_workflow_path,
)
from astrid.packs.vibecomfy.shared.workflow_ir import (
    WorkflowIrBridgeError,
    _diagnostic_payload,
    edit_workflow,
    inspect_canonical_bundle,
    inspect_workflow,
)

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tests" / "fixtures" / "vibecomfy_ir" / "flat.json"


def _write_operations(path: Path, ops: list[dict[str, object]]) -> None:
    path.write_text(
        json.dumps({"schema_version": 1, "expected_revision": 0, "ops": ops}),
        encoding="utf-8",
    )


def _frozen_ksampler_schema_provider():
    from vibecomfy.schema import (
        FrozenSchemaSnapshotProvider,
        InputSpec,
        NodeSchema,
        capture_schema_snapshot,
        schema_payload_from_node_schema,
    )

    schema = NodeSchema(
        class_type="KSampler",
        pack="fixture",
        inputs={
            "model": InputSpec(type="MODEL", required=True),
            "positive": InputSpec(type="CONDITIONING", required=True),
            "negative": InputSpec(type="CONDITIONING", required=True),
            "latent_image": InputSpec(type="LATENT", required=True),
            "seed": InputSpec(type="INT", default=42),
            "steps": InputSpec(type="INT", default=20, min=1),
            "cfg": InputSpec(type="FLOAT", default=8),
            "sampler_name": InputSpec(type="COMBO", choices=["euler"]),
            "scheduler": InputSpec(type="COMBO", choices=["normal"]),
            "denoise": InputSpec(type="FLOAT", default=1),
        },
        outputs=[],
        widget_input_order=(
            "seed",
            "control_after_generate",
            "steps",
            "cfg",
            "sampler_name",
            "scheduler",
            "denoise",
        ),
    )
    snapshot = capture_schema_snapshot(
        class_types=("KSampler",),
        request_snapshot={
            "contract_version": "schema_snapshot_v1",
            "schemas": {"KSampler": schema_payload_from_node_schema("KSampler", schema)},
            "missing_classes": [],
        },
        node_classes={"5": "KSampler"},
    )
    return FrozenSchemaSnapshotProvider(snapshot)


def test_diagnostic_payload_serializes_frozen_mapping_details() -> None:
    @dataclass(frozen=True)
    class Diagnostic:
        code: str
        message: str
        severity: str
        detail: object

    diagnostic = Diagnostic(
        code="stale_revision",
        message="expected revision differs from current revision",
        severity="error",
        detail=MappingProxyType(
            {
                "revision": MappingProxyType({"expected": 1, "current": 0}),
                "path": ("workflow", "nodes"),
            }
        ),
    )

    payload = _diagnostic_payload(diagnostic)

    assert payload == {
        "code": "stale_revision",
        "message": "expected revision differs from current revision",
        "severity": "error",
        "detail": {
            "revision": {"expected": 1, "current": 0},
            "path": ["workflow", "nodes"],
        },
    }
    assert json.loads(json.dumps(payload)) == payload


def test_ir_executors_are_manifested_without_growing_the_gateway() -> None:
    registry = load_default_registry()
    inspect_manifest = registry.get("vibecomfy.inspect")
    edit_manifest = registry.get("vibecomfy.edit")
    import_manifest = registry.get("vibecomfy.import")
    validate_manifest = registry.get("vibecomfy.validate")
    run_manifest = registry.get("vibecomfy.run")

    assert inspect_manifest.id == "vibecomfy.inspect"
    assert edit_manifest.id == "vibecomfy.edit"
    assert import_manifest.id == "vibecomfy.import"
    assert inspect_manifest.isolation.network is False
    assert edit_manifest.isolation.network is False
    assert import_manifest.isolation.network is False
    assert inspect_manifest.metadata["mutation"] == "none"
    assert {item.name for item in inspect_manifest.inputs} == {
        "workflow", "python", "companion", "source", "python_execution_consent"
    }
    assert {item.name for item in validate_manifest.inputs} == {
        "workflow", "python", "companion", "source", "python_execution_consent",
        "workflow_inputs",
    }
    assert {item.name for item in run_manifest.inputs} == {
        "comfy_url",
        "comfy_output_dir",
        "workflow",
        "python",
        "companion",
        "source",
        "managed_assets",
        "workflow_inputs",
        "source_video",
        "source_video_node",
        "source_video_widget",
        "attempt_identity",
        "task_identity",
        "execution_identity",
        "readiness_profile_path",
        "readiness_profile_hash",
    }
    assert {item.name for item in import_manifest.inputs} == {"source", "workflow_id"}
    assert {item.name for item in import_manifest.outputs} == {
        "python",
        "companion",
        "source",
        "report",
    }
    assert {
        item.name: item.path_template for item in import_manifest.outputs
    } == {
        "python": "{out}/workflow.py",
        "companion": "{out}/workflow.vibe.json",
        "source": "{out}/source.json",
        "report": "{out}/edit-report.json",
    }
    assert {output.name for output in inspect_manifest.outputs} == {
        "projection",
        "inspection",
    }
    assert {output.name for output in edit_manifest.outputs} == {
        "python",
        "companion",
        "source",
        "report",
    }
    assert {output.name for output in validate_manifest.outputs} == {"validation"}
    consent_port = next(
        item for item in edit_manifest.inputs if item.name == "python_execution_consent"
    )
    assert consent_port.required is True and consent_port.default is None
    assert next(
        item for item in inspect_manifest.inputs if item.name == "python_execution_consent"
    ).default is None
    assert next(
        item for item in validate_manifest.inputs if item.name == "python_execution_consent"
    ).default is None
    assert {item.name for item in edit_manifest.inputs} == {
        "python",
        "companion",
        "source",
        "workflow_id",
        "parent_revision",
        "parent_task_id",
        "origin_task_id",
        "transition_kind",
        "python_execution_consent",
        "operations",
        "capture_python",
        "capture_graph",
    }
    assert _top_level_commands() == frozenset(
        {
        "projects",
        "timelines",
        "media",
        "documents",
        "preferences",
        "tasks",
        "runs",
        "doctor",
        "backup",
        "skills",
        "generate",
        "dev",
        }
    )


def test_canonical_bundle_staging_preserves_exact_siblings_and_rejects_bad_modes(
    tmp_path: Path,
) -> None:
    members = {
        "python": b"workflow = load('fixture')\n",
        "companion": b'{"revision_id":"rev-7"}\r\n',
        "source": b'{"nodes":[],"links":[]}\r\n',
    }
    paths = {name: tmp_path / name for name in members}
    for name, path in paths.items():
        path.write_bytes(members[name])

    with staged_workflow_path(
        workflow=None,
        python=paths["python"],
        companion=paths["companion"],
        source=paths["source"],
    ) as (workflow_path, authority):
        assert authority == "canonical_bundle"
        assert workflow_path.name == "workflow.py"
        assert workflow_path.read_bytes() == members["python"]
        assert workflow_path.with_name("workflow.vibe.json").read_bytes() == members["companion"]
        assert workflow_path.with_name("source.json").read_bytes() == members["source"]
        staging_path = workflow_path.parent
    assert not staging_path.exists()
    assert {name: path.read_bytes() for name, path in paths.items()} == members

    with pytest.raises(CanonicalBundleInputError, match="requires python"):
        with staged_workflow_path(
            workflow=None,
            python=paths["python"],
            companion=None,
            source=paths["source"],
        ):
            pass
    with pytest.raises(CanonicalBundleInputError, match="either one workflow JSON"):
        with staged_workflow_path(
            workflow=paths["source"],
            python=paths["python"],
            companion=paths["companion"],
            source=paths["source"],
        ):
            pass


def test_canonical_validate_and_run_stage_bundle_for_package_loader(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ASTRID_INTERNAL_INVOCATION", "1")
    source_bytes = b'{"nodes":[],"links":[]}\n'
    inputs = {
        "python": tmp_path / "workflow.py",
        "companion": tmp_path / "workflow.vibe.json",
        "source": tmp_path / "source.json",
    }
    inputs["python"].write_bytes(b"workflow = load('fixture')\n")
    inputs["companion"].write_bytes(b'{"revision_id":"rev-origin"}\n')
    inputs["source"].write_bytes(source_bytes)

    validate = importlib.import_module("astrid.packs.vibecomfy.actions.validate.run")
    staged_validate_members = {}

    security = ModuleType("vibecomfy.security")
    active_gate = ContextVar("validate_fake_gate", default=None)

    class FakeGate:
        def __init__(self):
            self.audit = []

    security.current_gate_context = active_gate.get  # type: ignore[attr-defined]
    security.set_gate_context = active_gate.set  # type: ignore[attr-defined]
    cli = ModuleType("vibecomfy.cli")

    def capture_validate(argv):
        workflow_path = Path(argv[argv.index("validate") + 1])
        staged_validate_members["python"] = workflow_path.read_bytes()
        staged_validate_members["companion"] = workflow_path.with_name(
            "workflow.vibe.json"
        ).read_bytes()
        staged_validate_members["source"] = workflow_path.with_name(
            "source.json"
        ).read_bytes()
        assert "--yes" in argv and "--json" in argv and "--no-schema" in argv
        gate = FakeGate()
        gate.audit.append({"decision": "allow", "reason": "assume_yes_bypass"})
        security.set_gate_context(gate)  # type: ignore[attr-defined]
        print(json.dumps({"workflow_id": "fixture", "ok": True, "status": "ok", "issues": []}))
        return 0

    cli.main = Mock(side_effect=capture_validate)  # type: ignore[attr-defined]
    with patch.dict("sys.modules", {"vibecomfy.security": security, "vibecomfy.cli": cli}):
        assert validate.main(
            [
                "validate",
                "",
                "--python",
                str(inputs["python"]),
                "--companion",
                str(inputs["companion"]),
                "--source",
                str(inputs["source"]),
                "--python-execution-consent",
                "confirmed",
                "--out",
                str(tmp_path / "validation-output"),
            ]
        ) == 0
    validation_report = json.loads(
        (tmp_path / "validation-output" / "validation-report.json").read_text(
            encoding="utf-8"
        )
    )
    assert validation_report["python_execution_consent"] == "confirmed"
    assert validation_report["validation_mode"] == "canonical_bundle_structural"
    assert validation_report["runtime_validation"]["status"] == "deferred"
    assert validation_report["runtime_validation"]["executor"] == "vibecomfy.run"
    assert validation_report["security_gate_audit"][0]["reason"] == "assume_yes_bypass"
    assert staged_validate_members == {
        "python": inputs["python"].read_bytes(),
        "companion": inputs["companion"].read_bytes(),
        "source": source_bytes,
    }

    run = importlib.import_module("astrid.packs.vibecomfy.actions.run.run")
    staged_run_members = {}

    def capture_run(workflow_path, _output, **identity):
        staged_run_members["python"] = workflow_path.read_bytes()
        staged_run_members["companion"] = workflow_path.with_name(
            "workflow.vibe.json"
        ).read_bytes()
        staged_run_members["source"] = workflow_path.with_name("source.json").read_bytes()
        assert identity["task_identity"] == "task-1"
        return {}

    run_call = Mock(side_effect=capture_run)
    monkeypatch.setattr(run, "_run_and_settle", run_call)
    output = tmp_path / "run-output"
    assert run.main(
        [
            "run",
            "",
            "--python",
            str(inputs["python"]),
            "--companion",
            str(inputs["companion"]),
            "--source",
            str(inputs["source"]),
            "--out",
            str(output),
            "--task-identity",
            "task-1",
        ]
    ) == 0
    assert staged_run_members == staged_validate_members


def test_ui_json_validation_is_static_and_requires_no_python_consent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ASTRID_INTERNAL_INVOCATION", "1")
    validate = importlib.import_module("astrid.packs.vibecomfy.actions.validate.run")
    ui_path = tmp_path / "ui.json"
    ui_path.write_text('{"nodes":[],"links":[]}', encoding="utf-8")
    workflow = SimpleNamespace(
        id="static-ui",
        validate=lambda: SimpleNamespace(ok=True, issues=[]),
    )
    loader = ModuleType("vibecomfy.ingest.loader")
    loader.load_workflow_json = Mock(return_value={"nodes": [], "links": []})  # type: ignore[attr-defined]
    normalize = ModuleType("vibecomfy.ingest.normalize")
    normalize.from_ui = Mock(return_value=workflow)  # type: ignore[attr-defined]

    with patch.dict(
        "sys.modules",
        {
            "vibecomfy.ingest.loader": loader,
            "vibecomfy.ingest.normalize": normalize,
        },
    ):
        assert validate.main(
            ["validate", str(ui_path), "--out", str(tmp_path / "static-validation")]
        ) == 0

    report = json.loads(
        (tmp_path / "static-validation" / "validation-report.json").read_text(
            encoding="utf-8"
        )
    )
    assert report["validation_mode"] == "static_ui_graph"
    assert report["python_execution_consent"] is None
    assert report["security_gate_audit"] == []
    assert normalize.from_ui.call_args.kwargs["use_comfy_converter"] is False


def test_canonical_run_uses_managed_production_engine_without_gpu(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ASTRID_INTERNAL_INVOCATION", "1")
    run = importlib.import_module("astrid.packs.vibecomfy.actions.run.run")
    python_path = tmp_path / "workflow.py"
    python_path.write_bytes(b"workflow = VibeWorkflow(id='portrait')\n")
    python_path.with_name("workflow.vibe.json").write_text("{}", encoding="utf-8")
    python_path.with_name("source.json").write_text("{}", encoding="utf-8")
    output_root = tmp_path / "run-results"
    engine_result_path = output_root / "engine-output" / "engine-result.png"
    engine_result_path.parent.mkdir(parents=True)
    engine_result_path.write_bytes(b"fixture image bytes")
    from astrid.packs.vibecomfy import production_engine

    managed_run = Mock(return_value=(engine_result_path,))
    monkeypatch.setattr(production_engine, "run_workflow_path", managed_run)

    manifest = run._run_and_settle(
        python_path,
        output_root,
        task_identity="task-1",
        execution_identity="execution-1",
    )

    managed_run.assert_called_once_with(
        python_path,
        output_root,
        task_identity="task-1",
        expected_execution_identity="execution-1",
        profile_id="pip_embedded",
        hc03_profile=None,
    )
    assert manifest["outputs"][0]["path"].startswith("artifacts/")


def _load_import_runner(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ASTRID_INTERNAL_INVOCATION", "1")
    return importlib.import_module("astrid.packs.vibecomfy.actions.import.run")


def _fake_import_service_modules(service: Mock) -> dict[str, ModuleType]:
    package = ModuleType("vibecomfy")
    package.__path__ = []  # type: ignore[attr-defined]
    porting = ModuleType("vibecomfy.porting")
    porting.__path__ = []  # type: ignore[attr-defined]
    import_service = ModuleType("vibecomfy.porting.import_service")
    import_service.import_workflow_bytes = service  # type: ignore[attr-defined]
    return {
        "vibecomfy": package,
        "vibecomfy.porting": porting,
        "vibecomfy.porting.import_service": import_service,
    }


def _origin_artifacts(source_bytes: bytes, workflow_id: str) -> SimpleNamespace:
    member_bytes = {
        "workflow.py": b"workflow = load('fixture')\n",
        "workflow.vibe.json": b'{"revision_id":"rev-origin"}\n',
        "source.json": source_bytes,
    }
    digests = {
        name: "sha256:" + hashlib.sha256(data).hexdigest()
        for name, data in member_bytes.items()
    }
    report = {
        "schema_version": 1,
        "transition_kind": "origin",
        "workflow_id": workflow_id,
        "workflow_identity": f"workflow:{workflow_id}",
        "revision_id": "rev-origin",
        "parent_revision": None,
        "parent_task_id": None,
        "origin_task_id": None,
        "before": None,
        "after": {"revision_id": "rev-origin", "members": digests},
        "members": digests,
        "readiness": {"status": "ready"},
        "validation": {"status": "structural"},
    }
    return SimpleNamespace(
        source_bytes=source_bytes,
        python_bytes=member_bytes["workflow.py"],
        companion_bytes=member_bytes["workflow.vibe.json"],
        report=report,
    )


def _fake_transition_modules(
    *, transition_bundle, load_bundle, gate_context: bool = False
) -> dict[str, ModuleType]:
    package = ModuleType("vibecomfy")
    package.__path__ = []  # type: ignore[attr-defined]
    porting = ModuleType("vibecomfy.porting")
    porting.__path__ = []  # type: ignore[attr-defined]
    edit_package = ModuleType("vibecomfy.porting.edit")
    edit_package.__path__ = []  # type: ignore[attr-defined]
    bundle_service = ModuleType("vibecomfy.porting.edit.bundle_service")
    bundle_service.transition_bundle = transition_bundle  # type: ignore[attr-defined]
    workflow_bundle = ModuleType("vibecomfy.workflow_bundle")
    workflow_bundle.load_bundle = load_bundle  # type: ignore[attr-defined]
    modules = {
        "vibecomfy": package,
        "vibecomfy.porting": porting,
        "vibecomfy.porting.edit": edit_package,
        "vibecomfy.porting.edit.bundle_service": bundle_service,
        "vibecomfy.workflow_bundle": workflow_bundle,
    }
    if gate_context:
        security = ModuleType("vibecomfy.security")
        active_context = ContextVar("fake_vibecomfy_gate_context", default=None)

        class FakeGateContext:
            def __init__(self, *, non_interactive, assume_yes):
                self.non_interactive = non_interactive
                self.assume_yes = assume_yes
                self.audit = []

        security.GateContext = FakeGateContext  # type: ignore[attr-defined]
        security.set_gate_context = active_context.set  # type: ignore[attr-defined]
        security.current_gate_context = active_context.get  # type: ignore[attr-defined]
        modules["vibecomfy.security"] = security
    return modules


def test_import_calls_shared_service_and_emits_complete_origin_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = _load_import_runner(monkeypatch)
    source_bytes = b'{"nodes":[],"links":[]}\r\n'
    source_path = tmp_path / "source.json"
    source_path.write_bytes(source_bytes)
    service = Mock(return_value=_origin_artifacts(source_bytes, "portrait"))

    with patch.dict("sys.modules", _fake_import_service_modules(service)):
        outputs = runner.import_workflow(source_path, "portrait", tmp_path / "origin")

    service.assert_called_once_with(source_bytes, workflow_id="portrait")
    assert set(outputs) == {"python", "companion", "source", "report"}
    assert outputs["python"].read_bytes() == b"workflow = load('fixture')\n"
    assert outputs["companion"].read_bytes() == b'{"revision_id":"rev-origin"}\n'
    assert outputs["source"].read_bytes() == source_bytes
    report = json.loads(outputs["report"].read_text(encoding="utf-8"))
    assert report["transition_kind"] == "origin"
    assert report["parent_task_id"] is None
    assert report["origin_task_id"] is None
    assert report["members"]["source.json"] == (
        "sha256:" + hashlib.sha256(source_bytes).hexdigest()
    )

    precreated_output = tmp_path / "host-output"
    precreated_output.mkdir()
    service = Mock(return_value=_origin_artifacts(source_bytes, "portrait"))
    with patch.dict("sys.modules", _fake_import_service_modules(service)):
        host_outputs = runner.import_workflow(source_path, "portrait", precreated_output)
    assert all(path.is_file() for path in host_outputs.values())


def test_import_failure_and_invalid_service_receipt_leave_no_outputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = _load_import_runner(monkeypatch)
    source_path = tmp_path / "source.json"
    source_bytes = b'{"nodes":[],"links":[]}\n'
    source_path.write_bytes(source_bytes)
    out = tmp_path / "failed-origin"
    service = Mock(side_effect=ValueError("invalid workflow"))

    with patch.dict("sys.modules", _fake_import_service_modules(service)):
        with pytest.raises(runner.WorkflowImportError, match="rejected source"):
            runner.import_workflow(source_path, "portrait", out)
    assert not out.exists()

    artifacts = _origin_artifacts(source_bytes, "portrait")
    artifacts.report["members"]["source.json"] = "sha256:" + "0" * 64
    service = Mock(return_value=artifacts)
    with patch.dict("sys.modules", _fake_import_service_modules(service)):
        with pytest.raises(runner.WorkflowImportError, match="member digests"):
            runner.import_workflow(source_path, "portrait", out)
    assert not out.exists()


def test_canonical_edit_emits_parent_linked_successor_and_exact_members(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ASTRID_INTERNAL_INVOCATION", "1")
    runner = importlib.import_module("astrid.packs.vibecomfy.actions.edit.run")
    members = {
        "workflow.py": b"workflow = load('parent')\n",
        "workflow.vibe.json": b'{"revision_id":"rev-origin"}\n',
        "source.json": b'{"nodes":[],"links":[]}\r\n',
    }
    input_paths = {name: tmp_path / name for name in members}
    for name, path in input_paths.items():
        path.write_bytes(members[name])
    operations = tmp_path / "operations.json"
    op_list = [
        {"op": "add_node", "class_type": "Preview", "uid": "new_preview"},
        {"op": "upsert_link", "source": "new_preview", "target": "ksampler", "input": "image"},
    ]
    operations.write_text(
        json.dumps({"schema_version": 1, "expected_revision": 0, "ops": op_list}),
        encoding="utf-8",
    )
    after_members = {
        "workflow.py": b"workflow = load('edited')\n",
        "workflow.vibe.json": b'{"revision_id":"rev-edited"}\n',
        "source.json": members["source.json"],
    }
    service_call = {}

    def load_bundle(path):
        is_after = path.read_bytes() == after_members["workflow.py"]
        return SimpleNamespace(
            workflow_identity="portrait",
            revision_id="rev-edited" if is_after else "rev-origin",
            parent_revision="rev-origin" if is_after else None,
            semantic_digest="semantic:edited" if is_after else "semantic:origin",
            ui_digest="ui:edited" if is_after else "ui:origin",
        )

    def transition_bundle(reference, *, output, **kwargs):
        assert reference.name == "workflow.py"
        assert reference.read_bytes() == members["workflow.py"]
        assert reference.with_name("workflow.vibe.json").read_bytes() == members[
            "workflow.vibe.json"
        ]
        assert reference.with_name("source.json").read_bytes() == members["source.json"]
        service_call.update(kwargs)
        output.parent.mkdir(parents=True)
        output.write_bytes(after_members["workflow.py"])
        output.with_name("workflow.vibe.json").write_bytes(
            after_members["workflow.vibe.json"]
        )
        output.with_name("source.json").write_bytes(after_members["source.json"])
        return SimpleNamespace(
            status="saved",
            revision_id="rev-edited",
            to_dict=lambda: {
                "status": "saved",
                "kind": "edit",
                "parent_revision": "rev-origin",
                "revision": "rev-edited",
                "operations": op_list,
                "diff": [{"op": "add_node"}, {"op": "upsert_link"}],
                "diagnostics": [],
            },
        )

    with patch.dict(
        "sys.modules",
        _fake_transition_modules(
            transition_bundle=transition_bundle,
            load_bundle=load_bundle,
            gate_context=True,
        ),
    ):
        outputs = runner.edit_workflow(
            python_path=input_paths["workflow.py"],
            companion_path=input_paths["workflow.vibe.json"],
            source_path=input_paths["source.json"],
            workflow_id="portrait",
            parent_revision="rev-origin",
            parent_task_id="T_origin",
            origin_task_id="T_origin",
            transition_kind="typed_edit",
            operations_path=operations,
            capture_python_path=None,
            capture_graph_path=None,
            out_dir=tmp_path / "edited-output",
            python_execution_consent="confirmed",
        )

    assert service_call["expected_parent_revision"] == "rev-origin"
    assert service_call["tool_calls"] == [
        {"tool": "edit_batch", "args": {"ops": op_list}}
    ]
    assert {name: path.read_bytes() for name, path in input_paths.items()} == members
    assert outputs["python"].read_bytes() == after_members["workflow.py"]
    assert outputs["companion"].read_bytes() == after_members["workflow.vibe.json"]
    assert outputs["source"].read_bytes() == members["source.json"]
    report = json.loads(outputs["report"].read_text(encoding="utf-8"))
    assert report["workflow_identity"] == "portrait"
    assert report["parent_revision"] == "rev-origin"
    assert report["revision_id"] == "rev-edited"
    assert report["parent_task_id"] == "T_origin"
    assert report["origin_task_id"] == "T_origin"
    assert report["requested_operations"] == op_list
    assert report["after"]["members"]["source.json"] == _sha256_for_test(
        members["source.json"]
    )


def _sha256_for_test(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def test_canonical_edit_failure_does_not_publish_partial_members(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ASTRID_INTERNAL_INVOCATION", "1")
    runner = importlib.import_module("astrid.packs.vibecomfy.actions.edit.run")
    members = {
        "workflow.py": b"workflow = load('parent')\n",
        "workflow.vibe.json": b'{"revision_id":"rev-origin"}\n',
        "source.json": b'{"nodes":[],"links":[]}\n',
    }
    paths = {name: tmp_path / name for name in members}
    for name, path in paths.items():
        path.write_bytes(members[name])
    operations = tmp_path / "operations.json"
    operations.write_text(
        json.dumps({"schema_version": 1, "expected_revision": 0, "ops": [{"op": "remove_node", "target": "missing"}]}),
        encoding="utf-8",
    )
    out_dir = tmp_path / "failed-transition"
    def load_bundle(_path):
        return SimpleNamespace(
            workflow_identity="portrait",
            revision_id="rev-origin",
            parent_revision=None,
            semantic_digest="semantic:origin",
            ui_digest="ui:origin",
        )

    def fail_transition(*_args, **_kwargs):
        raise ValueError("invalid later operation")

    with patch.dict(
        "sys.modules",
        _fake_transition_modules(
            transition_bundle=fail_transition,
            load_bundle=load_bundle,
            gate_context=True,
        ),
    ):
        with pytest.raises(runner.WorkflowTransitionError, match="rejected workflow transition"):
            runner.edit_workflow(
                python_path=paths["workflow.py"],
                companion_path=paths["workflow.vibe.json"],
                source_path=paths["source.json"],
                workflow_id="portrait",
                parent_revision="rev-origin",
                parent_task_id="T_origin",
                origin_task_id="T_origin",
                transition_kind="typed_edit",
                operations_path=operations,
                capture_python_path=None,
                capture_graph_path=None,
                out_dir=out_dir,
                python_execution_consent="confirmed",
            )
    assert not out_dir.exists()


def test_canonical_edit_rejects_missing_or_nonexact_python_execution_consent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ASTRID_INTERNAL_INVOCATION", "1")
    runner = importlib.import_module("astrid.packs.vibecomfy.actions.edit.run")
    path = tmp_path / "unused.py"
    with pytest.raises(runner.WorkflowTransitionError, match="python_execution_consent.*confirmed"):
        runner.edit_workflow(
            python_path=path,
            companion_path=path,
            source_path=path,
            workflow_id="portrait",
            parent_revision="rev-origin",
            parent_task_id="T_origin",
            origin_task_id="T_origin",
            transition_kind="typed_edit",
            operations_path=None,
            capture_python_path=None,
            capture_graph_path=None,
            out_dir=tmp_path / "rejected",
            python_execution_consent="yes",
        )
    assert not (tmp_path / "rejected").exists()


def test_manual_python_capture_keeps_parent_and_candidate_separate_with_gate_audit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ASTRID_INTERNAL_INVOCATION", "1")
    runner = importlib.import_module("astrid.packs.vibecomfy.actions.edit.run")
    parent = {
        "workflow.py": b"workflow = load('parent')\n",
        "workflow.vibe.json": b'{"revision_id":"rev-origin"}\n',
        "source.json": b'{"nodes":[],"links":[]}\r\n',
    }
    paths = {name: tmp_path / name for name in parent}
    for name, path in paths.items():
        path.write_bytes(parent[name])
    candidate = tmp_path / "candidate.py"
    candidate.write_bytes(b"workflow = load('manually-edited')\n")
    after = {
        "workflow.py": candidate.read_bytes(),
        "workflow.vibe.json": b'{"revision_id":"rev-captured"}\n',
        "source.json": parent["source.json"],
    }

    def load_bundle(path):
        is_after = path.read_bytes() == after["workflow.py"]
        return SimpleNamespace(
            workflow_identity="portrait",
            revision_id="rev-captured" if is_after else "rev-origin",
            parent_revision="rev-origin" if is_after else None,
            semantic_digest="semantic:captured" if is_after else "semantic:origin",
            ui_digest="ui:captured" if is_after else "ui:origin",
        )

    service_call = {}

    def capture_bundle(reference, *, output, **kwargs):
        service_call.update(kwargs)
        candidate_python = kwargs["candidate_python"]
        assert reference.read_bytes() == parent["workflow.py"]
        assert candidate_python.read_bytes() == candidate.read_bytes()
        assert candidate_python.with_name("workflow.vibe.json").read_bytes() == parent[
            "workflow.vibe.json"
        ]
        assert candidate_python.with_name("source.json").read_bytes() == parent[
            "source.json"
        ]
        from vibecomfy.security import current_gate_context

        context = current_gate_context()
        assert context.non_interactive and context.assume_yes
        context.audit.append(
            {"decision": "allow", "reason": "assume_yes_bypass", "operation": "capture"}
        )
        output.parent.mkdir(parents=True)
        for name, data in after.items():
            target = output if name == "workflow.py" else output.with_name(name)
            target.write_bytes(data)
        return SimpleNamespace(
            status="saved",
            revision_id="rev-captured",
            to_dict=lambda: {
                "status": "saved",
                "kind": "python_capture",
                "parent_revision": "rev-origin",
                "revision": "rev-captured",
                "operations": [],
                "diff": [{"kind": "node_value_changed"}],
                "diagnostics": [],
            },
        )

    with patch.dict(
        "sys.modules",
        _fake_transition_modules(
            transition_bundle=capture_bundle,
            load_bundle=load_bundle,
            gate_context=True,
        ),
    ):
        outputs = runner.edit_workflow(
            python_path=paths["workflow.py"],
            companion_path=paths["workflow.vibe.json"],
            source_path=paths["source.json"],
            workflow_id="portrait",
            parent_revision="rev-origin",
            parent_task_id="T_edit",
            origin_task_id="T_origin",
            transition_kind="manual_capture",
            operations_path=None,
            capture_python_path=candidate,
            capture_graph_path=None,
            out_dir=tmp_path / "capture-output",
            python_execution_consent="confirmed",
        )

    assert service_call["capture"] is True
    assert service_call["expected_parent_revision"] == "rev-origin"
    assert outputs["python"].read_bytes() == candidate.read_bytes()
    assert {name: path.read_bytes() for name, path in paths.items()} == parent
    report = json.loads(outputs["report"].read_text(encoding="utf-8"))
    assert report["transition_kind"] == "manual_capture"
    assert report["python_execution_consent"] == "confirmed"
    assert report["operations"] == []
    assert report["diff"] == [{"kind": "node_value_changed"}]
    assert report["security_gate_audit"][0]["reason"] == "assume_yes_bypass"


@pytest.mark.integration  # needs vibecomfy from a second checkout
def test_inspect_emits_projection_without_mutating_ui_graph(tmp_path: Path) -> None:
    before = FIXTURE.read_bytes()

    outputs = inspect_workflow(FIXTURE, tmp_path / "inspection")

    assert FIXTURE.read_bytes() == before
    assert "ksampler" in outputs["projection"].read_text(encoding="utf-8")
    report = json.loads(outputs["inspection"].read_text(encoding="utf-8"))
    assert report["authority"] == "input_ui_graph"
    assert report["python_execution_consent"] is None
    assert report["security_gate_audit"] == []
    assert report["projection"] == "read_only_python_like_ir"
    assert report["source_sha256"].startswith("sha256:")
    assert report["lenses"]["topology"]


def test_canonical_inspect_projects_bundle_without_mutating_any_member(
    tmp_path: Path,
) -> None:
    members = {
        "workflow.py": b"workflow = load('fixture')\n",
        "workflow.vibe.json": b'{"revision_id":"rev-origin"}\n',
        "source.json": b'{"nodes":[],"links":[]}\r\n',
    }
    paths = {name: tmp_path / name for name in members}
    for name, path in paths.items():
        path.write_bytes(members[name])
    bundle = SimpleNamespace(
        workflow={"nodes": [], "links": []},
        workflow_identity="workflow:portrait",
        revision_id="rev-origin",
        parent_revision=None,
        semantic_digest="semantic:origin",
        ui_digest="ui:origin",
    )

    package = ModuleType("vibecomfy")
    package.__path__ = []  # type: ignore[attr-defined]
    porting = ModuleType("vibecomfy.porting")
    porting.__path__ = []  # type: ignore[attr-defined]
    render_module = ModuleType("vibecomfy.porting.render")
    render_module.render = Mock(
        return_value={
            "census": {"node_count": 0},
            "surface": "workflow = VibeWorkflow()\n",
            "topology": "graph: empty",
            "topology_source": "computed",
        }
    )
    bundle_module = ModuleType("vibecomfy.workflow_bundle")
    security_module = ModuleType("vibecomfy.security")
    active_gate = ContextVar("inspect_fake_gate", default=None)

    class FakeGateContext:
        def __init__(self, *, non_interactive, assume_yes):
            self.non_interactive = non_interactive
            self.assume_yes = assume_yes
            self.audit = []

    security_module.GateContext = FakeGateContext  # type: ignore[attr-defined]
    security_module.set_gate_context = active_gate.set  # type: ignore[attr-defined]

    def load_bundle(path: Path):
        assert path.name == "workflow.py"
        assert path.with_name("workflow.vibe.json").read_bytes() == members[
            "workflow.vibe.json"
        ]
        assert path.with_name("source.json").read_bytes() == members["source.json"]
        return bundle

    bundle_module.load_bundle = load_bundle  # type: ignore[attr-defined]
    with patch.dict(
        "sys.modules",
        {
            "vibecomfy": package,
            "vibecomfy.porting": porting,
            "vibecomfy.porting.render": render_module,
            "vibecomfy.workflow_bundle": bundle_module,
            "vibecomfy.security": security_module,
        },
    ):
        outputs = inspect_canonical_bundle(
            paths["workflow.py"],
            paths["workflow.vibe.json"],
            paths["source.json"],
            tmp_path / "inspection",
            python_execution_consent="confirmed",
        )

    assert outputs["projection"].read_text(encoding="utf-8") == "workflow = VibeWorkflow()\n"
    report = json.loads(outputs["inspection"].read_text(encoding="utf-8"))
    assert report["authority"] == "canonical_workflow_bundle"
    assert report["workflow_identity"] == "workflow:portrait"
    assert report["revision_id"] == "rev-origin"
    assert report["python_execution_consent"] == "confirmed"
    assert report["security_gate_audit"] == []
    assert set(report["members"]) == set(members)
    assert {name: path.read_bytes() for name, path in paths.items()} == members


def test_canonical_inspect_rejects_missing_consent_before_loading_python(
    tmp_path: Path,
) -> None:
    from astrid.packs.vibecomfy.shared.python_execution_consent import (
        PythonExecutionConsentError,
    )

    with pytest.raises(PythonExecutionConsentError, match="python_execution_consent.*confirmed"):
        inspect_canonical_bundle(
            tmp_path / "workflow.py",
            tmp_path / "workflow.vibe.json",
            tmp_path / "source.json",
            tmp_path / "inspection",
            python_execution_consent=None,
        )
    assert not (tmp_path / "inspection").exists()


@pytest.mark.integration  # needs vibecomfy from a second checkout
def test_edit_applies_one_atomic_typed_batch_and_emits_fresh_projection(
    tmp_path: Path,
) -> None:
    operations = tmp_path / "operations.json"
    _write_operations(
        operations,
        [
            {"op": "edit_node", "target": "ksampler", "field": "steps", "value": 25},
            {"op": "set_node_mode", "target": "ksampler", "mode": "bypassed"},
        ],
    )

    outputs = edit_workflow(
        FIXTURE,
        operations,
        tmp_path / "edited",
        schema_provider=_frozen_ksampler_schema_provider(),
    )

    edited = json.loads(outputs["workflow"].read_text(encoding="utf-8"))
    sampler = next(node for node in edited["nodes"] if node["type"] == "KSampler")
    assert sampler["widgets_values"][2] == 25
    assert sampler["mode"] == 4
    assert "steps=25" in outputs["projection"].read_text(encoding="utf-8")
    report = json.loads(outputs["report"].read_text(encoding="utf-8"))
    assert report["authority"] == "input_ui_graph_plus_typed_delta"
    assert report["revision"] == 1
    assert report["requested_tools"] == ["edit_node", "set_node_mode"]
    assert report["source_sha256"] != report["edited_sha256"]
    assert len(report["canonical_delta"]) == 2


@pytest.mark.integration  # needs vibecomfy from a second checkout
def test_edit_rejects_projection_rewrites_before_creating_outputs(tmp_path: Path) -> None:
    operations = tmp_path / "operations.json"
    _write_operations(
        operations,
        [{"op": "exec_python", "source": "ksampler.steps = 25"}],
    )
    out_dir = tmp_path / "edited"

    with pytest.raises(WorkflowIrBridgeError, match="must be one of"):
        edit_workflow(FIXTURE, operations, out_dir)

    assert not out_dir.exists()


@pytest.mark.integration  # needs vibecomfy from a second checkout
def test_edit_rejects_nested_batches_before_creating_outputs(tmp_path: Path) -> None:
    operations = tmp_path / "operations.json"
    _write_operations(
        operations,
        [{"op": "edit_batch", "ops": [{"op": "remove_node", "target": "preview"}]}],
    )
    out_dir = tmp_path / "edited"

    with pytest.raises(WorkflowIrBridgeError, match="must be one of"):
        edit_workflow(FIXTURE, operations, out_dir)

    assert not out_dir.exists()


@pytest.mark.integration  # needs vibecomfy from a second checkout
def test_edit_rejects_a_stale_expected_revision(tmp_path: Path) -> None:
    operations = tmp_path / "operations.json"
    operations.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "expected_revision": 1,
                "ops": [
                    {"op": "edit_node", "target": "ksampler", "field": "steps", "value": 25}
                ],
            }
        ),
        encoding="utf-8",
    )
    out_dir = tmp_path / "edited"

    with pytest.raises(WorkflowIrBridgeError, match="typed edit rejected"):
        edit_workflow(FIXTURE, operations, out_dir)

    assert not out_dir.exists()


@pytest.mark.integration  # needs vibecomfy from a second checkout
def test_edit_batch_is_atomic_when_any_leaf_operation_is_invalid(tmp_path: Path) -> None:
    operations = tmp_path / "operations.json"
    _write_operations(
        operations,
        [
            {"op": "edit_node", "target": "ksampler", "field": "steps", "value": 25},
            {"op": "edit_node", "target": "missing-node", "field": "steps", "value": 26},
        ],
    )
    out_dir = tmp_path / "edited"

    with pytest.raises(WorkflowIrBridgeError, match="typed edit rejected"):
        edit_workflow(
            FIXTURE,
            operations,
            out_dir,
            schema_provider=_frozen_ksampler_schema_provider(),
        )

    assert not out_dir.exists()


_T9_CHILD_PROGRAM = r'''
import json, os, sys
from pathlib import Path
from astrid.packs.vibecomfy import production_engine
from astrid.packs.vibecomfy.actions.run import run

def ordinary(workflow, destination, **kwargs):
    root = Path(destination); root.mkdir(parents=True, exist_ok=True)
    output = root / "ordinary.bin"; output.write_bytes(b"ordinary adapter result")
    Path(os.environ["T9_TEST_PRODUCTION_CALLED"]).write_text(json.dumps(kwargs))
    return production_engine.ProductionRunResult(outputs=(output,))

production_engine.run_workflow_result_path = ordinary
raise SystemExit(run.main(sys.argv[1:]))
'''


def _t9_child_case(tmp_path: Path):
    """Actual executor child + real pinned fixture; fake media subprocess only."""
    from astrid.core.generation.model_root import canonical_model_inventory_digest
    from astrid.packs.h3_av.shared import generation

    source_paths = (
        "tests/fixtures/h3_cpu/approved_character_swap_cpu_boundary.py",
        "astrid/packs/h3_av/shared/generation.py", "astrid/packs/h3_av/actions/compile/compile.py",
        "astrid/packs/vibecomfy/actions/run/run.py",
        "astrid/packs/vibecomfy/pack.yaml",
    )
    source_files = {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in source_paths}
    candidate_path = tmp_path / "candidate.json"
    candidate = {"schema": "astrid-h3-candidate-source-manifest/v1",
                 "synthetic_inference": True, "synthetic_inference_classification": "synthetic-inference-only",
                 "source_files": source_files}
    model_path = tmp_path / "models" / "loras" / generation.CHARACTER_SWAP_FILENAME
    model_path.parent.mkdir(parents=True)
    model_path.write_bytes(b"unit-test synthetic model, never production weights")
    asset = {"name": model_path.name, "subdir": "loras", "size": model_path.stat().st_size,
             "sha256": "sha256:" + hashlib.sha256(model_path.read_bytes()).hexdigest()}
    evidence_root = tmp_path / "evidence"; evidence_root.mkdir()
    fixture = ROOT / source_paths[0]
    profile = {"schema_version": "hc03-worker-readiness.v1", "status": "ready",
               "launch": {"model_root": {"schema_version": 1, "path": str(model_path.parents[1]),
                   "inventory": [asset], "inventory_digest": canonical_model_inventory_digest([asset]),
                   "qualification_identity": None}},
               "t9_model_substitute": {"approved": True, "mode": "deterministic_cpu_model_boundary_v1",
                   "purpose": "h3_character_swap_public_runtime_cpu_acceptance_v1",
                   "candidate_manifest_path": str(candidate_path), "source_path": str(fixture),
                   "source_sha256": "sha256:" + source_files[source_paths[0]], "model_asset": asset,
                   "activation_marker_path": str(evidence_root / "activation.json"),
                   "calls_path": str(evidence_root / "calls.jsonl"),
                   "evidence_path": str(evidence_root / "boundary.json")}}
    profile_path = tmp_path / "profile.json"
    workflow = tmp_path / "workflow.json"; workflow.write_text('{}')
    output = tmp_path / "out"; (output / "engine-input").mkdir(parents=True)
    binaries = tmp_path / "bin"; binaries.mkdir()
    # No FFmpeg render or provider work: exercise the real fixture's process
    # dispatch/output settlement with an explicitly fake tiny-media child.
    ffmpeg = binaries / "ffmpeg"
    ffmpeg.write_text('#!' + sys.executable + '\nimport sys\nfrom pathlib import Path\nPath(sys.argv[-1]).write_bytes(b"offline fake-media bytes")\n')
    ffmpeg.chmod(0o700)
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith(("ASTRID_H3_", "ASTRID_T9_"))}
    environment.update(ASTRID_INTERNAL_INVOCATION="1", PYTHONDONTWRITEBYTECODE="1",
                       ASTRID_H3_CANDIDATE_MANIFEST=str(candidate_path),
                       ASTRID_H3_T9_MODEL_SUBSTITUTE_PROFILE=str(profile_path),
                       PYTHONPATH=str(ROOT), PATH=str(binaries) + os.pathsep + environment.get("PATH", ""),
                       T9_TEST_PRODUCTION_CALLED=str(tmp_path / "ordinary-call.json"))
    return candidate, candidate_path, profile, profile_path, workflow, output, environment


def _invoke_t9_child(case, *, cli_hash=None, candidate_hash=None, attempt="attempt-test"):
    candidate, candidate_path, profile, profile_path, workflow, output, environment = case
    candidate_path.write_text(json.dumps(candidate, sort_keys=True))
    profile["t9_model_substitute"]["candidate_manifest_sha256"] = candidate_hash or hashlib.sha256(candidate_path.read_bytes()).hexdigest()
    profile_path.write_text(json.dumps(profile, sort_keys=True))
    profile_hash = "sha256:" + hashlib.sha256(profile_path.read_bytes()).hexdigest()
    command = [sys.executable, "-B", "-c", _T9_CHILD_PROGRAM, "run", str(workflow), "--out", str(output),
               "--task-identity", "task-test", "--attempt-identity", attempt,
               "--execution-identity", "execution-test", "--readiness-profile-path", str(profile_path),
               "--readiness-profile-hash", cli_hash or profile_hash]
    return subprocess.run(command, env=environment, cwd=workflow.parent, capture_output=True, text=True, timeout=20)


def test_t9_actual_executor_child_loads_pinned_callable_and_settles_output(tmp_path: Path):
    case = _t9_child_case(tmp_path)
    result = _invoke_t9_child(case)
    assert result.returncode == 0, result.stdout + result.stderr
    markers = list((tmp_path / "evidence").glob("activation-*.json"))
    assert len(markers) == 1
    marker = json.loads(markers[0].read_text())
    assert marker["child_pid"] != os.getpid()
    assert (marker["task_id"], marker["attempt_id"], marker["execution_id"]) == ("task-test", "attempt-test", "execution-test")
    token = hashlib.sha256(json.dumps(["task-test", "attempt-test", "execution-test", marker["child_pid"]], separators=(",", ":")).encode()).hexdigest()[:20]
    assert markers[0].name == "activation-" + token + ".json"
    assert marker["profile_id"] == "t9_cpu_stub" and marker["synthetic_inference"] is True
    assert not marker["queued_to_comfy"] and not marker["production_model_bytes_observed"]
    assert Path(marker["evidence_path"]).is_file()
    calls = [json.loads(line) for line in (tmp_path / "evidence" / "calls.jsonl").read_text().splitlines()]
    assert len(calls) == 1 and calls[0]["child_pid"] == marker["child_pid"]
    assert not (tmp_path / "ordinary-call.json").exists()
    assert (case[5] / "manifest.json").is_file()
    assert list((case[5] / "artifacts").iterdir())[0].read_bytes() == b"offline fake-media bytes"


@pytest.mark.parametrize("failure", ["paired-hash", "candidate-hash", "candidate-path", "candidate-classification", "fixture-pin", "executor-pin", "unqualified-profile", "deployment-qualified", "inventory-drift", "missing-profile", "evidence-alias", "missing-attempt"])
def test_t9_actual_child_mismatch_fails_before_activation_or_production(tmp_path: Path, failure: str):
    case = _t9_child_case(tmp_path)
    candidate, _, profile, _, _, _, env = case
    cli_hash = None; candidate_hash = None; attempt = "attempt-test"
    if failure == "paired-hash": cli_hash = "sha256:" + "0" * 64
    elif failure == "candidate-hash": candidate_hash = "0" * 64
    elif failure == "candidate-path": env["ASTRID_H3_CANDIDATE_MANIFEST"] = str(tmp_path / "other-candidate.json")
    elif failure == "candidate-classification": candidate["synthetic_inference"] = False
    elif failure == "fixture-pin": candidate["source_files"]["tests/fixtures/h3_cpu/approved_character_swap_cpu_boundary.py"] = "0" * 64
    elif failure == "executor-pin": candidate["source_files"]["astrid/packs/vibecomfy/actions/run/run.py"] = "0" * 64
    elif failure == "unqualified-profile": profile["t9_model_substitute"]["approved"] = False
    elif failure == "deployment-qualified": profile["launch"]["model_root"]["qualification_identity"] = "invented-deployment"
    elif failure == "inventory-drift": (tmp_path / "models" / "loras" / profile["t9_model_substitute"]["model_asset"]["name"]).write_bytes(b"changed")
    elif failure == "missing-profile": env["ASTRID_H3_T9_MODEL_SUBSTITUTE_PROFILE"] = str(tmp_path / "missing.json")
    elif failure == "evidence-alias": profile["t9_model_substitute"]["activation_marker_path"] = profile["t9_model_substitute"]["calls_path"]
    else: attempt = "-"
    result = _invoke_t9_child(case, cli_hash=cli_hash, candidate_hash=candidate_hash, attempt=attempt)
    assert result.returncode != 0, result.stdout + result.stderr
    assert not list((tmp_path / "evidence").iterdir())
    assert not (tmp_path / "ordinary-call.json").exists()
    assert not (case[5] / "manifest.json").exists()


@pytest.mark.parametrize("session", [False, True])
def test_t9_absent_explicit_activation_preserves_production_dispatch(tmp_path: Path, session: bool):
    case = _t9_child_case(tmp_path)
    case[6].pop("ASTRID_H3_T9_MODEL_SUBSTITUTE_PROFILE")
    if session: case[2]["vibecomfy_session"] = {}
    result = _invoke_t9_child(case)
    assert result.returncode == 0, result.stdout + result.stderr
    call = json.loads((tmp_path / "ordinary-call.json").read_text())
    assert call["profile_id"] == ("checkout_server" if session else "pip_embedded")
    assert not list((tmp_path / "evidence").iterdir())


def test_t9_run_declaration_forwards_only_explicit_profile_and_candidate(monkeypatch: pytest.MonkeyPatch):
    from astrid.core.subprocess_env import build_child_subprocess_env
    import yaml
    declaration = yaml.safe_load((ROOT / "astrid/packs/vibecomfy/pack.yaml").read_text())["actions"]["run"]
    allowed = declaration["isolation"]["env_passthrough"]
    assert allowed == ["ASTRID_H3_CANDIDATE_MANIFEST", "ASTRID_H3_T9_MODEL_SUBSTITUTE_PROFILE"]
    base = {name: "/selected/" + name for name in allowed}
    base["ASTRID_T9_MODEL_SUBSTITUTE_MARKER"] = "/ambient-marker"
    child = build_child_subprocess_env(base=base, parent={}, passthrough=allowed, declared_passthrough=allowed)
    assert all(child[name] == base[name] for name in allowed)
    assert "ASTRID_T9_MODEL_SUBSTITUTE_MARKER" not in child


@pytest.mark.parametrize("failure", [None, "missing", "relative", "direct-alias", "ancestor-alias", "fixture-drift", "profile-drift"])
def test_t9_child_uses_explicit_pinned_fixture_outside_product_root(tmp_path: Path, failure):
    case = _t9_child_case(tmp_path)
    fixture = tmp_path / "selected-fixture" / "approved_character_swap_cpu_boundary.py"
    fixture.parent.mkdir()
    fixture.write_bytes((ROOT / "tests/fixtures/h3_cpu/approved_character_swap_cpu_boundary.py").read_bytes())
    boundary = case[2]["t9_model_substitute"]
    boundary["source_path"] = str(fixture)
    if failure == "missing": boundary["source_path"] = str(tmp_path / "absent.py")
    elif failure == "relative": boundary["source_path"] = fixture.name
    elif failure == "direct-alias":
        alias = tmp_path / "alias.py"; alias.symlink_to(fixture); boundary["source_path"] = str(alias)
    elif failure == "ancestor-alias":
        alias = tmp_path / "alias-directory"; alias.symlink_to(fixture.parent, target_is_directory=True)
        boundary["source_path"] = str(alias / fixture.name)
    elif failure == "fixture-drift": fixture.write_bytes(fixture.read_bytes() + b"\n# drift\n")
    elif failure == "profile-drift": boundary["source_sha256"] = "sha256:" + "0" * 64
    result = _invoke_t9_child(case)
    if failure is None:
        assert result.returncode == 0, result.stdout + result.stderr
        marker = json.loads(next((tmp_path / "evidence").glob("activation-*.json")).read_text())
        assert marker["fixture_path"] == str(fixture)
        assert marker["fixture_sha256"] == boundary["source_sha256"]
        assert Path(marker["evidence_path"]).is_file()
    else:
        assert result.returncode != 0, result.stdout + result.stderr
        assert not list((tmp_path / "evidence").iterdir())
    assert not (tmp_path / "ordinary-call.json").exists()
