"""W26: immutable App inspector pair through actual command/commit/inverse paths."""
import copy
import json
from pathlib import Path

import pytest
from test_timeline_commit import core, request
from test_timeline_saved_publish import synthetic_scene_answers  # noqa: F401
from test_timeline_writer_direct_receipts import accepted, fixture

from astrid.sdk.timeline import commands
from astrid.sdk.timeline.checkout import Checkout
from astrid.sdk.timeline.commit import RuntimeCommitService
from astrid.sdk.timeline.inverse import inverse_command
from astrid.sdk.timeline.transaction import edit_transaction

pytestmark = pytest.mark.usefixtures("synthetic_scene_answers")
PAYLOAD = {
    "x": [
        {"time": 7.125, "value": 21.0, "interpolation": "linear", "easing": "ease-in",
         "opaque": {"siblings": [None, False, {"retain": 1.0}]}},
        {"time": -0.25, "value": False, "interpolation": "hold"},
        {"time": 7.125, "value": "authored duplicate", "interpolation": "hold"},
    ],
    "custom.param[0]": [{"time": 1.375, "value": "exact", "interpolation": "linear"}],
}


def wire(document, sid, **args):
    states = commands.entity_states(Checkout(document))
    return dict(op="clip.set", select=sid, args=args,
                expect={p: commands.state_at(states, sid, p) for p in ("keyframes", "keyframeOrigin")})


def clip(document, sid):
    return commands.recorded_owner(Checkout(document), commands.EditCommand("clip.set", sid, {}, resolved=(sid,)), "clip")


