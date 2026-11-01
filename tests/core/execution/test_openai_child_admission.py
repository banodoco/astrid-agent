"""Offline G07 custody proof through the real Agent/Runtime adapters.

The D18 fixture runs RuntimeService against a disposable realm and replaces
only the generated client's HTTP transport. No provider or command is run.
"""
from __future__ import annotations

import copy
import base64
import io
import json
import sys
from pathlib import Path

import pytest
import yaml
from PIL import Image

from astrid.core._shared.result_manifest import harvest_staged_outputs
from astrid.core.contracts.binding import expand_command
from astrid.core.execution._child_bridge import ChildBridgeError, task_resource
from astrid.core.execution.generic_host import GenericPackHost, HostError, _admitted_task_spec
from astrid.core.generation.storage_policy import OPENAI_IMAGE_STORAGE_POLICY
from tests.core.execution.test_generic_host_child_bridge_d18 import (
    PARENT,
    _claim_snapshot_task,
    _snapshot_task,
    bridge,
    digest,
    world,
)

pytestmark = pytest.mark.integration
OPENAI = "generation.generate_image_openai"


@pytest.fixture
def openai_world(world):
    transport = world.client.generated._transport

    def wire_transport(method, path, headers, body):
        status, response_headers, response_body = transport(method, path, headers, body)
        if path == "/v1/health":
            # RuntimeHandler adds this field to RuntimeService.health(); D18
            # intentionally calls the service without starting an HTTP server.
            health = json.loads(response_body)
            health["identity"] = None
            response_body = json.dumps(health).encode()
        return status, response_headers, response_body

    world.client.generated._transport = wire_transport
    yield world
    if hasattr(world, "host"):
        world.host.shutdown()


def _setup(world, *, root_object_ids=(), limits=None, allow_openai=True):
    """Use the production generation declaration and a small delegating parent."""
    (world.root / "actions").mkdir()
    (world.root / "actions/parent.py").write_text("def run():\n    return {}\n")
    (world.root / "pack.yaml").write_text(yaml.safe_dump({
        "schema_version": 3, "id": "bridge", "name": "G07 parent", "version": "1.0.0",
        "actions": {"parent": {"description": "Offline delegating parent", "inputs": [],
            "outputs": [], "invocation": {"kind": "python", "path": "actions/parent.py", "function": "run"}}},
    }))
    generation = Path(__file__).resolve().parents[3] / "astrid/packs/generation"
    host = GenericPackHost(pack_roots=[world.root, generation], client=world.client,
                           executor_id="worker", max_concurrency=2)
    host.discover()
    world.host = host
    for capability in (PARENT, OPENAI, "generation.generate_image"):
        record = host.capabilities[capability]
        world.service.register_capability({"capability_id": capability,
                                           "definition_digest": record.capability_digest})
    world.service.register_executor({"executor_id": "worker", "capabilities": [PARENT, OPENAI],
        "max_concurrency": 2}, idempotency_key="g07-register")
    permitted = OPENAI if allow_openai else "generation.generate_image"
    policy = {"capabilities": [{"capability_id": permitted,
        "capability_digest": host.capabilities[permitted].capability_digest}],
        "targets": [{"kind": "default"}], "input_object_ids": list(root_object_ids),
        "limits": {"max_children": 4, **(limits or {})}}
    world.service.create_task({"project": world.project, "capability_id": PARENT,
        "capability_digest": host.capabilities[PARENT].capability_digest,
        "input_object_ids": list(root_object_ids), "spec": {"inputs": {}},
        "child_delegation": policy, "idempotency_key": "g07-parent"}, enforce_readiness=True)
    claim = world.service.claim_next({"executor_id": "worker", "capability_ids": [PARENT],
        "runtime_epoch": 1}, idempotency_key="g07-claim-parent", identity=world.identity)
    world.client._attempt_runtime_epochs[claim["attempt_id"]] = claim["runtime_epoch"]
    world.claim = claim
    world.task = _snapshot_task(world, claim)
    return bridge(world)


