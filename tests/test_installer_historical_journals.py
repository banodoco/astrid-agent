"""Candidate recovery consumes journals emitted by the real pre-updater writers."""
from __future__ import annotations

from pathlib import Path

import pytest

from astrid.installer.core import current, read_json
from tests.test_installer_update_recovery import _assert_retry, _run, _setup, _tree_snapshot

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("point,meaning", [
    ("prepared", "stop intent before a retained image exists"),
    ("backed_up", "retained image without a candidate"),
    ("staged", "legacy staged means already stopped and backed up"),
    ("stopped", "old installer stop completed without backup"),
    ("upgraded", "migration complete while old pointer remains"),
    ("switched", "candidate pointer published before activation"),
    ("started", "candidate activated before verification"),
    ("runtime:stopped", "Runtime journal interrupted before migration"),
    ("runtime:activated", "Runtime migration committed before restart"),
])
def test_candidate_recovers_authentic_pre_updater_journal(tmp_path, monkeypatch, real_runtime, point, meaning):
    # Portable abrupt exit allows the journal compatibility proof to run on
    # any host. The Linux matrix separately proves real SIGKILL semantics.
    monkeypatch.setenv("ASTRID_TEST_CRASH_METHOD", "exit")
    _setup(tmp_path, previous_schema=False)  # the supported pre-updater is schema 31
    result = _run(tmp_path, point, mode="historical")
    assert result.returncode == 86, (meaning, result.stdout, result.stderr)
    transaction = read_json(tmp_path / "program/update-transaction.json")
    assert transaction["format"] == "astrid-install-update/v1"
    assert "transaction_kind" not in transaction
    assert "identity" not in transaction["old"]
    backup = transaction.get("backup")
    retained = _tree_snapshot(Path(backup["root"])) if backup else None
    if point.startswith("runtime:"):
        journal = read_json(tmp_path / "data/runtime/upgrade-journal.json")
        assert journal["operation"] == "astrid-runtime-upgrade/v1"
        assert journal["state"] == point.split(":")[1]
        assert journal["realm_id"] == transaction["realm"]["realm_id"]
    repair = _run(tmp_path, mode="install")
    assert repair.returncode == 0, repair.stderr
    assert current(tmp_path / "program").root.name == "old"
    assert read_json(tmp_path / "program/update-transaction.json")["step"] == "rolled_back"
    assert (tmp_path / "data/retained.txt").read_text() == "original user data"
    if backup:
        assert _tree_snapshot(Path(backup["root"])) == retained
    _assert_retry(tmp_path, restore_expected=False, real_runtime=real_runtime)
