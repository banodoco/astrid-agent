from __future__ import annotations

import copy
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from astrid.sdk.remote import RemoteTimelines
from astrid.sdk.timeline._common import TimelineEditError
from astrid.sdk.timeline.commands import execute
from tests.sdk.test_timeline_sheet_native import initial_bundle


def _create_empty(monkeypatch, config):
    before = {
        "parent": {
            "config": {
                "tracks": [
                    {"id": "V1", "kind": "visual"},
                    {"id": "A1", "kind": "audio"},
                ],
                "clips": [],
            },
            "clips": [],
            "registry": {},
        },
        "shots": {},
        "placements": [],
    }
    initialized = copy.deepcopy(before)
    initialized["parent"]["name"] = "Main"
    initialized["parent"]["slug"] = "main"
    initialized["parent"]["config"]["output"] = {
        "resolution": "1280x720", "fps": 30, "file": "output.mp4",
        "background": None, "background_scale": None,
    }

    class Session:
        outbox = SimpleNamespace(locked=nullcontext)

        def _state(self):
            return {"flight": None, "pending": []}

        def _bundle(self, state):
            return copy.deepcopy(before)

        def commit_many(self, commands, **kwargs):
            captured["commands"] = copy.deepcopy(commands)
            return {"commands": copy.deepcopy(commands)}

    captured = {}
    session = Session()
    monkeypatch.setattr(
        "astrid.sdk.timeline.sync.TimelineSession.open",
        classmethod(lambda cls, *args, **kwargs: session),
    )
    monkeypatch.setattr(
        "astrid.sdk.timeline.native_initialize.capture_initialize",
        lambda *args, **kwargs: {"schema_version": 1, "op": "timeline.init", "version": "1",
                                 "select": "parent", "args": {"name": "Main", "slug": "main",
                                                                    "output": {}}},
    )
    monkeypatch.setattr(
        "astrid.sdk.timeline.commands.execute",
        lambda bundle, commands: SimpleNamespace(bundle=copy.deepcopy(initialized)),
    )
    client = RemoteTimelines(SimpleNamespace())
    monkeypatch.setattr(client, "_resolve_project_ref", lambda project: SimpleNamespace(ok=True, data="p"))
    monkeypatch.setattr(client, "_typed", lambda *args, **kwargs: SimpleNamespace(
        ok=True, data={"project_id": "p"}, error=None,
    ))
    monkeypatch.setattr(client, "_native_empty_replay", lambda *args: None)
    monkeypatch.setattr(RemoteTimelines, "_native_empty_result",
                        staticmethod(lambda project, timeline, name, result, key: result))

    result = client.create_empty(project="p", timeline_id="film", config=config,
                                idempotency_key="create-content")
    return result, captured["commands"]


def test_create_empty_declares_picture_and_audio_track_and_clip_ids(monkeypatch):
    config = {
        "tracks": [
            {"id": "picture", "kind": "visual"},
            {"id": "voice", "kind": "audio"},
        ],
        "clips": [
            {"id": "picture-clip", "track": "picture", "clipType": "am-type",
             "at": 0, "hold": 2, "params": {"text": "frame"}},
            {"id": "voice-clip", "track": "voice", "clipType": "audio",
             "at": 0, "hold": 2, "params": {}},
        ],
    }

    _, commands = _create_empty(monkeypatch, config)

    assert [command["op"] for command in commands] == ["timeline.init", "doc.patch"]
    assert commands[1]["created"] == [
        "parent/track:picture", "parent/track:voice",
        "parent/clip:picture-clip", "parent/clip:voice-clip",
    ]


def test_create_empty_admits_raw_registry_media_with_initialization(monkeypatch):
    picture = "sha256:" + "a" * 64
    voice = "sha256:" + "b" * 64
    config = {
        "registry": {"assets": {
            "picture": {"media_id": picture},
            "voice": {"media_id": voice},
        }},
        "clips": [
            {"id": "picture-clip", "track": "V1", "asset": "picture",
             "clipType": "media", "at": 0, "hold": 2},
            {"id": "voice-clip", "track": "A1", "asset": "voice",
             "clipType": "media", "at": 0, "hold": 2},
        ],
    }

    _, commands = _create_empty(monkeypatch, config)

    assert [command["op"] for command in commands] == [
        "timeline.init", "media.admit", "doc.patch",
    ]
    assert commands[2]["created"] == [
        "parent/clip:picture-clip", "parent/clip:voice-clip",
        "parent/asset:picture", "parent/asset:voice",
    ]
    assert commands[1]["args"]["inputs"] == [
        {"pin": {"media_id": picture, "content_digest": picture}},
        {"pin": {"media_id": voice, "content_digest": voice}},
    ]


def test_config_patch_must_declare_exact_created_ids():
    bundle = initial_bundle()
    before_config = copy.deepcopy(bundle["parent"]["config"])
    after_config = copy.deepcopy(before_config)
    after_config["tracks"].append({"id": "voice", "kind": "audio"})
    after_config["clips"] = [*copy.deepcopy(bundle["parent"]["clips"]), {
        "id": "voice-clip", "track": "voice", "clipType": "audio", "at": 1,
        "hold": 1, "params": {},
    }]
    after_config["registry"] = {"assets": {"voice-asset": {"type": "audio/wav"}}}
    change = {"op": "set", "ref": "parent", "path": ["config"],
              "before": {"state": "present", "value": before_config}, "value": after_config}
    expected = ["parent/track:voice", "parent/clip:voice-clip", "parent/asset:voice-asset"]

    command = {"schema_version": 1, "op": "doc.patch", "version": "1", "select": None,
               "args": {"changes": [change]}, "created": expected}
    plan = execute(bundle, [command])
    assert plan.commands[0]["created"] == expected

    for forged in (None, [], [*expected, "parent/track:forged"], expected[:-1]):
        bad = copy.deepcopy(command)
        if forged is None:
            bad.pop("created")
        else:
            bad["created"] = forged
        with pytest.raises(TimelineEditError, match="declare every created identity"):
            execute(bundle, [bad])


def test_native_create_replay_returns_the_original_create_with_content_receipt(monkeypatch):
    created_ids = ["parent/track:voice", "parent/clip:voice-clip", "parent/asset:voice-asset"]
    receipt = {
        "idempotency_key": "create-content-head",
        "command_kind": "timeline.commit",
        "result": {"commands": [
            {"op": "timeline.init"},
            {"op": "doc.patch", "created": created_ids},
        ]},
    }
    client = RemoteTimelines(SimpleNamespace())
    monkeypatch.setattr(client, "_typed", lambda *args, **kwargs: SimpleNamespace(
        ok=True, data={"items": [{"receipt": receipt}], "next_cursor": None}, error=None,
    ))
    seen = {}

    def result(project_id, timeline_id, name, replay, result_key):
        seen.update(project_id=project_id, timeline_id=timeline_id, name=name,
                    replay=replay, result_key=result_key)
        return "replayed"

    monkeypatch.setattr(RemoteTimelines, "_native_empty_result", staticmethod(result))

    assert client._native_empty_replay("project", "film", "create-content-head", "create-content") == "replayed"
    assert seen == {
        "project_id": "project", "timeline_id": "film", "name": None,
        "replay": {"data": receipt["result"], "receipt": receipt},
        "result_key": "create-content",
    }
