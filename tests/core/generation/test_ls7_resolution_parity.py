"""RS1.10 format-level parity for the registered data descriptor format.

These synthetic entries exercise every supported scalar type, default/alias/
seed rule, named placement and typed refusal. They reconstruct input/request
preparation from the shared helpers; they do not invoke the Agent's complete
preparation or prove parity for real catalogue entries and admitted records.
RS1.10 remains open for that LS-10 comparison. Native inputs and executable
entries continue to use the full path.
"""
from __future__ import annotations

import copy
from types import SimpleNamespace

import pytest

from astrid.core.generation.entries import prepare_execution_bindings, validate_inputs
from astrid.core.generation.resolution_descriptor import ResolutionUnsupported, prepare_descriptor, registration_descriptors
from astrid.sdk.execution_request import normalize_execution_request
from astrid.sdk.generation import GenerationInputError
from runtime_protocol.resolution import ResolutionRefusal, resolve, validate_descriptor


DIGEST = "sha256:" + "1" * 64
PORTS = [
    {"name": "prompt", "type": "string", "required": True, "aliases": ["text"]},
    {"name": "width", "type": "integer", "default": 512, "min": 1, "max": 2048},
    {"name": "strength", "type": "number", "default": 0.5, "min": 0, "max": 1},
    {"name": "enabled", "type": "boolean", "default": True},
    {"name": "metadata", "type": "object", "default": {}},
    {"name": "tags", "type": "array", "default": []},
    {"name": "extra", "type": "json", "default": {"finite": [1, True, "text"]}},
    {"name": "quality", "type": "string", "default": "standard", "choices": ["standard", "high"]},
]


def declaration(entry_id="scalars"):
    ports = copy.deepcopy(PORTS)
    if entry_id == "seeded":
        ports.append({"name": "seed", "type": "integer"})
    return {
        "entry": {"id": entry_id, "kind": "image", "mode": "t2i", "inputs": ports},
        "admission": {"spec": {"entry": entry_id}, "execution_request": {"target": {"kind": "default"}}},
        "bindings": {p["name"]: ["spec", "inputs", p["name"]] for p in ports},
        "placement_generation": "prepared-placement-1", "project_id": None,
        "placements": {"local": {"kind": "default"}, "machine": {"kind": "machine", "id": "machine-1"}},
        "default_placement": "local",
        **({"seed_parameter": "seed"} if entry_id == "seeded" else {}),
    }


def prepared(value):
    descriptor = prepare_descriptor(value, capability_id="ls7.scalar", capability_digest=DIGEST,
                                    source_digest="captured-source", source_epoch="source-epoch")
    return validate_descriptor(descriptor, {
        "capabilities": [{"capability_id": "ls7.scalar", "definition_digest": DIGEST}],
        "source_digest": "captured-source", "source_epoch": "source-epoch",
    })


@pytest.mark.parametrize("entry_id", ["scalars", "seeded"])
@pytest.mark.parametrize("values", [
    {"prompt": "hello"},
    {"text": "alias", "width": 1024, "strength": 1, "enabled": False,
     "metadata": {"key": [1, 2]}, "tags": ["one"], "extra": "json scalar", "quality": "high"},
])
@pytest.mark.parametrize("placement", ["local", "machine"])
def test_every_registered_entry_matches_full_path_values_and_execution(entry_id, values, placement, monkeypatch):
    source = declaration(entry_id)
    # Randomness is selected independently by both existing full preparation
    # and Runtime resolution, then frozen once in admission/replay tests.
    monkeypatch.setattr("secrets.randbelow", lambda upper: 12345)
    descriptor = prepared(source)
    full_values = validate_inputs(source["entry"], values)
    defaults = {p["name"]: p["default"] for p in source["entry"]["inputs"] if "default" in p}
    full_values = {**defaults, **full_values}
    if entry_id == "seeded":
        full_values = prepare_execution_bindings(SimpleNamespace(inputs={"seed": object()}), full_values)
    full_request = normalize_execution_request({**source["admission"]["execution_request"],
                                                "target": source["placements"][placement]})
    result, actual_values = resolve(descriptor, {"values": values, "placement": placement})
    assert actual_values == full_values
    assert result["spec"] == {"entry": entry_id, "inputs": full_values}
    assert result["execution_request"] == full_request
    assert result["input_object_ids"] == []


