"""R93-C: classifier evidence maps to the owned readiness prefix and one action."""
from copy import deepcopy

import pytest

from astrid.core.generation import desktop_install, preflight
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
