"""HM2 R09: parent authority expiry ends a metadata collection permanently."""

from __future__ import annotations

import pytest

from astrid.core.execution.generic_host import HostError
from tests.core.execution.test_generic_host_child_bridge_d18 import discovery_collection_fixture


@pytest.mark.parametrize("expiry_point", ["before_collection", "during_output_readback"])
def test_r09_parent_lease_expiry_latches_collection_before_any_objects(expiry_point, monkeypatch):
    """R09: collect_metadata rechecks its parent before returning historical outputs."""
    case = discovery_collection_fixture()
    transport = case.client.generated._transport

    def expire_during_readback(method, key, headers, body):
        response = transport(method, key, headers, body)
        if expiry_point == "during_output_readback" and key == ("get_managed_output", "association"):
            case.parent["lease_expires_at"] = "2000-01-01T00:00:00+00:00"
        return response

    monkeypatch.setattr(case.client.generated, "_transport", expire_during_readback)
    reader = case.collection()
    if expiry_point == "before_collection":
        case.parent["lease_expires_at"] = "2000-01-01T00:00:00+00:00"
    with pytest.raises(HostError, match="discovery parent lease ended"):
        reader.collect_metadata()
    assert reader.failed is True
    assert reader.rejected_read["reason"] == "discovery parent lease ended"
    expected_calls = [("get_task", "parent-task")]
    if expiry_point == "during_output_readback":
        expected_calls += [
            ("get_project", "P"), ("get_run", "historical-run"),
            ("get_task", "historical-task"), ("list_managed_outputs", "historical-task"),
            ("get_managed_output", "association"), ("get_task", "parent-task"),
        ]
    assert case.calls == expected_calls
    assert reader.read_attempts == len(expected_calls)
    with pytest.raises(HostError, match="already ended"):
        reader.collect_metadata()
    with pytest.raises(HostError, match="already ended"):
        reader.prepare_objects(object())
    assert case.calls == expected_calls
    assert reader.acquired_object_bytes == 0