def _request(world, b, inputs, descriptors=(), *, key="image"):
    return {"v": 1, "request_id": b._sequence + 1, "op": "submit", "child_key": key,
        "child": {"capability_id": OPENAI,
                  "capability_digest": world.host.capabilities[OPENAI].capability_digest},
        "inputs": dict(inputs), "input_descriptors": list(descriptors), "wait": True,
        "timeout_seconds": 0.5, "poll_seconds": 0.01}


def _png(color):
    data = io.BytesIO()
    Image.new("RGB", (2, 2), color).save(data, format="PNG")
    return data.getvalue()


def _producer(b, name, filename, data, *, media_type="image/png"):
    path = b.output_root / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    value = {"filename": filename, "output_port": name, "media_type": media_type}
    return value, {"name": name, "kind": "producer_file", **value}


def _object(world, name, data, *, filename=None):
    oid = digest(data)
    world.service.ingest(world.project, data, original_name=filename or name,
                         idempotency_key="root-" + name + "-" + oid[7:])
    return {"object_id": oid, "digest": oid, "filename": filename or name}, {
        "name": name, "kind": "object", "object_id": oid}


def _children(world):
    return world.service.store.delegated_children(world.task["id"], world.claim["attempt_id"])


def _mutations(world):
    return [(method, path, payload) for method, path, payload in world.calls
            if method == "POST" and (path in {"/v1/objects", "/v1/delegated-tasks"}
                                     or path.endswith("/child-authority"))]


def _outgoing(world):
    return [body["task"] for _, path, body in world.calls if path == "/v1/delegated-tasks"]


def _materialize(world, task, attempt):
    record = world.host.capabilities[OPENAI]
    metadata = record.definition.metadata
    return world.host._materialize_inputs(task["spec"], attempt,
        authorized_input_object_ids=task["input_object_ids"],
        task_param_ports=metadata["hc04_param_ports"],
        cas_param_ports=metadata["hc04_cas_param_ports"],
        optional_cas_param_ports=metadata["hc04_optional_cas_param_ports"],
        storage_estimate=OPENAI_IMAGE_STORAGE_POLICY.estimate,
        input_size_limits=metadata["storage_input_max_bytes"],
        storage_policy_version=metadata["storage_policy_version"],
        file_input_names=frozenset(port.name for port in record.definition.inputs if port.type == "file"))


def test_openai_t2i_one_admitted_task_binds_frozen_outputs_and_replays(openai_world):
    world = openai_world
    b = _setup(world)
    request = _request(world, b, {"prompt": "a blue square", "model": "gpt-image-2",
                                 "mode": "t2i", "execution": "openai", "n": 2})
    child = b.dispatch(request)
    task = _outgoing(world)[0]
    params = task["spec"]["params"]
    assert params["model"] == "gpt-image-2" and params["execution"] == "openai"
    assert params["mode"] == "t2i" and params["count"] == 2
    jobs = json.loads(params["jobs"])
    assert [(job["prompt"], job["n"]) for job in jobs] == [("a blue square", 2)]
    assert task["storage_estimate"] == OPENAI_IMAGE_STORAGE_POLICY.estimate
    selectors = task["generation_intent"]["groups"][0]["selectors"]
    assert [(selector["selector"], selector["ordinal"], selector["required"]) for selector in selectors] == [
        ("main-0", 0, True), ("main-1", 1, True)]
    stored = task_resource(world.client.task(child["task_id"]))
    assert stored["generation_intent"] == task["generation_intent"]
    assert _admitted_task_spec(stored)["params"] == params
    assert len(_children(world)) == len(_outgoing(world)) == 1
    request["request_id"] = b._sequence + 1
    assert b.dispatch(request) == child
    assert len(_children(world)) == len(_outgoing(world)) == 1
    request["inputs"]["n"] = 1
    request["request_id"] = b._sequence + 1
    with pytest.raises(ChildBridgeError):
        b.dispatch(request)
    assert len(_children(world)) == len(_outgoing(world)) == 1


