"""Actual SDK receipts and publication inverses; synthetic acceptance port only."""
import copy
from pathlib import Path

import pytest
import test_timeline_publication_inverse as publication_fixtures
from test_timeline_commit import call, core, request
from test_timeline_identity_inverse import seed
from test_timeline_saved_publish import synthetic_scene_answers  # noqa: F401

from astrid.sdk import timeline_intent as intent
from astrid.sdk.timeline import commands
from astrid.sdk.timeline.checkout import Checkout
from astrid.sdk.timeline.commit import RuntimeCommitService
from astrid.sdk.timeline.identity import retain_cut_reservations
from astrid.sdk.timeline.inverse import inverse_command
from astrid.sdk.timeline.rebase import rebase

pytestmark = pytest.mark.usefixtures("synthetic_scene_answers")
publication_setup = publication_fixtures.setup
publish = publication_fixtures.publish


@pytest.fixture
def bundle():
    import json
    path = Path(__file__).parents[1] / "fixtures/timeline_editing/tiny_publishable.json"
    return Checkout(json.loads(path.read_text())).document()


def fixture(bundle, scope="parent"):
    tl = Checkout(copy.deepcopy(bundle))
    shot = None if scope == "parent" else "A"
    internal = tl._internal(shot)
    tracks = tl._tracks(shot, create=True)
    tracks[:] = [dict(id=i, kind=k) for i, k in
                 [("V1", "visual"), ("A1", "audio"), ("V2", "visual"), ("A4", "audio")]]
    internal["clips"] = [dict(id="movie", clipType="media", asset="movie", track="V1",
        at=0, **{"from": 1, "to": 6}, speed=1.25, volume=0.6,
        app=dict(x=dict(opaque=[None, False, {"retain": "exact"}])), opacity=0.75)]
    tl._registry(shot)["movie"] = dict(media_id="sha256:" + "a" * 64, type="video/mp4")
    return retain_cut_reservations(tl.document())


def acceptance_port(document, calls, base=None):
    body = request(*calls)
    if base:
        body.update(base_revision_id=base["accepted_revision_id"],
                    base_publication_id=base["accepted_publication_id"])
    service, port = core(body, document)
    if base:
        port.current = body["base_revision_id"]
        port.bundles[port.current] = document
        port.head = lambda: dict(revision_id=port.current,
            publication_id=body["base_publication_id"], updated_at="fixture")
    return service, port, body


def accepted(document, calls, base=None):
    after, plan, _ = RuntimeCommitService._apply(document, calls)
    service, port, body = acceptance_port(document, calls, base)
    result = service.commit(body, port)
    assert result["status"] == "accepted", result
    assert result["change"]["direct_writes"] == list(plan.direct_writes)
    assert result["change"]["commands"] == [c.to_json() for c in plan.calls]
    assert result["canonical_document"] == after
    assert port.accepts[0]["evidence"]["direct_writes"] == list(plan.direct_writes)
    return after, plan, result


def inverse_of(before, after, plan, publication):
    return inverse_command(plan.calls, before, after, publication, consequences=plan.consequences)


def roundtrip(before, after, plan, original_result):
    assert RuntimeCommitService._apply(before, plan.calls)[0] == after
    undo = inverse_of(before, after, plan, original_result["accepted_publication_id"])
    restored, undo_plan, result = accepted(after, [undo], original_result)
    assert restored == before
    assert RuntimeCommitService._apply(after, undo_plan.calls)[0] == before
    redone, _, _ = accepted(restored, [inverse_of(after, restored, undo_plan, result["accepted_publication_id"])], result)
    assert redone == after
    return undo


