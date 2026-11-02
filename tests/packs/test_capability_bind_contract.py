"""CPU-only regression tests for capability input-to-command binding."""

from __future__ import annotations

import ast
import inspect
import json
import sys
from pathlib import Path
from typing import Any

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
from astrid.packs.wan2gp.actions.generate_video_session import run as session_run
from astrid.packs.wan2gp.src.compiler import compile_from_inputs

REPO_ROOT = Path(__file__).resolve().parents[2]
WAN_GENERATE_VIDEO_ID = "wan2gp.generate_video"
WAN_SESSION_RUNTIME_MODULE = "astrid.packs.wan2gp.actions.generate_video_session.run"
FOLEY_TILE_VIDEO_ID = "foley.tile_video"
GENERIC_HOST = REPO_ROOT / "astrid/core/execution/generic_host.py"

WAN_SESSION_PUBLIC_INPUTS: dict[str, Any] = {
    "prompt": "p",
    "model": "wan-2.2",
    "negative_prompt": "blurry",
    "resolution": "512x512",
    "frames": 9,
    "fps": "8",
    "seed": 7,
    "guidance_scale": 5.5,
    "steps": 4,
    "loras": "[]",
}


class _SubmitCaptured(Exception):
    """Raised by the capturing session to stop the action right after submit."""


class _CapturingSession:
    """Session double that records the submitted settings and never returns evidence."""

    def __init__(self) -> None:
        self.submitted: dict[str, Any] | None = None
        self.timeout_seconds: float | None = None
        self.cancel_calls: list[str] = []

    def submit(self, settings: dict[str, Any], timeout_seconds: float) -> Any:
        self.submitted = dict(settings)
        self.timeout_seconds = timeout_seconds
        raise _SubmitCaptured

    def cancel(self, reason: str) -> None:
        self.cancel_calls.append(reason)


def _assert_flag_value(argv: tuple[str, ...], flag: str, value: str) -> None:
    index = argv.index(flag)
    assert argv[index + 1] == value


def test_wan_generation_controls_are_transported_by_shared_expander() -> None:
    definition = load_executor_registry().get(WAN_GENERATE_VIDEO_ID)
    values = {
        "prompt": "p",
        "model": "wan-2.2",
        "frames": 9,
        "fps": 8,
        "seed": 7,
        "python_exec": "python",
    }

    result = expand_command(
        definition.command,
        definition.inputs,
        values,
        definition.metadata,
    )
    assert_provided_inputs_bound(
        result,
        definition.inputs,
        values,
        definition.metadata,
    )

    _assert_flag_value(result.argv, "--prompt", "p")
    _assert_flag_value(result.argv, "--model", "wan-2.2")
    _assert_flag_value(result.argv, "--frames", "9")
    _assert_flag_value(result.argv, "--fps", "8")
    _assert_flag_value(result.argv, "--seed", "7")
    # The v3 action declares no ``out`` input arg: output placement is owned
    # by the host (``{out}/manifest.json`` result manifest), not the argv.
    assert "--out" not in result.argv


def test_command_kind_input_args_are_transported_to_argv_flags(tmp_path: Path) -> None:
    """Command-kind property on a non-wan fixture: each declared input_arg reaches its flag."""
    definition = load_executor_registry().get(FOLEY_TILE_VIDEO_ID)
    assert definition.command is not None
    input_args = tuple(definition.command.input_args)
    assert {mapping.input for mapping in input_args} == {
        "grid", "overlap", "trim", "force", "dry_run",
    }
    values = {
        "video": str(tmp_path / "clip.mp4"),
        "out": str(tmp_path / "out"),
        "python_exec": "python",
        "grid": "3x2",
        "overlap": 0.5,
        "trim": 4.0,
        "force": True,
        "dry_run": False,
    }

    result = expand_command(
        definition.command,
        definition.inputs,
        values,
        definition.metadata,
    )
    assert_provided_inputs_bound(
        result,
        definition.inputs,
        values,
        definition.metadata,
    )

    _assert_flag_value(result.argv, "--grid", "3x2")
    _assert_flag_value(result.argv, "--overlap", "0.5")
    _assert_flag_value(result.argv, "--trim", "4.0")
    # Boolean input_args are flag-only: truthy emits the flag, falsy emits nothing.
    assert "--force" in result.argv
    assert "--dry-run" not in result.argv
    # Declared input_args are transported by the mapping, not auto-forwarded.
    assert {"grid", "overlap", "trim", "force"} <= set(result.bound_inputs)
    assert "grid" not in result.auto_forwarded_inputs


def test_wan_session_action_transports_public_inputs_to_submit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Python-kind property: each declared public input reaches the session submit under its compiled key."""
    definition = load_executor_registry().get(WAN_GENERATE_VIDEO_ID)
    assert definition.metadata["runtime_module"] == WAN_SESSION_RUNTIME_MODULE
    assert definition.metadata.get("host_session") == "wan2gp"
    declared = [port.name for port in definition.inputs]
    assert declared == list(WAN_SESSION_PUBLIC_INPUTS)

    parameters = inspect.signature(session_run.generate_video_session).parameters
    for name in declared:
        assert name in parameters, f"declared input {name!r} is not a session function parameter"

    seen_by_compiler: dict[str, Any] = {}
    real_compile = session_run.compile_host_settings

    def recording_compile(inputs: dict[str, Any]) -> dict[str, Any]:
        seen_by_compiler.update(inputs)
        return real_compile(inputs)

    monkeypatch.setattr(session_run, "compile_host_settings", recording_compile)
    session = _CapturingSession()

    with pytest.raises(_SubmitCaptured):
        session_run.generate_video_session(
            **WAN_SESSION_PUBLIC_INPUTS,
            output_root=tmp_path / "out",
            timeout_seconds=30.0,
            session=session,
        )

    # Public names are preserved up to the compiler: frames stays frames.
    assert seen_by_compiler == WAN_SESSION_PUBLIC_INPUTS
    assert "video_length" not in seen_by_compiler

    # The submit carries each value under its native key.
    assert session.submitted is not None
    assert session.submitted["prompt"] == "p"
    assert session.submitted["model"] == "wan-2.2"
    assert session.submitted["negative_prompt"] == "blurry"
    assert session.submitted["resolution"] == "512x512"
    assert session.submitted["video_length"] == 9
    assert session.submitted["force_fps"] == "8"
    assert session.submitted["seed"] == 7
    assert session.submitted["guidance_scale"] == 5.5
    assert session.submitted["num_inference_steps"] == 4
    assert session.submitted["loras"] == "[]"
    assert session.timeout_seconds == 30.0
    assert session.cancel_calls == []


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


def test_wan_compiler_preserves_generation_controls() -> None:
    settings = compile_from_inputs(
        {
            "prompt": "p",
            "model": "wan-2.2",
            "frames": 9,
            "fps": 8,
            "force_fps": None,
            "seed": 7,
        }
    )

    assert settings["video_length"] == 9
    assert settings["force_fps"] == "8"
    assert settings["seed"] == 7


def test_wan_compiler_materializes_smoke_defaults() -> None:
    settings = compile_from_inputs({"prompt": "p", "model": "wan-2.2"})

    assert settings["video_length"] == 9
    assert settings["force_fps"] == "8"
    assert settings["resolution"] == "512x512"
    assert settings["seed"] == 0
    assert settings["num_inference_steps"] > 0


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
