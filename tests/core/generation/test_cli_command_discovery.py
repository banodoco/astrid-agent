"""A CLI command shares one entry-discovery snapshot, and the next command gets a fresh one."""
from __future__ import annotations

import json
import os
from types import SimpleNamespace

import pytest

from astrid.core.generation import cli, entries


ENTRY_ID = "test.command_discovery"


def _declaration(input_name: str) -> dict:
    return {
        "id": ENTRY_ID,
        "kind": "image",
        "modes": ["t2i"],
        "inputs": [
            {"name": "prompt", "type": "string", "required": True},
            {"name": input_name, "type": "string", "required": True},
        ],
        "bundle": "unused/workflow.py",
        "binds": {},
        "defaults": {},
        "contribution": "test.pack.workflow",
    }


def _install_discovery_sources(monkeypatch, tmp_path):
    from astrid.core.model_catalog.registry import ModelRegistry
    from astrid.core.extensions import scopes
    from vibecomfy import entries as vibe_entries

    scope_root = tmp_path / "extension-packs"
    pack = scope_root / "sample-pack"
    extension_dir = pack / "extensions" / "vibecomfy.workflows"
    ready = extension_dir / "ready"
    ready.mkdir(parents=True)
    source = extension_dir / "workflow.yaml"

    def write_declaration(input_name):
        import yaml

        source.write_text(yaml.safe_dump({
            "ready_root": "ready",
            "entries": [_declaration(input_name)],
        }), encoding="utf-8")

    write_declaration("style")
    observations = {"registry": [], "users": [], "actions": []}

    original_load = ModelRegistry.load_default

    def load_default(cls, **kwargs):
        observations["registry"].append(dict(kwargs))
        return original_load(**kwargs)

    def user_entries():
        observations["users"].append(source.stat().st_mtime_ns)
        return original_users()

    original_users = entries.user_entries

    def action_entries():
        observations["actions"].append(None)
        return []

    monkeypatch.setattr(ModelRegistry, "load_default", classmethod(load_default))
    monkeypatch.setattr(entries, "user_entries", user_entries)
    monkeypatch.setattr(entries, "action_entries", action_entries)
    monkeypatch.setattr(vibe_entries, "discover_entries", lambda: [])
    monkeypatch.setattr(scopes, "contribution_roots", lambda: [
        SimpleNamespace(path=str(scope_root), scope="user"),
    ])
    return source, observations, write_declaration


def test_cli_discovers_a_contributed_entry_once_per_command_and_observes_same_size_edit(
    monkeypatch, tmp_path, capsys,
):
    from astrid.sdk.generation import GenerationFacade

    source, observations, write_declaration = _install_discovery_sources(monkeypatch, tmp_path)
    admitted = []

    def prepare_target(self, **kwargs):
        admitted.append(kwargs)
        return {"ok": True, "inputs": dict(kwargs["inputs"])}

    monkeypatch.setattr(GenerationFacade, "_generate_on_target", prepare_target)

    before = source.stat()
    assert cli.main([
        "image", "--entry", ENTRY_ID, "--mode", "t2i", "--dry-run", "--json",
        "--input", "prompt=paint a fox", "--input", "style=soft",
    ]) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["inputs"] == {"prompt": "paint a fox", "style": "soft"}
    assert admitted[0]["inputs"] == first["inputs"]
    assert observations == {
        "registry": [{}], "users": [before.st_mtime_ns], "actions": [None],
    }

    write_declaration("theme")
    os.utime(source, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert source.stat().st_mtime_ns == before.st_mtime_ns
    assert source.stat().st_size == before.st_size

    assert cli.main([
        "image", "--entry", ENTRY_ID, "--mode", "t2i", "--dry-run", "--json",
        "--input", "prompt=paint a fox", "--input", "theme=autumn",
    ]) == 0
    second = json.loads(capsys.readouterr().out)
    assert second["inputs"] == {"prompt": "paint a fox", "theme": "autumn"}
    assert admitted[1]["inputs"] == second["inputs"]
    assert observations == {
        "registry": [{}, {}],
        "users": [before.st_mtime_ns, before.st_mtime_ns],
        "actions": [None, None],
    }


def test_cli_rejects_missing_contributed_input_before_opening_runtime(monkeypatch, tmp_path, capsys):
    from astrid.sdk.generation import GenerationFacade

    _install_discovery_sources(monkeypatch, tmp_path)
    monkeypatch.setattr(cli, "_open_client", lambda: pytest.fail("invalid entry opened Runtime"))
    monkeypatch.setattr(GenerationFacade, "_generate_on_target",
                        lambda self, **kwargs: pytest.fail("invalid entry reached target preparation"))

    with pytest.raises(SystemExit) as caught:
        cli.main([
            "image", "--entry", ENTRY_ID, "--mode", "t2i",
            "--input", "prompt=paint a fox",
        ])

    assert caught.value.code == 2
    assert "missing required entry inputs: style" in capsys.readouterr().err
