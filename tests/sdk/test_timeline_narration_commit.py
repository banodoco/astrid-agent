"""Narration publication is a native text-binding commit, not a shot write."""
from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

import pytest

from astrid.sdk.timeline import publish
from astrid.sdk.timeline.checkout import Checkout
from astrid.sdk.timeline._common import TimelineEditError
from tests.sdk.test_timeline_checkout import bundle


def descriptor(data: bytes) -> dict[str, object]:
    digest = "sha256:" + hashlib.sha256(data).hexdigest()
    return {"object_id": digest, "digest": digest, "size": len(data),
            "media_type": "text/plain; charset=utf-8"}


def modern_bundle():
    data = bundle()
    data["source_mapping"] = {"shots": {
        shot_id: {"shot_id": shot_id} for shot_id in data["shots"]
    }}
    shot = data["shots"]["A"]
    shot["internal_timeline"]["clips"][2]["app"]["text"] = "It really went viral."
    old = descriptor(b"It went viral.\n")
    shot["payload"]["text_bindings"] = [{
        "kind": "voiceover_script", "slot": None,
        "media_id": old["object_id"], "content_hash": old["digest"],
        "byte_size": old["size"], "mime_type": old["media_type"],
    }]
    return data


def test_capture_narration_uses_occurrence_semantic_command_and_exact_bytes():
    data = modern_bundle()
    commands, verified = publish._capture_narration_commands(
        data, {"A": "It really went viral.\n"}
    )

    assert len(commands) == 1
    assert commands[0]["op"] == "text-binding.set"
    assert commands[0]["select"] == "occurrence:occ-A"
    text = commands[0]["args"]["text"]
    assert text == descriptor(b"It really went viral.\n")
    assert verified == [{**text, "utf8": True}]
    assert data["shots"]["A"]["payload"]["text_bindings"][0]["content_hash"] != text["digest"]


def test_shared_main_shot_reports_the_missing_commit_ownership_seam():
    data = modern_bundle()
    data["placements"].append({**data["placements"][0], "occurrence_id": "occ-A-copy"})
    with pytest.raises(TimelineEditError, match="no commit-owned occurrence seam"):
        publish._capture_narration_commands(data, {"A": "It really went viral.\n"})


def test_publish_narration_ingests_bytes_and_submits_one_commit(monkeypatch):
    data = modern_bundle()
    checkout = Checkout(data)

    class Media:
        calls = []

        def import_bytes(self, **kwargs):
            self.calls.append(kwargs)
            return SimpleNamespace(ok=True, data=descriptor(kwargs["data"]))

    class Lock:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    class Session:
        def __init__(self):
            self.outbox = SimpleNamespace(locked=lambda: Lock())
            self.state = {"pending": [], "flight": None, "base": {
                "revision_id": "r0", "publication_id": "p0"}}
            self.submitted = None

        def _state(self):
            return self.state

        def _bundle(self, state):
            return data

        def submit(self, commands, **kwargs):
            self.submitted = (commands, kwargs)
            return {"data": {"accepted_revision_id": "r1"}}

    session = Session()
    monkeypatch.setattr(publish, "_NARRATION_MEDIA_TYPE", "text/plain; charset=utf-8")
    monkeypatch.setattr(
        "astrid.sdk.timeline.sync.TimelineSession.open",
        lambda *args, **kwargs: session,
    )
    client = SimpleNamespace(media=Media())

    result = checkout._publish_narration("edit-key", client=client)

    assert result == {"narration_pinned": ["A"], "new_head": "r1", "narration_head": "r1"}
    assert len(client.media.calls) == 1
    assert client.media.calls[0]["data"] == b"It really went viral.\n"
    commands, options = session.submitted
    assert len(commands) == 1 and commands[0]["op"] == "text-binding.set"
    assert options["label"] == "Bind narration"
    assert options["idempotency_key"] == "edit-key-narration"


def test_publish_narration_retries_existing_flight_without_recapture(monkeypatch):
    checkout = Checkout(modern_bundle())

    class Lock:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    class Session:
        def __init__(self):
            body = {"label": "Bind narration", "commands": [{"op": "text-binding.set"}]}
            self.outbox = SimpleNamespace(locked=lambda: Lock())
            self.state = {"pending": [{"op": "text-binding.set"}], "flight": {
                "key": "original-narration", "body": json.dumps(body)}, "base": {}}
            self.key = None

        def _state(self):
            return self.state

        def flush(self, *, idempotency_key):
            self.key = idempotency_key
            return {"data": {"accepted_revision_id": "r2"}}

    session = Session()
    monkeypatch.setattr(
        "astrid.sdk.timeline.sync.TimelineSession.open",
        lambda *args, **kwargs: session,
    )
    client = SimpleNamespace(media=SimpleNamespace(
        import_bytes=lambda **kwargs: pytest.fail("retry re-ingested narration bytes")
    ))

    result = checkout._publish_narration("new-outer-key", client=client)

    assert result["new_head"] == "r2"
    assert session.key == "original-narration"
