"""Real opener/runner/compiler on persisted disposable exact closures.

The JSON port implements only Runtime's request-bound persistence contract;
this is provider evidence, not authenticated installed Runtime/App or rendering.
"""
from __future__ import annotations

import copy
import json

import pytest
from test_timeline_commit import StaleHead, request
from test_timeline_saved_publish import synthetic_scene_answers  # noqa: F401

from astrid.core.timeline.authoring_bundle import open_authoring_bundle
from astrid.core.timeline.chapter_storage import ChapterStorageError
from astrid.sdk.timeline import commands
from astrid.sdk.timeline.chapter_storage import compile_candidate
from astrid.sdk.timeline.checkout import Checkout
from astrid.sdk.timeline.commit import RuntimeCommitService, _digest
from astrid.sdk.timeline.inverse import inverse_command
from astrid.sdk.timeline.rebase import rebase

pytestmark = pytest.mark.usefixtures("synthetic_scene_answers")


def scene():
    tracks = [dict(id="picture", kind="visual", scaleAppliesToPositionedClips=True, opaque=dict(v=9)),
              dict(id="music", kind="music"), dict(id="vo", kind="audio")]
    ranges = [dict(name="First", kind="chapter", **{"from": "c1", "to": "c2"}),
              dict(name="Second", kind="chapter", **{"from": "c2", "to": None}),
              dict(name="Beat", kind="beat", **{"from": "c1 +1s", "to": "c1 +3s"})]
    clips = [dict(id="one", track="picture", clipType="media", asset="image", at=0, hold=2,
                  app=dict(astrid=dict(cut="c1")), opaque=dict(keep=None)),
             dict(id="two", track="picture", clipType="media", asset="image", at=2, hold=2,
                  app=dict(astrid=dict(cut="c2"))),
             dict(id="cross", track="vo", clipType="media", asset="sound", at=1.5, **{"from": .25, "to": 1.25}),
             dict(id="bed", track="music", clipType="media", asset="sound", at=0, **{"from": .5, "to": 4.5})]
    assets = dict(image=dict(type="image", object_id="sha256:" + "1" * 64),
                  sound=dict(type="audio", object_id="sha256:" + "2" * 64))
    internal = dict(tracks=tracks, clips=clips, registry=dict(assets=assets), effects=[], audio=[], assets=[], layout={})
    shot = dict(metadata=dict(name="Film", untouched=dict(x=True)), items=[], pools=[], selected_variants={},
                provenance={}, generation_inputs={}, audio_bindings=[], text_bindings=[], internal_timeline_revision_id="int-0")
    occurrence = dict(occurrence_id="film-occ", shot_id="film-shot", shot_revision_id="shot-0", at_ms=0,
                      duration_ms=4000, source_offset=0, speed=1, track=None, transform={}, gain=1, muted=False, provenance={})
    parent = dict(config=dict(app=dict(astrid=dict(v=2, ranges=ranges, slots={}, cut_id_high_water=dict(number=2, suffixes={}), opaque=dict(future=True)))),
                  clips=[], registry={}, occurrences=[occurrence])
    return dict(parent_revision=dict(project_id="p", timeline_id="t", revision_id="rev-0", payload=parent, content_digest=_digest(parent)),
                shot_revisions=[dict(project_id="p", shot_id="film-shot", revision_id="shot-0", internal_timeline_revision_id="int-0", payload=shot, content_digest=_digest(shot))],
                internal_timeline_revisions=[dict(project_id="p", timeline_id="inner", revision_id="int-0", payload=internal, content_digest=_digest(internal))])


