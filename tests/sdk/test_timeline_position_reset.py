"""W32's eight App clip fields use one guarded clear and the existing inverse."""
# ruff: noqa: F811 -- imported pytest fixtures share parameter names.
import copy
import json

import pytest
from test_timeline_saved_publish import synthetic_scene_answers  # noqa: F401
from test_timeline_writer_direct_receipts import (  # noqa: F401
    acceptance_port,
    accepted,
    bundle,
    fixture,
)

from astrid.core.timeline import clip_fields as cf
from astrid.sdk.timeline import commands
from astrid.sdk.timeline.checkout import Checkout
from astrid.sdk.timeline.inverse import inverse_command
from astrid.sdk.timeline.rebase import rebase

pytestmark = pytest.mark.usefixtures("synthetic_scene_answers")

# These are the eight top-level paths cleared by useClipPositioning.ts at
# App 14caa76c119387517c683926a6f74f657b2ac469. Element params are separate.
FIELDS = dict(position_x="x", position_y="y", position_width="width", position_height="height",
              crop_top="cropTop", crop_bottom="cropBottom", crop_left="cropLeft", crop_right="cropRight")


def serialized(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False)


def owner(document, sid):
    return commands.authored_owner(Checkout(document), sid, selected=True)


def source(bundle, scope, kind, presence):
    document = fixture(bundle, scope)
    sid = scope + "/clip:movie"
    clip = owner(document, sid)
    if kind == "element":
        clip.data.update(clipType="am-sprite", params=dict(x=5, y=6, width=7, height=8))
    values = [12, -0.0, 720.0, 480, .1, .2, .3, .4]
    for index, (path, value) in enumerate(zip(FIELDS.values(), values)):
        if presence == "mixed" and index % 3 == 0:
            continue
        clip.data[path] = None if presence == "null" or (presence == "mixed" and index % 3 == 1) else value
    clip.data["opaque"] = dict(negative_zero=-0.0, integer=1, decimal=1.0, nested=[None, False, "雪"])
    clip.data["app"]["geometry"] = dict(x=30, width=None)
    return document, sid


def reset(document, sid):
    states = commands.entity_states(Checkout(document))
    return dict(op="clip.clear", select=sid, args=dict(keys=list(FIELDS)),
                expect={sid + "::" + path: commands.state_at(states, sid, path) for path in FIELDS.values()})


def receipt_inverse(result):
    """Use only the accepted receipt, as the App history consumer must."""
    return dict(op="timeline.inverse", select=None, args=dict(
        publication=result["accepted_publication_id"], writes=[
            dict(id=w["id"], path=w["path"], before=copy.deepcopy(w["after"]), after=copy.deepcopy(w["before"]))
            for w in result["change"]["direct_writes"]]))


@pytest.mark.parametrize("scope", ["parent", "occurrence:occ-A"])
@pytest.mark.parametrize("kind", ["media", "element"])
@pytest.mark.parametrize("presence", ["value", "null", "mixed"])
def test_position_reset_exact_receipts_undo_and_receipt_only_redo(bundle, scope, kind, presence):
    before, sid = source(bundle, scope, kind, presence)
    captured = serialized(before)
    after, plan, result = accepted(before, [reset(before, sid)])
    assert serialized(before) == captured
    assert len(plan.calls) == 1 and plan.calls[0].op == "clip.clear"
    expected = copy.deepcopy(before)
    for path in FIELDS.values():
        owner(expected, sid).data.pop(path, None)
    assert serialized(after) == serialized(expected)
    writes = result["change"]["direct_writes"]
    assert {(w["id"], w["path"]) for w in writes} == {(sid, path) for path in FIELDS.values()}
    assert len(writes) == 8
    states = commands.entity_states(Checkout(before))
    for write in writes:
        assert write["before"] == commands.state_at(states, sid, write["path"])
        assert write["after"] == dict(state="absent")
    for path in FIELDS.values():
        key = commands.write_key(sid, path, plan.calls[0].resolved)
        assert plan.calls[0].expect[key] == commands.state_at(states, sid, path)
        assert plan.calls[0].authorship[key] == "direct"
    undo = inverse_command(plan.calls, before, after, result["accepted_publication_id"],
                           consequences=plan.consequences)
    assert {w["path"] for w in undo["args"]["writes"]} == set(FIELDS.values())
    restored, _, undone = accepted(after, [undo], result)
    assert serialized(restored) == captured
    assert len(undone["change"]["direct_writes"]) == 8
    redone, _, redone_result = accepted(restored, [receipt_inverse(undone)], undone)
    assert serialized(redone) == serialized(after)
    assert len(redone_result["change"]["direct_writes"]) == 8
    restored_from_receipt, _, _ = accepted(after, [receipt_inverse(result)], result)
    assert serialized(restored_from_receipt) == captured


