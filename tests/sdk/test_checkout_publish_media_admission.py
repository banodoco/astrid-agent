"""Ordinary Checkout.publish admits newly referenced media through Runtime."""
from __future__ import annotations

import copy

import pytest

from astrid.sdk.timeline.checkout import Checkout

pytest_plugins = ("tests.sdk.test_timeline_normal_publish",)


def _add_media_clip(checkout, media_id: str, suffix: str) -> None:
    internal = checkout.bundle["shots"][checkout._shot_ids()[0]]["internal_timeline"]
    internal["registry"]["assets"][suffix] = {"media_id": media_id}
    internal["clips"].append({
        "id": "new-" + suffix,
        "clipType": "image",
        "track": "sprite",
        "asset": suffix,
        "at": 0.0,
        "hold": 1.0,
        "params": {},
    })


def test_publish_admits_new_media_and_retries_the_canonical_request(restructure_runtime):
    env = restructure_runtime
    media = env.service.ingest(env.project, b"ordinary-new-media", media_type="image/png",
                               idempotency_key="ordinary-new-media")["data"]["object_id"]
    checkout = Checkout.open(env.project, "t", client=env.client)
    _add_media_clip(checkout, media, "ordinary-new")

    result = checkout.publish("Admit ordinary media", client=env.client, idempotency_key="ordinary-media")
    body = env.calls[0][0]
    assert [call["op"] for call in body["commands"][:-1]] == ["doc.patch"] * (len(body["commands"]) - 1)
    assert body["commands"][-1] == {
        "schema_version": 1,
        "op": "media.admit",
        "version": "1",
        "select": None,
        "args": {"inputs": [{"pin": {"media_id": media, "content_digest": media}}]},
    }
    assert body["steps"][0]["command_ordinals"] == list(range(len(body["commands"]) - 1))
    assert body["steps"][-1]["command_ordinals"] == [len(body["commands"]) - 1]
    assert result["receipt"] == env.replies[0]["receipt"]
    assert media in {row["media_id"] for row in env.replies[0]["data"]["canonical_document"]["dependency_manifest"]["media"]}

    retry = Checkout.open(env.project, "t", client=env.client).publish(
        "Admit ordinary media", client=env.client, idempotency_key="ordinary-media"
    )
    assert retry["receipt"] == result["receipt"]
    assert [key for _, key in env.calls] == ["ordinary-media", "ordinary-media-narration"]


def test_publish_refuses_foreign_media_without_writing(restructure_runtime, monkeypatch):
    from astrid.sdk.timeline.sync import CommitConflict

    env = restructure_runtime
    foreign = env.service.create_project({"slug": "foreign-ordinary", "name": "Foreign ordinary"},
                                          idempotency_key="foreign-ordinary")["id"]
    foreign_media = env.service.ingest(foreign, b"foreign-ordinary-media", media_type="image/png",
                                       idempotency_key="foreign-ordinary-media")["data"]["object_id"]
    checkout = Checkout.open(env.project, "t", client=env.client)
    local_media = env.service.ingest(env.project, b"local-ordinary-media", media_type="image/png",
                                     idempotency_key="local-ordinary-media")["data"]["object_id"]
    _add_media_clip(checkout, local_media, "foreign-test")
    before_document = copy.deepcopy(env.document())
    before_history = env.client.timelines.history(env.project, "t", limit=200).data
    send = env.generated.commit_timeline

    def forged(project, timeline, body, *, idempotency_key):
        body = copy.deepcopy(body)
        body["commands"][-1]["args"]["inputs"][0]["pin"] = {
            "media_id": foreign_media, "content_digest": foreign_media,
        }
        return send(project, timeline, body, idempotency_key=idempotency_key)

    monkeypatch.setattr(env.generated, "commit_timeline", forged)
    with pytest.raises(CommitConflict):
        checkout.publish("Reject foreign ordinary media", client=env.client,
                         idempotency_key="foreign-ordinary-publish")
    assert env.document() == before_document
    assert env.client.timelines.history(env.project, "t", limit=200).data == before_history
