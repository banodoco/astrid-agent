"""Exercise the real validator without allowing Comfy lifecycle or network I/O."""

from __future__ import annotations

import hashlib
import importlib
import json
import socket
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

pytestmark = pytest.mark.integration  # needs vibecomfy from a second checkout


@pytest.fixture
def offline_validator(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from vibecomfy.runtime import server
    from vibecomfy.schema import provider

    monkeypatch.setenv("ASTRID_INTERNAL_INVOCATION", "1")
    monkeypatch.chdir(tmp_path)  # No local node_index.json may override auto selection.
    monkeypatch.setattr(provider, "has_comfyui_runtime", lambda: True)
    monkeypatch.setattr(provider.RuntimeSchemaProvider, "_load_valid_cached_object_info", lambda _: None)
    assert isinstance(provider.get_schema_provider("auto"), provider.RuntimeSchemaProvider)

    # Simulate the occupied endpoint at the launch boundary. Nothing is started
    # or contacted, even if a regression takes the old automatic-schema path.
    launch = AsyncMock(side_effect=RuntimeError("Managed Comfy endpoint 127.0.0.1:8188 is already in use"))
    shutdown = AsyncMock(side_effect=AssertionError("validation attempted server shutdown"))
    network = AsyncMock(side_effect=AssertionError("validation requested target schemas"))
    connect = Mock(side_effect=AssertionError("validation attempted a network connection"))
    monkeypatch.setattr(server, "_spawn_comfy_server", launch)
    monkeypatch.setattr(server, "_stop_managed_process", shutdown)
    monkeypatch.setattr(provider.ComfyClient, "object_info", network)
    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect)
    monkeypatch.setattr(socket, "create_connection", connect)
    validator = importlib.import_module("astrid.packs.vibecomfy.actions.validate.run")
    return validator, (launch, shutdown, network, connect)


def _bundle_args(tmp_path: Path, *, empty: bool = False) -> list[str]:
    from vibecomfy.workflow import VibeWorkflow, WorkflowSource
    from vibecomfy.workflow_bundle import emit_bundle

    workflow = VibeWorkflow("structural-regression", WorkflowSource("structural-regression"))
    if not empty:
        workflow.add_node("Integer", uid="integer-node", value=7)
    emit_bundle(workflow, tmp_path / "workflow.py", {"operation": "authored"})
    (tmp_path / "source.json").write_text('{"nodes":[],"links":[]}\n')
    return [
        "validate", "", "--python", str(tmp_path / "workflow.py"),
        "--companion", str(tmp_path / "workflow.vibe.json"),
        "--source", str(tmp_path / "source.json"),
        "--python-execution-consent", "confirmed", "--out", str(tmp_path / "out"),
    ]


def _model_bundle_args(
    tmp_path: Path,
    model_bytes: bytes = b"model-fixture",
    *,
    class_type: str = "CheckpointLoaderSimple",
    declared_model_names: tuple[str, ...] = ("fixture.safetensors",),
) -> tuple[list[str], Path]:
    from vibecomfy.workflow import VibeWorkflow, WorkflowSource
    from vibecomfy.workflow_bundle import emit_bundle

    model_root = tmp_path / "models"
    model_path = model_root / "checkpoints" / "fixture.safetensors"
    model_path.parent.mkdir(parents=True)
    model_path.write_bytes(model_bytes)
    workflow = VibeWorkflow("model-regression", WorkflowSource("model-regression"))
    workflow.add_node(
        class_type,
        uid="checkpoint-node",
        ckpt_name=model_path.name,
    )
    model_entries = [
        {
            "name": name,
            "url": f"https://example.test/{name}",
            "subdir": "checkpoints",
        }
        for name in declared_model_names
    ]
    workflow.requirements.models.extend(model_entries)
    workflow.metadata["model_assets"] = model_entries
    emit_bundle(workflow, tmp_path / "workflow.py", {"operation": "authored"})
    (tmp_path / "source.json").write_text('{"nodes":[],"links":[]}\n')
    return [
        "validate", "", "--python", str(tmp_path / "workflow.py"),
        "--companion", str(tmp_path / "workflow.vibe.json"),
        "--source", str(tmp_path / "source.json"),
        "--python-execution-consent", "confirmed", "--out", str(tmp_path / "out"),
        "--workflow-inputs", "{}",
    ], model_root