def test_openai_jsonl_and_ordered_edit_refs_are_retained_before_binding_and_upload(openai_world, monkeypatch):
    from astrid.core.generation.backends import openai

    world = openai_world
    b = _setup(world)
    prompt_data = b'{"prompt":"first","n":2}\n{"prompt":"second","n":1}\n'
    blobs = {"prompts_file": prompt_data, "image_ref_00": _png("red"), "image_ref_01": _png("blue")}
    inputs = {"model": "gpt-image-2", "mode": "edit", "execution": "openai"}
    descriptors, files = [], {}
    for name, data in blobs.items():
        filename = name + (".jsonl" if name == "prompts_file" else ".png")
        value, descriptor = _producer(b, name, filename, data,
            media_type="application/jsonl" if name == "prompts_file" else "image/png")
        inputs[name] = value
        descriptors.append(descriptor)
        files[name] = b.output_root / filename
    events, captured = [], []
    normalize = openai.normalize_request
    upload = world.client.upload_child_input
    replaced = False

    def normalized(values, **kwargs):
        nonlocal replaced
        result = normalize(values, **kwargs)
        if kwargs.get("prompt_bytes") == prompt_data and not kwargs.get("require_frozen"):
            events.append("expanded")
            if not replaced:
                assert not _mutations(world)
                assert not b._children  # Observe the required pre-binding boundary.
                for path in files.values():
                    path.unlink()
                replaced = True
        return result

    def uploaded(data, **kwargs):
        events.append("upload")
        assert events[0] == "expanded" and len(b._children) == 1
        captured.append((kwargs["descriptor"]["name"], bytes(data)))
        return upload(data, **kwargs)

    monkeypatch.setattr(openai, "normalize_request", normalized)
    monkeypatch.setattr(world.client, "upload_child_input", uploaded)
    request = _request(world, b, inputs, descriptors)
    child = b.dispatch(request)
    assert captured == list(blobs.items())
    task = _outgoing(world)[0]
    params = task["spec"]["params"]
    assert params["count"] == 3
    assert [(job["prompt"], job["n"]) for job in json.loads(params["jobs"])] == [("first", 2), ("second", 1)]
    positions = task["execution_request"]["inputs"]
    assert [row["name"] for row in positions] == list(blobs)
    assert [row["object_id"] for row in positions] == [digest(data) for data in blobs.values()]
    assert all(params[row["name"]]["digest"] == row["object_id"] for row in positions)
    assert len(task["generation_intent"]["groups"][0]["selectors"]) == 3
    for name, data in blobs.items():
        files[name].write_bytes(data)
    before = list(_mutations(world))
    request["request_id"] = b._sequence + 1
    assert b.dispatch(request) == child
    assert _mutations(world) == before and len(_children(world)) == 1
    files["image_ref_00"].write_bytes(_png("green"))
    request["request_id"] = b._sequence + 1
    with pytest.raises(ChildBridgeError):
        b.dispatch(request)
    assert _mutations(world) == before and len(_children(world)) == 1


