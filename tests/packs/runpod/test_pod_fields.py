"""Pod record fields read from the shapes RunPod actually returns."""

import pytest

from astrid.packs.runpod.pod_fields import InvalidPodField, pod_field


def test_read_exact_pod_requests_network_volume(monkeypatch: pytest.MonkeyPatch) -> None:
    """Exact v2 GET projects native mounts into the compatibility volume alias."""
    import httpx
    from astrid.packs.runpod import worker_preparation as wp

    seen = {}

    def fake_get(url, *, headers, timeout, params=None):
        seen["params"] = params
        return httpx.Response(200, json={"id": "pod-exact", "mounts": {"network": [
            {"volumeId": "vol-persist", "path": "/workspace"}]}},
                              request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", fake_get)
    status, record = wp.read_exact_pod("test", "pod-exact")
    assert status == 200 and record["networkVolume"]["id"] == "vol-persist"
    assert seen["params"] is None or "includeNetworkVolume" not in seen["params"]



@pytest.mark.parametrize(("record", "expected"), [
    ({"networkVolumeId": "v1"}, "v1"),
    ({"networkVolume": {"id": "v1", "name": "storage"}}, "v1"),
    ({"networkVolumeId": "v1", "networkVolume": {"id": "v1"}}, "v1"),
    ({"networkVolume": None}, None),
    ({}, None),
])
def test_volume_field_reads_sdk_and_rest_shapes(record, expected):
    assert pod_field(record, "volume") == expected


@pytest.mark.parametrize("record", [
    {"networkVolumeId": "v1", "networkVolume": {"id": "v2"}},
    {"networkVolume": {"name": "no id"}},
    {"networkVolume": "v1"},
    {"imageName": "a", "image": "b"},
])
def test_contradictory_or_malformed_fields_refuse(record):
    field = "image" if "imageName" in record else "volume"
    with pytest.raises(InvalidPodField):
        pod_field(record, field)
