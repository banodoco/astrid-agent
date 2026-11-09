from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

import jsonschema
import pytest

from astrid.core.execution.orchestrator.registry import load_default_registry
from astrid.core.execution.orchestrator.runner import OrchestratorRunRequest, run_orchestrator
from astrid.core.foundation.paths import REPO_ROOT
from astrid.core.pack.manifest import load_manifest_mapping
from astrid.core.pack.validate import KNOWN_SCHEMA_VERSIONS, PackValidator

PACKS_ROOT = REPO_ROOT / "astrid" / "packs"
PACK_ROOTS = tuple(sorted(path for path in PACKS_ROOT.iterdir() if (path / "pack.yaml").is_file()))

# Built-in orchestrators live as pack.yaml (schema_version 3) actions marked
# ``metadata.local_orchestrator: true``; there are no standalone orchestrator.yaml
# files. Map orchestrator id -> (pack.yaml path, raw action declaration).
_V3_PACK_MANIFEST_SCHEMA = KNOWN_SCHEMA_VERSIONS[3]["pack"]
_V1_ORCHESTRATOR_SCHEMA = KNOWN_SCHEMA_VERSIONS[1]["orchestrator"]
_V1_ORCHESTRATOR_PAYLOAD: dict[str, Any] = {
    "schema_version": 1,
    "id": "example_pack.example_orchestrator",
    "name": "Example",
    "kind": "built_in",
    "version": "1.0",
    "runtime": {
        "kind": "command",
        "command": {
            "argv": [
                "{python_exec}",
                "-m",
                "example_pack.orchestrators.example.run",
                "{orchestrator_args}",
            ],
        },
    },
}


def _builtin_local_orchestrators() -> dict[str, tuple[Path, dict[str, Any]]]:
    found: dict[str, tuple[Path, dict[str, Any]]] = {}
    for pack_root in PACK_ROOTS:
        pack_manifest = pack_root / "pack.yaml"
        payload = load_manifest_mapping(pack_manifest, manifest_kind="pack")
        for local_id, action in (payload.get("actions") or {}).items():
            if (action.get("metadata") or {}).get("local_orchestrator") is True:
                found[f"{payload['id']}.{local_id}"] = (pack_manifest, action)
    return found


BUILTIN_LOCAL_ORCHESTRATORS = _builtin_local_orchestrators()
BUILTIN_LOCAL_ORCHESTRATOR_PACK_MANIFESTS = tuple(
    sorted({pack_manifest for pack_manifest, _action in BUILTIN_LOCAL_ORCHESTRATORS.values()})
)


def _v1_orchestrator_schema_errors(payload: dict[str, Any]) -> list[str]:
    schema, registry = PackValidator(REPO_ROOT)._load_schema(_V1_ORCHESTRATOR_SCHEMA, "orchestrator", 1)
    validator = jsonschema.Draft7Validator(schema, registry=registry)
    return sorted(error.message for error in validator.iter_errors(payload))


def _v3_pack_schema_errors(payload: dict[str, Any]) -> list[str]:
    schema, registry = PackValidator(REPO_ROOT)._load_schema(_V3_PACK_MANIFEST_SCHEMA, "pack", 3)
    validator = jsonschema.validators.validator_for(schema)(schema, registry=registry)
    return sorted(error.message for error in validator.iter_errors(payload))


class _FakeOwnedProcess:
    """Stands in for the Popen-backed owned process a local orchestrator launches through."""

    returncode = 0

    def wait(self) -> int:
        return 0


def _capture_owned_launch(monkeypatch, captured: dict[str, Any]) -> None:
    import astrid.core.execution.process_group as process_group

    def fake_popen_owned_process(argv, *, cwd=None, env=None, **_kwargs):
        captured["argv"] = tuple(argv)
        captured["env"] = dict(env or {})
        return _FakeOwnedProcess()

    monkeypatch.setattr(process_group, "popen_owned_process", fake_popen_owned_process)
    monkeypatch.setattr(process_group, "verify_tree_absent", lambda process, **_kwargs: None)


def test_all_builtin_orchestrator_manifests_validate_against_json_schema() -> None:
    assert BUILTIN_LOCAL_ORCHESTRATOR_PACK_MANIFESTS

    failures: dict[str, list[str]] = {}
    for pack_manifest in BUILTIN_LOCAL_ORCHESTRATOR_PACK_MANIFESTS:
        payload = load_manifest_mapping(pack_manifest, manifest_kind="pack")
        errors = _v3_pack_schema_errors(payload)
        if errors:
            failures[str(pack_manifest.relative_to(REPO_ROOT))] = errors

    assert failures == {}


