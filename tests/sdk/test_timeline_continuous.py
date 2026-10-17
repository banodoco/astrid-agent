"""W27 top-level App continuous writes; actual SDK with synthetic acceptance."""
import copy
import json

import pytest
from test_timeline_commit import CoreService
from test_timeline_fade_history_receipts import (
    FIXTURE,
    accept,
    literal,
    port_for,
    reverse_receipt,
)
from test_timeline_saved_publish import synthetic_scene_answers  # noqa: F401

from astrid.sdk.timeline import commands
from astrid.sdk.timeline.checkout import Checkout
from astrid.sdk.timeline.commit import RuntimeCommitService
from astrid.sdk.timeline.rebase import rebase
from astrid.sdk.timeline.transaction import edit_transaction

pytestmark = pytest.mark.usefixtures("synthetic_scene_answers")
SID = "occurrence:occ-A/clip:clip-1"
EFFECT = {"type": "custom:orbit", "intensity": 0.731, "params": {
    "amount": 2, "opaque": {"dots.and[brackets]": [True, None, {"op": "clear"}, 1.0]},
}}


def row(document):
    return document["shots"]["A"]["internal_timeline"]["clips"][0]


@pytest.fixture
def case():
    value = json.loads(FIXTURE.read_text())
    before = value["before"]
    clip = row(before)
    clip["params"] = {"continuous": {"unrelated": True}}
    clip["continuous"] = copy.deepcopy(EFFECT)
    return before, value["requests"][0]


def call(document, **args):
    state = commands.state_at(commands.entity_states(Checkout(document)), SID, "continuous")
    return dict(op="clip.set-continuous", select=SID, args=args, expect={"continuous": state})


def body_for(template, *calls):
    body = copy.deepcopy(template)
    body.update(request_id="w27-forward")
    body["change"]["commands"] = list(calls)
    return body


@pytest.mark.parametrize("mode", ["set", "replace", "app-patch", "patch", "clear", "no-params"])
def test_w27_accepted_receipt_undo_and_receipt_only_redo(case, mode, tmp_path):
    before, template = case
    if mode == "set":
        del row(before)["continuous"]
    elif mode == "no-params":
        row(before)["continuous"].pop("params")
    expected = copy.deepcopy(row(before).get("continuous"))
    if mode in {"set", "replace"}:
        expected = {"type": "custom:new", "intensity": 0, "params": {"new": [1.0, None]}}
        args = {"continuous": expected}
    elif mode == "app-patch":
        expected["params"]["amount"] = {"nested": [False, 1.0]}
        args = {"continuous": expected}  # exact App spread-and-replace shape
    elif mode in {"patch", "no-params"}:
        args = {"params": {"amount": {"nested": [False, 1.0]}}}
        expected.setdefault("params", {}).update(args["params"])
    else:
        expected, args = None, {"clear": True}
    command = call(before, **args)
    body = body_for(template, command)
    original = literal(before), literal(body)
    forward = accept(before, body)
    after = forward["canonical_document"]
    expected_state = {"state": "absent"} if mode == "clear" else {"state": "present", "value": expected}
    writes = forward["change"]["direct_writes"]
    assert writes == [dict(id=SID, path="continuous", before=command["expect"]["continuous"], after=expected_state)]
    actual = copy.deepcopy(row(after))
    actual.pop("continuous", None)
    siblings = copy.deepcopy(row(before))
    siblings.pop("continuous", None)
    assert literal(actual) == literal(siblings)
    assert row(after).get("continuous") == expected
    assert row(after)["params"]["continuous"] == {"unrelated": True}
    assert (literal(before), literal(body)) == original

    undo_body = body_for(template, reverse_receipt(forward))
    undo_body.update(request_id="w27-undo", session_sequence=2,
                     base_revision_id=forward["accepted_revision_id"])
    undone = accept(after, undo_body)
    assert literal(undone["canonical_document"]) == literal(before)
    assert undone["change"]["direct_writes"] == reverse_receipt(forward)["args"]["writes"]
    redo_body = body_for(template, reverse_receipt(undone))
    redo_body.update(request_id="w27-redo", session_sequence=3,
                     base_revision_id=undone["accepted_revision_id"])
    redone = accept(undone["canonical_document"], redo_body)
    assert literal(redone["canonical_document"]) == literal(after)
    (tmp_path / (mode + "-proof.json")).write_text(json.dumps(dict(
        before=before, requests=[body, undo_body, redo_body],
        results=[forward, undone, redone]), indent=2) + "\n")


