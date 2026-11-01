"""Skip the runpod pack tests when runpod_lifecycle isn't installed, except in a required lane."""

from __future__ import annotations

import importlib.util
import os

import pytest

_RUNPOD_LIFECYCLE_AVAILABLE = importlib.util.find_spec("runpod_lifecycle") is not None
_INSTALL = 'pip install -e ".[dev]" (the dev extra pins runpod-lifecycle to a public Git revision)'

# NOTE: do NOT use pytest.importorskip at conftest module scope. When pytest
# loads a conftest during the INITIAL conftest discovery pass, a module-level
# skip/importorskip raises Skipped through the loader and aborts the whole run
# (exit 1, 0 collected) instead of skipping the pack's tests. Defer the skip to
# collection time instead.


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Skip every runpod-pack test when the dependency is absent; fail a required lane (R-6)."""
    if _RUNPOD_LIFECYCLE_AVAILABLE:
        return
    runpod_items = [item for item in items if "runpod" in str(item.path)]
    expression = (config.getoption("markexpr", default="") or "").strip()
    if runpod_items and (bool(os.environ.get("CI")) or ("integration" in expression and "not integration" not in expression)):
        raise pytest.UsageError(f"runpod_lifecycle is a declared dev dependency and is not installed: {_INSTALL}")
    for item in runpod_items:
        item.add_marker(pytest.mark.integration)

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
