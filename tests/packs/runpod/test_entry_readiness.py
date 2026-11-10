"""Inventory absence before managed alignment cannot reject a valid pod entry."""
import json
import subprocess
import sys

import pytest

from astrid.core.generation import cli
from astrid.packs.runpod import machine
from scripts.gen_capability_index import _in_repo_packs_only


def test_public_fresh_pod_listing_allows_declared_local_entry_before_alignment(tmp_path, monkeypatch, capsys):
    # The model-free install has no attached ComfyUI until the normal managed
    # job starts/aligns it. Keep the real listing producer and product catalog.
    monkeypatch.setattr("astrid.packs.vibecomfy.local.scan", lambda *a, **k: {"servers": []})
    with _in_repo_packs_only():
        assert cli.main(["list", "--json"]) == 0
    output = capsys.readouterr().out
    rows = json.loads(output)
    selected = [r for r in rows if r["id"] == "qwen-image-2.1-turbo" and r["backend"] == "local"
                and r["mode"] == "t2i"]
    assert selected and all(r["availability"] == "available" and r["ready"] is False for r in selected)
    path = tmp_path / "listing.json"
    path.write_text(output)
    checked = subprocess.run([sys.executable, "-c", machine._ENTRY_CHECK, str(path),
                              "qwen-image-2.1-turbo", "t2i"], capture_output=True, text=True)
    assert checked.returncode == 0, checked.stderr


def _check(tmp_path, rows, mode="t2i"):
    path = tmp_path / "listing.json"
    path.write_text(json.dumps(rows))
    return subprocess.run([sys.executable, "-c", machine._ENTRY_CHECK, str(path), "entry", mode],
                          capture_output=True, text=True)


def _row(**changes):
    return {"id": "entry", "mode": "t2i", "backend": "local", "source": "catalog",
            "availability": "available", "ready": False, **changes}


def test_unrelated_mode_backend_and_target_readiness_do_not_gate_selected_route(tmp_path):
    rows = [_row(), _row(mode="edit", availability="unavailable"),
            _row(backend="cloud", availability="unavailable"), _row(target="other", ready=True)]
    assert _check(tmp_path, rows).returncode == 0


@pytest.mark.parametrize("rows", [
    [], [_row(id="other")], [_row(mode="edit")], [_row(backend="cloud")],
    [_row(backend="action", source="action-entry")], [_row(availability="unavailable")],
    [_row(availability="fixture-only")], [_row(availability=None)], [_row(ready="false")],
    [_row(), _row(availability="unavailable")], [None], {},
])
def test_invalid_unavailable_or_unmatched_local_declaration_still_refuses(tmp_path, rows):
    assert _check(tmp_path, rows).returncode != 0


def test_omitted_mode_requires_one_unambiguous_local_mode(tmp_path):
    assert _check(tmp_path, [_row()], mode="").returncode == 0
    assert _check(tmp_path, [_row(), _row(mode="edit")], mode="").returncode != 0
    assert _check(tmp_path, [_row(mode=None)], mode="").returncode != 0


def test_generated_command_carries_selected_mode_to_the_guard():
    import shlex
    command = machine.generation_command("entry", "/root/astrid/runs/job", mode="edit")
    args = shlex.split(command)
    assert args[args.index(machine._ENTRY_CHECK) + 1:][:3] == [
        "/root/astrid/runs/job/listing.json", "entry", "edit"]
