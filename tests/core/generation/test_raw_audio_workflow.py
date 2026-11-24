"""WS-0a: raw audio through the real importer and target dry-run."""
from __future__ import annotations

import json

import pytest

from astrid.core.generation import cli, preflight
from astrid.sdk.generation import _explicit_workflow_needs
from tests.core.generation.backends.test_comfy_model_resolution import install_target
from tests.core.generation.backends.test_comfy_target import (  # noqa: F401
    engine_configuration, network, observation_configuration,
)


def test_raw_audio_workflow_dry_run_uses_real_draft_entry(network, monkeypatch, tmp_path, capsys):
    info = {
        "LoadAudio": {
            "input": {"required": {"audio": [["input.wav"]]}},
            "output": ["AUDIO"], "output_name": ["AUDIO"], "output_is_list": [False],
        },
        "SaveAudio": {
            "input": {"required": {"audio": ["AUDIO"],
                                    "filename_prefix": ["STRING", {"default": "out"}]}},
            "output": [], "output_name": [], "output_is_list": [],
            "output_node": True,
        },
    }
    client, requests = install_target(network, info)
    monkeypatch.setattr(preflight, "_module_available", lambda name: name == "vibecomfy")
    monkeypatch.setattr(preflight, "_comfyui_runtime_available",
                        lambda: pytest.fail("managed installation probed"))
    monkeypatch.setattr(cli, "_open_client", lambda: pytest.fail("dry run opened Runtime"))
    monkeypatch.setenv("VIBECOMFY_ENTRIES_DIR", str(tmp_path / "entries"))
    path = tmp_path / "audio.json"
    path.write_text(json.dumps({
        "1": {"class_type": "LoadAudio", "inputs": {"audio": "input.wav"}},
        "2": {"class_type": "SaveAudio", "inputs": {"audio": ["1", 0], "filename_prefix": "out"}},
    }), encoding="utf-8")
    original = path.read_bytes()
    assert cli.main(["workflow", str(path), "--placement", client.target.url,
                     "--dry-run", "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["entry"]["kind"] == "audio"
    assert result["entry"]["mode"] == "workflow"
    assert result["status"] == "ready"
    assert not result["issues"]
    assert ("GET", "/object_info") in requests
    assert path.read_bytes() == original
    assert not (tmp_path / "entries").exists()


def test_filter_only_the_coded_mode_need():
    mode = {"code": "audio_mode_unknown", "ambiguity": "changed or translated wording"}
    binding = {"ambiguity": "multiple targets for audio", "candidates": []}
    unrelated = {"code": "unknown_input", "ambiguity": "audio mode is unknown; choose tts, music, or sfx"}
    assert _explicit_workflow_needs([binding, mode, unrelated]) == [binding, unrelated]
