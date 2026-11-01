from __future__ import annotations

import json
import socket
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
import astrid
from astrid.sdk import invocation, _child_bridge as bridge_module
from pathlib import Path
import yaml

def write_pack(parent: Path, pack_id="ordinary", *, actions=None) -> Path:
    root = parent / pack_id
    (root / "actions").mkdir(parents=True, exist_ok=True)
    (root / "actions/echo.py").write_text("def echo(message):\n    return {'message': message}\n")
    (root / "actions/composed.py").write_text(
        "import astrid\n"
        "def compose(message):\n"
        "    result = astrid.invoke('ordinary.echo', kind='action', inputs={'message': message}, "
        "extra_pack_roots=(_pack_root,), client=_client)\n"
        "    return {'child': result.raw_result['payload']['action_result']}\n"
    )
    if actions is None:
        actions = {
            name: {"description": f"Run {name}.",
                   "invocation": {"kind": "python", "path": path, "function": function},
                   "inputs": [{"name": "message", "type": "string", "required": True}],
                   "metadata": {"project_scope": "optional"}}
            for name, path, function in (("echo", "actions/echo.py", "echo"),
                                         ("compose", "actions/composed.py", "compose"))
        }
    for action in actions.values():
        action.setdefault("inputs", [])
        action.setdefault("outputs", [])
    (root / "pack.yaml").write_text(yaml.safe_dump({"schema_version": 3, "id": pack_id,
                                                  "name": "Ordinary", "version": "1.0.0", "actions": actions}))
    return root


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    from astrid.core.execution.executor import registry as executor_registry
    from astrid.core.execution.orchestrator import registry as orchestrator_registry
    from astrid.core.pack import discovery as pack_discovery

    monkeypatch.setenv("ASTRID_SOURCE_STATE", str(tmp_path / "absent-state.json"))
    monkeypatch.delenv("ASTRID_PACKS_PATH", raising=False)
    # These tests own their fixture packs. Keep real external discovery while
    # avoiding unrelated bundled resource reads on every fixture lookup; the
    # F04 composition regression separately exercises actual selected roots.
    for module in (executor_registry, orchestrator_registry, pack_discovery):
        monkeypatch.setattr(module, "discover_packs", lambda *args, **kwargs: ())



@pytest.fixture
def channel(monkeypatch):
    local, host = socket.socketpair()
    bridge = bridge_module.ChildBridge(local, io_timeout_seconds=0.2)
    monkeypatch.setattr(bridge_module, "_bridge", bridge)
    requests, failures = [], []
    terminal = {"state": "completed", "result": {"payload": {"action_result": {"ok": False, "returncode": 42}}}}

    def serve():
        try:
            with host.makefile("rb") as stream:
                while frame := stream.readline():
                    request = json.loads(frame)
                    requests.append(request)
                    key = request["child_key"]
                    task = {"run_id": "R-" + key, "task_id": "T-" + key, "attempt_id": "A-" + key}
                    if request["op"] == "status":
                        task.update(terminal)
                    data = [] if request["op"] == "outputs" else task
                    host.sendall(json.dumps({"v": 1, "request_id": request["request_id"], "ok": True, "data": data}).encode() + b"\n")
        except (OSError, ValueError) as exc:
            failures.append(type(exc).__name__)

    worker = threading.Thread(target=serve, daemon=True)
    worker.start()
    yield bridge, requests, terminal
    bridge.close()
    host.close()
    worker.join(timeout=1)
    assert not worker.is_alive()


def call(root, **kwargs):
    return astrid.invoke("ordinary.echo", kind="action", extra_pack_roots=(str(root),), **kwargs)


def test_parent_policy_forwarded_outside_action_inputs(tmp_path, isolated, monkeypatch):
    root = write_pack(tmp_path)
    policy = {"capabilities": [{"capability_id": "ordinary.echo", "capability_digest": "sha256:" + "a" * 64}],
              "targets": [{"kind": "local", "id": "test"}], "input_object_ids": [], "limits": {"max_children": 2}}
    calls = []
    def create(**kwargs):
        calls.append(kwargs)
        return {"run_id": "R", "task_id": "T"}
    client = SimpleNamespace(tasks=SimpleNamespace(create=create))
    assert call(root, inputs={"message": "hello"}, child_delegation=policy, client=client).ok
    assert calls[0]["child_delegation"] == policy
    assert calls[0]["spec"]["inputs"] == {"message": "hello"}
    assert "child_delegation" not in calls[0]["spec"]
    assert call(root, inputs={"message": "hello"}, client=client).ok
    assert "child_delegation" not in calls[1]


def recoverable_policy(**changes):
    pin = {"capability_id": "editorial.human_review", "capability_digest": "sha256:" + "a" * 64}
    return {"capabilities": [pin], "targets": [{"kind": "local", "id": "test"}],
            "input_object_ids": [], "limits": {"max_children": 2},
            "recoverable_outputs": [{**pin, "output_ports": ["state_result"]}], **changes}


@pytest.fixture
def generated_admission(monkeypatch):
    from astrid_runtime_client.generated import WorkspaceClient as GeneratedWorkspaceClient
    from astrid.sdk.remote import RemoteAstridClient
    from astrid.sdk.workspace_client import WorkspaceClient

    requests = []

    def transport(method, path, headers, body):
        assert (method, path) == ("POST", "/v1/tasks")
        requests.append(json.loads(body))
        data = {"run_id": "R", "task_id": "T", "attempt_id": "A"}
        receipt = {"receipt_id": "receipt-1", "command_kind": "task.admit",
                   "idempotency_key": headers["Idempotency-Key"], "request_hash": "b" * 64,
                   "project_id": "test", "project_seq": [1, 1], "event_ids": [],
                   "result": data, "created_at": "2026-10-03T00:00:00Z"}
        return 201, {}, json.dumps({"data": data, "receipt": receipt}).encode()

    workspace = WorkspaceClient("http://127.0.0.1:1", "test-token")
    monkeypatch.setattr(workspace, "_generated", GeneratedWorkspaceClient(workspace.endpoint, transport=transport))
    # Control the catalog read only; retain RemoteTasks, the workspace facade,
    # and the actual generated admission serializer on the public invoke path.
    monkeypatch.setattr(workspace, "list_capabilities", lambda **kwargs: ([{
        "capability_id": "ordinary.echo", "definition_digest": "sha256:" + "b" * 64,
        "status": "ready"}], None))
    return RemoteAstridClient(workspace), requests


def invoke_parent_policy(root, policy, client, entry_point, *, project=None):
    if entry_point == "public":
        return call(root, inputs={"message": "hello"}, child_delegation=policy, client=client, project=project)
    capability = astrid.get_capability("ordinary.echo", kind="action", extra_pack_roots=(str(root),))
    return invocation._kernel_invoke(capability, kind="executor", project=project,
                                     inputs={"message": "hello"}, outputs=None,
                                     child_delegation=policy, _client=client)


def test_recoverable_policy_public_to_generated_admission_unchanged(tmp_path, isolated, monkeypatch,
                                                                   generated_admission):
    root = write_pack(tmp_path)
    client, requests = generated_admission
    policy = recoverable_policy()
    original = json.dumps(policy, sort_keys=True, separators=(",", ":"))
    validations = []
    validate = bridge_module._validate_child_policy

    def checked(value):
        validations.append(json.loads(json.dumps(value)))
        return validate(value)

    monkeypatch.setattr(bridge_module, "_validate_child_policy", checked)
    result = invoke_parent_policy(root, policy, client, "public")
    assert result.ok and result.kernel_task_id == "T"
    assert validations == [policy, policy]  # Public invoke and _kernel_invoke both validate.
    assert len(requests) == 1
    assert json.dumps(requests[0]["child_delegation"], sort_keys=True, separators=(",", ":")) == original
    assert json.dumps(policy, sort_keys=True, separators=(",", ":")) == original
    assert requests[0]["spec"]["inputs"] == {"message": "hello"}
    assert "child_delegation" not in requests[0]["spec"]
    assert "recoverable_outputs" not in requests[0]["spec"]


@pytest.mark.parametrize("entry_point", ["public", "kernel"])
@pytest.mark.parametrize("grants", [None, False, {}, [], "state_result",
    [None], [False], [[]], [{}],
    *[[{key: value for key, value in recoverable_policy()["recoverable_outputs"][0].items()
        if key != missing}] for missing in ("capability_id", "capability_digest", "output_ports")],
    [{**recoverable_policy()["recoverable_outputs"][0], "authority": "forged"}],
    recoverable_policy()["recoverable_outputs"] * 2,
    [*recoverable_policy()["recoverable_outputs"],
     {**recoverable_policy()["recoverable_outputs"][0], "capability_digest": "sha256:" + "b" * 64}],
    *[[{**recoverable_policy()["recoverable_outputs"][0], field: value}]
      for field, values in (
          ("capability_id", (None, False, "", "ordinary.echo")),
          ("capability_digest", (None, False, "", "sha256:" + "b" * 64,
                                 "sha256:" + "A" * 64, "a" * 64)),
          ("output_ports", (None, False, "state_result", [], ["other"],
                            ["state_result", "other"], ["state_result", "state_result"], ("state_result",))),
      ) for value in values],
])
def test_recoverable_policy_invalid_grant_rejected_before_admission(tmp_path, isolated,
                                                                   generated_admission, entry_point, grants):
    root = write_pack(tmp_path)
    client, requests = generated_admission
    with pytest.raises(astrid.CapabilityValidationError, match="recoverable_outputs"):
        invoke_parent_policy(root, recoverable_policy(recoverable_outputs=grants), client, entry_point)
    assert requests == []


