"""Pure placement metadata validation; no recipe or provider lookup."""
from dataclasses import FrozenInstanceError
import json

import pytest

from astrid.core.generation.engine_contracts import PlacementDescriptor, UnsupportedPlacement


VALID = {
    "schema": "astrid.generation-placement.v1",
    "supported": ["local", "runpod"],
    "default": "runpod",
    "engine": "vibecomfy-h3",
    "requires_endpoint": True,
}


def test_roundtrip_frozen_descriptor_and_json_arrays():
    descriptor = PlacementDescriptor.from_dict(VALID)
    assert descriptor.supported == ("local", "runpod")
    assert descriptor.to_dict() == VALID
    assert PlacementDescriptor.from_dict(json.loads(json.dumps(descriptor.to_dict()))) == descriptor
    with pytest.raises(FrozenInstanceError):
        descriptor.default = "local"
    data = descriptor.to_dict()
    data["supported"].clear()
    assert descriptor.supported == ("local", "runpod")


@pytest.mark.parametrize("changes", [
    {"schema": "astrid.generation-placement.v2"}, {"schema": True},
    {"schema": None}, {"supported": []}, {"supported": ["local", "local"]},
    {"supported": ["local", "remote"]}, {"supported": [True]},
    {"supported": [1]}, {"supported": [None]}, {"supported": [["local"]]},
    {"supported": "local"}, {"supported": ("local",)}, {"supported": None},
    {"default": True}, {"default": 1}, {"default": None},
    {"default": "remote"}, {"supported": ["local"]},
    {"engine": ""}, {"engine": "Bad-engine"}, {"engine": "a_b"},
    {"engine": "a\n"}, {"engine": True}, {"engine": 1}, {"engine": []},
    {"engine": None}, {"requires_endpoint": 0}, {"requires_endpoint": 1},
    {"requires_endpoint": "false"}, {"requires_endpoint": None},
    {"extra": False},
])
def test_rejects_malformed_wire_values(changes):
    with pytest.raises(ValueError):
        PlacementDescriptor.from_dict({**VALID, **changes})


@pytest.mark.parametrize("key", VALID)
def test_rejects_missing_fields_in_explicit_descriptor(key):
    data = dict(VALID)
    del data[key]
    with pytest.raises(ValueError):
        PlacementDescriptor.from_dict(data)


@pytest.mark.parametrize("value", [None, False, 1, [], "local"])
def test_rejects_non_object_descriptor(value):
    with pytest.raises(ValueError, match="object"):
        PlacementDescriptor.from_dict(value)


@pytest.mark.parametrize("changes", [
    {"supported": ["local"]}, {"supported": ()},
    {"supported": (True,)}, {"supported": ("local", "local")},
    {"default": False}, {"engine": True}, {"requires_endpoint": 1},
])
def test_direct_constructor_also_validates(changes):
    kwargs = {**VALID, "supported": ("local", "runpod"), **changes}
    with pytest.raises(ValueError):
        PlacementDescriptor(**kwargs)


@pytest.mark.parametrize("supported,default,engine,requires_endpoint", [
    (("local",), "local", None, False),
    (("runpod",), "runpod", "unindexed-engine", True),
    (("local", "runpod"), "local", "vibecomfy-h3", True),
    (("local", "runpod"), "runpod", None, False),
    (("local",), "local", "vibecomfy-h3", False),
])
def test_valid_combinations_do_not_require_a_recipe(supported, default, engine, requires_endpoint):
    descriptor = PlacementDescriptor("astrid.generation-placement.v1", supported, default, engine, requires_endpoint)
    assert descriptor.resolve() == default
    assert all(descriptor.resolve(value) == value for value in supported)


@pytest.mark.parametrize("value", ["runpod", "remote", True, 1, [], {}])
def test_local_descriptor_rejects_unsupported_choice_with_stable_error(value):
    descriptor = PlacementDescriptor("astrid.generation-placement.v1", ("local",), "local", None, False)
    with pytest.raises(UnsupportedPlacement) as caught:
        descriptor.resolve(value)
    assert caught.value.code == "unsupported_placement"
    assert caught.value.retryable is False
    assert caught.value.recovery_handle is None


def test_descriptor_public_identity_and_subclass_roundtrip():
    import pickle

    from astrid.core.generation.backends import registry
    from astrid.core.generation import engine_contracts

    assert registry.PlacementDescriptor is engine_contracts.PlacementDescriptor is PlacementDescriptor
    assert PlacementDescriptor.__module__ == "astrid.core.generation.engine_contracts"
    descriptor = PlacementDescriptor.from_dict(VALID)
    assert type(pickle.loads(pickle.dumps(descriptor))) is PlacementDescriptor

    class CustomPlacement(PlacementDescriptor):
        pass

    assert type(CustomPlacement.from_dict(VALID)) is CustomPlacement


@pytest.mark.parametrize("placement", [
    VALID,
    {**VALID, "schema": "invalid"},
    {**VALID, "supported": ["local", "local"]},
    {**VALID, "supported": "local"},
    {**VALID, "default": "remote"},
    {**VALID, "engine": None},
    {**VALID, "requires_endpoint": 1},
    {**VALID, "extra": False},
    None,
])
def test_pack_normalization_matches_descriptor_and_preserves_errors(placement):
    from astrid.core.pack import PackValidationError
    from astrid.core.pack.permissions import _normalize_generation_backends

    backend = {"id": "probe", "module": "example", "class": "Adapter", "placement": placement}
    try:
        descriptor = PlacementDescriptor.from_dict(placement)
    except ValueError as expected:
        with pytest.raises(PackValidationError) as caught:
            _normalize_generation_backends([backend], path="extensions.generation.backends")
        assert str(caught.value) == f"extensions.generation.backends[0].placement: {expected}"
        assert type(caught.value.__cause__) is ValueError
        assert str(caught.value.__cause__) == str(expected)
    else:
        normalized = _normalize_generation_backends([backend], path="extensions.generation.backends")
        assert normalized[0]["placement"] == descriptor.to_dict()
