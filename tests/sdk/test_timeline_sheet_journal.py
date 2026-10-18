"""Sheet creation retains authored anchors in the journal and exact history."""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from astrid.sdk import timeline_checkout as tc
from astrid.sdk.timeline import journal, store
from astrid.sdk.timeline.apply_file import apply_file
from astrid.sdk.timeline_checkout import Checkout, TimelineEditError
from astrid.sdk.timeline_sheet import render_sheet


@pytest.fixture
def draft(tmp_path, monkeypatch):
    fixture = Path(__file__).resolve().parents[1] / "fixtures/timeline_editing/tiny.json"
    bundle = json.loads(fixture.read_text(encoding="utf-8"))
    namespace = bundle["parent"]["config"].setdefault("app", {}).setdefault("astrid", {})
    namespace["cut_id_high_water"] = {"number": 2, "suffixes": {}}
    namespace["future-journal-control"] = {"keep": [None, {"value": 17}]}
    bundle["shots"]["A"]["internal_timeline"]["clips"][1]["future-clip-control"] = {"keep": True}
    monkeypatch.setenv("BANODOCO_LOCAL_DATA_ROOT", str(tmp_path / "data"))
    monkeypatch.setattr(tc, "resolve_ids", lambda *args, **kwargs: ("p", "t", "rev-0"))
    monkeypatch.setattr(tc, "fetch_bundle", lambda *args, **kwargs: copy.deepcopy(bundle))
    return Checkout.draft("P", "t")


@pytest.mark.parametrize("on", ['"now"', None])
def test_sheet_add_journals_its_authored_start_and_restores_exact_history(tmp_path, draft, on):
    tl = draft
    before = tl.document()
    records_before = journal.read(journal.journal_path("p", "t"))
    history = store._history_dir(Path(tl.path))
    steps_before = set(history.glob("*.json"))
    relation = f" on {on}" if on is not None else ""
    sheet = render_sheet(tl, cuts=["c2"])
    sheet += f"         sprite pin sprite R{relation} for 1.5s x=600 y=300\n"
    source = tmp_path / "add.sheet"
    source.write_text(sheet, encoding="utf-8")

    assert apply_file(tl, source)
    after = tl.document()
    pin = tl.clip("c2.pin")
    assert pin.start == pytest.approx(tl.time(on) if on is not None else tl.cut("c2").start)
    assert pin.duration == pytest.approx(1.5)
    assert (pin.get("x"), pin.get("y"), pin.asset) == (600, 300, "R")
    assert after["parent"] == before["parent"]
    assert after["shots"]["A"] == before["shots"]["A"]
    records = journal.read(journal.journal_path("p", "t"))
    assert len(records) == len(records_before) + 1
    assert set(history.glob("*.json")) - steps_before == {history / records[-1].undo_step}
    adds = [call for call in records[-1].calls if call["op"] == "timeline.add"]
    assert len(adds) == 1
    add = adds[0]
    assert add["args"]["at"] == (on if on is not None else tl.cut("c2").start)
    assert add["args"]["anchor"] is (on is not None)
    assert add["bindings"] == ({'"now"': "word:s2:1"} if on is not None else {})

    assert len(tl.undo()) == 1
    assert tl.document() == before
    assert len(tl.redo()) == 1
    assert tl.document() == after
    records = journal.read(journal.journal_path("p", "t"))
    assert [record.kind for record in records] == [record.kind for record in records_before] + ["save", "undo", "redo"]
    assert records[-2].before == records[-1].after == records[-3].after
    assert records[-2].after == records[-1].before == records[-3].before


