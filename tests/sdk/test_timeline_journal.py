"""The edit journal (API.md §G.1): records per save, undo, redo, checkout, discard, edit and publish; query; verify."""
from __future__ import annotations

import copy
import hashlib
import json
import sys
from pathlib import Path

import pytest

from astrid.packs.timeline import cli
from astrid.sdk import timeline_checkout as tc
from astrid.sdk.timeline import journal
from astrid.sdk.timeline import store as _store
from astrid.sdk.timeline.checkout import Checkout

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "timeline_editing" / "tiny_publishable.json"


def tiny() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def python_source(monkeypatch):
    """No CLI verb is running (the parser sets the flag; each test that needs it sets it again)."""
    monkeypatch.setattr(cli, "_IN_VERB", False)
    monkeypatch.delenv("ASTRID_ACTOR", raising=False)
    monkeypatch.delenv("ASTRID_SESSION", raising=False)


@pytest.fixture
def draft(tmp_path, monkeypatch):
    """A working copy of the tiny fixture under a temp data root. The seams are fakes; nothing real is touched."""
    monkeypatch.setenv("ASTRID_RUNTIME_DATA_ROOT", str(tmp_path / "data"))
    monkeypatch.setattr(tc, "resolve_ids", lambda project, timeline, client=None: ("p", "t", "rev-0"))
    monkeypatch.setattr(tc, "fetch_bundle", lambda project, timeline, revision_id=None, client=None: copy.deepcopy(tiny()))
    monkeypatch.setattr(_store, "resolve_ids", lambda project, timeline, client=None: ("p", "t", "rev-0"))
    monkeypatch.setattr(_store, "fetch_bundle", lambda project, timeline, revision_id=None, client=None: copy.deepcopy(tiny()))
    return Checkout.draft("P", "t")  # writes the working copy: the first record is "checkout"


def _path(tl: Checkout) -> Path:
    return Path(tl.path)


def _text(tl: Checkout) -> str:
    return _path(tl).read_text(encoding="utf-8")


def _journal_file() -> Path:
    return journal.journal_path("p", "t")


def _records() -> list[journal.JournalRecord]:
    return journal.read(_journal_file())


def _digest(text: str) -> str:
    return journal.digest(text)


def test_an_api_save_writes_one_save_record_with_digests_and_the_step_name(draft):
    before = _text(draft)
    draft.clip("c1.rocket").set(x=90)
    draft.save()
    rec = _records()[-1]
    assert rec.kind == "save" and rec.outcome == "changed"
    assert rec.before == _digest(before) and rec.after == _digest(_text(draft))
    steps = sorted(_store._history_dir(_path(draft)).glob("*.json"))
    assert rec.undo_step == steps[-1].name
    assert draft.clip("c1.rocket").id in rec.clips
    assert rec.source == {"via": "python", "detail": rec.label}
    assert [r.kind for r in _records()] == ["checkout", "save"]


def test_a_cli_edit_writes_one_record_via_cli(draft, monkeypatch):
    cli._IN_VERB = True  # what the parser sets when a `timelines` verb runs
    argv = ["edit", "t", "--project", "P", "--clip", "c1.rocket", "--set", "x=5"]
    monkeypatch.setattr(sys, "argv", ["timelines", *argv])
    parsed = cli.build_parser(None).parse_args(argv)
    assert parsed.handler(parsed) == 0
    saves = [r for r in _records() if r.kind == "save"]
    assert len(saves) == 1
    assert saves[0].source["via"] == "cli" and "edit" in saves[0].source["detail"]


def test_a_sheet_apply_is_recorded_via_sheet(draft, monkeypatch):
    monkeypatch.setattr(cli, "_IN_VERB", True)
    with draft.edit("apply demo.sheet"):
        draft.clip("c1.rocket").set(x=7)
    rec = _records()[-1]
    assert rec.kind == "save" and rec.label == "apply demo.sheet"
    assert rec.source["via"] == "sheet"


def test_undo_and_redo_are_recorded_with_both_digests(draft):
    with draft.edit("move"):
        draft.clip("c1.rocket").set(x=90)
    edited = _text(draft)
    draft.undo()
    undone = _text(draft)
    rec = _records()[-1]
    assert (rec.kind, rec.outcome, rec.label) == ("undo", "changed", "move")
    assert rec.before == _digest(edited) and rec.after == _digest(undone)
    draft.redo()
    rec = _records()[-1]
    assert (rec.kind, rec.label) == ("redo", "move")
    assert rec.before == _digest(undone) and rec.after == _digest(_text(draft))