@pytest.mark.parametrize("entry_point", ["public", "kernel"])
@pytest.mark.parametrize("capabilities", [
    [{"capability_id": "ordinary.echo", "capability_digest": "sha256:" + "a" * 64}],
    [{"capability_id": "editorial.human_review", "capability_digest": "sha256:" + "b" * 64}],
    [{"capability_id": "editorial.human_review"}],
    [{**recoverable_policy()["capabilities"][0], "authority": "forged"}],
])
def test_recoverable_policy_requires_exact_capability_pin_before_admission(tmp_path, isolated,
                                                                         generated_admission, entry_point,
                                                                         capabilities):
    root = write_pack(tmp_path)
    client, requests = generated_admission
    with pytest.raises(astrid.CapabilityValidationError, match="recoverable_outputs"):
        invoke_parent_policy(root, recoverable_policy(capabilities=capabilities), client, entry_point)
    assert requests == []


@pytest.mark.parametrize("digest", [None, False, "", "sha256:" + "A" * 64, "a" * 64])
def test_recoverable_policy_malformed_digest_rejected_even_when_pin_matches(digest):
    policy = recoverable_policy()
    policy["capabilities"][0]["capability_digest"] = digest
    policy["recoverable_outputs"][0]["capability_digest"] = digest
    with pytest.raises(astrid.CapabilityValidationError, match="recoverable_outputs"):
        astrid.invoke("absent", kind="action", child_delegation=policy)


@pytest.mark.parametrize("explicit_key", [False, True])
def test_recoverable_policy_valid_grant_still_rejected_in_child_mode(tmp_path, isolated, monkeypatch,
                                                                  channel, explicit_key):
    root = write_pack(tmp_path)
    _, messages, _ = channel
    monkeypatch.setattr(invocation, "_kernel_invoke", lambda *a, **k: pytest.fail("normal admission"))
    policy = recoverable_policy()
    assert bridge_module._validate_child_policy(policy) == policy
    with pytest.raises(astrid.CapabilityValidationError, match="child invocation cannot set child_delegation"):
        call(root, inputs={"message": "hello"}, child_delegation=policy,
             **({"child_key": "explicit"} if explicit_key else {}))
    assert messages == []


def test_recoverable_policy_valid_grant_rejected_with_child_key_without_bridge(monkeypatch):
    monkeypatch.setattr(bridge_module, "_bridge", None)
    with pytest.raises(astrid.CapabilityValidationError, match="child invocation cannot set child_delegation"):
        astrid.invoke("absent", kind="action", child_key="explicit", child_delegation=recoverable_policy())


@pytest.mark.parametrize("policy", [False, {}, {"capabilities": [], "targets": [], "input_object_ids": []},
    {"capabilities": [{}], "targets": [{}], "input_object_ids": [], "limits": {"max_children": float("inf")}}])
def test_policy_shape_rejected_before_admission(policy):
    with pytest.raises(astrid.CapabilityValidationError):
        astrid.invoke("absent", kind="action", child_delegation=policy)


def test_bridge_exact_identity_key_result_and_no_normal_admission(tmp_path, isolated, monkeypatch, channel):
    root = write_pack(tmp_path)
    _, messages, _ = channel
    monkeypatch.setattr(invocation, "_kernel_invoke", lambda *a, **k: pytest.fail("normal admission"))
    client = SimpleNamespace(tasks=SimpleNamespace(create=lambda **k: pytest.fail("ambient credentials")))
    result = call(root, inputs={"message": "hello"}, child_key="echo-1", wait=True,
                  timeout_seconds=0.5, poll_seconds=0.01, client=client)
    assert result.ok and result.kernel_task_id == "T-echo-1" and result.kernel_attempt_id == "A-echo-1"
    assert result.raw_result["result"]["payload"]["action_result"] == {"ok": False, "returncode": 42}
    assert result.outputs == {"artifacts": [], "managed_outputs": []} and result.run_root is None
    assert [m["op"] for m in messages] == ["submit", "status", "outputs"]
    submit = messages[0]
    assert set(submit) == {"v", "request_id", "op", "child", "inputs", "input_descriptors", "child_key", "wait", "timeout_seconds", "poll_seconds"}
    assert submit["child"] == {"capability_id": "ordinary.echo", "capability_digest": "sha256:" + result.executor_version}
    assert submit["child_key"] == "echo-1" and submit["inputs"] == {"message": "hello"}
    assert submit["wait"] is True and submit["timeout_seconds"] == 0.5 and submit["poll_seconds"] == 0.01
    assert all(m["child_key"] == "echo-1" for m in messages)


def _command_action(*, input_type="string", input_args=None):
    command = {"argv": ["python", "-m", "fixture.tool"]}
    if input_args is not None:
        command["input_args"] = input_args
    return {"description": "Repeatable command.",
            "invocation": {"kind": "command", "command": command},
            "inputs": [{"name": "serve", "type": input_type, "required": False}]}


def _invoke_command_child(root, *, pack_id, serve, **kwargs):
    return astrid.invoke(f"{pack_id}.echo", kind="action", extra_pack_roots=(str(root),),
                         inputs={"serve": serve}, **kwargs)


def test_repeatable_child_string_input_preserves_list_and_binds_repeated_flags(
        tmp_path, isolated, channel):
    _, messages, _ = channel
    mounts = ["/clips=one", "/assets=two"]
    root = write_pack(
        tmp_path,
        pack_id="repeatable",
        actions={"echo": _command_action(
            input_args=[{"input": "serve", "flag": "--serve", "repeatable": True,
                         "optional": True}]
        )},
    )
    capability = astrid.get_capability("repeatable.echo", kind="action",
                                       extra_pack_roots=(str(root),))
    assert invocation._manifest_preview_command(
        capability, inputs={"serve": mounts}, outputs=None, brief=None, python_exec=None
    ) == ["python", "-m", "fixture.tool", "--serve", "/clips=one",
          "--serve", "/assets=two"]

    many = _invoke_command_child(root, pack_id="repeatable", serve=mounts,
                                 child_key="serve-many")
    scalar = _invoke_command_child(root, pack_id="repeatable", serve="/single=mount",
                                   child_key="serve-one")
    assert many.ok and scalar.ok
    assert messages[0]["inputs"] == {"serve": mounts}
    assert messages[1]["inputs"] == {"serve": "/single=mount"}


@pytest.mark.parametrize("action", [
    _command_action(),
    _command_action(input_args=[{"input": "serve", "flag": "--serve", "optional": True}]),
    _command_action(input_type="path", input_args=[
        {"input": "serve", "flag": "--serve", "repeatable": True, "optional": True}
    ]),
])
def test_repeatable_child_list_requires_exact_supported_mapping(
        tmp_path, isolated, channel, action):
    _, messages, _ = channel
    root = write_pack(tmp_path, pack_id="invalid", actions={"echo": action})
    with pytest.raises(astrid.CapabilityValidationError):
        _invoke_command_child(root, pack_id="invalid", serve=["/clips=one", "/assets=two"],
                              child_key="invalid-mapping")
    assert messages == []


def test_repeatable_child_rejects_non_string_members_before_bridge(tmp_path, isolated, channel):
    _, messages, _ = channel
    root = write_pack(
        tmp_path,
        pack_id="invalid_members",
        actions={"echo": _command_action(
            input_args=[{"input": "serve", "flag": "--serve", "repeatable": True,
                         "optional": True}]
        )},
    )
    for value in (["/clips=one", 2], [["/nested=mount"]], [True], [None]):
        with pytest.raises(astrid.CapabilityValidationError):
            _invoke_command_child(root, pack_id="invalid_members", serve=value,
                                  child_key="invalid-members")
    assert messages == []


def test_schema_backed_python_child_preserves_ordered_array(tmp_path, isolated, channel):
    _, messages, _ = channel
    root = write_pack(tmp_path, pack_id="schema_array", actions={"echo": {
        "description": "Schema-backed array.",
        "invocation": {"kind": "python", "path": "actions/echo.py", "function": "echo"},
        "inputs": {"type": "object", "properties": {
            "items": {"type": "array", "items": {"type": "object", "properties": {
                "id": {"type": "string"}}, "required": ["id"], "additionalProperties": False}}},
            "required": ["items"], "additionalProperties": False}}})
    ordered = [{"id": "first"}, {"id": "second"}]
    result = astrid.invoke("schema_array.echo", kind="action", extra_pack_roots=(str(root),),
                           inputs={"items": ordered}, child_key="schema-array")
    assert result.ok
    assert messages[0]["inputs"] == {"items": ordered}


