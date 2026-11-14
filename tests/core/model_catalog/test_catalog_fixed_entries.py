"""Repaired catalog inputs are admitted through the public generation CLI."""

import json

import pytest

from astrid import console
from astrid.core.model_catalog.registry import ModelRegistry
from astrid.core.model_catalog.requests import PaidControls, build_generate_argv
from tests import runpod_guard


@pytest.fixture
def paid_controls(tmp_path):
    path = tmp_path / "stage.json"
    path.write_text(json.dumps({"max_usd": 2, "deadline": "2099-01-01T00:00:00Z"}))
    return PaidControls.from_json(path)


@pytest.mark.parametrize("entry_id,mode", [
    ("z-image", "i2i"),
    ("hunyuan-foley-local", "sfx"),
    ("qwen-image-edit", "edit"),
])
def test_fixed_entry_dry_run(entry_id, mode, paid_controls, tmp_path, capsys, monkeypatch):
    from astrid.core.generation import preflight

    monkeypatch.setattr(preflight, "require_generation_target_readiness", lambda *a, **k: None)
    monkeypatch.delenv(runpod_guard.LIVE_OPT_IN_ENV, raising=False)
    entry = next(e for e in ModelRegistry.load_default().list_all() if e.id == entry_id)
    inputs = {}
    for name in entry.modes[mode].requires:
        if name in {"image_ref", "video_ref"}:
            path = tmp_path / f"{name}.png"
            path.write_bytes(b"test image")
            inputs[name] = str(path)
        else:
            inputs[name] = "A clear test scene"
    args = build_generate_argv(entry, mode, inputs, "runpod", dry_run=True)
    before = len(runpod_guard.blocked_attempts)
    rc = console.main(args)
    output = json.loads(capsys.readouterr().out)
    assert rc == 0 and output.get("ok", True), output
    assert runpod_guard.blocked_attempts[before:] == []


def test_unqualified_wan_i2v_is_not_offered():
    entry = next(e for e in ModelRegistry.load_default().list_all() if e.id == "wan-2.2")
    assert "i2v" in entry.modes
    assert set(entry.modes["i2v"].backends) == {"cloud"}
    assert entry.modes["i2v"].backends["cloud"].endpoint == "fal-ai/wan/v2.2-a14b/image-to-video/turbo"


# scail-2's full dry run waits for banodoco/astrid-dev#607.
@pytest.mark.parametrize("modality", ["video", "audio"])
def test_video_ref_is_accepted_for_video_and_audio(modality):
    from astrid.core.generation import cli

    args = [modality, "test prompt", "--video-ref", "reference.mp4"]
    if modality == "audio":
        args.extend(["--mode", "sfx"])
    parsed = cli._parser().parse_args(args)
    assert parsed.video_ref == "reference.mp4"


def test_video_ref_is_rejected_for_image():
    from astrid.core.generation import cli

    with pytest.raises(SystemExit):
        cli._parser().parse_args(["image", "test prompt", "--video-ref", "reference.mp4"])


@pytest.mark.parametrize("mode,required", [
    ("t2v", ("prompt",)),
    ("flf", ("prompt", "image_ref", "image_end_ref")),
])
def test_ltx_required_inputs_dry_run(mode, required, paid_controls, tmp_path, capsys, monkeypatch):
    from astrid.core.generation import preflight

    monkeypatch.setattr(preflight, "require_generation_target_readiness", lambda *a, **k: None)
    monkeypatch.delenv(runpod_guard.LIVE_OPT_IN_ENV, raising=False)
    entry = ModelRegistry.load_default().get("ltx-2.3")
    assert set(entry.modes[mode].requires) == set(required)
    inputs = {"prompt": "A clear test scene"}
    for name in required:
        if name != "prompt":
            path = tmp_path / f"{name}.png"
            path.write_bytes(b"test image")
            inputs[name] = str(path)
    args = build_generate_argv(entry, mode, inputs, "runpod", dry_run=True)
    before = len(runpod_guard.blocked_attempts)
    rc = console.main(args)
    output = json.loads(capsys.readouterr().out)
    assert rc == 0 and output.get("ok", True), output
    assert runpod_guard.blocked_attempts[before:] == []


@pytest.mark.parametrize("entry_id", ["lava-sr", "deepfilternet3"])
def test_audio_enhancement_required_input_type(entry_id):
    from astrid.core.generation import entries

    spec = ModelRegistry.load_default().get(entry_id).modes["sfx"]
    assert set(spec.backends) == {"cloud"}
    assert set(spec.requires) == {"audio_ref"}
    description = {"inputs": entries._catalog_inputs("audio", spec, "sfx")}
    fields = {row["name"]: row for row in description["inputs"]}
    assert fields["audio_ref"]["type"] == "string"
    assert fields["audio_ref"]["required"]
    assert entries.validate_inputs(description, {"audio_ref": "reference.wav"}) == {"audio_ref": "reference.wav"}
