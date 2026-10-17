"""W28 captured App metadata; synthetic COMMIT/scene boundaries, no shader execution."""
import copy
import json
from pathlib import Path

import pytest
from test_timeline_commit import CoreService
from test_timeline_commit import request as _request
from test_timeline_fade_history_receipts import port_for, reverse_receipt
from test_timeline_saved_publish import synthetic_scene_answers  # noqa: F401

from astrid.sdk.timeline import commands
from astrid.sdk.timeline.checkout import Checkout
from astrid.sdk.timeline.commit import RuntimeCommitService
from astrid.sdk.timeline.rebase import rebase

pytestmark = pytest.mark.usefixtures("synthetic_scene_answers")
SID = "occurrence:occ-A/clip:clip-1"
FIXTURES = Path(__file__).parents[1] / "fixtures/timeline_editing"
APP = json.loads((FIXTURES / "w28_shader_metadata.json").read_text())


def literal(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False)


def request(*calls):
    return dict(_request(*calls), base_revision_id="parent-r0", base_publication_id="pub-parent-r0")


def target(document, scope):
    if scope == "postprocess":
        return document["parent"]["config"]
    return next(r for r in document["shots"]["A"]["internal_timeline"]["clips"] if r["id"] == "clip-1")


def slot(scope):
    return "shader" if scope == "clip" else "shaderPostprocess"


def owner_path(scope, container=False):
    return (SID, "app" if container else "app.shader") if scope == "clip" else (
        "parent", "config.app" if container else "config.app.shaderPostprocess")


def call(bundle, scope, value):
    sid, path = owner_path(scope)
    old = commands.state_at(commands.entity_states(Checkout(copy.deepcopy(bundle))), sid, path)
    return dict(op="clip.set-shader" if scope == "clip" else "timeline.set-shader-postprocess",
                select=SID if scope == "clip" else None, args=dict(shader=copy.deepcopy(value)),
                expect={sid + "::" + path: old})


def apply(bundle, command):
    return RuntimeCommitService._apply(bundle, [command])[:2]


def accepted(document, body):
    before = literal(document), literal(body)
    port = port_for(document, body)
    result = CoreService().commit(body, port)
    assert result["status"] == "accepted", result
    assert len(port.accepts) == 1
    assert port.accepts[0]["evidence"]["direct_writes"] == result["change"]["direct_writes"]
    assert (literal(document), literal(body)) == before
    return result


def inverse_receipt(plan):
    return reverse_receipt(dict(accepted_publication_id="metadata-publication", change=dict(direct_writes=plan.direct_writes)))


@pytest.fixture
def bundle():
    return json.loads((FIXTURES / "w13_fade_history.json").read_text())["before"]


@pytest.mark.parametrize("scope", ["clip", "postprocess"])
def test_w28_actual_constructor_inspector_receipt_only_undo_redo(bundle, scope, tmp_path):
    original = copy.deepcopy(bundle)
    constructor = copy.deepcopy(APP["constructors"][scope])
    assert "revision" not in constructor and "sourceHash" not in constructor
    first = accepted(bundle, request(call(bundle, scope, constructor)))
    assigned = first["canonical_document"]
    assert target(assigned, scope)["app"][slot(scope)] == constructor
    edit = copy.deepcopy(APP["inspector_edits"][scope]["shader"])
    assert edit["sourceHash"].startswith("inline:") and "revision" not in edit
    queued = call(assigned, scope, edit)
    frozen = literal(queued)
    # A changed descriptor does not regenerate the frozen command or its replay.
    edit["sourceHash"] = "module:changed-after-capture"
    body = dict(request(queued), base_revision_id=first["accepted_revision_id"])
    forward = accepted(assigned, body)
    assert literal(queued) == frozen
    after = forward["canonical_document"]
    assert literal(target(after, scope)["app"][slot(scope)]) == literal(APP["inspector_edits"][scope]["shader"])
    assert [(w["id"], w["path"]) for w in forward["change"]["direct_writes"]] == [owner_path(scope)]
    replay, plan = apply(assigned, forward["change"]["commands"][0])
    assert literal(replay) == literal(after)
    assert literal(list(plan.direct_writes)) == literal(forward["change"]["direct_writes"])
    undo_body = dict(request(reverse_receipt(forward)), base_revision_id=forward["accepted_revision_id"])
    undone = accepted(after, undo_body)
    assert literal(undone["canonical_document"]) == literal(assigned)
    redo_body = dict(request(reverse_receipt(undone)), base_revision_id=undone["accepted_revision_id"])
    redone = accepted(assigned, redo_body)
    assert literal(redone["canonical_document"]) == literal(after)
    other = "postprocess" if scope == "clip" else "clip"
    assert literal(target(after, other)) == literal(target(original, other))
    (tmp_path / "metadata-proof.json").write_text(json.dumps(dict(before=original, assigned=first,
        forward=forward, undo=undone, redo=redone), indent=2) + "\n")


@pytest.mark.parametrize("scope", ["clip", "postprocess"])
@pytest.mark.parametrize("initial", ["absent-app", "null-app", "orphan", "null-slot"])
def test_w28_sparse_containers_and_orphan_removal_exact_history(bundle, scope, initial):
    data = target(bundle, scope)
    container = initial.endswith("app")
    if container:
        if scope == "postprocess":
            # No cut reservations are discarded by this absent/null fixture.
            for shot in bundle["shots"].values():
                for clip in shot["internal_timeline"]["clips"]:
                    clip.get("app", {}).get("astrid", {}).pop("cut", None)
        data.pop("app", None)
        if initial == "null-app":
            data["app"] = None
        value = APP["constructors"][scope]
    else:
        legacy = {"unknownLegacyShader": True, "revision": {"opaque": None}, "opaque": [7.0, {"op": "clear"}]}
        data.setdefault("app", {}).update(opaque={"null": None}, **{slot(scope): legacy if initial == "orphan" else None})
        value = None
    before = literal(bundle)
    after, plan = apply(bundle, call(bundle, scope, value))
    assert [(w["id"], w["path"]) for w in plan.direct_writes] == [owner_path(scope, container)]
    assert literal(bundle) == before
    if value is None:
        assert slot(scope) not in target(after, scope)["app"]
        assert target(after, scope)["app"]["opaque"] == {"null": None}
    restored, undo = apply(after, inverse_receipt(plan))
    assert literal(restored) == before
    redone, _ = apply(restored, inverse_receipt(undo))
    assert literal(redone) == literal(after)


