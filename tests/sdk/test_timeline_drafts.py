"""DRAFTS proofs: controlled clocks and disposable detached timelines only."""
from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from astrid.packs.timeline import cli
from astrid.sdk import timeline_checkout as tc
from astrid.sdk.timeline import commands, history, journal, store
from astrid.sdk.timeline.checkout import Checkout
from astrid.sdk.timeline.drafts import draft_lease, expire_drafts
from astrid.sdk.timeline.identity import retain_cut_reservations

FIXTURE = Path(__file__).parents[1] / "fixtures/timeline_editing/tiny_publishable.json"
NOW = 2_000_000_000
DAYS = 7


@pytest.fixture
def drafts(tmp_path, monkeypatch):
    monkeypatch.setenv("BANODOCO_LOCAL_DATA_ROOT", str(tmp_path / "data"))
    monkeypatch.setenv("ASTRID_DRAFT_EXPIRY_DAYS", str(DAYS))
    bundle = json.loads(FIXTURE.read_text())
    # This fixture also carries historical base payloads. Model a fresh fetch:
    # its pinned base must be the supplied head, including text bindings.
    for shot in bundle["shots"].values():
        shot["base_payload"] = copy.deepcopy(shot["payload"])
        shot["base_internal_timeline"] = copy.deepcopy(shot["internal_timeline"])
    monkeypatch.setattr(tc, "resolve_ids", lambda *a, **k: ("p", "t", "rev-0"))
    monkeypatch.setattr(tc, "fetch_bundle", lambda *a, **k: copy.deepcopy(bundle))
    monkeypatch.setattr("astrid.sdk.timeline.drafts.time.time", lambda: NOW)

    def create(name="main"):
        checkout = Checkout.draft("P", "t", name)
        os.utime(checkout.path, (NOW, NOW))
        return checkout

    return create


def age(path, seconds=DAYS * 86400 + 1):
    os.utime(path, (NOW - seconds, NOW - seconds))


def status(capsys, *argv):
    capsys.readouterr()
    parsed = cli.build_parser(None).parse_args(["status", "t", "--project", "P", *argv])
    assert parsed.handler(parsed) == 0
    output = capsys.readouterr().out
    return output if "--text" in argv else json.loads(output)["data"]


def test_status_expires_clean_draft_with_local_journal_and_markers_only(drafts, tmp_path, capsys):
    checkout = drafts("old")
    path = checkout.path
    # Disposable stand-ins for Runtime-owned film bytes and shared audit history.
    film = tmp_path / "film"
    film.mkdir()
    (film / "head.json").write_bytes(b'{"head":"revision-0"}')
    (film / "history.jsonl").write_bytes(b'{"revision":"revision-0"}\n')
    audit = journal.journal_path("p", "t")
    before = {p: p.read_bytes() for p in [*film.iterdir(), audit]}
    for folder in [store._history_dir(path), store._redo_dir(path)]:
        folder.mkdir()
        (folder / "step.json").write_text('{}')
    local_journal = path.with_suffix(".json.journal.jsonl")
    local_journal.write_text("draft-only journal")
    temporary = path.with_name(f".{path.name}.123.tmp")
    temporary.write_text("partial")
    age(path)
    data = status(capsys)
    assert data["working_copy"] == "absent" and data["drafts"] == []
    assert not path.exists() and not (path.parent / ".current").exists()
    assert not store._history_dir(path).exists() and not store._redo_dir(path).exists()
    assert not local_journal.exists() and not temporary.exists()
    assert all(p.read_bytes() == original for p, original in before.items())


def test_expiry_uses_last_change_and_configured_days(drafts, capsys):
    old, young = drafts("old"), drafts("young")
    age(old.path)
    age(young.path, DAYS * 86400 - 1)
    assert expire_drafts("p", "t") == ["old"]
    assert young.path.exists()
    assert expire_drafts("p", "t", now=NOW + 2) == ["young"]


