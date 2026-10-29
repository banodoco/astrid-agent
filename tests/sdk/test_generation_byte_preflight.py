"""Generation preflight rejects invalid requests before either admission route."""

from pathlib import Path

import pytest

import astrid
import astrid.sdk as sdk
from astrid.core.model_catalog.registry import ModelRegistry
from astrid.sdk import _child_bridge, invocation


@pytest.fixture(scope="module")
def discovered_registries(tmp_path_factory):
    """Discover once in isolated state; the selected model registry is still tested per route."""
    root = tmp_path_factory.mktemp("generation-preflight-discovery")
    with pytest.MonkeyPatch.context() as env:
        for name in ("HOME", "ASTRID_HOME", "ASTRID_WORKSPACE_CONFIG_DIR", "XDG_STATE_HOME", "XDG_DATA_HOME"):
            env.setenv(name, str(root / name))
        for name in ("ASTRID_PACKS_PATH", "ASTRID_SOURCE_DECLARATIONS", "ASTRID_SOURCE_STATE", "ASTRID_SOURCE_DATA"):
            env.delenv(name, raising=False)
        capabilities = sdk._load_registries(project_root=root, extra_pack_roots=(),
            banodoco_config=None, include_missing_roots=False, include_elements=False,
            include_internal=False)
        models = ModelRegistry.load_default(project_root=root)
    return capabilities, models


@pytest.fixture
def registry(discovered_registries):
    return discovered_registries[1]


@pytest.fixture
def cached_route_discovery(monkeypatch, discovered_registries):
    capabilities, models = discovered_registries
    monkeypatch.setattr(sdk, "_load_registries", lambda **kwargs: capabilities)
    monkeypatch.setattr(sdk, "_load_model_registry", lambda **kwargs: models)
    return capabilities


def _request(**changes):
    return {"model": "z-image", "mode": "t2i", "execution": "cloud", "prompt": "a paper boat", **changes}


@pytest.mark.parametrize("route", ["ordinary", "child"])
@pytest.mark.parametrize("bad_option", [{"strength": 0.5}, {"not_a_generation_option": "oops"}])
def test_invalid_explicit_options_never_reach_dispatch_or_admission(monkeypatch, cached_route_discovery, route, bad_option):
    dispatched = []
    monkeypatch.setattr(invocation, "_kernel_invoke", lambda *a, **k: dispatched.append("admit"))
    monkeypatch.setattr(invocation, "_invoke_bridge_child", lambda *a, **k: dispatched.append("child"))
    monkeypatch.setattr(_child_bridge, "_bridge", object() if route == "child" else None)
    with pytest.raises(astrid.CapabilityValidationError):
        astrid.invoke("generation.generate_image", kind="executor", inputs=_request(**bad_option),
                      **({"project": "demo"} if route == "ordinary" else {}))
    assert dispatched == []


@pytest.mark.parametrize("content", [
    b'[{"prompt":"first","seed":3},{"prompt":"second","seed":4}]',
    b'{"prompt":"first","seed":3}\n{"prompt":"second","seed":4}\n',
])
def test_preflight_preserves_exact_bytes_and_parses_the_snapshot(registry, tmp_path: Path, content):
    path = tmp_path / "prompts.jsonl"
    path.write_bytes(content)
    snapshot = path.read_bytes()
    path.write_bytes(b'{"prompt":"replacement","strength":0.7}\n')
    inputs = _request(prompt="", prompts_file=str(path))
    result = invocation.preflight_generation_inputs(
        registry=registry, modality="image", inputs=inputs, explicit_inputs=inputs,
        prompt_bytes=snapshot,
    )
    assert result.prompt_bytes == content
    assert [row["prompt"] for row in result.prompt_rows] == ["first", "second"]
    assert [row["seed"] for row in result.prompt_rows] == [3, 4]
    assert path.read_bytes() != result.prompt_bytes


