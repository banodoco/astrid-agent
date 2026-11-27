"""Delivery joins existing lifecycle fences; it never creates stop authority."""
from __future__ import annotations

from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from astrid.installer.core import Installer, RuntimeOperationError, Version, current, read_json, switch, write_json
from tests.test_boundary_handoff import case, state, upgrade  # original real-adoption fixture


@pytest.mark.parametrize("fail", [None, "stop", "activation"])
def test_shared_update_publishes_only_after_stop_and_rolls_back_before_selection(tmp_path, fail):
    root, data = tmp_path / "program", tmp_path / "data"
    events = []
    versions = []
    for name in ("old", "candidate"):
        folder = root / "versions" / name
        folder.mkdir(parents=True)
        value = Version(folder, Path(sys.executable), folder / "profile.json", {}, release=16)
        write_json(folder / "version.json", value.document())
        versions.append(value)
    old, candidate = versions
    switch(root, old)

    def apply(new, previous):
        assert events[-1] == "stop:old" and current(root) == old
        assert (new, previous) == (candidate, old)
        events.append("defaults:apply")

    def rollback(previous, new):
        assert events[-1] == "stop:candidate"
        assert current(root) == candidate and (previous, new) == (old, candidate)
        events.append("defaults:rollback")

    backend = SimpleNamespace(apply_default_packs=apply, rollback_default_packs=rollback)
    installer = Installer(root, data, backend, say=lambda _: None)

    def stop(version, _data):
        events.append("stop:" + version.root.name)
        if fail == "stop":
            raise RuntimeOperationError("busy", details={"safe": False})

    def select(new):
        assert events[-1] == "defaults:apply"
        switch(root, new)
        events.append("select:new")

    def restore(previous, _candidate):
        assert events[-1] == "defaults:rollback"
        switch(root, previous)
        events.append("select:old")

    def activate(*_):
        events.append("activate:new")
        if fail == "activation":
            raise RuntimeError("readiness")

    def operation():
        return installer._run_update_transaction(old, {"realm_id": "r", "realm_root": str(data / "r")},
            prepare=lambda: candidate, stop=stop, switch_candidate=select, switch_old=restore,
            activate=activate, activate_old=lambda *_: events.append("activate:old"),
            kind="app", restore_workspace=False)

    if fail:
        with pytest.raises(RuntimeError):
            operation()
        assert current(root) == old
        if fail == "stop":
            assert events == ["stop:old"]
        else:
            assert events[-4:] == ["stop:candidate", "defaults:rollback", "select:old", "activate:old"]
    else:
        operation()
        assert events == ["stop:old", "defaults:apply", "select:new", "activate:new"]
        assert current(root) == candidate


def test_copy_only_handoff_uses_its_existing_stop_and_restore_fences(case, monkeypatch):
    c = case
    events = []
    previous = current(c.root)

    def apply(candidate, old):
        assert old == previous and current(c.root) == previous
        assert not state(c.manager)["running"] and state(c.manager)["r1"] == "inactive"
        events.append("apply")

    def rollback(old, candidate):
        assert old == previous and current(c.root) == candidate
        assert not state(c.manager)["running"] and state(c.manager)["r1"] == "inactive"
        events.append("rollback")

    monkeypatch.setattr(c.backend, "apply_default_packs", apply)
    monkeypatch.setattr(c.backend, "rollback_default_packs", rollback)
    upgrade(c)
    assert events == ["apply"]
    c.tx.recover(rollback=True)
    assert events == ["apply", "rollback"]
    assert current(c.root) == previous
    assert state(c.manager)["running"]


def test_copy_only_stop_refusal_cannot_run_default_rollback(case, monkeypatch):
    c = case
    monkeypatch.setattr(c.backend, "apply_default_packs", lambda *_: None)
    upgrade(c)
    before = current(c.root)
    manager = read_json(c.manager)
    manager["fail_r1_stop"] = True
    write_json(c.manager, manager)
    monkeypatch.setattr(c.backend, "rollback_default_packs", lambda *_: pytest.fail("refused stop cannot restore packs"))
    with pytest.raises(Exception):
        c.tx.recover(rollback=True)
    assert current(c.root) == before