def test_schema_backed_malformed_array_rejected_before_bridge(tmp_path, isolated, channel):
    _, messages, _ = channel
    root = write_pack(tmp_path, pack_id="schema_array_invalid", actions={"echo": {
        "description": "Schema-backed array.",
        "invocation": {"kind": "python", "path": "actions/echo.py", "function": "echo"},
        "inputs": {"type": "object", "properties": {
            "items": {"type": "array", "items": {"type": "object", "properties": {
                "id": {"type": "string"}}, "required": ["id"], "additionalProperties": False}}},
            "required": ["items"], "additionalProperties": False}}})
    with pytest.raises(astrid.CapabilityValidationError):
        astrid.invoke("schema_array_invalid.echo", kind="action", extra_pack_roots=(str(root),),
                      inputs={"items": [{"id": "first"}, {"id": 2}]}, child_key="schema-array-invalid")
    assert messages == []


def test_json_child_port_preserves_list(tmp_path, isolated, channel):
    _, messages, _ = channel
    root = write_pack(tmp_path, pack_id="json_port", actions={"echo": {
        "description": "JSON port.",
        "invocation": {"kind": "python", "path": "actions/echo.py", "function": "echo"},
        "inputs": [{"name": "payload", "type": "json", "required": True}]}})
    payload = ["first", {"second": 2}]
    result = astrid.invoke("json_port.echo", kind="action", extra_pack_roots=(str(root),),
                           inputs={"payload": payload}, child_key="json-port")
    assert result.ok
    assert messages[0]["inputs"] == {"payload": payload}


def test_auto_key_is_stable_for_exact_request(tmp_path, isolated, channel):
    root = write_pack(tmp_path)
    _, messages, _ = channel
    call(root, inputs={"message": "hello"})
    call(root, inputs={"message": "hello"})
    call(root, inputs={"message": "different"})
    assert messages[0]["child_key"] == messages[1]["child_key"] != messages[2]["child_key"]


def test_validation_and_recursion_precede_bridge(tmp_path, isolated, channel):
    root = write_pack(tmp_path, actions={"echo": {"description": "Validate.",
        "invocation": {"kind": "python", "path": "actions/echo.py", "function": "echo"},
        "inputs": {"type": "object", "properties": {"message": {"type": "string"}},
                   "required": ["message"], "additionalProperties": False}}})
    _, messages, _ = channel
    for kwargs in ({"inputs": {"message": 1}}, {"inputs": {"message": "hello"}, "child_delegation": {}},
                   {"inputs": {"message": "hello"}, "idempotency_context": {"parent_attempt_id": "forged"}},
                   {"inputs": {"message": "hello"}, "child_key": "bad key"},
                   {"inputs": {"message": "hello"}, "timeout_seconds": float("nan")}):
        with pytest.raises(astrid.CapabilityValidationError):
            call(root, **kwargs)
    assert messages == []


def test_missing_and_closed_bridge_fail_closed(tmp_path, isolated, monkeypatch):
    root = write_pack(tmp_path)
    monkeypatch.setattr(bridge_module, "_bridge", None)
    monkeypatch.setattr(invocation, "_kernel_invoke", lambda *a, **k: pytest.fail("fallback"))
    with pytest.raises(astrid.CapabilityInvocationError, match="live host bridge"):
        call(root, inputs={"message": "hello"}, child_key="explicit")
    local, host = socket.socketpair()
    bridge = bridge_module._install_child_bridge(local)
    bridge.close()
    host.close()
    with pytest.raises(astrid.CapabilityInvocationError, match="closed"):
        call(root, inputs={"message": "hello"})


def test_ordered_file_bindings_have_only_relative_producer_identity(tmp_path, isolated, channel):
    root = write_pack(tmp_path, actions={"echo": {"description": "Files.",
        "invocation": {"kind": "python", "path": "actions/echo.py", "function": "echo"},
        "inputs": [{"name": "z", "type": "file"}, {"name": "a", "type": "file"}]}})
    _, messages, _ = channel
    value = {"filename": "scratch/child.txt", "media_type": "text/plain", "output_port": "text"}
    call(root, inputs={"a": "sha256:" + "b" * 64, "z": value})
    assert messages[0]["input_descriptors"] == [
        {"name": "z", "kind": "producer_file", **value},
        {"name": "a", "kind": "object", "object_id": "sha256:" + "b" * 64}]
    for filename in ("/private/foreign", "../foreign", "scratch/../../foreign", "scratch\\foreign"):
        with pytest.raises(astrid.CapabilityValidationError):
            call(root, inputs={"z": dict(value, filename=filename)})
    with pytest.raises(astrid.CapabilityValidationError):
        call(root, inputs={"z": dict(value, size=42)})
    assert len(messages) == 1


def test_concurrent_submission_during_child_wait(tmp_path, isolated, channel):
    root = write_pack(tmp_path)
    _, messages, terminal = channel
    terminal.update(state="running", result={})
    with ThreadPoolExecutor(max_workers=2) as pool:
        waiting = pool.submit(call, root, inputs={"message": "hello"}, child_key="waiting", wait=True,
                              timeout_seconds=0.2, poll_seconds=0.01)
        admitted = pool.submit(call, root, inputs={"message": "hello"}, child_key="other")
        assert admitted.result(timeout=1).ok
        timed = waiting.result(timeout=1)
    assert not timed.ok and timed.error["code"] == "task_wait_timeout"
    assert timed.error["sdk_category"] == "runtime"
    assert any(m["op"] == "submit" and m["child_key"] == "other" for m in messages)


def test_cancelled_and_strict_output_semantics(tmp_path, isolated, channel):
    root = write_pack(tmp_path, actions={"echo": {"description": "Strict output.",
        "invocation": {"kind": "python", "path": "actions/echo.py", "function": "echo"},
        "outputs": {"type": "integer"}}})
    _, _, terminal = channel
    terminal.update(state="cancelled", result={"error": {"message": "parent revoked"}})
    result = call(root, wait=True)
    assert not result.ok and result.error["code"] == "task_cancelled"
    terminal.update(state="completed", result={"payload": {"action_result": "invalid"}})
    with pytest.raises(astrid.CapabilityValidationError):
        call(root, wait=True)


@pytest.mark.parametrize("reply", [b"invalid\n", b'{"v":1,"request_id":999,"ok":true,"data":{}}\n', b"x" * (1024 * 1024 + 1)])
def test_invalid_channel_reply_is_bounded_and_revokes(reply):
    local, host = socket.socketpair()
    bridge = bridge_module.ChildBridge(local, io_timeout_seconds=0.2)
    def send():
        try:
            host.recv(65536)
            host.sendall(reply)
        except OSError:
            pass
    worker = threading.Thread(target=send, daemon=True)
    worker.start()
    with pytest.raises(astrid.CapabilityInvocationError):
        bridge.submit({"child_key": "x"})
    assert bridge._closed
    host.close()
    worker.join(timeout=1)


def test_invalid_inputs_and_oversized_request_never_reach_host(tmp_path, isolated, channel):
    root = write_pack(tmp_path)
    _, messages, _ = channel
    for inputs in ({"message": object()}, {"message": float("nan")}, {"message": 42}, {"message": "x" * 1024 * 1024},
                   {"message": "hello", "authority": "forged"}, {}):
        with pytest.raises((astrid.CapabilityValidationError, astrid.CapabilityMissingInputError)):
            call(root, inputs=inputs)
    assert not messages


def test_host_admission_rejection_preserves_runtime_result_taxonomy(tmp_path, isolated, monkeypatch):
    root = write_pack(tmp_path)
    local, host = socket.socketpair()
    bridge = bridge_module.ChildBridge(local, io_timeout_seconds=0.2)
    monkeypatch.setattr(bridge_module, "_bridge", bridge)
    error = {"code": "authorization_error", "message": "child outside policy", "details": {}}
    def reject():
        request = json.loads(host.makefile("rb").readline())
        host.sendall(json.dumps({"v": 1, "request_id": request["request_id"], "ok": False, "error": error}).encode() + b"\n")
    worker = threading.Thread(target=reject, daemon=True)
    worker.start()
    try:
        result = call(root, inputs={"message": "hello"})
        assert not result.ok and result.error == {**error, "sdk_error": "CapabilityRuntimeError", "sdk_category": "runtime"}
        assert result.kernel_task_id is None
    finally:
        bridge.close()
        host.close()
        worker.join(timeout=1)


def test_unresponsive_channel_times_out_and_revokes():
    local, host = socket.socketpair()
    bridge = bridge_module.ChildBridge(local, io_timeout_seconds=0.03)
    try:
        with pytest.raises(astrid.CapabilityInvocationError):
            bridge.status("child", "T-child")
        assert bridge._closed
    finally:
        bridge.close()
        host.close()


def test_foreign_status_fails_closed(tmp_path, isolated, channel):
    root = write_pack(tmp_path)
    _, messages, terminal = channel
    terminal["task_id"] = "T-foreign"
    with pytest.raises(astrid.CapabilityInvocationError, match="foreign task"):
        call(root, inputs={"message": "hello"}, wait=True)
    assert [item["op"] for item in messages] == ["submit", "status"]