@pytest.mark.parametrize("damage", [
    "stale-type", "stale-intensity", "stale-params", "stale-opaque", "stale-presence",
    "type", "intensity", "bool-intensity", "params", "null", "missing-type", "blank-type",
    "missing-expect", "bad-expect", "alias", "missing-owner", "selector-owner", "duplicate-owner",
    "empty", "mixed", "bad-clear", "unknown", "patch-array", "patch-absent", "patch-null",
])
def test_w27_refuses_atomically_without_acceptance_or_receipt(case, damage):
    before, template = case
    command = call(before, continuous={"type": "new", "intensity": 0.5, "params": {}})
    if damage.startswith("stale-"):
        field = damage.removeprefix("stale-")
        effect = row(before)["continuous"]
        if field == "presence":
            del row(before)["continuous"]
        elif field == "opaque":
            effect["params"]["opaque"]["dots.and[brackets]"].append(None)
        else:
            effect[field] = {"type": "other", "intensity": 0.2, "params": {}}[field]
    elif damage in {"type", "intensity", "bool-intensity", "params", "null", "missing-type", "blank-type"}:
        effect = command["args"]["continuous"]
        if damage == "null":
            command["args"]["continuous"] = None
        elif damage == "missing-type":
            del effect["type"]
        elif damage == "blank-type":
            effect["type"] = " "
        else:
            effect["intensity" if damage == "bool-intensity" else damage] = {
                "type": 3, "intensity": "0.2", "bool-intensity": True, "params": [],
            }[damage]
    elif damage == "missing-expect":
        command.pop("expect")
    elif damage == "bad-expect":
        command["expect"]["continuous"] = {"state": "absent", "value": None}
    elif damage in {"alias", "missing-owner", "selector-owner"}:
        command["select"] = "clip-1" if damage == "alias" else "parent/clip:clip-1"
        if damage == "selector-owner":
            command["resolved"] = [SID]
    elif damage == "duplicate-owner":
        before["shots"]["A"]["internal_timeline"]["clips"].append(copy.deepcopy(row(before)))
    else:
        command["args"] = {
            "empty": {}, "mixed": {"continuous": EFFECT, "clear": True},
            "bad-clear": {"clear": 1}, "unknown": {"intensity": 0.5},
            "patch-array": {"params": []}, "patch-absent": {"params": {"x": 2}},
            "patch-null": {"params": {"x": 2}},
        }[damage]
        if damage in {"patch-absent", "patch-null"}:
            if damage == "patch-absent":
                del row(before)["continuous"]
            else:
                row(before)["continuous"] = None
            command["expect"]["continuous"] = commands.state_at(
                commands.entity_states(Checkout(before)), SID, "continuous")
    body = body_for(template, command)
    snapshot = literal(before), literal(body)
    port = port_for(before, body)
    result = CoreService().commit(body, port)
    assert result["status"] in {"refused", "conflict"}, result
    assert not port.accepts and port.saved is None
    assert not result["change"]["direct_writes"] and result["canonical_document"] is None
    assert (literal(before), literal(body)) == snapshot


@pytest.mark.parametrize("damage", ["type", "intensity", "params", "opaque", "absent", "malformed", "duplicate", "owner"])
def test_w27_inverse_guards_whole_slot_and_rolls_back_prior_writes(case, damage):
    before, _ = case
    after, forward, _ = RuntimeCommitService._apply(before, [call(before, params={"amount": 5})])
    receipt = dict(accepted_publication_id="w27-pub", change=dict(direct_writes=list(forward.direct_writes)))
    undo = reverse_receipt(receipt)
    write = undo["args"]["writes"][0]
    if damage in {"type", "intensity", "params", "opaque", "absent"}:
        effect = row(after)["continuous"]
        if damage == "absent":
            del row(after)["continuous"]
        elif damage == "opaque":
            effect["params"]["opaque"] = None
        else:
            effect[damage] = {"type": "later", "intensity": 0, "params": {}}[damage]
    elif damage == "malformed":
        write["after"] = {"state": "absent", "value": None}
    elif damage == "duplicate":
        undo["args"]["writes"].append(copy.deepcopy(write))
    else:
        write["id"] = "parent/clip:clip-1"
    tl = Checkout(copy.deepcopy(after))
    snapshot = literal(tl.document())
    view = tl.clip("clip-1")
    with pytest.raises(commands.TimelineEditError):
        with edit_transaction(tl, "atomic inverse", persist=False, quiet=True) as tx:
            commands.execute(tl, {"op": "clip.set", "select": SID, "args": {"opacity": 0.2}})
            commands.execute(tl, undo)
    assert literal(tl.document()) == snapshot
    assert literal(view.data) == literal(row(after))
    assert tx.plan is None and not getattr(tx, "direct_writes", ())


