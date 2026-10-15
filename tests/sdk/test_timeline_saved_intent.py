"""Exact saved intent recovery, using only disposable normal draft storage."""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from astrid.sdk.timeline import commands, journal, store
from astrid.sdk.timeline.checkout import Checkout
from astrid.sdk.timeline.saved_intent import SavedIntentError, read_saved_calls


FIXTURE = Path(__file__).resolve().parents[1] / "fixtures/timeline_editing/tiny_publishable.json"


@pytest.fixture
def draft(tmp_path, monkeypatch):
    monkeypatch.setenv("BANODOCO_LOCAL_DATA_ROOT", str(tmp_path / "data"))
    monkeypatch.setattr(Checkout, "SAY_SAVED", False)
    monkeypatch.setattr(store, "resolve_ids", lambda *a, **kw: ("p", "t", "rev-0"))
    monkeypatch.setattr(store, "fetch_bundle", lambda *a, **kw: json.loads(FIXTURE.read_text()))
    return Checkout.draft("p", "t")


def edit(tl, **params):
    return commands.run(tl, dict(op="clip.set", select="c1.rocket", args=params))


def rows():
    return [row.to_json() for row in journal.read(journal.journal_path("p", "t"))]


def rewrite(values):
    """Change semantic evidence while preserving a valid full hash chain."""
    path = journal.journal_path("p", "t")
    path.unlink()
    for row in values:
        fields = {key: value for key, value in row.items() if key not in {"hash", "prev", "seq", "v"}}
        journal.append(path, **fields)
    assert journal.verify(path) == []


def test_two_saved_opcalls_reload_exact_commands_and_current_digest(draft, monkeypatch):
    first = edit(draft, x=90)
    second = edit(draft, y=180)
    original = [call for row in rows() if row["kind"] == "save" for call in row["calls"]]
    assert original == [first.calls[0].to_json(), second.calls[0].to_json()]
    loaded = Checkout.load(draft.path)
    before = copy.deepcopy(loaded.bundle)
    disk = Path(draft.path).read_bytes()
    monkeypatch.setattr(store, "fetch_bundle", lambda *a, **kw: pytest.fail("no mutable head read"))
    assert read_saved_calls(loaded) == tuple(original)
    assert rows()[-1]["after"] == journal.digest(disk.decode())
    assert loaded.bundle == before and Path(draft.path).read_bytes() == disk
    recovered = read_saved_calls(loaded)
    recovered[0]["args"]["x"] = -1
    assert read_saved_calls(loaded) == tuple(original)  # independent serialized values


def test_true_unchanged_base_returns_empty_with_persisted_provenance(draft):
    anchor = rows()[0]
    assert anchor["evidence"][0]["document"]["base_parent"] == draft.bundle["base_parent"]
    assert read_saved_calls(Checkout.load(draft.path)) == ()


def test_additive_base_evidence_keeps_existing_record_schema_and_log_reader(draft):
    from jsonschema import Draft202012Validator
    from referencing import Registry, Resource
    from referencing.jsonschema import DRAFT202012

    edit(draft, x=90)
    root = Path(__file__).resolve().parents[2] / "schemas/change-contract"
    schemas = [json.loads(path.read_text()) for path in root.glob("*.schema.json")]
    registry = Registry().with_resources(
        (schema["$id"], Resource.from_contents(schema, default_specification=DRAFT202012))
        for schema in schemas)
    validator = Draft202012Validator(json.loads((root / "journal-record.schema.json").read_text()), registry=registry)
    for row in rows():
        validator.validate(row)
    base = rows()[0]["evidence"][0]["document"]
    assert base["shots"]["A"]["internal_timeline"] != draft.bundle["shots"]["A"]["internal_timeline"]
    assert base["base_parent"] == draft.bundle["base_parent"]
    assert journal.query(journal.read(journal.journal_path("p", "t")), op="clip.set")[-1].calls
    assert read_saved_calls(Checkout.load(draft.path)) == tuple(rows()[-1]["calls"])