@pytest.mark.parametrize("failure", [
    "late_reply", "late_timeout", "late_partial", "early_timeout", "early_timeout_diagnostic_delay",
    "early_timeout_cleanup_delay", "late_eof", "late_oversized", "late_multiframe",
    "late_malformed", "late_envelope", "late_foreign_task", "late_foreign_run", "late_nodata",
    "late_rejection", "late_bad_error",
])
def test_child_wait_deadline_preserves_transport_and_protocol_taxonomy(
        tmp_path, isolated, monkeypatch, failure):
    root = write_pack(tmp_path)
    clock = SimpleNamespace(now=0.0)
    task = {"run_id": "R-child", "task_id": "T-child", "attempt_id": "A-child", "state": "queued"}

    def sleep(seconds):
        clock.now += seconds

    fake_time = SimpleNamespace(monotonic=lambda: clock.now, sleep=sleep)
    monkeypatch.setattr(invocation, "time", fake_time)
    monkeypatch.setattr(bridge_module, "time", fake_time)
    monkeypatch.setattr(invocation, "_kernel_invoke", lambda *a, **k: pytest.fail("normal admission"))

    class Channel:
        def __init__(self):
            self.requests = []
            self.events = []
            self.closed = False

        def settimeout(self, seconds):
            self.events.append(("budget", clock.now, seconds))

        def sendall(self, data):
            self.requests.append(json.loads(data))
            self.events.append(("send", clock.now, self.requests[-1]["op"]))

        def recv(self, size):
            request = self.requests[-1]
            data = dict(task)
            if request["op"] == "status":
                clock.now = 1.0 if failure.startswith("early_timeout") else 2.0
                self.events.append(("receive", clock.now, request["op"]))
                if failure == "late_timeout" or failure.startswith("early_timeout"):
                    raise TimeoutError("socket receive timed out")
                if failure == "late_partial":
                    return b'{"v":'
                if failure == "late_eof":
                    return b""
                if failure == "late_oversized":
                    return b"x" * (bridge_module._MAX_FRAME + 1)
                if failure == "late_malformed":
                    return b"invalid\n"
                if failure == "late_multiframe":
                    return b"{}\n{}\n"
                # A completed reply at the deadline must not become success.
                data.update(state="completed", result={})
                if failure == "late_foreign_task":
                    data["task_id"] = "T-foreign"
                if failure == "late_foreign_run":
                    data["run_id"] = "R-foreign"
            reply = {"v": 1, "request_id": request["request_id"], "ok": True, "data": data}
            if request["op"] == "status" and failure == "late_envelope":
                reply["request_id"] += 1
            if request["op"] == "status" and failure == "late_nodata":
                reply["data"] = None
            if request["op"] == "status" and failure in {"late_rejection", "late_bad_error"}:
                reply = {"v": 1, "request_id": request["request_id"], "ok": False,
                    "error": {"code": "authorization_error", "message": "rejected", "details": {}}}
                if failure == "late_bad_error":
                    reply["error"]["details"] = []
            return json.dumps(reply).encode() + b"\n"

        def shutdown(self, how):
            pass

        def close(self):
            self.closed = True
            if failure == "early_timeout_cleanup_delay":
                clock.now = 2.0

    channel = Channel()
    if failure == "early_timeout_diagnostic_delay":
        def delayed_diagnostic(**kwargs):
            channel.events.append(("diagnostic", clock.now))
            clock.now = 2.0
        monkeypatch.setattr(bridge_module, "_emit_bridge_io_failure", delayed_diagnostic)
    bridge = bridge_module.ChildBridge(channel)
    monkeypatch.setattr(bridge_module, "_bridge", bridge)
    kwargs = dict(inputs={"message": "hello"}, child_key="child", wait=True,
                  timeout_seconds=2, poll_seconds=0.01)
    if failure in {"late_foreign_task", "late_foreign_run", "late_nodata"}:
        with pytest.raises(astrid.CapabilityInvocationError, match="foreign task"):
            call(root, **kwargs)
    else:
        result = call(root, **kwargs)
        expected = "task_wait_timeout" if failure in {"late_reply", "late_timeout", "late_partial"} else "task_status_unavailable"
        assert not result.ok and result.error["code"] == expected, channel.events
        if failure == "late_rejection":
            assert result.error["details"]["code"] == "authorization_error"
    assert [item["op"] for item in channel.requests] == ["submit", "status"]
    assert channel.closed == (failure != "late_rejection")


@pytest.mark.parametrize("budget", [0, 2])
def test_child_bridge_lock_timeout_preserves_inflight_channel(monkeypatch, budget):
    clock = SimpleNamespace(now=0.0)
    monkeypatch.setattr(bridge_module, "time", SimpleNamespace(monotonic=lambda: clock.now))

    class OwnedLock:
        def __init__(self):
            self.budgets = []

        def acquire(self, *, timeout):
            self.budgets.append(timeout)
            clock.now += timeout
            return False

        def release(self):
            pytest.fail("another request owns the lock")

    channel = SimpleNamespace(shutdown=lambda *a: pytest.fail("inflight channel shutdown"),
                              close=lambda: pytest.fail("inflight channel closed"))
    bridge = bridge_module.ChildBridge(channel)
    lock = OwnedLock()
    bridge._lock = lock
    with pytest.raises(bridge_module._BridgeTimeout) as caught:
        bridge.status("child", "T-child", timeout_seconds=budget)
    assert isinstance(caught.value.__cause__, TimeoutError)
    assert caught.value.observed_at == budget
    assert lock.budgets == ([budget] if budget else [])
    assert not bridge._closed and bridge._sequence == 0


def snapshot_receipt(**changes):
    return {"association_id": "association-1", "receipt_id": "receipt-1", "revision": 1,
            "output_port": "state_result", "digest": "sha256:" + "a" * 64,
            "size": 123, "durability": "durable", **changes}


@pytest.fixture
def snapshot_channel():
    local, host = socket.socketpair()
    bridge = bridge_module.ChildBridge(local, io_timeout_seconds=0.2)
    requests, failures = [], []
    response = {"ok": True, "data": snapshot_receipt()}

    def serve():
        try:
            with host.makefile("rb") as stream:
                while frame := stream.readline():
                    request = json.loads(frame)
                    requests.append(request)
                    host.sendall(json.dumps({"v": 1, "request_id": request["request_id"],
                                             **response}).encode() + b"\n")
        except (OSError, ValueError) as exc:
            failures.append(type(exc).__name__)

    worker = threading.Thread(target=serve, daemon=True)
    worker.start()
    try:
        yield bridge, requests, response
    finally:
        bridge.close()
        host.close()
        worker.join(timeout=1)
        assert not worker.is_alive()
        assert not failures


@pytest.mark.parametrize("filename", ["state.json", "snapshots/revision-1.json", "./snapshots/state.json"])
@pytest.mark.parametrize("size", [0, 123])
def test_snapshot_exact_request_and_durable_receipt(snapshot_channel, filename, size):
    bridge, requests, response = snapshot_channel
    response["data"] = snapshot_receipt(size=size)
    result = bridge.publish_snapshot(filename=filename, output_port="state_result", revision=1)
    assert result == response["data"]
    assert set(result) == {"association_id", "receipt_id", "revision", "output_port", "digest", "size", "durability"}
    assert requests == [{"v": 1, "request_id": 1, "op": "publish_snapshot", "filename": filename,
                         "output_port": "state_result", "revision": 1}]
    response["data"] = snapshot_receipt(revision=2, output_port="other_state")
    assert bridge.publish_snapshot(filename="snapshots/revision-2.json", output_port="other_state",
                                   revision=2) == response["data"]
    assert requests[1] == {"v": 1, "request_id": 2, "op": "publish_snapshot",
                           "filename": "snapshots/revision-2.json", "output_port": "other_state", "revision": 2}


@pytest.mark.parametrize("field,value", [
    ("filename", value) for value in (None, False, 1, "", ".", "./", "././", "/state.json",
                                      "//state.json", "../state.json", "snapshots/../state.json",
                                      "snapshots/..", "snapshots\\state.json", "state\x00.json")
] + [("output_port", value) for value in (None, False, 1, "")]
  + [("revision", value) for value in (None, True, False, 0, -1, 1.0, "1")])
def test_snapshot_invalid_request_never_reaches_host(snapshot_channel, field, value):
    bridge, requests, _ = snapshot_channel
    request = {"filename": "state.json", "output_port": "state_result", "revision": 1, field: value}
    with pytest.raises(astrid.CapabilityValidationError):
        bridge.publish_snapshot(**request)
    assert not requests and not bridge._closed
    # A successful exchange orders the host reader after any possible invalid transmission.
    assert bridge.publish_snapshot(filename="state.json", output_port="state_result", revision=1) == snapshot_receipt()
    assert len(requests) == 1 and requests[0]["request_id"] == 1


@pytest.mark.parametrize("field,value", [("filename", "x" * 1024 * 1024),
                                         ("output_port", "x" * 1024 * 1024),
                                         ("filename", "state\ud800.json")])
def test_snapshot_strict_request_bounds_precede_transmission(snapshot_channel, field, value):
    bridge, requests, _ = snapshot_channel
    with pytest.raises(astrid.CapabilityValidationError):
        bridge.publish_snapshot(**{"filename": "state.json", "output_port": "state_result",
                                   "revision": 1, field: value})
    assert not requests and not bridge._closed
    bridge.publish_snapshot(filename="state.json", output_port="state_result", revision=1)
    assert len(requests) == 1


