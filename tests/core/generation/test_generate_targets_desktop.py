"""R93-C: actual public CLI and SDK producer payloads, with no implicit writes."""
import copy
import json
from pathlib import Path

import pytest

from astrid.core.generation import cli, desktop_install, placement, preflight, settings, targets
from astrid.packs.vibecomfy import local
from astrid.sdk.generation import GenerationFacade, GenerationInputError
from tests.core.generation.test_desktop_rungs import (
    INSTALLATION_DENIAL_CASES, denied_installation_for, installation_for,
)


URL = "http://127.0.0.1:8000"
OTHER = "http://127.0.0.1:8188"


@pytest.fixture
def scenario(monkeypatch, tmp_path, generation_settings_store):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.delenv("VIBECOMFY_COMFYUI_URL", raising=False)
    monkeypatch.setattr(preflight, "_platform", lambda: "win32")
    state = {"installations": [installation_for("running")], "servers": [], "errors": [],
             "pinokio": {"available": False, "home": None}}
    calls = []

    def scan(ports):
        assert ports == [8188, 8000]
        return copy.deepcopy(state)

    def probe(url, path):
        calls.append((url, path))
        if path == "/object_info":
            return {"ExampleNode": {}}
        raise ConnectionRefusedError("no responder")

    def absent(*args, **kwargs):
        raise ConnectionRefusedError("no Pinokio")

    monkeypatch.setattr(local, "scan", scan)
    monkeypatch.setattr(local, "_get_json", absent)
    monkeypatch.setattr(desktop_install, "observe_installations",
                        lambda **kw: copy.deepcopy(state["installations"]))
    monkeypatch.setattr(preflight, "_comfy_get_json", probe)
    return state, calls


def assert_no_writes(store, tmp_path, before=None):
    if before is None:
        before = {}
    assert store.documents == before
    assert not (tmp_path / ".vibecomfy").exists()


