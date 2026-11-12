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
    ("scail-2", "v2v"),
    ("hunyuan-foley-local", "sfx"),
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
    args = build_generate_argv(entry, mode, inputs, "runpod", paid_controls, dry_run=True)
    before = len(runpod_guard.blocked_attempts)
    rc = console.main(args)
    output = json.loads(capsys.readouterr().out)
    assert rc == 0 and output.get("ok", True), output
    assert runpod_guard.blocked_attempts[before:] == []
