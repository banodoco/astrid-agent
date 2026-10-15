"""Documented gestures through the real recorder/compiler/commit seam.

The request-bound acceptance port is a double, not installed Runtime evidence.
"""

import copy
import json
from pathlib import Path

import pytest
from test_timeline_commit import call, core, request

from astrid.sdk.timeline import commands
from astrid.sdk.timeline.checkout import Checkout
from astrid.sdk.timeline.rebase import rebase


@pytest.fixture(params=["parent", "occurrence:occ-A"])
def source(request):
    data = json.loads(
        (Path(__file__).parents[1] / "fixtures/timeline_editing/tiny_publishable.json").read_text()
    )
    clip = dict(
        id="literal",
        clipType="media",
        track="sprite",
        at=0.5,
        **{"from": 1.0, "to": 5.0},
        speed=2,
        asset="P",
        params={},
        opaque={"null": None, "clear-shaped": {"op": "clear"}},
    )
    if request.param == "parent":
        data["parent"].update(
            tracks=[dict(id="sprite", kind="visual")],
            registry=copy.deepcopy(data["shots"]["A"]["internal_timeline"]["registry"]),
            clips=[clip],
        )
    else:
        data["shots"]["A"]["internal_timeline"]["clips"].append(clip)
    return Checkout(data).document(), request.param + "/clip:literal"


def read(bundle, sid):
    return commands.recorded_owner(
        Checkout(copy.deepcopy(bundle)),
        commands.EditCommand("clip.set", sid, {}, resolved=(sid,)),
        "clip",
    )


def gesture(op, sid, **args):
    if op == "timeline.add":
        return call(op, None, **{"from": sid}, **args)
    return call(op, sid, **args)


@pytest.mark.parametrize("op", ["timeline.add", "timeline.split"])
def test_commit_records_exact_identity_and_retry_is_verbatim(source, op):
    bundle, sid = source
    body = request(gesture(op, sid, at=1.5))
    body["source"] = {"via": "app"}
    service, port = core(body, bundle)
    before = copy.deepcopy(bundle)
    result = service.commit(body, port)
    assert result["status"] == "accepted", result
    assert bundle == before and len(port.accepts) == 1
    (record,) = result["change"]["commands"]
    (child,) = record["created"]
    assert child == sid.rsplit(":", 1)[0] + ":literal-01"
    assert record["resolved"] == ([child] if op == "timeline.add" else [sid])
    assert record["bindings"] == {} and record["authorship"] and record["expect"]
    assert (
        read(result["canonical_document"], child).data["opaque"] == read(bundle, sid).data["opaque"]
    )
    assert (
        read(result["canonical_document"], sid).data["opaque"] == read(bundle, sid).data["opaque"]
    )
    if op == "timeline.add":
        assert read(result["canonical_document"], sid).data == read(bundle, sid).data
        assert record["args"]["from"] == sid
        assert record["expect"][sid + "::"]["value"] == read(bundle, sid).data
    else:
        assert read(result["canonical_document"], sid).data["to"] == 3
        assert read(result["canonical_document"], child).data["from"] == 3
        assert read(result["canonical_document"], child).data["to"] == 5
    port.current = "head-moved"
    assert service.commit(body, port) == result and len(port.accepts) == 1
    port.body = changed = copy.deepcopy(body)
    changed["change"]["commands"][0]["args"]["at"] = 1.6
    assert service.commit(changed, port)["error"]["code"] == "request_id_conflict"
    assert len(port.accepts) == 1


@pytest.mark.parametrize("op", ["timeline.add", "timeline.split"])
def test_recorded_replay_and_disjoint_rebase_preserve_exact_allocations(source, op):
    bundle, sid = source
    tl = Checkout(copy.deepcopy(bundle))
    plan = commands.run(tl, gesture(op, sid, at=1.5))
    (record,) = plan.calls
    replay = rebase([record], bundle)
    assert not replay.conflicts, replay.conflicts
    assert replay.document == tl.document()
    assert replay.plan.calls[0].to_json() == record.to_json()
    assert replay.id_map == {}
    head = copy.deepcopy(bundle)
    head["parent"]["config"]["opaque-other-writer"] = None
    rebased = rebase([record], head)
    assert not rebased.conflicts, rebased.conflicts
    assert rebased.plan.calls[0].created == record.created
    assert rebased.document["parent"]["config"]["opaque-other-writer"] is None
    collision = rebase([record], tl.document())
    assert collision.conflicts and collision.conflicts[0]["kind"] == "dependent"
    assert collision.document is None


