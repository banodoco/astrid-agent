from __future__ import annotations

import argparse
import io
import json
import subprocess
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass

import pytest

from astrid import mcp_server


EXPECTED = {
    "generate_list", "generate_image", "generate_video", "generate_describe",
    "generate_workflow", "generate_targets", "generate_release",
    "tasks_create", "tasks_list", "tasks_show", "tasks_cancel", "tasks_retry",
    "tasks_events", "tasks_follow", "engines_install",
}


def _tools():
    return {**mcp_server.build_tools(), **mcp_server._extra_tools()}


def _walk(parser):
    sub = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    return sub.choices


def _real_leaf(tool):
    family, *verbs = tool["path"]
    if family == "generate":
        from astrid.core.generation.cli import _parser
        parser = _parser()
    elif family == "engines":
        from astrid.core.engine_cli import _parser
        parser = _parser()
    else:
        from astrid.core.cli.domain_product import family_parsers
        parser = family_parsers()[family]
    for verb in verbs:
        parser = _walk(parser)[verb]
    return parser


def _parse_tool(tool, arguments):
    argv = mcp_server.argv_for(tool, arguments)
    parsed = _real_leaf(tool).parse_args(argv[len(tool["path"]):])
    return argv, parsed


def test_phase_one_tools_list_and_parser_registration():
    tools = _tools()
    assert set(tools) & EXPECTED == EXPECTED
    assert {name for name in tools if name.startswith(("generate_", "tasks_", "engines_"))} == EXPECTED
    assert "engines_status" not in tools
    assert "generate_audio" not in tools and "generate_speech" not in tools
    assert "tasks_submit" not in tools and "tasks_get" not in tools

    from astrid.core.cli.domain_product import family_parsers
    task_names = {"tasks_" + path[-1] for path, _, _ in mcp_server._leaves(
        family_parsers()["tasks"], ("tasks",), "")}
    from astrid.core.generation.cli import _parser as generation_parser
    gen_names = {"generate_" + path[-1] for path, _, _ in mcp_server._leaves(
        generation_parser(), ("generate",), "")}
    assert task_names == {"tasks_create", "tasks_list", "tasks_show", "tasks_cancel", "tasks_retry", "tasks_events", "tasks_follow"}
    assert gen_names == {"generate_list", "generate_image", "generate_video", "generate_describe", "generate_workflow", "generate_targets", "generate_release"}

    state = {"tools": tools, "prompts": {}}
    listed = mcp_server.handle({"jsonrpc": "2.0", "id": 8, "method": "tools/list"}, state)
    assert {row["name"] for row in listed["result"]["tools"]} >= EXPECTED