@pytest.mark.parametrize("receipt", [None, [], "durable", {},
    *[{key: value for key, value in snapshot_receipt().items() if key != missing}
      for missing in snapshot_receipt()],
    *[snapshot_receipt(**{extra: "forged"}) for extra in
      ("authority", "credentials", "task_id", "attempt_id", "lease", "fence", "epoch", "provenance")],
    *[snapshot_receipt(**{field: value}) for field, values in (
        ("association_id", ("", None, False, 123)),
        ("receipt_id", ("", None, False, 123)),
        ("revision", (0, -1, 2, True, 1.0, "1")),
        ("output_port", ("", None, False, "foreign_port")),
        ("digest", (None, "", "sha256:" + "A" * 64, "sha256:" + "g" * 64,
                    "sha256:" + "a" * 63, "sha256:" + "a" * 65, "a" * 64,
                    "sha256:" + "a" * 64 + "\n")),
        ("size", (-1, True, False, 1.0, "123", None)),
        ("durability", (None, False, "pending", "volatile", "DURABLE")),
    ) for value in values],
])
def test_snapshot_invalid_receipt_revokes_without_acknowledgment(snapshot_channel, receipt):
    bridge, requests, response = snapshot_channel
    response["data"] = receipt
    with pytest.raises(astrid.CapabilityInvocationError, match="invalid reply"):
        bridge.publish_snapshot(filename="state.json", output_port="state_result", revision=1)
    assert bridge._closed and len(requests) == 1
    with pytest.raises(astrid.CapabilityInvocationError, match="closed"):
        bridge.publish_snapshot(filename="state.json", output_port="state_result", revision=1)
    with pytest.raises(astrid.CapabilityInvocationError, match="closed"):
        bridge.submit({"child_key": "after-invalid-receipt"})
    assert len(requests) == 1


def test_snapshot_explicit_host_rejection_is_not_a_saved_result(snapshot_channel):
    bridge, requests, response = snapshot_channel
    error = {"code": "authorization_error", "message": "snapshot outside grant", "details": {}}
    response.clear()
    response.update(ok=False, error=error)
    with pytest.raises(astrid.CapabilityInvocationError) as raised:
        bridge.publish_snapshot(filename="state.json", output_port="state_result", revision=1)
    assert raised.value.details == error and len(requests) == 1


def test_snapshot_unresponsive_channel_revokes_without_retry():
    local, host = socket.socketpair()
    bridge = bridge_module.ChildBridge(local, io_timeout_seconds=0.03)
    try:
        with pytest.raises(astrid.CapabilityInvocationError):
            bridge.publish_snapshot(filename="state.json", output_port="state_result", revision=1)
        assert bridge._closed
        with pytest.raises(astrid.CapabilityInvocationError, match="closed"):
            bridge.publish_snapshot(filename="state.json", output_port="state_result", revision=1)
        with host.makefile("rb") as stream:
            assert json.loads(stream.readline())["op"] == "publish_snapshot"
            assert stream.readline() == b""
    finally:
        bridge.close()
        host.close()


def child_output(key="materialize-child", **changes):
    return {"association_id": "association-1", "run_id": "R-" + key,
            "task_id": "T-" + key, "attempt_id": "A-" + key,
            "object_id": "sha256:" + "a" * 64, "digest": "sha256:" + "a" * 64,
            "size": 123, "filename": "child/audio.wav", "media_type": "audio/wav",
            "output_port": "audio", "ordinal": 0, **changes}


@pytest.fixture
def materialization_channel(monkeypatch):
    # Socket fixtures prove the SDK contract only: no bytes are read or written.
    local, host = socket.socketpair()
    bridge = bridge_module.ChildBridge(local, io_timeout_seconds=2.0)
    monkeypatch.setattr(bridge_module, "_bridge", bridge)
    requests, failures, controls = [], [], {}

    def serve():
        try:
            with host.makefile("rb") as stream:
                while frame := stream.readline():
                    request = json.loads(frame)
                    requests.append(request)
                    controls.setdefault("raw_requests", []).append(frame)
                    key = request["child_key"]
                    output = child_output(key, **controls.get("output_changes", {}))
                    if request["op"] == "outputs":
                        data = controls.get("outputs", [output])
                    elif request["op"] == "materialize_output":
                        data = controls.get("materialized", {"output": output,
                            "filename": "child-outputs/association-1/audio.wav"})
                    else:
                        data = {"run_id": "R-" + key, "task_id": "T-" + key,
                                "attempt_id": "A-" + key}
                        if request["op"] == "status":
                            data.update(state=controls.get("state", "completed"), result={})
                    reply = {"v": 1, "request_id": request["request_id"], "ok": True, "data": data}
                    if request["op"] == "materialize_output":
                        reply.update(controls.get("envelope", {}))
                        if "error" in controls:
                            reply.pop("data")
                            reply.update(ok=False, error=controls["error"])
                    encoded = json.dumps(reply, separators=(",", ":")).encode() + b"\n"
                    if request["op"] == "materialize_output" and "raw_reply" in controls:
                        encoded = controls["raw_reply"]
                    host.sendall(encoded)
        except ConnectionResetError as exc:
            # A reply the SDK rejects revokes the channel. Closing a Unix socket
            # that still holds unread bytes (the tail of an oversized frame)
            # resets the peer's next read on Linux; that is the revoke itself,
            # not a host fault. A reset on a live bridge still fails the test.
            if not bridge._closed:
                failures.append(type(exc).__name__)
        except (OSError, ValueError) as exc:
            failures.append(type(exc).__name__)

    worker = threading.Thread(target=serve, daemon=True)
    worker.start()
    try:
        yield bridge, requests, controls
    finally:
        bridge.close()
        host.close()
        worker.join(timeout=1)
        assert not worker.is_alive()
        assert not failures


def materializable(root, **kwargs):
    return call(root, inputs={"message": "hello"}, wait=True,
                timeout_seconds=0.5, poll_seconds=0.01, **kwargs)


@pytest.mark.parametrize("child_key", [None, "materialize-child"])
@pytest.mark.usefixtures("isolated")
def test_materialize_origin_request_typed_result_repeats_and_serialization(
        tmp_path, materialization_channel, child_key):
    from astrid.sdk import MaterializedChildOutput

    root = write_pack(tmp_path)
    bridge, messages, controls = materialization_channel
    result = materializable(root, child_key=child_key)
    key = messages[0]["child_key"]
    if child_key is None:
        assert key.startswith("sdk-child-")
    before = json.dumps(result.to_dict(), sort_keys=True)
    materialized = result.materialize_output("association-1")
    assert isinstance(materialized, MaterializedChildOutput)
    assert materialized.to_dict() == {"output": child_output(key),
        "filename": "child-outputs/association-1/audio.wav"}
    assert materialized.producer_file() == {"filename": materialized.filename,
        "media_type": "audio/wav", "output_port": "audio"}
    assert materialized.output["filename"] == "child/audio.wav"
    with pytest.raises(TypeError):
        materialized.output["task_id"] = "T-foreign"
    assert result.materialize_output("association-1") == materialized
    assert messages[3:] == [{"v": 1, "request_id": number, "op": "materialize_output",
        "child_key": key, "task_id": result.kernel_task_id, "association_id": "association-1"}
        for number in (4, 5)]
    assert controls["raw_requests"][3] == bridge_module._strict_json(messages[3]) + b"\n"
    assert json.loads(bridge_module._strict_json(materialized.to_dict())) == materialized.to_dict()
    assert json.dumps(result.to_dict(), sort_keys=True) == before
    assert "_child_output_binding" not in result.to_dict()
    assert "_child_output_binding" not in repr(result)
    assert key not in json.dumps({k: v for k, v in result.to_dict().items()
                                if k not in {"raw_result", "run_id", "kernel_run_id",
                                             "kernel_task_id", "kernel_attempt_id", "outputs"}})
    assert bridge._closed is False


@pytest.mark.usefixtures("isolated")
def test_materialize_association_is_bound_to_its_own_successful_child(tmp_path,
                                                                  materialization_channel):
    root = write_pack(tmp_path)
    _, messages, controls = materialization_channel
    first = materializable(root, child_key="first")
    controls["output_changes"] = {"association_id": "association-2"}
    second = materializable(root, child_key="second")
    with pytest.raises(astrid.CapabilityValidationError):
        first.materialize_output(second.outputs["managed_outputs"][0]["association_id"])
    assert len(messages) == 6
    assert second.materialize_output("association-2").output == child_output(
        "second", association_id="association-2")
    controls["output_changes"] = {}
    assert first.materialize_output("association-1").output == child_output("first")
    assert messages[-2]["child_key"] == "second" and messages[-1]["child_key"] == "first"


@pytest.mark.parametrize("field,value", [("kernel_run_id", "R-foreign"),
                                         ("kernel_task_id", "T-foreign"),
                                         ("kernel_attempt_id", "A-foreign"), ("ok", False)])
@pytest.mark.usefixtures("isolated")
def test_materialize_original_identity_mutation_fails_before_request(tmp_path,
                                                                   materialization_channel, field, value):
    root = write_pack(tmp_path)
    _, messages, _ = materialization_channel
    result = materializable(root)
    object.__setattr__(result, field, value)
    with pytest.raises(astrid.CapabilityValidationError):
        result.materialize_output("association-1")
    assert len(messages) == 3


