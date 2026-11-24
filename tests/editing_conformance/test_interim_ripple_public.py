"""Public ripple preview and fixed-list acceptance over 621 clip spans."""
from __future__ import annotations

import copy
import hashlib
from types import SimpleNamespace

import pytest

from astrid.core.receipts.canonical import canonical_json
from astrid.core.receipts.contract import CommandReceipt
from astrid.sdk.contracts import DomainResult
from astrid.sdk.timeline import store
from astrid.sdk.timeline._shot_word_retime import ShotWordRetimeInputs
from astrid.sdk.timeline.commit import _digest, bundle_from_document, create_runtime_commit_service
from astrid.sdk.timeline.commit_session import Timeline
from astrid.sdk.timeline.outbox import Outbox
from astrid.sdk.timeline.sync import TimelineSession
from tests.sdk.test_timeline_commit_provider import DetachedPort, DetachedRealm
from tests.sdk.test_timeline_retained_nudge_guidance import _canonical_nudge_inputs


def _film(anchored):
    bundle, capture = _canonical_nudge_inputs("stop")
    document = copy.deepcopy(bundle["source_mapping"]["_runtime_document"])
    internal, = document["internal_timeline_revisions"]
    internal["payload"]["output"]["fps"] = 30
    clips = internal["payload"]["clips"]
    if not anchored:
        clips[1].pop("app")
        document["dependency_manifest"]["documents"].clear()
        config = document["parent_revision"]["payload"]["config"]
        config["bundle"].pop("transcriptReferences")
    clips.extend(
        {"id": f"early-{frame}", "clipType": "text", "track": "overlay",
         "at": frame / 30, "hold": 1 / 30}
        for frame in range(15)
    )
    clips.extend(
        {"id": f"late-{frame}", "clipType": "text", "track": "overlay",
         "at": frame / 30, "hold": 1 / 30}
        for frame in range(103, 706)
    )
    occurrence, = document["parent_revision"]["payload"]["occurrences"]
    occurrence["duration_ms"] = 25000
    occurrence["source_offset"] = 0
    occurrence["speed"] = 1
    internal["content_digest"] = _digest(internal["payload"])
    document["parent_revision"]["content_digest"] = _digest(document["parent_revision"]["payload"])
    for row in document["dependency_manifest"]["internal_timelines"]:
        row["content_digest"] = internal["content_digest"]
    return document, capture if anchored else None


def _open_film(tmp_path, monkeypatch, document, capture):
    realm = DetachedRealm(document)
    calls = []
    identity = {"realm_id": "realm-1", "actor_id": "actor-1"}
    transport = SimpleNamespace(_last_handshake=identity)
    if capture:
        realm.retained_sources[_digest(capture["pin"])] = copy.deepcopy(capture)

        def verified_document(project, pin):
            assert (project, pin) == ("project-1", capture["pin"])
            return copy.deepcopy(capture)

        transport.get_project_verified_document = verified_document

    def send(project, timeline, body, *, idempotency_key):
        assert (project, timeline) == ("project-1", "film")
        calls.append(copy.deepcopy(body))
        base = {"revision_id": body["base_revision_id"], "publication_id": body["base_publication_id"]}
        context = realm.context(body["commands"], base=base, dry_run=body.get("dry_run", False))
        context["request"] = {"rationale": "Public ripple", **copy.deepcopy(body)}
        context["idempotency_key"] = idempotency_key
        invocation = {"operation": "commitTimeline", "path": "/v1/projects/project-1/timelines/film/commit",
                      "publisher": identity["actor_id"], "request": body}
        context["request_hash"] = hashlib.sha256(canonical_json(invocation).encode()).hexdigest()
        result = create_runtime_commit_service().commit(context, DetachedPort(realm, context))
        receipt = result.get("receipt")
        if receipt:
            receipt["project_seq"] *= 2
            realm.entries[-1]["receipt"] = copy.deepcopy(receipt)
        return DomainResult.success(result["data"], receipt=CommandReceipt.from_dict(receipt) if receipt else None)

    def fetch(project, timeline, *, revision_id, publication_id=None, client=None):
        assert (project, timeline) == ("project-1", "film")
        assert any(entry["revision_id"] == revision_id and entry["publication_id"] == publication_id
                   for entry in realm.entries)
        bundle = bundle_from_document(realm.documents[revision_id])
        bundle["base_parent"]["publication_id"] = publication_id
        return bundle

    monkeypatch.setattr(store, "fetch_bundle", fetch)
    client = SimpleNamespace(_remote=SimpleNamespace(_transport=transport), timelines=SimpleNamespace(commit=send))
    outbox = Outbox(tmp_path / "outbox.json")
    outbox.write({"scope": {**identity, "project_id": "project-1", "timeline_id": "film"},
                  "base": copy.deepcopy(realm.head), "pending": [], "flight": None})
    session = TimelineSession(client, outbox, identity, "project-1", "film")
    bundle = fetch("project-1", "film", **realm.head)
    return Timeline(session, bundle, realm.head), realm, calls