def test_task_schemas_and_exact_parser_ordered_argv():
    tools = _tools()
    cases = {
        "tasks_create": ({"project": "string", "capability": "string", "spec": "object",
                          "input_manifest": "array", "generation_intent": "object",
                          "settlement_effect": "object", "execution_request": "object",
                          "idempotency_key": "string"}, ["project", "capability", "spec"],
                         {"project": "p", "capability": "image.make", "spec": {"size": 1},
                          "input_manifest": [{"id": "a"}], "generation_intent": {"x": True},
                          "settlement_effect": {"kind": "publish"},
                          "execution_request": {"target": {"kind": "default"}}, "idempotency_key": "k"},
                         ["tasks", "create", "--project", "p", "--capability", "image.make", "--spec", '{"size": 1}',
                          "--input-manifest", '[{"id": "a"}]', "--generation-intent", '{"x": true}',
                          "--settlement-effect", '{"kind": "publish"}', "--execution-request", '{"target": {"kind": "default"}}',
                          "--idempotency-key", "k", "--json"]),
        "tasks_list": ({"project": "string", "capability": "string", "limit": "integer"}, ["project"],
                        {"project": "p", "capability": "image.make", "limit": 3},
                        ["tasks", "list", "--project", "p", "--capability", "image.make", "--limit", "3", "--json"]),
        "tasks_show": ({"project": "string", "task_id": "string"}, ["project", "task_id"],
                        {"project": "p", "task_id": "abcdef12"},
                        ["tasks", "show", "abcdef12", "--project", "p", "--json"]),
        "tasks_cancel": ({"project": "string", "task_id": "string", "idempotency_key": "string"}, ["project", "task_id"],
                          {"project": "p", "task_id": "abcdef12", "idempotency_key": "cancel-1"},
                          ["tasks", "cancel", "abcdef12", "--project", "p", "--idempotency-key", "cancel-1", "--json"]),
        "tasks_retry": ({"project": "string", "task_id": "string", "idempotency_key": "string"}, ["project", "task_id"],
                         {"project": "p", "task_id": "abcdef12", "idempotency_key": "retry-1"},
                         ["tasks", "retry", "abcdef12", "--project", "p", "--idempotency-key", "retry-1", "--json"]),
        "tasks_events": ({"project": "string", "task_id": "string"}, ["project", "task_id"],
                          {"project": "p", "task_id": "abcdef12"},
                          ["tasks", "events", "abcdef12", "--project", "p", "--json"]),
    }
    for name, (expected_types, required, arguments, expected_argv) in cases.items():
        schema = tools[name]["definition"]["inputSchema"]
        assert {key: val["type"] for key, val in schema["properties"].items()} == expected_types
        assert schema["required"] == required
        assert schema["additionalProperties"] is False
        argv, parsed = _parse_tool(tools[name], arguments)
        assert argv == expected_argv
        assert parsed.json is True

    schema = tools["tasks_follow"]["definition"]["inputSchema"]
    assert {key: value["type"] for key, value in schema["properties"].items()} == {
        "project": "string", "task_id": "string", "poll_seconds": "number", "timeout_seconds": "number"}
    assert schema["required"] == ["project", "task_id"]
    assert schema["properties"]["poll_seconds"]["exclusiveMinimum"] == 0
    assert schema["properties"]["timeout_seconds"]["exclusiveMinimum"] == 0
    tool = tools["tasks_follow"]
    assert mcp_server.argv_for(tool, {"project": "p", "task_id": "task-123"}) == [
        "tasks", "follow", "task-123", "--project", "p", "--json"]
    _, defaults = _parse_tool(tool, {"project": "p", "task_id": "task-123"})
    assert (defaults.poll_seconds, defaults.timeout_seconds) == (2.0, 3600.0)
    args = {"project": "p", "task_id": "task-123", "poll_seconds": 0.25, "timeout_seconds": 4}
    argv, parsed = _parse_tool(tool, args)
    assert argv == ["tasks", "follow", "task-123", "--project", "p", "--poll-seconds", "0.25", "--timeout-seconds", "4", "--json"]
    assert (parsed.poll_seconds, parsed.timeout_seconds) == (0.25, 4.0)
    for option in ("--poll-seconds", "--timeout-seconds"):
        for invalid in ("0", "-1", "nan", "inf"):
            with pytest.raises(SystemExit) as exc:
                _real_leaf(tool).parse_args(["abc12345", "--project", "p", option, invalid])
            assert exc.value.code == 2
    for required_args in ([], ["task-123"], ["--project", "p"]):
        with pytest.raises(SystemExit):
            _real_leaf(tool).parse_args(required_args)
    with pytest.raises(ValueError, match="unknown arguments"):
        mcp_server.argv_for(tool, {"project": "p", "task_id": "abc12345", "json": True})


