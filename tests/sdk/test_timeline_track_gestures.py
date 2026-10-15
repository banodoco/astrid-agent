"""The documented empty-track remove form; no guessed cascaded effects."""

import copy

import pytest
import test_timeline_gesture_producers as gesture_fixtures
from test_timeline_commit import call, core, request

from astrid.sdk.timeline import commands
from astrid.sdk.timeline.checkout import Checkout
from astrid.sdk.timeline.rebase import rebase

source = gesture_fixtures.source


def track_fixture(source):
    bundle, clip = source
    scope = clip.split("/clip:", 1)[0]
    internal = bundle["parent"] if scope == "parent" else bundle["shots"]["A"]["internal_timeline"]
    internal["tracks"].append(dict(id="unused", kind="visual", opaque={"nullable": None}))
    return bundle, scope + "/track:unused"


def test_empty_track_remove_commit_replay_and_disjoint_rebase(source):
    bundle, sid = track_fixture(source)
    cmd = call("timeline.remove", sid)
    cmd["expect"] = {
        "": {
            "state": "present",
            "value": dict(id="unused", kind="visual", opaque={"nullable": None}),
        }
    }
    body = request(cmd)
    body["source"] = {"via": "app"}
    service, port = core(body, bundle)
    before = copy.deepcopy(bundle)
    result = service.commit(body, port)
    assert result["status"] == "accepted", result["error"]
    (record,) = result["change"]["commands"]
    assert record["resolved"] == [sid] and record["created"] == [] and record["bindings"] == {}
    assert record["authorship"][""] == "direct"
    assert all(kind == "consequence" for key, kind in record["authorship"].items() if key != "")
    assert sid not in commands.entity_states(Checkout(copy.deepcopy(result["canonical_document"])))
    assert bundle == before
    assert service.commit(body, port) == result and len(port.accepts) == 1
    head = copy.deepcopy(bundle)
    head["parent"]["config"]["opaque-unrelated"] = None
    replay = rebase([record], head)
    assert not replay.conflicts, replay.conflicts
    assert replay.id_map == {} and replay.plan.calls[0].to_json() == record
    assert replay.document["parent"]["config"]["opaque-unrelated"] is None


@pytest.mark.parametrize("negative", ["occupied", "stale", "wrong-owner", "missing", "extra-args"])
def test_remove_refusals_never_mutate_document_or_accept(source, negative):
    bundle, sid = track_fixture(source)
    internal = (
        bundle["parent"] if sid.startswith("parent/") else bundle["shots"]["A"]["internal_timeline"]
    )
    cmd = call("timeline.remove", sid)
    if negative == "occupied":
        internal["clips"][0]["track"] = "unused"
    elif negative == "stale":
        cmd["expect"] = {"opaque.nullable": {"state": "absent"}}
    elif negative == "wrong-owner":
        cmd["select"] = "occurrence:missing/track:unused"
    elif negative == "missing":
        cmd["select"] = sid + "-missing"
    else:
        cmd["args"] = {"cascade": True}
    before = copy.deepcopy(bundle)
    body = request(call(select="a-rocket", x=123), cmd)
    service, port = core(body, bundle)
    result = service.commit(body, port)
    assert result["status"] == "refused", result
    assert not port.accepts and port.saved is None and bundle == before
    if negative == "occupied":
        assert "move or remove" in result["error"]["message"]


def test_track_remove_undo_redo_and_stale_rebase(source, tmp_path, monkeypatch):
    from astrid.sdk.timeline.identity import retain_cut_reservations

    monkeypatch.setenv("BANODOCO_LOCAL_DATA_ROOT", str(tmp_path / "data"))
    bundle, sid = track_fixture(source)
    tl = Checkout(copy.deepcopy(bundle), path=tmp_path / "track.json")
    tl.save(quiet=True)
    before = tl.document()
    record = commands.run(tl, call("timeline.remove", sid)).calls[0]
    after = tl.document()
    assert tl.undo()
    retain_cut_reservations(before)
    retain_cut_reservations(after)
    assert tl.document() == before
    assert tl.redo() and tl.document() == after
    head = copy.deepcopy(bundle)
    internal = (
        head["parent"] if sid.startswith("parent/") else head["shots"]["A"]["internal_timeline"]
    )
    internal["tracks"][-1]["opaque"]["nullable"] = "new-value"
    result = rebase([record], head)
    assert result.conflicts and result.conflicts[0]["kind"] == "same-property"


def test_track_mapping_and_deleted_track_conflict_use_existing_rebase(source):
    bundle, sid = track_fixture(source)
    tl = Checkout(copy.deepcopy(bundle))
    record = commands.run(tl, call("timeline.remove", sid)).calls[0]
    deleted = rebase([record], tl.document())
    assert deleted.conflicts[0]["kind"] == "delete-vs-edit"
    head = copy.deepcopy(bundle)
    internal = (
        head["parent"] if sid.startswith("parent/") else head["shots"]["A"]["internal_timeline"]
    )
    internal["tracks"][-1]["id"] = "renamed"
    mapped = sid.rsplit(":", 1)[0] + ":renamed"
    replay = rebase([record], head, id_map={sid: mapped})
    assert not replay.conflicts, replay.conflicts
    assert replay.id_map == {sid: mapped}
    assert replay.plan.calls[0].resolved == (mapped,)
    assert mapped not in commands.entity_states(Checkout(copy.deepcopy(replay.document)))


def test_zero_output_authored_carrier_keeps_its_track_occupied(source):
    from astrid.sdk.timeline.music_beds import collection

    bundle, sid = track_fixture(source)
    tl = Checkout(copy.deepcopy(bundle))
    owner = sid.split("/track:", 1)[0] + "/clip:managed"
    collection(tl, create=True)[owner] = dict(
        owner=owner,
        source=dict(
            id="managed",
            clipType="media",
            track="unused",
            asset="P",
            at=0.0,
            **{"from": 0.0, "to": 2.0},
        ),
        ripple_basis=[],
        outputs=[],
    )
    body = request(call("timeline.remove", sid))
    service, port = core(body, tl.document())
    result = service.commit(body, port)
    assert result["status"] == "refused"
    assert owner in result["error"]["message"]
    assert not port.accepts
