"""Plan 56 CPU stand-in: exact owner API graph, no weights or paid resources."""
from __future__ import annotations

import json

import pytest

# plans/56-workflow-setup/evidence/sam-test-files/sam-audio-separation.api.json
# source blob 13895ffee92f7bd50e04ff5973253d961f18c87e.
SAM_API = {
    "1": {"class_type": "SAMAudioPipelineLoader", "_meta": {"title": "SAM-Audio Model Loader"},
          "inputs": {"model_name": "facebook/sam-audio-large", "attention_backend": "pytorch",
                     "precision": "auto", "initial_device": "cpu"}},
    "2": {"class_type": "LoadAudio", "_meta": {"title": "Load Audio (set to the clip's audio file)"},
          "inputs": {"audio": "input.wav"}},
    "3": {"class_type": "SAMAudioTextSeparator", "_meta": {"title": "SAM-Audio Text Separate (set description)"},
          "inputs": {"pipeline": ["1", 0], "audio": ["2", 0], "description": "man speaking",
                     "predict_spans": False, "seed": 0, "inference_steps": 32, "chunk_duration": 10.0,
                     "chunk_overlap": 1.0, "reranking_candidates": 1}},
    "4": {"class_type": "SaveAudio", "_meta": {"title": "Save target (the described sound)"},
          "inputs": {"audio": ["3", 0], "filename_prefix": "audio/sam_target"}},
    "5": {"class_type": "SaveAudio", "_meta": {"title": "Save residual (everything else)"},
          "inputs": {"audio": ["3", 1], "filename_prefix": "audio/sam_residual"}},
}


def _schema(required, outputs, *, names=None, sink=False):
    return {"input": {"required": required}, "output": outputs,
            "output_name": outputs if names is None else names,
            "output_is_list": [False] * len(outputs), "output_node": sink}


def sam_object_info():
    """Advertise the graph's exact selected values and connected socket types."""
    return {
        "SAMAudioPipelineLoader": _schema({
            "model_name": [["facebook/sam-audio-large"]],
            "attention_backend": [["pytorch"]], "precision": [["auto"]],
            "initial_device": [["cpu"]]}, ["SAM_AUDIO_PIPELINE"], names=["pipeline"]),
        "LoadAudio": _schema({"audio": [["input.wav"]]}, ["AUDIO"]),
        "SAMAudioTextSeparator": _schema({
            "pipeline": ["SAM_AUDIO_PIPELINE"], "audio": ["AUDIO"],
            "description": ["STRING", {"default": "man speaking"}],
            "predict_spans": ["BOOLEAN", {"default": False}], "seed": ["INT", {"default": 0}],
            "inference_steps": ["INT", {"default": 32}],
            "chunk_duration": ["FLOAT", {"default": 10.0}],
            "chunk_overlap": ["FLOAT", {"default": 1.0}],
            "reranking_candidates": ["INT", {"default": 1}]}, ["AUDIO", "AUDIO"],
            # Upstream nodes.py at 3cfbd03da3019c7252ba13efd5694b1b2ff487e5:477-478.
            names=["target", "residual"]),
        "SaveAudio": _schema({"audio": ["AUDIO"], "filename_prefix": ["STRING", {"default": "audio"}]},
                             [], sink=True),
    }


@pytest.fixture
def sam_offline_lookup(monkeypatch, tmp_path):
    """Keep real loopback object_info; replay empty public lookup responses.

    The CPU stand-in does not require discovering or downloading any pack.
    Isolate the resolver cache and fail closed on unexpected remote HTTP.
    """
    import httpx
    from vibecomfy.registry import pack_resolver

    cache = tmp_path / "registry-cache"
    monkeypatch.setenv("VIBECOMFY_REGISTRY_CACHE", str(cache))
    monkeypatch.setattr(pack_resolver, "DEFAULT_CACHE_ROOT", cache)
    monkeypatch.setenv("GITHUB_TOKEN", "sam-cpu-test-token")
    monkeypatch.setenv("NO_PROXY", "localhost,127.0.0.1,::1")
    send = httpx.Client.send

    def replay(client, request, *args, **kwargs):
        host, path = request.url.host, request.url.path
        if host in {"localhost", "127.0.0.1", "::1"}:
            return send(client, request, *args, **kwargs)
        assert request.method == "GET", f"Unexpected SAM lookup method: {request.method}"
        if host == "api.comfy.org" and path.startswith("/comfy-nodes/"):
            return httpx.Response(404, json={}, request=request)
        if host == "api.comfy.org" and path == "/nodes/search":
            return httpx.Response(200, json={"nodes": []}, request=request)
        if host == "api.github.com" and path in {"/search/code", "/search/repositories"}:
            return httpx.Response(200, json={"total_count": 0, "items": []}, request=request)
        if host == "raw.githubusercontent.com" and path.startswith("/ltdrdata/ComfyUI-Manager/main/"):
            if path.endswith(("/custom-node-map.json", "/extension-node-map.json")):
                return httpx.Response(200, json={}, request=request)
            if path.endswith("/custom-node-list.json"):
                return httpx.Response(200, json={"custom_nodes": []}, request=request)
        pytest.fail(f"Unexpected non-loopback SAM lookup: {request.method} {host}{path}")

    monkeypatch.setattr(httpx.Client, "send", replay)


@pytest.fixture
def sam_api_path(tmp_path, sam_offline_lookup):
    source = tmp_path / "sam.json"
    source.write_text(json.dumps(SAM_API), encoding="utf-8")
    return source
