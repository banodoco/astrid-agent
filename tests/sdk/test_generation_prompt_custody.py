"""Authenticated prompt custody across the real invoke/submit/worker boundaries."""
from __future__ import annotations

import copy
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import astrid
import astrid.sdk as sdk
from astrid.core.execution import _child_bridge as host_bridge
from astrid.core.execution.generic_host import GenericPackHost
from astrid.core.foundation.hash import executor_definition_digest
from astrid.sdk import _child_bridge, invocation
from tests.test_generic_host import _write_manifest
from tests.sdk.test_generation_byte_preflight import (
    cached_route_discovery, discovered_registries, registry,
)


ORIGINAL = b'{"prompt":"authenticated paper boat","seed":29}\n'
REPLACEMENT = b'{"prompt":"replacement fire engine","seed":91}\n'


def _oid(data):
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _inputs(prompts_file, **controls):
    return {"model": "z-image", "mode": "t2i", "execution": "cloud",
            "prompts_file": prompts_file, **controls}


@pytest.fixture
def custody(monkeypatch, tmp_path, cached_route_discovery):
    """Stub Runtime authority only; retain the SDK, submit, snapshot and parser."""
    definition = cached_route_discovery[0].get("generation.generate_image")
    record = SimpleNamespace(id=definition.id, definition=definition,
        capability_digest="sha256:" + executor_definition_digest(definition))
    events, objects, requests = [], {}, []
    parent = {"task_id": "parent-task", "run_id": "parent-run", "project_id": "P",
        "state": "running", "attempt_id": "parent-attempt", "runtime_epoch": 1,
        "lease_expires_at": "2099-01-01T00:00:00Z", "spec": {"child_delegation": {
            "capabilities": [{"capability_id": record.id,
                              "capability_digest": record.capability_digest}],
            "input_object_ids": [], "limits": {
                "max_children": 4, "max_child_inputs": 8, "max_child_bytes": 65536,
                "max_derived_bytes": 65536, "max_derived_objects": 8}}}}

    def get_object(digest):
        events.append(("read", digest))
        return objects[digest.removeprefix("sha256:")]

    def upload(data, *, descriptor, **context):
        events.append(("upload", data, copy.deepcopy(descriptor)))
        assert descriptor["object_id"] == _oid(data)
        objects[descriptor["object_id"].removeprefix("sha256:")] = data

    def admit(*, child, inputs, ordered_inputs, derived_inputs, **context):
        events.append(("admit", copy.deepcopy(inputs), copy.deepcopy(ordered_inputs)))
        return {"task_id": "child-task", "run_id": "child-run", "state": "queued",
            "capability_id": child["capability_id"], "capability_digest": child["capability_digest"],
            "idempotency_key": child["child_id"], "spec": {"delegated_parent": {
                "parent_task_id": "parent-task", "parent_attempt_id": "parent-attempt",
                "parent_lease_id": "parent-lease", "parent_fence": 1,
                "runtime_epoch": 1, "project_id": "P", "executor_id": "worker"}}}

    client = SimpleNamespace(task=lambda task_id: copy.deepcopy(parent),
        get_object=get_object, upload_child_input=upload, admit_child=admit)
    trusted_root = tmp_path / "trusted-project"
    trusted_root.mkdir()
    host = SimpleNamespace(client=client, pack_roots=(trusted_root,), executor_id="worker",
        capabilities={record.id: record}, admit=lambda *args: (record, {}))
    monkeypatch.setattr(host_bridge, "_verify_admitted_source", lambda admission: None)
    attempt = tmp_path / "parent-attempt"
    attempt.mkdir()
    bridge = host_bridge.HostChildBridge(host, parent, attempt_id="parent-attempt",
        lease_id="parent-lease", fence=1, runtime_epoch=1, output_root=attempt / "outputs",
        cancelled=lambda: False)

    class LocalSubmit:
        def submit(self, request):
            requests.append(copy.deepcopy(request))
            return bridge.dispatch({"v": 1, "request_id": len(requests), "op": "submit", **request})

    monkeypatch.setattr(_child_bridge, "_bridge", LocalSubmit())
    f = SimpleNamespace(host=host, bridge=bridge, client=client, parent=parent,
        events=events, objects=objects, requests=requests, root=trusted_root)
    yield f
    bridge.revoke()


