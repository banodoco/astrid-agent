"""Plan 32 T2a: the generation.engine point and its single ComfyUI contribution load through the registry."""

from __future__ import annotations

import inspect

import pytest

from astrid.core.extensions import registry as reg
from astrid.core.extensions.registry import ExtensionRegistry

REQUIRED = ("requirements", "align", "execute")


@pytest.fixture
def registry(monkeypatch: pytest.MonkeyPatch) -> ExtensionRegistry:
    monkeypatch.setattr(reg, "disabled_ids", lambda: frozenset())
    return ExtensionRegistry().load()


def test_generation_engine_point_is_declared_on_the_agent_side(registry: ExtensionRegistry) -> None:
    point = next(p for p in registry.points() if p.id == "generation.engine")
    assert point.side == "agent"
    assert point.protocol is not None
    assert not [p for p in registry.problems() if "generation.engine" in str(getattr(p, "id", ""))]


def test_registry_resolves_comfyui_engine_and_checks_the_protocol(
    registry: ExtensionRegistry,
) -> None:
    contribution = registry.resolve("generation.engine", "comfyui")
    assert contribution.id == "vibecomfy.comfyui"
    engine = registry.entry(contribution)  # runs the protocol check; raises if a method is missing
    for name in REQUIRED:
        assert callable(getattr(engine, name)), name
    params = {n: list(inspect.signature(getattr(engine, n)).parameters) for n in REQUIRED}
    assert params["requirements"] == ["entry", "inputs"]
    assert params["align"] == ["manifest", "mode"]
    assert params["execute"] == ["job"]


def test_probe_is_optional_but_checked_when_present(
    registry: ExtensionRegistry,
) -> None:
    point = next(p for p in registry.points() if p.id == "generation.engine")

    class _Req:
        def requirements(self, entry, inputs): ...
        def align(self, manifest, mode): ...
        def execute(self, job): ...

    registry._check_protocol(point, _Req())  # no probe: still valid

    class _BadProbe(_Req):
        def probe(self): ...

    with pytest.raises(reg._LoadFailure):
        registry._check_protocol(point, _BadProbe())
