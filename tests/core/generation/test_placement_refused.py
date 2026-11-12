"""Shared placement refusals and provider extension boundaries."""
import pytest

from astrid.core.generation import engine_contracts as contracts
from astrid.core.generation.engine_contracts import PlacementDescriptor, PlacementRefused, next_delay


def test_refusal_requires_gate():
    with pytest.raises(TypeError):
        PlacementRefused("unconfirmed")


def test_delay_respects_provider_minimum_and_exponential_cap(monkeypatch):
    monkeypatch.setattr(contracts.random, "uniform", lambda *args: 0)
    assert [next_delay(n, None) for n in range(1, 8)] == [5, 10, 20, 40, 60, 60, 60]
    assert next_delay(1, 12) == 12
    assert next_delay(1000000, 90) == 90
    assert next_delay(1000000, 1) == 60


def test_delay_adds_bounded_jitter(monkeypatch):
    monkeypatch.setattr(contracts.random, "uniform", lambda *args: 0.25)
    assert next_delay(1, 12) == 12.25


@pytest.mark.parametrize("kind", ["local", "runpod"])
def test_builtin_engine_placement_is_accepted(kind):
    assert PlacementDescriptor("astrid.generation-placement.v1", (kind,), kind, None, False).default == kind


def test_unregistered_engine_placement_is_refused():
    with pytest.raises(ValueError, match="invalid placement"):
        PlacementDescriptor("astrid.generation-placement.v1", ("unregistered",), "unregistered", None, False)


def test_registered_provider_needs_no_core_change(monkeypatch):
    from astrid.core.generation import placement
    monkeypatch.setattr(placement, "registered_machine_kinds", lambda: ("second-provider",))
    assert PlacementDescriptor("astrid.generation-placement.v1", ("second-provider",),
                               "second-provider", None, False).default == "second-provider"
