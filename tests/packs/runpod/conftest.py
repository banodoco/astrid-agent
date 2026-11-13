"""Skip the runpod pack tests when runpod_lifecycle isn't installed, except in a required lane."""

from __future__ import annotations

import importlib.util
import os

import pytest

_RUNPOD_LIFECYCLE_AVAILABLE = importlib.util.find_spec("runpod_lifecycle") is not None
_INSTALL = 'pip install -e ".[dev]" (the dev extra pins runpod-lifecycle to a public Git revision)'


@pytest.fixture(autouse=True)
def lease_https_transport(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch):
    """Complete the small lease's fake provider with its HTTPS command edge."""
    if ("small_lease" not in request.fixturenames
            or "retained" in request.fixturenames):
        return

    from runpod_lifecycle import https
    from runpod_lifecycle.config import RunPodConfig

    from astrid.packs.runpod.remote_placement import _HttpsOps

    case = request.getfixturevalue("lease_selection_case")
    config = case.owner.config_factory
    token = "test-only-" + "x" * 40

    def https_config(offer):
        values = vars(config(offer)).copy()
        values["gpu_type"] = values.pop("gpu_type_candidates")
        return RunPodConfig(**values, transport="https", https_token=token)

    case.owner.config_factory = https_config
    # Production writes the token file, reference and watchdog acknowledgement.
    case.owner.https_token_file = case.owner.path.with_name("https-token")

    class Transport:
        def __init__(self, pod_id, https_token, *, deadline=None):
            assert any(pod["id"] == pod_id for pod in case.pods)
            assert https_token == token

        def execute_command(self, command, timeout):
            assert command.startswith("python3 -c ")
            return 0, "", ""

        def disconnect(self):
            pass

    monkeypatch.setattr(https, "HttpsPodTransport", Transport)
    # The fake provider and transport live in this process, as in retained.
    monkeypatch.setattr(_HttpsOps, "_bounded_call", _HttpsOps._call_direct)

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


@pytest.fixture
def real_acquisition_confirmation():
    """Opt out of the autouse stub below: run confirm_acquired_machine for real."""


@pytest.fixture(autouse=True)
def stub_acquisition_graphql_machine(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch):
    """Acquisition confirms from GraphQL pod.machine + the gpuTypes catalog (never REST).

    Unit tests that fake the account inventory (``api.list_pods``) and are not
    about the confirmation get a confirmation that echoes the contract. Tests
    that exercise it request ``real_acquisition_confirmation`` and fake the two
    GraphQL calls themselves.
    """
    if (request.node.get_closest_marker("integration") is not None
            or "real_acquisition_confirmation" in request.fixturenames):
        yield
        return

    from astrid.packs.runpod import worker_preparation

    monkeypatch.setattr(
        worker_preparation, "confirm_acquired_machine",
        lambda api_key, contract, *, timeout_seconds: (contract.gpu_type, contract.datacenter_id),
        raising=False,
    )
    # The recorded one-GPU pods (gpuCount 1); the real reader is exercised by the tests that
    # request real_acquisition_confirmation.
    monkeypatch.setattr(worker_preparation, "read_acquired_gpu_count", lambda api_key, pod_id: 1, raising=False)
    yield