def test_openai_authenticated_managed_jsonl_and_ref_order_reach_real_materialization(openai_world, monkeypatch):
    from astrid.core.generation.backends import openai
    from astrid.packs.generation.actions.generate_image.run import generate_core

    world = openai_world
    blobs = {"prompts_file": b'{"prompt":"edit both in order","n":2}\n',
             "image_ref_00": _png("red"), "image_ref_01": _png("blue")}
    values, descriptors = {}, []
    for name, data in blobs.items():
        value, descriptor = _object(world, name, data, filename=name + (".jsonl" if name == "prompts_file" else ".png"))
        values[name] = value
        descriptors.append(descriptor)
    b = _setup(world, root_object_ids=[digest(data) for data in blobs.values()])
    child = b.dispatch(_request(world, b, {**values, "mode": "edit", "execution": "openai", "model": "gpt-image-2"}, descriptors))
    assert not any(path == "/v1/objects" for _, path, _ in world.calls)
    assert len(_children(world)) == 1
    claim = _claim_snapshot_task(world, OPENAI, key="g07-claim-image")
    assert claim["task_id"] == child["task_id"]
    task = _snapshot_task(world, claim)
    materialized = _materialize(world, task, world.tmp / "image-materialized")
    assert [Path(materialized[name]).read_bytes() for name in blobs] == list(blobs.values())
    record = world.host.capabilities[OPENAI]
    bound = expand_command(record.definition.command, record.definition.inputs,
        {**materialized, "out": str(world.tmp / "command-output"), "python_exec": sys.executable},
        record.definition.metadata)
    assert bound.argv[bound.argv.index("--image-ref-00") + 1] == materialized["image_ref_00"]
    assert bound.argv[bound.argv.index("--image-ref-01") + 1] == materialized["image_ref_01"]
    assert json.loads(bound.argv[bound.argv.index("--jobs") + 1]) == json.loads(_admitted_task_spec(task)["params"]["jobs"])
    altered = copy.deepcopy(task)
    _admitted_task_spec(altered)["params"]["count"] = 1
    with pytest.raises(HostError):
        _materialize(world, altered, world.tmp / "wrong-frozen-count")
    swapped = copy.deepcopy(task)
    params = _admitted_task_spec(swapped)["params"]
    params["image_ref_00"], params["image_ref_01"] = params["image_ref_01"], params["image_ref_00"]
    with pytest.raises(HostError):
        _materialize(world, swapped, world.tmp / "swapped-reference-positions")
    wrong_size = copy.deepcopy(task)
    _admitted_task_spec(wrong_size)["params"]["prompts_file"]["size_bytes"] += 1
    with pytest.raises(HostError):
        _materialize(world, wrong_size, world.tmp / "wrong-declared-size")

    # Continue the actual bound command through its parser and executor. Only
    # the credential resolver and provider HTTP boundary are replaced.
    calls = []
    output = _png("green")

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, maximum):
            data = json.dumps({"data": [{"b64_json": base64.b64encode(output).decode()}] * 2,
                               "created": 1}).encode()
            return data[:maximum]

    def fake_urlopen(request, *, timeout):
        calls.append(request)
        assert request.full_url == "https://api.openai.com/v1/images/edits"
        assert request.get_header("Authorization") == "Bearer fake-g07"
        assert timeout == 180 and request.data.count(b'name="image[]"') == 2
        assert request.data.index(blobs["image_ref_00"]) < request.data.index(blobs["image_ref_01"])
        return Response()

    monkeypatch.setattr(openai, "_resolve_key", lambda **_kwargs: "fake-g07")
    monkeypatch.setattr(openai, "urlopen", fake_urlopen)
    result = generate_core(list(bound.argv[3:]))
    assert result.ok and len(calls) == 1 and len(result.image_paths) == 2
    assert all(path.read_bytes() == output for path in result.image_paths)
    harvested = harvest_staged_outputs(result.run_dir, definition=record.definition,
                                       declared_outputs=record.definition.outputs)
    images = [row for row in harvested if row["name"] == "generated_images"]
    assert [row["ordinal"] for row in images] == [0, 1]
    assert all(row["bytes"] == len(output) and row["content_hash"] == digest(output) for row in images)
    assert [row["artifact_type"] for row in result.manifest["outputs"]] == ["image", "image"]
    assert len(_children(world)) == len(_outgoing(world)) == 1


@pytest.mark.parametrize("case", ["malformed-jsonl", "duplicate-key", "too-many-outputs", "oversized-jsonl",
    "bad-image", "oversized-image", "missing-producer", "unsafe-producer", "wrong-provider", "undelegated"])