@pytest.mark.parametrize("days", ["invalid", "nan", "inf", "-1"])
def test_invalid_expiry_configuration_retains_draft(drafts, monkeypatch, days):
    checkout = drafts()
    age(checkout.path)
    monkeypatch.setenv("ASTRID_DRAFT_EXPIRY_DAYS", days)
    assert expire_drafts("p", "t") == []
    assert checkout.path.exists()


def test_leased_draft_is_never_expired_and_leases_are_independent(drafts, capsys):
    checkout = drafts()
    with draft_lease(checkout.path):
        with draft_lease(checkout.path):
            age(checkout.path)
            assert expire_drafts("p", "t", days=0) == []
        age(checkout.path)
        assert expire_drafts("p", "t", days=0) == []
        assert status(capsys)["drafts"][0]["leased"]
    age(checkout.path)
    assert expire_drafts("p", "t") == ["main"]
    assert not checkout.path.with_suffix(".leases").exists()


def test_persistent_lease_marker_is_conservative(drafts):
    checkout = drafts()
    checkout.path.with_suffix(".lease").write_text("owner whose liveness is unknown")
    age(checkout.path)
    assert expire_drafts("p", "t", days=0) == []
    assert checkout.path.exists()


@pytest.mark.parametrize("operation", ["clip", "parent", "tracks", "registry", "payload", "parent-registry", "internal-metadata", "placement-order"])
def test_dirty_draft_never_expires_status_lists_it_and_only_discard_removes_it(drafts, capsys, operation):
    checkout = drafts("unpublished")
    if operation == "clip":
        commands.run(checkout, {"op": "clip.set", "select": "c1.rocket", "args": {"x": 120}})
    else:
        # Persist out-of-band input exactly as the existing load boundary accepts it.
        raw = json.loads(checkout.path.read_text())
        if operation == "parent":
            raw["parent"]["config"]["custom_setting"] = "unpublished"
        elif operation == "payload":
            raw["shots"]["A"]["payload"]["name"] = "unpublished"
        elif operation == "placement-order":
            raw["placements"].reverse()
        elif operation == "parent-registry":
            raw["parent"]["registry"] = {"assets": {"unpublished": {"media_id": "sha256:" + "9" * 64}}}
        elif operation == "internal-metadata":
            raw["shots"]["A"]["internal_timeline"]["custom_metadata"] = "unpublished"
        else:
            internal = raw["shots"]["A"]["internal_timeline"]
            if operation == "tracks":
                internal["tracks"].append({"id": "unpublished", "kind": "visual"})
            else:
                internal["registry"]["assets"]["unpublished"] = {"media_id": "sha256:" + "9" * 64}
        checkout.path.write_text(json.dumps(raw))
    before = checkout.path.read_bytes()
    age(checkout.path, 365 * 86400)
    drafts("selected-clean")  # another selected draft cannot hide dirty work
    age(checkout.path, 365 * 86400)
    data = status(capsys)
    row = next(row for row in data["drafts"] if row["draft"] == "unpublished")
    assert row["change_state"] == "changed"
    assert 'working copy "unpublished" · changed' in status(capsys, "--draft", "unpublished", "--text")
    assert 'working copy "unpublished" · changed' in status(capsys, "--text")
    assert checkout.path.read_bytes() == before
    parsed = cli.build_parser(None).parse_args(["discard", "t", "--project", "P", "--draft", "unpublished"])
    assert parsed.handler(parsed) == 0
    assert not checkout.path.exists()
    assert not store._history_dir(checkout.path).exists()
    assert not store._redo_dir(checkout.path).exists()


@pytest.mark.parametrize("damage", ["missing-base", "malformed"])
def test_unverifiable_draft_is_retained_and_listed(drafts, capsys, damage):
    checkout = drafts()
    if damage == "missing-base":
        raw = json.loads(checkout.path.read_text())
        raw.pop("base_parent_payload")
        checkout.path.write_text(json.dumps(raw))
    else:
        checkout.path.write_text("incomplete JSON")
    age(checkout.path)
    assert expire_drafts("p", "t") == []
    from astrid.sdk.timeline.drafts import list_drafts
    assert list_drafts("p", "t")[0]["change_state"] == "unknown"
    assert checkout.path.exists()


