"""P4.2: one step saves a ComfyUI endpoint as the Runtime placement default, and one step undoes it."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from astrid.core.generation import cli, placement as placements, settings
from astrid.sdk.generation import GenerationFacade


class _Client:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def run(monkeypatch, capsys, tmp_path, generation_settings_store):
    """Run the generation CLI against the in-memory Runtime store and a temporary home."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("VIBECOMFY_COMFYUI_URL", raising=False)
    monkeypatch.setattr(cli, "_open_client", lambda: _Client())
    monkeypatch.setattr(settings, "transport_of", lambda client: generation_settings_store)

    def invoke(argv):
        assert cli.main(argv) == 0
        return capsys.readouterr().out

    return invoke


def test_settings_set_placement_saves_attached_endpoint_on_a_non_default_port(run, generation_settings_store):
    run(["settings", "set", "placement", "http://127.0.0.1:9123"])
    assert generation_settings_store.documents["generation"][0]["placement"] == {
        "kind": "attached", "endpoint": "http://127.0.0.1:9123"}
    payload = GenerationFacade().targets()
    assert payload["placement"] == {"resolved": {"kind": "attached", "endpoint": "http://127.0.0.1:9123"},
                                    "source": "settings"}


def test_settings_set_placement_is_reported_by_generate_targets_cli(run, generation_settings_store):
    run(["settings", "set", "placement", "http://localhost:9123"])
    payload = json.loads(run(["targets", "--json"]))
    assert payload["placement"]["source"] == "settings"
    assert payload["placement"]["resolved"] == {"kind": "attached", "endpoint": "http://127.0.0.1:9123"}


def test_settings_unset_placement_undoes_it_in_one_command(run, generation_settings_store):
    run(["settings", "set", "placement", "http://127.0.0.1:9123"])
    run(["settings", "unset", "placement"])
    assert generation_settings_store.documents["generation"][0]["placement"] == {}
    payload = GenerationFacade().targets()
    assert payload["placement"]["source"] != "settings"


@pytest.mark.parametrize("url", ["https://127.0.0.1:9123", "http://example.com:9123", "ftp://127.0.0.1:9123",
                                 "http://127.0.0.1:99999", "not a url"])
def test_settings_set_placement_refuses_what_normalize_target_refuses(url, run, monkeypatch, capsys,
                                                                    generation_settings_store):
    from astrid.core.generation.preflight import GenerationReadinessError
    with pytest.raises(GenerationReadinessError, match="comfy_url must be http"):
        cli.main(["settings", "set", "placement", url])
    capsys.readouterr()
    assert "generation" not in generation_settings_store.documents


def test_settings_set_placement_leaves_the_toml_byte_identical(run, tmp_path, generation_settings_store):
    config = tmp_path / ".vibecomfy" / "config.toml"
    config.parent.mkdir()
    config.write_bytes(b'[comfyui]\nurl = "http://127.0.0.1:18111"\n# keep me\n')
    before = config.read_bytes()
    run(["settings", "set", "placement", "http://127.0.0.1:9123"])
    run(["settings", "unset", "placement"])
    assert config.read_bytes() == before


def test_settings_set_placement_creates_no_toml_when_none_exists(run, tmp_path, generation_settings_store):
    run(["settings", "set", "placement", "http://127.0.0.1:9123"])
    run(["settings", "unset", "placement"])
    assert not (tmp_path / ".vibecomfy" / "config.toml").exists()
    assert not (tmp_path / ".vibecomfy").exists()


def test_saved_placement_resolves_for_workflow_dry_run_without_placement_flag(run, tmp_path, monkeypatch,
                                                                              generation_settings_store):
    run(["settings", "set", "placement", "http://127.0.0.1:9123"])
    workflow = tmp_path / "workflow.json"
    workflow.write_text("{}")
    seen = {}

    class FakeFacade:
        def workflow(self, *, placement=None, dry_run=False, **kwargs):
            seen["placement"] = placements.resolve_placement(placements.request_from_selector(placement))
            seen["dry_run"] = dry_run
            return SimpleNamespace(ok=True, to_dict=lambda: {"dry_run": dry_run})

    monkeypatch.setattr(cli, "_generate_facade", lambda: FakeFacade())
    run(["workflow", str(workflow), "--dry-run"])
    assert seen["dry_run"] is True
    assert (seen["placement"].kind, seen["placement"].endpoint) == ("attached", "http://127.0.0.1:9123")