@pytest.mark.parametrize("invalid", [False, True])
def test_sheet_property_mapping_preserves_authored_values_and_atomic_refusal(tmp_path, draft, invalid):
    from astrid.sdk.timeline_sheet import SheetError

    before = draft.document()
    path = Path(draft.path)
    journal_path = journal.journal_path("p", "t")
    document_bytes, journal_bytes = path.read_bytes(), journal_path.read_bytes()
    history = store._history_dir(path)
    history_before = {p.name: p.read_bytes() for p in history.glob("*.json")}
    source = tmp_path / "properties.sheet"
    # The rocket row is valid; the later c2 plate's fractional zoom is refused.
    # No earlier setter may survive the enclosing file transaction.
    sheet = render_sheet(draft).replace("x=60", "x=180 y=300")
    if invalid:
        sheet = sheet.replace("snap-plate  Q", "snap-plate  Q zoom=1.5")
    source.write_text(sheet, encoding="utf-8")
    if invalid:
        with pytest.raises(SheetError):
            apply_file(draft, source)
        assert draft.document() == before
        assert path.read_bytes() == document_bytes
        assert journal_path.read_bytes() == journal_bytes
        assert {p.name: p.read_bytes() for p in history.glob("*.json")} == history_before
        return

    assert apply_file(draft, source)
    after = draft.document()
    assert (draft.clip("c1.rocket").get("x"), draft.clip("c1.rocket").get("y")) == (180, 300)
    assert after["parent"] == before["parent"]
    rocket = next(clip for clip in after["shots"]["A"]["internal_timeline"]["clips"] if clip["id"] == "a-rocket")
    assert rocket["future-clip-control"] == {"keep": True}
    record = journal.read(journal_path)[-1]
    assert [call["args"] for call in record.calls if call["op"] == "clip.set"] == [{"x": 180, "y": 300}]
    assert len(draft.undo()) == 1
    assert draft.document() == before
    assert len(draft.redo()) == 1
    assert draft.document() == after


def _history_files(folder):
    return {path.name: path.read_bytes() for path in folder.glob("*.json")}


def test_apply_file_single_opcall_noop_audits_but_declarative_sheets_stay_silent(tmp_path, draft):
    from astrid.sdk.timeline import journal, store

    tl = draft
    document_path = Path(tl.path)
    journal_path = journal.journal_path("p", "t")
    history_dir = store._history_dir(document_path)
    before_document = document_path.read_bytes()
    before_records = journal.read(journal_path)
    before_history = _history_files(history_dir)
    source = tmp_path / "noop.sheet"

    for contents in ("", "# comment only\n", render_sheet(tl)):
        source.write_text(contents, encoding="utf-8")
        assert apply_file(tl, source) == []
        assert journal.read(journal_path) == before_records
        assert document_path.read_bytes() == before_document

    value = tl.clip("c1.rocket").get("x")
    call = {"op": "clip.set", "select": "c1.rocket", "args": {"x": value}}
    source.write_text("edit " + json.dumps(call, separators=(",", ":")) + "\n", encoding="utf-8")
    assert apply_file(tl, source) == []

    records = journal.read(journal_path)
    assert len(records) == len(before_records) + 1
    record = records[-1]
    assert record.kind == "edit" and record.outcome == "no-change"
    assert record.label == "clip.set"
    assert len(record.calls) == 1 and record.calls[0]["op"] == "clip.set"
    assert record.before == record.after and record.before
    assert record.undo_step is None
    assert document_path.read_bytes() == before_document
    assert _history_files(history_dir) == before_history
    assert journal.verify(journal_path) == []

    changed_source = tmp_path / "changed.sheet"
    changed_call = {"op": "clip.set", "select": "c1.rocket", "args": {"x": 120}}
    changed_source.write_text("edit " + json.dumps(changed_call, separators=(",", ":")) + "\n", encoding="utf-8")
    assert apply_file(tl, changed_source)
    changed_records = journal.read(journal_path)
    assert len(changed_records) == len(before_records) + 2
    changed_record = changed_records[-1]
    assert changed_record.outcome == "changed"
    assert changed_record.label == 'clip.set a-rocket {"x":120}'
    assert changed_record.undo_step is not None
    assert len(_history_files(history_dir)) == len(before_history) + 1


