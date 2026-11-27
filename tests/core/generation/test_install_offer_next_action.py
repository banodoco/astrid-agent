"""94-F: public P93 actions consume real, read-only install previews."""
import copy
import json
import platform

import pytest

from astrid.core import engine_cli
from astrid.core.generation import cli, comfy_portable as portable, preflight
from astrid.sdk.generation import GenerationFacade, GenerationInputError
from tests.core.generation.environment_states import snapshot
from tests.core.generation.test_comfy_portable_install import case, apply, interrupt_extract
from tests.core.generation.test_desktop_rungs import installation_for
from tests.core.generation.test_generate_targets_desktop import scenario, URL, OTHER


@pytest.fixture
def windows_offer(monkeypatch, case, scenario):
    # Real Portable offer and CLI; only release, disk and GPU evidence are fixtures.
    monkeypatch.setenv("LOCALAPPDATA", str(case.machine.localappdata))
    monkeypatch.setattr(portable, "load_pin", lambda: case.pin)
    real_build = portable.build_offer

    def build(destination, **kw):
        return real_build(destination, **{**kw, **case.kwargs})

    monkeypatch.setattr(portable, "build_offer", build)
    return case


def _unchanged(tmp_path, store, before):
    assert (snapshot(tmp_path), store.documents, store.receipts) == before


def _snapshot(tmp_path, store):
    return snapshot(tmp_path), copy.deepcopy(store.documents), copy.deepcopy(store.receipts)


@pytest.mark.parametrize("installation,reason", [(None, "no_install_found"),
                                                  ("stale", "stale_registration")])
