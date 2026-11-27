"""R93-C: classifier evidence maps to the owned readiness prefix and one action."""
from copy import deepcopy

import pytest

from astrid.core.generation import desktop_install, preflight
from astrid.core.generation.desktop_install import observe_installations as real_observe_installations
from astrid.core.generation.targets import TargetRecord
from tests.fixtures.comfyui_desktop import get_json_for, load


def installation_for(case):
    source = "running" if case in {"denied", "ambiguous", "wrong_install", "incomplete"} else case
    fixture = load(source)
    if case == "ambiguous":
        fixture["responders"]["8000"].pop("folder_paths")
    elif case == "wrong_install":
        fixture["responders"]["8000"]["folder_paths"]["custom_nodes"] = [r"C:\Other\custom_nodes"]
    elif case == "incomplete":
        fixture["registration"]["data_dir"] = None
        fixture["files"]["data_dir"] = False
    responders = {}
    if all(fixture["files"].values()):
        responders = desktop_install.probe_ports(
            [8000, 8188],
            get_json=get_json_for(fixture, denied_ports=(8000,) if case == "denied" else ()),
        )
    return desktop_install.classify(fixture["registration"], files=fixture["files"], responders=responders)


INSTALLATION_DENIAL_CASES = (
    "registry", "config", "stat_executable", "stat_data_dir", "partial_registry",
)


def denied_installation_for(case, tmp_path, monkeypatch):
    """Exercise B's actual reader/classifier, injecting only denied OS I/O."""
    import json

    executable = tmp_path / "ComfyUI.exe"
    executable.touch()
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    entry = {"DisplayIcon": str(executable), "DisplayVersion": "0.7.2"}
    environ = {"APPDATA": str(tmp_path)}

    def registry():
        if case == "registry":
            raise PermissionError("registration denied")
        if case == "partial_registry":
            return [{**entry, "_denied": True}]
        return [entry]

    def read_text(path):
        if case == "config":
            raise PermissionError("config denied")
        return json.dumps({"basePath": str(data_dir)})

    real_stat = desktop_install.os.stat
    denied_path = {"stat_executable": executable, "stat_data_dir": data_dir}.get(case)

    def stat(path, *args, **kwargs):
        if denied_path is not None and str(path) == str(denied_path):
            raise PermissionError("stat denied")
        return real_stat(path, *args, **kwargs)

    def no_network(*args):
        pytest.fail("inaccessible registration must not trigger B's responder probe")

    with monkeypatch.context() as m:
        m.setattr(desktop_install.os, "stat", stat)
        # The public-payload scenario stubs the module attribute for SDK calls.
        # Keep this OS-I/O witness bound to B's real observer, not that stub.
        installations = real_observe_installations(
            environ=environ, registry=registry, read_text=read_text, get_json=no_network,
        )
    assert len(installations) == 1
    installation = installations[0]
    assert installation["state"] == "denied"
    assert installation["responder"]["kind"] == "none"
    assert installation["files"] == {
        "executable": case not in ("registry", "stat_executable"),
        "data_dir": case not in ("config", "stat_data_dir"),
    }
    if case == "registry":
        assert installation["recorded"]["executable"] is None
    if case == "config":
        assert installation["recorded"]["data_dir"] is None
    return installation


@pytest.mark.parametrize("case", INSTALLATION_DENIAL_CASES)
def test_denied_installation_evidence_never_advances_ladder(case, tmp_path, monkeypatch):
    installation = denied_installation_for(case, tmp_path, monkeypatch)
    record = TargetRecord(endpoint=installation["endpoint"])
    before = deepcopy(installation)
    ladder = preflight.observe_target_rungs(
        record, installation=installation, get_json=lambda *_: pytest.fail("unverified install probe"),
    )
    action = preflight.ladder_next_action(ladder, url=record.endpoint, installation=installation)
    payload = preflight.readiness_envelope(target=record, ladder=ladder, next_action=action)
    assert installation == before
    assert payload["status"] == "blocked"
    assert payload["ladder"]["stopped_at"] == "installed"
    assert ladder["rungs"][0]["state"] == "unchecked"
    assert ladder["rungs"][0]["observation"] is None
    assert ladder["rungs"][0]["reason"] == "installation_access_denied"
    assert all(item["state"] == "unchecked" and item["reason"] == "not_reached"
               and item["observation"] is None for item in ladder["rungs"][1:])
    assert len(payload["issues"]) == len(payload["fixes"]) == 1
    assert payload["issues"][0]["code"] == "rung_unchecked"
    assert payload["issues"][0]["details"]["reason"] == "installation_access_denied"
    assert action["who"] == "agent"
    assert action["argv"] == ["astrid", "generate", "targets", "--placement", record.endpoint]
    assert action["alternative"] is None and "requires_consent" not in action
    assert payload["fixes"][0]["scope"] == "read_only"


