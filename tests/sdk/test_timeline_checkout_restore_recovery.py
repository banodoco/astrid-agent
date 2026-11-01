"""Interrupted local restores recover through reload and public undo/redo."""
import os
from pathlib import Path

import pytest

from astrid.sdk.timeline import store
from astrid.sdk.timeline._common import TimelineEditError
from astrid.sdk.timeline_checkout import Checkout
from tests.sdk import test_timeline_transaction

draft = test_timeline_transaction.draft


@pytest.mark.parametrize("operation", ["undo", "redo"])
def test_reverse_stack_is_recoverable_after_post_replace_interruption(draft, monkeypatch, operation):
    for value in (40, 99):
        draft.clip('c1.rocket').set(x=value)
        draft.save(quiet=True)
    if operation == "redo":
        draft.undo(2)
    source_folder = (store._history_dir if operation == "undo" else store._redo_dir)(draft.path)
    reverse_folder = (store._redo_dir if operation == "undo" else store._history_dir)(draft.path)
    inverse = "redo" if operation == "undo" else "undo"
    before = draft.document()
    sources = {p: p.read_bytes() for p in source_folder.glob("*.json")}
    replace = os.replace

    def interrupt_reverse_promotion(source, target):
        if Path(target).parent == reverse_folder:
            raise OSError('interrupted after target replace, before reverse promotion')
        return replace(source, target)

    with monkeypatch.context() as fault:
        fault.setattr(os, 'replace', interrupt_reverse_promotion)
        with pytest.raises(OSError, match='interrupted after target replace'):
            getattr(draft, operation)(2)

    assert {p: p.read_bytes() for p in source_folder.glob('*.json')} == sources
    assert len(list(reverse_folder.glob('.*.tmp'))) == 2
    reloaded = Checkout.load(draft.path)
    # The draft replacement succeeded; its previous state must remain recoverable
    # through the public inverse after reopening, before another save clears redo.
    assert reloaded.document() != before
    assert len(getattr(reloaded, inverse)(2)) == 2
    assert reloaded.document() == before


@pytest.mark.parametrize("operation", ["undo", "redo"])
@pytest.mark.parametrize("interruption", ["target", "second-promotion", "source-removal"])
def test_restore_recovers_each_committed_stage(draft, monkeypatch, operation, interruption):
    for value in (40, 99):
        draft.clip("c1.rocket").set(x=value)
        draft.save(quiet=True)
    if operation == "redo":
        draft.undo(2)
    before = draft.path.read_bytes()
    source = (store._history_dir if operation == "undo" else store._redo_dir)(draft.path)
    reverse = (store._redo_dir if operation == "undo" else store._history_dir)(draft.path)
    sources = {p: p.read_bytes() for p in source.glob("*.json")}
    replace, unlink = os.replace, Path.unlink
    promotions = 0
    removals = 0

    def interrupt_replace(old, new):
        nonlocal promotions
        if Path(new).parent == reverse:
            promotions += 1
            if interruption == "second-promotion" and promotions == 2:
                raise KeyboardInterrupt("restore interrupted")
        replace(old, new)
        if interruption == "target" and Path(new) == draft.path:
            raise KeyboardInterrupt("restore interrupted")

    def interrupt_unlink(path, *args, **kwargs):
        nonlocal removals
        if path in sources:
            removals += 1
            if interruption == "source-removal" and removals == 2:
                raise KeyboardInterrupt("restore interrupted")
        return unlink(path, *args, **kwargs)

    with monkeypatch.context() as fault:
        fault.setattr(os, "replace", interrupt_replace)
        fault.setattr(Path, "unlink", interrupt_unlink)
        with pytest.raises(KeyboardInterrupt, match="restore interrupted"):
            getattr(draft, operation)(2)
    restored = draft.path.read_bytes()
    assert restored != before
    if interruption != "source-removal":
        assert {p: p.read_bytes() for p in source.glob("*.json")} == sources
    reloaded = Checkout.load(draft.path)
    assert reloaded.path.read_bytes() == restored
    assert not list(source.glob("*.json"))
    assert len(list(reverse.glob("*.json"))) == 2
    assert not list(reverse.glob(".*.tmp"))
    assert not store._restore_marker(draft.path).exists()
    inverse = "redo" if operation == "undo" else "undo"
    assert len(getattr(reloaded, inverse)(2)) == 2
    assert reloaded.path.read_bytes() == before
    assert len(getattr(reloaded, operation)(2)) == 2
    assert reloaded.path.read_bytes() == restored


