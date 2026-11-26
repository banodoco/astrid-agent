"""CD-1: bounded observations of an already selected human ComfyUI."""
import json
from urllib.error import HTTPError, URLError

import pytest

from astrid.core.generation import preflight
from astrid.core.generation.backends.comfy_target import MAX_JSON_BYTES, ComfyTargetClient
from astrid.core.generation.targets import TargetRecord


URL = "http://127.0.0.1:8188"
COMMAND = ["astrid", "generate", "targets", "--placement", URL]
HEALTHY_STATS = {"system": {"comfyui_version": "0.43.2"}}
HEALTHY_SCHEMA = {"ExampleNode": {}}


class Probe:
    def __init__(self, stats=HEALTHY_STATS, schema=HEALTHY_SCHEMA):
        self.responses = {"/system_stats": stats, "/object_info": schema}
        self.calls = []

    def __call__(self, url, path):
        assert url == URL
        self.calls.append((url, path))
        response = self.responses[path]
        if isinstance(response, Exception):
            raise response
        return response


def wrapped(exc):
    try:
        raise ValueError("private transport details") from URLError(exc)
    except ValueError as error:
        return error


def assert_failure(ladder, reason, *, who="person"):
    assert ladder["through"] == "compatible"
    stop = preflight.ladder_stop(ladder)
    assert stop["rung"] == preflight.LADDER_REASONS[reason]
    assert stop["state"] == "failed"
    assert stop["reason"] == reason
    assert stop["observation"] is None
    wording = preflight.LADDER_TEXT[reason]
    assert stop["message"] == wording["message"].format(url=URL)
    index = preflight.LADDER_RUNGS.index(stop["rung"])
    assert all(item["state"] == "observed" for item in ladder["rungs"][:index])
    assert all(
        item["state"] == "unchecked" and item["reason"] == "not_reached"
        and item["observation"] is None and item["message"] is None
        for item in ladder["rungs"][index + 1:]
    )
    action = preflight.ladder_next_action(ladder, url=URL)
    assert action == {
        "who": who, "step": wording["step"].format(url=URL),
        "argv": COMMAND if who == "agent" else None,
        "then_argv": COMMAND if who == "person" else None,
        "alternative": None, "choices": None,
    }
    envelope = preflight.readiness_envelope(
        target=TargetRecord(endpoint=URL), ladder=ladder, next_action=action,
    )
    assert envelope["status"] == "blocked"
    assert envelope["ladder"]["stopped_at"] == stop["rung"]
    assert envelope["next_action"] == action
    assert len(envelope["issues"]) == len(envelope["fixes"]) == 1
    assert envelope["issues"][0]["severity"] == "error"
    assert envelope["issues"][0]["details"]["reason"] == reason
    assert "private transport details" not in json.dumps(envelope)
    assert json.loads(json.dumps(ladder)) == ladder


def test_healthy_endpoint_only():
    fake = Probe()
    record = TargetRecord(endpoint=URL)
    ladder = preflight.observe_target_rungs(record, get_json=fake)
    assert ladder["through"] == "compatible"
    assert [rung["state"] for rung in ladder["rungs"]] == ["observed"] * 4 + ["unchecked"] * 3
    assert [rung["observation"] for rung in ladder["rungs"][:4]] == [
        {"identity": "endpoint_only"}, {"endpoint": URL},
        {"tied_to_install": False}, {"comfyui_version": "0.43.2"},
    ]
    assert preflight.ladder_stop(ladder) is None
    assert preflight.ladder_next_action(ladder, url=URL) is None
    envelope = preflight.readiness_envelope(target=record, ladder=ladder)
    assert envelope["status"] == "ready"
    assert envelope["next_action"] is None
    assert envelope["issues"] == envelope["fixes"] == []
    assert fake.calls == [(URL, "/system_stats"), (URL, "/object_info")]
    assert ladder == preflight.observe_target_rungs(record, get_json=Probe())
    assert json.loads(json.dumps(ladder)) == ladder


def test_existing_root_and_unknown_version(tmp_path):
    root = tmp_path / "ComfyUI"
    root.mkdir()
    record = TargetRecord(endpoint=URL, root=str(root))
    ladder = preflight.observe_target_rungs(record, get_json=Probe(stats={"system": {}}))
    assert ladder["rungs"][0]["observation"] == {"root_exists": True}
    assert ladder["rungs"][3]["observation"] == {"comfyui_version": None}
    assert preflight.ladder_stop(ladder) is None
    other = preflight.observe_target_rungs(
        record, get_json=Probe(stats={"system": {"comfyui_version": 42}}),
    )
    assert ladder == other


@pytest.mark.parametrize("kind", ["removed", "file"])
def test_missing_root_stops_before_probe(tmp_path, kind):
    root = tmp_path / "ComfyUI"
    if kind == "removed":
        root.mkdir()
        root.rmdir()
    else:
        root.write_text("not a directory")
    fake = Probe()
    ladder = preflight.observe_target_rungs(
        TargetRecord(endpoint=URL, root=str(root)), get_json=fake,
    )
    assert_failure(ladder, "files_missing")
    assert fake.calls == []


