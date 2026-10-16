"""Returned IDENTITY/REBASE contracts consumed by publication inverses; fake Runtime only."""
import copy

import pytest

from astrid.core.timeline.authoring_bundle import compile_authoring_candidate
from astrid.packs.timeline import cli_history
from astrid.sdk import timeline_intent as intent
from astrid.sdk.timeline import commands, inverse, saved_publish
from astrid.sdk.timeline.checkout import Checkout
from astrid.sdk.timeline.commit import RuntimeCommitService, _digest, _Refusal
from astrid.sdk.timeline.identity import next_cut_id
from astrid.sdk.timeline.rebase import rebase
from astrid.sdk.timeline._common import TimelineEditError
from test_timeline_publication_inverse import setup, publish, edit, parsed


from test_timeline_saved_publish import synthetic_scene_answers  # noqa: F401

pytestmark = pytest.mark.usefixtures("synthetic_scene_answers")


def call(op, select=None, **args):
    return dict(op=op, select=select, args=args)


def apply(document, *calls):
    return RuntimeCommitService._apply(document, calls)


def backward(before, after, plan):
    return inverse.inverse_command(plan.calls, before, after, "publication-under-test", consequences=plan.consequences)


def seed(runtime, document):
    compiled = compile_authoring_candidate(document).publication
    parent = compiled["parent_composition"]
    runtime.parents["rev-0"].update(payload=parent, content_digest=_digest(parent))
    runtime.shots.update({r["revision_id"]: dict(r, project_id="p") for r in compiled["shot_revisions"]})
    runtime.internals.update({r["revision_id"]: dict(r, project_id="p") for r in compiled["internal_timeline_revisions"]})


@pytest.mark.parametrize("op", ["clip.remove", "clip.remove-layer"])
def test_remove_resurrects_exact_payload_scope_order_and_keeps_later_work(setup, op):
    draft, runtime, client = setup
    original = copy.deepcopy(draft.clip("a-rocket").data)
    order = [c["id"] for c in draft._internal("A")["clips"]]
    commands.run(draft, call(op, "c1.rocket"))
    first = publish(draft, client, "remove exact layer")
    other = Checkout.draft("p", "t", name="other")
    commands.run(other, call("clip.set", "b-type", text="later text"))
    later = publish(other, client, "unrelated text")
    draft.undo(client=client)
    result = publish(draft, client, "Undo removal")
    assert result["previous_revision_id"] == later["accepted_revision_id"]
    assert draft.clip("a-rocket").data == original
    assert [c["id"] for c in draft._internal("A")["clips"]] == order
    assert draft.clip("b-type").get("text") == "later text"
    assert result["change"]["commands"][0]["created"] == ["occurrence:occ-A/clip:a-rocket"]


def test_add_then_set_inverse_removes_created_id_without_touching_source(setup):
    draft, _runtime, client = setup
    original = copy.deepcopy(draft.clip("a-rocket").data)
    commands.run(draft, call("timeline.add", element="am-sprite", at='"viral"', hold=0.5,
                             track="sprite", layer="added", params=dict(x=600)))
    added = draft.clip("c1.added").id
    commands.run(draft, call("clip.set", added, x=120))
    result = publish(draft, client, "add and edit")
    assert any(added in sid for c in result["change"]["commands"] for sid in c["created"])
    draft.undo(client=client)
    publish(draft, client, "Undo add and edit")
    assert added not in {c.id for c in draft.clips()}
    assert draft.clip("a-rocket").data == original


@pytest.mark.parametrize("op,args", [("cut.split", dict(on=2.0)), ("cut.duplicate", dict(at=0.0))])
def test_split_and_duplicate_inverses_preserve_members_and_retired_cut_reservations(setup, op, args):
    draft, _runtime, client = setup
    before = {c.id: copy.deepcopy(c.data) for c in draft.clips()}
    commands.run(draft, call(op, "c1", **args))
    first = publish(draft, client, op)
    assert "cut:c1a" in first["change"]["commands"][0]["created"]
    draft.undo(client=client)
    publish(draft, client, "Undo " + op)
    assert {c.id: c.data for c in draft.clips()} == before
    assert next_cut_id(draft.bundle, "c1") == "c1b"
    commands.run(draft, call("cut.split", "c1", on=2.0))
    assert draft.cut("c1b").id == "c1b"


def test_detach_audio_inverse_keeps_video_identity_source_range_and_gain(setup):
    draft, runtime, client = setup
    doc = draft.document()
    tl = Checkout(doc)
    picture = tl.cut("c1").picture
    picture.data.update(clipType="media", asset="video", **{"from": 1.0, "to": 5.0, "volume": 0.6})
    picture.data.pop("hold", None)
    tl._internal("A")["tracks"].append(dict(id="audio", kind="audio"))
    tl._registry("A")["video"] = {**copy.deepcopy(tl._registry("A")["P"]), "type": "video"}
    seed(runtime, tl.document())
    draft = Checkout.draft("p", "t", name="detachment", fresh=True)
    original = copy.deepcopy(draft.cut("c1").picture.data)
    commands.run(draft, call("clip.detach-audio", original["id"]))
    first = publish(draft, client, "detach audio")
    audio_id = first["change"]["commands"][0]["created"][0].split("/clip:", 1)[1]
    assert draft.clip(audio_id).data["from"] == 1.0 and draft.clip(audio_id).data["to"] == 5.0
    draft.undo(client=client)
    publish(draft, client, "Undo detach audio")
    assert draft.clip(original["id"]).data == original
    assert audio_id not in {c.id for c in draft.clips()}


