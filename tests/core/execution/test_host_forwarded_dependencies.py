"""Host helper dependencies remain replaceable after extraction."""

import pytest

from astrid.core.execution import generic_host
from tests.core.execution.test_generic_host_contract import _task


class FalseCallable:
    def __init__(self, function):
        self.function = function
        self.calls = []

    def __bool__(self):
        return False

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.function(*args, **kwargs)


def test_managed_asset_identity_uses_current_host_digest(monkeypatch):
    resolver = FalseCallable(lambda raw: "f" * 64)
    monkeypatch.setattr(generic_host, "managed_asset_digest", resolver)
    asset = {"object_id": "opaque"}
    assert generic_host._managed_asset_identity("asset", asset) == ("opaque", "f" * 64)
    assert resolver.calls == [((asset,), {})]


def test_task_selector_uses_false_valued_host_spec_resolver(monkeypatch):
    resolver = FalseCallable(lambda task: {"inputs": {"selector": "chosen"}})
    monkeypatch.setattr(generic_host, "_admitted_task_spec", resolver)
    assert generic_host._task_selector({}) == "chosen"
    assert len(resolver.calls) == 1


def test_admitted_task_spec_uses_false_valued_host_envelope_resolver(monkeypatch):
    resolver = FalseCallable(lambda spec: {"params": {"selector": "chosen"}})
    monkeypatch.setattr(generic_host, "_admitted_spec_envelope", resolver)
    assert generic_host._admitted_task_spec({"spec": {}}) == {"params": {"selector": "chosen"}}
    assert len(resolver.calls) == 1


@pytest.mark.parametrize("name", [
    "_admitted_spec_envelope",
    "_assert_verified_placement_binding",
    "_canonical_digest",
    "normalize_execution_request",
])
def test_execution_contract_uses_false_valued_host_dependency(monkeypatch, name):
    resolver = FalseCallable(getattr(generic_host, name))
    monkeypatch.setattr(generic_host, name, resolver)
    task = _task()["task"]
    assert generic_host._execution_contract(task) == task["spec"]["execution_request"]
    assert resolver.calls