def test_apply_file_dynamic_operation_noop_matches_sdk_audit(tmp_path, draft, monkeypatch):
    from astrid.sdk.timeline.commands import run as run_command
    from astrid.sdk.timeline.tools import operation_discovery_scope, resolve_operation

    repo = Path(__file__).resolve().parents[2]
    discovery = {
        "project_root": repo / "out/extension-harness",
        "extra_pack_roots": (str(repo / "examples/packs"),),
    }
    identity = "timeline.operation/beat_tools.fit_to_beats"
    with operation_discovery_scope(**discovery):
        operation = resolve_operation(identity)
    assert operation.implementation_digest == "dfd107e08a5ff42948cc24b83d72f09c42c67cec8f8a8c5cfef2ef417a0e125b"
    call = {
        "op": "timeline.operation",
        "select": None,
        "args": {
            "identity": identity,
            "implementation_digest": operation.implementation_digest,
            "inputs": {"clips": [], "beats": []},
        },
    }
    label = f"{identity}@{operation.implementation_digest}"

    def exercise(tl, route, source_path):
        document_path = Path(tl.path)
        journal_path = journal.journal_path("p", "t")
        history_dir = store._history_dir(document_path)
        redo_dir = store._redo_dir(document_path)
        before_document = document_path.read_bytes()
        before_records = journal.read(journal_path)
        before_history = _history_files(history_dir)
        before_redo = _history_files(redo_dir)
        if route == "sheet":
            source_path.write_text("edit " + json.dumps(call, separators=(",", ":")) + "\n", encoding="utf-8")
            with operation_discovery_scope(**discovery):
                assert apply_file(tl, source_path) == []
        else:
            with operation_discovery_scope(**discovery):
                assert not run_command(tl, call).changed

        records = journal.read(journal_path)
        assert len(records) == len(before_records) + 1
        record = records[-1]
        assert record.kind == "edit" and record.outcome == "no-change"
        assert record.label == label
        assert record.before == record.after and record.before
        assert record.undo_step is None
        assert document_path.read_bytes() == before_document
        assert _history_files(history_dir) == before_history
        assert _history_files(redo_dir) == before_redo
        assert journal.verify(journal_path) == []
        return record.to_json()

    sheet_record = exercise(draft, "sheet", tmp_path / "dynamic-noop.sheet")
    monkeypatch.setenv("BANODOCO_LOCAL_DATA_ROOT", str(tmp_path / "sdk-data"))
    sdk_draft = Checkout.draft("P", "t")
    sdk_record = exercise(sdk_draft, "sdk", tmp_path / "unused.sheet")
    variable = {"source", "actor", "at", "id", "seq", "prev", "hash"}
    comparable = [{key: value for key, value in record.items() if key not in variable}
                  for record in (sheet_record, sdk_record)]
    assert comparable[0] == comparable[1]


@pytest.mark.parametrize("suffix", [".jsonl", ".py"])
def test_apply_file_non_sheet_noop_formats_remain_silent(tmp_path, draft, suffix):
    from astrid.sdk.timeline import journal, store

    tl = draft
    document_path = Path(tl.path)
    journal_path = journal.journal_path("p", "t")
    history_dir = store._history_dir(document_path)
    before_document = document_path.read_bytes()
    before_journal = journal_path.read_bytes()
    before_history = _history_files(history_dir)
    value = tl.clip("c1.rocket").get("x")
    source = tmp_path / ("noop" + suffix)
    if suffix == ".jsonl":
        contents = json.dumps({"op": "clip.set", "select": "c1.rocket", "args": {"x": value}}) + "\n"
    else:
        contents = f'tl.clip("c1.rocket").set(x={value!r})\n'
    source.write_text(contents, encoding="utf-8")

    assert apply_file(tl, source) == []
    assert document_path.read_bytes() == before_document
    assert journal_path.read_bytes() == before_journal
    assert _history_files(history_dir) == before_history


