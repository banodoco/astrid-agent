"""The same HTTP client, with target-specific checked output custody.

Reuse the real local TLS agent/proxy fixture: pod transfers exercise the
existing attested EngineEndpointTransport, not a mock /view replacement.
"""
from __future__ import annotations

import hashlib
import pytest
from aiohttp import web

from astrid.core.generation.backends.comfy_target import ComfyTarget, ComfyTargetClient
from astrid.core.generation.engine_transport import EngineEndpointTransport
from astrid.core.generation.engine_contracts import TransportError
from tests.core.generation.test_engine_endpoint_transport import (
    assert_only_claimed_egress, endpoint, local_tls, network,  # noqa: F401
)


PROMPT = {"prompt": {"1": {"class_type": "Example", "inputs": {}}}}
DESCRIPTOR = {"filename": "result.png", "subfolder": "renders", "type": "output"}


@pytest.fixture
def engine_configuration(tmp_path):
    root = tmp_path / "pod-input" / "astrid" / "task-1"
    root.mkdir(parents=True)
    return {"input_files": [str(root / "source.png")],
            "output_roots": [str(tmp_path / "pod-output")]}


@pytest.fixture
def observation_configuration():
    return None


@pytest.fixture
def comfy(network):
    state = {"prompts": [], "mode": "success", "upload": None, "controls": [], "running": "unrelated"}

    async def handler(request):
        if request.path == "/upload/image":
            state["upload"] = await request.read()
            return web.json_response({"name": "source.png", "subfolder": "astrid/task-1", "type": "input"})
        if request.path == "/prompt":
            state["prompts"].append(await request.json())
            if state["mode"] == "lost-reply":
                request.transport.close()
                return web.Response()
            if state["mode"] == "bad-acceptance":
                return web.json_response({"number": 1})
            return web.json_response({"prompt_id": "ours"})
        if request.path.startswith("/history/"):
            return web.json_response({"ours": {"outputs": {"1": {"images": [DESCRIPTOR]}}}})
        if request.path in {"/queue", "/interrupt"}:
            if request.method == "POST":
                state["controls"].append((request.path, await request.json()))
                return web.json_response({})
            return web.json_response({"queue_running": [[0, state["running"]]], "queue_pending": []})
        return None

    network["handler"] = handler
    return state


def attached(network):
    # No output-directory fact or shared filesystem accessor is supplied.
    return ComfyTargetClient(ComfyTarget(f"http://127.0.0.1:{network['port']}", "attached"))


def pod_client(transport, tmp_path):
    return ComfyTargetClient(ComfyTarget(transport.endpoint.url, "pod", {
        "input_directory": str(tmp_path / "pod-input"),
        "output_directory": str(tmp_path / "pod-output"),
    }), endpoint_transport=transport)


def exercise(client, source, destination, expected):
    reference = client.upload_media(source, task_id="task-1", expected_sha256=hashlib.sha256(source.read_bytes()).hexdigest())
    assert reference == "astrid/task-1/source.png"
    acceptance = client.submit_prompt(PROMPT)
    assert acceptance["prompt_id"] == "ours"
    history = client.history(acceptance["prompt_id"])
    descriptor = history["ours"]["outputs"]["1"]["images"][0]
    client.download_output(descriptor, destination)
    assert destination.read_bytes() == expected
    client.cancel_prompt("ours")


@pytest.mark.parametrize("kind", ["attached", "pod"])
def test_same_client_upload_prompt_history_and_custody(kind, comfy, endpoint, local_tls, network, tmp_path):
    source = tmp_path / "source.png"
    source.write_bytes(b"synthetic image input")
    destination = tmp_path / "custody.png"
    if kind == "attached":
        client = attached(network)
        assert "output_directory" not in client.target.facts
        exercise(client, source, destination, network["image"])
        assert source.read_bytes() in comfy["upload"]
        assert any(path.startswith("/view?") for _, path, _ in network["upstream"])
    else:
        output = tmp_path / "pod-output" / "renders" / "result.png"
        output.parent.mkdir(parents=True)
        output.write_bytes(network["image"])
        with EngineEndpointTransport(endpoint, pod_id="pod123", agent_port=8000) as transport:
            client = pod_client(transport, tmp_path)
            exercise(client, source, destination, network["image"])
        assert (tmp_path / "pod-input" / "astrid" / "task-1" / "source.png").read_bytes() == source.read_bytes()
        paths = [path for _, path, _ in network["exchanges"]]
        assert "/uploads" in paths and paths.count("/download-chunk") >= 2
        assert not any("/view" in path or "/upload/image" in path for path in paths)
        assert_only_claimed_egress(network)
    assert comfy["prompts"] == [PROMPT]
    posts = [(path, headers) for method, path, headers in network["upstream"] if method == "POST"]
    assert all(path in {"/upload/image", "/prompt", "/queue"} for path, _ in posts)
    assert comfy["controls"] == [("/queue", {"delete": ["ours"]})]
    assert not list(tmp_path.glob(".checkout-download-*"))


@pytest.mark.parametrize("path,payload", [
    ("/api/free", {}), ("/interrupt", {}), ("/queue", {"clear": True}),
    ("/queue", {"delete": ["unrelated"]}),
    ("/prompt/../api/free", {}), ("//example.test/prompt", {}),
])
def test_attached_client_denies_server_lifecycle_and_unscoped_controls(path, payload, comfy, network):
    client = attached(network)
    with pytest.raises(ValueError):
        client.request_json(path, payload)
    with pytest.raises(ValueError):
        client.cancel_prompt("unrelated")
    assert network["upstream"] == []


