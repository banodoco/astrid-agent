"""Structural object-info fingerprints keep runtime picker drift out of custody."""
from copy import deepcopy
from types import SimpleNamespace
import time

import pytest

from astrid.core.generation.engine_contracts import EndpointHealthError
from astrid.core.generation.engine_endpoint import _probe_prepared_endpoint
from astrid.core.generation.engine_runner import _object_info_sha256, _sha
from astrid.core.generation.engine_runner_target import _health_facts


FILE_PICKERS = [
    ("LoadImage", "image"),
    ("LoadImageMask", "image"),
    ("LoadVideo", "video"),
    ("LoadVideo", "file"),
    ("VHS_LoadVideo", "video"),
    ("VHS_LoadVideo", "file"),
]


def schema(class_type="LoadImage", input_name="image"):
    return {
        class_type: {
            "input": {"required": {
                input_name: [["old.png"], {"image_upload": True}],
                "mode": [["alpha", "red"]],
            }, "optional": {"steps": ["INT", {"min": 1, "max": 10}]}},
            "output": ["IMAGE"],
            "output_is_list": [False],
        }
    }


@pytest.mark.parametrize("class_type,input_name", FILE_PICKERS)
@pytest.mark.parametrize("requiredness", ["required", "optional"])
def test_file_picker_choices_change_without_changing_fingerprint(class_type, input_name, requiredness):
    original = schema(class_type, input_name)
    if requiredness == "optional":
        original[class_type]["input"]["optional"][input_name] = original[class_type]["input"]["required"].pop(input_name)
    snapshot = deepcopy(original)
    changed = deepcopy(original)
    changed[class_type]["input"][requiredness][input_name][0] = ["new.png", "other.png"]
    assert _object_info_sha256(original) == _object_info_sha256(changed)
    assert original == snapshot
    assert _health_facts({}, original, [class_type])["object_info_sha256"] == _health_facts({}, changed, [class_type])["object_info_sha256"]


@pytest.mark.parametrize("change", ["node", "input", "requiredness", "input_type", "output", "constraint", "semantic_choices", "model_choices", "picker_options"])
def test_meaningful_schema_changes_change_fingerprint(change):
    original = schema()
    original["UNETLoader"] = {"input": {"required": {"unet_name": [["one.safetensors"]]}}, "output": ["MODEL"]}
    changed = deepcopy(original)
    node = changed["LoadImage"]
    if change == "node":
        changed["AnotherNode"] = changed.pop("LoadImage")
    elif change == "input":
        node["input"]["required"]["renamed"] = node["input"]["required"].pop("image")
    elif change == "requiredness":
        node["input"]["optional"]["image"] = node["input"]["required"].pop("image")
    elif change == "input_type":
        node["input"]["required"]["image"][0] = "STRING"
    elif change == "output":
        node["output"][0] = "MASK"
    elif change == "constraint":
        node["input"]["optional"]["steps"][1]["max"] = 11
    elif change == "semantic_choices":
        node["input"]["required"]["mode"][0].append("blue")
    elif change == "model_choices":
        changed["UNETLoader"]["input"]["required"]["unet_name"][0].append("two.safetensors")
    else:
        node["input"]["required"]["image"][1]["image_upload"] = False
    assert _object_info_sha256(original) != _object_info_sha256(changed)


@pytest.mark.parametrize("spec", [None, [], "STRING", ["STRING"], [[1, 2]], [["file.png", 1]]])
def test_malformed_or_non_choice_specs_are_not_normalized(spec):
    value = schema()
    value["LoadImage"]["input"]["required"]["image"] = spec
    assert _object_info_sha256(value) == _sha(value)


def test_prepared_endpoint_accepts_file_choice_drift_but_rejects_schema_drift(monkeypatch):
    from astrid.core.generation import engine_transport

    original = schema()
    current = deepcopy(original)
    current["LoadImage"]["input"]["required"]["image"][0] = ["new.png"]
    endpoint = SimpleNamespace(attestation=SimpleNamespace(pod_id="synthetic", object_info_sha256=_object_info_sha256(original)))
    recipe = SimpleNamespace(health=SimpleNamespace(required_nodes=["LoadImage"]))

    class Transport:
        def __init__(self, *args, **kwargs):
            pass

        def object_info(self, *, deadline):
            return current

        def close(self):
            pass

    monkeypatch.setattr(engine_transport, "EngineEndpointTransport", Transport)
    _probe_prepared_endpoint(endpoint, recipe, agent_port=1, deadline=time.monotonic() + 10)
    current["LoadImage"]["output"] = ["MASK"]
    with pytest.raises(EndpointHealthError):
        _probe_prepared_endpoint(endpoint, recipe, agent_port=1, deadline=time.monotonic() + 10)