def bytes_of(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()


@pytest.fixture
def bundle():
    path = Path(__file__).parents[1] / "fixtures/timeline_editing/tiny_publishable.json"
    return Checkout(json.loads(path.read_text())).document()


def source(bundle, scope):
    before = fixture(bundle, "parent")
    if scope != "parent":
        before = fixture(before, scope)  # same local id in both scopes
    sid = scope + "/clip:movie"
    # Keep the non-frame-aligned movie within the fixture's four-second shot.
    clip(before, sid).data.update(at=2.375, to=3, hold=2)
    # Resolve the unchanged fixture once; W26 never owns adjacent timing cleanup.
    before = RuntimeCommitService._apply(before, [dict(op="timeline.resolve", select=None, args={})])[0]
    return before, sid


@pytest.mark.parametrize("scope", ["parent", "occurrence:occ-A"])
@pytest.mark.parametrize("prior", ["absent", "null", "object", "declared"])
def test_app_pair_exact_receipt_and_whole_document_receipt_only_undo_redo(bundle, scope, prior):
    before, sid = source(bundle, scope)
    row = clip(before, sid).data
    if prior == "null":
        row["keyframes"] = None
    elif prior in {"object", "declared"}:
        row["keyframes"] = {"x": [{"time": 2.0, "value": 1.0, "interpolation": "hold", "old": None}]}
    if prior == "declared":
        row["keyframeOrigin"] = -1.375
    origin = row.get("keyframeOrigin", -row["at"])
    old = copy.deepcopy(before)
    value = wire(before, sid, keyframes=copy.deepcopy(PAYLOAD), keyframeOrigin=origin)
    after, plan, receipt = accepted(before, [value])
    assert bytes_of(before) == bytes_of(old)
    target = clip(after, sid).data
    assert bytes_of(target["keyframes"]) == bytes_of(PAYLOAD)
    assert target["keyframeOrigin"] == origin
    if prior != "declared":
        assert origin == -2.375
        assert 7.125 - (target["at"] + origin) == 7.125
    if scope != "parent":
        assert bytes_of(clip(after, "parent/clip:movie").data) == bytes_of(clip(before, "parent/clip:movie").data)
    assert target["from"] == row["from"] and target["speed"] == row["speed"]
    recorded = receipt["change"]["commands"][0]
    assert recorded["select"] == sid and recorded["resolved"] == [sid]
    assert bytes_of(recorded["args"]) == bytes_of(value["args"])
    assert {w["path"] for w in receipt["change"]["direct_writes"]} == {"keyframes", "keyframeOrigin"}
    assert all(w["id"] == sid for w in receipt["change"]["direct_writes"])
    assert all(recorded["authorship"][p] == "direct" for p in ("keyframes", "keyframeOrigin"))
    assert recorded["expect"] == value["expect"]
    assert not any(w["path"].startswith("keyframes") for w in plan.consequences)
    # Existing history inverse takes only accepted command evidence for values.
    undo = inverse_command(receipt["change"]["commands"], before, after,
                           receipt["accepted_publication_id"], consequences=receipt["change"]["consequences"])
    restored, _, undo_receipt = accepted(after, [undo], receipt)
    assert bytes_of(restored) == bytes_of(before)
    # Reconstruct redo solely from the accepted Undo receipt, with exact closure
    # witnesses required by the existing history API; never reuse the forward call.
    redo = inverse_command(undo_receipt["change"]["commands"], after, restored,
                           undo_receipt["accepted_publication_id"],
                           consequences=undo_receipt["change"]["consequences"])
    redone, _, _ = accepted(restored, [redo], undo_receipt)
    assert bytes_of(redone) == bytes_of(after)


def test_sdk_adapter_captures_pair_and_preserves_opaque_numeric_values(bundle):
    before, sid = source(bundle, "parent")
    tl = Checkout(copy.deepcopy(before))
    target = commands.recorded_owner(tl, commands.EditCommand("clip.set", sid, {}, resolved=(sid,)), "clip")
    with edit_transaction(tl, "App inspector SDK", quiet=True, persist=False) as tx:
        target.set(keyframes=copy.deepcopy(PAYLOAD), keyframeOrigin=-target.data["at"])
    recorded = tx.plan.calls[0]
    assert recorded.select == sid
    assert bytes_of(recorded.args["keyframes"]) == bytes_of(PAYLOAD)
    assert recorded.expect == {"keyframes": {"state": "absent"}, "keyframeOrigin": {"state": "absent"}}


@pytest.mark.parametrize("fault", ["stale-keyframes", "stale-origin", "absent-v-null", "null-v-absent",
    "omit-expect", "omit-one-expect", "omit-keyframes", "omit-origin", "null-keyframes", "null-origin",
    "nan-origin", "inf-time", "wrong-origin", "null-before-origin", "bad-interpolation", "frame-shape",
    "extra-field", "unscoped", "wrong-resolved", "bool-origin", "bool-time", "object-value"])
def test_app_pair_refuses_atomically_without_accepted_receipt(bundle, fault):
    before, sid = source(bundle, "parent")
    origin = -clip(before, sid).data["at"]
    value = wire(before, sid, keyframes=copy.deepcopy(PAYLOAD), keyframeOrigin=origin)
    if fault == "stale-keyframes":
        value["expect"]["keyframes"] = dict(state="present", value={"stale": []})
    elif fault == "stale-origin":
        value["expect"]["keyframeOrigin"] = dict(state="present", value=99)
    elif fault == "absent-v-null":
        value["expect"]["keyframes"] = dict(state="present", value=None)
    elif fault == "null-v-absent":
        clip(before, sid).data["keyframes"] = None
    elif fault == "omit-expect":
        value.pop("expect")
    elif fault == "omit-one-expect":
        value["expect"].pop("keyframeOrigin")
    elif fault.startswith("omit-"):
        value["args"].pop("keyframes" if fault == "omit-keyframes" else "keyframeOrigin")
    elif fault == "null-keyframes":
        value["args"]["keyframes"] = None
    elif fault == "null-origin":
        value["args"]["keyframeOrigin"] = None
    elif fault == "nan-origin":
        value["args"]["keyframeOrigin"] = float("nan")
    elif fault == "inf-time":
        value["args"]["keyframes"]["x"][0]["time"] = float("inf")
    elif fault == "wrong-origin":
        value["args"]["keyframeOrigin"] = origin + 1
    elif fault == "null-before-origin":
        clip(before, sid).data["keyframeOrigin"] = None
        value["expect"]["keyframeOrigin"] = dict(state="present", value=None)
    elif fault == "bad-interpolation":
        value["args"]["keyframes"]["x"][0]["interpolation"] = "frames"
    elif fault == "frame-shape":
        value["args"]["keyframes"] = [{"frame": 30, "value": 1}]
    elif fault == "extra-field":
        value["args"]["opacity"] = 0.5
    elif fault == "unscoped":
        value["select"] = "movie"
    elif fault == "wrong-resolved":
        value["resolved"] = ["occurrence:occ-A/clip:a-rocket"]
    elif fault == "bool-origin":
        value["args"]["keyframeOrigin"] = False
    elif fault == "bool-time":
        value["args"]["keyframes"]["x"][0]["time"] = True
    elif fault == "object-value":
        value["args"]["keyframes"]["x"][0]["value"] = {"not": "App value"}
    original = bytes_of(before)
    tl = Checkout(copy.deepcopy(before))
    with pytest.raises((commands.CommandError, ValueError)):
        commands.run(tl, value)
    assert bytes_of(tl.document()) == original
    # Nonfinite envelopes refuse at decoder before commit request hashing.
    if fault not in {"nan-origin", "inf-time"}:
        body = request(value)
        service, port = core(body, before)
        result = service.commit(body, port)
        assert result["status"] in {"refused", "conflict"}, result
        assert not port.accepts
        assert not result.get("change", {}).get("direct_writes")
    assert bytes_of(before) == original


def test_late_pair_refusal_rolls_back_prior_batch_write_and_publishes_nothing(bundle):
    before, sid = source(bundle, "parent")
    bad = wire(before, sid, keyframes=copy.deepcopy(PAYLOAD), keyframeOrigin=-2.375)
    bad["expect"]["keyframes"] = dict(state="present", value=None)
    earlier = dict(op="clip.set-look", select=sid, args=dict(opacity=0.25))
    batch = dict(op="timeline.batch", select=None, args=dict(commands=[earlier, bad]))
    tl = Checkout(copy.deepcopy(before))
    original = bytes_of(before)
    with pytest.raises(commands.CommandError):
        commands.run(tl, batch)
    assert bytes_of(tl.document()) == original
    body = request(earlier, bad)
    service, port = core(body, before)
    result = service.commit(body, port)
    assert result["status"] in {"refused", "conflict"}
    assert not port.accepts and not result.get("change", {}).get("direct_writes")
    assert bytes_of(before) == original