@pytest.mark.parametrize("case,rung,reason", [
    ("running", None, None),
    ("closed", "reachable", "closed"),
    ("stale", "installed", "stale_registration"),
    ("wrong_responder", "identified", "not_comfyui"),
    ("wrong_install", "identified", "not_this_install"),
    ("denied", "reachable", "access_denied"),
    ("ambiguous", "identified", "cannot_tell_which_install"),
    ("incomplete", "installed", "discovery_incomplete"),
])
def test_classifier_to_ladder(case, rung, reason):
    installation = installation_for(case)
    record = TargetRecord(endpoint=installation["endpoint"])
    before = deepcopy(installation)
    calls = []

    def probe(url, path):
        calls.append((url, path))
        assert case == "running"
        assert url == record.endpoint and path == "/object_info"
        return {"ExampleNode": {}}

    ladder = preflight.observe_target_rungs(record, installation=installation, get_json=probe)
    action = preflight.ladder_next_action(ladder, url=record.endpoint, installation=installation)
    payload = preflight.readiness_envelope(target=record, ladder=ladder, next_action=action)
    assert payload["ladder"]["through"] == "compatible"
    assert payload["ladder"]["stopped_at"] == rung
    assert installation == before
    assert all(item["state"] == "unchecked" for item in ladder["rungs"][4:])
    if reason is None:
        assert payload["status"] == "ready"
        assert action is None and payload["issues"] == payload["fixes"] == []
        assert ladder["rungs"][2]["observation"] == {"tied_to_install": True, "by": "data_dir"}
        assert calls == [(record.endpoint, "/object_info")]
        assert "not been checked" in preflight.ladder_summary(ladder, installation=installation)
        return
    assert calls == []
    assert payload["status"] == "blocked"
    assert len(payload["issues"]) == len(payload["fixes"]) == 1
    assert payload["issues"][0]["details"]["reason"] == reason
    assert action == payload["next_action"]
    index = preflight.LADDER_RUNGS.index(rung)
    assert all(item["state"] == "unchecked" for item in ladder["rungs"][index + 1:])
    assert action["alternative"] is None or reason == "stale_registration"
    assert "requires_consent" not in action
    if case == "closed":
        assert action["then_argv"] == ["astrid", "generate", "targets", "--placement", record.endpoint]
    if case == "stale":
        assert action["alternative"] == {
            "kind": "separate_copy", "owner": "plan-94", "available": False,
            "step": "Set up a separate copy of ComfyUI, with your consent.",
        }
    if case == "incomplete":
        assert ladder["rungs"][0]["state"] == "unchecked"
        assert ladder["rungs"][1]["state"] == "unchecked"
    if case == "denied":
        # Both registered paths were observed; the denial is at the responder.
        assert ladder["rungs"][0]["state"] == "observed"
        assert ladder["rungs"][0]["observation"]["files"] == {"executable": True, "data_dir": True}
        assert ladder["rungs"][1]["state"] == "failed"
        assert action["argv"] == ["astrid", "generate", "targets", "--placement", record.endpoint]


def test_unsupported_desktop_no_probe(monkeypatch):
    monkeypatch.setattr(preflight, "_platform", lambda: "linux")
    record = TargetRecord(layout="desktop", endpoint="http://127.0.0.1:8000")
    ladder = preflight.observe_target_rungs(record, get_json=lambda *args: pytest.fail("unsupported probe"))
    assert preflight.ladder_stop(ladder)["reason"] == "not_supported_here"
    action = preflight.ladder_next_action(ladder, url=record.endpoint)
    assert action["alternative"] is None


def test_running_api_incompatible_has_no_attach_or_offer():
    installation = installation_for("running")
    record = TargetRecord(endpoint=installation["endpoint"])
    ladder = preflight.observe_target_rungs(record, installation=installation, get_json=lambda *_: {})
    assert preflight.ladder_stop(ladder)["reason"] == "api_unsupported"
    action = preflight.ladder_next_action(ladder, url=record.endpoint, installation=installation, bound=False)
    assert action["alternative"] is None and "requires_consent" not in action


def test_consent_marker_round_trips_and_mirror_is_not_read_only():
    installation = installation_for("running")
    record = TargetRecord(endpoint=installation["endpoint"])
    ladder = preflight.observe_target_rungs(record, installation=installation, get_json=lambda *_: {"Node": {}})
    action = preflight.ladder_next_action(ladder, url=record.endpoint, installation=installation, bound=False)
    assert action == {
        "who": "agent", "step": f"Use the ComfyUI at {record.endpoint} for generation only after the person says yes.",
        "argv": ["astrid", "generate", "settings", "set", "placement", record.endpoint],
        "then_argv": None, "alternative": None, "choices": None, "requires_consent": "attach_existing",
    }
    payload = preflight.readiness_envelope(target=record, ladder=ladder, next_action=action)
    assert payload["next_action"] == action
    assert payload["fixes"][0]["scope"] == "attached_target"
    assert payload["fixes"][0]["details"]["requires_consent"] == "attach_existing"
    with pytest.raises(ValueError):
        preflight.make_next_action("agent", "Inspect.", argv=["astrid", "generate", "targets"],
                                   requires_consent="attach_existing")