def _geometry(bundle):
    placement, = bundle["placements"]
    shot = bundle["shots"][placement["shot_id"]]
    offset = placement["placement"]["start_ms"] / 1000
    return {
        clip["id"]: (origin + clip["at"], origin + clip["at"] + (
            clip["hold"] if "hold" in clip else (clip["to"] - clip["from"]) / clip["speed"]), clip)
        for origin, rows in ((0, bundle["parent"]["clips"]), (offset, shot["internal_timeline"]["clips"]))
        for clip in rows
    }


@pytest.mark.parametrize("anchored", [False, True], ids=["unanchored", "retained-word"])
def test_public_ripple_preview_then_accepts_exact_fixed_list(tmp_path, monkeypatch, anchored):
    document, capture = _film(anchored)
    timeline, realm, calls = _open_film(tmp_path, monkeypatch, document, capture)
    head = copy.deepcopy(realm.head)
    old = _geometry(bundle_from_document(document))
    assert len(old) == 621

    preview = timeline.commit(lambda t: t.ripple_delete(2.5, 3.0), label="Ripple 15 frames", dry_run=True)
    assert preview["data"]["status"] == "preview"
    assert preview["data"]["would_status"] == "accepted"
    assert preview["receipt"] is None
    assert realm.head == head and len(realm.entries) == 1 and not realm.accepted
    request, = calls
    assert request["dry_run"] is True and request["merge"] == "atomic"
    step, = request["steps"]
    assert step["command_ordinals"] == list(range(len(request["commands"])))
    commands = copy.deepcopy(preview["data"]["commands"])

    accepted = timeline.commit(commands, label="Ripple 15 frames", merge="atomic", key="ripple-fixed")
    assert accepted["data"]["status"] == "accepted"
    assert accepted["receipt"]["receipt_id"]
    assert len(calls) == 2 and calls[1]["commands"] == commands
    landed_document = realm.documents[realm.head["revision_id"]]
    assert landed_document == accepted["data"]["canonical_document"] == preview["data"]["canonical_document"]
    source = bundle_from_document(document)
    landed = bundle_from_document(landed_document)
    assert landed_document["dependency_manifest"]["documents"] == document["dependency_manifest"]["documents"]
    assert landed["placements"][0]["duration_ms"] == source["placements"][0]["duration_ms"] - 500
    new = _geometry(landed)
    assert new.keys() == old.keys()
    for clip_id, (start, end, original) in old.items():
        left, right, updated = new[clip_id]
        assert start >= 3 or end <= 2.5 + 1e-6
        delta = -0.5 if start >= 3 else 0
        assert (left, right) == pytest.approx((start + delta, end + delta))
        assert {k: v for k, v in original.items() if k != "at"} == {
            k: v for k, v in updated.items() if k != "at"}
    assert sum(start >= 3 for start, _, _ in old.values()) == 605

    if capture:
        placement, = source["placements"]
        key = (placement["occurrence_id"], placement["shot_id"], "card")
        for bundle in (source, landed):
            bundle["source_mapping"]["pinned_documents"] = [capture]
        before_word = ShotWordRetimeInputs(source).word(key, "on", '"stop"')
        after_word = ShotWordRetimeInputs(landed).word(key, "on", '"stop"')
        assert before_word is not None and after_word is not None
        assert before_word.binding == after_word.binding
        assert [float(b - a) for a, b in zip(before_word.interval, after_word.interval)] == [-0.5, -0.5]
