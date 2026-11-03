"""Interim refusal while the offline transaction capability is unavailable."""

import json
from types import SimpleNamespace

import pytest

from astrid.installer import app_update, core


def _unexpected(*_args, **_kwargs):
    pytest.fail("the unsafe update reached an effect")


@pytest.mark.parametrize("kind", ["migration", "app"])
def test_shared_update_refuses_before_prepare_or_stop(tmp_path, kind):
    installer = core.Installer(tmp_path / "program", tmp_path / "data", SimpleNamespace())
    with pytest.raises(ValueError, match="Safe offline update is unavailable"):
        installer._run_update_transaction(
            None, {}, prepare=_unexpected, stop=_unexpected, switch_candidate=_unexpected,
            switch_old=_unexpected, activate=_unexpected, activate_old=_unexpected, kind=kind)
    assert not (tmp_path / "program").exists()


@pytest.mark.parametrize("step", ["staged", "stopping", "backed_up", "migrated", "switched", "verified"])
def test_recovery_preserves_journal_and_possible_served_data(tmp_path, step):
    root = tmp_path / "program"
    root.mkdir()
    data = tmp_path / "data"
    data.mkdir()
    served = data / "served.txt"
    served.write_text("newly served write")
    journal = root / "update-transaction.json"
    original = json.dumps({"step": step, "backup": {"root": "retained"}}).encode()
    journal.write_bytes(original)
    with pytest.raises(ValueError, match="Safe offline update is unavailable"):
        core.recover_transaction(root, data, SimpleNamespace(), stop=_unexpected,
                                 switch_old=_unexpected, activate_old=_unexpected)
    assert journal.read_bytes() == original
    assert served.read_text() == "newly served write"


def test_pinokio_recovery_refuses_before_runner_publication(tmp_path, monkeypatch):
    root = tmp_path / "program"
    root.mkdir()
    journal = root / "update-transaction.json"
    original = b'{"transaction_kind":"migration","step":"switched"}'
    journal.write_bytes(original)
    monkeypatch.setattr(app_update, "publish_runtime_runner", _unexpected)
    with pytest.raises(ValueError, match="Safe offline update is unavailable"):
        app_update._recover_app_locked(root, SimpleNamespace())
    assert journal.read_bytes() == original


def test_legacy_rollback_refuses_before_manager_or_data_operations(tmp_path):
    with pytest.raises(ValueError, match="Safe offline update is unavailable"):
        core.rollback_transaction(tmp_path, tmp_path, SimpleNamespace(), {},
                                  stop=_unexpected, switch_old=_unexpected,
                                  activate_old=_unexpected, save=_unexpected)
