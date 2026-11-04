from __future__ import annotations

import hashlib
import io
import json
from types import SimpleNamespace

import pytest
from PIL import Image

import astrid.sdk as sdk
from astrid.core.generation.storage_policy import OPENAI_IMAGE_STORAGE_POLICY
from astrid.sdk import _child_bridge, invocation
from astrid.sdk.exceptions import CapabilityValidationError


def _descriptor(data: bytes, *, filename: str = "prompts.jsonl", media_type: str = "application/jsonl"):
    return {"digest": "sha256:" + hashlib.sha256(data).hexdigest(), "filename": filename,
            "media_type": media_type, "size_bytes": len(data)}


def _image(color: str) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (2, 2), color).save(buffer, format="PNG")
    return buffer.getvalue()


class _Client:
    def __init__(self, payloads=None):
        self.payloads = payloads or {}
        self.reads = []
        self.admissions = []
        self.tasks = SimpleNamespace(create=self._create)
        self.media = SimpleNamespace(read_bytes=self._read, show=self._show)

    def _create(self, **request):
        self.admissions.append(request)
        return SimpleNamespace(ok=False, data=None, error={"code": "fixture", "message": "captured"})

    def _read(self, digest):
        self.reads.append(digest)
        return self.payloads[digest]

    def _show(self, project, digest):
        assert project == "demo"
        data = self.payloads[digest]
        return {"digest": digest, "object_id": digest, "filename": "guide.png",
                "media_type": "image/png", "size_bytes": len(data)}


def _invoke(client, inputs, capability="generation.generate_image"):
    return invocation.invoke(capability, kind="executor", project="demo", client=client, inputs=inputs)


@pytest.fixture(autouse=True)
def _no_provider(monkeypatch):
    from astrid.core.generation.backends import openai

    def forbidden(*args, **kwargs):
        raise AssertionError("SDK admission must not access provider credentials or transport")

    monkeypatch.setattr(openai, "_resolve_key", forbidden)
    monkeypatch.setattr(openai, "_call_image_api", forbidden)


def test_explicit_gpt_image_selects_existing_capability_before_one_admission():
    client = _Client()
    _invoke(client, {"model": "gpt-image-1", "prompt": "a lantern", "count": 2})
    assert client.reads == []
    assert len(client.admissions) == 1
    request = client.admissions[0]
    assert request["capability"] == "generation.generate_image_openai"
    assert "inputs" not in request["spec"]
    params = request["spec"]["params"]
    assert (params["mode"], params["execution"], params["count"]) == ("t2i", "openai", 2)
    assert json.loads(params["jobs"])[0]["n"] == 2
    assert request["storage_estimate"] == OPENAI_IMAGE_STORAGE_POLICY.estimate
    assert request["settlement_effect"]["effect_type"] == "generation.publish_v1"
    assert [s["ordinal"] for s in request["generation_intent"]["groups"][0]["selectors"]] == [0, 1]


def test_authenticated_jsonl_and_ordered_project_handles_are_frozen_before_admission():
    prompts = b'{"prompt":"first","n":2}\n{"prompt":"second","n":1}\n'
    images = [_image("blue"), _image("red")]
    prompt = _descriptor(prompts)
    refs = [_descriptor(data, filename=f"guide-{index}.png", media_type="image/png")
            for index, data in enumerate(images)]
    payloads = {item["digest"]: data for item, data in zip([prompt, *refs], [prompts, *images])}
    client = _Client(payloads)
    _invoke(client, {"model": "gpt-image-1", "execution": "openai", "prompts_file": prompt,
                     "image_ref": [refs[1]["digest"], refs[0]["digest"]]})
    request = client.admissions[0]
    params = request["spec"]["params"]
    assert params["mode"] == "edit"
    assert params["count"] == 3
    assert [job["prompt"] for job in json.loads(params["jobs"])] == ["first", "second"]
    ordered = [prompt["digest"], refs[1]["digest"], refs[0]["digest"]]
    assert client.reads == ordered
    assert request["input_manifest"] == ordered
    assert request["spec"]["input_digests"] == [
        {"name": name, "digest": digest}
        for name, digest in zip(("prompts_file", "image_ref_00", "image_ref_01"), ordered)
    ]
    assert len(request["generation_intent"]["groups"][0]["selectors"]) == 3
    assert len(client.admissions) == 1


def test_legacy_capability_uses_same_normalization_and_one_admission():
    data = b'{"prompt":"legacy","n":2}\n'
    prompt = _descriptor(data)
    client = _Client({prompt["digest"]: data})
    _invoke(client, {"prompts_file": prompt}, capability="generation.generate_image_openai")
    assert len(client.admissions) == 1
    params = client.admissions[0]["spec"]["params"]
    assert params["model"] == "gpt-image-2"
    assert params["count"] == 2
    assert json.loads(params["jobs"])[0]["prompt"] == "legacy"


@pytest.mark.parametrize("data", [
    b'{"prompt":', b'\xff', b'{"prompt":"x","callback":"forbidden"}',
    b'{"prompt":"x","n":11}', b'[]', b'{"prompt":""}',
])
def test_invalid_authenticated_prompt_jobs_refuse_without_admission(data):
    prompt = _descriptor(data)
    client = _Client({prompt["digest"]: data})
    with pytest.raises(CapabilityValidationError):
        _invoke(client, {"model": "gpt-image-1", "prompts_file": prompt})
    assert client.admissions == []


