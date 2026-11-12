"""Required catalog inputs are admitted through the public generation CLI."""

import json

from astrid import console
from astrid.core.model_catalog.registry import ModelRegistry


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
    registry = ModelRegistry.load_default()
    rows = []
    for entry in registry.list_all():
        for mode, spec in entry.modes.items():
            if spec.requires:
                rows.append((entry, mode, spec))
    assert rows
    failures = []
    for entry, mode, spec in rows:
        modality = entry.modality
        args = ["generate", modality, "A clear test scene", "--entry", entry.id,
                "--mode", mode, "--placement", "runpod", "--dry-run", "--json"]
        for name in spec.requires:
            value = _required_value(name, "string", tmp_path)
            if name == "prompt":
                continue  # supplied by the positional public prompt above
            if name == "image_ref":
                args.extend(["--edit", value])
            elif name == "video_ref":
                args.extend(["--video-ref", value])
            else:
                args.extend(["--input", f"{name}={value}"])
        rc = console.main(args)
        output = capsys.readouterr().out
        payload = json.loads(output)
        if rc != 0 or not payload.get("ok", True):
            failures.append(f"{entry.id}/{mode}: {output.strip()}")
    assert not failures, "\n".join(failures)
