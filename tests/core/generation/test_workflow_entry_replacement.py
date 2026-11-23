"""WS-0d: exercise the actual CLI and pinned VibeComfy import/replacement."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from astrid.core.generation import cli
from vibecomfy.entries import load_entry


def test_workflow_cli_replacement_is_explicit_and_atomic(tmp_path, monkeypatch, capsys):
    root = tmp_path / "entries"
    monkeypatch.setenv("VIBECOMFY_ENTRIES_DIR", str(root))
    monkeypatch.chdir(tmp_path)
    # These are offline API-format graphs; no server or Runtime is required
    # to validate and save them.
    monkeypatch.setattr(cli, "_workflow_comfy_url", lambda parsed: {})
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
        return cli.main(["workflow", str(source), "--placement", "local",
                         "--as-entry", "sample", "--json", *flags])

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
    assert command(new, "--replace", "--dry-run") == 0
    preview = json.loads(capsys.readouterr().out)
    assert preview["written"] is False
    assert preview["replaces_existing"] is True
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
