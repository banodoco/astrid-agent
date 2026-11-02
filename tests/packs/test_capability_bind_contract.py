"""CPU-only regression tests for capability input-to-command binding."""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

import pytest

from astrid.core.contracts.binding import (
    BindingError,
    assert_provided_inputs_bound,
    expand_command,
)
from astrid.core.contracts.schema import CommandSpec, Port
from astrid.core.execution.executor.registry import ExecutorRegistry, load_default_registry as load_executor_registry
from astrid.core.execution.executor.runner import ExecutorRunRequest, build_executor_command
from astrid.core.execution.generic_host import GenericPackHost

REPO_ROOT = Path(__file__).resolve().parents[2]
GENERIC_HOST = REPO_ROOT / "astrid/core/execution/generic_host.py"


def _assert_flag_value(argv: tuple[str, ...], flag: str, value: str) -> None:
    index = argv.index(flag)
    assert argv[index + 1] == value


def test_json_input_args_are_encoded_as_json() -> None:
    """Host-owned JSON handoffs must survive the subprocess boundary."""
    command = CommandSpec(
        argv=("python", "-c", "pass"),
        input_args=({"input": "materialized_objects", "flag": "--objects", "optional": True},),
    )
    ports = (Port("materialized_objects", type="json", required=False),)
    result = expand_command(
        command,
        ports,
        {"materialized_objects": {"sha256:abc": "/tmp/managed-object"}},
        {},
    )
    _assert_flag_value(result.argv, "--objects", '{"sha256:abc":"/tmp/managed-object"}')


def test_provided_declared_input_cannot_be_silently_dropped() -> None:
    command = CommandSpec(argv=("python", "-c", "pass"))
    ports = (Port(name="frames", type="integer", required=False),)
    values = {"frames": 9}
    metadata = {"auto_forward_inputs": False}

    result = expand_command(command, ports, values, metadata)
    with pytest.raises(BindingError, match="frames"):
        assert_provided_inputs_bound(result, ports, values, metadata)


def test_cwd_and_env_placeholders_are_binding_evidence() -> None:
    command = CommandSpec(
        argv=("python", "-m", "fixture"),
        cwd="{workspace}",
        env={"FIXTURE_TOKEN": "prefix-{token}"},
    )
    ports = (
        Port("workspace", type="directory", required=False),
        Port("token", type="string", required=False),
    )
    values = {"workspace": "/tmp/work", "token": "abc"}

    result = expand_command(command, ports, values, {})
    assert_provided_inputs_bound(result, ports, values, {})

    assert result.cwd == "/tmp/work"
    assert result.env == {"FIXTURE_TOKEN": "prefix-abc"}


def test_manifest_fixed_inputs_reject_profile_escape_before_expansion() -> None:
    command = CommandSpec(
        argv=("run", "--model", "{model}", "--mode", "{mode}", "--execution", "{execution}")
    )
    ports = (
        Port("model", type="string", required=True),
        Port("mode", type="string", required=True),
        Port("execution", type="string", required=True),
    )
    metadata = {
        "fixed_inputs": {"model": "z-image", "mode": "i2i", "execution": "cloud"}
    }
    values = {"model": "z-image", "mode": "i2i", "execution": "cloud"}
    result = expand_command(command, ports, values, metadata)
    assert result.argv == (
        "run", "--model", "z-image", "--mode", "i2i", "--execution", "cloud"
    )
    for field, value in (("model", "qwen-image-edit"), ("mode", "edit"), ("execution", "local")):
        conflicting = {**values, field: value}
        with pytest.raises(BindingError, match=f"fixed input {field!r}"):
            expand_command(command, ports, conflicting, metadata)
    with pytest.raises(BindingError, match="must be a non-empty object"):
        expand_command(command, ports, values, {"fixed_inputs": {}})


def test_generic_host_run_command_definition_calls_shared_expander() -> None:
    module = ast.parse(GENERIC_HOST.read_text(encoding="utf-8"))
    methods = [
        node
        for node in ast.walk(module)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "_run_command_definition"
    ]
    assert len(methods) == 1

    called_names = {
        call.func.id
        for call in ast.walk(methods[0])
        if isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
    }
    assert "expand_command" in called_names


def test_generic_host_and_runner_share_auto_forward_transport(tmp_path: Path) -> None:
    executor_root = tmp_path / "fixture"
    executor_root.mkdir()
    script = (
        "import json,sys; from pathlib import Path; "
        "Path(r'{out}/argv.json').write_text(json.dumps(sys.argv[1:]))"
    )
    manifest = {
        "schema_version": 1,
        "id": "fixture.capture_argv",
        "name": "Capture argv",
        "kind": "external",
        "version": "1.0",
        "command": {"argv": ["{python_exec}", "-c", script]},
        "inputs": [{"name": "frames", "type": "integer", "required": False}],
        "outputs": [],
    }
    (executor_root / "executor.yaml").write_text(json.dumps(manifest), encoding="utf-8")
    host = GenericPackHost(pack_roots=[tmp_path])
    host.discover()
    record = host.capabilities["fixture.capture_argv"]
    attempt = tmp_path / "attempt"
    output_root = attempt / "outputs"
    output_root.mkdir(parents=True)
    values = {
        "frames": 9,
        "out": str(output_root),
        "run_root": str(attempt),
        "python_exec": sys.executable,
    }

    shared = expand_command(
        record.definition.command,
        record.definition.inputs,
        values,
        record.definition.metadata,
    )
    runner_argv = build_executor_command(
        ExecutorRunRequest(
            executor_id=record.id,
            out=output_root,
            run_root=attempt,
            python_exec=sys.executable,
            inputs={"frames": 9},
        ),
        ExecutorRegistry((record.definition,)),
    )
    assert runner_argv == shared.argv

    host._run_command_definition(record, {"frames": 9}, output_root, attempt)
    assert json.loads((output_root / "argv.json").read_text(encoding="utf-8")) == [
        "--frames",
        "9",
    ]