@pytest.mark.parametrize("kind", ["multiple", "alias", "cancelled", "restructure", "invalid", "image", "storage"])
def test_apply_file_noneligible_or_refused_sheet_commands_stay_silent(tmp_path, draft, monkeypatch, kind):
    tl = draft
    value = tl.clip("c1.rocket").get("x")
    noop = {"op": "clip.set", "select": "c1.rocket", "args": {"x": value}}
    expected_error = None
    if kind == "multiple":
        calls = [noop, noop]
    elif kind == "alias":
        calls = [{"op": "clip.hold-for", "select": "c1.rocket",
                  "args": {"seconds": tl.clip("c1.rocket").duration}}]
    elif kind == "cancelled":
        calls = [{"op": "clip.nudge", "select": "occurrence:occ-A/clip:a-rocket",
                  "args": {"frames": 1, "relation_choices": [
                      {"target": "occurrence:occ-A/clip:a-rocket", "edge": "start", "choice": "cancel"}
                  ]}}]
    elif kind == "restructure":
        from astrid.sdk.timeline import chapter_storage

        tl.bundle["storage_policy"] = "chapters"
        tl._accept_bundle()  # Admit this test's fixture setup before exercising the file adapter.
        tl.save(quiet=True)
        monkeypatch.setattr(chapter_storage, "resolved_chapters", lambda _bundle: [])
        monkeypatch.setattr(chapter_storage, "split_chapter_storage", lambda candidate, chapters: candidate)
        calls = [{"op": "timeline.restructure", "select": None,
                  "args": {"expected_head": "rev-0", "policy": "chapters"}}]
    elif kind == "invalid":
        calls = [{"op": "clip.set", "select": "missing.clip", "args": {"x": 1}}]
        expected_error = "clip"
    elif kind == "image":
        calls = [{"op": "timeline.restore", "select": None,
                  "args": {"revision_id": "rev-0", "content_digest": "sha256:" + "0" * 64}}]
        expected_error = "authenticated commit service"
    else:
        calls = [{"op": "timeline.save", "select": None, "args": {}}]
        expected_error = "storage boundary"

    document_path = Path(tl.path)
    journal_path = journal.journal_path("p", "t")
    history_dir = store._history_dir(document_path)
    redo_dir = store._redo_dir(document_path)
    before_document = document_path.read_bytes()
    before_journal = journal_path.read_bytes()
    before_history = _history_files(history_dir)
    before_redo = _history_files(redo_dir)
    source = tmp_path / f"{kind}.sheet"
    source.write_text("\n".join("edit " + json.dumps(call, separators=(",", ":")) for call in calls) + "\n",
                      encoding="utf-8")

    if expected_error:
        with pytest.raises(TimelineEditError, match=expected_error):
            apply_file(tl, source)
    else:
        assert apply_file(tl, source) == []
    assert document_path.read_bytes() == before_document
    assert journal_path.read_bytes() == before_journal
    assert _history_files(history_dir) == before_history
    assert _history_files(redo_dir) == before_redo


def test_apply_file_noop_then_failure_preserves_document_and_journal_bytes(tmp_path, draft):
    from astrid.sdk.timeline.commands import CommandError

    tl = draft
    document_path = Path(tl.path)
    journal_path = journal.journal_path("p", "t")
    history_dir = store._history_dir(document_path)
    before_document = document_path.read_bytes()
    before_journal = journal_path.read_bytes()
    before_history = _history_files(history_dir)
    noop = {"op": "clip.set", "select": "c1.rocket", "args": {"x": tl.clip("c1.rocket").get("x")}}
    invalid = {"op": "not.registered", "select": None, "args": {}}
    source = tmp_path / "noop-then-failure.sheet"
    source.write_text("\n".join("edit " + json.dumps(call, separators=(",", ":"))
                                 for call in (noop, invalid)) + "\n", encoding="utf-8")

    with pytest.raises(CommandError, match="unknown edit operation"):
        apply_file(tl, source)
    assert document_path.read_bytes() == before_document
    assert journal_path.read_bytes() == before_journal
    assert _history_files(history_dir) == before_history
