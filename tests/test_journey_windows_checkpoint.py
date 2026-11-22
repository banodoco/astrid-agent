from pathlib import Path
from types import SimpleNamespace
import json

from scripts import journey_windows_r13_update as windows


def test_checkpoint_poll_catches_short_lived_owned_journal_phase(tmp_path, monkeypatch):
    root = tmp_path / "program"
    data = tmp_path / "data"
    journal = root / "update-transaction.json"
    root.mkdir()
    journal.write_text(json.dumps({"operation_id": "0" * 32, "step": "complete"}))
    now = 0.0

    class Child:
        pid = 43210
        exit_code = None

        def poll(self):
            return self.exit_code

        def wait(self, timeout=None):
            return self.exit_code

    child = Child()

    def publish(phase):
        journal.write_text(json.dumps({
            "format": "astrid-install-update/v1", "data_root": str(data),
            "operation_id": "1" * 32, "step": phase,
        }))

    def spawn(*_args, **_kwargs):
        publish("staged")
        return child

    def sleep(seconds):
        nonlocal now
        now += seconds
        if 0.003 <= now < 0.007:
            publish("stopped")
        elif now >= 0.007:
            publish("complete")
            child.exit_code = 0

    def taskkill(args, **_kwargs):
        assert args[-2:] == ["/PID", "43210"]
        child.exit_code = 137
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(windows.journey, "bootstrap_flags", lambda *_args: [])
    monkeypatch.setattr(windows.subprocess, "Popen", spawn)
    monkeypatch.setattr(windows.subprocess, "run", taskkill)
    monkeypatch.setattr(windows.time, "monotonic", lambda: now)
    monkeypatch.setattr(windows.time, "sleep", sleep)

    result = windows.kill_update_at_owned_checkpoint(
        Path("app"), Path("manifest"), "powershell", tmp_path / "evidence", root, data)

    assert result["phase"] == "stopped"
    assert result["operation_id"] == "1" * 32
    assert result["installer_pid"] == 43210
    assert result["installer_exit_code"] == 137
    assert result["observed_phase_sequence"] == ["staged", "stopped"]