def test_generate_schema_and_argv_for_all_leaves():
    tools = _tools()
    expectations = {
        "generate_list": ({"kind": {"type": "string", "enum": ["image", "video", "audio", "generation", "fixture"]}}, [],
                          {"kind": "audio"}, ["generate", "list", "--kind", "audio", "--json"]),
        "generate_describe": ({"workflow": "string", "entry": "string", "mode": "string"}, [],
                              {"workflow": "bundle/workflow.py", "entry": "entry-a", "mode": "video"},
                              ["generate", "describe", "bundle/workflow.py", "--entry", "entry-a", "--mode", "video", "--json"]),
        "generate_targets": ({"target": "string", "engine": "string", "save": "boolean", "input": "array"}, [],
                             {"target": "local", "engine": "comfyui", "save": True, "input": ["a=1", "b=true"]},
                             ["generate", "targets", "--target", "local", "--engine", "comfyui", "--save", "--input", "a=1", "--input", "b=true", "--json"]),
        "generate_release": ({"engine": "string", "target": "string", "no_wait": "boolean"}, ["engine", "target"],
                             {"engine": "engine-a", "target": "local", "no_wait": True},
                             ["generate", "release", "--engine", "engine-a", "--target", "local", "--no-wait", "--json"]),
    }
    for name, (types, required, arguments, argv_expected) in expectations.items():
        schema = tools[name]["definition"]["inputSchema"]
        actual = schema["properties"]
        for key, expected in types.items():
            assert actual[key] == expected if isinstance(expected, dict) else actual[key]["type"] == expected
        assert schema["required"] == required
        argv, _ = _parse_tool(tools[name], arguments)
        assert argv == argv_expected
        assert mcp_server.argv_for(tools[name], {})[-1:] == ["--json"]

    for name in ("generate_image", "generate_video"):
        props = tools[name]["definition"]["inputSchema"]["properties"]
        expected_types = {
            "prompt": "string", "named_prompt": "string", "model": "string", "entry": "string",
            "inputs_json": "string", "mode": "string", "execution": "string", "target": "string",
            "project": "string", "out": "string", "dry_run": "boolean", "image_ref": "string",
            "width": "integer", "height": "integer", "steps": "integer", "seed": "integer",
            "guidance_scale": "number", "negative_prompt": "string", "frames": "integer", "fps": "number",
            "input": "array", "engine": "string", "no_wait": "boolean", "consent": "array",
            "max_usd": "number", "deadline": "string", "inputs": "object",
        }
        assert {key: value["type"] for key, value in props.items()} == expected_types
        assert props["consent"]["items"]["enum"] == ["user_env", "paid"]
        assert tools[name]["definition"]["inputSchema"]["required"] == []
        arguments = {"prompt": "portrait", "named_prompt": "alternate", "model": "m", "entry": "e",
                     "inputs_json": "declared.json", "mode": "image", "execution": "catalog",
                     "target": "local", "project": "p", "out": "out.png", "dry_run": True,
                     "image_ref": "source.png", "width": 640, "height": 480, "steps": 22, "seed": 19,
                     "guidance_scale": 4.5, "negative_prompt": "blur", "frames": 9, "fps": 12.5,
                     "input": ["style=soft", "count=2"], "engine": "engine-a", "no_wait": False,
                     "consent": ["user_env", "paid"], "max_usd": 1.5, "deadline": "2030-01-01T00:00:00Z",
                     }
        argv, parsed = _parse_tool(tools[name], arguments)
        assert argv == ["generate", name.removeprefix("generate_"), "portrait", "--prompt", "alternate",
                        "--model", "m", "--entry", "e", "--inputs-json", "declared.json", "--mode", "image",
                        "--execution", "catalog", "--target", "local", "--project", "p", "--out", "out.png",
                        "--dry-run", "--edit", "source.png", "--width", "640", "--height", "480", "--steps", "22",
                        "--seed", "19", "--cfg", "4.5", "--negative-prompt", "blur", "--frames", "9", "--fps", "12.5",
                        "--input", "style=soft", "--input", "count=2", "--engine", "engine-a", "--consent", "user_env",
                        "--consent", "paid", "--max-usd", "1.5", "--deadline", "2030-01-01T00:00:00Z", "--json"]
        assert parsed.consent == ["user_env", "paid"] and parsed.width == 640 and parsed.fps == 12.5
        with pytest.raises(ValueError, match="unknown arguments"):
            mcp_server.argv_for(tools[name], {"invented": 1})

    workflow = tools["generate_workflow"]
    schema = workflow["definition"]["inputSchema"]
    assert {key: value["type"] for key, value in schema["properties"].items()} == {
        "workflow": "string", "as_entry": "string", "replace": "boolean", "inputs_json": "string",
        "target": "string", "mode": "string", "project": "string", "out": "string", "prompt": "string",
        "negative_prompt": "string", "image_ref": "string", "width": "integer", "height": "integer",
        "seed": "integer", "frames": "integer", "fps": "number", "steps": "integer",
        "guidance_scale": "number", "dry_run": "boolean", "input": "array", "engine": "string",
        "no_wait": "boolean", "consent": "array", "max_usd": "number", "deadline": "string", "inputs": "object",
    }
    assert schema["properties"]["consent"]["items"]["enum"] == ["user_env", "paid"]
    assert schema["required"] == ["workflow"]
    assert schema["properties"]["inputs"]["type"] == "object"
    assert "recovery_path" not in schema["properties"]
    assert "target" not in tools["generate_describe"]["definition"]["inputSchema"]["properties"]
    assert "engine" not in tools["generate_describe"]["definition"]["inputSchema"]["properties"]
    args = {"workflow": "bundle.json", "as_entry": "new", "replace": True, "inputs_json": "in.json",
            "target": "local", "mode": "video", "project": "p", "out": "o.mp4", "prompt": "p",
            "negative_prompt": "n", "image_ref": "i.png", "width": 640, "height": 360, "seed": 4,
            "frames": 12, "fps": 24.0, "steps": 30, "guidance_scale": 7.0, "dry_run": True,
            "input": ["x=1", "y=2"], "engine": "engine-x", "no_wait": False,
            "consent": ["paid"], "max_usd": 3.0, "deadline": "2030-01-01T00:00:00Z",
            "inputs": {"node:5.value": [1, 2]}}
    mcp_inputs = args.pop("inputs")
    argv, parsed = _parse_tool(workflow, args)
    assert argv == ["generate", "workflow", "bundle.json", "--as-entry", "new", "--replace", "--inputs-json", "in.json",
                    "--target", "local", "--mode", "video", "--project", "p", "--out", "o.mp4", "--prompt", "p",
                    "--negative-prompt", "n", "--edit", "i.png", "--width", "640", "--height", "360", "--seed", "4",
                    "--frames", "12", "--fps", "24.0", "--steps", "30", "--cfg", "7.0", "--dry-run", "--input",
                    "x=1", "--input", "y=2", "--engine", "engine-x", "--consent", "paid", "--max-usd", "3.0",
                    "--deadline", "2030-01-01T00:00:00Z", "--json"]
    assert parsed.workflow == "bundle.json" and parsed.consent == ["paid"]
    assert mcp_inputs == {"node:5.value": [1, 2]}


