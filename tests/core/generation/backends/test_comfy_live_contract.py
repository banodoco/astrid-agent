"""CPU replay of wire captures from managed ComfyUI 423cbd20, S-45."""
import io
import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from astrid.core.generation.backends.comfy_schema import TargetSchemaProvider
from astrid.core.generation.backends.comfy_target import ComfyTarget, ComfyTargetClient
from vibecomfy.runtime.session import _collect_output_entries, _history_observation, _outputs_from_server_history
from vibecomfy.runtime.watchdog import Watchdog


FIXTURE = Path(__file__).parents[3] / "fixtures/comfyui/live-423cbd20"


def captured(name):
    return json.loads((FIXTURE / name).read_text())


def test_live_response_contract_replay(tmp_path):
    object_info = captured("used-nodes.json")
    history = captured("history.json")
    queue = captured("queue.json")
    stats = captured("system-stats.json")
    free = captured("free-response.json")
    view = captured("view-headers.json")
    prompt_id, entry = next(iter(history.items()))
    requests = []

    class Response(io.BytesIO):
        status = 200

    def opener(request, timeout):
        url = urlsplit(request.full_url)
        requests.append(url)
        if url.path == "/object_info":
            return Response(json.dumps(object_info).encode())
        if url.path == "/system_stats":
            return Response(json.dumps(stats).encode())
        if url.path == "/history/" + prompt_id:
            return Response(json.dumps(history).encode())
        if url.path == "/queue":
            return Response(json.dumps(queue).encode())
        if url.path == "/api/free":
            assert free["status"] == 200
            return Response(free["body"].encode())
        if url.path == "/view":
            assert view["status"] == 200
            return Response(b"synthetic image bytes")
        raise AssertionError(url.path)

    client = ComfyTargetClient(ComfyTarget("http://127.0.0.1:8188", "managed"), opener=opener)
    observed = client.object_info()
    provider = TargetSchemaProvider.from_object_info(observed, server_url=client.http_origin)
    assert provider.object_info() == object_info
    for node in object_info:
        assert provider.get_schema(node) is not None
    assert client.system_stats() == stats
    assert client.request_json("/queue") == queue
    assert client.request_json("/api/free", {"free_memory": True}) == {"acknowledged": True}
    result = client.history(prompt_id)
    assert _history_observation(result, prompt_id)["status"] == entry["status"]
    outputs = _outputs_from_server_history(result, prompt_id)
    records = _collect_output_entries(outputs)
    assert len(records) == 1
    descriptor = records[0][0]
    assert descriptor["metadata"] == {"kind": "image", "width": 512, "height": 512}
    destination = tmp_path / "output.png"
    client.download_output(descriptor, destination)
    assert destination.read_bytes() == b"synthetic image bytes"
    assert view["headers"]["Content-Type"] == "image/png"
    assert parse_qs(requests[-1].query, keep_blank_values=True) == {
        "filename": [descriptor["filename"]], "subfolder": [""], "type": ["output"]}

    # History messages carry actual event payloads; websocket framing itself
    # was not retained by the runner, as recorded in unavailable.json.
    watchdog = Watchdog(server_url=client.http_origin, client_id="contract", prompt_id=prompt_id)
    for kind, data in entry["status"]["messages"]:
        watchdog.feed({"type": kind, "data": data})
    assert watchdog.state.prompt_id == prompt_id
    assert captured("unavailable.json")["prompt_submit_response"]
    assert captured("unavailable.json")["websocket_progress_frames"]