@pytest.mark.usefixtures("isolated")
def test_materialize_producer_file_uses_existing_child_input_shape(tmp_path,
                                                                  materialization_channel):
    root = write_pack(tmp_path)
    _, messages, controls = materialization_channel
    result = materializable(root)
    local_path = "child-outputs/association-1/nested/audio.wav"
    controls["materialized"] = {"output": child_output(messages[0]["child_key"]),
                                "filename": local_path}
    materialized = result.materialize_output("association-1")
    expected = {"filename": local_path, "media_type": "audio/wav", "output_port": "audio"}
    assert materialized.filename == local_path
    assert materialized.producer_file() == expected
    file_root = write_pack(tmp_path, pack_id="files", actions={"consume": {
        "description": "Consume a file.", "invocation": {
            "kind": "python", "path": "actions/echo.py", "function": "echo"},
        "inputs": [{"name": "clip", "type": "file", "required": True}]}})
    next_child = astrid.invoke("files.consume", kind="action", extra_pack_roots=(str(file_root),),
                              inputs={"clip": materialized.producer_file()})
    assert next_child.ok
    assert messages[-1]["input_descriptors"] == [{"name": "clip", "kind": "producer_file",
                                                  **expected}]
    assert messages[-1]["inputs"] == {"clip": expected}
    assert "input_object_ids" not in messages[-1]


@pytest.mark.parametrize("association", [None, True, False, 1, 1.0, [], {}, "", "foreign",
                                        "association-1\n", "bad/id", "a" * 129])
@pytest.mark.usefixtures("isolated")
def test_materialize_only_exact_association_can_reach_host(tmp_path,
                                                         materialization_channel, association):
    root = write_pack(tmp_path)
    _, messages, _ = materialization_channel
    result = materializable(root)
    with pytest.raises(astrid.CapabilityValidationError):
        result.materialize_output(association)
    assert len(messages) == 3
    assert result.materialize_output("association-1").output["association_id"] == "association-1"
    assert len(messages) == 4


@pytest.mark.usefixtures("isolated")
def test_materialize_wrong_result_and_bridge_cannot_supply_authority(tmp_path,
                                                                  materialization_channel, monkeypatch):
    from dataclasses import replace

    root = write_pack(tmp_path)
    bridge, messages, _ = materialization_channel
    result = materializable(root)
    copied = replace(result)
    for foreign in (copied, replace(result, kernel_task_id="T-foreign"),
                    replace(result, kernel_attempt_id="A-foreign"), replace(result, ok=False)):
        with pytest.raises(astrid.CapabilityValidationError):
            foreign.materialize_output("association-1")
    # Even copying the private binding fails the originating-object check.
    object.__setattr__(copied, "_child_output_binding", result._child_output_binding)
    with pytest.raises(astrid.CapabilityValidationError):
        copied.materialize_output("association-1")
    with pytest.raises(TypeError):
        result.materialize_output("association-1", task_id="T-foreign")
    with pytest.raises(TypeError):
        result.materialize_output("association-1", filename="arbitrary.wav")
    monkeypatch.setattr(bridge_module, "_bridge", None)
    # The originating bridge remains bound even when ambient discovery disappears.
    assert result.materialize_output("association-1").output["task_id"] == result.kernel_task_id
    bridge.close()
    with pytest.raises(astrid.CapabilityInvocationError, match="closed"):
        result.materialize_output("association-1")
    assert [request["op"] for request in messages] == ["submit", "status", "outputs", "materialize_output"]


@pytest.mark.parametrize("mode", ["not_waited", "failed", "empty", "non_child"])
@pytest.mark.usefixtures("isolated")
def test_materialize_unavailable_without_successful_origin(tmp_path,
                                                          materialization_channel, mode):
    from astrid.sdk import InvocationResult

    root = write_pack(tmp_path)
    _, messages, controls = materialization_channel
    if mode == "non_child":
        result = InvocationResult("ordinary.echo", "executor", "action", True,
            kernel_task_id="T-materialize-child", kernel_attempt_id="A-materialize-child",
            outputs={"managed_outputs": [child_output()]})
    elif mode == "not_waited":
        result = call(root, inputs={"message": "hello"})
    else:
        controls.update({"state": "failed"} if mode == "failed" else {"outputs": []})
        result = materializable(root)
    with pytest.raises(astrid.CapabilityValidationError):
        result.materialize_output("association-1")
    assert all(request["op"] != "materialize_output" for request in messages)


@pytest.mark.parametrize("mutation", ["missing", "duplicate", "foreign", "hostile"])
@pytest.mark.usefixtures("isolated")
def test_materialize_rejects_mutated_public_descriptors(tmp_path,
                                                      materialization_channel, mutation):
    root = write_pack(tmp_path)
    _, messages, _ = materialization_channel
    result = materializable(root)
    rows = result.outputs["managed_outputs"]
    if mutation == "missing":
        rows.clear()
    elif mutation == "duplicate":
        rows.append(dict(rows[0]))
    else:
        rows[0]["attempt_id" if mutation == "foreign" else "ordinal"] = (
            "A-foreign" if mutation == "foreign" else False)
    with pytest.raises(astrid.CapabilityValidationError):
        result.materialize_output("association-1")
    assert len(messages) == 3


_BAD_OUTPUT_CHANGES = [
    ("association_id", ""), ("association_id", None), ("association_id", False),
    ("task_id", "T-foreign"), ("run_id", "R-foreign"), ("attempt_id", "A-foreign"),
    ("digest", "sha256:" + "A" * 64), ("digest", "sha256:" + "a" * 64 + "\n"),
    ("object_id", "sha256:" + "b" * 64), ("size", -1), ("size", True),
    ("size", 1.0), ("size", 5 * 1024 ** 3 + 1), ("ordinal", False), ("ordinal", -1),
    ("filename", "../audio.wav"), ("filename", "/audio.wav"), ("filename", "x\\audio.wav"),
    ("filename", "audio\x00.wav"), ("media_type", None), ("media_type", ""),
    ("output_port", []), ("output_port", ""), ("credentials", "forged"),
]


@pytest.mark.parametrize("field,value", _BAD_OUTPUT_CHANGES)
@pytest.mark.usefixtures("isolated")
def test_materialize_invalid_initial_output_page_closes_bridge(tmp_path,
                                                              materialization_channel, field, value):
    root = write_pack(tmp_path)
    bridge, messages, controls = materialization_channel
    controls["output_changes"] = {field: value}
    result = materializable(root)
    assert not result.ok and bridge._closed
    with pytest.raises(astrid.CapabilityValidationError):
        result.materialize_output("association-1")
    assert len(messages) == 3


@pytest.mark.parametrize("page", [None, {}, [None], [child_output(), child_output()],
                                 [{k: v for k, v in child_output().items() if k != "association_id"}]])
@pytest.mark.usefixtures("isolated")
def test_materialize_missing_duplicate_or_malformed_output_pages(tmp_path,
                                                              materialization_channel, page):
    root = write_pack(tmp_path)
    bridge, _, controls = materialization_channel
    controls["outputs"] = page
    result = materializable(root, child_key="materialize-child")
    assert not result.ok and bridge._closed
    with pytest.raises(astrid.CapabilityValidationError):
        result.materialize_output("association-1")


@pytest.mark.parametrize("field,value", _BAD_OUTPUT_CHANGES + [("association_id", "foreign")])
@pytest.mark.usefixtures("isolated")
def test_materialize_descriptor_reply_must_equal_original_and_revokes(tmp_path,
                                                                   materialization_channel, field, value):
    root = write_pack(tmp_path)
    bridge, messages, controls = materialization_channel
    result = materializable(root, child_key="materialize-child")
    controls["materialized"] = {"output": child_output(**{field: value}), "filename": "safe/audio.wav"}
    with pytest.raises(astrid.CapabilityInvocationError, match="invalid reply"):
        result.materialize_output("association-1")
    assert bridge._closed
    with pytest.raises(astrid.CapabilityInvocationError, match="closed"):
        result.materialize_output("association-1")
    assert len(messages) == 4


@pytest.mark.parametrize("response", [None, [], {},
    {"filename": "safe/audio.wav"}, {"output": child_output()},
    {"output": child_output(), "filename": "safe/audio.wav", "bytes": "YWJj"},
    *[{"output": child_output(), "filename": filename} for filename in
      (None, False, 1, "", ".", "./", "../audio.wav", "/audio.wav", "x/../audio.wav",
       "x\\audio.wav", "audio\x00.wav", "audio\n.wav")],
    *[{"output": {k: v for k, v in child_output().items() if k != missing},
       "filename": "safe/audio.wav"} for missing in child_output()],
])
@pytest.mark.usefixtures("isolated")
def test_materialize_exact_reply_shape_and_relative_filename(tmp_path,
                                                           materialization_channel, response):
    root = write_pack(tmp_path)
    bridge, _, controls = materialization_channel
    result = materializable(root, child_key="materialize-child")
    controls["materialized"] = response
    with pytest.raises(astrid.CapabilityInvocationError, match="invalid reply"):
        result.materialize_output("association-1")
    assert bridge._closed


@pytest.mark.parametrize("envelope", [{"v": True}, {"request_id": True}, {"request_id": 99},
                                      {"ok": 1}, {"authority": "forged"}])