def test_pack_validator_loads_every_builtin_orchestrator_manifest() -> None:
    assert BUILTIN_LOCAL_ORCHESTRATOR_PACK_MANIFESTS

    failures: dict[str, list[str]] = {}
    for pack_manifest in BUILTIN_LOCAL_ORCHESTRATOR_PACK_MANIFESTS:
        errors = PackValidator(pack_manifest.parent).validate()
        if errors:
            failures[str(pack_manifest.relative_to(REPO_ROOT))] = errors

    assert failures == {}


def test_all_builtin_orchestrator_manifests_parse_through_runtime_and_registry() -> None:
    assert BUILTIN_LOCAL_ORCHESTRATORS

    registry = load_default_registry()
    failures: dict[str, str] = {}

    for orchestrator_id, (pack_manifest, action) in BUILTIN_LOCAL_ORCHESTRATORS.items():
        try:
            registered = registry.get(orchestrator_id)
        except KeyError as exc:
            failures[orchestrator_id] = str(exc)
            continue

        declared_argv = tuple(action["invocation"]["command"]["argv"])
        if registered.runtime.kind != "command" or registered.runtime.command.argv != declared_argv:
            failures[orchestrator_id] = (
                f"registry runtime {registered.runtime!r} does not match "
                f"{pack_manifest.relative_to(REPO_ROOT)} invocation argv {declared_argv!r}"
            )

    assert failures == {}


def test_h3_runs_through_vibecomfy_run_which_declares_the_import_environment() -> None:
    """h3 has no actions of its own (it is the ``h3`` entry of the one generate path): the graph runs in
    vibecomfy.run, which declares the headless flag and passes through the H3 candidate variables."""
    payload = load_manifest_mapping(PACKS_ROOT / "vibecomfy" / "pack.yaml", manifest_kind="pack")
    run = payload["actions"]["run"]

    assert "actions" not in load_manifest_mapping(PACKS_ROOT / "h3_av" / "pack.yaml", manifest_kind="pack")
    assert run["invocation"]["command"]["env"]["VIBECOMFY_HEADLESS"] == "1"
    assert {"ASTRID_H3_CANDIDATE_MANIFEST", "ASTRID_H3_T9_MODEL_SUBSTITUTE_PROFILE"} <= set(
        run["isolation"]["env_passthrough"])


def test_h3_sdk_orchestrator_subprocess_receives_selected_runpod_account_ref(
    tmp_path, monkeypatch,
) -> None:
    import json

    request_path = tmp_path / "request.json"
    request_path.write_text("{}\n", encoding="utf-8")
    asset_map_path = tmp_path / "asset-map.json"
    asset_map_path.write_text("{}\n", encoding="utf-8")
    execution_request_path = tmp_path / "execution-request.json"
    execution_request_path.write_text(
        json.dumps({
            "target": {
                "kind": "runpod",
                "pod_id": "pod-fixture",
                "provider_account_ref": "runpod-default",
                "storage": {"network_volume_id": "volume-fixture"},
            },
            "lifecycle": {"mode": "leave_running"},
        }),
        encoding="utf-8",
    )
    selection_path = tmp_path / "prepared-worker-selection.json"
    selection_path.write_text("{}\n", encoding="utf-8")
    registry = load_default_registry()
    monkeypatch.setenv("ASTRID_RUNPOD_ACCOUNT_REF", "runpod-default")
    captured: dict[str, Any] = {}
    _capture_owned_launch(monkeypatch, captured)
    result = run_orchestrator(
        OrchestratorRunRequest(
            orchestrator_id="h3_av.transform",
            out=tmp_path / "out",
            project="prepared-worker-env-test",
            project_was_auto_resolved=True,
                        inputs={
                "request": request_path,
                "asset_map": asset_map_path,
                "execution_request": execution_request_path,
                "prepared_worker": selection_path,
                "require_worker_qualification": True,
            },
        ),
        registry,
    )

    assert result.returncode == 0
    assert captured["env"]["ASTRID_RUNPOD_ACCOUNT_REF"] == "runpod-default"
    assert "--prepared-worker" in captured["argv"]
    assert str(selection_path) in captured["argv"]
    assert "RUNPOD_API_KEY" not in captured["env"]


def test_orchestrator_schema_accepts_legacy_python_cli_runtime_shape() -> None:
    legacy_payload = deepcopy(_V1_ORCHESTRATOR_PAYLOAD)
    legacy_payload["runtime"] = {
        "type": "python-cli",
        "entrypoint": "run.py",
        "callable": "main",
    }

    errors = _v1_orchestrator_schema_errors(legacy_payload)

    assert errors == []