class DiskPort:
    """Actual bytes and exact old/new immutable pins, not chapter-layout injection."""
    def __init__(self, root, closure):
        self.path = root / "synthetic-store.json"
        self.accepts = []
        self.race = False
        self._write(dict(current="rev-0", parents={"rev-0": closure["parent_revision"]},
                         shots={r["revision_id"]: r for r in closure["shot_revisions"]},
                         internals={r["revision_id"]: r for r in closure["internal_timeline_revisions"]}, saved={}))

    def _write(self, data):
        self.path.write_text(json.dumps(data, sort_keys=True))

    def data(self):
        return json.loads(self.path.read_text())

    def lookup_request(self):
        return self.data()["saved"].get(self.body["request_id"])

    def head(self):
        rid = self.data()["current"]
        return dict(revision_id=rid, publication_id="pub-" + rid, updated_at="synthetic")

    def revision_closure(self, rid):
        data = self.data()
        parent = data["parents"][rid]
        shots = [data["shots"][key] for key in dict.fromkeys(r["shot_revision_id"] for r in parent["payload"]["occurrences"])]
        internals = [data["internals"][key] for key in dict.fromkeys(r["internal_timeline_revision_id"] for r in shots)]
        return dict(parent_revision=parent, shot_revisions=shots, internal_timeline_revisions=internals)

    def open(self, rid=None):
        return open_authoring_bundle(**self.revision_closure(rid or self.head()["revision_id"]))

    def commit(self, *calls, base=None, dry_run=False, request_id="request-1"):
        self.body = request(*calls, dry_run=dry_run)
        self.body.update(base_revision_id=base or self.head()["revision_id"], request_id=request_id)
        return RuntimeCommitService().commit(self.body, self)

    def accept(self, prepared):
        if self.race:
            raise StaleHead()
        data = self.data()
        if data["current"] != prepared["expected_head_revision_id"]:
            raise StaleHead()
        self.accepts.append(copy.deepcopy(prepared))
        result = copy.deepcopy(prepared["result"])
        previous = data["current"]
        if prepared["publication"] is not None:
            rid = "rev-" + str(len(data["parents"]))
            publication = prepared["publication"]
            for row in publication["shot_revisions"]:
                data["shots"][row["revision_id"]] = dict(row, project_id="p")
            for row in publication["internal_timeline_revisions"]:
                data["internals"][row["revision_id"]] = dict(row, project_id="p")
            payload = publication["parent_composition"]
            data["parents"][rid] = dict(project_id="p", timeline_id="t", revision_id=rid, payload=payload, content_digest=_digest(payload))
            data["current"] = rid
            result.update(accepted_revision_id=rid, accepted_publication_id="pub-" + rid)
        result.update(previous_revision_id=previous, runtime_receipt=dict(publisher="synthetic", accepted_at="synthetic", evidence_digest=_digest(prepared["evidence"]), narration_state=None))
        data["saved"][self.body["request_id"]] = dict(request_hash=_digest(self.body), result=result)
        self._write(data)
        return dict(stored_result=result)


def restructure(head="rev-0"):
    return dict(op="timeline.restructure", select=None, args=dict(expected_head=head, policy="chapters"))


@pytest.fixture
def port(tmp_path):
    return DiskPort(tmp_path, scene())


def projected_clips(closure):
    from astrid.core.timeline.shot_composition_projection import project_runtime_parent_composition
    value = project_runtime_parent_composition(**closure)
    result = []
    for clip in value.config["clips"]:
        clip = copy.deepcopy(clip)
        for key in ("id", "shot_id", "shot_occurrence_id", "shot_name"):
            clip.pop(key, None)
        clip.get("app", {}).pop("astrid_shot_composition", None)
        if not clip.get("app"):
            clip.pop("app", None)
        for key in ("asset", "asset_id"):
            if key in clip:
                clip[key] = value.registry["assets"][clip[key]]
        clip.setdefault("volume", 1)
        result.append(clip)
    return sorted(result, key=lambda c: (c["track"], c.get("at", 0)))