@pytest.mark.parametrize("scope", ["parent", "occurrence:occ-A"])
def test_reorder_plan_and_commit_have_exact_synthetic_direct_permutation(bundle, scope):
    before = fixture(bundle, scope)
    old = [scope + "/track:" + i for i in ["V1", "A1", "V2", "A4"]]
    new = [old[i] for i in [1, 2, 0, 3]]
    sid = old[0]
    value = {**call("timeline.reorder", sid, order=new, expected_order=old),
             "expect": {"track_order": dict(state="present", value=old)}}
    after, plan, result = accepted(before, [value])
    assert list(plan.direct_writes) == [dict(id=sid, path="track_order",
        before=dict(state="present", value=old), after=dict(state="present", value=new))]
    if scope == "parent":
        assert any(w["id"] == "parent" and w["path"] == "config.tracks" for w in plan.consequences)
    roundtrip(before, after, plan, result)


@pytest.mark.parametrize("bad", ["missing", "duplicate", "stale", "cross-owner"])
def test_reorder_bad_permutation_refuses_without_acceptance(bundle, bad):
    before = fixture(bundle)
    old = ["parent/track:" + i for i in ["V1", "A1", "V2", "A4"]]
    order, expected = old[::-1], old[:]
    if bad == "missing":
        order.pop()
    elif bad == "duplicate":
        order[-1] = order[0]
    elif bad == "stale":
        expected.reverse()
    else:
        order[0] = "occurrence:occ-A/track:A4"
    body = request({**call("timeline.reorder", old[0], order=order, expected_order=expected),
                    "expect": {"track_order": dict(state="present", value=old)}})
    service, port = core(body, before)
    original = copy.deepcopy(before)
    result = service.commit(body, port)
    assert result["status"] in {"refused", "conflict"} and not port.accepts
    assert before == original


@pytest.mark.parametrize("scope", ["parent", "occurrence:occ-A"])
@pytest.mark.parametrize("new_track", [False, True])
def test_detach_allocated_root_plan_commit_inverse_and_replay(bundle, scope, new_track):
    before = fixture(bundle, scope)
    track = scope + "/track:detached"
    child = scope + "/clip:clip-1-1792242000000"
    values = []
    if new_track:
        values.append({**call("timeline.add", track, track=dict(id="detached", kind="audio", label="Detached", app={"opaque": None})),
                       "expect": {track + "::": dict(state="absent")}})
    values.append({**call("clip.detach-audio", scope + "/clip:movie", track="detached" if new_track else "A1"),
                   "created": [child]})
    after, plan, result = accepted(before, values)
    writes = {(w["id"], w["path"]): w for w in plan.direct_writes}
    root = writes[(child, "")]
    payload = commands.state_at(commands.entity_states(Checkout(after)), child, "")
    assert root == dict(id=child, path="", before=dict(state="absent"), after=payload)
    assert plan.calls[-1].created == (child,)
    source = commands.state_at(commands.entity_states(Checkout(before)), scope + "/clip:movie", "")["value"]
    expected = {**source, "id": child.split("/clip:")[1], "track": "detached" if new_track else "A1"}
    assert payload["value"] == expected
    assert (scope + "/clip:movie", "volume") in writes
    if new_track:
        assert writes[(track, "")]["before"] == dict(state="absent")
    undo = roundtrip(before, after, plan, result)
    changed, _, _ = RuntimeCommitService._apply(after, [call("clip.set-look", child, volume=0.2)])
    service, port, body = acceptance_port(changed, [undo], result)
    snapshot = copy.deepcopy(changed)
    refused = service.commit(body, port)
    assert refused["status"] in {"refused", "conflict"} and not port.accepts, refused
    assert changed == snapshot
    tl = Checkout(copy.deepcopy(changed))
    with pytest.raises(commands.TimelineEditError):
        commands.run(tl, undo)
    assert tl.document() == snapshot
    assert rebase(plan.calls, after).conflicts  # allocated child already exists