@pytest.mark.parametrize("op", ["timeline.add", "timeline.split"])
def test_stale_tagged_expectations_refuse_whole_commit_without_mutation(source, op):
    bundle, sid = source
    cmd = gesture(op, sid, at=1.5)
    cmd["expect"] = {sid + "::opaque.null": {"state": "absent"}}
    body = request(call(select="a-rocket", x=123), cmd)
    service, port = core(body, bundle)
    before = copy.deepcopy(bundle)
    result = service.commit(body, port)
    assert result["status"] == "refused" and "expected" in result["error"]["message"]
    assert bundle == before and not port.accepts and port.saved is None
    cmd["expect"] = {sid + "::opaque.null": {"state": "present", "value": None}}
    body = request(cmd)
    service, port = core(body, bundle)
    assert service.commit(body, port)["status"] == "accepted"


@pytest.mark.parametrize("op", ["timeline.add", "timeline.split"])
@pytest.mark.parametrize(
    "field,value", [("on", '"viral"'), ("sequence", "seq"), ("until", "end"), ("cut", "c1")]
)
def test_tied_content_requires_explicit_choice_and_rolls_back(source, op, field, value):
    bundle, sid = source
    # Work on the fixture itself, not the detached helper view.
    rows = (
        bundle["parent"]["clips"]
        if sid.startswith("parent/")
        else bundle["shots"]["A"]["internal_timeline"]["clips"]
    )
    target = next(c for c in rows if c["id"] == "literal")
    target.setdefault("app", {}).setdefault("astrid", {})[field] = value
    before = copy.deepcopy(bundle)
    body = request(gesture(op, sid, at=1.5))
    service, port = core(body, bundle)
    result = service.commit(body, port)
    assert result["status"] == "refused", result
    assert bundle == before and not port.accepts


@pytest.mark.parametrize("op", ["timeline.add", "timeline.split"])
def test_missing_or_wrong_owner_refuses_without_accept(source, op):
    bundle, sid = source
    for target in [sid + "-missing", "occurrence:missing/clip:literal", "shot:A/clip:literal"]:
        body = request(gesture(op, target, at=1.5))
        service, port = core(body, bundle)
        assert service.commit(body, port)["status"] == "refused"
        assert not port.accepts


@pytest.mark.parametrize("op", ["timeline.add", "timeline.split"])
def test_occurrence_request_never_accepts_a_different_owner(source, op):
    bundle, sid = source
    body = request(gesture(op, sid, at=1.5))
    body["scope"]["target"] = "occurrence:occ-B"
    service, port = core(body, bundle)
    assert service.commit(body, port)["status"] == "refused"
    assert not port.accepts


def test_media_acquisition_and_undocumented_payloads_stay_closed(source):
    bundle, sid = source
    for cmd in [
        call("timeline.add", None, element="media", at=1, asset="/tmp/unadmitted"),
        call("timeline.add", None, **{"media.asset": "P"}, at=1),
        gesture("timeline.add", sid, at=1.5, asset="/tmp/unadmitted"),
        call("timeline.reorder", "parent/track:sprite", order=0),
    ]:
        body = request(cmd)
        service, port = core(body, bundle)
        assert service.commit(body, port)["status"] == "refused"
        assert not port.accepts


@pytest.mark.parametrize("op", ["timeline.add", "timeline.split"])
def test_rebase_refuses_source_changes_including_opaque_presence(source, op):
    bundle, sid = source
    tl = Checkout(copy.deepcopy(bundle))
    record = commands.run(tl, gesture(op, sid, at=1.5)).calls[0]
    head = copy.deepcopy(bundle)
    rows = (
        head["parent"]["clips"]
        if sid.startswith("parent/")
        else head["shots"]["A"]["internal_timeline"]["clips"]
    )
    next(c for c in rows if c["id"] == "literal")["opaque"]["null"] = "other-writer"
    before = copy.deepcopy(head)
    result = rebase([record], head)
    assert result.conflicts and result.conflicts[0]["kind"] == "same-property"
    assert head == before and result.document is None


@pytest.mark.parametrize("op", ["timeline.add", "timeline.split"])
def test_local_one_step_undo_redo_and_failed_batch_preserve_history(
    source, op, tmp_path, monkeypatch
):
    from astrid.sdk.timeline import journal, store
    from astrid.sdk.timeline._common import TimelineEditError
    from astrid.sdk.timeline.identity import retain_cut_reservations

    monkeypatch.setenv("BANODOCO_LOCAL_DATA_ROOT", str(tmp_path / "data"))
    bundle, sid = source
    path = tmp_path / "draft.json"
    tl = Checkout(copy.deepcopy(bundle), path=path)
    tl.save(quiet=True)
    before = tl.document()
    plan = commands.run(tl, gesture(op, sid, at=1.5))
    after = tl.document()
    assert len(list(store._history_dir(path).glob("*.json"))) == 1
    records = journal.read(path.with_suffix(".json.journal.jsonl"))
    assert records[-1].outcome == "changed" and len(records[-1].calls) == 1
    assert tl.undo()
    retain_cut_reservations(before)
    retain_cut_reservations(after)
    assert tl.document() == before
    assert tl.redo() and tl.document() == after
    disk = path.read_bytes()
    previous = tl.document(), len(tl._pending_calls)
    with pytest.raises(TimelineEditError):
        commands.run(
            tl,
            call(
                "timeline.batch",
                None,
                commands=[gesture(op, sid, at=1.25), call("not.registered", None)],
            ),
        )
    assert path.read_bytes() == disk and (tl.document(), len(tl._pending_calls)) == previous
    assert journal.read(path.with_suffix(".json.journal.jsonl"))[-1].outcome == "rolled-back"
    assert journal.verify(path.with_suffix(".json.journal.jsonl")) == []
    assert plan.calls[0].created


