"""W30 App text projection through the existing command/acceptance/inverse path."""
import copy
import json
import os
import subprocess
from functools import lru_cache
from pathlib import Path

import pytest
from test_timeline_fade_history_receipts import accept, literal, reverse_receipt
from test_timeline_saved_publish import synthetic_scene_answers  # noqa: F401

from astrid.core.element import catalog
from astrid.sdk.timeline import commands
from astrid.sdk.timeline.checkout import Checkout
from astrid.sdk.timeline.inverse import inverse_command
from astrid.sdk.timeline.transaction import edit_transaction

pytestmark = pytest.mark.usefixtures("synthetic_scene_answers", "declared")
FIXTURE = Path(__file__).parents[1] / "fixtures/timeline_editing/tiny_publishable.json"


@pytest.fixture
def declared(monkeypatch):
    from astrid.sdk import invocation
    element = catalog._registry().get("effects", "am-type", pack_id="astrid_motion")
    # Only the resource/renderer boundary is synthetic. Use the real declaration
    # and its real schema/component, plus the real SDK recorder/commit/inverse.
    monkeypatch.setattr(invocation, "_admitted_timeline_element_resources", lambda config, **kw: {
        row["id"]: {"declaration": {"pack_root": str(element.component.parent), "resources": [
            {"kind": "implementation", "path": element.component.name}]}} for row in config["clips"]})
    descriptor = catalog._element_descriptor(element)
    return {key: descriptor[key] for key in ("id", "kind", "packId", "revision")}


def apply(document, *calls):
    tl = Checkout(copy.deepcopy(document))
    with edit_transaction(tl, "manual text", quiet=True, persist=False) as tx:
        tx.canonical = True
        for call in calls:
            commands.execute(tl, call)
    return tl.document(), tx.plan


@lru_cache(maxsize=1)
def canonical_base():
    document = json.loads(FIXTURE.read_text())
    document["parent"]["config"]["tracks"] = copy.deepcopy(document["shots"]["A"]["internal_timeline"]["tracks"])
    document, _ = apply(document, dict(op="timeline.resolve", select=None, args={}))
    document["parent"]["config"]["app"]["astrid"]["cut_id_high_water"] = {"number": 2, "suffixes": {}}
    return document


@pytest.fixture
def base():
    return copy.deepcopy(canonical_base())


def rows(document, sid):
    return document["parent"]["clips"] if sid.startswith("parent/") else document["shots"]["B"]["internal_timeline"]["clips"]


def create(base, ref, sid="parent/clip:clip-9", time_domain="timeline-seconds"):
    start = 1 if sid.startswith("parent/") else 4.5
    if time_domain == "owner-seconds" and sid.startswith("occurrence:"):
        start = .5
    order = [r["id"] for r in rows(base, sid) if r.get("track") == "type"]
    return dict(op="timeline.add", select=None, created=[sid], args=dict(
        element="am-type", text=dict(content="Café\nHello 🌍", fontSize=64.0, color="#12ab34", align="center"),
        elementRef=copy.deepcopy(ref), action=dict(id="clip-9", start=start, end=start+.5, effectId="effect-clip-9"),
        time_domain=time_domain, clip_id="clip-9", track="type", geometry=dict(x=120.0, y=120, width=640, height=180), opacity=1,
        expected_order=order, clip_order=[*order, "clip-9"]))


@pytest.mark.parametrize("sid,flat,time_domain", [("parent/clip:clip-9", False, "timeline-seconds"),
    ("parent/clip:clip-9", True, "owner-seconds"),
    ("occurrence:occ-B/clip:clip-9", False, "timeline-seconds"),
    ("occurrence:occ-B/clip:clip-9", False, "owner-seconds")])
