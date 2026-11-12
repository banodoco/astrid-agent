"""The stage's dry and paid catalog requests share a single builder."""

import json

from astrid import console
from astrid.core.model_catalog.registry import ModelRegistry
from astrid.core.model_catalog.requests import PaidControls, build_generate_argv
from tests import runpod_guard


def test_catalog_request_parity_and_required_inputs(tmp_path):
    controls_path = tmp_path / "stage.json"
    controls_path.write_text(json.dumps({"max_usd": 2.5, "deadline": "2099-01-01T00:00:00Z"}))
    controls = PaidControls.from_json(controls_path)
    assert controls == PaidControls(max_usd=2.5, deadline="2099-01-01T00:00:00Z")
    for entry in ModelRegistry.load_default().list_all():
        for mode, spec in entry.modes.items():
            inputs = {name: f"value-{name}" for name in spec.requires}
            live = build_generate_argv(entry, mode, inputs, "runpod", controls)
            dry = build_generate_argv(entry, mode, inputs, "runpod", controls, dry_run=True)
            assert dry.count("--dry-run") == 1
            assert [arg for arg in dry if arg != "--dry-run"] == live
            assert live[:2] == ["generate", entry.modality]
            assert ("value-prompt" in live) == ("prompt" in spec.requires)
            for name in spec.requires:
                if name == "prompt":
                    continue
                flag, value = {"image_ref": ("--edit", inputs[name]),
                               "video_ref": ("--video-ref", inputs[name])}.get(
                                   name, ("--input", f"{name}={inputs[name]}"))
                assert live[live.index(flag) + 1] == value or (flag == "--input" and value in live)
            assert live[-7:] == ["--consent", "paid", "--max-usd", "2.5", "--deadline",
                                  "2099-01-01T00:00:00Z", "--json"]


def test_paid_catalog_dry_run_attempts_no_runpod_request(tmp_path, monkeypatch, capsys):
    from astrid.core.generation import preflight

    monkeypatch.setattr(preflight, "require_generation_target_readiness", lambda *a, **k: None)
    monkeypatch.delenv(runpod_guard.LIVE_OPT_IN_ENV, raising=False)
    path = tmp_path / "stage.json"
    path.write_text(json.dumps({"max_usd": 2, "deadline": "2099-01-01T00:00:00Z"}))
    entry = next(e for e in ModelRegistry.load_default().list_all() if e.id == "z-image")
    args = build_generate_argv(entry, "t2i", {"prompt": "A clear test scene"}, "runpod",
                               PaidControls.from_json(path), dry_run=True)
    before = len(runpod_guard.blocked_attempts)
    rc = console.main(args)
    output = json.loads(capsys.readouterr().out)
    assert rc == 0 and output.get("ok", True), output
    assert runpod_guard.blocked_attempts[before:] == []