@pytest.mark.parametrize("content", [
    b'{"prompt":"first","strength":0.5}\n',
    b'{"prompt":"first","model":"not-a-model","mode":"t2i"}\n',
    b'{"prompt":"first","not_a_generation_option":true}\n',
])
def test_prompt_rows_are_validated_before_consumption(registry, content):
    inputs = _request(prompt="", prompts_file="prompts.jsonl")
    with pytest.raises(astrid.CapabilityValidationError):
        invocation.preflight_generation_inputs(
            registry=registry, modality="image", inputs=inputs, explicit_inputs=inputs,
            prompt_bytes=content,
        )


@pytest.mark.parametrize("route", ["ordinary", "child"])
def test_valid_request_reaches_the_selected_route_with_defaults(monkeypatch, cached_route_discovery, route):
    seen = []
    marker = object()
    monkeypatch.setattr(_child_bridge, "_bridge", object() if route == "child" else None)
    def child(_capability, **kwargs):
        seen.append(kwargs["inputs"])
        return marker
    def kernel(_capability, **kwargs):
        seen.append(kwargs["inputs"])
        return "run", "task", "attempt", None, {}, True, None
    monkeypatch.setattr(invocation, "_invoke_bridge_child", child)
    monkeypatch.setattr(invocation, "_kernel_invoke", kernel)
    monkeypatch.setattr(invocation, "_observe_task_invocation", lambda *a, **k: marker)
    result = astrid.invoke("generation.generate_image", kind="executor", inputs=_request(),
                           **({"project": "demo"} if route == "ordinary" else {}))
    assert result is marker
    assert len(seen) == 1
    assert seen[0]["model"] == "z-image"
    assert seen[0]["mode"] == "t2i"
    assert seen[0]["execution"] == "cloud"
    if route == "child":
        assert int(seen[0]["count"]) == 1
    else:
        # The host injects omitted port defaults after ordinary admission.
        assert "count" not in seen[0]
        capability = sdk.get_capability("generation.generate_image", kind="executor",
                                        _registries=cached_route_discovery)
        assert int(next(port.default for port in capability.inputs if port.name == "count")) == 1


@pytest.mark.parametrize("route", ["ordinary", "child"])
def test_preflight_uses_registry_selected_for_project(monkeypatch, registry, tmp_path, route):
    from dataclasses import replace
    import astrid.sdk as sdk

    class CustomRegistry:
        def get_by_mode(self, model, mode):
            entry, spec = registry.get_by_mode(model, mode)
            return entry, replace(spec, supports=(*spec.supports, "strength"))

    selected = []
    def load(*, project_root, extra_pack_roots):
        selected.append((project_root, extra_pack_roots))
        return CustomRegistry()
    monkeypatch.setattr(sdk, "_load_model_registry", load)
    monkeypatch.setattr(_child_bridge, "_bridge", object() if route == "child" else None)
    seen = []
    marker = object()
    def child(_capability, **kwargs):
        seen.append(kwargs["inputs"])
        return marker
    def kernel(_capability, **kwargs):
        seen.append(kwargs["inputs"])
        return "run", "task", "attempt", None, {}, True, None
    monkeypatch.setattr(invocation, "_invoke_bridge_child", child)
    monkeypatch.setattr(invocation, "_kernel_invoke", kernel)
    monkeypatch.setattr(invocation, "_observe_task_invocation", lambda *a, **k: marker)
    result = astrid.invoke("generation.generate_image", kind="executor", project_root=tmp_path,
                           inputs=_request(strength=0.5),
                           **({"project": "demo"} if route == "ordinary" else {}))
    assert result is marker
    assert selected == [(tmp_path, ())]
    assert seen[0]["strength"] == 0.5


def test_allow_dropped_reports_explicit_unsupported_feature(registry):
    inputs = _request(strength=0.5)
    result = invocation.preflight_generation_inputs(
        registry=registry, modality="image", inputs=inputs, explicit_inputs=inputs,
        allow_dropped=True,
    )
    assert any(warning.get("feature") == "strength" for warning in result.warnings)
    # The runner filters it using the existing drop/warning path.
    assert result.inputs["strength"] == 0.5