def test_creation_exact_identity_action_order_receipt_replay_and_inverse(base, declared, sid, flat, time_domain):
    if flat:
        base["parent"]["tracks"] = base["parent"]["config"].pop("tracks")
    call = create(base, declared, sid, time_domain)
    after, plan = apply(base, call)
    made = next(r for r in rows(after, sid) if r["id"] == "clip-9")
    assert made["text"] == call["args"]["text"]
    assert made["params"]["text"] == made["text"]["content"]
    assert made["params"]["size"] == 64 and made["params"]["color"] == "#12ab34"
    assert {key: made[key] for key in ("x", "y", "width", "height")} == call["args"]["geometry"]
    assert not set(made["params"]) & {"x", "y", "width", "height"}
    assert made["opacity"] == 1
    assert made["elementRef"] == declared
    assert made["clipType"] == "am-type" and made["hold"] == .5
    assert [r for r in rows(after, sid) if r["id"] != "clip-9"] == rows(base, sid)
    assert plan.calls[0].args["action"] == call["args"]["action"]
    assert literal(plan.calls[0].args["text"]) == literal(call["args"]["text"])
    assert plan.calls[0].created == (sid,) and plan.calls[0].resolved == (sid,)
    assert [(w["id"], w["path"], w["before"]) for w in plan.direct_writes] == [(sid, "", {"state": "absent"})]
    assert literal(apply(base, plan.calls[0].to_json())[0]) == literal(after)
    undone, undo_plan = apply(after, inverse_command(plan.calls, base, after, "create"))
    assert literal(undone) == literal(base)
    redone, _ = apply(undone, inverse_command(undo_plan.calls, after, undone, "undo"))
    assert literal(redone) == literal(after)


@pytest.mark.parametrize("params", [False, True])
def test_edit_preserves_siblings_exact_undo_and_redo(base, declared, params):
    sid = "parent/clip:legacy-text"
    legacy = dict(id="legacy-text", clipType="text", track="type", at=1, hold=.5,
                  text=dict(content="Original", fontSize=60, color="#ffffff"), x=120, y=120, width=640,
                  app={"opaque": {"nested": [None, 6.0]}}, opacity=.7)
    if params:
        legacy["params"] = {"opaque": [None, 7.0]}
    base["parent"]["clips"].append(legacy)
    call = dict(op="clip.set-text", select=sid, args=dict(text=dict(content="New ✨", fontSize=72.0, color="#111111"), elementRef=declared))
    after, plan = apply(base, call)
    edited = next(r for r in after["parent"]["clips"] if r["id"] == "legacy-text")
    assert edited["text"] == call["args"]["text"] and edited["params"]["size"] == 72
    assert edited["app"] == legacy["app"] and edited["opacity"] == .7
    assert after["parent"]["clips"][:-1] == base["parent"]["clips"][:-1]
    assert {w["id"] for w in plan.direct_writes} == {sid}
    assert {w["path"] for w in plan.direct_writes} >= {"clipType", "text", "elementRef"}
    undone, undo_plan = apply(after, inverse_command(plan.calls, base, after, "edit"))
    assert literal(undone) == literal(base)
    assert literal(apply(undone, inverse_command(undo_plan.calls, after, undone, "undo"))[0]) == literal(after)


@pytest.mark.parametrize("bad", ["style", "height", "size", "clamped-size", "clamped-width", "content", "align", "color", "width", "geometry-bool", "time", "action", "order", "append", "id", "owner", "track", "revision", "declaration", "created"])
def test_invalid_mapping_identity_owner_order_is_atomic_without_receipt(base, declared, bad):
    call = create(base, declared)
    args = call["args"]
    if bad == "style":
        args["text"]["fontFamily"] = 17
    if bad == "height":
        args["geometry"]["height"] = -180
    if bad == "size":
        args["text"]["fontSize"] = 0
    if bad == "clamped-size":
        args["text"]["fontSize"] = .5
    if bad == "clamped-width":
        args["geometry"]["width"] = .5
    if bad == "content":
        args["text"]["content"] = None
    if bad == "align":
        args["text"]["align"] = "justify"
    if bad == "color":
        args["text"]["color"] = None
    if bad == "width":
        args["geometry"]["width"] = -1
    if bad == "geometry-bool":
        args["geometry"]["x"] = True
    if bad == "time":
        args["at"] = 9
    if bad == "action":
        args["action"]["effectId"] = "different"
    if bad == "order":
        args["expected_order"] = ["stale"]
    if bad == "append":
        args["clip_order"] = []
    if bad == "id":
        args["clip_id"] = "not-allocated"
    if bad == "owner":
        call["created"] = ["occurrence:missing/clip:clip-9"]
    if bad == "track":
        args["track"] = "vo"
    if bad == "revision":
        args["elementRef"]["revision"] = "stale"
    if bad == "declaration":
        args["elementRef"]["id"] = "other"
    if bad == "created":
        call.pop("created")
    tl = Checkout(copy.deepcopy(base))
    original = literal(tl.document())
    with pytest.raises(commands.TimelineEditError):
        with edit_transaction(tl, "refuse", quiet=True, persist=False) as tx:
            commands.execute(tl, call)
    assert literal(tl.document()) == original
    assert tx.plan is None and not tl._pending_calls