def _producer(f, content=ORIGINAL):
    path = f.bridge.output_root / "prompts.jsonl"
    path.write_bytes(content)
    return path, {"filename": path.name, "output_port": "prompts", "media_type": "application/jsonl"}


def _execute_snapshot(tmp_path, client, oid):
    """Real CAS materialization, inherited FD launch and generation prompt loader."""
    root = tmp_path / "loader-pack"
    manifest_path = _write_manifest(root)
    manifest = json.loads(manifest_path.read_text())
    manifest["inputs"] = [{"name": "prompts_file", "type": "file", "required": True}]
    manifest["command"]["argv"] = ["{python_exec}", "-c",
        "import json; from pathlib import Path; import astrid.sdk.invocation as inv; "
        "from astrid.packs.generation.shared._common import _load_prompts; "
        "rows=_load_prompts(Path('{prompts_file}'), 'z-image', 't2i'); "
        "Path('{out}/answer.txt').write_text(json.dumps(dict(rows=rows, origin=inv.__file__)))"]
    manifest_path.write_text(json.dumps(manifest))
    host = GenericPackHost(pack_roots=[root], client=client)
    host.discover()
    attempt = tmp_path / "execute-attempt"
    output = attempt / "outputs"
    output.mkdir(parents=True)
    values = host._materialize_inputs({"input_object_ids": [oid], "spec": {
        "inputs": {"prompts_file": {"object_id": oid, "filename": "prompts.jsonl"}}}},
        attempt, file_input_names=frozenset({"prompts_file"}))
    staged = Path(values["prompts_file"])
    replacement = staged.with_suffix(".replacement")
    replacement.write_bytes(REPLACEMENT)
    replacement.replace(staged)
    assert staged.read_bytes() == REPLACEMENT
    result = host._run_command_definition(host.capabilities["test.echo"], values, output, attempt)
    assert result.returncode == 0
    answer = json.loads((output / "answer.txt").read_text())
    assert answer["rows"][0]["prompt"] == "authenticated paper boat"
    assert answer["rows"][0]["seed"] == 29
    assert Path(answer["origin"]).resolve() == Path(invocation.__file__).resolve()


@pytest.mark.parametrize("source", ["producer", "object"])
def test_child_validates_authenticated_bytes_before_mutation_and_path_replacement(
    custody, monkeypatch, tmp_path, source,
):
    f = custody
    path, descriptor = _producer(f)
    if source == "object":
        f.objects[_oid(ORIGINAL).removeprefix("sha256:")] = ORIGINAL
        f.parent["spec"]["child_delegation"]["input_object_ids"].append(_oid(ORIGINAL))
        # Host retains this exact persisted policy, independently of SDK inputs.
        descriptor = {"object_id": _oid(ORIGINAL), "filename": "prompts.jsonl"}
    preflight, validated = invocation.preflight_generation_inputs, []

    def check(**kwargs):
        assert not any(event[0] in {"upload", "admit"} for event in f.events)
        assert f.bridge._children == {}
        assert kwargs["prompt_bytes"] == ORIGINAL
        result = preflight(**kwargs)
        validated.append(result.prompt_bytes)
        replacement = path.with_suffix(".replacement")
        replacement.write_bytes(REPLACEMENT)
        replacement.replace(path)
        return result

    monkeypatch.setattr(invocation, "preflight_generation_inputs", check)
    result = astrid.invoke("generation.generate_image", kind="executor",
        inputs=_inputs(descriptor, count=2, seed=17), child_key="prompt-child")
    assert result.ok
    assert validated == [ORIGINAL]
    assert path.read_bytes() == REPLACEMENT
    admitted = next(event for event in f.events if event[0] == "admit")
    assert admitted[1]["count"] == 2 and admitted[1]["seed"] == 17
    assert admitted[2][0]["object_id"] == _oid(ORIGINAL)
    if source == "producer":
        assert [event[1] for event in f.events if event[0] == "upload"] == [ORIGINAL]
    else:
        assert not any(event[0] == "upload" for event in f.events)
    _execute_snapshot(tmp_path, f.client, _oid(validated[0]))


@pytest.mark.parametrize("content", [b"not json", b'[{"seed":1}]',
    b'{"prompt":"boat","strength":0.5}\n'])