def test_pending_replay_never_expires(drafts):
    checkout = drafts()
    history.pending_path(checkout.path).write_text("unresolved recovery")
    age(checkout.path)
    assert expire_drafts("p", "t") == []
    assert checkout.path.exists()


def test_expiry_skips_busy_writer_without_waiting(drafts):
    checkout = drafts()
    age(checkout.path)
    with store._file_lock(checkout.path):
        assert expire_drafts("p", "t") == []
    assert checkout.path.exists()


# A real terminated process leaves only durable files; no Python unwind/rollback.
CRASH_SCRIPT = r'''
import json, os, sys
from pathlib import Path
from astrid.sdk.timeline import history, store
from astrid.sdk.timeline.checkout import Checkout
path, direction, window = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
checkout = Checkout.load(path)
source = store._history_dir(path) if direction == "undo" else store._redo_dir(path)
original_write = history.atomic_write
original_unlink = Path.unlink

def write(target, text):
    if target == path and window == "removed-before-target":
        # Exercise the opposite ordering with the durable recovery record in place.
        sorted(source.glob("*.json"))[-1].unlink()
        os._exit(73)
    original_write(target, text)
    if window == "target-before-remove" and target == path:
        os._exit(73)
    if window == "destination-before-target" and target.parent != path.parent and target.suffix == ".json":
        os._exit(73)

def unlink(target, *args, **kwargs):
    if window == "audit-before-marker" and target == history.pending_path(path):
        os._exit(73)
    original_unlink(target, *args, **kwargs)
    if window == "removed-after-target" and target.parent == source and target.suffix == ".json":
        os._exit(73)

history.atomic_write = write
Path.unlink = unlink
getattr(checkout, direction)(2)
raise AssertionError("crash point was not reached")
'''


@pytest.mark.parametrize("direction", ["undo", "redo"])
@pytest.mark.parametrize("window", ["target-before-remove", "removed-before-target", "destination-before-target",
                                     "removed-after-target", "audit-before-marker"])