@pytest.mark.parametrize("bad", ["order", "owner", "placement", "id", "track"])
def test_recorded_creation_stale_inputs_refuse_atomically(base, declared, bad):
    call = create(base, declared, "occurrence:occ-B/clip:clip-9")
    _, plan = apply(base, call)
    changed = copy.deepcopy(base)
    if bad == "order":
        rows(changed, call["created"][0]).reverse()
    if bad == "owner":
        changed["placements"][1]["shot_id"] = "A"
    if bad == "placement":
        changed["placements"][1]["placement"]["start_ms"] += 100
    if bad == "id":
        rows(changed, call["created"][0]).append(dict(id="clip-9", clipType="am-type", track="type", at=0, hold=.5, params={}))
    if bad == "track":
        changed["shots"]["B"]["internal_timeline"]["tracks"][1]["kind"] = "audio"
    tl = Checkout(changed)
    snapshot = literal(tl.document())
    with pytest.raises(commands.TimelineEditError):
        with edit_transaction(tl, "stale", quiet=True, persist=False) as tx:
            commands.execute(tl, plan.calls[0])
    assert literal(tl.document()) == snapshot and tx.plan is None


def test_manual_edit_does_not_convert_word_caption(base, declared):
    after, _ = apply(base, dict(op="timeline.add", select=None, created=["parent/clip:caption"],
        args=dict(element="am-type", params=dict(text="Word caption"), at=1, hold=.5, clip_id="caption", track="type")))
    with pytest.raises(commands.TimelineEditError, match="word captions"):
        apply(after, dict(op="clip.set-text", select="parent/clip:caption", args=dict(text={"content": "new"}, elementRef=declared)))


@pytest.mark.parametrize("edit", [False, True])
def test_accepted_creation_edit_undo_receipt_only_redo(base, declared, tmp_path, edit):
    call = create(base, declared)
    call["args"]["text"] = dict(content="Double-click to edit", fontSize=64, color="#ffffff", align="center")
    call["args"]["action"]["end"] = call["args"]["action"]["start"] + 5
    if edit:
        base, _ = apply(base, call)
        call = dict(op="clip.set-text", select="parent/clip:clip-9", args=dict(text=dict(content="Updated", fontSize=71.0, color="#fff000", fontFamily="Inter, system-ui, sans-serif", bold=True, italic=True), elementRef=declared))
    body = json.loads((FIXTURE.parent / "w13_fade_history.json").read_text())["requests"][0]
    body.update(project_id=base["project_id"], timeline_id=base["timeline_id"], request_id="w30-forward-"+str(edit), scope={"target": "parent", "complete": [], "projection_version": 1})
    body["change"]["commands"] = [call]
    forward = accept(base, body)
    undo = copy.deepcopy(body)
    undo.update(request_id="w30-undo-"+str(edit), session_sequence=2, base_revision_id=forward["accepted_revision_id"])
    undo["change"]["commands"] = [reverse_receipt(forward)]
    undone = accept(forward["canonical_document"], undo)
    assert literal(undone["canonical_document"]) == literal(base)
    redo = copy.deepcopy(undo)
    redo.update(request_id="w30-redo-"+str(edit), session_sequence=3, base_revision_id=undone["accepted_revision_id"])
    redo["change"]["commands"] = [reverse_receipt(undone)]
    redone = accept(undone["canonical_document"], redo)
    assert literal(redone["canonical_document"]) == literal(forward["canonical_document"])
    (tmp_path / "accepted-history-proof.json").write_text(json.dumps(dict(before=base, requests=[body, undo, redo], results=[forward, undone, redone]), indent=2)+"\n")