def test_a_fresh_checkout_and_a_discard_are_recorded(tmp_path, monkeypatch):
    monkeypatch.setenv("ASTRID_RUNTIME_DATA_ROOT", str(tmp_path / "data"))
    monkeypatch.setattr(_store, "resolve_ids", lambda project, timeline, client=None: ("p", "t", "rev-0"))
    monkeypatch.setattr(_store, "fetch_bundle", lambda project, timeline, revision_id=None, client=None: copy.deepcopy(tiny()))
    tl = Checkout.draft("P", "t")
    rec = _records()[0]
    assert (rec.kind, rec.outcome, rec.label) == ("checkout", "changed", "checkout")
    assert rec.after == _digest(_text(tl))
    text = _text(tl)
    tl.discard()
    rec = _records()[-1]
    assert (rec.kind, rec.outcome) == ("discard", "changed")
    assert rec.before == _digest(text)  # written before the file was deleted
    assert not Path(tl.path).exists()
    assert [r.kind for r in _records()] == ["checkout", "discard"]


def test_a_transaction_with_no_change_is_an_edit_record_with_equal_digests(draft):
    with draft.edit("nothing", reason="just looking"):
        pass
    rec = _records()[-1]
    assert (rec.kind, rec.outcome, rec.label) == ("edit", "no-change", "nothing")
    assert rec.before == rec.after and rec.reason == "just looking"


def test_a_dry_run_is_an_edit_record_and_saves_nothing(draft):
    text = _text(draft)
    with draft.edit("try it", dry_run=True) as tx:
        draft.clip("c1.rocket").set(x=90)
    rec = _records()[-1]
    assert (rec.kind, rec.outcome) == ("edit", "dry-run")
    assert rec.before == tx.plan.digests["before"] and rec.after == tx.plan.digests["after"] and rec.after != rec.before
    assert _text(draft) == text
    assert [r.kind for r in _records()] == ["checkout", "edit"]


def test_an_exception_is_an_edit_record_rolled_back_with_its_message(draft):
    text = _text(draft)
    with pytest.raises(RuntimeError, match="boom"):
        with draft.edit("doomed"):
            draft.clip("c1.rocket").set(x=90)
            raise RuntimeError("boom\nsecond line")
    rec = _records()[-1]
    assert (rec.kind, rec.outcome, rec.error) == ("edit", "rolled-back", "boom")
    assert _text(draft) == text


def test_publish_records_the_message_as_reason_and_the_new_head_as_evidence(draft, monkeypatch):
    with draft.edit("move"):
        draft.clip("c1.rocket").set(x=90)
    published: dict = {}

    def fake_publish(document, key, client=None, force=False):
        published["doc"] = copy.deepcopy(document)
        return {"old_head": "rev-0", "new_head": "rev-1", "structure": "changed"}

    def fake_fetch(project, timeline, revision_id=None, client=None):
        head = copy.deepcopy(published["doc"])
        for key in ("source_mapping", "base_placements", "base_parent_payload"):
            head.pop(key, None)
        for shot in head["shots"].values():
            shot["base_internal_timeline"] = copy.deepcopy(shot.get("internal_timeline"))
            shot["base_payload"] = copy.deepcopy(shot.get("payload"))
        head["base_parent"] = {"revision_id": "rev-1"}
        return head

    monkeypatch.setattr(draft, "_commit_document", lambda document, message, **kw:
        fake_publish(document, kw.get("idempotency_key"), client=kw.get("client")))
    monkeypatch.setattr(_store, "fetch_bundle", fake_fetch)
    monkeypatch.setattr(draft, "_publish_narration", lambda key, client=None: {})
    draft.publish("move the rocket")
    rec = _records()[-1]
    assert (rec.kind, rec.outcome, rec.label) == ("publish", "changed", "publish")
    assert rec.reason == "move the rocket"
    assert rec.evidence == ({"kind": "revision", "id": "rev-1"},)


def test_the_actor_comes_from_astrid_actor_and_astrid_session(draft, monkeypatch):
    monkeypatch.setenv("ASTRID_ACTOR", "agent:t1")
    monkeypatch.setenv("ASTRID_SESSION", "sess-9")
    with draft.edit("by an agent"):
        draft.clip("c1.rocket").set(x=90)
    rec = _records()[-1]
    assert rec.actor == {"kind": "agent", "id": "t1", "session": "sess-9"}


def test_a_bare_actor_value_is_a_human_and_no_value_is_the_os_user(monkeypatch):
    monkeypatch.setenv("ASTRID_ACTOR", "hannah")
    assert journal.current_actor() == {"kind": "human", "id": "hannah", "session": None}
    monkeypatch.delenv("ASTRID_ACTOR")
    assert journal.current_actor()["kind"] == "human"
    assert journal.current_actor()["id"]


def test_query_filters_by_seq_and_by_time(draft, monkeypatch):
    monkeypatch.setenv("ASTRID_ACTOR", "agent:t1")
    with draft.edit("one"):
        draft.clip("c1.rocket").set(x=90)
    with draft.edit("two"):
        draft.clip("c1.rocket").set(y=44)
    with draft.edit("nothing"):
        pass
    records = _records()
    assert [r.seq for r in records] == [1, 2, 3, 4]
    assert [r.seq for r in journal.query(records, since=3)] == [3, 4]
    assert [r.seq for r in journal.query(records, since="3")] == [3, 4]
    assert [r.seq for r in journal.query(records, since=records[2].at)] == [3, 4]
    with pytest.raises(ValueError):
        journal.query(records, since="yesterday-ish")