@pytest.mark.parametrize("operation", ["undo", "redo"])
@pytest.mark.parametrize("next_action", ["inverse", "save"])
def test_next_mutation_reconciles_before_reading_or_clearing_stacks(draft, monkeypatch, operation, next_action):
    for value in (40, 99):
        draft.clip("c1.rocket").set(x=value)
        draft.save(quiet=True)
    if operation == "redo":
        draft.undo(2)
    before = draft.path.read_bytes()
    reverse = (store._redo_dir if operation == "undo" else store._history_dir)(draft.path)
    replace = os.replace

    def interrupt(old, new):
        if Path(new).parent == reverse:
            raise OSError("promotion interrupted")
        return replace(old, new)

    with monkeypatch.context() as fault:
        fault.setattr(os, "replace", interrupt)
        with pytest.raises(OSError, match="promotion interrupted"):
            getattr(draft, operation)(2)
    restored = draft.path.read_bytes()
    if next_action == "inverse":
        inverse = "redo" if operation == "undo" else "undo"
        assert len(getattr(draft, inverse)(2)) == 2
        assert draft.path.read_bytes() == before
    else:
        draft.clip("c1.rocket").set(x=120)
        draft.save(quiet=True, force=True)
        saved = draft.path.read_bytes()
        assert not list(store._redo_dir(draft.path).glob("*.json"))
        assert len(draft.undo()) == 1
        assert draft.path.read_bytes() == restored
        if operation == "undo":
            with pytest.raises(TimelineEditError, match="no unsent draft edit"):
                draft.undo()
        assert len(draft.redo()) == 1
        assert draft.path.read_bytes() == saved
    assert not store._restore_marker(draft.path).exists()


@pytest.mark.parametrize("operation", ["undo", "redo"])
def test_retired_draft_history_cannot_be_revived_by_a_pending_move(draft, monkeypatch, operation):
    for value in (40, 99):
        draft.clip("c1.rocket").set(x=value)
        draft.save(quiet=True)
    if operation == "redo":
        draft.undo(2)
    reverse = (store._redo_dir if operation == "undo" else store._history_dir)(draft.path)
    replace = os.replace

    def interrupt(old, new):
        if Path(new).parent == reverse:
            raise OSError("promotion interrupted")
        return replace(old, new)

    with monkeypatch.context() as fault:
        fault.setattr(os, "replace", interrupt)
        with pytest.raises(OSError, match="promotion interrupted"):
            getattr(draft, operation)(2)
    assert store._restore_marker(draft.path).is_file()
    draft._retire_draft_history()
    reloaded = Checkout.load(draft.path)
    for action in (reloaded.undo, reloaded.redo):
        with pytest.raises(TimelineEditError, match="no unsent draft edit"):
            action()
    before = reloaded.path.read_bytes()
    reloaded.clip("c1.rocket").set(x=120)
    reloaded.save(quiet=True)
    assert len(reloaded.undo()) == 1
    assert reloaded.path.read_bytes() == before
    assert len(reloaded.redo()) == 1
    assert reloaded.clip("c1.rocket").get("x") == 120


@pytest.mark.parametrize("operation", ["undo", "redo"])
@pytest.mark.parametrize("failure", ["first-stage", "second-stage", "marker-write", "marker-replace",
                                     "target-write", "target-replace"])
def test_failed_restore_preparation_keeps_both_stacks(draft, monkeypatch, operation, failure):
    for value in (40, 99):
        draft.clip("c1.rocket").set(x=value)
        draft.save(quiet=True)
    draft.undo()
    folders = [store._history_dir(draft.path), store._redo_dir(draft.path)]
    # Two steps in the selected stack, and an existing opposite stack to preserve.
    source = (store._history_dir if operation == "undo" else store._redo_dir)(draft.path)
    state = next(source.glob("*.json"))
    label, text = store._read_step(state)
    store._write_step(source, label, text)
    before = draft.path.read_bytes()
    stacks = {p: p.read_bytes() for folder in folders for p in folder.glob("*.json")}
    reverse = (store._redo_dir if operation == "undo" else store._history_dir)(draft.path)
    marker = store._restore_marker(draft.path)
    target_tmp = draft.path.with_name(f".{draft.path.name}.{os.getpid()}.tmp")
    write, replace = Path.write_text, os.replace
    stages = 0

    def interrupt_write(path, *args, **kwargs):
        nonlocal stages
        if path.parent == reverse and path.name.endswith(".tmp"):
            stages += 1
            if failure == f"{('first' if stages == 1 else 'second')}-stage":
                write(path, "partial", encoding="utf-8")
                raise OSError("preparation interrupted")
        if failure == "marker-write" and path == marker.with_suffix(".tmp"):
            write(path, "partial", encoding="utf-8")
            raise OSError("preparation interrupted")
        if failure == "target-write" and path == target_tmp:
            write(path, "partial", encoding="utf-8")
            raise OSError("preparation interrupted")
        return write(path, *args, **kwargs)

    def interrupt_replace(old, new):
        if failure == "marker-replace" and Path(new) == marker:
            raise OSError("preparation interrupted")
        if failure == "target-replace" and Path(new) == draft.path:
            raise OSError("preparation interrupted")
        return replace(old, new)

    with monkeypatch.context() as fault:
        fault.setattr(Path, "write_text", interrupt_write)
        fault.setattr(os, "replace", interrupt_replace)
        with pytest.raises(OSError, match="preparation interrupted"):
            getattr(draft, operation)(2)
    assert draft.path.read_bytes() == before
    assert {p: p.read_bytes() for folder in folders for p in folder.glob("*.json")} == stacks
    assert not [p for folder in folders for p in folder.glob(".*.tmp")]
    assert not list(draft.path.parent.glob(".*.tmp"))
    assert not marker.exists()
    reloaded = Checkout.load(draft.path)
    assert len(getattr(reloaded, operation)(2)) == 2
    inverse = "redo" if operation == "undo" else "undo"
    assert len(getattr(reloaded, inverse)(2)) == 2
    assert reloaded.path.read_bytes() == before


