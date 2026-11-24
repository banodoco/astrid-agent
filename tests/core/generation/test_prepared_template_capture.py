"""The cache observes the existing bounded transport; it never fetches."""
from __future__ import annotations

import io
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from astrid.core.generation import prepared_templates as prepared
from astrid.core.generation.backends.comfy_target import ComfyTarget, ComfyTargetClient
from astrid.sdk.command_timing import TimingCollector, timing_scope


@pytest.fixture(autouse=True)
def no_optional_provenance_probe(monkeypatch):
    monkeypatch.setattr("astrid.core.generation.engine_readiness.current_comfy_provenance", lambda: None)


def test_object_info_retains_exact_raw_response_and_same_return_without_second_fetch():
    raw = b' { "Example": {"input": {}, "output": []} }\r\n'
    requests = []

    def opener(request, **kwargs):
        requests.append((request.method, request.full_url))
        return io.BytesIO(raw)

    client = ComfyTargetClient(ComfyTarget("http://127.0.0.1:8188", "attached"), opener=opener)
    result = client.object_info()
    capture = prepared.current_node_capture(client, result)
    assert result == {"Example": {"input": {}, "output": []}}
    assert capture is not None
    assert capture.observed is result
    assert capture.raw == raw
    assert capture.endpoint == client.http_origin
    assert capture.digest() == prepared._digest(raw)
    assert requests == [("GET", "http://127.0.0.1:8188/object_info")]


@pytest.mark.parametrize("bad", [b"[]", b"not JSON", b"\xff"])
def test_failed_object_info_read_clears_older_raw_capture_and_keeps_refusal(bad):
    replies = iter((b'{"Example":{}}', bad))
    client = ComfyTargetClient(ComfyTarget("http://127.0.0.1:8188", "attached"),
                               opener=lambda *args, **kwargs: io.BytesIO(next(replies)))
    prior = client.object_info()
    assert prepared.current_node_capture(client, prior) is not None
    with pytest.raises(ValueError):
        client.object_info()
    assert client.node_list_capture is None


def test_parsed_only_attested_transport_retains_return_and_reports_unhashable():
    from astrid.core.generation.engine_transport import EngineEndpointTransport
    transport = Mock(spec=EngineEndpointTransport)
    transport.endpoint = SimpleNamespace(url="https://pod.example/engines/owned")
    payload = {"Example": {"input": {}}}
    transport.object_info.return_value = payload
    opener = Mock(side_effect=AssertionError("cache attempted an extra HTTP fetch"))
    client = ComfyTargetClient(ComfyTarget(transport.endpoint.url, "pod"),
                               endpoint_transport=transport, opener=opener)
    # Even an older, real-shaped capture cannot survive a parsed-only read.
    client.node_list_capture = prepared.NodeListCapture(b"{}", {}, "http://old")
    assert client.object_info() is payload
    assert prepared.current_node_capture(client, payload) is None
    session = prepared.PreparationSession()
    graph = object()
    collector = TimingCollector()
    with timing_scope(collector):
        session.prepare_emission(graph, capture=client.node_list_capture, role_plan={})
    assert session.use.workflow is graph
    assert session.use.filled is None
    assert collector.cache_event_snapshot() == [
        {"cache": "prepared_template", "outcome": "miss", "reason": "node_list_unhashable"},
        {"cache": "prepared_template", "outcome": "recompute", "reason": "node_list_unhashable"},
    ]
    transport.object_info.assert_called_once()
    opener.assert_not_called()