def test_query_filters_by_actor_cut_clip_op_and_outcome(draft, monkeypatch):
    with draft.edit("one"):
        draft.clip("c1.rocket").set(x=90)
    with draft.edit("nothing"):
        pass
    records = _records()
    edit_rec = records[-1]
    save_rec = records[-2]
    assert save_rec.cuts, "the clip's cut is recorded"
    assert [r.seq for r in journal.query(records, outcome="no-change")] == [edit_rec.seq]
    assert [r.seq for r in journal.query(records, cut=save_rec.cuts[0])] == [save_rec.seq]
    assert [r.seq for r in journal.query(records, clip=save_rec.clips[0])] == [save_rec.seq]
    assert [r.seq for r in journal.query(records, actor="human:" + edit_rec.actor["id"])] == [r.seq for r in records]
    assert [r.seq for r in journal.query(records, actor="agent:nobody")] == []
    assert [r.seq for r in journal.query(records, op="nothing")] == [edit_rec.seq]


def _add(path: Path, label: str) -> None:
    journal.append(path, project="p", timeline="t", draft="main", actor={"kind": "human", "id": "h", "session": None},
                   source={"via": "python", "detail": label}, kind="save", outcome="changed", label=label)


def test_verify_passes_on_an_intact_chain_and_links_each_record(tmp_path):
    path = tmp_path / "journal.jsonl"
    for label in ("a", "b", "c", "d"):
        _add(path, label)
    assert journal.verify(path) == []
    records = journal.read(path)
    assert [r.seq for r in records] == [1, 2, 3, 4]
    assert records[0].prev is None
    assert all(records[i].prev == records[i - 1].hash for i in range(1, 4))
    assert records[0].hash.startswith("sha256:") and len(records[0].at) == 24  # ms precision, Z


def test_verify_detects_a_hand_edited_line(tmp_path):
    path = tmp_path / "journal.jsonl"
    for label in ("a", "b", "c"):
        _add(path, label)
    lines = path.read_text(encoding="utf-8").splitlines()
    lines[1] = lines[1].replace('"label":"b"', '"label":"B"')
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    problems = journal.verify(path)
    assert problems and any("seq 2" in p and "hash" in p for p in problems)


def test_verify_detects_a_removed_line(tmp_path):
    path = tmp_path / "journal.jsonl"
    for label in ("a", "b", "c"):
        _add(path, label)
    lines = path.read_text(encoding="utf-8").splitlines()
    path.write_text("\n".join([lines[0], lines[2]]) + "\n", encoding="utf-8")
    problems = journal.verify(path)
    assert any("expected" in p for p in problems)
    assert any("prev" in p for p in problems)


def test_an_in_memory_checkout_writes_no_journal(tmp_path, monkeypatch):
    monkeypatch.setenv("ASTRID_RUNTIME_DATA_ROOT", str(tmp_path / "data"))
    tl = Checkout(copy.deepcopy(tiny()))
    with tl.edit("in memory"):
        tl.clip("c1.rocket").set(x=90)
    assert tl.path is None
    assert not list((tmp_path / "data").rglob("journal.jsonl"))


def test_the_source_is_the_argv_for_the_cli_and_capped_at_200_characters(monkeypatch):
    monkeypatch.setattr(cli, "_IN_VERB", True)
    monkeypatch.setattr(sys, "argv", ["timelines", "edit", "t", "--set", "x=" + "9" * 300])
    source = journal.current_source("edit t")
    assert source["via"] == "cli" and len(source["detail"]) == 200
    assert source["detail"].startswith("edit t --set")
    monkeypatch.setattr(cli, "_IN_VERB", False)
    assert journal.current_source("a label") == {"via": "python", "detail": "a label"}


def test_a_record_and_the_plan_share_one_document_digest(draft):
    with draft.edit("move the rocket", dry_run=True) as tx:
        draft.clip("c1.rocket").set(x=77)
    rec = _records()[-1]
    assert rec.kind == "edit" and rec.outcome == "dry-run"
    assert rec.before == tx.plan.digests["before"] and rec.after == tx.plan.digests["after"]
    raw = "sha256:" + hashlib.sha256(_text(draft).encode("utf-8")).hexdigest()
    assert rec.before != raw  # a document digest, never the file text


def test_a_damaged_last_line_does_not_fail_an_api_save_and_the_checkout_says_so(draft):
    path = _journal_file()
    lines = path.read_text(encoding="utf-8").splitlines()
    lines[-1] = lines[-1][:-7] + "XX"  # the last record no longer parses
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    before = _text(draft)
    draft.clip("c1.rocket").set(x=90)
    draft.save()
    assert _text(draft) != before  # the save succeeded
    assert any(n.startswith("journal not written:") and "timelines log" in n and "--verify" in n for n in draft.notes)
    assert path.read_text(encoding="utf-8").splitlines() == lines  # a damaged chain is never extended