def _set_attested_model_root(
    monkeypatch: pytest.MonkeyPatch,
    root: Path,
    *,
    model_name: str | None = None,
    model_bytes: bytes = b"model-fixture",
) -> None:
    from astrid.core.generation.model_root import (
        MODEL_ROOT_BINDING_ENV,
        MODEL_ROOT_ENV,
        canonical_model_inventory_digest,
    )

    root.mkdir(parents=True, exist_ok=True)
    inventory = []
    if model_name is not None:
        model_path = root / "checkpoints" / model_name
        model_path.parent.mkdir(parents=True, exist_ok=True)
        model_path.write_bytes(model_bytes)
        inventory.append({
            "name": model_name,
            "sha256": "sha256:" + hashlib.sha256(model_bytes).hexdigest(),
            "size": len(model_bytes),
            "subdir": "checkpoints",
        })
    payload = {
        "schema_version": 1,
        "path": str(root),
        "inventory": inventory,
        "inventory_digest": canonical_model_inventory_digest(inventory),
    }
    monkeypatch.setenv(MODEL_ROOT_ENV, str(root))
    monkeypatch.setenv(MODEL_ROOT_BINDING_ENV, json.dumps(payload, sort_keys=True))


def test_real_canonical_validation_is_structural_with_comfy_installed_and_port_occupied(
    tmp_path: Path, offline_validator,
) -> None:
    validator, forbidden = offline_validator
    args = _bundle_args(tmp_path)
    assert validator.main(args) == 0
    report = json.loads((tmp_path / "out" / "validation-report.json").read_text())
    assert report["ok"] is True
    assert report["authority"] == "canonical_workflow_bundle"
    assert report["validation_mode"] == "canonical_bundle_structural"
    assert report["runtime_validation"] == {
        "status": "deferred", "executor": "vibecomfy.run",
        "checks": ["session_identity", "target_schema"],
    }
    assert report["python_execution_consent"] == "confirmed"
    assert any(entry["decision"] == "allow" for entry in report["security_gate_audit"])
    for operation in forbidden:
        operation.assert_not_called()


def test_real_canonical_validation_projects_concrete_inputs_without_target_models(
    tmp_path: Path, offline_validator, monkeypatch: pytest.MonkeyPatch,
) -> None:
    validator, forbidden = offline_validator
    monkeypatch.delenv("ASTRID_VIBECOMFY_MODELS_ROOT", raising=False)
    monkeypatch.delenv("ASTRID_VIBECOMFY_MODEL_ROOT_BINDING", raising=False)

    args = _bundle_args(tmp_path) + ["--workflow-inputs", "{}"]
    assert validator.main(args) == 0
    report = json.loads((tmp_path / "out" / "validation-report.json").read_text())
    assert report["concrete_workflow_inputs_projected"] is True
    for operation in forbidden:
        operation.assert_not_called()


def test_real_canonical_validation_uses_attested_model_root_and_model_aware_compile(
    tmp_path: Path, offline_validator, monkeypatch: pytest.MonkeyPatch,
) -> None:
    validator, forbidden = offline_validator
    args, model_root = _model_bundle_args(tmp_path)
    _set_attested_model_root(
        monkeypatch,
        model_root,
        model_name="fixture.safetensors",
    )

    assert validator.main(args) == 0
    report = json.loads((tmp_path / "out" / "validation-report.json").read_text())
    assert report["concrete_workflow_inputs_projected"] is True
    for operation in forbidden:
        operation.assert_not_called()