def test_resurrection_and_dependent_set_rebase_with_one_explicit_injective_map(setup):
    draft, _runtime, _client = setup
    base = draft.document()
    removed, forward, _ = apply(base, call("clip.remove", "a-rocket"))
    value = backward(base, removed, forward)
    _, plan, _ = apply(removed, value, call("clip.set", "a-rocket", x=120))
    old, new = "occurrence:occ-A/clip:a-rocket", "occurrence:occ-A/clip:restored-mapped"
    rebased = rebase(plan.calls, removed, id_map={old: new})
    assert not rebased.conflicts and rebased.id_map == {old: new}
    assert rebased.plan.calls[0].created == (new,)
    assert rebased.plan.calls[1].resolved == (new,)
    assert Checkout(rebased.document).clip("restored-mapped").get("x") == 120
    assert old not in commands.entity_states(Checkout(rebased.document))


def test_inverse_staged_then_created_member_edit_names_actual_later_publication(setup):
    draft, _runtime, client = setup
    commands.run(draft, call("cut.duplicate", "c1", at=0.0))
    first = publish(draft, client, "duplicate")
    member = next(s for s in first["change"]["commands"][0]["created"] if "/clip:" in s)
    draft.undo(client=client)
    other = Checkout.draft("p", "t", name="other")
    commands.run(other, call("clip.set-look", member, opacity=0.4))
    later = publish(other, client, "edit created member")
    result = draft.publish("Undo duplicate", client=client)
    assert result["status"] == "conflict" and result["conflicts"][0]["kind"] == "same-property"
    assert later["accepted_publication_id"] in result["choices"][0]["options"][0]["label"]
    assert other.clip(member.split("/clip:")[1]).data["opacity"] == 0.4


def test_removed_entity_undo_conflict_names_prior_restore_event(setup, capsys):
    draft, _runtime, client = setup
    commands.run(draft, call("clip.remove", "a-rocket"))
    publish(draft, client, "remove")
    assert cli_history.run_restore(parsed(client, from_ref="rev-0", idempotency_key="restore-identity")) == 0
    with pytest.raises(TimelineEditError, match="same-property") as exc:
        draft.undo(client=client)
    assert "restore-publication (restore)" in str(exc.value)


def test_batch_add_set_remove_lifetime_does_not_resurrect_a_transient_entity(setup):
    draft, _runtime, _client = setup
    before = draft.document()
    after, plan, _ = apply(before, call("timeline.batch", commands=[
        call("timeline.add", element="am-sprite", at='"viral"', hold=0.5, track="sprite", clip_id="temporary"),
        call("clip.set", "temporary", x=120), call("clip.remove", "temporary"),
        call("clip.set", "a-rocket", x=240)]))
    restored, _, _ = apply(after, backward(before, after, plan))
    assert "temporary" not in {c.id for c in Checkout(restored).clips()}
    assert Checkout(restored).clip("a-rocket").params == Checkout(before).clip("a-rocket").params


def test_reflow_uses_recorded_causal_writes_instead_of_inferring_commands_from_a_diff(setup):
    draft, _runtime, _client = setup
    tl = Checkout(draft.document())
    intent.set_gap_after(tl.clip("a-vo").data, None)
    before = tl.document()
    after, plan, _ = apply(before, call("timeline.reflow", gaps={"a-vo": 0.8}))
    assert plan.changed and plan.consequences
    assert plan.calls[0].expect == {
        "occurrence:occ-A/clip:a-music::music_bed.ripple_basis": {"state": "absent"}}
    value = backward(before, after, plan)
    assert value["args"]["writes"]
    assert inverse.authored_paths(plan.calls) == {commands.write_target(key, call.resolved)
        for call in plan.calls for key in call.authorship}
    restored, _, _ = apply(after, value)
    assert {c.id for c in Checkout(restored).clips()} == {c.id for c in Checkout(before).clips()}
    assert Checkout(restored).clip("b-vo").start == Checkout(before).clip("b-vo").start


def test_external_add_is_a_literal_existing_adapter_refusal_not_an_identity_gap(setup):
    draft, _runtime, _client = setup
    with pytest.raises(_Refusal, match="timeline.add with media requires an admitted scoped media/adapter producer"):
        apply(draft.document(), call("timeline.add", element="media", at=0, asset={"ref": "run:synthetic#0"}))