def test_persisted_restructure_reopen_edit_commit_and_exact_replay(port):
    from unittest.mock import patch

    from astrid.core.timeline import shot_composition_projection
    from astrid.core.timeline.chapter_storage import _render_input
    from astrid.sdk.timeline.story import story_overview

    original = port.path.read_bytes()
    before = port.revision_closure("rev-0")
    opened = port.open()
    assert "structure_base" not in opened and "storage_policy" not in opened
    preview = commands.run(Checkout(copy.deepcopy(opened)), restructure(), dry_run=True)
    assert preview.changed and port.path.read_bytes() == original
    first = port.commit(restructure())
    assert first["status"] == "accepted", first
    assert len(port.accepts) == 1
    rid = first["accepted_revision_id"]
    closure = port.revision_closure(rid)
    assert len(closure["parent_revision"]["payload"]["occurrences"]) == 2
    assert all(r["payload"]["tracks"] == [] for r in closure["internal_timeline_revisions"])
    parent = closure["parent_revision"]["payload"]
    assert {c["id"] for c in parent["clips"]} == {"cross", "bed"}
    assert [t["id"] for t in parent["config"]["tracks"]] == ["picture", "music", "vo"]
    assert parent["config"]["tracks"][0]["scaleAppliesToPositionedClips"] is True
    assert projected_clips(before) == projected_clips(closure)
    # Observe the real opener's exact persisted inputs; delegate to the shared
    # production projection rather than replacing its track reconciliation.
    with patch.object(shot_composition_projection, "project_runtime_parent_composition",
                      wraps=shot_composition_projection.project_runtime_parent_composition) as project:
        fresh = port.open(rid)
        project.assert_called_once_with(closure["parent_revision"],
                                       shot_revisions=closure["shot_revisions"],
                                       internal_timeline_revisions=closure["internal_timeline_revisions"])
    assert fresh["base_parent"]["revision_id"] == rid
    renderer_before = _render_input(opened)
    assert renderer_before == _render_input(fresh["structure_base"])
    assert renderer_before[2] == 120
    assert fresh["placements"][0]["shot_id"] == opened["placements"][0]["shot_id"]
    assert fresh["placements"][0]["occurrence_id"] == opened["placements"][0]["occurrence_id"]
    assert {c["id"]: c["at"] for c in fresh["shots"]["film-shot"]["internal_timeline"]["clips"]} == {
        c["id"]: c["at"] for c in opened["shots"]["film-shot"]["internal_timeline"]["clips"]}
    assert compile_candidate(fresh).publication["parent_composition"] == parent
    second = port.commit(dict(op="clip.set", select="occurrence:film-occ/clip:one", args=dict(opacity=.75)), request_id="request-2")
    assert second["status"] == "accepted", second
    assert len(port.accepts) == 2
    second_rid = second["accepted_revision_id"]
    assert port.accepts[1]["expected_head_revision_id"] == rid
    read = port.open(second_rid)
    assert read["base_parent"]["revision_id"] == second_rid
    assert Checkout(read).clip("one").data["opacity"] == .75
    edited = port.revision_closure(second_rid)
    assert edited["parent_revision"]["payload"]["config"]["tracks"] == parent["config"]["tracks"]
    assert all(r["payload"]["tracks"] == [] for r in edited["internal_timeline_revisions"])
    tracks = {t["id"] for t in parent["config"]["tracks"]}
    assert all(c["track"] in tracks for r in edited["internal_timeline_revisions"] for c in r["payload"]["clips"])
    ranges = opened["parent"]["config"]["app"]["astrid"]["ranges"]
    expected_ranges = [dict(range=row, start_frame=start, end_frame=end)
                       for row, (start, end) in zip(ranges, [(0, 60), (60, 120), (30, 90)])]
    for document, revision in ((opened, "rev-0"), (fresh, rid), (read, second_rid)):
        overview = story_overview(document, revision_id=revision)
        assert overview["findings"] == []
        assert overview["ranges"] == expected_ranges
        clips = {c["id"]: c for c in document["shots"]["film-shot"]["internal_timeline"]["clips"]}
        assert {cid: (clips[cid]["from"], clips[cid]["to"], clips[cid]["at"])
                for cid in ("cross", "bed")} == {"cross": (.25, 1.25, 1.5), "bed": (.5, 4.5, 0)}
    assert set(port.data()["parents"]) == {"rev-0", rid, second_rid}
    assert port.revision_closure("rev-0") == before
    assert port.revision_closure(rid) == closure
    # Replay needs only exact retained closure bytes, no current store/head.
    offline = open_authoring_bundle(**copy.deepcopy(closure))
    assert compile_candidate(offline).publication["parent_composition"] == parent
    assert _render_input(offline["structure_base"]) == renderer_before


