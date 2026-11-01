from __future__ import annotations

import json

from astrid.packs.timeline.cli import build_parser
from astrid.sdk.contracts import DomainResult
from astrid.sdk.remote import RemoteTimelines


def test_remote_timelines_replace_clip_is_typed_410() -> None:
    class Transport:
        def replace_timeline_clip(self, *args, **kwargs):
            raise AssertionError("retired route must not reach the generated document mutation")

    result = RemoteTimelines(Transport()).replace_clip(
        "project-1", "timeline-1", clip_id="clip-1",
        source_object_id="sha256:" + "b" * 64, expected_version=4,
        idempotency_key="replace-4",
    )
    assert not result.ok
    assert result.error.code == "retired_route"
    assert result.error.details["status"] == 410


def test_timeline_cli_has_no_replace_clip_command() -> None:
    client = type("Client", (), {})()
    client.timelines = type("Timelines", (), {})()
    parser = build_parser(client)
    choices = parser._subparsers._group_actions[0].choices
    assert "replace-clip" not in choices
    assert "save" not in choices
    assert "retime-clip" not in choices


def test_remote_parent_media_replacement_publishes_exact_closure_once() -> None:
    from tests.timeline.test_authoring_bundle import NEW, _closure, _digest

    parent, shots, internals = _closure(shared=False)
    internal = internals[0]
    internal["payload"]["registry"]["assets"]["new-picture"] = {
        "media_id": NEW,
        "type": "image",
    }
    internal["content_digest"] = _digest(internal["payload"])
    shot = shots[0]
    parent["content_digest"] = _digest(parent["payload"])

    class Transport:
        def __init__(self):
            self.published = []

        def list_timelines(self, project, *, cursor=None, limit=50):
            return ([{"project_id": project, "timeline_id": "main", "slug": "main"}], None)

        def get_timeline(self, timeline_id, *, project_id=None):
            raise AssertionError("canonical parent replacement must not read the legacy timeline document")

        def get_project_parent_composition_revision(self, project_id, timeline_id, revision):
            assert (project_id, timeline_id, revision) == ("project-1", "main", "parent-1")
            return parent

        def get_project_shot_revision(self, project_id, shot_id, revision):
            assert (project_id, shot_id, revision) == ("project-1", "shot-shared", "shot-rev-1")
            return shot

        def get_project_timeline_revision(self, project_id, timeline_id, revision):
            assert (project_id, timeline_id, revision) == ("project-1", "main", "internal-1")
            return internal

        def publish_parent_composition(self, project_id, timeline_id, publication, *, idempotency_key):
            self.published.append((project_id, timeline_id, publication, idempotency_key))
            return {"data": {"new_head": publication["parent_revision_id"]}, "receipt": None}

    transport = Transport()
    result = RemoteTimelines(transport).replace_parent_media(
        "project-1",
        "main",
        occurrence_id="occ-1",
        clip_id="picture-1",
        source_object_id=NEW,
        expected_head="parent-1",
        idempotency_key="parent-replace-1",
    )

    assert result.ok, result.error
    assert len(transport.published) == 1
    _project, _timeline, publication, key = transport.published[0]
    assert key == "parent-replace-1"
    child = publication["internal_timeline_revisions"][0]["payload"]
    selected = next(clip for clip in child["clips"] if clip["id"] == "picture-1")
    assert selected["asset"] == "new-picture"
    assert selected["at"] == 0 and selected["hold"] == 1
    assert publication["expected_head"] == "parent-1"


def test_parent_media_cli_forwards_exact_locator_and_head(capsys) -> None:
    class Timelines:
        def __init__(self):
            self.calls = []

        def replace_parent_media(self, *args, **kwargs):
            self.calls.append((args, kwargs))
            return DomainResult.success({"representation": "parent_composition"}, idempotency_key=kwargs["idempotency_key"])

    class Client:
        def __init__(self):
            self.timelines = Timelines()

    client = Client()
    parser = build_parser(client)
    parsed = parser.parse_args([
        "replace-parent-media", "--project", "project-1", "main",
        "--occurrence-id", "occ-1", "--clip-id", "picture-1",
        "--source-object-id", "sha256:" + "d" * 64,
        "--expected-head", "parent-1", "--idempotency-key", "parent-1",
    ])

    assert parsed.handler(parsed) == 0
    assert json.loads(capsys.readouterr().out)["data"] == {"representation": "parent_composition"}
    assert client.timelines.calls == [
        (("project-1", "main"), {
            "occurrence_id": "occ-1",
            "clip_id": "picture-1",
            "source_object_id": "sha256:" + "d" * 64,
            "expected_head": "parent-1",
            "idempotency_key": "parent-1",
        })
    ]
