"""Regression coverage for the bounded legacy launchd action-cache window."""

from __future__ import annotations

from pathlib import Path

import pytest

from astrid.installer.core import Installer, Version, current, read_json, switch, write_json


class _Backend:
    """Small installer caller double; stop is the production C1 callback seam."""

    def __init__(self, root: Path, data: Path):
        self.root, self.data = root, data
        self.calls: list[str] = []
        self.disk_action = "legacy"
        self.loaded_action = "legacy"

    def has_workspace(self, _data):
        return True

    def workspace(self, *_args):
        realm = self.data / "runtime" / "realms" / "realm-1"
        realm.mkdir(parents=True, exist_ok=True)
        return {"realm_id": "realm-1", "realm_root": str(realm)}

    def stage(self, _manifest, root, _data):
        folder = root / "versions" / "candidate"
        folder.mkdir(parents=True)
        python = folder / "py" / "bin" / "python"
        python.parent.mkdir(parents=True)
        python.write_text("python", encoding="utf-8")
        candidate = Version(folder, python, folder / "profile.json", {}, release=2)
        write_json(folder / "version.json", candidate.document())
        self.calls.append("stage")
        return candidate

    def validate_candidate(self, *_args):
        self.calls.append("validate")

    def publish_launch_admission(self, _version, _data):
        # Model atomic plist replacement. launchd's separately loaded action is
        # intentionally unchanged; this test does not emulate native launchd.
        self.disk_action = "current-gated"
        self.calls.append("publish")

    def stop(self, _version, _data):
        # Production Backend.stop delegates to Runtime down(), whose C1 fence
        # runs before its service manager callback can issue launchctl bootout.
        self.calls.append("C1-stop")

    def backup(self, *_args):
        self.calls.append("backup")
        return None

    def upgrade(self, *_args):
        self.calls.append("upgrade")

    def register(self, *_args):
        self.calls.append("register")

    def start(self, *_args):
        self.calls.append("start")

    def verify(self, *_args, **_kwargs):
        self.calls.append("verify")

    def sync_skills(self, *_args):
        self.calls.append("sync")

    def finish(self, *_args):
        self.calls.append("finish")


def test_published_plist_interruption_recovers_only_through_c1_stop(tmp_path):
    root, data = tmp_path / "program", tmp_path / "data"
    old_folder = root / "versions" / "old"
    old_folder.mkdir(parents=True)
    old_python = old_folder / "py" / "bin" / "python"
    old_python.parent.mkdir(parents=True)
    old_python.write_text("python", encoding="utf-8")
    old = Version(old_folder, old_python, old_folder / "profile.json", {}, release=1)
    write_json(old_folder / "version.json", old.document())
    switch(root, old)
    backend = _Backend(root, data)
    installer = Installer(root, data, backend, say=lambda _line: None)
    save_update = installer._save_update

    def interrupt_after_stop_intent(transaction, step, **details):
        save_update(transaction, step, **details)
        if step == "stopping":
            raise KeyboardInterrupt("simulated interruption before C1 bootout")

    installer._save_update = interrupt_after_stop_intent
    with pytest.raises(KeyboardInterrupt, match="before C1 bootout"):
        installer.run("update", {"release": 2, "agent": {}, "runtime": {}})

    journal = root / "update-transaction.json"
    assert backend.calls[:3] == ["stage", "validate", "publish"]
    assert backend.calls.count("C1-stop") == 0
    assert backend.disk_action == "current-gated"
    assert backend.loaded_action == "legacy"
    assert current(root) == old
    assert read_json(journal)["step"] == "stopping"

    # A fresh installer recovers the durable stop intent via Backend.stop/C1
    # before it can restore/activate anything or select a different version.
    backend.calls.clear()
    recovered = Installer(root, data, backend, say=lambda _line: None)
    recovered._recover_update()

    assert backend.calls.index("C1-stop") < backend.calls.index("register")
    assert current(root) == old
    assert read_json(journal)["step"] == "rolled_back"