def test_retry_rebase_inverse_and_rename_identity(port):
    opened = port.open()
    a = Checkout(copy.deepcopy(opened))
    plan = commands.run(a, restructure())
    assert compile_candidate(a.document()) == compile_candidate(a.document())
    onto = copy.deepcopy(opened)
    onto["base_parent"]["revision_id"] = "head-other"
    rebased = rebase(plan.calls, onto)
    assert not rebased.conflicts, rebased.conflicts
    assert compile_candidate(rebased.document).publication["parent_composition"]["occurrences"] == compile_candidate(a.document()).publication["parent_composition"]["occurrences"]
    inverse = inverse_command(plan.calls, opened, a.document(), "pub-change", consequences=plan.consequences)
    commands.run(a, inverse)
    assert "storage_policy" not in a.bundle
    commands.run(a, restructure())
    original_ids = [r["shot_id"] for r in compile_candidate(a.document()).publication["parent_composition"]["occurrences"]]
    a.bundle["parent"]["config"]["app"]["astrid"]["ranges"][1]["name"] = "Renamed"
    assert [r["shot_id"] for r in compile_candidate(a.document()).publication["parent_composition"]["occurrences"]] == original_ids
    first = port.commit(restructure())
    snapshot = port.path.read_bytes()
    retry = port.commit(restructure(), base="rev-0")
    assert first == retry and port.path.read_bytes() == snapshot and len(port.accepts) == 1


@pytest.mark.parametrize("change,match", [
    (lambda c: c["parent_revision"]["payload"]["config"]["app"]["astrid"]["ranges"][1].update({"from": "c1 +3s"}), "contiguously"),
    (lambda c: c["parent_revision"]["payload"]["config"]["app"]["astrid"]["ranges"][1].update({"from": "missing-cut"}), ""),
    (lambda c: c["parent_revision"]["payload"]["config"]["app"]["astrid"]["ranges"][0].update({"to": "c1 +1f"}), "representable"),
    (lambda c: c["parent_revision"]["payload"]["occurrences"][0].update(transform=dict(scale=2)), "transform"),
])
def test_refusals_are_write_free(tmp_path, change, match):
    closure = scene()
    change(closure)
    closure["parent_revision"]["content_digest"] = _digest(closure["parent_revision"]["payload"])
    port = DiskPort(tmp_path, closure)
    before = port.path.read_bytes()
    result = port.commit(restructure())
    assert result["status"] == "refused", result
    assert match in result["error"]["message"]
    assert port.path.read_bytes() == before and not port.accepts


def test_dry_run_stale_cas_noop_and_tampered_baseline(port):
    before = port.path.read_bytes()
    result = port.commit(restructure(), dry_run=True)
    assert result["status"] == "preview", result
    assert port.path.read_bytes() == before and not port.accepts
    port.race = True
    stale = port.commit(restructure())
    assert stale["status"] == "conflict", stale
    assert port.path.read_bytes() == before and not port.accepts
    port.race = False
    first = port.commit(restructure())
    fresh = port.open()
    before = port.path.read_bytes()
    plan = commands.run(Checkout(copy.deepcopy(fresh)), restructure(first["accepted_revision_id"]), dry_run=True)
    assert not plan.changed and port.path.read_bytes() == before
    fresh["structure_base"]["shots"]["film-shot"]["internal_timeline"]["clips"][0]["hold"] += 1
    with pytest.raises(ChapterStorageError, match="structure_base"):
        compile_candidate(fresh)
    assert port.path.read_bytes() == before


def inventory(path):
    return {str(p.relative_to(path)): p.read_bytes() for p in path.rglob("*") if p.is_file()}


def test_saved_command_undo_redo_and_cli_preflight_no_writes(tmp_path, port, capsys):
    from argparse import Namespace

    from astrid.packs.timeline.cli_working import _cmd_restructure
    target = tmp_path / "draft.json"
    tl = Checkout(port.open())
    tl.save(target, quiet=True)
    before = inventory(tmp_path)
    args = Namespace(file=str(target), dry_run=True, json=True)
    assert _cmd_restructure(args) == 0
    assert json.loads(capsys.readouterr().out)["changed"]
    assert inventory(tmp_path) == before
    commands.run(tl, restructure())
    expected = compile_candidate(tl.document()).publication
    tl.undo()
    assert "storage_policy" not in tl.bundle
    tl.redo()
    actual = compile_candidate(tl.document()).publication
    from astrid.core.timeline.authoring_bundle import _diff_values
    differences = []
    _diff_values(expected, actual, "", differences)
    if actual != expected:
        print(json.dumps(differences, indent=2))
    assert actual == expected, differences
    before = inventory(tmp_path)
    noop = commands.run(tl, restructure())
    assert not noop.changed and inventory(tmp_path) == before
    bad = restructure("wrong-head")
    with pytest.raises(commands.CommandError, match="expected_head"):
        commands.run(tl, bad)
    assert inventory(tmp_path) == before
    with pytest.raises(commands.CommandError, match="expected_head"):
        tl.restructure(expected_head="wrong-head")
    assert inventory(tmp_path) == before