def test_process_crash_recovers_exact_multi_step_state_and_both_stacks(drafts, direction, window, record_property):
    checkout = drafts()
    states = [checkout.document()]
    for x in (20, 30, 40):
        commands.run(checkout, {"op": "clip.set", "select": "c1.rocket", "args": {"x": x}})
        states.append(checkout.document())
    # Current history retains cut reservations, including legacy live ids.
    states = [retain_cut_reservations(state, checkout.document()) for state in states]
    if direction == "redo":
        checkout.undo(3)
    records_before = len(journal.read(journal.journal_path("p", "t")))
    with subprocess.Popen([sys.executable, "-c", CRASH_SCRIPT, str(checkout.path), direction, window],
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as process:
        stdout, stderr = process.communicate(timeout=20)
        record_property("crash_pid", process.pid)
        record_property("crash_exit", process.returncode)
    assert process.returncode == 73, stdout + stderr
    assert history.pending_path(checkout.path).is_file()
    recovered = Checkout.load(checkout.path)
    assert recovered.document() == states[1 if direction == "undo" else 2]
    assert not history.pending_path(checkout.path).exists()
    records = journal.read(journal.journal_path("p", "t"))
    assert len(records) == records_before + 1
    assert records[-1].kind == direction and records[-1].calls[0]["op"] == "timeline." + direction
    assert journal.verify(journal.journal_path("p", "t")) == []
    # Continue through every remaining entry in both directions, checking no
    # duplicate/no-op step and no missing state after the interrupted batch.
    if direction == "undo":
        recovered.undo()
        assert recovered.document() == states[0]
    else:
        recovered.redo()
        assert recovered.document() == states[3]
        recovered.undo(3)
        assert recovered.document() == states[0]
    for expected in states[1:]:
        recovered.redo()
        assert recovered.document() == expected
    for expected in reversed(states[:-1]):
        recovered.undo()
        assert recovered.document() == expected


@pytest.mark.parametrize("direction", ["undo", "redo"])
@pytest.mark.parametrize("window", ["destination-before-target", "removed-before-target"])
def test_crash_history_retains_allocated_ids_and_opaque_fields(drafts, direction, window):
    checkout = drafts()
    checkout.bundle["parent"]["config"]["future-owner"] = {"nested": [None, {"x": "opaque"}]}
    checkout.save(quiet=True)
    before = checkout.document()
    commands.run(checkout, {"op": "cut.split", "select": "c1", "args": {"on": 2.0}})
    commands.run(checkout, {"op": "clip.set", "select": "c1.rocket", "args": {"x": 40}})
    after = checkout.document()
    expected_before = retain_cut_reservations(before, after)
    if direction == "redo":
        checkout.undo(2)
    result = subprocess.run([sys.executable, "-c", CRASH_SCRIPT, str(checkout.path), direction, window],
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 73, result.stdout + result.stderr
    recovered = Checkout.load(checkout.path)
    assert recovered.document() == (expected_before if direction == "undo" else after)
    assert recovered._next_cut_id("c1") == "c1b"
    if direction == "undo":
        recovered.redo(2)
        assert recovered.document() == after
    else:
        recovered.undo(2)
        assert recovered.document() == expected_before
    assert recovered._next_cut_id("c1") == "c1b"
    assert journal.verify(journal.journal_path("p", "t")) == []


@pytest.mark.parametrize("direction", ["undo", "redo"])
@pytest.mark.parametrize("problem", ["busy", "stale", "unsaved"])
def test_history_refusal_preserves_document_stacks_and_audit(drafts, monkeypatch, direction, problem):
    from astrid.sdk.timeline._common import TimelineEditError

    checkout = drafts()
    commands.run(checkout, {"op": "clip.set", "select": "c1.rocket", "args": {"x": 20}})
    if direction == "redo":
        checkout.undo()
    if problem == "stale":
        other = Checkout.load(checkout.path)
        commands.run(other, {"op": "clip.set", "select": "c1.rocket", "args": {"y": 30}})
    if problem == "unsaved":
        with checkout.step("unsaved move"):
            checkout.clip("c1.rocket").set(x=40)
    def snapshot():
        return {str(p): p.read_bytes() for p in checkout.path.parent.rglob("*")
                if p.is_file() and not p.name.endswith(".lock")}
    before = snapshot()
    if problem == "busy":
        monkeypatch.setenv(store.LOCK_WAIT_ENV, "0")
        with store._file_lock(checkout.path), pytest.raises(TimelineEditError, match="lock|busy"):
            getattr(checkout, direction)()
    else:
        with pytest.raises(TimelineEditError, match="another writer|unsaved"):
            getattr(checkout, direction)()
    assert snapshot() == before
    assert not history.pending_path(checkout.path).exists()


@pytest.mark.parametrize("direction", ["undo", "redo"])
def test_invalid_snapshot_refuses_before_any_transition_write(drafts, direction):
    checkout = drafts()
    for x in (20, 30):
        commands.run(checkout, {"op": "clip.set", "select": "c1.rocket", "args": {"x": x}})
    if direction == "redo":
        checkout.undo(2)
    source = store._redo_dir(checkout.path) if direction == "redo" else store._history_dir(checkout.path)
    # The newest step parses first; the older corrupt step must not leave a
    # partially moved stack or working copy when planning the batch fails.
    sorted(source.glob("*.json"))[0].write_text("corrupt snapshot")
    before = {str(p): p.read_bytes() for p in checkout.path.parent.rglob("*") if p.is_file()}
    with pytest.raises((ValueError, KeyError)):
        getattr(checkout, direction)(2)
    assert {str(p): p.read_bytes() for p in checkout.path.parent.rglob("*") if p.is_file()} == before
    assert not history.pending_path(checkout.path).exists()