def test_engine_install_schema_and_argv_including_defaults():
    tool = _tools()["engines_install"]
    schema = tool["definition"]["inputSchema"]
    assert schema["required"] == ["engine"]
    assert {key: value["type"] for key, value in schema["properties"].items()} == {
        "engine": "string", "platform": "string", "placement": "string", "root": "string",
        "deadline": "number", "dry_run": "boolean", "model_cache_root": "string",
        "model_cache_device": "integer", "model_cache_mount_id": "integer"}
    assert schema["properties"]["placement"]["enum"] == ["local", "runpod"]
    argv, parsed = _parse_tool(tool, {"engine": "engine-a", "platform": "linux-x86_64-cu130",
                                     "placement": "runpod", "root": "/engine", "deadline": 30.5,
                                     "dry_run": True, "model_cache_root": "/cache",
                                     "model_cache_device": 9, "model_cache_mount_id": 42})
    assert argv == ["engines", "install", "engine-a", "--platform", "linux-x86_64-cu130", "--placement", "runpod",
                    "--root", "/engine", "--deadline", "30.5", "--dry-run", "--model-cache-root", "/cache",
                    "--model-cache-device", "9", "--model-cache-mount-id", "42"]
    assert (parsed.platform, parsed.placement, parsed.deadline, parsed.dry_run) == ("linux-x86_64-cu130", "runpod", 30.5, True)
    assert mcp_server.argv_for(tool, {"engine": "e"}) == ["engines", "install", "e"]
    _, defaults = _parse_tool(tool, {"engine": "e"})
    assert (defaults.platform, defaults.placement, defaults.deadline, defaults.dry_run) == (
        "linux-x86_64-cu130", "local", 3600.0, False)
    assert (defaults.root, defaults.model_cache_root, defaults.model_cache_device,
            defaults.model_cache_mount_id) == (None, None, None, None)
    for invalid in ([], ["e", "--placement", "invalid"]):
        with pytest.raises(SystemExit) as exc:
            _real_leaf(tool).parse_args(invalid)
        assert exc.value.code == 2
    with pytest.raises(ValueError, match="unknown arguments"):
        mcp_server.argv_for(tool, {"engine": "e", "json": True})