@pytest.mark.parametrize("operation", ["undo", "redo"])
@pytest.mark.parametrize("shape", ["unchanged", "ABA"])
@pytest.mark.parametrize("replaced", [False, True])
def test_equal_value_restore_uses_replacement_identity(draft, monkeypatch, operation, shape, replaced):
    original = draft.path.read_text(encoding="utf-8")
    folder = (store._history_dir if operation == "undo" else store._redo_dir)(draft.path)
    if shape == "ABA":
        draft.clip("c1.rocket").set(x=40)
        draft.save(quiet=True)
        middle = draft.path.read_text(encoding="utf-8")
        draft = Checkout.load(draft.path)
        # Build an A -> B -> A chain of local states, including exact equal bytes.
        draft.path.write_text(original, encoding="utf-8")
        if operation == "undo":
            store._write_step(folder, "back to A", middle)
        else:
            store._write_step(folder, "A", original)
            store._write_step(folder, "B", middle)
    else:
        store._write_step(folder, "unchanged first", original)
        store._write_step(folder, "unchanged second", original)
    reverse = (store._redo_dir if operation == "undo" else store._history_dir)(draft.path)
    sources = {p: p.read_bytes() for p in folder.glob("*.json")}
    replace = os.replace

    def interrupt(old, new):
        if Path(new) == draft.path:
            if replaced:
                replace(old, new)
            raise OSError("equal-value interruption")
        return replace(old, new)

    with monkeypatch.context() as fault:
        fault.setattr(os, "replace", interrupt)
        with pytest.raises(OSError, match="equal-value interruption"):
            getattr(draft, operation)(2)
    assert draft.path.read_text(encoding="utf-8") == original
    reloaded = Checkout.load(draft.path)
    if replaced:
        assert not list(folder.glob("*.json"))
        assert len(list(reverse.glob("*.json"))) == 2 + (1 if operation == "redo" and shape == "ABA" else 0)
        inverse = "redo" if operation == "undo" else "undo"
        assert len(getattr(reloaded, inverse)(2)) == 2
    else:
        assert {p: p.read_bytes() for p in folder.glob("*.json")} == sources
        assert len(getattr(reloaded, operation)(2)) == 2
    assert reloaded.path.read_text(encoding="utf-8") == original


@pytest.mark.parametrize("operation", ["undo", "redo"])
@pytest.mark.parametrize("point", ["prepared", "target", "first-promotion", "second-promotion"])
def test_process_exit_recovers_through_public_reload(draft, monkeypatch, operation, point):
    for value in (40, 99):
        draft.clip("c1.rocket").set(x=value)
        draft.save(quiet=True)
    if operation == "redo":
        draft.undo(2)
    before = draft.path.read_bytes()
    reverse = (store._redo_dir if operation == "undo" else store._history_dir)(draft.path)
    pid = os.fork()
    if pid == 0:
        replace = os.replace
        stage = store._stage_restore
        promotions = 0

        def exit_after_preparation(*args, **kwargs):
            stage(*args, **kwargs)
            if point == "prepared":
                os._exit(95)

        def exit_at_replace(old, new):
            nonlocal promotions
            if Path(new).parent == reverse:
                promotions += 1
                if point == f"{('first' if promotions == 1 else 'second')}-promotion":
                    os._exit(95)
            replace(old, new)
            if point == "target" and Path(new) == draft.path:
                os._exit(95)

        monkeypatch.setattr(store, "_stage_restore", exit_after_preparation)
        monkeypatch.setattr(os, "replace", exit_at_replace)
        try:
            getattr(draft, operation)(2)
        finally:
            os._exit(96)
    _, status = os.waitpid(pid, 0)
    assert os.waitstatus_to_exitcode(status) == 95
    reloaded = Checkout.load(draft.path)
    if point == "prepared":
        assert reloaded.path.read_bytes() == before
        assert len(getattr(reloaded, operation)(2)) == 2
    else:
        assert reloaded.path.read_bytes() != before
    inverse = "redo" if operation == "undo" else "undo"
    assert len(getattr(reloaded, inverse)(2)) == 2
    assert reloaded.path.read_bytes() == before
