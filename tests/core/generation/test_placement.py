"""Plan 32 T3: one frozen Placement, resolved request -> user default -> built-in."""
from __future__ import annotations

import dataclasses

import pytest

from astrid.core.contracts.errors import AstridError
from astrid.core.execution.warm_hold import DEFAULT_IDLE_SECONDS
from astrid.core.generation import placement as placements
from astrid.core.generation.placement import Placement, PlacementPolicy

KINDS = ("runpod",)


def _config(tmp_path, text=""):
    path = tmp_path / "config.toml"
    path.write_text(text)
    return path


def test_builtin_is_local_with_default_idle(tmp_path):
    resolved = placements.resolve_placement(None, path=_config(tmp_path))
    assert resolved == Placement(kind="local")
    assert resolved.policy.idle_timeout_s == DEFAULT_IDLE_SECONDS == 1800
    assert resolved.policy.gpu_types == ()


def test_user_default_beats_builtin(tmp_path):
    path = _config(tmp_path, '[placement]\nkind = "attached"\nendpoint = "http://127.0.0.1:8199"\n')
    resolved = placements.resolve_placement(None, path=path)
    assert (resolved.kind, resolved.endpoint) == ("attached", "http://127.0.0.1:8199")


def test_request_beats_user_default(tmp_path):
    path = _config(tmp_path, '[placement]\nkind = "attached"\nendpoint = "http://127.0.0.1:8199"\n')
    resolved = placements.resolve_placement({"kind": "local"}, path=path)
    assert resolved.kind == "local" and resolved.endpoint is None


def test_per_generation_device_override_keeps_default_kind(tmp_path):
    path = _config(tmp_path, '[placement]\nkind = "local"\ndevice = 0\n[placement.policy]\nidle_timeout_s = 60\n')
    resolved = placements.resolve_placement({"device": 1}, path=path)
    assert (resolved.kind, resolved.device) == ("local", 1)
    assert resolved.policy.idle_timeout_s == 60  # untouched fields come from the default


def test_unknown_machine_kind_refused_listing_known_kinds():
    with pytest.raises(AstridError) as caught:
        placements.validate_placement(Placement(kind="nimbus"), machine_kinds=KINDS, live_leases=lambda kind: [])
    text = str(caught.value)
    assert "nimbus" in text and "local" in text and "attached" in text and "runpod" in text


def test_machine_without_live_lease_refused_listing_live_leases():
    placement = Placement(kind="runpod", machine="pod-9")
    with pytest.raises(AstridError) as caught:
        placements.validate_placement(placement, machine_kinds=KINDS,
                                      live_leases=lambda kind: [{"machine": "pod-1"}, {"machine": "pod-2"}])
    assert "pod-9" in str(caught.value) and "pod-1" in str(caught.value) and "pod-2" in str(caught.value)


def test_machine_with_live_lease_is_admitted():
    placement = Placement(kind="runpod", machine="pod-1")
    assert placements.validate_placement(placement, machine_kinds=KINDS,
                                         live_leases=lambda kind: [{"machine": "pod-1"}]) is placement


def test_placement_is_frozen_and_unknown_keys_refused():
    placement = Placement(kind="local")
    with pytest.raises(dataclasses.FrozenInstanceError):
        placement.kind = "attached"  # type: ignore[misc]
    with pytest.raises(AstridError):
        Placement.from_mapping({"kind": "local", "colour": "red"})
    with pytest.raises(AstridError):
        Placement.from_mapping({"kind": "local", "policy": {"warm": True}})


def test_attached_requires_endpoint_and_local_forbids_it():
    with pytest.raises(AstridError):
        placements.validate_placement(Placement(kind="attached"), machine_kinds=KINDS, live_leases=lambda k: [])
    with pytest.raises(AstridError):
        placements.validate_placement(Placement(kind="local", endpoint="http://x:1"),
                                      machine_kinds=KINDS, live_leases=lambda k: [])


def test_policy_is_typed():
    with pytest.raises(AstridError):
        PlacementPolicy.from_mapping({"idle_timeout_s": -1})
    assert PlacementPolicy.from_mapping({"gpu_types": ["A100"]}).gpu_types == ("A100",)
