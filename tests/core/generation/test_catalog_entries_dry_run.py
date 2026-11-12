"""Required catalog inputs are admitted through the public generation CLI."""

import json

from astrid import console
from astrid.core.model_catalog.registry import ModelRegistry
from astrid.core.model_catalog.requests import PaidControls, build_generate_argv


def _required_value(name, kind, tmp_path):
    if name in {"prompt", "negative_prompt"}:
        return "A clear test scene"
    if name in {"image_ref", "video_ref"}:
        path = tmp_path / f"{name}.png"
        path.write_bytes(b"test image")
        return str(path)
    if kind in {"integer", "int"}:
        return "7"
    if kind in {"number", "float"}:
        return "1.0"
    if kind in {"boolean", "bool"}:
        return "true"
    return "test-value"


def test_catalog_entries_with_required_inputs_dry_run_to_admission(tmp_path, capsys, monkeypatch):
    from astrid.core.generation import preflight

    # Catalog-backed validation/admission is the subject; readiness is an
    # external host property and would make this test machine-dependent.
    monkeypatch.setattr(preflight, "require_generation_target_readiness", lambda *a, **k: None)
    controls_path = tmp_path / "stage.json"
    controls_path.write_text(json.dumps({"max_usd": 2, "deadline": "2099-01-01T00:00:00Z"}))
    controls = PaidControls.from_json(controls_path)
    registry = ModelRegistry.load_default()
    rows = []
    for entry in registry.list_all():
        for mode, spec in entry.modes.items():
            if spec.requires:
                rows.append((entry, mode, spec))
    assert rows
    failures = []
    for entry, mode, spec in rows:
        inputs = {name: _required_value(name, "string", tmp_path) for name in spec.requires}
        args = build_generate_argv(entry, mode, inputs, "runpod", controls, dry_run=True)
        rc = console.main(args)
        output = capsys.readouterr().out
        payload = json.loads(output)
        if rc != 0 or not payload.get("ok", True):
            failures.append(f"{entry.id}/{mode}: {output.strip()}")
    assert not failures, "\n".join(failures)