@pytest.mark.parametrize("case,reason", [
    ("running", None), ("closed", "closed"), ("stale", "stale_registration"),
    ("wrong_responder", "not_comfyui"), ("wrong_install", "not_this_install"),
    ("denied", "access_denied"), ("ambiguous", "cannot_tell_which_install"),
    ("incomplete", "discovery_incomplete"),
])
def test_bound_sdk_and_cli_payload(case, reason, scenario, tmp_path, generation_settings_store, capsys):
    state, calls = scenario
    state["installations"] = [installation_for(case)]
    payload = GenerationFacade().targets(placement=URL)
    assert cli.main(["targets", "--placement", URL, "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == payload
    assert payload["target"]["endpoint"] == URL
    assert payload["placement"] == {"resolved": {"kind": "attached", "endpoint": URL}, "source": "request"}
    assert payload["ladder"]["through"] == "compatible"
    if reason is None:
        assert payload["status"] == "ready" and payload["next_action"] is None
        assert "not been checked" in payload["summary"]
        assert calls == [(URL, "/object_info")] * 2
    else:
        assert payload["status"] == "blocked"
        assert len(payload["issues"]) == len(payload["fixes"]) == 1
        assert payload["issues"][0]["details"]["reason"] == reason
        assert "requires_consent" not in payload["next_action"]
        assert (payload["next_action"]["alternative"] is not None) == (case == "stale")
        assert calls == []
    assert_no_writes(generation_settings_store, tmp_path)


@pytest.mark.parametrize("case", INSTALLATION_DENIAL_CASES)
@pytest.mark.parametrize("selector", [URL, "auto"])
def test_installation_denial_public_payload_and_no_writes(
    case, selector, scenario, tmp_path, generation_settings_store, monkeypatch, capsys,
):
    state, calls = scenario
    state["installations"] = [denied_installation_for(case, tmp_path, monkeypatch)]
    before = copy.deepcopy((generation_settings_store.documents, generation_settings_store.receipts))
    facade = GenerationFacade()
    payload = facade.targets(placement=selector)
    assert payload == facade.targets(placement=selector)
    assert cli.main(["targets", "--placement", selector, "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == payload
    assert payload["target"]["endpoint"] == URL
    assert payload["placement"]["source"] == ("discovery" if selector == "auto" else "request")
    assert payload["status"] == "blocked"
    assert payload["ladder"]["stopped_at"] == "installed"
    rungs = payload["ladder"]["rungs"]
    assert rungs[0]["state"] == "unchecked" and rungs[0]["observation"] is None
    assert rungs[0]["reason"] == "installation_access_denied"
    assert all(rung["state"] == "unchecked" and rung["reason"] == "not_reached"
               for rung in rungs[1:])
    assert len(payload["issues"]) == len(payload["fixes"]) == 1
    assert payload["issues"][0]["details"]["reason"] == "installation_access_denied"
    action = payload["next_action"]
    assert action["who"] == "agent"
    assert action["argv"] == ["astrid", "generate", "targets", "--placement", selector]
    assert action["alternative"] is None and "requires_consent" not in action
    assert payload["fixes"][0]["scope"] == "read_only"

    # No bound or discovered path can save an unverified denied installation.
    message = "explicit target" if selector == "auto" else "installation_access_denied"
    with pytest.raises(GenerationInputError, match=message):
        facade.targets(placement=selector, save=True)
    assert calls == []
    assert (generation_settings_store.documents, generation_settings_store.receipts) == before
    assert_no_writes(generation_settings_store, tmp_path, before[0])


def test_unbound_attach_proposal_repeated_decline_and_explicit_yes(
    scenario, tmp_path, generation_settings_store, capsys, monkeypatch,
):
    facade = GenerationFacade()
    before = copy.deepcopy(generation_settings_store.documents)
    first = facade.targets()
    assert first["placement"]["source"] == "discovery"
    assert first["target"]["endpoint"] == URL
    action = first["next_action"]
    assert action["who"] == "agent" and action["requires_consent"] == "attach_existing"
    assert action["argv"] == ["astrid", "generate", "settings", "set", "placement", URL]
    assert first["ladder"]["through"] == "compatible"
    assert first == facade.targets()  # refusal = do not run the offered command
    assert cli.main(["targets", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == first
    assert_no_writes(generation_settings_store, tmp_path, before)

    # A fresh affirmative choice invokes the real existing placement writer.
    class Client:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False

    monkeypatch.setattr(cli, "_open_client", lambda: Client())
    monkeypatch.setattr(settings, "transport_of", lambda client: generation_settings_store)
    assert cli.main(action["argv"][2:]) == 0
    capsys.readouterr()
    assert generation_settings_store.documents["generation"][0]["placement"]["endpoint"] == URL
    bound = facade.targets()
    assert bound["placement"]["source"] == "settings" and bound["next_action"] is None
    assert bound["target"]["endpoint"] == placement.resolve_placement().endpoint == URL
    assert not (tmp_path / ".vibecomfy").exists()


def test_manual_open_reprobe_is_same_address_and_does_not_attach(scenario, generation_settings_store, tmp_path, capsys):
    state, calls = scenario
    state["installations"] = [installation_for("closed")]
    first = GenerationFacade().targets()
    action = first["next_action"]
    assert first["issues"][0]["details"]["reason"] == "closed"
    assert action["who"] == "person"
    assert action["then_argv"] == ["astrid", "generate", "targets", "--placement", URL]
    assert action["alternative"] is None
    assert calls == []
    state["installations"] = [installation_for("running")]
    assert cli.main(action["then_argv"][2:] + ["--json"]) == 0
    second = json.loads(capsys.readouterr().out)
    assert second["target"]["endpoint"] == URL
    assert second["status"] == "ready" and second["next_action"] is None
    assert calls == [(URL, "/object_info")]
    assert_no_writes(generation_settings_store, tmp_path)


def test_multiple_running_choices_never_offer_attach(scenario, tmp_path, generation_settings_store):
    state, _ = scenario
    state["servers"] = [{"url": OTHER, "folder_paths": {"custom_nodes": ["/portable/custom_nodes"]}}]
    payload = GenerationFacade().targets()
    assert payload["issues"][0]["details"]["reason"] == "several_installs"
    action = payload["next_action"]
    assert action["who"] == "person" and action["argv"] is None and action["alternative"] is None
    assert {row["endpoint"] for row in action["choices"]} == {URL, OTHER}
    assert all(row["label"] for row in action["choices"])
    assert "requires_consent" not in action
    assert_no_writes(generation_settings_store, tmp_path)


@pytest.mark.parametrize("case", ["wrong_install", "wrong_responder", "ambiguous", "incomplete"])
def test_explicit_save_refuses_unidentified(case, scenario, generation_settings_store, tmp_path):
    state, _ = scenario
    state["installations"] = [installation_for(case)]
    with pytest.raises(GenerationInputError, match="unidentified ComfyUI"):
        GenerationFacade().targets(placement=URL, save=True)
    assert_no_writes(generation_settings_store, tmp_path)


def test_explicit_legacy_save_closed_retains_prior_semantics(scenario, generation_settings_store, tmp_path):
    state, _ = scenario
    state["installations"] = [installation_for("closed")]
    payload = GenerationFacade().targets(placement=URL, save=True)
    assert payload["issues"][0]["details"]["reason"] == "closed"
    assert targets.read_target().endpoint == URL
    assert generation_settings_store.documents == {}


def test_save_cannot_bind_discovered_candidate(scenario, generation_settings_store, tmp_path):
    with pytest.raises(GenerationInputError, match="explicit target"):
        GenerationFacade().targets(save=True)
    assert_no_writes(generation_settings_store, tmp_path)


@pytest.mark.parametrize("failure,reason,offer", [
    (ConnectionRefusedError, "no_install_found", True),
    (TimeoutError, "timeout", False),
    (PermissionError, "access_denied", False),
    (OSError, "discovery_incomplete", False),
])
def test_absence_requires_complete_positive_observations(
    failure, reason, offer, scenario, monkeypatch, generation_settings_store, tmp_path, capsys,
):
    state, _ = scenario
    state["installations"] = []
    monkeypatch.setattr(preflight, "_comfy_get_json", lambda *args: (_ for _ in ()).throw(failure("probe")))
    payload = GenerationFacade().targets()
    assert payload["issues"][0]["details"]["reason"] == reason
    assert (payload["next_action"]["alternative"] is not None) is offer
    if offer:
        assert payload["next_action"]["alternative"]["owner"] == "plan-94"
        assert payload["next_action"]["alternative"]["available"] is False
        assert payload["next_action"]["then_argv"][-1] == "auto"
    assert cli.main(["targets", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == payload
    assert_no_writes(generation_settings_store, tmp_path)


def test_scan_error_never_becomes_absence(scenario, generation_settings_store, tmp_path):
    state, _ = scenario
    state["installations"] = []
    state["errors"] = [{"port": 8188, "endpoint": "/system_stats", "error": "unresolved"}]
    payload = GenerationFacade().targets()
    assert payload["issues"][0]["details"]["reason"] == "discovery_incomplete"
    assert payload["next_action"]["alternative"] is None
    assert_no_writes(generation_settings_store, tmp_path)


def test_explicit_url_unknown_filesystem_identity(scenario, monkeypatch, generation_settings_store, tmp_path):
    state, _ = scenario
    state["installations"] = []
    monkeypatch.setattr(preflight, "_comfy_get_json",
                        lambda url, path: {"system": {"comfyui_version": "0.8"}, "devices": []}
                        if path == "/system_stats" else {"ExampleNode": {}})
    payload = GenerationFacade().targets(placement=OTHER)
    assert payload["status"] == "ready" and payload["next_action"] is None
    assert payload["target"]["root"] is None and payload["target"]["python"] is None
    assert payload["target"]["layout"] == "unknown"
    assert payload["ladder"]["rungs"][2]["observation"]["tied_to_install"] is False
    assert_no_writes(generation_settings_store, tmp_path)


def test_inspect_and_execution_agree_on_precedence(
    scenario, monkeypatch, generation_settings_store, tmp_path,
):
    state, _ = scenario
    state["installations"] = []
    cfg = targets.config_path()
    cfg.parent.mkdir()
    cfg.write_text('[comfyui]\nurl = "http://127.0.0.1:18001"\n')
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "vibecomfy.toml").write_text('[comfyui]\nurl = "http://127.0.0.1:18002"\n')
    settings.set_placement("http://127.0.0.1:18003")
    monkeypatch.setenv("VIBECOMFY_COMFYUI_URL", "http://127.0.0.1:18004")
    facade = GenerationFacade()

    def check(selector, source, endpoint):
        result = facade.targets(placement=selector, project_root=repo)
        assert result["placement"]["source"] == source
        assert result["target"]["endpoint"] == endpoint
        assert placement.resolve_placement(placement.request_from_selector(selector), repo=repo).endpoint == endpoint

    check("http://127.0.0.1:18005", "request", "http://127.0.0.1:18005")
    check("auto", "env", "http://127.0.0.1:18004")
    monkeypatch.delenv("VIBECOMFY_COMFYUI_URL")
    check("auto", "settings", "http://127.0.0.1:18003")
    settings.unset_placement()
    check("auto", "repo", "http://127.0.0.1:18002")
    (repo / "vibecomfy.toml").unlink()
    check("auto", "global", "http://127.0.0.1:18001")