@pytest.mark.parametrize("scope", ["clip", "postprocess"])
def test_w28_missing_remove_is_guarded_noop_and_opaque_revision_is_retained(bundle, scope):
    data = target(bundle, scope)
    data.setdefault("app", {}).pop(slot(scope), None)
    before = literal(bundle)
    after, plan = apply(bundle, call(bundle, scope, None))
    assert literal(after) == before and not plan.direct_writes
    metadata = copy.deepcopy(APP["constructors"][scope])
    metadata["revision"] = {"legacy-opaque": [None, 2.0]}
    after, plan = apply(bundle, call(bundle, scope, metadata))
    assert literal(plan.calls[0].args["shader"]) == literal(metadata)
    assert literal(target(after, scope)["app"][slot(scope)]) == literal(metadata)


@pytest.mark.parametrize("scope", ["clip", "postprocess"])
@pytest.mark.parametrize("bad", ["missing-expect", "stale", "wrong-owner", "wrong-resolved", "scope", "shaderId",
                                 "extensionId", "contributionId", "enabled", "uniforms", "sourceHash", "keyframes", "textures", "invented-pin"])
def test_w28_atomic_refusal_without_partial_receipt(bundle, scope, bad):
    invalid = call(bundle, scope, APP["constructors"][scope])
    if bad == "missing-expect":
        invalid.pop("expect")
    elif bad == "stale":
        invalid["expect"] = {k: dict(state="present", value={"stale": True}) for k in invalid["expect"]}
    elif bad == "wrong-owner":
        invalid["select"] = "parent/track:V1" if scope == "clip" else SID
    elif bad == "wrong-resolved":
        invalid["resolved"] = ["occurrence:occ-A/clip:clip-2"]
    elif bad == "invented-pin":
        invalid["args"]["pin"] = {"revision": "synthetic"}
    else:
        invalid["args"]["shader"][bad] = {
            "scope": "postprocess" if scope == "clip" else "clip", "shaderId": "", "extensionId": None,
            "contributionId": "", "enabled": 1, "uniforms": [], "sourceHash": 1,
            "keyframes": {"x": [{"time": 1, "value": 2}]}, "textures": {"x": {"kind": "invalid"}},
        }[bad]
    other = "postprocess" if scope == "clip" else "clip"
    body = request(call(bundle, other, APP["constructors"][other]), invalid)
    snapshot = literal(bundle), literal(body)
    port = port_for(bundle, body)
    result = CoreService().commit(body, port)
    assert result["status"] in {"refused", "conflict"}, result
    assert result["canonical_document"] is None and not result["change"]["direct_writes"]
    assert not port.accepts and port.saved is None
    assert (literal(bundle), literal(body)) == snapshot
    assert literal(port.bundles[body["base_revision_id"]]) == snapshot[0]


@pytest.mark.parametrize("scope", ["clip", "postprocess"])
def test_w28_sibling_rebase_stale_slot_and_moved_base_are_atomic(bundle, scope):
    data = target(bundle, scope)
    data.setdefault("app", {})[slot(scope)] = copy.deepcopy(APP["constructors"][scope])
    command = call(bundle, scope, None)
    after, plan = apply(bundle, command)
    head = copy.deepcopy(bundle)
    target(head, scope)["app"]["new-sibling"] = {"opaque": None}
    replay = rebase(plan.calls, head)
    assert not replay.conflicts
    restored, _ = apply(replay.document, inverse_receipt(plan))
    assert target(restored, scope)["app"] == target(head, scope)["app"]
    changed = copy.deepcopy(after)
    target(changed, scope)["app"][slot(scope)] = {"later": True}
    tl = Checkout(copy.deepcopy(changed))
    original = literal(tl.document())
    with pytest.raises(commands.TimelineEditError):
        commands.run(tl, inverse_receipt(plan))
    assert literal(tl.document()) == original
    # A real moved synthetic head keeps the old base and exact request intact.
    body = request(command)
    port = port_for(bundle, body)
    port.current = "parent-r1"
    port.bundles[port.current] = changed
    result = CoreService().commit(body, port)
    assert result["status"] in {"conflict", "refused"}, result
    assert not port.accepts and not result["change"]["direct_writes"]


def test_w28_sdk_captures_expectations_and_one_history_step(bundle, tmp_path):
    tl = Checkout(copy.deepcopy(bundle))
    tl.save(tmp_path / "shader-history.json", quiet=True)
    before = literal(tl.document())
    with tl.edit("two shader owners") as tx:
        tl.clip(SID).set_shader(APP["constructors"]["clip"])
        tl.set_shader_postprocess(APP["constructors"]["postprocess"])
    assert len(tx.plan.calls) == 2 and all(c.expect for c in tx.plan.calls)
    assert {(w["id"], w["path"]) for w in tx.plan.direct_writes} == {owner_path("clip"), owner_path("postprocess")}
    after = literal(tl.document())
    tl.undo()
    assert literal(tl.document()) == before
    tl.redo()
    assert literal(tl.document()) == after