@pytest.mark.parametrize("later", ["track-edit", "track-user"])
def test_created_track_inverse_refuses_later_work_atomically(bundle, later):
    before = fixture(bundle)
    track, child = "parent/track:detached", "parent/clip:allocated"
    values = [{**call("timeline.add", track, track=dict(id="detached", kind="audio", label="Detached")),
               "expect": {track + "::": dict(state="absent")}},
              {**call("clip.detach-audio", "parent/clip:movie", track="detached"), "created": [child]}]
    after, plan, result = accepted(before, values)
    undo = inverse_of(before, after, plan, result["accepted_publication_id"])
    change = ({**call("timeline.set", track, values={"label": "later"}),
               "expect": {"label": dict(state="present", value="Detached")}} if later == "track-edit" else
              call("timeline.add", None, **{"from": child, "at": 7, "track": "detached", "clip_id": "later"}))
    changed, _, _ = RuntimeCommitService._apply(after, [change])
    snapshot = copy.deepcopy(changed)
    service, port, body = acceptance_port(changed, [undo], result)
    refused = service.commit(body, port)
    assert refused["status"] in {"refused", "conflict"} and not port.accepts, refused
    assert changed == snapshot
    tl = Checkout(copy.deepcopy(changed))
    with pytest.raises(commands.TimelineEditError):
        commands.run(tl, undo)
    assert tl.document() == snapshot
    assert child in commands.entity_states(Checkout(changed))


def seed_history(runtime, document):
    seed(runtime, document)
    # The read API's exact shot resource carries this identity in addition to
    # the publication input. RuntimeFixture.__init__ models the same envelope.
    for shot in runtime.shots.values():
        shot["internal_timeline_id"] = runtime.internals[shot["internal_timeline_revision_id"]]["timeline_id"]


@pytest.mark.parametrize("new_track", [False, True])
def test_saved_publication_history_detach_undo_redo_and_later_child_refusal(publication_setup, new_track):
    draft, runtime, client = publication_setup
    original = draft.document()
    document = fixture(original)
    # Untouched child snapshots still reference film-owned tracks (e.g. plate).
    # Keep their definitions on the parent while adding this case's media lanes.
    tracks = document['parent']['config']['tracks']
    definitions = {track['id']: track for track in tracks}
    for track in original['parent']['config']['tracks']:
        if track['id'] in definitions:
            assert definitions[track['id']] == track
        else:
            tracks.append(copy.deepcopy(track))
            definitions[track['id']] = tracks[-1]
    seed_history(runtime, document)
    draft = Checkout.draft("p", "t", name="writer-receipt", fresh=True)
    before = commands.entity_states(draft)
    track, child = "parent/track:detached", "parent/clip:allocated"
    values = []
    if new_track:
        values.append({**call("timeline.add", track, track=dict(id="detached", kind="audio", label="Detached")),
                       "expect": {track + "::": dict(state="absent")}})
    values.append({**call("clip.detach-audio", "parent/clip:movie", track="detached" if new_track else "A1"),
                   "created": [child]})
    commands.run(draft, call("timeline.batch", None, commands=values))
    first = publish(draft, client, "detach actual allocated child")
    after = commands.entity_states(draft)
    assert any(w["id"] == child and w["path"] == "" for w in first["change"]["direct_writes"])
    draft.undo(client=client, publication=first["accepted_publication_id"])
    undone = publish(draft, client, "Undo detach")
    assert commands.entity_states(draft) == before
    draft.undo(client=client, publication=undone["accepted_publication_id"])
    redone = publish(draft, client, "Redo detach")
    assert commands.entity_states(draft) == after
    other = Checkout.draft("p", "t", name="later-child")
    commands.run(other, call("clip.set-look", child, volume=0.2))
    later = publish(other, client, "edit detached child later")
    clean = Path(draft.path).read_bytes()
    with pytest.raises(commands.TimelineEditError, match="same-property") as exc:
        draft.undo(client=client, publication=redone["accepted_publication_id"])
    assert later["accepted_publication_id"] in str(exc.value)
    assert Path(draft.path).read_bytes() == clean
    assert runtime.current == later["accepted_revision_id"]


