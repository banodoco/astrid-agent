"""LE-15: a saved SAM graph has one supported, explicit audio replay spelling."""
from __future__ import annotations

import json
import wave

import pytest

from astrid.core.generation import cli, entries, preflight
from tests.core.generation.backends.test_comfy_model_resolution import install_target
from tests.core.generation.backends.test_comfy_target import network  # noqa: F401
from tests.core.generation.fixtures.plan56_sam import sam_api_path, sam_object_info  # noqa: F401
from vibecomfy.entries import load_entry


def test_saved_sam_audio_entry_replays_through_real_cli_on_cpu_target(
    network, monkeypatch, tmp_path, capsys, sam_api_path,
):
    client, requests = install_target(network, sam_object_info())
    monkeypatch.setattr(preflight, "_module_available", lambda name: name == "vibecomfy")
    monkeypatch.setattr(preflight, "_comfyui_runtime_available",
                        lambda: pytest.fail("managed installation probed"))
    monkeypatch.setattr(cli, "_open_client", lambda: pytest.fail("save/preview opened Runtime"))
    root = tmp_path / "entries"
    monkeypatch.setenv("VIBECOMFY_ENTRIES_DIR", str(root))
    endpoint = client.target.url
    assert cli.main(["workflow", str(sam_api_path), "--as-entry", "sam-separate",
                     "--placement", endpoint, "--no-run", "--json"]) == 0
    saved = json.loads(capsys.readouterr().out)
    assert saved["saved"] and not saved["ran"]
    before = (root / "sam-separate" / "entry.json").read_bytes()
    raw = load_entry(root / "sam-separate")
    assert raw["modes"] == []
    assert any(n.get("code") == "audio_mode_unknown" for n in raw["needs"])
    selected = entries.find_entry("sam-separate")
    assert selected["modes"] == ["workflow"]
    assert not any(n.get("code") == "audio_mode_unknown" for n in selected["needs"])
    assert any(n.get("unresolved") and n.get("field") == "model_name" for n in selected["needs"])

    audio = tmp_path / "clip.wav"
    with wave.open(str(audio), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(48000)
        stream.writeframes(b"\0\0" * 48)
    assert cli.main(["audio", "--entry", "sam-separate", "--mode", "workflow",
                     "--audio-ref", str(audio), "--placement", endpoint,
                     "--dry-run", "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "ready", result
    assert result["entry"]["kind"] == "audio"
    assert result["entry"]["mode"] == "workflow"
    assert "audio" in result["entry"]["accepted_inputs"]
    assert ("GET", "/object_info") in requests
    assert (root / "sam-separate" / "entry.json").read_bytes() == before


def test_workflow_audio_mode_requires_an_explicit_entry(capsys):
    assert cli.main(["audio", "--mode", "workflow", "--dry-run", "--json"]) == 2
    result = json.loads(capsys.readouterr().out)
    assert "--entry" in result["issues"][0]["message"]