def test_ripple_inverse_restores_each_recorded_deleted_member_and_timing(setup):
    draft, _runtime, _client = setup
    before = draft.document()
    after, plan, _ = apply(before, call("timeline.ripple-delete", start=0, end=4))
    assert len(Checkout(after).clips()) < len(Checkout(before).clips())
    assert apply(before, *plan.calls)[0] == after
    restored, undo, _ = apply(after, backward(before, after, plan))
    assert {c.id: c.data for c in Checkout(restored).clips()} == {c.id: c.data for c in Checkout(before).clips()}
    assert Checkout(restored).clip('b-vo').start == Checkout(before).clip('b-vo').start
    redone, _, _ = apply(restored, backward(after, restored, undo))
    assert {c.id: c.data for c in Checkout(redone).clips()} == {c.id: c.data for c in Checkout(after).clips()}
    assert Checkout(redone).clip('b-vo').start == Checkout(after).clip('b-vo').start


def test_music_alias_allocation_replays_and_inverts_exact_ids_and_payloads(setup):
    draft, _runtime, _client = setup
    tl = Checkout(draft.document())
    tl.clip('a-music').data['to'] = 8.
    unrelated = dict(id='a-music:b', clipType='am-sprite', track='sprite', asset='R',
                     at=0., hold=.5, params={})
    tl._internal('A')['clips'].append(copy.deepcopy(unrelated))
    tl.insert_time(2, 1)
    before = tl.document()
    after, plan, _ = apply(before, call('timeline.insert-time', at=2, seconds=1))
    assert Checkout(after).clip('a-music:b').data == unrelated
    assert Checkout(after).clip('a-music:b-2').track == 'music'
    assert apply(before, *plan.calls)[0] == after
    restored, undo, _ = apply(after, backward(before, after, plan))
    assert {c.id: c.data for c in Checkout(restored).clips()} == {c.id: c.data for c in Checkout(before).clips()}
    redone, _, _ = apply(restored, backward(after, restored, undo))
    assert {c.id: c.data for c in Checkout(redone).clips()} == {c.id: c.data for c in Checkout(after).clips()}


def test_root_payload_remaps_declared_cut_relation_but_not_opaque_text(setup):
    draft, _runtime, _client = setup
    original = Checkout(draft.document())
    original.clip("a-rocket").params["opaque"] = "cut:c1"
    before = original.document()
    after, plan, _ = apply(before, call("clip.remove", "a-rocket"))
    _, undo, _ = apply(after, backward(before, after, plan))
    head = Checkout(copy.deepcopy(after))
    intent.set_cut(head.clip("a-plate").data, "c9")
    old, new = "occurrence:occ-A/clip:a-rocket", "occurrence:occ-A/clip:renamed"
    result = rebase(undo.calls, head.document(), id_map={old: new, "cut:c1": "cut:c9"})
    assert not result.conflicts and result.id_map == {old: new, "cut:c1": "cut:c9"}
    clip = Checkout(result.document).clip("renamed")
    assert intent.cut_of(clip.data) == "c9" and clip.params["opaque"] == "cut:c1"


def test_inverse_mapping_is_injective_and_preserves_member_kind(setup):
    draft, _runtime, _client = setup
    before = draft.document()
    after, plan, _ = apply(before, call("clip.remove", "a-rocket"))
    _, undo, _ = apply(after, backward(before, after, plan))
    old = "occurrence:occ-A/clip:a-rocket"
    for destination in ("cut:c99", "occurrence:occ-A/clip:a-plate"):
        refused = rebase(undo.calls, after, id_map={old: destination})
        assert refused.document is None and refused.conflicts[0]["kind"] == "dependent"


def test_local_undo_redo_retains_returned_identity_reservations(setup):
    from astrid.sdk.timeline import journal
    draft, _runtime, _client = setup
    commands.run(draft, call("cut.split", "c1", on=2.0))
    draft.undo()
    assert next_cut_id(draft.bundle, "c1") == "c1b"
    draft.redo()
    assert draft.cut("c1a").id == "c1a" and next_cut_id(draft.bundle, "c1") == "c1b"
    assert journal.verify(journal.journal_path("p", "t")) == []


def test_returned_cut_binding_map_preserves_split_add_dependency_and_its_inverse(setup):
    draft, _runtime, _client = setup
    before = draft.document()
    after, plan, _ = apply(before, call("cut.split", "c1", on=2.0),
        call("timeline.add", element="am-sprite", at="c1a +3f", hold=0.5, track="sprite", clip_id="dependent"))
    picture = next(sid for sid in plan.calls[0].created if "/clip:" in sid)
    dependent = next(sid for sid in plan.calls[1].created if "/clip:" in sid)
    mapping = {"cut:c1a": "cut:c1b", picture: "occurrence:occ-A/clip:mapped-picture",
               dependent: "occurrence:occ-A/clip:mapped-dependent"}
    remapped = rebase(plan.calls, before, id_map=mapping)
    assert not remapped.conflicts and remapped.id_map == mapping
    assert remapped.plan.calls[0].args["id"] == "c1b"
    assert remapped.plan.calls[1].args["at"] == "c1b +3f"
    assert remapped.plan.calls[1].bindings["c1b +3f"] == "cut:c1b:start"
    undo = backward(before, remapped.document, remapped.plan)
    restored, _, _ = apply(remapped.document, undo)
    assert {c.id: c.data for c in Checkout(restored).clips()} == {c.id: c.data for c in Checkout(before).clips()}