def test_generation_call_adapter_forces_admission_and_preserves_cli_envelopes(monkeypatch):
    calls = []
    replies = [
        subprocess.CompletedProcess([], 0, stdout='{"ok": true, "data": {"task_id": "t1"}}', stderr=""),
        subprocess.CompletedProcess([], 0, stdout='{"ok": true, "data": {"task_id": "t2"}}', stderr=""),
        subprocess.CompletedProcess([], 0, stdout='{"ok": true, "data": {"task_id": "t3"}}', stderr=""),
        subprocess.CompletedProcess([], 2, stdout='{"ok": false, "error": {"code": "invalid"}}', stderr="bad input"),
    ]

    def fake_run(argv, cwd):
        calls.append(argv)
        return replies.pop(0)

    monkeypatch.setattr(mcp_server, "_run", fake_run)
    tool = _tools()["generate_image"]
    state = {"tools": _tools(), "prompts": {}}
    result = mcp_server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                "params": {"name": "generate_image", "arguments": {
                                    "prompt": "portrait", "input": ["style=soft"],
                                    "inputs": {"width": 512, "node:4.seed": 3}, "no_wait": False}}}, state)
    assert result["result"]["structuredContent"] == {"ok": True, "data": {"task_id": "t1"}}
    assert calls[0][-10:] == ["image", "portrait", "--input", "style=soft", "--input", "width=512",
                              "--input", "node:4.seed=3", "--no-wait", "--json"]

    for name, arguments, suffix in (
        ("generate_video", {"prompt": "motion", "no_wait": False}, ["video", "motion", "--no-wait", "--json"]),
        ("generate_workflow", {"workflow": "bundle.json", "inputs": {"node:7.seed": 9}, "no_wait": False},
         ["workflow", "bundle.json", "--input", "node:7.seed=9", "--no-wait", "--json"]),
    ):
        response = mcp_server.handle({"jsonrpc": "2.0", "id": name, "method": "tools/call",
                                      "params": {"name": name, "arguments": arguments}}, state)["result"]
        assert response["isError"] is False
        assert calls[-1][-len(suffix):] == suffix

    failed = mcp_server.call_tool(tool, {"prompt": "portrait"})
    assert failed["isError"] is True
    assert failed["structuredContent"] == {"ok": False, "error": {"code": "invalid"}}
    assert failed["content"] == [{"type": "text", "text": '{"ok": false, "error": {"code": "invalid"}}'},
                                  {"type": "text", "text": "bad input"}]


def test_task_follow_tool_runs_real_handler_with_fake_reads(monkeypatch):
    from astrid.core.cli import domain_tasks
    from astrid.core.cli.domain_tasks import build_parser
    from astrid.sdk.contracts import DomainResult, ErrorObject
    real_follow_task = domain_tasks.follow_task

    def no_sleep_follow(*args, **kwargs):
        return real_follow_task(*args, sleep=lambda _: None, **kwargs)

    monkeypatch.setattr(domain_tasks, "follow_task", no_sleep_follow)

    class Tasks:
        def __init__(self, terminal):
            self.terminal = terminal
            self.reads = 0

        def list(self, *args, **kwargs):
            raise AssertionError("full task IDs must not trigger a prefix lookup")

        def show(self, task_id):
            self.reads += 1
            if self.terminal == "error":
                if self.reads > 1:
                    return DomainResult.failure(ErrorObject("runtime_down", "read failed", {}))
                return DomainResult.success({"task_id": task_id, "state": "running", "version": self.reads})
            return DomainResult.success({"task_id": task_id, "state": self.terminal, "version": self.reads})

    class Client:
        def __init__(self, terminal):
            self.tasks = Tasks(terminal)

    calls = []
    def fake_run(argv, cwd):
        calls.append(argv)
        terminal = "completed" if len(calls) == 1 else "error"
        client = Client(terminal)
        parser = build_parser(client)
        output, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            parsed = parser.parse_args(argv[argv.index("tasks") + 1:])
            code = parsed.handler(parsed)
        assert client.tasks.reads >= 1
        return subprocess.CompletedProcess(argv, code, stdout=output.getvalue(), stderr=errors.getvalue())

    monkeypatch.setattr(mcp_server, "_run", fake_run)
    toolmap = _tools()
    state = {"tools": toolmap, "prompts": {}}
    for ident in (21, 22):
        reply = mcp_server.handle({"jsonrpc": "2.0", "id": ident, "method": "tools/call",
                                   "params": {"name": "tasks_follow", "arguments": {
                                       "project": "p", "task_id": "task-123", "poll_seconds": 0.01,
                                       "timeout_seconds": 2}}}, state)["result"]
        assert reply["isError"] is (ident == 22)
        assert "structuredContent" not in reply
        rows = [json.loads(line) for line in reply["content"][0]["text"].splitlines()]
        assert rows[0]["kind"] == "observed"
        assert rows[0]["state"] == ("completed" if ident == 21 else "running")
        assert rows[-1]["kind"] == "final"
        assert len(rows) == 2
        assert rows[-1]["ok"] is (ident == 21)
        assert rows[-1]["error"] == (None if ident == 21 else {
            "code": "runtime_down", "message": "read failed"})
        if ident == 21:
            assert rows[-1]["task_id"] == "task-123"
            assert rows[-1]["terminal"] is True
            assert rows[-1]["observation_count"] == 1
        assert calls[-1][-9:] == ["follow", "task-123", "--project", "p", "--poll-seconds", "0.01",
                                  "--timeout-seconds", "2", "--json"]