@pytest.mark.parametrize("bold,italic", [(False, False), (True, False), (False, True), (True, True)])
@pytest.mark.parametrize("family", ["Georgia, serif", "Inter, system-ui, sans-serif"])
def test_existing_app_styles_and_inspector_spread_preserve_opaque_payload(base, declared, family, bold, italic):
    legacy = dict(id="legacy", clipType="text", track="type", at=1, hold=5,
                  x=120, y=120, width=640, height=180, opacity=.7,
                  text=dict(content="Keep style", fontFamily=family, bold=bold, italic=italic,
                            opaque={"retained": [None, 6.0]}),
                  params={"opaque": [7.0, None], "width": 999})
    base["parent"]["clips"].append(legacy)
    # Actual inspector spreads the captured text for each of its three edits.
    current = base
    for field, value in [("content", "New\n✨"), ("fontSize", 73.0), ("color", "#123456")]:
        text = {**current["parent"]["clips"][-1]["text"], field: value}
        after, plan = apply(current, dict(op="clip.set-text", select="parent/clip:legacy",
                                        args=dict(text=text, elementRef=declared)))
        row = after["parent"]["clips"][-1]
        assert literal(row["text"]) == literal(text)
        assert row["params"]["fontFamily"] == family
        assert row["params"]["bold"] is bold and row["params"]["italic"] is italic
        assert row["params"]["opaque"] == legacy["params"]["opaque"]
        assert row["params"]["width"] == 999  # retained, never a manual box authority
        assert {k: row[k] for k in ("x", "y", "width", "height", "opacity")} == {
            k: legacy[k] for k in ("x", "y", "width", "height", "opacity")}
        undone, undo_plan = apply(after, inverse_command(plan.calls, current, after, "style"))
        assert literal(undone) == literal(current)
        assert literal(apply(undone, inverse_command(undo_plan.calls, after, undone, "undo"))[0]) == literal(after)
        current = after


def test_invalid_text_after_valid_edit_rolls_back_all_calls(base, declared):
    tl = Checkout(copy.deepcopy(base))
    original = literal(tl.document())
    invalid = create(base, declared)
    invalid["args"]["text"]["fontSize"] = -3
    with pytest.raises(commands.TimelineEditError):
        with edit_transaction(tl, "batch", quiet=True, persist=False) as tx:
            commands.execute(tl, dict(op="clip.set", select="occurrence:occ-A/clip:a-rocket", args=dict(opacity=.3)))
            commands.execute(tl, invalid)
    assert literal(tl.document()) == original and tx.plan is None and not tl._pending_calls


@pytest.mark.parametrize("bad", ["text", "renderer", "geometry", "height", "owner", "bold", "italic", "family"])
def test_recorded_edit_stale_dependency_has_no_partial_receipt(base, declared, bad):
    base, _ = apply(base, create(base, declared))
    call = dict(op="clip.set-text", select="parent/clip:clip-9", args=dict(text={"content": "Edit"}, elementRef=declared))
    _, plan = apply(base, call)
    changed = copy.deepcopy(base)
    row = next(r for r in changed["parent"]["clips"] if r["id"] == "clip-9")
    if bad == "text":
        row["text"]["color"] = "#654321"
    if bad == "renderer":
        row["elementRef"]["revision"] = "stale"
    if bad == "geometry":
        row["x"] = 999
    if bad == "height":
        row["height"] = 181
    if bad in {"bold", "italic", "family"}:
        row["params"][{"family": "fontFamily"}.get(bad, bad)] = "changed" if bad == "family" else True
    if bad == "owner":
        row["track"] = "fx"
    tl = Checkout(changed)
    snapshot = literal(tl.document())
    with pytest.raises(commands.TimelineEditError):
        with edit_transaction(tl, "stale", quiet=True, persist=False) as tx:
            commands.execute(tl, plan.calls[0])
    assert literal(tl.document()) == snapshot and tx.plan is None


def test_unbound_renderer_implementation_refuses_atomically(base, declared, monkeypatch):
    from astrid.sdk import invocation
    monkeypatch.setattr(invocation, "_admitted_timeline_element_resources", lambda document, **kw: {
        row["id"]: {"declaration": {"pack_root": ".", "resources": []}} for row in document["clips"]})
    with pytest.raises(commands.TimelineEditError, match="implementation"):
        apply(base, create(base, declared))