def test_invalid_child_prompts_leave_no_upload_admission_or_key_reservation(custody, content):
    f = custody
    _, descriptor = _producer(f, content)
    with pytest.raises((astrid.CapabilityValidationError, host_bridge.ChildBridgeError)):
        astrid.invoke("generation.generate_image", kind="executor",
            inputs=_inputs(descriptor), child_key="invalid-prompt")
    assert not any(event[0] in {"upload", "admit"} for event in f.events)
    assert f.bridge._children == {}


def test_every_child_file_authenticates_before_prompt_preflight(custody, monkeypatch):
    from dataclasses import replace

    f = custody
    _, descriptor = _producer(f)
    # This boundary supports arbitrary declared file ports. Add another trusted
    # file port to check that even a later binding authenticates before parsing.
    record = f.host.capabilities["generation.generate_image"]
    prompt_port = next(port for port in record.definition.inputs if port.name == "prompts_file")
    record.definition = replace(record.definition, inputs=(*record.definition.inputs,
        replace(prompt_port, name="reference_file", default=None)))
    record.capability_digest = "sha256:" + executor_definition_digest(record.definition)
    f.parent["spec"]["child_delegation"]["capabilities"][0]["capability_digest"] = record.capability_digest
    capability = SimpleNamespace(id=record.id, inputs=record.definition.inputs)
    values = _inputs(descriptor, reference_file={"object_id": _oid(b"foreign"), "filename": "ref.png"})
    request = _child_bridge._child_request(capability, values, digest=record.capability_digest,
        child_key="all-files", wait=False, timeout_seconds=1, poll_seconds=0.1)
    called = []
    monkeypatch.setattr(invocation, "preflight_generation_inputs", lambda **kwargs: called.append(kwargs))
    with pytest.raises(host_bridge.ChildBridgeError, match="custody"):
        f.bridge.dispatch({"v": 1, "request_id": 1, "op": "submit", **request})
    assert called == []
    assert not any(event[0] in {"upload", "admit"} for event in f.events)


@pytest.mark.parametrize("source", ["symlink", "foreign", "wrong-bytes"])
def test_prompt_snapshot_rejects_unconfined_or_unverified_input(custody, tmp_path, source):
    f = custody
    path, descriptor = _producer(f)
    if source == "symlink":
        outside = tmp_path / "outside.jsonl"
        outside.write_bytes(ORIGINAL)
        path.unlink()
        path.symlink_to(outside)
    else:
        descriptor = {"object_id": _oid(ORIGINAL), "filename": "prompts.jsonl"}
        if source == "wrong-bytes":
            f.parent["spec"]["child_delegation"]["input_object_ids"].append(_oid(ORIGINAL))
            f.objects[_oid(ORIGINAL).removeprefix("sha256:")] = REPLACEMENT
    with pytest.raises((host_bridge.ChildBridgeError, OSError)):
        astrid.invoke("generation.generate_image", kind="executor", inputs=_inputs(descriptor))
    assert not any(event[0] in {"upload", "admit"} for event in f.events)
    assert f.bridge._children == {}


@pytest.mark.parametrize("content", [ORIGINAL, b'{"prompt":"boat","strength":0.5}\n'])
def test_ordinary_managed_prompt_preflight_precedes_single_admission(
    monkeypatch, cached_route_discovery, tmp_path, content,
):
    monkeypatch.setattr(_child_bridge, "_bridge", None)
    reads, admissions = [], []
    oid = _oid(content)
    client = SimpleNamespace(get_object=lambda digest: reads.append(digest) or content)
    marker = object()

    def admit(capability, **kwargs):
        admissions.append(kwargs)
        return "R", "T", "A", None, {}, True, None

    monkeypatch.setattr(invocation, "_kernel_invoke", admit)
    monkeypatch.setattr(invocation, "_observe_task_invocation", lambda *args, **kwargs: marker)
    if content == ORIGINAL:
        result = astrid.invoke("generation.generate_image", kind="executor", project="demo", client=client,
            inputs=_inputs({"object_id": oid, "filename": "prompts.jsonl"}, count=2, seed=17))
        assert result is marker and len(admissions) == 1
        assert admissions[0]["inputs"]["prompts_file"]["object_id"] == oid
        assert admissions[0]["inputs"]["count"] == 2
    else:
        with pytest.raises(astrid.CapabilityValidationError):
            astrid.invoke("generation.generate_image", kind="executor", project="demo", client=client,
                inputs=_inputs({"object_id": oid, "filename": "prompts.jsonl"}))
        assert admissions == []
    assert reads == [oid.removeprefix("sha256:")]
    if content == ORIGINAL:
        _execute_snapshot(tmp_path, client, oid)


