"""WS-0d: exercise the actual CLI and pinned VibeComfy import/replacement."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from astrid.core.generation import cli, entries
from tests.core.generation.test_generate_target_journey import loopback_comfy  # noqa: F401
from vibecomfy.entries import load_entry


def test_workflow_cli_replacement_is_explicit_and_atomic(tmp_path, monkeypatch, capsys, loopback_comfy):
    root = tmp_path / "entries"
    monkeypatch.setenv("VIBECOMFY_ENTRIES_DIR", str(root))
    monkeypatch.chdir(tmp_path)
    # Save-only stays offline. LE-12's full preview must also inspect a
    # selected target, so supply the two real graph classes on the existing
    # CPU-only loopback stand-in; do not stub target or invocation readiness.
    loopback_comfy.schema["LoadImage"] = {
        "input": {"required": {"image": [["old.png", "new.png"], {"image_upload": True}]}},
        "input_order": {"required": ["image"]},
        "output": ["IMAGE", "MASK"], "output_name": ["IMAGE", "MASK"],
        "output_is_list": [False, False], "output_node": False,
    }
    monkeypatch.setattr(cli, "_open_client", lambda: pytest.fail("save or preview opened Runtime"))
    old = tmp_path / "old.json"
    new = tmp_path / "new.json"
    raw = {
        "1": {"class_type": "LoadImage", "inputs": {"image": "old.png"}},
        "2": {"class_type": "SaveImage", "inputs": {"images": ["1", 0], "filename_prefix": "out"}},
    }
    old.write_text(json.dumps(raw), encoding="utf-8")
    raw["1"]["inputs"]["image"] = "new.png"
    new.write_text(json.dumps(raw), encoding="utf-8")

    def command(source, *flags):
        return cli.main(["workflow", str(source), "--placement", loopback_comfy.url,
                         "--as-entry", "sample", "--json", *flags])

    # The same source still requires image for a real generation request.
    with pytest.raises(entries.MissingRequiredEntryInputsError, match="image"):
        entries.resolve_workflow_inputs(old, {})
    assert command(old, "--no-run") == 0
    assert json.loads(capsys.readouterr().out) == {
        "saved": True, "ran": False, "entry": "sample", "replaced": False,
    }
    entry_path = root / "sample"
    first = load_entry(entry_path)
    manifest = (entry_path / "entry.json").read_bytes()
    original_paths = set(entry_path.iterdir())
    assert (Path(first["bundle"]) / "source.json").read_bytes() == old.read_bytes()

    # Omitting the explicit replacement flag must not change the saved entry.
    assert command(new, "--no-run") == 2
    capsys.readouterr()
    assert (entry_path / "entry.json").read_bytes() == manifest
    assert set(entry_path.iterdir()) == original_paths

    # Preview is non-writing, including when replacement is requested.
    assert loopback_comfy.calls == []
    code = command(new, "--replace", "--dry-run")
    preview = json.loads(capsys.readouterr().out)
    assert code == 0, preview
    assert preview["written"] is False
    assert preview["replaces_existing"] is True
    assert preview["status"] in {"ready", "ready_with_warnings"}
    assert any(call[:2] == ("GET", "/object_info") for call in loopback_comfy.calls)
    assert (entry_path / "entry.json").read_bytes() == manifest
    assert set(entry_path.iterdir()) == original_paths

    # A real target refusal in a replacement preview must not be bypassed.
    save_schema = loopback_comfy.schema.pop("SaveImage")
    try:
        assert command(new, "--replace", "--dry-run") == 2
        blocked = json.loads(capsys.readouterr().out)
        assert blocked["status"] == "blocked"
        assert any(issue["code"] == "missing_node_pack" and issue["details"]["node"] == "SaveImage"
                   for issue in blocked["issues"]), blocked
        assert (entry_path / "entry.json").read_bytes() == manifest
        assert set(entry_path.iterdir()) == original_paths
    finally:
        loopback_comfy.schema["SaveImage"] = save_schema
    assert not any(call[0] == "POST" for call in loopback_comfy.calls)
    assert not loopback_comfy.prompts

    # Save-only still refuses an invalid supplied input and changes nothing.
    assert command(new, "--replace", "--no-run", "--input", "nonexistent=oops") == 2
    bad_input = json.loads(capsys.readouterr().out)
    assert "nonexistent" in bad_input["issues"][0]["message"]
    assert (entry_path / "entry.json").read_bytes() == manifest
    assert set(entry_path.iterdir()) == original_paths

    # A failed input/source validation may not erase an existing entry.
    assert command(tmp_path / "missing.json", "--replace", "--no-run") == 2
    capsys.readouterr()
    assert (entry_path / "entry.json").read_bytes() == manifest
    assert set(entry_path.iterdir()) == original_paths

    assert command(new, "--replace", "--no-run") == 0
    assert json.loads(capsys.readouterr().out) == {
        "saved": True, "ran": False, "entry": "sample", "replaced": True,
    }
    second = load_entry(entry_path)
    assert second["bundle"] != first["bundle"]
    assert (Path(second["bundle"]) / "source.json").read_bytes() == new.read_bytes()
    assert (Path(first["bundle"]) / "source.json").read_bytes() == old.read_bytes()