def test_openai_invalid_producer_request_never_binds_uploads_or_admits(openai_world, case):
    world = openai_world
    b = _setup(world, allow_openai=case != "undelegated")
    prompt_data = b'{"prompt":"valid"}\n'
    if case == "malformed-jsonl":
        prompt_data = b'{"prompt":\n'
    elif case == "duplicate-key":
        prompt_data = b'{"prompt":"one","prompt":"two"}\n'
    elif case == "too-many-outputs":
        prompt_data = b'{"prompt":"one","n":10}\n' * 4
    elif case == "oversized-jsonl":
        prompt_data = b"x" * (OPENAI_IMAGE_STORAGE_POLICY.prompt_max_bytes + 1)
    value, descriptor = _producer(b, "prompts_file", "prompts.jsonl", prompt_data, media_type="application/jsonl")
    inputs = {"prompts_file": value, "mode": "t2i", "model": "gpt-image-2", "execution": "openai"}
    descriptors = [descriptor]
    if case in {"bad-image", "oversized-image"}:
        value, descriptor = _producer(b, "image_ref_00", "ref.png", b"invalid" if case == "bad-image" else _png("red"))
        inputs.update(mode="edit", image_ref_00=value)
        descriptors.append(descriptor)
        if case == "oversized-image":
            with (b.output_root / "ref.png").open("r+b") as stream:
                stream.truncate(OPENAI_IMAGE_STORAGE_POLICY.source_max_bytes + 1)
    elif case == "missing-producer":
        (b.output_root / "prompts.jsonl").unlink()
    elif case == "unsafe-producer":
        inputs["prompts_file"]["filename"] = descriptor["filename"] = "../prompts.jsonl"
    elif case == "wrong-provider":
        inputs["execution"] = "cloud"
    with pytest.raises((HostError, ValueError, OSError)):
        b.dispatch(_request(world, b, inputs, descriptors))
    assert not b._children and not _children(world) and not _mutations(world)


@pytest.mark.parametrize("case", ["unauthorized", "wrong-hash", "oversized", "malformed"])
def test_openai_managed_jsonl_custody_refusal_precedes_binding_and_mutation(openai_world, monkeypatch, case):
    world = openai_world
    data = b'{"prompt":"managed"}\n' if case != "malformed" else b'{"prompt":\n'
    if case == "oversized":
        data = b"x" * (OPENAI_IMAGE_STORAGE_POLICY.prompt_max_bytes + 1)
    value, descriptor = _object(world, "prompts_file", data, filename="prompts.jsonl")
    b = _setup(world, root_object_ids=[] if case == "unauthorized" else [digest(data)])
    fetched = []
    get_object = world.client.get_object

    def fetch(oid):
        fetched.append(oid)
        if case == "wrong-hash":
            return b'{"prompt":"forged"}\n'
        return get_object(oid)

    monkeypatch.setattr(world.client, "get_object", fetch)
    with pytest.raises((HostError, ValueError)):
        b.dispatch(_request(world, b, {"prompts_file": value, "model": "gpt-image-2",
            "execution": "openai", "mode": "t2i"}, [descriptor]))
    assert len(fetched) == (0 if case == "unauthorized" else 1)
    assert not b._children and not _children(world) and not _mutations(world)


@pytest.mark.parametrize("limit", [{"max_child_inputs": 1}, {"max_derived_objects": 1},
                                   {"max_child_bytes": 1}, {"max_derived_bytes": 1}])
def test_openai_existing_child_limits_refuse_before_upload_or_admission(openai_world, limit):
    world = openai_world
    b = _setup(world, limits=limit)
    prompt, p_binding = _producer(b, "prompts_file", "prompts.jsonl", b'{"prompt":"edit"}\n', media_type="application/jsonl")
    image, i_binding = _producer(b, "image_ref_00", "ref.png", _png("red"))
    with pytest.raises(HostError):
        b.dispatch(_request(world, b, {"prompts_file": prompt, "image_ref_00": image,
            "mode": "edit", "model": "gpt-image-2", "execution": "openai"}, [p_binding, i_binding]))
    assert not b._children and not _children(world) and not _mutations(world)