@pytest.mark.parametrize("custom,allow_dropped", [(True, False), (False, True)])
def test_child_retains_explicit_controls_and_selects_only_the_host_registry(
    custody, registry, monkeypatch, tmp_path, custom, allow_dropped,
):
    from dataclasses import replace

    class CustomRegistry:
        def get_by_mode(self, model, mode):
            entry, mode_spec = registry.get_by_mode(model, mode)
            return entry, replace(mode_spec, supports=(*mode_spec.supports, "strength"))

    f = custody
    _, descriptor = _producer(f)
    loads, checked = [], []
    selected = CustomRegistry() if custom else registry

    def load(**kwargs):
        loads.append(kwargs)
        return selected

    preflight = invocation.preflight_generation_inputs

    def check(**kwargs):
        assert kwargs["registry"] is selected
        assert kwargs["explicit_inputs"]["strength"] == 0.5
        assert kwargs["explicit_inputs"]["seed"] == 17
        assert "negative_prompt" not in kwargs["explicit_inputs"]
        assert kwargs["allow_dropped"] is allow_dropped
        result = preflight(**kwargs)
        checked.append(result)
        return result

    monkeypatch.setattr(sdk, "_load_model_registry", load)
    monkeypatch.setattr(invocation, "preflight_generation_inputs", check)
    result = astrid.invoke("generation.generate_image", kind="executor",
        project_root=tmp_path / "caller-selected-root", extra_pack_roots=("caller-pack-root",),
        inputs=_inputs(descriptor, strength=0.5, allow_dropped=allow_dropped, count=2, seed=17))
    assert result.ok and len(checked) == 1
    assert loads[-1] == {"project_root": f.root, "extra_pack_roots": (str(f.root),)}
    assert any(warning["feature"] == "strength" for warning in checked[0].warnings) is (not custom)
    assert "allow_dropped" not in next(event[1] for event in f.events if event[0] == "admit")


@pytest.mark.parametrize("change", ["omit-option", "change-model", "change-file"])
def test_forged_private_explicit_context_cannot_bypass_host_validation(custody, monkeypatch, change):
    f = custody
    _, descriptor = _producer(f)
    request_builder = _child_bridge._child_request

    def forge(*args, **kwargs):
        request = request_builder(*args, **kwargs)
        explicit = request["generation_preflight"]["explicit_inputs"]
        if change == "omit-option":
            explicit.pop("strength")
        elif change == "change-model":
            explicit["model"] = "flux-dev"
        else:
            explicit["prompts_file"] = {"object_id": _oid(REPLACEMENT), "filename": "other.jsonl"}
        return request

    monkeypatch.setattr(_child_bridge, "_child_request", forge)
    with pytest.raises(host_bridge.ChildBridgeError, match="disagree"):
        astrid.invoke("generation.generate_image", kind="executor",
            inputs=_inputs(descriptor, strength=0.5, allow_dropped=True))
    assert not any(event[0] in {"upload", "admit"} for event in f.events)
    assert f.bridge._children == {}


def test_ordinary_prompt_digest_mismatch_rejects_before_admission(monkeypatch, cached_route_discovery):
    monkeypatch.setattr(_child_bridge, "_bridge", None)
    client = SimpleNamespace(get_object=lambda digest: REPLACEMENT)
    admissions = []
    monkeypatch.setattr(invocation, "_kernel_invoke", lambda *args, **kwargs: admissions.append(kwargs))
    with pytest.raises(astrid.CapabilityValidationError, match="digest"):
        astrid.invoke("generation.generate_image", kind="executor", client=client, project="demo",
            inputs=_inputs({"object_id": _oid(ORIGINAL), "filename": "prompts.jsonl"}))
    assert admissions == []


