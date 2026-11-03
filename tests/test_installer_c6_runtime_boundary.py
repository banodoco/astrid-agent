"""The Agent journal must not roll back across a Runtime upgrade."""

from types import SimpleNamespace

import pytest

from astrid.installer import core


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