@pytest.mark.usefixtures("isolated")
def test_materialize_protocol_corruption_closes_channel(tmp_path,
                                                     materialization_channel, envelope):
    root = write_pack(tmp_path)
    bridge, _, controls = materialization_channel
    result = materializable(root)
    controls["envelope"] = envelope
    with pytest.raises(astrid.CapabilityInvocationError, match="invalid reply"):
        result.materialize_output("association-1")
    assert bridge._closed


@pytest.mark.parametrize("raw_reply", [b'{"v":1,"v":1,"request_id":4,"ok":true,"data":{}}\n',
                                      b'{}\n{}\n', b'not-json\n',
                                      b'{"padding":"' + b'a' * (1024 * 1024) + b'"}\n'])
@pytest.mark.usefixtures("isolated")
def test_materialize_malformed_json_and_frame_bound_revoke(tmp_path,
                                                         materialization_channel, raw_reply):
    root = write_pack(tmp_path)
    bridge, _, controls = materialization_channel
    result = materializable(root)
    controls["raw_reply"] = raw_reply
    with pytest.raises(astrid.CapabilityInvocationError, match="invalid reply"):
        result.materialize_output("association-1")
    assert bridge._closed


@pytest.mark.usefixtures("isolated")
def test_materialize_host_rejection_returns_no_file_and_allows_authoritative_retry(
        tmp_path, materialization_channel):
    root = write_pack(tmp_path)
    bridge, messages, controls = materialization_channel
    result = materializable(root)
    error = {"code": "authorization_error", "message": "association unavailable", "details": {}}
    controls["error"] = error
    with pytest.raises(astrid.CapabilityInvocationError) as raised:
        result.materialize_output("association-1")
    assert raised.value.details == error and not bridge._closed
    controls.pop("error")
    assert result.materialize_output("association-1").filename == "child-outputs/association-1/audio.wav"
    assert len(messages) == 5


_DISCOVERY_LIMITS = {
    "max_discovery_rows": 750, "max_discovery_metadata_bytes": 67108864,
    "max_selected_output_objects": 2, "max_selected_output_bytes": 16777216,
    "max_child_media_bindings": 1, "max_child_media_bytes": 16777216,
}


def discovery_policy(**grant_changes):
    # Root identity is independent of the permitted Human Review child pin.
    grant = {"project_id": "test", "run_id": "historical-run", "task_id": "historical-task",
             "attempt_id": "historical-attempt", "capability_id": "ordinary.echo",
             "capability_digest": "sha256:" + "b" * 64, "limits": dict(_DISCOVERY_LIMITS), **grant_changes}
    return {**recoverable_policy(), "discovery_grant": grant}


@pytest.mark.parametrize("entry_point", ["public", "kernel"])
def test_discovery_grant_public_to_generated_admission_unchanged(tmp_path, isolated, monkeypatch,
                                                                generated_admission, entry_point):
    root = write_pack(tmp_path)
    client, requests = generated_admission
    policy = discovery_policy()
    original = json.dumps(policy, sort_keys=True, separators=(",", ":"))
    validations = []
    validate = bridge_module._validate_child_policy
    def checked(value):
        validations.append(json.loads(json.dumps(value)))
        return validate(value)
    monkeypatch.setattr(bridge_module, "_validate_child_policy", checked)
    result = invoke_parent_policy(root, policy, client, entry_point, project="test")
    if entry_point == "public":
        assert result.ok and result.kernel_task_id == "T"
    else:
        assert result[:3] == ("R", "T", "A") and result[5] is True
    assert validations == [policy] * (2 if entry_point == "public" else 1)
    assert len(requests) == 1
    assert json.dumps(requests[0]["child_delegation"], sort_keys=True, separators=(",", ":")) == original
    assert json.dumps(policy, sort_keys=True, separators=(",", ":")) == original
    assert requests[0]["spec"]["inputs"] == {"message": "hello"}
    assert not {"child_delegation", "discovery_grant", "recoverable_outputs"} & set(requests[0]["spec"])
    assert requests[0]["child_delegation"]["recoverable_outputs"] == recoverable_policy()["recoverable_outputs"]


@pytest.mark.parametrize("entry_point", ["public", "kernel"])
@pytest.mark.parametrize("bad", [None, False, {}, [], "target", [discovery_policy()["discovery_grant"]],
    *[{key: value for key, value in discovery_policy()["discovery_grant"].items() if key != missing}
      for missing in ("project_id", "run_id", "task_id", "attempt_id", "capability_id", "capability_digest", "limits")],
    {**discovery_policy()["discovery_grant"], "authority": "forged"},
    {**discovery_policy()["discovery_grant"], "parent_attempt_id": "forged"},
    *[{**discovery_policy()["discovery_grant"], field: value}
      for field in ("project_id", "run_id", "task_id", "attempt_id", "capability_id")
      for value in (None, False, "", "*", "target-*", ["target"])],
    *[{**discovery_policy()["discovery_grant"], "capability_digest": value}
      for value in (None, False, "", "b" * 64, "sha256:" + "B" * 64)],
    {**discovery_policy()["discovery_grant"], "limits": {}},
    {**discovery_policy()["discovery_grant"], "limits": {**_DISCOVERY_LIMITS, "extra": 1}},
])
def test_discovery_grant_malformed_shape_rejected_before_admission(tmp_path, isolated,
                                                                  generated_admission, entry_point, bad):
    root = write_pack(tmp_path)
    client, requests = generated_admission
    policy = {**recoverable_policy(), "discovery_grant": bad}
    with pytest.raises(astrid.CapabilityValidationError, match="discovery_grant"):
        invoke_parent_policy(root, policy, client, entry_point, project="test")
    assert requests == []


@pytest.mark.parametrize("field", list(_DISCOVERY_LIMITS))
@pytest.mark.parametrize("bad", [None, False, True, 0, -1, 1.5, "1", "missing", "excessive"])
def test_discovery_grant_requires_every_strict_finite_bound(tmp_path, isolated, generated_admission, field, bad):
    root = write_pack(tmp_path)
    client, requests = generated_admission
    policy = discovery_policy()
    limits = policy["discovery_grant"]["limits"]
    if bad == "missing":
        del limits[field]
    else:
        limits[field] = _DISCOVERY_LIMITS[field] + 1 if bad == "excessive" else bad
    with pytest.raises(astrid.CapabilityValidationError, match="discovery_grant.limits"):
        invoke_parent_policy(root, policy, client, "public", project="test")
    assert requests == []


def test_discovery_grant_caps_copy_and_independent_root_pin():
    assert bridge_module._DISCOVERY_GRANT_CEILINGS == _DISCOVERY_LIMITS
    policy = discovery_policy(limits={name: 1 for name in _DISCOVERY_LIMITS})
    normalized = bridge_module._validate_child_policy(policy)
    assert normalized == policy
    assert normalized is not policy
    assert normalized["discovery_grant"] is not policy["discovery_grant"]
    assert normalized["discovery_grant"]["limits"] is not policy["discovery_grant"]["limits"]
    normalized["discovery_grant"]["limits"]["max_discovery_rows"] = 2
    assert policy["discovery_grant"]["limits"]["max_discovery_rows"] == 1
    assert policy["discovery_grant"]["capability_id"] not in [item["capability_id"] for item in policy["capabilities"]]


@pytest.mark.parametrize("explicit_key", [False, True])
def test_discovery_grant_valid_policy_rejected_in_child_mode(tmp_path, isolated, monkeypatch,
                                                           channel, explicit_key):
    root = write_pack(tmp_path)
    _, messages, _ = channel
    monkeypatch.setattr(invocation, "_kernel_invoke", lambda *a, **k: pytest.fail("normal admission"))
    policy = discovery_policy()
    assert bridge_module._validate_child_policy(policy) == policy
    with pytest.raises(astrid.CapabilityValidationError, match="child invocation cannot set child_delegation"):
        call(root, inputs={"message": "hello"}, child_delegation=policy,
             **({"child_key": "explicit"} if explicit_key else {}))
    assert messages == []


def test_discovery_grant_valid_policy_rejected_with_child_key_without_bridge(monkeypatch):
    monkeypatch.setattr(bridge_module, "_bridge", None)
    with pytest.raises(astrid.CapabilityValidationError, match="child invocation cannot set child_delegation"):
        astrid.invoke("absent", kind="action", child_key="explicit", child_delegation=discovery_policy())



@pytest.fixture
def private_nonblocking_diagnostic_sink():
    """Install the owned PIPE during the call, after pytest capture setup."""
    from contextlib import contextmanager
    import fcntl
    import os

    @contextmanager
    def scope():
        reader, writer = os.pipe()
        saved = os.dup(2)
        os.dup2(writer, 2)
        # Ordinary warm-up write sets Darwin's kernel FWASWRITTEN bit.
        # It is not a caller-settable flag and F_SETFL cannot clear it.
        os.write(writer, b"x")
        assert os.read(reader, 1) == b"x"
        flags = fcntl.fcntl(2, fcntl.F_GETFL)
        fcntl.fcntl(reader, fcntl.F_SETFL, fcntl.fcntl(reader, fcntl.F_GETFL) | os.O_NONBLOCK)

        def captured():
            assert fcntl.fcntl(2, fcntl.F_GETFL) == flags
            chunks = []
            while True:
                try:
                    chunk = os.read(reader, 65536)
                except BlockingIOError:
                    break
                if not chunk:
                    break
                chunks.append(chunk)
            return b"".join(chunks).decode("ascii")

        try:
            yield captured
            assert fcntl.fcntl(2, fcntl.F_GETFL) == flags
        finally:
            os.dup2(saved, 2)
            os.close(saved)
            os.close(writer)
            os.close(reader)

    return scope


