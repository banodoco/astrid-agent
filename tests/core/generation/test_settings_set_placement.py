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
    with pytest.raises(GenerationReadinessError,
                       match="placement must be http://127.0.0.1:<port> or http://localhost:<port>") as caught:
        cli.main(["settings", "set", "placement", url])
    assert "comfy_url" not in str(caught.value)
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


def _seed(store, placement):
    settings.update(lambda document: document.__setitem__("placement", placement))
    assert store.documents["generation"][0]["placement"] == placement


def test_set_and_unset_keep_a_saved_policy_and_report_previous(run, generation_settings_store):
    policy = {"idle_timeout_s": 30.0, "gpu_types": ["RTX 4090"]}
    _seed(generation_settings_store, {"kind": "attached", "endpoint": "http://127.0.0.1:18111", "policy": policy})

    result = settings.set_placement("http://127.0.0.1:9123")
    assert result["placement"] == {"kind": "attached", "endpoint": "http://127.0.0.1:9123", "policy": policy}
    assert result["previous"] == {"kind": "attached", "endpoint": "http://127.0.0.1:18111", "policy": policy}

    result = settings.unset_placement()
    assert result["placement"] == {"policy": policy}
    assert result["previous"] == {"kind": "attached", "endpoint": "http://127.0.0.1:9123", "policy": policy}
    assert generation_settings_store.documents["generation"][0]["placement"] == {"policy": policy}


def test_set_drops_a_device_from_another_kind_and_reports_it(run, generation_settings_store):
    policy = {"idle_timeout_s": 60.0}
    _seed(generation_settings_store, {"kind": "local", "device": 1, "policy": policy})

    result = settings.set_placement("http://127.0.0.1:9123")
    assert result["placement"] == {"kind": "attached", "endpoint": "http://127.0.0.1:9123", "policy": policy}
    assert result["previous"] == {"kind": "local", "device": 1, "policy": policy}

    result = settings.unset_placement()
    assert result["placement"] == {"policy": policy}
    assert result["previous"] == {"kind": "attached", "endpoint": "http://127.0.0.1:9123", "policy": policy}


def test_set_keeps_a_device_when_the_placement_was_already_attached(run, generation_settings_store):
    policy = {"idle_timeout_s": 60.0}
    _seed(generation_settings_store, {"kind": "attached", "endpoint": "http://127.0.0.1:18111", "device": 1,
                                      "policy": policy})
    result = settings.set_placement("http://127.0.0.1:9123")
    assert result["placement"] == {"kind": "attached", "endpoint": "http://127.0.0.1:9123", "device": 1,
                                   "policy": policy}
    assert result["previous"] == {"kind": "attached", "endpoint": "http://127.0.0.1:18111", "device": 1,
                                  "policy": policy}


def test_unset_removes_the_device_and_leaves_only_the_policy(run, generation_settings_store):
    policy = {"idle_timeout_s": 60.0}
    _seed(generation_settings_store, {"kind": "attached", "endpoint": "http://127.0.0.1:18111", "device": 1,
                                      "policy": policy})
    result = settings.unset_placement()
    assert result["placement"] == {"policy": policy}
    assert result["previous"] == {"kind": "attached", "endpoint": "http://127.0.0.1:18111", "device": 1,
                                  "policy": policy}


def test_cli_set_names_a_dropped_device_and_says_it_is_not_restorable(run, generation_settings_store):
    _seed(generation_settings_store, {"kind": "local", "device": 1})
    line = run(["settings", "set", "placement", "http://127.0.0.1:9123"]).strip()
    assert generation_settings_store.documents["generation"][0]["placement"] == {
        "kind": "attached", "endpoint": "http://127.0.0.1:9123"}
    assert '"device": 1' in line and "restore: none in one command" in line


def test_cli_set_prints_one_line_naming_previous_and_the_restore_command(run, generation_settings_store):
    _seed(generation_settings_store, {"kind": "attached", "endpoint": "http://127.0.0.1:18111"})
    line = run(["settings", "set", "placement", "http://127.0.0.1:9123"]).strip()
    assert line.count("\n") == 0
    assert '"endpoint": "http://127.0.0.1:18111"' in line
    assert "restore: astrid generate settings set placement http://127.0.0.1:18111" in line


def test_cli_set_with_no_previous_placement_restores_with_unset(run, generation_settings_store):
    line = run(["settings", "set", "placement", "http://127.0.0.1:9123"]).strip()
    assert "(was the built-in default)" in line
    assert line.endswith("restore: astrid generate settings unset placement")


def test_cli_unset_restores_the_endpoint_it_removed(run, generation_settings_store):
    run(["settings", "set", "placement", "http://127.0.0.1:9123"])
    line = run(["settings", "unset", "placement"]).strip()
    assert "(was {" in line and "http://127.0.0.1:9123" in line
    assert line.endswith("restore: astrid generate settings set placement http://127.0.0.1:9123")
