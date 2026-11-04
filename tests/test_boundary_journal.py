import json
import os
import uuid
from pathlib import Path

import pytest

from astrid.installer import boundary_journal as bj


@pytest.fixture
def setup(tmp_path):
    directory = tmp_path / "journal"
    directory.mkdir()
    identity = dict(operation_id=str(uuid.uuid4()), r0_root=str(tmp_path / "r0"),
                    r1_root=str(tmp_path / "r1"), realm="realm-A")
    return directory, identity


def journal(setup):
    directory, identity = setup
    return bj.BoundaryJournal(directory, **identity)


def test_intents_are_uncertain_until_explicit_effect_and_rollback_is_recorded(setup):
    j = journal(setup)
    j.create()
    j.intent("stop")
    assert journal(setup).load()["status"] == {"stop": "uncertain"}
    j.effect("stop", "succeeded")
    j.intent("adopt_receipt")
    assert journal(setup).load()["status"] == {"stop": "succeeded", "adopt_receipt": "uncertain"}
    with pytest.raises(bj.JournalError, match="contract unavailable"):
        j.effect("adopt_receipt", "succeeded")
    with pytest.raises(bj.JournalError):
        j.intent("selection")
    j.intent("rollback")
    assert j.load()["status"]["adopt_receipt"] == "uncertain"
    assert j.load()["status"]["rollback"] == "uncertain"
    j.effect("rollback", "succeeded")
    assert j.load()["status"]["rollback"] == "succeeded"
    with pytest.raises(bj.JournalError):
        j.intent("stop")


@pytest.mark.parametrize("outcome", ["failed", "uncertain"])
def test_failed_or_uncertain_effect_cannot_be_reclassified(setup, outcome):
    j = journal(setup)
    j.create()
    j.intent("stop")
    j.effect("stop", outcome)
    with pytest.raises(bj.JournalError):
        j.effect("stop", "succeeded")
    with pytest.raises(bj.JournalError):
        j.intent("adopt_receipt")
    j.intent("rollback")
    assert j.load()["status"]["stop"] == outcome


def test_order_and_duplicate_creation_refused(setup):
    j = journal(setup)
    j.create()
    with pytest.raises(bj.JournalError):
        j.create()
    with pytest.raises(bj.JournalError):
        j.effect("stop", "succeeded")
    with pytest.raises(bj.JournalError):
        j.intent("registration")
    j.intent("stop")
    with pytest.raises(bj.JournalError):
        j.intent("stop")
    assert len(j.load()["events"]) == 1


@pytest.mark.parametrize("field,value", [("operation_id", str(uuid.uuid4())), ("r0_root", "other"),
                                         ("r1_root", "other"), ("realm", "realm-B")])
def test_identity_binding(setup, field, value, tmp_path):
    directory, identity = setup
    journal(setup).create()
    other = dict(identity)
    other[field] = str(tmp_path / value) if field.endswith("root") else value
    with pytest.raises(bj.JournalError):
        bj.BoundaryJournal(directory, **other).load()
    with pytest.raises(bj.JournalError):
        bj.BoundaryJournal(directory, **other).create()


def test_placement_and_root_validation(setup, tmp_path):
    _, identity = setup
    root = Path(identity["r0_root"])
    root.mkdir()
    inside = root / "journal"
    inside.mkdir()
    with pytest.raises(bj.JournalError):
        bj.BoundaryJournal(inside, **identity)
    alias = tmp_path / "alias"
    alias.symlink_to(root, target_is_directory=True)
    with pytest.raises(bj.JournalError):
        bj.BoundaryJournal(alias / "journal", **identity)
    for replacement in (str(root / "child"), str(root), "relative"):
        with pytest.raises(bj.JournalError):
            bj.BoundaryJournal(setup[0], **dict(identity, r1_root=replacement))
    with pytest.raises(bj.JournalError):
        bj.BoundaryJournal(setup[0], **dict(identity, operation_id="not-a-uuid"))
    assert list(root.iterdir()) == [inside]


