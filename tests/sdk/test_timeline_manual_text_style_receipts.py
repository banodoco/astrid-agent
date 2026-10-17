"""Actual inline style values at the shared acceptance/receipt-only history boundary."""
import copy
import json

import pytest
from test_timeline_commit import CoreService
from test_timeline_fade_history_receipts import accept, literal, port_for, reverse_receipt
from test_timeline_manual_text import FIXTURE, base, declared, rows  # noqa: F401
from test_timeline_saved_publish import synthetic_scene_answers  # noqa: F401

pytestmark = pytest.mark.usefixtures("synthetic_scene_answers", "declared")


@pytest.mark.parametrize("bold,italic,sid,family", [
    (False, False, "parent/clip:legacy", "Georgia, serif"),
    (True, False, "occurrence:occ-B/clip:legacy", "Inter, system-ui, sans-serif"),
    (False, True, "parent/clip:legacy", "Inter, system-ui, sans-serif"),
    (True, True, "occurrence:occ-B/clip:legacy", "Georgia, serif"),
])
def test_full_style_acceptance_direct_receipt_exact_undo_redo_and_stale_refusal(
        base, declared, bold, italic, sid, family, tmp_path):  # noqa: F811
    legacy = dict(id="legacy", clipType="text", track="type", at=.5, hold=.5,
                  x=120.0, y=-0.0, width=640, height=180.0, opacity=.7,
                  text=dict(content="Original", fontSize=64, color="#ffffff", fontFamily=family,
                            bold=bold, italic=italic, align="center", opaque={"nested": [None, 6.0]}),
                  params={"opaque": {"nested": [7.0, None]}}, app={"opaque": [None, 8.0]})
    rows(base, sid).append(legacy)
    # Each inspector control spreads the captured whole text. Its final content,
    # size and color values still contain every other style and opaque sibling.
    text = {**legacy["text"], "content": "Edited\n✨", "fontSize": 71.0, "color": "#12ab34"}
    body = json.loads((FIXTURE.parent / "w13_fade_history.json").read_text())["requests"][0]
    body.update(project_id=base["project_id"], timeline_id=base["timeline_id"],
                request_id="style-forward", scope={"target": "parent", "complete": [], "projection_version": 1})
    body["change"]["commands"] = [dict(op="clip.set-text", select=sid,
                                      args=dict(text=text, elementRef=declared))]
    forward = accept(base, body)
    row = next(r for r in rows(forward["canonical_document"], sid) if r["id"] == "legacy")
    assert literal(row["text"]) == literal(text)
    assert row["params"]["bold"] is bold and row["params"]["italic"] is italic
    assert row["params"]["fontFamily"] == family and row["params"]["opaque"] == legacy["params"]["opaque"]
    assert literal({k: row[k] for k in ("x", "y", "width", "height", "opacity", "app")}) == literal({
        k: legacy[k] for k in ("x", "y", "width", "height", "opacity", "app")})
    writes = forward["change"]["direct_writes"]
    assert {w["id"] for w in writes} == {sid}
    assert {"text", "params.bold", "params.italic", "params.fontFamily"} <= {w["path"] for w in writes}
    undo = copy.deepcopy(body)
    undo.update(request_id="style-undo", session_sequence=2,
                base_revision_id=forward["accepted_revision_id"])
    undo["change"]["commands"] = [reverse_receipt(forward)]
    undone = accept(forward["canonical_document"], undo)
    assert literal(undone["canonical_document"]) == literal(base)
    redo = copy.deepcopy(undo)
    redo.update(request_id="style-redo", session_sequence=3,
                base_revision_id=undone["accepted_revision_id"])
    redo["change"]["commands"] = [reverse_receipt(undone)]
    redone = accept(undone["canonical_document"], redo)
    assert literal(redone["canonical_document"]) == literal(forward["canonical_document"])
    stale = copy.deepcopy(forward["canonical_document"])
    next(r for r in rows(stale, sid) if r["id"] == "legacy")["params"]["bold"] = not bold
    snapshot = literal(stale)
    port = port_for(stale, undo)
    refused = CoreService().commit(undo, port)
    assert refused["status"] in {"conflict", "refused"}
    assert refused["canonical_document"] is None and not refused["change"]["direct_writes"]
    assert not port.accepts and port.saved is None and literal(stale) == snapshot
    (tmp_path / "accepted-style-history.json").write_text(json.dumps(dict(
        before=base, requests=[body, undo, redo], results=[forward, undone, redone], refused=refused), indent=2)+"\n")