@pytest.mark.parametrize("op", ["timeline.add", "timeline.split"])
def test_created_owner_mismatch_refuses_atomic_allocation(source, op):
    bundle, sid = source
    cmd = gesture(op, sid, at=1.5)
    cmd["created"] = ["occurrence:occ-B/clip:literal-01"]
    before = copy.deepcopy(bundle)
    body = request(cmd)
    service, port = core(body, bundle)
    result = service.commit(body, port)
    assert result["status"] == "refused"
    assert bundle == before and not port.accepts


def test_duplicate_supplied_id_map_rewrites_source_and_created_as_distinct_identities(source):
    bundle, sid = source
    tl = Checkout(copy.deepcopy(bundle))
    record = commands.run(tl, gesture("timeline.add", sid, at=1.5)).calls[0]
    head = copy.deepcopy(bundle)
    rows = (
        head["parent"]["clips"]
        if sid.startswith("parent/")
        else head["shots"]["A"]["internal_timeline"]["clips"]
    )
    next(c for c in rows if c["id"] == "literal")["id"] = "renamed"
    mapped = sid.rsplit(":", 1)[0] + ":renamed"
    created = record.created[0]
    mapped_child = created.rsplit(":", 1)[0] + ":forked"
    result = rebase([record], head, id_map={sid: mapped, created: mapped_child})
    assert not result.conflicts, result.conflicts
    assert result.id_map == {sid: mapped, created: mapped_child}
    assert result.plan.calls[0].args["from"] == mapped
    assert result.plan.calls[0].created == (mapped_child,)
    assert read(result.document, mapped_child).data["id"] == "forked"


def test_split_time_ref_uses_existing_binding_resolver(source):
    bundle, sid = source
    body = request(gesture("timeline.split", sid, at='"viral"'))
    service, port = core(body, bundle)
    result = service.commit(body, port)
    assert result["status"] == "accepted", result["error"]
    (record,) = result["change"]["commands"]
    assert record["bindings"] == {'"viral"': "word:s1:2"}
    assert record["args"]["at"] == '"viral"'


@pytest.mark.parametrize("op", ["timeline.add", "timeline.split"])
def test_child_clear_replays_without_resurrecting_values(source, op):
    bundle, sid = source
    rows = (
        bundle["parent"]["clips"]
        if sid.startswith("parent/")
        else bundle["shots"]["A"]["internal_timeline"]["clips"]
    )
    next(c for c in rows if c["id"] == "literal")["opacity"] = 0.5
    tl = Checkout(copy.deepcopy(bundle))
    first = commands.run(tl, gesture(op, sid, at=1.5)).calls[0]
    child = first.created[0]
    second = commands.run(tl, call("clip.clear", child, keys=["opacity"])).calls[0]
    replay = rebase([first, second], bundle)
    assert not replay.conflicts, replay.conflicts
    assert "opacity" not in read(replay.document, child).data
    assert read(replay.document, sid).data["opacity"] == 0.5
    assert read(replay.document, child).data["opaque"]["null"] is None
    assert read(replay.document, child).data["opaque"]["clear-shaped"] == {"op": "clear"}


@pytest.mark.parametrize("op", ["timeline.add", "timeline.split"])
def test_unadmitted_parent_media_and_animated_sources_refuse(source, op):
    bundle, sid = source
    for extra in [{"keyframes": []}, {"modifiers": []}, {"hold": 4}]:
        changed = copy.deepcopy(bundle)
        changed_rows = (
            changed["parent"]["clips"]
            if sid.startswith("parent/")
            else changed["shots"]["A"]["internal_timeline"]["clips"]
        )
        next(c for c in changed_rows if c["id"] == "literal").update(extra)
        body = request(gesture(op, sid, at=1.5))
        service, port = core(body, changed)
        assert service.commit(body, port)["status"] == "refused"
        assert not port.accepts
    bundle["parent"].setdefault("registry", {}).setdefault("assets", {})["LOCAL"] = {
        "local_path": "/unadmitted"
    }
    body = request(gesture(op, sid, at=1.5))
    service, port = core(body, bundle)
    assert service.commit(body, port)["status"] == "refused" and not port.accepts