def test_names_do_not_reallocate_after_persisted_rename_or_rebase(port):
    first = port.commit(restructure())
    fresh = port.open()
    before = compile_candidate(fresh).publication["parent_composition"]["occurrences"]
    fresh["parent"]["config"]["app"]["astrid"]["ranges"][0]["name"] = "Renamed first"
    fresh["parent"]["config"]["app"]["astrid"]["ranges"][1]["name"] = "Renamed second"
    after = compile_candidate(fresh).publication["parent_composition"]["occurrences"]
    assert [(r["shot_id"], r["occurrence_id"]) for r in before] == [(r["shot_id"], r["occurrence_id"]) for r in after]
    flat = port.open("rev-0")
    tl = Checkout(copy.deepcopy(flat))
    plan = commands.run(tl, restructure())
    onto = copy.deepcopy(flat)
    onto["base_parent"]["revision_id"] = first["accepted_revision_id"]
    onto["parent"]["config"]["app"]["astrid"]["ranges"][1]["name"] = "Display changed"
    rebased = rebase(plan.calls, onto)
    assert not rebased.conflicts, rebased.conflicts
    a, b = compile_candidate(tl.document()).publication, compile_candidate(rebased.document).publication
    assert [(r["shot_id"], r["occurrence_id"]) for r in a["parent_composition"]["occurrences"]] == [(r["shot_id"], r["occurrence_id"]) for r in b["parent_composition"]["occurrences"]]


def test_opaque_projection_refusal_precedes_write(port):
    tl = Checkout(port.open())
    tl.bundle["shots"]["film-shot"]["internal_timeline"]["renderer_future"] = dict(affects_frames=True)
    before = port.path.read_bytes()
    with pytest.raises(ChapterStorageError, match="renderer state"):
        commands.run(tl, restructure(), dry_run=True)
    assert port.path.read_bytes() == before and not port.accepts


@pytest.mark.parametrize("mode", ["equal", "disjoint", "conflict", "app-conflict"])
def test_legacy_track_merge_uses_existing_projection_atomically(tmp_path, mode):
    closure = scene()
    original = copy.deepcopy(closure["internal_timeline_revisions"][0]["payload"]["tracks"][0])
    parent_track = copy.deepcopy(original)
    parent_track["app"] = dict(owner="film")
    child_track = closure["internal_timeline_revisions"][0]["payload"]["tracks"][0]
    child_track["app"] = dict(owner="film")
    if mode == "disjoint":
        child_track["app"] = dict(editor=dict(future=17))
    elif mode == "conflict":
        child_track["scaleAppliesToPositionedClips"] = False
    elif mode == "app-conflict":
        child_track["app"] = dict(owner="other")
    closure["parent_revision"]["payload"]["config"]["tracks"] = [parent_track]
    closure["parent_revision"]["content_digest"] = _digest(closure["parent_revision"]["payload"])
    closure["internal_timeline_revisions"][0]["content_digest"] = _digest(closure["internal_timeline_revisions"][0]["payload"])
    port = DiskPort(tmp_path, closure)
    before = port.path.read_bytes()
    result = port.commit(restructure())
    if "conflict" in mode:
        assert result["status"] == "refused", result
        assert port.path.read_bytes() == before and not port.accepts
    else:
        assert result["status"] == "accepted", result
        tracks = port.revision_closure(port.head()["revision_id"])["parent_revision"]["payload"]["config"]["tracks"]
        merged = next(t for t in tracks if t["id"] == "picture")
        assert merged["opaque"] == dict(v=9) and merged["scaleAppliesToPositionedClips"] is True
        assert merged["app"] == (dict(owner="film", editor=dict(future=17)) if mode == "disjoint" else dict(owner="film"))
        assert all(not r["payload"]["tracks"] for r in port.revision_closure(port.head()["revision_id"])["internal_timeline_revisions"])


