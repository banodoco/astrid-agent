"""Fail closed until Runtime and the native adapter implement transfer custody."""

import io
import json
from types import SimpleNamespace

import pytest

from astrid.installer import app_update, core, custody, local_update


@pytest.mark.parametrize("step", ["staged", "stopping", "switched", "complete", "rolled_back"])
@pytest.mark.parametrize("transfer", [None, {}, {"release_pending": True}, {"release_pending": False}])
def test_transfer_journal_is_pending_and_never_replayed_as_legacy(tmp_path, step, transfer):
    journal = {"step": step, "transaction_context": {"lifecycle_transfer": transfer}}
    path = tmp_path / "update-transaction.json"
    original = json.dumps(journal).encode()
    path.write_bytes(original)
    assert core.pending_transaction(tmp_path) == journal

    def forbidden(*args, **kwargs):
        pytest.fail("legacy lifecycle mutation was called")

    with pytest.raises(ValueError, match="original Runtime session"):
        core.recover_transaction(tmp_path, tmp_path, object(), stop=forbidden,
                                 switch_old=forbidden, activate_old=forbidden, save=forbidden)
    with pytest.raises(ValueError, match="original Runtime session"):
        core.rollback_transaction(tmp_path, tmp_path, object(), journal, stop=forbidden,
                                  switch_old=forbidden, activate_old=forbidden, save=forbidden)
    assert path.read_bytes() == original


def test_transfer_request_refuses_before_preparation_or_stop(tmp_path):
    installer = core.Installer(tmp_path, tmp_path, object())

    def forbidden(*args, **kwargs):
        pytest.fail("transfer reached legacy preparation or mutation")

    with pytest.raises(ValueError, match="original Runtime session"):
        installer._run_update_transaction(
            None, {}, prepare=forbidden, stop=forbidden, switch_candidate=forbidden,
            switch_old=forbidden, activate=forbidden, activate_old=forbidden,
            backup=forbidden, migrate=forbidden,
            transaction_context={"lifecycle_transfer": {}},
        )
    assert not (tmp_path / "update-transaction.json").exists()


def test_transfer_reentry_refuses_before_adapter_or_custody_side_effects(tmp_path, monkeypatch):
    path = tmp_path / "update-transaction.json"
    original = b'{"step":"complete","transaction_context":{"lifecycle_transfer":{"release_pending":true}}}'
    path.write_bytes(original)

    def forbidden(*args, **kwargs):
        pytest.fail("reentry tried to acquire custody or create an adapter")

    monkeypatch.setattr(custody, "stopped_custody", forbidden)
    monkeypatch.setattr(local_update, "_backend", forbidden)
    with pytest.raises(ValueError, match="original Runtime session"):
        with custody.operation(tmp_path, SimpleNamespace()):
            forbidden()
    with pytest.raises(ValueError, match="original Runtime session"):
        local_update.execute({"action": "update"}, tmp_path, stdin=io.StringIO(), stdout=io.StringIO())
    with pytest.raises(ValueError, match="original Runtime session"):
        app_update._recover_app_locked(tmp_path, object())
    assert path.read_bytes() == original


def test_legacy_terminal_journal_remains_terminal(tmp_path):
    (tmp_path / "update-transaction.json").write_text('{"step":"complete"}')
    assert core.pending_transaction(tmp_path) is None