@pytest.mark.parametrize("failure,phase,exception_class", [
    ("timeout", "receive", "TimeoutError"),
    ("decode", "decode", "ValueError"),
    ("envelope", "envelope", "ValueError"),
    ("data", "data", "ValueError"),
])
@pytest.mark.posix_native
def test_bridge_failure_diagnostic_is_redacted_and_preserves_revocation(
        failure, phase, exception_class, capsys, private_nonblocking_diagnostic_sink):
    with private_nonblocking_diagnostic_sink() as private_nonblocking_diagnostic_sink:
        from astrid.sdk.exceptions import CapabilityInvocationError

        local, host = socket.socketpair()
        bridge = bridge_module.ChildBridge(local, io_timeout_seconds=0.05)
        done = threading.Event()
        errors = []
        secret = "token-private-path-payload-must-not-be-logged"

        def reply():
            try:
                with host.makefile("rb") as stream:
                    request = json.loads(stream.readline())
                    if failure == "timeout":
                        assert done.wait(1)
                        return
                    if failure == "decode":
                        frame = (secret + "\n").encode()
                    elif failure == "envelope":
                        frame = json.dumps({"secret": secret}).encode() + b"\n"
                    else:
                        frame = json.dumps({"v": 1, "request_id": request["request_id"],
                                            "ok": True, "data": [secret]}).encode() + b"\n"
                    host.sendall(frame)
            except Exception as exc:
                errors.append(type(exc))

        worker = threading.Thread(target=reply)
        worker.start()
        try:
            with pytest.raises(CapabilityInvocationError, match="unavailable or returned an invalid reply") as caught:
                bridge.outputs("child", "task")
            assert isinstance(caught.value.__cause__, (TimeoutError, ValueError))
            assert bridge._closed and local.fileno() == -1
            with pytest.raises(CapabilityInvocationError, match="closed"):
                bridge.outputs("child", "task")
        finally:
            done.set()
            worker.join(timeout=1)
            host.close()
            bridge.close()
        assert not worker.is_alive() and not errors
        captured = capsys.readouterr()
        diagnostic = private_nonblocking_diagnostic_sink()
        assert captured.out == "" and secret not in diagnostic
        row, = [json.loads(line) for line in diagnostic.splitlines()]
        assert row["diagnostic"] == "astrid.child_bridge_io_failure"
        assert (row["endpoint"], row["phase"], row["exception_class"], row["operation"]) == (
            "sdk", phase, exception_class, "outputs")
        assert row["request_sequence"] == 1 and row["budget_seconds"] == 0.05
        assert row["elapsed_seconds"] >= 0 and type(row["frame_bytes"]) is int
        assert set(row) == {"diagnostic", "endpoint", "phase", "exception_class", "operation", "errno",
                            "request_sequence", "frame_bytes", "elapsed_seconds", "budget_seconds",
                            "dispatch_elapsed_seconds"}


@pytest.mark.posix_native
def test_bridge_failure_diagnostic_never_emits_custom_class_or_untrusted_operation(private_nonblocking_diagnostic_sink):
    with private_nonblocking_diagnostic_sink() as private_nonblocking_diagnostic_sink:
        class PrivateTokenCustomError(OSError):
            pass

        bridge_module._emit_bridge_io_failure(
            endpoint="sdk", phase="send", operation="secret-operation-not-admitted",
            exception=PrivateTokenCustomError(32, "secret-message-private-path"),
            request_sequence=7, frame_bytes=13, elapsed_seconds=0.1, budget_seconds=5.0)
        diagnostic = private_nonblocking_diagnostic_sink()
        assert "PrivateToken" not in diagnostic and "secret" not in diagnostic
        row = json.loads(diagnostic)
        assert row["exception_class"] == "OSError" and row["errno"] == 32
        assert row["operation"] == "unknown" and row["phase"] == "send"


@pytest.mark.posix_native
def test_bridge_diagnostic_sink_failure_cannot_mask_error_or_skip_close(monkeypatch, private_nonblocking_diagnostic_sink):
    with private_nonblocking_diagnostic_sink() as private_nonblocking_diagnostic_sink:
        from astrid.sdk.exceptions import CapabilityInvocationError

        class PrivateTokenTimeout(TimeoutError):
            pass

        class FailedChannel:
            def __init__(self):
                self.closed = self.shutdown_called = False
            def settimeout(self, value):
                pass
            def sendall(self, data):
                raise PrivateTokenTimeout("secret-message-private-path")
            def shutdown(self, how):
                self.shutdown_called = True
            def close(self):
                self.closed = True

        original_write = bridge_module.os.write

        def broken_sink(fd, frame):
            if fd == 2:
                raise OSError("secret-sink-error")
            return original_write(fd, frame)

        channel = FailedChannel()
        bridge = bridge_module.ChildBridge(channel)
        with monkeypatch.context() as patch:
            patch.setattr(bridge_module.os, "write", broken_sink)
            with pytest.raises(CapabilityInvocationError, match="unavailable") as caught:
                bridge.submit({})
        assert type(caught.value.__cause__) is PrivateTokenTimeout
        assert bridge._closed and channel.closed and channel.shutdown_called
        assert bridge._lock.acquire(blocking=False)
        bridge._lock.release()


@pytest.mark.parametrize("concurrent_writer", [False, True])
@pytest.mark.posix_native
def test_full_stderr_pipe_diagnostic_never_blocks_cleanup_and_restores_flags(monkeypatch, concurrent_writer):
    import fcntl
    import os
    import time
    from astrid.sdk.exceptions import CapabilityInvocationError

    reader, writer = os.pipe()
    saved = os.dup(2)
    local, peer = socket.socketpair()
    peer.close()
    bridge = bridge_module.ChildBridge(local)
    os.dup2(writer, 2)
    saved_flags = fcntl.fcntl(2, fcntl.F_GETFL)
    original_write = os.write
    entered = threading.Event()
    observed = threading.Event()
    writer_outcome = []
    worker = None
    try:
        fcntl.fcntl(2, fcntl.F_SETFL, saved_flags | os.O_NONBLOCK)
        while True:
            try:
                original_write(2, b"x" * 512)
            except BlockingIOError:
                break
        fcntl.fcntl(2, fcntl.F_SETFL, saved_flags)
        # The fill itself sets kernel FWASWRITTEN; snapshot the exact flags
        # immediately BEFORE the diagnostic rather than before filling.
        saved_flags = fcntl.fcntl(2, fcntl.F_GETFL)

        def concurrent():
            assert entered.wait(1)
            try:
                original_write(2, b"other-writer")
            except BlockingIOError:
                writer_outcome.append("temporarily-EAGAIN")
            finally:
                observed.set()

        def checked_write(fd, frame):
            if fd == 2:
                assert bridge._closed and local.fileno() == -1
                assert len(frame) <= 512
                assert fcntl.fcntl(fd, fcntl.F_GETFL) & os.O_NONBLOCK
                if concurrent_writer:
                    entered.set()
                    assert observed.wait(1)
            return original_write(fd, frame)

        if concurrent_writer:
            worker = threading.Thread(target=concurrent)
            worker.start()
        with monkeypatch.context() as patch:
            patch.setattr(bridge_module.os, "write", checked_write)
            with pytest.raises(CapabilityInvocationError, match="unavailable") as caught:
                bridge.submit({})
        assert isinstance(caught.value.__cause__, OSError)
        assert bridge._closed and local.fileno() == -1
        assert fcntl.fcntl(2, fcntl.F_GETFL) == saved_flags
        assert bridge._lock.acquire(blocking=False)
        bridge._lock.release()
        if concurrent_writer:
            assert writer_outcome == ["temporarily-EAGAIN"]
    finally:
        entered.set()
        if worker is not None:
            worker.join(timeout=1)
            assert not worker.is_alive()
        os.dup2(saved, 2)
        os.close(saved)
        os.close(reader)
        os.close(writer)
        bridge.close()


def test_regular_stderr_file_diagnostic_follows_close_and_never_masks_error(tmp_path, monkeypatch):
    import os
    from astrid.sdk.exceptions import CapabilityInvocationError

    saved = os.dup(2)
    local, peer = socket.socketpair()
    peer.close()
    bridge = bridge_module.ChildBridge(local)
    path = tmp_path / "private-diagnostic.jsonl"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    original_write = os.write
    writes = []

    def checked_write(target, frame):
        if target == 2:
            assert bridge._closed and local.fileno() == -1
            assert len(frame) <= 512
            writes.append(len(frame))
        return original_write(target, frame)

    try:
        os.dup2(fd, 2)
        with monkeypatch.context() as patch:
            patch.setattr(bridge_module.os, "write", checked_write)
            with pytest.raises(CapabilityInvocationError, match="unavailable"):
                bridge.submit({})
        assert len(writes) == 1
        row = json.loads(path.read_text())
        assert row["endpoint"] == "sdk" and row["phase"] == "send"
        assert path.stat().st_mode & 0o777 == 0o600
    finally:
        os.dup2(saved, 2)
        os.close(saved)
        os.close(fd)
        bridge.close()