def test_multiple_calls_in_one_saved_transaction_preserve_order(draft):
    with draft.edit("two calls", quiet=True):
        draft.clip("c1.rocket").set(x=81)
        draft.clip("c1.rocket").set(x=82)
    saved = rows()[-1]["calls"]
    assert len(saved) == 2
    assert read_saved_calls(Checkout.load(draft.path)) == tuple(saved)


@pytest.mark.parametrize("damage", ["missing", "empty", "corrupt", "gap", "digest", "missing-digest",
    "commands", "wrong-project", "wrong-timeline", "wrong-draft", "ambiguous-draft", "duplicate-id",
    "duplicate-checkout", "legacy-base", "base-digest", "base-identity", "base-payload",
    "altered-command", "unrecorded-command"])
def test_refuses_unproven_journal_even_when_rehashed(draft, damage):
    edit(draft, x=90)
    edit(draft, y=180)
    path = journal.journal_path("p", "t")
    values = rows()
    if damage == "missing":
        path.unlink()
    elif damage == "empty":
        path.write_text("")
    elif damage == "corrupt":
        path.write_text(path.read_text().replace('"x":90', '"x":91'))
    else:
        if damage == "gap":
            values.pop(1)
        elif damage == "digest":
            values[-1]["before"] = "sha256:" + "0" * 64
        elif damage == "missing-digest":
            values[-1]["after"] = None
        elif damage == "commands":
            values[-1]["calls"] = []
        elif damage == "wrong-project":
            values[-1]["project"] = "other"
        elif damage == "wrong-timeline":
            values[-1]["timeline"] = "other"
        elif damage == "wrong-draft":
            values[-1]["draft"] = "other"
        elif damage == "ambiguous-draft":
            values[-1]["draft"] = None
        elif damage == "duplicate-id":
            values[-1]["id"] = values[0]["id"]
        elif damage == "duplicate-checkout":
            values.append({**values[0], "id": "a" * 32})
        elif damage == "legacy-base":
            values[0]["evidence"] = []
        elif damage == "base-digest":
            values[0]["evidence"][0]["document"]["parent"]["config"]["unrecorded"] = True
        elif damage == "base-identity":
            values[0]["evidence"][0]["document"]["base_parent"] = {}
        elif damage == "base-payload":
            del values[0]["evidence"][0]["document"]["base_parent_payload"]
        elif damage == "altered-command":
            values[-1]["calls"][0]["args"]["y"] = 999
        elif damage == "unrecorded-command":
            del values[-1]["calls"][0]["expect"]
        rewrite(values)
    with pytest.raises(SavedIntentError) as error:
        read_saved_calls(Checkout.load(draft.path))
    assert error.value.code == "unverified_saved_intent"


@pytest.mark.parametrize("damage", ["content", "helper", "whitespace", "line-endings", "base"])
def test_manual_current_file_edits_are_refused(draft, damage):
    edit(draft, x=90)
    path = Path(draft.path)
    value = json.loads(path.read_text())
    if damage == "content":
        value["shots"]["A"]["internal_timeline"]["clips"][1]["params"]["x"] = 999
    elif damage == "helper":
        value["shots"]["A"]["internal_timeline"]["clips"][1]["_timeline"]["start"] += 1
    elif damage == "base":
        value["base_parent"]["revision_id"] = "another-base"
    if damage == "whitespace":
        path.write_text(path.read_text() + "\n")
    elif damage == "line-endings":
        path.write_bytes(path.read_bytes().replace(b"\n", b"\r\n"))
    else:
        from astrid.sdk.timeline.doc import _serialize
        path.write_text(_serialize(value))
    with pytest.raises(SavedIntentError):
        read_saved_calls(Checkout.load(path))


def test_material_manual_save_with_no_calls_is_refused(draft):
    draft.bundle["parent"]["config"]["manual"] = True
    draft.save(quiet=True)
    assert rows()[-1]["calls"] == []
    with pytest.raises(SavedIntentError, match="no commands"):
        read_saved_calls(Checkout.load(draft.path))


@pytest.mark.parametrize("redo", [False, True])
def test_undo_redo_history_cannot_be_reported_as_original_commands(draft, redo):
    edit(draft, x=90)
    draft.undo()
    if redo:
        draft.redo()
    with pytest.raises(SavedIntentError, match="undo"):
        read_saved_calls(Checkout.load(draft.path))