def test_orchestrator_schema_rejects_missing_schema_version() -> None:
    payload = deepcopy(_V1_ORCHESTRATOR_PAYLOAD)
    payload.pop("schema_version")

    errors = _v1_orchestrator_schema_errors(payload)

    assert "'schema_version' is a required property" in errors


@pytest.mark.parametrize("supplied", [False, True])
def test_h3_optional_request_matches_sdk_preview_and_command_execution(
    tmp_path, monkeypatch, supplied,
) -> None:
    from types import SimpleNamespace
    import sys

    from astrid.sdk.invocation import _manifest_preview_command

    registry = load_default_registry()
    definition = registry.get("h3_av.transform")
    _pack_manifest, action = BUILTIN_LOCAL_ORCHESTRATORS["h3_av.transform"]
    # The SDK preview expands the same command declaration the runner executes.
    preview_definition = {
        "runtime": {"command": action["invocation"]["command"]},
        "metadata": action["metadata"],
    }
    inputs = {}
    for name in ("request", "asset_map"):
        path = tmp_path / (name + ".json")
        path.write_text("{}", encoding="utf-8")
        inputs[name] = str(path)
    selected = tmp_path / "placement.json"
    selected.write_text('{"target":{"kind":"default"}}', encoding="utf-8")
    if supplied:
        inputs["execution_request"] = str(selected)
    # Local command orchestrators forward only declared input_args; passthrough
    # orchestrator_args are not spliced into their argv, so the dry-run flag is a
    # declared boolean input.
    inputs["dry_run"] = True
    out = tmp_path / "out"
    captured: dict[str, Any] = {}
    _capture_owned_launch(monkeypatch, captured)

    result = run_orchestrator(OrchestratorRunRequest(
        orchestrator_id="h3_av.transform", out=out, inputs=inputs,
        project="optional-command-source-proof", project_was_auto_resolved=True,
        python_exec=sys.executable,
    ), registry)
    preview = _manifest_preview_command(
        SimpleNamespace(definition=preview_definition, inputs=definition.inputs, capability_type="orchestrator"),
        inputs=inputs, outputs=None, brief=None, python_exec=sys.executable,
        out=out,
    )
    assert result.returncode == 0
    assert tuple(preview) == captured["argv"]
    assert ("--execution-request" in preview) is supplied
    if supplied:
        assert preview[preview.index("--execution-request") + 1] == str(selected)
        assert selected.read_text() == '{"target":{"kind":"default"}}'
    assert "--request" in preview and "--asset-map" in preview and "--dry-run" in preview


@pytest.mark.parametrize("argv,ports", [
    (["tool", "--required", "{required}"], [{"name": "required", "required": True}]),
    (["tool", "--unknown", "{unknown}"], [{"name": "optional", "required": False}]),
    (["tool", "{optional}"], [{"name": "optional", "required": False}]),
    (["tool", "--optional={optional}"], [{"name": "optional", "required": False}]),
])
def test_optional_omission_does_not_hide_missing_required_or_unmapped_values(argv, ports):
    from astrid.core.contracts.binding import BindingError, expand_command

    with pytest.raises(BindingError, match="missing value for placeholder"):
        expand_command({"argv": argv}, ports, {}, {})


def test_optional_omission_keeps_defaults_aliases_and_false_values():
    from astrid.core.contracts.binding import expand_command

    ports = [{"name": "optional", "placeholder": "selected", "required": False, "default": "exact"}]
    assert expand_command({"argv": ["tool", "--selected", "{selected}"]}, ports, {}, {}).argv == (
        "tool", "--selected", "exact",
    )
    assert expand_command({"argv": ["tool", "--selected", "{selected}"]}, ports, {"optional": False}, {}).argv == (
        "tool", "--selected", "False",
    )
    no_default = [{"name": "optional", "placeholder": "selected", "required": False}]
    assert expand_command({"argv": ["tool", "--selected", "{selected}"]}, no_default, {}, {}).argv == ("tool",)


def test_h3_unbound_command_still_requires_its_request_document(tmp_path):
    from astrid.core.execution.orchestrator.runner import (
        OrchestratorRunnerError, build_orchestrator_command,
    )

    with pytest.raises(OrchestratorRunnerError, match="request"):
        build_orchestrator_command(OrchestratorRunRequest(
            orchestrator_id="h3_av.transform", out=tmp_path / "out",
            inputs={"asset_map": str(tmp_path / "assets.json")},
        ), load_default_registry())
