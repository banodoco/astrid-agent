"""The Agent journal must not roll back across a Runtime upgrade."""

import os
from types import SimpleNamespace

import pytest

from astrid.installer import core
from runtime_protocol import platform


def test_published_runtime_refuses_installer_owned_lifetime_mutex(tmp_path, monkeypatch):
    from astrid_runtime import operator_upgrade, paths, update

    selection, lock = tmp_path / "selection.json", tmp_path / "installer.lock"
    selection.touch()
    monkeypatch.setattr(paths, "_install_record_path", lambda _home: selection)
    monkeypatch.setattr(update, "_installer_lock_path", lambda _selection: lock)
    descriptor = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        platform.lock_file(descriptor, blocking=False)
        with pytest.raises(operator_upgrade.OperatorUpgradeError, match="another installer owns"):
            with operator_upgrade._installer_mutex(SimpleNamespace(home=tmp_path, runtime_support=tmp_path)):
                pytest.fail("a second owner acquired the installer mutex")
    finally:
        platform.unlock_file(descriptor)
        os.close(descriptor)


@pytest.mark.parametrize("failed_step", ["stop", "wait"])
def test_uncertain_stop_never_starts_old_service_on_failure_or_recovery(tmp_path, monkeypatch, failed_step):
    root, data = tmp_path / "program", tmp_path / "data"
    root.mkdir()
    old = core.Version(root / "old", root / "old-python", root / "old-profile", {})
    candidate = core.Version(root / "new", root / "new-python", root / "new-profile", {})
    realm = {"realm_id": "r", "realm_root": str(data / "realm")}
    events = []
    monkeypatch.setattr(core, "install_runtime", lambda *_: None)

    def step(name):
        def run(*_args):
            events.append(name)
            if name == failed_step:
                raise RuntimeError(name)
        return run

    backend = SimpleNamespace(wait_for_realm_owner=step("wait"))
    with pytest.raises(RuntimeError, match=failed_step):
        core.Installer(root, data, backend)._run_update_transaction(
            old, realm, prepare=lambda: candidate, stop=step("stop"),
            migrate=step("upgrade"), runtime_upgrade=True,
            switch_candidate=step("switch"), switch_old=step("old-selection"),
            activate=step("activate"), activate_old=step("old-start"))
    assert events == (["stop"] if failed_step == "stop" else ["stop", "wait"])
    saved = core.read_json(root / "update-transaction.json")
    assert saved["original_service_state_unverified"] is True
    with pytest.raises(RuntimeError, match="original service state is unknown"):
        core.recover_transaction(root, data, backend, stop=step("recovery-stop"),
                                 switch_old=step("old-selection"), activate_old=step("old-start"))
    assert core.read_json(root / "update-transaction.json") == saved
    assert "old-start" not in events and "recovery-stop" not in events


@pytest.mark.parametrize("failure", [None, "upgrade", "switch", "activate"])
def test_runtime_handoff_journals_before_call_and_never_restores_after_it(tmp_path, monkeypatch, failure):
    root, data = tmp_path / "program", tmp_path / "data"
    root.mkdir()
    old = core.Version(root / "old", root / "old-python", root / "old-profile", {})
    candidate = core.Version(root / "new", root / "new-python", root / "new-profile", {})
    realm = {"realm_id": "r", "realm_root": str(data / "realm")}
    events = []
    monkeypatch.setattr(core, "install_runtime", lambda *_: None)
    monkeypatch.setattr(core, "install_launcher", lambda *_: None)
    backend = SimpleNamespace(wait_for_realm_owner=lambda _: events.append("lock-free"),
                              finish=lambda *_: events.append("finish"))

    def step(name):
        def run(*_args):
            events.append(name)
            if name == "upgrade":
                assert core.read_json(root / "update-transaction.json")["runtime_upgrade_started"] is True
            if name == failure:
                raise RuntimeError(name)
        return run

    def update():
        return core.Installer(root, data, backend)._run_update_transaction(
            old, realm, prepare=lambda: candidate, stop=step("stop"),
            migrate=step("upgrade"), runtime_upgrade=True,
            switch_candidate=step("switch"), switch_old=step("old-selection"),
            activate=step("activate"), activate_old=step("old-start"))

    if failure is None:
        update()
        assert events == ["stop", "lock-free", "upgrade", "switch", "activate", "finish"]
        assert core.read_json(root / "update-transaction.json")["step"] == "complete"
    else:
        with pytest.raises(RuntimeError, match=failure):
            update()
        assert "old-selection" not in events and "old-start" not in events
        saved = core.read_json(root / "update-transaction.json")
        assert saved["runtime_upgrade_started"] is True
        with pytest.raises(RuntimeError, match="may have changed"):
            core.recover_transaction(root, data, backend, stop=step("recovery-stop"),
                                     switch_old=step("old-selection"), activate_old=step("old-start"))
        assert "recovery-stop" not in events
        assert core.read_json(root / "update-transaction.json") == saved