def test_w27_sdk_capture_scoped_guard_noop_and_disjoint_rebase(case):
    before, _ = case
    tl = Checkout(copy.deepcopy(before))
    patch = {"amount": 7, "literal": [1.0, {"op": "clear"}]}
    with edit_transaction(tl, "SDK", persist=False, quiet=True) as tx:
        assert tl.clip("clip-1").set_continuous(params=patch) is not None
    call = tx.calls[0]
    assert literal(call.args["params"]) == literal(patch)
    assert literal(row(tl.document())["continuous"]["params"]["literal"]) == literal(patch["literal"])
    assert call.select == SID and call.resolved == (SID,)
    assert call.expect["continuous"] == {"state": "present", "value": EFFECT}
    assert tx.plan.direct_writes[0]["path"] == "continuous"
    later = copy.deepcopy(before)
    row(later)["app"]["opaque-later"] = [None, 1.0]
    result = rebase(tx.calls, later)
    assert not result.conflicts
    assert row(result.document)["app"]["opaque-later"] == [None, 1.0]
    no_op = call.to_json()
    no_op["expect"] = {SID + "::continuous": {"state": "present", "value": row(tl.document())["continuous"]}}
    _, plan, _ = RuntimeCommitService._apply(tl.document(), [no_op])
    assert not plan.direct_writes


def test_w27_parent_owner_with_same_local_id_is_isolated(case):
    before, _ = case
    before["parent"]["clips"].append(copy.deepcopy(row(before)))
    command = call(before, params={"amount": 9})
    command["select"] = "parent/clip:clip-1"
    after, plan, _ = RuntimeCommitService._apply(before, [command])
    assert literal(row(after)) == literal(row(before))
    assert after["parent"]["clips"][0]["continuous"]["params"]["amount"] == 9
    assert {w["id"] for w in plan.direct_writes} == {"parent/clip:clip-1"}


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf")])
def test_w27_nonfinite_payload_refuses_before_mutation(case, value):
    before, _ = case
    command = call(before, continuous={"type": "new", "intensity": value})
    tl = Checkout(copy.deepcopy(before))
    with pytest.raises(commands.TimelineEditError):
        commands.run(tl, command)
    assert literal(tl.document()) == literal(before)


def test_w27_shared_source_changes_only_the_selected_occurrence(case):
    before, _ = case
    before["placements"].append({**copy.deepcopy(before["placements"][0]), "occurrence_id": "occ-B"})
    original = literal(before)
    after, plan, _ = RuntimeCommitService._apply(before, [call(before, params={"amount": 8})])
    states = commands.entity_states(Checkout(after))
    assert literal(states["occurrence:occ-B/clip:clip-1"]) == literal(row(before))
    assert states[SID]["continuous"]["params"]["amount"] == 8
    assert literal(before) == original
    assert {w["id"] for w in plan.direct_writes} == {SID}


def test_w27_late_forward_refusal_has_no_partial_receipt(case):
    before, template = case
    first = call(before, params={"amount": 8})
    second = call(before, continuous={"type": "new", "intensity": False})
    second["expect"]["continuous"]["value"]["params"]["amount"] = 8
    body = body_for(template, first, second)
    port = port_for(before, body)
    original = literal(before)
    result = CoreService().commit(body, port)
    assert result["status"] == "refused", result
    assert not result["change"]["direct_writes"] and result["canonical_document"] is None
    assert not port.accepts and port.saved is None
    assert literal(before) == original
    tl = Checkout(copy.deepcopy(before))
    with pytest.raises(commands.TimelineEditError, match="finite number"):
        with edit_transaction(tl, "late continuous refusal", quiet=True, persist=False) as tx:
            commands.execute(tl, first)
            commands.execute(tl, second)
    assert literal(tl.document()) == original and tx.plan is None