@pytest.mark.parametrize("kind", ["attached", "pod"])
@pytest.mark.parametrize("mode", ["lost-reply", "bad-acceptance"])
def test_uncertain_prompt_acceptance_is_not_replayed(kind, mode, comfy, endpoint, local_tls, network, tmp_path):
    comfy["mode"] = mode
    with EngineEndpointTransport(endpoint, pod_id="pod123", agent_port=8000) as transport:
        client = attached(network) if kind == "attached" else pod_client(transport, tmp_path)
        with pytest.raises(TransportError) as error:
            client.submit_prompt(PROMPT)
        assert getattr(error.value, "acceptance_known", None) is False
        with pytest.raises(TransportError):
            client.submit_prompt(PROMPT)
        with pytest.raises(ValueError):
            client.cancel_prompt("guessed-prompt")
    assert comfy["prompts"] == [PROMPT]
    assert not any(path == "/interrupt" for _, path, _ in network["upstream"])


def test_attached_interrupt_is_scoped_to_its_witnessed_running_prompt(comfy, network):
    client = attached(network)
    client.submit_prompt(PROMPT)
    comfy["running"] = "ours"
    client.cancel_prompt("ours")
    assert comfy["controls"] == [
        ("/queue", {"delete": ["ours"]}), ("/interrupt", {"prompt_id": "ours"}),
    ]


def test_local_request_compatibility_uses_guarded_prompt_submission(comfy, network):
    from astrid.core.generation.backends.vibecomfy_local_server import _request

    url = f"http://127.0.0.1:{network['port']}"
    assert _request(url, "/prompt", PROMPT)["prompt_id"] == "ours"
    assert _request(url, "/history/ours")["ours"]["outputs"]
    assert comfy["prompts"] == [PROMPT]
    assert comfy["controls"] == []


def test_json_response_limit_is_enforced(comfy, network):
    with pytest.raises(ValueError, match="too large"):
        attached(network).request_json("/queue", response_limit=8)


def test_output_response_limit_does_not_publish(comfy, network, tmp_path, monkeypatch):
    monkeypatch.setattr("astrid.core.generation.backends.comfy_target.MAX_OUTPUT_BYTES", 16)
    destination = tmp_path / "result.png"
    with pytest.raises(ValueError, match="too large"):
        attached(network).download_output(DESCRIPTOR, destination)
    assert not destination.exists()
    assert not list(tmp_path.glob(".checkout-download-*"))


@pytest.mark.parametrize("path", ["/prompt/../prompt", "/api/prompt", "/other", "/free"])
def test_managed_raw_controls_cannot_bypass_prompt_guard(path, comfy, network):
    client = ComfyTargetClient(ComfyTarget(f"http://127.0.0.1:{network['port']}", "managed"))
    with pytest.raises(ValueError):
        client.request_json(path, PROMPT)
    assert network["upstream"] == []


def test_attached_custody_digest_mismatch_does_not_publish(comfy, network, tmp_path):
    destination = tmp_path / "result.png"
    destination.write_bytes(b"prior custody")
    with pytest.raises(ValueError):
        attached(network).download_output(DESCRIPTOR, destination, expected_sha256="0" * 64)
    assert destination.read_bytes() == b"prior custody"
    assert not list(tmp_path.glob(".checkout-download-*"))


def test_pod_corrupt_chunks_do_not_publish(comfy, endpoint, local_tls, network, tmp_path):
    from runpod_lifecycle.https import PodTransportError

    output = tmp_path / "pod-output" / "renders" / "result.png"
    output.parent.mkdir(parents=True)
    output.write_bytes(network["image"])
    network["corrupt_digest"] = True
    destination = tmp_path / "result.png"
    with EngineEndpointTransport(endpoint, pod_id="pod123", agent_port=8000) as transport:
        with pytest.raises(PodTransportError, match="invalid download chunk"):
            pod_client(transport, tmp_path).download_output(DESCRIPTOR, destination)
    assert not destination.exists()
    assert not any("/view" in path for _, path, _ in network["exchanges"])


@pytest.mark.parametrize("descriptor", [
    {**DESCRIPTOR, "filename": "../result.png"},
    {**DESCRIPTOR, "subfolder": "../input"},
    {**DESCRIPTOR, "type": "input"},
    {**DESCRIPTOR, "filename": "C:\\result.png"},
    {**DESCRIPTOR, "unknown": "unsafe"},
])
def test_unsafe_output_descriptor_is_rejected_before_http(descriptor, comfy, network, tmp_path):
    with pytest.raises(ValueError):
        attached(network).download_output(descriptor, tmp_path / "result.png")
    assert network["upstream"] == []


def test_attached_redirect_is_not_followed(comfy, network):
    network["redirect"] = f"http://127.0.0.1:{network['port']}/recipient"
    with pytest.raises(ValueError, match="redirect"):
        attached(network).history("ours")
    assert network["recipient"] == []
    assert len(network["upstream"]) == 1


@pytest.mark.parametrize("url", ["http://192.168.1.1:8188", "https://127.0.0.1:8188", "http://127.0.0.1:8188/path"])
def test_attached_target_rejects_non_loopback_origins(url):
    with pytest.raises(ValueError):
        ComfyTargetClient(ComfyTarget(url, "attached"))


def test_pod_target_requires_attested_transport(endpoint, network, tmp_path):
    with pytest.raises(ValueError):
        ComfyTargetClient(ComfyTarget(endpoint.url, "pod"))
    with EngineEndpointTransport(endpoint, pod_id="pod123", agent_port=8000) as transport:
        with pytest.raises(ValueError):
            ComfyTargetClient(ComfyTarget(endpoint.url.replace("/single", "/other"), "pod"), endpoint_transport=transport)
    assert network["upstream"] == []