@pytest.mark.parametrize("bad", [b'{', b'{"version":1,"version":1}', b'null', b'[]', b'\xff'])
def test_corrupt_records_refused(setup, bad):
    j = journal(setup)
    j.create()
    (setup[0] / "0000.json").write_bytes(bad)
    with pytest.raises(bj.JournalError):
        j.load()


def test_unknown_version_fields_and_invalid_history_refused(setup):
    j = journal(setup)
    j.create()
    j.intent("stop")
    path = setup[0] / "0001.json"
    original = json.loads(path.read_text())
    for change in ({"version": 2}, {"unknown": 1}, {"events": [{"step": "selection", "kind": "intent"}]}):
        path.write_text(json.dumps(dict(original, **change)))
        with pytest.raises(bj.JournalError):
            j.load()
    path.write_text(json.dumps(original))
    (setup[0] / "0000.json").unlink()
    with pytest.raises(bj.JournalError):
        j.load()


def test_foreign_and_symlink_files_refused(setup, tmp_path):
    j = journal(setup)
    j.create()
    (setup[0] / "unexpected").write_text("foreign")
    with pytest.raises(bj.JournalError):
        j.load()
    (setup[0] / "unexpected").unlink()
    path = setup[0] / "0000.json"
    copy = tmp_path / "copy"
    copy.write_bytes(path.read_bytes())
    path.unlink()
    path.symlink_to(copy)
    with pytest.raises(bj.JournalError):
        j.load()


def test_interrupted_before_link_refuses_pending_file(setup, monkeypatch):
    j = journal(setup)
    j.create()
    def fail_link(*args):
        raise OSError("interrupted")
    monkeypatch.setattr(bj.os, "link", fail_link)
    with pytest.raises(bj.JournalError):
        j.intent("stop")
    assert not (setup[0] / "0001.json").exists()
    with pytest.raises(bj.JournalError):
        j.load()


def test_interrupted_after_link_refuses_pending_file(setup, monkeypatch):
    j = journal(setup)
    j.create()
    def fail_unlink(*args):
        raise OSError("interrupted")
    monkeypatch.setattr(bj.os, "unlink", fail_unlink)
    with pytest.raises(bj.JournalError):
        j.intent("stop")
    assert (setup[0] / "0001.json").exists()
    with pytest.raises(bj.JournalError):
        j.load()


def test_directory_sync_failure_retried_on_reload(setup, monkeypatch):
    j = journal(setup)
    j.create()
    original = bj._sync_dir
    calls = 0
    def fail_commit(directory):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("fsync failed")
        original(directory)
    monkeypatch.setattr(bj, "_sync_dir", fail_commit)
    with pytest.raises(bj.JournalError):
        j.intent("stop")
    assert j.load()["status"] == {"stop": "uncertain"}


def test_competing_commit_never_overwrites(setup):
    j = journal(setup)
    j.create()
    j.intent("stop")
    before = (setup[0] / "0001.json").read_bytes()
    with pytest.raises(bj.JournalError):
        j._commit([{"step": "rollback", "kind": "intent"}])
    assert (setup[0] / "0001.json").read_bytes() == before
    with pytest.raises(bj.JournalError):
        j.load()  # a losing writer leaves an ambiguous temporary file


def test_unsupported_platform_refuses_initialization(setup, monkeypatch):
    monkeypatch.setattr(bj.sys, "platform", "darwin")
    with pytest.raises(bj.JournalError, match="only on Linux"):
        journal(setup)
    assert list(setup[0].iterdir()) == []


def test_file_fsync_failure_leaves_no_published_intent(setup, monkeypatch):
    j = journal(setup)
    j.create()
    original = bj.os.fsync
    def fail_file(fd):
        if bj.stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError("file fsync failed")
        original(fd)
    monkeypatch.setattr(bj.os, "fsync", fail_file)
    with pytest.raises(bj.JournalError):
        j.intent("stop")
    assert not (setup[0] / "0001.json").exists()
    with pytest.raises(bj.JournalError):
        j.load()