@pytest.mark.parametrize(
    "extracted_references",
    [
        [],
        [{
            "node_id": "checkpoint-node",
            "class_type": "CheckpointLoaderSimple",
            "field": "ckpt_name",
            "value": "fixture.safetensors",
            "subdir": "checkpoints",
        }],
    ],
    ids=["empty", "incomplete"],
)
def test_model_fallback_checks_declared_assets_when_reference_extraction_is_incomplete(
    tmp_path: Path,
    offline_validator,
    monkeypatch: pytest.MonkeyPatch,
    extracted_references: list[dict[str, str]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    validator, forbidden = offline_validator
    args, model_root = _model_bundle_args(
        tmp_path,
        declared_model_names=("fixture.safetensors", "declared-missing.safetensors"),
    )
    _set_attested_model_root(monkeypatch, model_root, model_name="fixture.safetensors")

    model_assets = importlib.import_module("vibecomfy.model_assets")
    monkeypatch.setattr(model_assets, "_referenced_model_values", lambda _workflow: extracted_references)
    from vibecomfy.workflow_bundle import WorkflowBundle, WorkflowBundleError

    def deferred_compile(*_args, **_kwargs):
        raise WorkflowBundleError("object-info identity is deferred to the target schema")

    monkeypatch.setattr(
        WorkflowBundle,
        "compile",
        deferred_compile,
    )

    assert validator.main(args) == 1
    assert "declared-missing.safetensors" in capsys.readouterr().err
    for operation in forbidden:
        operation.assert_not_called()


@pytest.mark.parametrize("negative", ["wrong_root", "missing_model"])
def test_real_canonical_model_validation_rejects_wrong_root_and_missing_model(
    tmp_path: Path,
    offline_validator,
    monkeypatch: pytest.MonkeyPatch,
    negative: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    validator, forbidden = offline_validator
    args, model_root = _model_bundle_args(tmp_path)
    if negative == "wrong_root":
        wrong_root = tmp_path / "wrong-models"
        _set_attested_model_root(monkeypatch, wrong_root)
    else:
        _set_attested_model_root(monkeypatch, tmp_path / "missing-models")

    assert validator.main(args) == 1
    assert "not present locally" in capsys.readouterr().err
    for operation in forbidden:
        operation.assert_not_called()


def test_occupied_endpoint_fixture_exposes_old_default_schema_path(
    tmp_path: Path, offline_validator, monkeypatch: pytest.MonkeyPatch, capsys,
) -> None:
    """Negative control: removing the fix reaches the trapped server launch."""
    from vibecomfy import cli

    validator, (launch, shutdown, network, connect) = offline_validator
    args = _bundle_args(tmp_path)
    real_main = cli.main
    monkeypatch.setattr(cli, "main", lambda argv: real_main([arg for arg in argv if arg != "--no-schema"]))
    assert validator.main(args) == 1
    assert "already in use" in capsys.readouterr().err
    launch.assert_called_once()
    for operation in (shutdown, network, connect):
        operation.assert_not_called()


@pytest.mark.parametrize("defect", ["empty_graph", "bad_companion", "missing_member"])
def test_real_structural_validation_rejects_malformed_inputs(
    tmp_path: Path, offline_validator, defect: str, capsys,
) -> None:
    validator, forbidden = offline_validator
    args = _bundle_args(tmp_path, empty=defect == "empty_graph")
    if defect == "bad_companion":
        (tmp_path / "workflow.vibe.json").write_text("{invalid-json")
    elif defect == "missing_member":
        args[args.index("--source") + 1] = ""
    assert validator.main(args) == 1
    error = capsys.readouterr().err
    assert "vibecomfy.validate:" in error
    if defect == "empty_graph":
        assert "empty_workflow" in error
    assert not (tmp_path / "out" / "validation-report.json").exists()
    for operation in forbidden:
        operation.assert_not_called()


@pytest.mark.parametrize("consent", [None, "true"])
def test_real_canonical_validation_requires_exact_consent_before_python_load(
    tmp_path: Path, offline_validator, consent: str | None,
    monkeypatch: pytest.MonkeyPatch, capsys,
) -> None:
    from vibecomfy import cli

    validator, forbidden = offline_validator
    args = _bundle_args(tmp_path)
    index = args.index("--python-execution-consent")
    if consent is None:
        del args[index:index + 2]
    else:
        args[index + 1] = consent
    load = Mock(side_effect=AssertionError("CLI reached without Python consent"))
    monkeypatch.setattr(cli, "main", load)
    assert validator.main(args) == 1
    assert "python_execution_consent" in capsys.readouterr().err
    load.assert_not_called()
    for operation in forbidden:
        operation.assert_not_called()