@pytest.mark.parametrize("descriptor", [
    "caller-local.jsonl",
    {"digest": "sha256:" + "a" * 64},
    {"digest": "sha256:" + "a" * 64, "size_bytes": 1024 * 1024 + 1},
    {"digest": "sha256:" + "a" * 64, "object_id": "sha256:" + "b" * 64, "size_bytes": 8},
])
def test_unauthenticated_or_oversized_prompt_descriptor_refuses_before_read(descriptor):
    client = _Client()
    with pytest.raises(CapabilityValidationError):
        _invoke(client, {"model": "gpt-image-1", "prompts_file": descriptor})
    assert client.reads == client.admissions == []


@pytest.mark.parametrize("replacement", [b'bad hash', b'{"prompt":"x"}\nextra'])
def test_changed_managed_prompt_bytes_refuse_before_admission(replacement):
    prompt = _descriptor(b'{"prompt":"x"}\n')
    client = _Client({prompt["digest"]: replacement})
    with pytest.raises(CapabilityValidationError, match="digest or size"):
        _invoke(client, {"model": "gpt-image-1", "prompts_file": prompt})
    assert client.admissions == []


@pytest.mark.parametrize("inputs", [
    {"count": True}, {"count": 11}, {"callback": "forbidden"},
    {"execution": "cloud"}, {"jobs": "[]"}, {"image_ref": []},
])
def test_invalid_scalar_request_has_no_admission(inputs):
    client = _Client()
    with pytest.raises(CapabilityValidationError):
        _invoke(client, {"model": "gpt-image-1", "prompt": "a lantern", **inputs})
    assert client.reads == client.admissions == []


def test_duplicate_and_non_image_references_refuse_without_admission():
    image = _image("blue")
    ref = _descriptor(image, filename="guide.png", media_type="image/png")
    client = _Client({ref["digest"]: image})
    with pytest.raises(CapabilityValidationError, match="distinct"):
        _invoke(client, {"model": "gpt-image-1", "prompt": "edit", "image_ref": [ref, ref]})
    assert client.reads == client.admissions == []
    invalid = _descriptor(b"not an image", filename="guide.png", media_type="image/png")
    client = _Client({invalid["digest"]: b"not an image"})
    with pytest.raises(CapabilityValidationError, match="decodable"):
        _invoke(client, {"model": "gpt-image-1", "prompt": "edit", "image_ref": invalid})
    assert client.admissions == []


def test_explicit_intent_must_match_expanded_cardinality():
    client = _Client()
    cap = sdk.get_capability("generation.generate_image_openai", kind="executor")
    intent = invocation._automatic_generation_intent(cap, {"count": 1, "jobs": "[]"}, modality="image")
    with pytest.raises(CapabilityValidationError, match="cardinality"):
        _invoke(client, {"model": "gpt-image-1", "prompt": "x", "count": 2,
                         "generation_intent": intent})
    assert client.admissions == []


def test_openai_automatic_intent_waits_for_authenticated_expanded_jobs():
    cap = sdk.get_capability("generation.generate_image_openai", kind="executor")
    assert invocation._automatic_generation_intent(
        cap, {"count": 1, "prompts_file": "unresolved.jsonl"}, modality="image",
    ) is None


def test_child_sdk_projects_scalar_files_without_parsing_or_expanding(monkeypatch):
    captured = []
    bridge = SimpleNamespace(submit=lambda request: captured.append(request) or
                             {"task_id": "task-1", "run_id": "run-1", "attempt_id": "attempt-1"})
    monkeypatch.setattr(_child_bridge, "_bridge", bridge)
    invocation.invoke("generation.generate_image", kind="executor", inputs={
        "model": "gpt-image-1", "prompts_file": {
            "filename": "prompts.jsonl", "media_type": "application/jsonl", "output_port": "prompts"},
        "image_ref": [{"digest": "sha256:" + "a" * 64, "size_bytes": 4,
                       "media_type": "image/png", "filename": "guide.png"}],
    })
    request = captured[0]
    assert request["child"]["capability_id"] == "generation.generate_image_openai"
    assert "jobs" not in request["inputs"]
    assert [item["name"] for item in request["input_descriptors"]] == ["prompts_file", "image_ref_00"]
    assert request["inputs"]["image_ref_00"] == {"digest": "sha256:" + "a" * 64, "filename": "guide.png"}


@pytest.mark.parametrize("control", ["generation_intent", "variant_of", "primary"])
def test_child_sdk_refuses_publication_controls_without_bridge_submission(monkeypatch, control):
    captured = []
    monkeypatch.setattr(_child_bridge, "_bridge", SimpleNamespace(submit=lambda request: captured.append(request)))
    with pytest.raises(CapabilityValidationError, match="host-derived"):
        invocation.invoke("generation.generate_image", kind="executor", inputs={
            "model": "gpt-image-1", "prompt": "x", control: "unsupported",
        })
    assert captured == []


def test_fal_profile_scope_remains_fixed_and_existing_inference_unchanged():
    fal = sdk.get_capability("generation.generate_image", kind="executor")
    assert fal.definition["metadata"]["fixed_inputs"] == {"mode": "t2i", "execution": "cloud"}
    assert fal.definition["metadata"]["api_provider"] == "fal"
    from astrid.core.model_catalog.registry import ModelRegistry
    from astrid.sdk.generation import _resolve_execution

    registry = ModelRegistry.load_default()
    assert _resolve_execution(registry.get("flux-dev"), "t2i", None, model="flux-dev") == "cloud"


def test_unqualified_target_request_keeps_local_comfyui_route(monkeypatch):
    from astrid.sdk.generation import GenerationFacade

    seen = {}
    marker = object()

    def capture(self, **request):
        seen.update(request)
        return marker

    monkeypatch.setattr(GenerationFacade, "_generate_on_target", capture)
    result = GenerationFacade().image(target="auto", prompt="a lantern", dry_run=True)
    assert result is marker
    assert seen["execution"] is None
    assert seen["model"] is None
    assert seen["target"] == "auto"