@pytest.mark.parametrize("content", [b'not json', b'[{"seed": 1}]', b''])
def test_invalid_prompt_content_is_rejected(registry, content):
    inputs = _request(prompt="", prompts_file="prompts.jsonl")
    with pytest.raises(astrid.CapabilityValidationError):
        invocation.preflight_generation_inputs(
            registry=registry, modality="image", inputs=inputs, explicit_inputs=inputs,
            prompt_bytes=content,
        )


def test_prompt_row_features_override_shared_inputs(registry):
    inputs = _request(prompt="", prompts_file="prompts.jsonl", seed=1)
    result = invocation.preflight_generation_inputs(
        registry=registry, modality="image", inputs=inputs, explicit_inputs=inputs,
        prompt_bytes=b'{"prompt":"boat","seed":9}\n',
    )
    assert result.prompt_rows[0]["seed"] == 9
    assert result.prompt_rows[0]["prompt"] == "boat"


def test_preflight_requires_immutable_bytes(registry):
    inputs = _request(prompt="", prompts_file="prompts.jsonl")
    with pytest.raises(astrid.CapabilityValidationError, match="immutable"):
        invocation.preflight_generation_inputs(
            registry=registry, modality="image", inputs=inputs, explicit_inputs=inputs,
            prompt_bytes=bytearray(b'{"prompt":"boat"}\n'),
        )


def test_model_override_revalidates_inherited_features(registry):
    inputs = _request(prompt="", prompts_file="prompts.jsonl", negative_prompt="rain")
    # z-image supports negative_prompt; flux-dev's t2i mode does not.
    with pytest.raises(astrid.CapabilityValidationError, match="negative_prompt"):
        invocation.preflight_generation_inputs(
            registry=registry, modality="image", inputs=inputs, explicit_inputs=inputs,
            prompt_bytes=b'{"prompt":"boat","model":"flux-dev","mode":"t2i"}\n',
        )


def test_execution_request_prompt_file_is_checked_before_admission(monkeypatch, cached_route_discovery):
    admitted = []
    monkeypatch.setattr(_child_bridge, "_bridge", None)
    monkeypatch.setattr(invocation, "_kernel_invoke", lambda *a, **k: admitted.append(True))
    request = {
        "target": {"kind": "default"},
        "inputs": [{"name": "prompts_file", "object_id": "sha256:source",
                    "filename": "prompts.jsonl"}],
    }
    with pytest.raises(astrid.CapabilityPreconditionError, match="prompt-byte snapshot"):
        astrid.invoke("generation.generate_image", kind="executor", project="demo",
                      inputs=_request(), execution_request=request)
    assert admitted == []


def test_child_builds_real_request_with_absent_optional_file_defaults(monkeypatch, cached_route_discovery):
    class RecordingBridge:
        def __init__(self):
            self.requests = []
        def submit(self, request):
            self.requests.append(request)
            return {"run_id": "R", "task_id": "T", "attempt_id": "A"}

    bridge = RecordingBridge()
    monkeypatch.setattr(_child_bridge, "_bridge", bridge)
    result = astrid.invoke("generation.generate_image", kind="executor", inputs=_request(),
                           child_key="generation-child")
    assert result.ok
    assert len(bridge.requests) == 1
    request = bridge.requests[0]
    assert request["inputs"].get("prompts_file") is None
    assert not any(item["name"] == "prompts_file" for item in request["input_descriptors"])
    assert type(request["inputs"]["count"]) is int
    assert request["inputs"]["count"] == 1
    assert request["inputs"].get("seed") is None


def test_child_rejects_explicit_string_count_before_bridge(monkeypatch, cached_route_discovery):
    class RejectUnexpectedSubmit:
        def submit(self, request):
            pytest.fail("invalid explicit count reached child bridge")

    monkeypatch.setattr(_child_bridge, "_bridge", RejectUnexpectedSubmit())
    with pytest.raises(astrid.CapabilityValidationError):
        astrid.invoke("generation.generate_image", kind="executor",
                      inputs=_request(count="1"), child_key="invalid-count")