@pytest.mark.parametrize("later", ["stale-source", "conflicting-child"])
def test_detach_refuses_captured_source_or_allocated_child_conflict(bundle, later):
    before = fixture(bundle)
    sid, child = "parent/clip:movie", "parent/clip:allocated"
    source = commands.state_at(commands.entity_states(Checkout(before)), sid, "")
    value = {**call("clip.detach-audio", sid, track="A1"), "created": [child], "expect": {sid + "::": source}}
    changed = copy.deepcopy(before)
    if later == "stale-source":
        changed["parent"]["clips"][0]["speed"] = 1.5
    else:
        changed["parent"]["clips"].append({**copy.deepcopy(source["value"]), "id": "allocated", "track": "A1"})
    original = copy.deepcopy(changed)
    service, port, body = acceptance_port(changed, [value])
    result = service.commit(body, port)
    assert result["status"] == "refused" and not port.accepts, result
    assert changed == original
    tl = Checkout(copy.deepcopy(changed))
    with pytest.raises(commands.TimelineEditError):
        commands.run(tl, value)
    assert tl.document() == original


def test_detach_root_keeps_anchor_opaque_payload_and_drops_cut_sequence_ownership(bundle):
    tl = Checkout(copy.deepcopy(bundle))
    source = tl.clip("a-rocket")
    source.data.update(clipType="media", asset="video", speed=1.25, volume=0.6, **{"from": 1.0, "to": 4.0})
    source.data.pop("hold", None)
    source.data["app"]["opaque"] = {"null": None, "retained": [False, 7]}
    intent.set_sequence(source.data, "source-sequence", 0)
    tl._internal("A")["tracks"].append(dict(id="audio", kind="audio"))
    tl._registry("A")["video"] = {**copy.deepcopy(tl._registry("A")["P"]), "type": "video"}
    before = retain_cut_reservations(tl.document())
    old = copy.deepcopy(Checkout(before).clip("a-rocket").data)
    after, plan, result = accepted(before, [{**call("clip.detach-audio", "a-rocket"),
        "created": ["occurrence:occ-A/clip:allocated"]}])
    child = Checkout(after).clip("allocated").data
    root = next(w for w in plan.direct_writes if w["id"] == "occurrence:occ-A/clip:allocated")
    assert root == dict(id="occurrence:occ-A/clip:allocated", path="", before=dict(state="absent"),
                        after=dict(state="present", value=child))
    assert [child[k] for k in ("from", "to", "speed", "volume")] == [1.0, 4.0, 1.25, 0.6]
    assert child["app"]["opaque"] == old["app"]["opaque"]
    assert intent.on(child) == intent.on(old)
    assert intent.cut_of(child) is intent.layer_of(child) is intent.sequence(child) is None
    assert Checkout(after).clip("a-rocket").data == {**old, "volume": 0}
    roundtrip(before, after, plan, result)


@pytest.mark.parametrize("local_lane", [False, True])
def test_created_parent_track_inverse_distinguishes_inherited_and_child_owned_users(bundle, local_lane):
    before = fixture(bundle)
    row = dict(id="detached", kind="audio", label="Detached")
    if local_lane:
        before["shots"]["A"]["internal_timeline"]["tracks"].append(copy.deepcopy(row))
    track = "parent/track:detached"
    after, plan, result = accepted(before, [
        {**call("timeline.add", track, track=row), "expect": {track + "::": dict(state="absent")}},
        {**call("clip.detach-audio", "parent/clip:movie", track="detached"), "created": ["parent/clip:allocated"]}])
    undo = inverse_of(before, after, plan, result["accepted_publication_id"])
    head = copy.deepcopy(after)
    voice = next(c for c in head["shots"]["A"]["internal_timeline"]["clips"] if c["id"] == "a-vo")
    voice["track"] = "detached"
    tl = Checkout(copy.deepcopy(head))
    if local_lane:
        restored, _, _ = RuntimeCommitService._apply(head, [undo])
        assert track not in commands.entity_states(Checkout(restored))
        assert next(c for c in restored["shots"]["A"]["internal_timeline"]["clips"] if c["id"] == "a-vo") == voice
        assert row in restored["shots"]["A"]["internal_timeline"]["tracks"]
    else:
        with pytest.raises(commands.TimelineEditError, match="occupied"):
            commands.run(tl, undo)
        assert tl.document() == head