def test_eligible_windows_preview(installation, reason, windows_offer, scenario,
                                  tmp_path, generation_settings_store, capsys):
    state, _ = scenario
    state["installations"] = [installation_for(installation)] if installation else []
    before = _snapshot(tmp_path, generation_settings_store)
    payload = GenerationFacade().targets()
    assert payload["status"] == "blocked"
    assert payload["issues"][0]["details"]["reason"] == reason
    action = payload["next_action"]
    assert action["who"] == "person"
    alternative = action["alternative"]
    argv = ["astrid", "generate", "install-comfyui", "--destination",
            str(windows_offer.destination), "--json"]
    assert alternative["available"] is True and alternative["argv"] == argv
    assert "requires_consent" not in action and "--apply" not in argv
    assert cli.main(["targets", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == payload
    assert cli.main(argv[2:]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "offered"
    assert windows_offer.fake.work_calls == (0, 0)
    _unchanged(tmp_path, generation_settings_store, before)


def test_decline_is_current_and_not_install_consent(windows_offer, scenario, tmp_path,
                                                   generation_settings_store, capsys):
    before = _snapshot(tmp_path, generation_settings_store)
    facade = GenerationFacade()
    initial = facade.targets()
    assert initial["next_action"]["requires_consent"] == "attach_existing"
    assert initial["next_action"]["argv"][-1] == URL
    declined = facade.targets(decline_attach=URL)
    action = declined["next_action"]
    assert action["who"] == "agent" and action["argv"][2] == "install-comfyui"
    assert "--apply" not in action["argv"] and "requires_consent" not in action
    assert facade.targets() == initial
    assert cli.main(["targets", "--decline-attach", URL, "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == declined
    assert cli.main(action["argv"][2:]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "offered"
    with pytest.raises(GenerationInputError):
        facade.targets(decline_attach=OTHER)
    with pytest.raises(GenerationInputError):
        facade.targets(decline_attach=URL, save=True)
    assert windows_offer.fake.work_calls == (0, 0)
    _unchanged(tmp_path, generation_settings_store, before)


@pytest.mark.parametrize("installation,reason", [
    ("closed", "closed"), ("denied", "access_denied"),
    ("ambiguous", "cannot_tell_which_install"), ("wrong_responder", "not_comfyui"),
    ("wrong_install", "not_this_install"), ("incomplete", "discovery_incomplete"),
])
def test_ineligible_first_stop_never_offers(installation, reason, scenario, monkeypatch):
    state, _ = scenario
    state["installations"] = [installation_for(installation)]
    monkeypatch.setattr(preflight, "separate_copy_preview",
                        lambda **kw: pytest.fail("ineligible install preview"))
    payload = GenerationFacade().targets(placement=URL)
    assert payload["issues"][0]["details"]["reason"] == reason
    assert payload["next_action"]["alternative"] is None


def test_unknown_origin_does_not_offer(scenario, monkeypatch):
    state, _ = scenario
    state["installations"] = []
    monkeypatch.setattr(preflight, "_comfy_get_json", lambda url, path:
                        {"system": {"comfyui_version": "0.3"}, "devices": []}
                        if path == "/system_stats" else {"ExampleNode": {}})
    monkeypatch.setattr(preflight, "separate_copy_preview",
                        lambda **kw: pytest.fail("healthy responder isn't absence"))
    assert GenerationFacade().targets(placement=OTHER)["next_action"] is None


@pytest.mark.parametrize("phase", ["partial", "complete"])
def test_partial_and_complete_preview_never_reinstalls(phase, windows_offer, scenario,
                                                       tmp_path, generation_settings_store, capsys):
    state, _ = scenario
    state["installations"] = []
    if phase == "partial":
        interrupt_extract(windows_offer)
    else:
        apply(windows_offer)
    before = _snapshot(tmp_path, generation_settings_store)
    work = windows_offer.fake.work_calls
    alternative = GenerationFacade().targets()["next_action"]["alternative"]
    assert alternative["available"] is True
    assert cli.main(alternative["argv"][2:]) == 0
    offer = json.loads(capsys.readouterr().out)
    assert offer["status"] == "offered"
    assert offer["state"] == ("ours" if phase == "partial" else "complete")
    assert windows_offer.fake.work_calls == work
    _unchanged(tmp_path, generation_settings_store, before)


def test_null_pin_stays_unavailable(case, scenario, monkeypatch):
    state, _ = scenario
    state["installations"] = []
    monkeypatch.setattr(portable, "load_pin", lambda: {**case.pin, "release": None})
    monkeypatch.setattr(portable, "build_offer", lambda *a, **kw: pytest.fail("null pin"))
    alternative = GenerationFacade().targets()["next_action"]["alternative"]
    assert alternative["available"] is False and not alternative.get("argv")


@pytest.mark.parametrize("os_name,machine", [("darwin", "arm64"), ("linux", "aarch64")])
def test_unsupported_preview(scenario, monkeypatch, tmp_path, os_name, machine):
    monkeypatch.setattr(preflight, "_platform", lambda: os_name)
    monkeypatch.setattr(platform, "machine", lambda: machine)
    assert preflight.separate_copy_preview(install_root=str(tmp_path / "selected")) is None


def test_linux_explicit_root_actual_dry_run(scenario, monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(preflight, "_platform", lambda: "linux")
    monkeypatch.setattr(platform, "machine", lambda: "x86_64")
    root = str(tmp_path / "chosen")
    argv = ["astrid", "engines", "install", "comfyui", "--platform",
            "linux-x86_64-cu130", "--placement", "local", "--root", root, "--dry-run"]
    assert preflight._linux_preview_argv(root) == argv
    assert preflight._linux_preview_argv(None) is None
    assert preflight._linux_preview_argv("relative") is None
    # Before D, the old policy floor is not an offer.
    monkeypatch.delattr(engine_cli, "_managed_preview", raising=False)
    assert preflight.separate_copy_preview(install_root=root) is None
    # Actual public legacy dry-run, without provisioning or root creation.
    assert engine_cli.main(argv[2:], backend_factory=lambda **kw: pytest.fail("install")) == 0
    assert json.loads(capsys.readouterr().out)
    assert not (tmp_path / "chosen").exists()


def test_linux_qualified_producer_boundary(scenario, monkeypatch, tmp_path):
    # D's shape is a simulated boundary, not evidence of a real measured offer.
    monkeypatch.setattr(preflight, "_platform", lambda: "linux")
    monkeypatch.setattr(platform, "machine", lambda: "x86_64")
    calls = []

    def preview(plan, recipe, deadline):
        calls.append(plan)
        return {"available": True, "offer_sha256": "a" * 64}

    monkeypatch.setattr(engine_cli, "_managed_preview", preview, raising=False)
    assert preflight.separate_copy_preview() is None
    assert calls == []
    root = str(tmp_path / "selected")
    assert preflight.separate_copy_preview(install_root=root) == preflight._linux_preview_argv(root)
    assert len(calls) == 1 and not (tmp_path / "selected").exists()
