"""CD-1: targets reports observed readiness for an attached local endpoint."""
import json
from pathlib import Path

import pytest

from astrid.core.generation import cli, preflight, targets
from astrid.sdk.generation import GenerationFacade


URL = "http://127.0.0.1:18111"
COMMAND = ["astrid", "generate", "targets", "--placement", URL]


@pytest.fixture
def probe(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("VIBECOMFY_COMFYUI_URL", raising=False)
    calls = []
    responses = {
        "/system_stats": {"system": {"comfyui_version": "0.43.2"}},
        "/object_info": {"ExampleNode": {}},
    }

    def fake(url, path):
        assert url == URL
        calls.append((url, path))
        response = responses[path]
        if isinstance(response, Exception):
            raise response
        return response

    monkeypatch.setattr(preflight, "_comfy_get_json", fake)
    return calls, responses


def test_healthy_target(probe, tmp_path):
    calls, _ = probe
    payload = GenerationFacade().targets(placement=URL)
    assert payload["status"] == "ready"
    assert payload["ladder"]["through"] == "compatible"
    assert payload["ladder"]["stopped_at"] is None
    assert payload["next_action"] is None
    assert payload["issues"] == payload["fixes"] == []
    assert payload["target"]["endpoint"] == URL
    assert payload["placement"] == {
        "resolved": {"kind": "attached", "endpoint": URL}, "source": "request",
    }
    assert calls == [(URL, "/system_stats"), (URL, "/object_info")]
    assert not (tmp_path / ".vibecomfy").exists()


def assert_refused(payload):
    assert payload["status"] == "blocked"
    assert payload["ladder"]["stopped_at"] == "reachable"
    assert len(payload["issues"]) == len(payload["fixes"]) == 1
    assert payload["issues"][0]["code"] == "target_unreachable"
    assert payload["next_action"]["who"] == "person"
    assert payload["next_action"]["then_argv"] == COMMAND


def test_refused_target_and_explicit_save(probe, tmp_path):
    calls, responses = probe
    responses["/system_stats"] = ConnectionRefusedError("closed")
    facade = GenerationFacade()
    assert_refused(facade.targets(placement=URL))
    assert not (tmp_path / ".vibecomfy").exists()
    saved = facade.targets(placement=URL, save=True)
    assert_refused(saved)
    assert targets.config_path().is_file()
    assert targets.resolve_target().record.endpoint == URL
    assert calls == [(URL, "/system_stats")] * 2


def test_managed_keeps_unprobed_shape(probe, tmp_path):
    calls, _ = probe
    payload = GenerationFacade().targets(placement="managed")
    assert payload == {
        **preflight.readiness_envelope(target=targets.TargetRecord(owner="astrid")),
        "placement": {
            "resolved": {"kind": "managed", "endpoint": None}, "source": "request:managed",
        },
    }
    assert "ladder" not in payload
    assert not calls
    assert not (tmp_path / ".vibecomfy").exists()


@pytest.mark.parametrize("placement", ["auto", "runpod"])
def test_targets_without_save_do_not_import_legacy_settings(probe, generation_settings_store, placement):
    config = targets.config_path()
    config.parent.mkdir(parents=True)
    legacy = (
        '[placement]\nkind = "attached"\nendpoint = "' + URL + '"\n'
        '[runpod]\ngpu_types = ["A40"]\n'
    ).encode()
    config.write_bytes(legacy)
    assert generation_settings_store.documents == {}

    GenerationFacade().targets(placement=placement, save=False)

    assert config.read_bytes() == legacy
    assert generation_settings_store.documents == {}
    assert generation_settings_store.receipts == {}


@pytest.mark.parametrize("refused", [False, True])
def test_stable_cli_facade_payload(probe, monkeypatch, capsys, refused):
    calls, responses = probe
    if refused:
        responses["/system_stats"] = ConnectionRefusedError("closed")
    monkeypatch.setattr(cli, "_open_client", lambda: pytest.fail("discovery opened Runtime"))
    facade = GenerationFacade()
    payload = facade.targets(placement=URL)
    assert facade.targets(placement=URL) == payload
    assert cli.main(["targets", "--placement", URL, "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == payload
    expected = [(URL, "/system_stats")]
    if not refused:
        expected.append((URL, "/object_info"))
    assert calls == expected * 3


@pytest.mark.parametrize("selector,env,runtime,repo,global_,source,endpoint", [
    ("http://127.0.0.1:18100", "http://127.0.0.1:18101", True, True, True, "request", 18100),
    ("auto", "http://127.0.0.1:18101", True, True, True, "env", 18101),
    ("auto", None, True, True, True, "settings", 18102),
    ("auto", None, False, True, True, "repo", 18103),
    ("auto", None, False, False, True, "global", 18104),
    ("auto", None, False, False, False, "discovery", 8188),
])
def test_snapshot_selection_precedence(probe, tmp_path, monkeypatch, generation_settings_store,
                                       selector, env, runtime, repo, global_, source, endpoint):
    import copy
    from astrid.core.generation import listing, settings

    config = targets.config_path()
    project = tmp_path / "project"
    project.mkdir()
    repo_config = project / "vibecomfy.toml"
    if repo:
        repo_config.write_text('[comfyui]\nurl = "http://127.0.0.1:18103"\n')
    if global_:
        config.parent.mkdir()
        config.write_text('[comfyui]\nurl = "http://127.0.0.1:18104"\n')
    if runtime:
        settings.set_placement("http://127.0.0.1:18102")
    if env is not None:
        monkeypatch.setenv("VIBECOMFY_COMFYUI_URL", env)
    before = copy.deepcopy((generation_settings_store.documents, generation_settings_store.receipts))
    bytes_before = {p: p.read_bytes() for p in (config, repo_config) if p.exists()}
    selection = listing.target_selection(request=selector, repo=project)
    assert selection.source == source
    assert selection.record.endpoint == f"http://127.0.0.1:{endpoint}"
    assert (generation_settings_store.documents, generation_settings_store.receipts) == before
    assert {p: p.read_bytes() for p in (config, repo_config) if p.exists()} == bytes_before


def test_runtime_endpoint_wins_over_invalid_lower_priority_target(probe, tmp_path):
    from astrid.core.generation import listing, settings

    settings.set_placement(URL)
    project = tmp_path / "project"
    project.mkdir()
    config = project / "vibecomfy.toml"
    config.write_text("not valid TOML!")
    chosen = listing.target_selection(repo=project)
    assert chosen.source == "settings" and chosen.record.endpoint == URL
    assert config.read_text() == "not valid TOML!"