def test_other_drafts_do_not_leak_and_other_timelines_are_not_read(draft):
    edit(draft, x=90)
    original = rows()[-1]["calls"]
    other = Checkout.draft("p", "t", "other")
    edit(other, y=180)
    journal.append(journal.journal_path("p", "elsewhere"), project="p", timeline="elsewhere", draft="main")
    assert read_saved_calls(Checkout.load(draft.path)) == tuple(original)
    assert read_saved_calls(Checkout.load(other.path)) == tuple(rows()[-1]["calls"])


def test_full_journal_is_verified_including_unselected_drafts(draft):
    Checkout.draft("p", "t", "other")
    path = journal.journal_path("p", "t")
    text = path.read_text()
    path.write_text(text.replace('"draft":"other"', '"draft":"damaged"'))
    with pytest.raises(SavedIntentError, match="damaged"):
        read_saved_calls(Checkout.load(draft.path))


def test_a_copy_outside_the_exact_draft_path_has_no_identity(draft, tmp_path):
    copy_path = tmp_path / "main.json"
    copy_path.write_bytes(Path(draft.path).read_bytes())
    with pytest.raises(SavedIntentError, match="canonical draft path"):
        read_saved_calls(Checkout.load(copy_path))


def test_audit_previews_and_failures_do_not_become_saved_intent(draft):
    with draft.edit("preview", dry_run=True, quiet=True):
        draft.clip("c1.rocket").set(x=91)
    with pytest.raises(RuntimeError):
        with draft.edit("rollback", quiet=True):
            draft.clip("c1.rocket").set(x=92)
            raise RuntimeError("rollback")
    edit(draft, x=90)
    assert read_saved_calls(Checkout.load(draft.path)) == tuple(rows()[-1]["calls"])


def test_stale_or_unsaved_handle_is_refused(draft):
    stale = Checkout.load(draft.path)
    edit(draft, x=90)
    with pytest.raises(SavedIntentError, match="stale"):
        read_saved_calls(stale)
    draft.clip("c1.rocket").set(y=180)
    with pytest.raises(SavedIntentError, match="unsaved"):
        read_saved_calls(draft)


def test_unrepresented_concurrent_force_merge_is_refused(draft):
    stale = Checkout.load(draft.path)
    edit(draft, x=90)
    stale.clip("c1.rocket").set(x=99)
    stale.save(force=True, quiet=True)
    with pytest.raises(SavedIntentError):
        read_saved_calls(Checkout.load(draft.path))


def test_ambient_file_adapters_are_refused_before_execution(draft, monkeypatch):
    edit(draft, x=90)
    values = rows()
    values[-1]["calls"][0].update(op="timeline.adopt", select=None, resolved=[], args={"timeline": "elsewhere"})
    rewrite(values)
    monkeypatch.setattr(store, "fetch_bundle", lambda *a, **kw: pytest.fail("must not fetch"))
    with pytest.raises(SavedIntentError, match="scoped"):
        read_saved_calls(Checkout.load(draft.path))


@pytest.mark.parametrize("location", ["file", "journal", "overflow"])
def test_nonfinite_json_cannot_be_verified_as_saved_intent(draft, location):
    if location in {"file", "overflow"}:
        path = Path(draft.path)
        value = json.loads(path.read_text())
        value["opaque"] = float("nan")
        text = json.dumps(value)
        path.write_text(text.replace("NaN", "1e9999") if location == "overflow" else text)
    else:
        values = rows()
        values[0]["evidence"][0]["document"]["opaque"] = float("inf")
        rewrite(values)
    with pytest.raises(SavedIntentError, match="finite JSON"):
        read_saved_calls(Checkout.load(draft.path))


def test_duplicate_json_row_fields_refuse_even_if_existing_hash_verifier_passes(draft):
    path = journal.journal_path("p", "t")
    path.write_text(path.read_text().replace('"project":"p"', '"project":"p","project":"p"'))
    assert journal.verify(path) == []
    with pytest.raises(SavedIntentError, match="ambiguous duplicate"):
        read_saved_calls(Checkout.load(draft.path))
