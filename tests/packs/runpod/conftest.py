"""Keep RunPod unit tests from reaching the provider GPU-rate API."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def forbid_provider_gpu_rate_lookup(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch):
    """Guard every unit test in this package; integration tests keep their lane."""
    if request.node.get_closest_marker("integration") is not None:
        yield
        return

    from runpod_lifecycle import api as runpod_api
    from astrid.packs.runpod.shared import common

    attempts: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def isolated_gpu_rate_lookup(gpu_type: object, *args: object, **kwargs: object) -> dict[str, float]:
        # Keep tests that exercise cost bookkeeping on the existing exact local
        # values. Unknown names fail closed and are checked again at teardown.
        if isinstance(gpu_type, str):
            rate = common._PRICING_TABLE.get(gpu_type)
            if rate is not None:
                return {"securePrice": rate}
        attempts.append(((gpu_type, *args), kwargs))
        raise AssertionError("provider GPU-rate lookup is forbidden in RunPod unit tests")

    monkeypatch.setattr(runpod_api, "find_gpu_type", isolated_gpu_rate_lookup)
    yield

    # _get_hourly_rate intentionally catches provider exceptions and falls back
    # to its local pricing table, so the independent ledger must fail teardown.
    assert not attempts, (
        f"{request.node.nodeid} attempted {len(attempts)} provider GPU-rate lookup(s); "
        "RunPod unit tests must use local rate behavior"
    )