@pytest.mark.parametrize(("error", "reason", "who"), [
    (wrapped(ConnectionRefusedError("private transport details")), "not_answering", "person"),
    (wrapped(PermissionError("private transport details")), "access_denied", "agent"),
    (RuntimeError("private transport details"), "not_answering", "person"),
])
def test_wrapped_and_unexpected_failures(error, reason, who):
    fake = Probe(stats=error)
    ladder = preflight.observe_target_rungs(TargetRecord(endpoint=URL), get_json=fake)
    assert_failure(ladder, reason, who=who)
    assert fake.calls == [(URL, "/system_stats")]
    assert ladder == preflight.observe_target_rungs(TargetRecord(endpoint=URL), get_json=Probe(stats=error))


@pytest.mark.parametrize("stats", [
    HTTPError(URL + "/system_stats", 404, "private transport details", {}, None),
    {},
    {"system": []},
    [],
])
def test_reachable_but_not_identified(stats):
    fake = Probe(stats=stats)
    ladder = preflight.observe_target_rungs(TargetRecord(endpoint=URL), get_json=fake)
    assert_failure(ladder, "not_comfyui")
    assert ladder["rungs"][1]["observation"] == {"endpoint": URL}
    assert fake.calls == [(URL, "/system_stats")]


@pytest.mark.parametrize("schema", [RuntimeError("private transport details"), [], {}])
def test_identified_but_incompatible(schema):
    fake = Probe(schema=schema)
    ladder = preflight.observe_target_rungs(TargetRecord(endpoint=URL), get_json=fake)
    assert_failure(ladder, "api_unsupported")
    assert fake.calls == [(URL, "/system_stats"), (URL, "/object_info")]


@pytest.mark.parametrize("record", [TargetRecord(owner="astrid", endpoint=URL), TargetRecord(), None])
def test_only_a_human_owned_endpoint_is_accepted(record):
    fake = Probe()
    with pytest.raises(ValueError):
        preflight.observe_target_rungs(record, get_json=fake)
    assert fake.calls == []


def test_default_probe_unwraps_permission_denial(monkeypatch):
    calls = []

    def denied(self, path, **kwargs):
        assert self.target.url == URL
        assert self.target.kind == "attached"
        calls.append((path, kwargs))
        raise ValueError("private transport details") from URLError(PermissionError("private transport details"))

    monkeypatch.setattr(ComfyTargetClient, "request_json", denied)
    ladder = preflight.observe_target_rungs(TargetRecord(endpoint=URL))
    assert_failure(ladder, "access_denied", who="agent")
    assert calls == [("/system_stats", {"timeout": 1.0})]


def test_default_probe_uses_bounded_existing_client(monkeypatch):
    calls = []

    def respond(self, path, **kwargs):
        assert self.target.url == URL
        assert self.target.kind == "attached"
        calls.append((path, kwargs))
        return {"/system_stats": HEALTHY_STATS, "/object_info": HEALTHY_SCHEMA,
                "/internal/folder_paths": {"models": []}}[path]

    monkeypatch.setattr(ComfyTargetClient, "request_json", respond)
    assert preflight.ladder_stop(preflight.observe_target_rungs(TargetRecord(endpoint=URL))) is None
    assert preflight._comfy_get_json(URL, "/internal/folder_paths") == {"models": []}
    assert calls == [
        ("/system_stats", {"timeout": 1.0}),
        ("/object_info", {"timeout": 5.0, "response_limit": MAX_JSON_BYTES}),
        ("/internal/folder_paths", {"timeout": 1.0}),
    ]


def test_default_probe_is_looked_up_at_call_time(monkeypatch):
    fake = Probe()
    monkeypatch.setattr(preflight, "_comfy_get_json", fake)
    assert preflight.ladder_stop(preflight.observe_target_rungs(TargetRecord(endpoint=URL))) is None
    assert fake.calls == [(URL, "/system_stats"), (URL, "/object_info")]


def test_reason_table_and_unknown_reason_default():
    assert preflight.LADDER_REASONS == {
        "files_missing": "installed", "not_answering": "reachable",
        "access_denied": "reachable", "not_comfyui": "identified",
        "api_unsupported": "compatible",
    }
    assert set(preflight.LADDER_TEXT) == set(preflight.LADDER_REASONS)
    ladder = preflight.with_rung(
        preflight.new_ladder("compatible"), "installed", "failed", reason="future_reason",
    )
    assert preflight.ladder_next_action(ladder) == preflight.readiness_envelope(ladder=ladder)["next_action"]
    action = preflight.ladder_next_action(ladder, url=URL, installation={"unused": True})
    assert action["then_argv"] == COMMAND


def test_http_error_has_priority_across_bounded_exception_links():
    exc = URLError(PermissionError("private transport details"))
    exc.__cause__ = HTTPError(URL, 403, "private transport details", {}, None)
    assert preflight._failure_kind(exc) == "answered_not_comfyui"
    exc.__cause__ = exc
    assert preflight._failure_kind(exc) == "access_denied"