@pytest.mark.parametrize("binding", ["descriptor", "digest-string", "execution-request"])
def test_ordinary_custody_freezes_descriptor_and_preserves_explicit_controls(
    monkeypatch, cached_route_discovery, tmp_path, binding,
):
    monkeypatch.setattr(_child_bridge, "_bridge", None)
    oid = _oid(ORIGINAL)
    descriptor = {"object_id": oid, "filename": "prompts.jsonl"}
    caller = _inputs(descriptor if binding == "descriptor" else oid,
        seed=17, strength=0.5, allow_dropped=True)
    request = None
    if binding == "execution-request":
        caller.pop("prompts_file")
        request = {"target": {"kind": "default"}, "inputs": [dict(name="prompts_file", **descriptor)]}
    checked, admissions = [], []
    preflight = invocation.preflight_generation_inputs

    def read(digest):
        assert digest == oid.removeprefix("sha256:")
        descriptor["object_id"] = _oid(REPLACEMENT)
        caller["seed"] = 999
        return ORIGINAL

    def check(**kwargs):
        assert kwargs["explicit_inputs"]["seed"] == 17
        assert kwargs["explicit_inputs"]["strength"] == 0.5
        assert kwargs["allow_dropped"] is True
        result = preflight(**kwargs)
        checked.append(result)
        return result

    def admit(capability, **kwargs):
        admissions.append(kwargs)
        return "R", "T", "A", None, {}, True, None

    client = SimpleNamespace(_remote=SimpleNamespace(_transport=SimpleNamespace(get_object=read)),
        media=SimpleNamespace(show=lambda project, digest: {"digest": digest,
            "filename": "prompts.jsonl", "media_type": "application/jsonl", "size": len(ORIGINAL)}))
    monkeypatch.setattr(invocation, "preflight_generation_inputs", check)
    monkeypatch.setattr(invocation, "_kernel_invoke", admit)
    monkeypatch.setattr(invocation, "_observe_task_invocation", lambda *a, **k: object())
    astrid.invoke("generation.generate_image", kind="executor", project="demo", client=client,
        inputs=caller, execution_request=request)
    assert len(admissions) == len(checked) == 1
    bound = admissions[0]["inputs"]["prompts_file"]
    assert (bound.get("object_id", bound.get("digest")) if isinstance(bound, dict) else bound) == oid
    assert admissions[0]["inputs"]["seed"] == 17
    assert descriptor["object_id"] == _oid(REPLACEMENT)


@pytest.mark.parametrize("binding", ["descriptor", "digest-string"])
def test_real_worker_fd_consumes_retained_bytes_after_staged_symlink_replacement(tmp_path, binding):
    """Independent FD/worker proof; host-launch cleanup tests remain mandatory."""
    from astrid.core.execution._prompt_snapshot import _prompt_snapshot_transport

    oid = _oid(ORIGINAL)
    value = {"object_id": oid, "filename": "prompts.jsonl"} if binding == "descriptor" else oid
    host = GenericPackHost(pack_roots=[tmp_path],
        client=SimpleNamespace(get_object=lambda digest: ORIGINAL))
    attempt = tmp_path / "worker"
    attempt.mkdir()
    values = host._materialize_inputs({"input_object_ids": [oid], "spec": {
        "inputs": {"prompts_file": value}}}, attempt, file_input_names=frozenset({"prompts_file"}))
    staged = Path(values["prompts_file"])
    foreign = tmp_path / "foreign.jsonl"
    foreign.write_bytes(REPLACEMENT)
    staged.unlink()
    staged.symlink_to(foreign)
    script = (
        "import json; from pathlib import Path; import astrid.sdk.invocation as inv; "
        "from astrid.packs.generation.shared._common import _load_prompts; "
        f"rows=_load_prompts(Path({str(staged)!r}), 'z-image', 't2i'); "
        "print(json.dumps(dict(rows=rows, origin=inv.__file__)))"
    )
    checkout = Path(invocation.__file__).resolve().parents[2]
    bootstrap = (
        f"import sys; sys.path.insert(0, {str(checkout)!r}); "
        "from astrid.core.execution.generic_host_worker import _run_child_command; "
        "raise SystemExit(_run_child_command(sys.argv[1], sys.argv[2], prompt=True))"
    )
    with _prompt_snapshot_transport(values) as transport:
        result = subprocess.run([sys.executable, "-I", "-c", bootstrap, str(transport.fileno()),
            json.dumps(["-c", script])], pass_fds=(transport.fileno(),), capture_output=True,
            text=True, timeout=30, check=True)
    observed = json.loads(result.stdout)
    assert observed["rows"][0]["prompt"] == "authenticated paper boat"
    assert observed["rows"][0]["seed"] == 29
    assert Path(observed["origin"]).resolve() == Path(invocation.__file__).resolve()
    assert staged.read_bytes() == REPLACEMENT
