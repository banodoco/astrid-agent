"""A missing model on an attached ComfyUI names its file and folder; managed targets keep align."""
from __future__ import annotations

import json

import pytest

from astrid.core.generation.asset_gaps import align_argv
from astrid.core.generation.backends.comfy_target import (
    ComfyModelMissing, ComfyTarget, ComfyTargetClient, _capture_model_resolution,
)
from astrid.core.model_catalog.registry import ModelRegistry
from tests.core.generation.backends.test_comfy_model_resolution import target_info

URL = "http://127.0.0.1:8188"
FOLDERS = {
    "custom_nodes": ["/srv/ComfyUI/custom_nodes"],
    "vae": ["/srv/ComfyUI/models/vae"],
    "diffusion_models": ["/srv/ComfyUI/models/diffusion_models"],
    "text_encoders": ["/srv/ComfyUI/models/text_encoders"],
}


def _fake_server(monkeypatch, info, folders):
    """Serve object_info and the folder listing at the client's request seam only."""
    seen = []

    def request_json(self, path, payload=None, **_):
        seen.append(path)
        if path == "/object_info":
            return info
        if path == "/internal/folder_paths":
            return folders
        raise AssertionError(f"unexpected target operation: {path}")

    monkeypatch.setattr(ComfyTargetClient, "request_json", request_json)
    return seen


def _capture(kind, monkeypatch, *, folders=FOLDERS):
    # z-image's VAE role expects FLUX1\ae.safetensors; this target only has other.safetensors.
    info = target_info("t2i", vaes=("other.safetensors",))
    seen = _fake_server(monkeypatch, info, folders)
    client = ComfyTargetClient(ComfyTarget(URL, kind))
    with pytest.raises(ComfyModelMissing) as caught:
        _capture_model_resolution(client, ModelRegistry.load_default(), "z-image", "t2i")
    return caught.value, seen


def test_attached_missing_model_names_file_role_and_folder_without_align(monkeypatch):
    error, seen = _capture("attached", monkeypatch)
    text = str(error)
    assert "ae.safetensors" in text
    assert "vae" in text
    assert "/srv/ComfyUI/models/vae" in text
    assert "align" not in text
    assert "astrid generate" not in text
    assert "put " in text.lower() and "then retry" in text
    assert "/internal/folder_paths" in seen
    fixes = error.readiness["fixes"]
    assert fixes, "attached readiness must still carry a fix"
    assert all("argv" not in fix for fix in fixes)
    assert "align" not in json.dumps(error.readiness)
    assert any("/srv/ComfyUI/models/vae" in issue["message"] for issue in error.readiness["issues"])


def test_managed_missing_model_keeps_align_message_and_fixes(monkeypatch):
    error, seen = _capture("managed", monkeypatch)
    argv = align_argv("z-image")
    assert error.readiness["fixes"] == [{
        "argv": argv, "scope": "user_env",
        "details": {"action": "Install missing locked assets on the managed target; unlocked assets require a manifest."},
    }]
    assert str(error).endswith("run: " + " ".join(argv))
    assert error.readiness["issues"] == [{
        "code": "missing_model", "severity": "error",
        "message": "Missing model ae.safetensors, FLUX1\\ae.safetensors (role vae). Run: " + " ".join(argv),
        "details": {"model": "ae.safetensors, FLUX1\\ae.safetensors (role vae)"},
    }]
    assert "/internal/folder_paths" not in seen