def test_engine_install_success_and_coded_error_envelopes(monkeypatch):
    from astrid.core import engine_cli
    from astrid.core.generation.engine_contracts import RecipeValidationError

    @dataclass
    class Plan:
        root: str
        platform: str
        placement: str

    monkeypatch.setattr(engine_cli, "load_recipe", lambda engine, platform: {"id": engine, "platform": platform})
    monkeypatch.setattr(engine_cli, "_root", lambda args: "/fake/engine")
    monkeypatch.setattr(engine_cli, "plan_install", lambda recipe, root, placement: Plan(
        root=root, platform=recipe["platform"], placement=placement))
    monkeypatch.setattr(engine_cli, "validate_model_cache_root", lambda *args, **kwargs: None)
    monkeypatch.setattr(engine_cli, "_custody_preflight", lambda root: None)
    monkeypatch.setattr(engine_cli, "install_engine", lambda *args, **kwargs: {"status": "installed", "engine": "e"})
    monkeypatch.setattr(engine_cli, "_default_backend", lambda **kwargs: (object(), object()))
    calls = []

    def fake_run(argv, cwd):
        calls.append(argv)
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = engine_cli.main(argv[argv.index("engines") + 1:])
        return subprocess.CompletedProcess(argv, code, stdout=stdout.getvalue(), stderr=stderr.getvalue())

    monkeypatch.setattr(mcp_server, "_run", fake_run)
    tools = _tools()
    state = {"tools": tools, "prompts": {}}
    success = mcp_server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                 "params": {"name": "engines_install", "arguments": {"engine": "e", "dry_run": False}}}, state)["result"]
    assert success["isError"] is False and success["structuredContent"] == {"status": "installed", "engine": "e"}
    monkeypatch.setattr(engine_cli, "install_engine", lambda *args, **kwargs: (_ for _ in ()).throw(RecipeValidationError("bad")))
    failure = mcp_server.call_tool(tools["engines_install"], {"engine": "e"})
    assert failure["isError"] is True and "structuredContent" not in failure
    coded = json.loads(failure["content"][0]["text"])
    assert coded == {"error": {"code": "invalid_recipe", "step": None, "retryable": False,
                               "recovery_handle": None, "message": "Engine operation failed."}}
    assert calls[0][calls[0].index("engines") + 1:] == ["install", "e"]

    def unexpected_execution(*args, **kwargs):
        raise AssertionError("dry-run must not execute the backend or install")

    monkeypatch.setattr(engine_cli, "_default_backend", unexpected_execution)
    monkeypatch.setattr(engine_cli, "install_engine", unexpected_execution)
    dry_run = mcp_server.call_tool(tools["engines_install"], {"engine": "e", "dry_run": True})
    assert dry_run["isError"] is False
    assert dry_run["structuredContent"] == {
        "plan": {"root": "/fake/engine", "platform": "linux-x86_64-cu130", "placement": "local"},
        "recipe": {"id": "e", "platform": "linux-x86_64-cu130"},
    }