def test_acceptance_refusal_emits_no_partial_receipt_or_document(base, declared):
    from test_timeline_commit import CoreService
    from test_timeline_fade_history_receipts import port_for

    invalid = create(base, declared)
    invalid["args"]["geometry"]["height"] = -180
    body = json.loads((FIXTURE.parent / "w13_fade_history.json").read_text())["requests"][0]
    body.update(project_id=base["project_id"], timeline_id=base["timeline_id"],
                request_id="w30-invalid-height", scope={"target": "parent", "complete": [], "projection_version": 1})
    body["change"]["commands"] = [
        dict(op="clip.set", select="occurrence:occ-A/clip:a-rocket", args=dict(opacity=.3)), invalid]
    snapshot = literal(base)
    port = port_for(base, body)
    result = CoreService().commit(body, port)
    assert result["status"] in {"refused", "conflict"}, result
    assert not port.accepts and port.saved is None
    assert result["canonical_document"] is None and not result["change"]["direct_writes"]
    assert literal(base) == snapshot and literal(port.bundles[body["base_revision_id"]]) == snapshot


def test_default_compilation_and_w32_geometry_codec_reach_real_am_type_layout(base, declared, tmp_path):
    from astrid.core.timeline import clip_fields
    from astrid.core.timeline.authoring_bundle import compile_authoring_candidate

    call = create(base, declared)
    call["args"]["text"] = dict(content="Double-click to edit", fontSize=64, color="#ffffff", align="center")
    call["args"]["geometry"] = dict(x=120, y=120, width=640, height=180)
    call["args"]["action"]["end"] = call["args"]["action"]["start"] + 5
    default, _ = apply(base, call)
    moved, _ = apply(default, dict(op="clip.set", select="parent/clip:clip-9", args=dict(
        position_x=200.0, position_y=240, position_width=800, position_height=220)))
    styled, _ = apply(moved, dict(op="clip.set-text", select="parent/clip:clip-9", args=dict(
        text=dict(content="Styled\ntext", fontSize=72, fontFamily="Inter, system-ui, sans-serif",
                  bold=True, italic=True, color="#123456", align="right"), elementRef=declared)))
    styled, _ = apply(styled, dict(op="clip.set", select="parent/clip:clip-9", args=dict(opacity=.7)))
    compiled = []
    for document in (default, moved, styled):
        publication = compile_authoring_candidate(document).publication
        clip = next(r for r in publication["parent_composition"]["clips"] if r["id"] == "clip-9")
        assert not set(clip["params"]) & {"x", "y", "width", "height"}
        for field in ("x", "y", "width", "height"):
            assert clip_fields.read(clip, "position_" + field) == clip[field]
        compiled.append(clip)
    # No new explicit inline typography means the original editorial path.
    compiled.append(dict(id="editorial", clipType="am-type", hold=5, params=dict(text="Caption")))
    input_path = tmp_path / "compiled-text.json"
    input_path.write_text(json.dumps(compiled))
    repo = Path(__file__).parents[2]
    deps = os.environ.get("ASTRID_TEST_REMOTION_DEPENDENCY_ROOT", str(repo / "remotion"))
    result = subprocess.run(["node", str(Path(__file__).with_name("manual_text_render_probe.cjs")),
                             str(repo), deps, str(input_path)], text=True, capture_output=True)
    assert result.returncode == 0, result.stdout + result.stderr
    rendered = json.loads(result.stdout)
    for output, expected in zip(rendered, [(120, 120, 640, 180), (200, 240, 800, 220), (200, 240, 800, 220)]):
        assert tuple(output["style"][k] for k in ("left", "top", "width", "height")) == expected
        assert output["style"]["justifyContent"] == "center"
        assert "opacity" not in output["style"]  # canonical ClipLook owns it once
    assert rendered[0]["style"]["fontFamily"] == "Georgia, serif"
    assert rendered[0]["style"]["fontSize"] == 64
    assert rendered[0]["opacity"] == 1 and rendered[2]["opacity"] == .7
    assert rendered[2]["style"]["fontWeight"] == 700 and rendered[2]["style"]["fontStyle"] == "italic"
    assert rendered[2]["style"]["fontFamily"] == "Inter, system-ui, sans-serif"
    assert rendered[2]["style"]["textAlign"] == "right"
    assert "Gelasio" in rendered[3]["html"] and "Georgia" not in rendered[3]["html"]
    (tmp_path / "rendered-text.json").write_text(result.stdout)