@pytest.mark.parametrize("values", [
    {}, {"prompt": " "}, {"prompt": None}, {"prompt": 2},
    {"prompt": "ok", "width": True}, {"prompt": "ok", "width": 0},
    {"prompt": "ok", "width": 4096}, {"prompt": "ok", "strength": "bad"},
    {"prompt": "ok", "enabled": 1}, {"prompt": "ok", "metadata": []},
    {"prompt": "ok", "tags": {}}, {"prompt": "ok", "quality": "unknown"},
    {"prompt": "ok", "unknown": 1}, {"prompt": "one", "text": "two"},
])
@pytest.mark.parametrize("entry_id", ["scalars", "seeded"])
def test_every_registered_entry_preserves_full_typed_refusal(entry_id, values):
    source = declaration(entry_id)
    with pytest.raises(ValueError) as rejected:
        validate_inputs(source["entry"], values)
    full = GenerationInputError(str(rejected.value), entry=source["entry"])
    with pytest.raises(ResolutionRefusal) as error:
        resolve(prepared(source), {"values": values})
    assert error.value.as_dict() == {"code": full.code, "message": str(full), "details": full.details}


def test_explicit_seed_is_authoritative_and_static_prerequisite_refusal_is_exact(monkeypatch):
    source = declaration("seeded")
    monkeypatch.setattr("secrets.randbelow", lambda _: pytest.fail("explicit seed was replaced"))
    _, values = resolve(prepared(source), {"values": {"prompt": "ok", "seed": 0}})
    assert values["seed"] == 0
    from astrid.core.generation.preflight import generation_error
    full = generation_error(ValueError("selected model is unavailable"), entry=source["entry"],
                            code="missing_dependency", details={"field": "model"},
                            fixes=[{"code": "install_model", "message": "Install the selected model", "details": {}}])
    source["refusal"] = {"code": full.code, "message": full.message, "details": {"readiness": full.to_envelope()}}
    with pytest.raises(ResolutionRefusal) as error:
        resolve(prepared(source), {"values": {"prompt": "ok", "seed": 0}})
    assert error.value.as_dict() == source["refusal"]


@pytest.mark.parametrize("change", ["file", "hook", "native", "invalid_default"])
def test_unsupported_preparation_is_not_registered(change):
    source = declaration()
    if change == "file":
        source["entry"]["inputs"][0]["type"] = "file"
    elif change == "hook":
        source["entry"]["hooks_declared"] = ["build"]
    elif change == "native":
        source["entry"]["inputs"][0]["_binding"] = ["node-1", "text"]
    else:
        source["entry"]["inputs"][1]["default"] = False
    record = SimpleNamespace(id="ls7.scalar", capability_digest=DIGEST, ready=True,
                             definition=SimpleNamespace(metadata={"runtime_resolution": source}))
    descriptors, fallback = registration_descriptors([record], {"source_digest": "captured-source", "source_epoch": "source-epoch"})
    assert descriptors == [] and len(fallback) == 1
    assert fallback[0]["capability_id"] == record.id


def test_registration_binds_captured_identity_and_settings_snapshot():
    source = declaration()
    source["settings_snapshot"] = {"actor_id": "caller", "key": "generation", "version": 3, "content": {"defaults": {}}}
    record = SimpleNamespace(id="ls7.scalar", capability_digest=DIGEST, ready=True,
                             definition=SimpleNamespace(metadata={"runtime_resolution": source}))
    descriptors, fallback = registration_descriptors([record], {"source_digest": "captured-source", "source_epoch": "source-epoch"})
    assert fallback == [] and len(descriptors) == 1
    result = descriptors[0]
    assert result["capability_digest"] == DIGEST and result["source_digest"] == "captured-source"
    assert result["settings_ref"] == {"actor": "caller", "key": "generation", "version": 3}
    assert source["entry"]["inputs"] == PORTS
