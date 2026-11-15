"""Contract checks against recorded provider projections."""

import copy
import json
from pathlib import Path

import pytest

from astrid.core.generation.machine_provider import MachineRecord
from astrid.packs.runpod.provider import parse_rest_list_item, parse_rest_pod, parse_sdk_pod


FIXTURES = Path(__file__).parent / "runpod" / "fixtures"


def _load(name):
    return json.loads((FIXTURES / name).read_text())


# Add each provider's recorded projections here as providers are registered.
PROVIDERS = {
    "runpod": [
        (parse_rest_pod, _load("rest-get-pod-default.json")),
        (parse_rest_pod, _load("rest-get-pod-include-network-volume.json")),
        (parse_rest_list_item, _load("rest-list-pods-include-network-volume.json")["pods"][0]),
        (parse_sdk_pod, _load("sdk-graphql-myself-pods.json")["data"]["myself"]["pods"][0]),
    ],
}


@pytest.mark.parametrize("kind", PROVIDERS)
def test_recorded_provider_contract(kind):
    for parse, fixture in PROVIDERS[kind]:
        assert isinstance(parse(fixture, strict=True), MachineRecord)
        unknown = {**fixture, "unexpected_key": True}
        with pytest.raises(ValueError):
            parse(unknown, strict=True)
        conflicting = copy.deepcopy(fixture)
        conflicting.update(networkVolumeId="one", networkVolume={"id": "two"})
        with pytest.raises(ValueError):
            parse(conflicting)


def test_sdk_projection_does_not_observe_storage():
    """Tonight's failure 1: the GraphQL pod record carries no attachment at all.

    That is "not observed", never "no volume": a consumer that needs storage must
    refuse to decide from this shape instead of treating it as unattached.
    """
    from astrid.core.generation.machine_provider import FieldNotObserved

    record = parse_sdk_pod(_load("sdk-graphql-myself-pods.json")["data"]["myself"]["pods"][0], strict=True)
    assert "storage_id" not in record.observed
    with pytest.raises(FieldNotObserved):
        record.require("storage_id")


@pytest.mark.parametrize("name", ["rest-get-pod-default.json", "rest-get-pod-include-network-volume.json"])
def test_rest_projections_observe_storage(name):
    record = parse_rest_pod(_load(name), strict=True)
    assert record.require("storage_id") == "VOLUME_ID"


def test_machine_record_requires_an_id():
    fixture = dict(_load("rest-get-pod-default.json"), id=None)
    with pytest.raises(ValueError):
        parse_rest_pod(fixture)


def test_runpod_read_uses_enriched_exact_rest_and_only_404_is_absence(monkeypatch):
    import httpx
    from astrid.packs.runpod.provider import RunPodMachineProvider

    fixture = _load("rest-get-pod-include-network-volume.json")
    seen = []
    status = 200

    class Response:
        @property
        def status_code(self):
            return status

        def json(self):
            return fixture

    def get(url, **kwargs):
        seen.append((url, kwargs))
        return Response()

    monkeypatch.setattr(httpx, "get", get)
    provider = RunPodMachineProvider("test-key")
    assert provider.read("POD_ID").storage_id == "VOLUME_ID"
    assert len(seen) == 1
    assert seen[0][0].endswith("/v2/pods/POD_ID")
    assert "includeNetworkVolume" not in seen[0][1].get("params", {})
    assert seen[0][0] == "https://api.runpod.io/v2/pods/POD_ID"
    assert seen[0][1]["headers"] == {"Authorization": "Bearer test-key"}
    status = 404
    assert provider.read("POD_ID") is None
    status = 503
    with pytest.raises(ValueError):
        provider.read("POD_ID")


@pytest.mark.parametrize("status,expected", [("RUNNING", "running"), ("EXITED", "stopped"),
                                              ("TERMINATED", "gone"), ("STOPPED", "stopped"),
                                              ("STARTING", "starting")])
def test_runpod_status_mapping(status, expected):
    record = _load("rest-get-pod-default.json")
    record["status"] = status
    assert parse_rest_pod(record).state == expected