def test_persisted_storage_inverse_and_redo_reuses_chapter_ids(port):
    opened = port.open()
    tl = Checkout(copy.deepcopy(opened))
    plan = commands.run(tl, restructure())
    first = port.commit(*[c.to_json() for c in plan.calls])
    assert first["status"] == "accepted", first
    chapter_ids = [(r["shot_id"], r["occurrence_id"]) for r in port.revision_closure("rev-1")["parent_revision"]["payload"]["occurrences"]]
    fresh = Checkout(port.open())
    inverse = inverse_command(plan.calls, opened, fresh.document(), "synthetic-publication", consequences=plan.consequences)
    inverse_plan = commands.run(fresh, inverse)
    flat = compile_candidate(fresh.document()).publication
    assert len(flat["parent_composition"]["occurrences"]) == 1
    assert flat["shot_revisions"][0]["payload"]["metadata"] == opened["shots"]["film-shot"]["payload"]["metadata"]
    assert "name" not in flat["shot_revisions"][0]["payload"]
    redo = inverse_command(inverse_plan.calls, port.open(), fresh.document(), "synthetic-undo", consequences=inverse_plan.consequences)
    commands.run(fresh, redo)
    again = compile_candidate(fresh.document()).publication["parent_composition"]["occurrences"]
    assert [(r["shot_id"], r["occurrence_id"]) for r in again] == chapter_ids


def test_ordinary_flat_edits_do_not_opt_in_from_ranges(port):
    first = port.commit(dict(op="clip.set", select="occurrence:film-occ/clip:one", args=dict(opacity=.5)))
    assert first["status"] == "accepted", first
    closure = port.revision_closure(first["accepted_revision_id"])
    assert len(closure["parent_revision"]["payload"]["occurrences"]) == 1
    assert "storage_policy" not in port.open()
    assert "structure_base" not in port.open()


def test_renderer_order_change_refuses_without_writing(port):
    tl = Checkout(port.open())
    # A crossing picture moved to the parent would precede an overlapping
    # chapter picture in the renderer. Refuse rather than change layer order.
    clips = tl.bundle["shots"]["film-shot"]["internal_timeline"]["clips"]
    clips.append(dict(id="overlay", track="picture", clipType="media", asset="image", at=1.5, hold=1))
    before = port.path.read_bytes()
    with pytest.raises(ChapterStorageError, match="payload/order"):
        commands.run(tl, restructure(), dry_run=True)
    assert port.path.read_bytes() == before and not port.accepts


def test_flat_legacy_labels_enter_through_existing_lossless_migration(tmp_path):
    closure = scene()
    parent = closure["parent_revision"]["payload"]
    parent["config"]["app"]["astrid"].pop("ranges")
    parent["config"]["chapters"] = [dict(name="First", **{"from": "c1"}), dict(name="Second", **{"from": "c2"})]
    internal = closure["internal_timeline_revisions"][0]["payload"]
    internal["clips"][0]["app"]["astrid"]["on"] = '"one"'
    internal["clips"][1]["app"]["astrid"]["on"] = '"two"'
    internal["clips"].append(dict(id="voice", track="vo", clipType="media", asset="sound", at=0,
                                  **{"from": 0, "to": 4}, app=dict(astrid=dict(segment="n1", text="one two", words=[[0, .1, "one"], [2, 2.1, "two"]]))))
    closure["parent_revision"]["content_digest"] = _digest(parent)
    closure["internal_timeline_revisions"][0]["content_digest"] = _digest(internal)
    port = DiskPort(tmp_path, closure)
    before = port.path.read_bytes()
    checked = Checkout(port.open())
    ranges = checked.bundle["parent"]["config"]["app"]["astrid"]["ranges"]
    assert [r["name"] for r in ranges] == ["First", "Second"]
    assert port.path.read_bytes() == before
    result = port.commit(restructure())
    assert result["status"] == "accepted", result
    assert len(port.revision_closure(port.head()["revision_id"])["parent_revision"]["payload"]["occurrences"]) == 2