def refuse(document, call):
    snapshot = serialized(document)
    tl = Checkout(copy.deepcopy(document))
    with pytest.raises(commands.TimelineEditError):
        commands.run(tl, call)
    assert serialized(tl.document()) == snapshot
    service, port, body = acceptance_port(document, [call])
    result = service.commit(body, port)
    assert result["status"] in {"refused", "conflict"}, result
    assert not port.accepts and result["change"]["direct_writes"] == []
    assert serialized(document) == snapshot


@pytest.mark.parametrize("scope", ["parent", "occurrence:occ-A"])
@pytest.mark.parametrize("path", FIELDS.values())
def test_position_reset_each_stale_field_refuses_whole_clear_undo_and_redo(bundle, scope, path):
    before, sid = source(bundle, scope, "media", "mixed")
    value = reset(before, sid)
    stale = copy.deepcopy(before)
    owner(stale, sid).data[path] = .51
    refuse(stale, value)
    after, _, result = accepted(before, [value])
    undo = receipt_inverse(result)
    stale = copy.deepcopy(after)
    owner(stale, sid).data[path] = None
    refuse(stale, undo)
    restored, _, undone = accepted(after, [undo], result)
    redo = receipt_inverse(undone)
    stale = copy.deepcopy(restored)
    owner(stale, sid).data[path] = .51
    refuse(stale, redo)
    replay = rebase([redo], stale)
    assert replay.conflicts and replay.document is None


@pytest.mark.parametrize("damage", ["unknown-field", "wrong-owner", "audio"])
def test_position_reset_bad_last_field_or_owner_rolls_back(bundle, damage):
    before, sid = source(bundle, "parent", "media", "value")
    value = reset(before, sid)
    if damage == "unknown-field":
        value["args"]["keys"].append("not_a_field")
    elif damage == "wrong-owner":
        value["select"] = "occurrence:occ-A/clip:movie"
    else:
        clip = owner(before, sid)
        clip.data.update(clipType="audio", track="A1")
        assert clip.is_audio  # audio applicability follows the owning track
    refuse(before, value)


@pytest.mark.parametrize("name,path", list(FIELDS.items())[:4])
def test_position_reset_fields_share_numeric_codec_without_element_param_aliases(name, path):
    assert cf.coerce(name, "-12.5") == (path, -12.5)
    assert cf.FIELDS[name].stored_at == path
    for invalid in [True, None, float("inf"), float("nan"), "bad"]:
        with pytest.raises(cf.ClipFieldError):
            cf.coerce(name, invalid)
    with pytest.raises(cf.ClipFieldError, match="visual field"):
        cf.coerce(name, 1, audio=True)


def test_position_reset_already_absent_keeps_eight_guards_without_a_publication(bundle):
    before = fixture(bundle)
    sid = "parent/clip:movie"
    snapshot = serialized(before)
    service, port, body = acceptance_port(before, [reset(before, sid)])
    result = service.commit(body, port)
    assert result["status"] == "no_change", result
    assert serialized(result["canonical_document"]) == snapshot == serialized(before)
    assert port.accepts[0]["publication"] is None
    writes = result["change"]["direct_writes"]
    assert len(writes) == 8 and {w["path"] for w in writes} == set(FIELDS.values())
    assert all(w["before"] == w["after"] == dict(state="absent") for w in writes)


def test_position_reset_field_writer_preserves_element_geometry_and_crop_mapping(bundle):
    before, sid = source(bundle, "parent", "element", "value")
    clip = owner(before, sid)
    params = copy.deepcopy(clip.data["params"])
    for index, (name, path) in enumerate(list(FIELDS.items())[:4]):
        cf.write(clip.data, name, index + .25)
        assert clip.data[path] == index + .25
    assert clip.data["params"] == params
    assert {name: cf.FIELDS[name].stored_at for name in FIELDS} == FIELDS