def test_authored_flat_shot_name_and_payload_survive_restructure(tmp_path):
    closure = scene()
    payload = closure["shot_revisions"][0]["payload"]
    payload["name"] = "Original film name"
    payload["opaque"] = dict(future=dict(name="never rewrite"))
    closure["shot_revisions"][0]["content_digest"] = _digest(payload)
    port = DiskPort(tmp_path, closure)
    result = port.commit(restructure())
    assert result["status"] == "accepted", result
    fresh = port.open()
    assert "structure_base" in fresh
    actual = fresh["shots"]["film-shot"]["payload"]
    assert actual["name"] == payload["name"] and actual["opaque"] == payload["opaque"]


def test_saved_publish_generated_route_finalizes_restructured_exact_head(tmp_path, monkeypatch, port):
    from test_timeline_saved_publish import RuntimeFixture

    from astrid.sdk.authoring_revision_diff import retain_revision_closure
    from astrid.sdk.timeline import journal, store
    from astrid.sdk.timeline.saved_intent import read_saved_calls

    monkeypatch.setenv("BANODOCO_LOCAL_DATA_ROOT", str(tmp_path / "data"))
    seed = port.open()
    seed["shots"]["film-shot"]["payload"]["metadata"]["fixture_seed"] = True
    runtime = RuntimeFixture(seed)
    closure = runtime.closure("rev-0")
    shots = {r["revision_id"]: r for r in closure["shot_revisions"]}
    internals = {r["revision_id"]: r for r in closure["internal_timeline_revisions"]}
    bundle = open_authoring_bundle(closure["parent_revision"], shot_revisions=shots, internal_timeline_revisions=internals)
    retain_revision_closure(bundle, closure["parent_revision"], shots, internals)
    monkeypatch.setattr(store, "resolve_ids", lambda *a, **kw: ("p", "t", "rev-0"))
    monkeypatch.setattr(store, "fetch_bundle", lambda *a, **kw: copy.deepcopy(bundle))
    draft = Checkout.draft("p", "t")
    commands.run(draft, restructure())
    result = Checkout.load(draft.path).publish("chapter storage", client=runtime.client())
    assert result["status"] == "accepted" and result["local_state"] == "finalized", result
    assert len(runtime.publications) == 1
    fresh = Checkout.load(draft.path)
    assert "structure_base" in fresh.bundle and fresh.bundle["storage_policy"] == "chapters"
    assert not read_saved_calls(fresh)
    commands.run(fresh, dict(op="clip.set", select="occurrence:film-occ/clip:one", args=dict(opacity=.5)))
    second = Checkout.load(draft.path).publish("semantic edit", client=runtime.client())
    assert second["status"] == "accepted" and second["local_state"] == "finalized", second
    assert len(runtime.publications) == 2
    assert journal.verify(journal.journal_path("p", "t")) == []


def test_missing_track_reference_refuses_before_write(tmp_path):
    closure = scene()
    internal = closure["internal_timeline_revisions"][0]["payload"]
    internal["tracks"] = [t for t in internal["tracks"] if t["id"] != "vo"]
    closure["internal_timeline_revisions"][0]["content_digest"] = _digest(internal)
    port = DiskPort(tmp_path, closure)
    before = port.path.read_bytes()
    result = port.commit(restructure())
    assert result["status"] == "refused", result
    assert port.path.read_bytes() == before and not port.accepts


def test_opaque_parent_storage_policy_does_not_block_ordinary_edits(tmp_path):
    closure = scene()
    parent = closure["parent_revision"]["payload"]
    parent["storage_policy"] = dict(opaque="other authored data")
    closure["parent_revision"]["content_digest"] = _digest(parent)
    port = DiskPort(tmp_path, closure)
    result = port.commit(dict(op="clip.set", select="occurrence:film-occ/clip:one", args=dict(opacity=.5)))
    assert result["status"] == "accepted", result
    assert port.revision_closure(port.head()["revision_id"])["parent_revision"]["payload"]["storage_policy"] == parent["storage_policy"]
    assert "structure_base" not in port.open()
