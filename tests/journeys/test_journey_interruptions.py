"""Signal interruption must unwind through disposable-journey cleanup."""

from __future__ import annotations

import json
import signal
from pathlib import Path
from types import SimpleNamespace

import pytest

from astrid.core.dev import journey


@pytest.mark.parametrize("restore_fails", [False, True])
def test_sigterm_during_replace_restores_source_and_runs_private_cleanup(
    tmp_path: Path, monkeypatch, restore_fails: bool,
) -> None:
    checkout = tmp_path / "checkout"
    source = checkout / "astrid" / "core" / "dev" / "journey.py"
    source.parent.mkdir(parents=True)
    original = b"# temporary journey source\n"
    source.write_bytes(original)
    if restore_fails:
        write_bytes = Path.write_bytes

        def fail_source_restore(path: Path, data: bytes) -> int:
            if path == source and data == original:
                raise OSError("injected source restore failure")
            return write_bytes(path, data)

        monkeypatch.setattr(Path, "write_bytes", fail_source_restore)

    reference = tmp_path / "reference"
    profile = reference / "runtime" / "source-profiles" / "astrid.json"
    profile.parent.mkdir(parents=True)
    profile.write_text("{}", encoding="utf-8")
    realm = reference / "runtime" / "realms" / "realm-test"
    realm.mkdir(parents=True)
    runtime_checkout = tmp_path / "runtime-checkout"
    runtime_checkout.mkdir()
    monkeypatch.setenv("ASTRID_RUNTIME_CHECKOUT", str(runtime_checkout))

    private_root = tmp_path / "private-journey"
    private_root.mkdir()
    summary = tmp_path / "summary.json"
    monkeypatch.setattr(journey, "__file__", str(source))
    monkeypatch.setattr(journey.tempfile, "mkdtemp", lambda **_kwargs: str(private_root))
    monkeypatch.setattr(journey.shutil, "disk_usage", lambda _path: SimpleNamespace(free=10**12))
    monkeypatch.setattr(journey, "disk_preflight", lambda *_args: {"ok": True})
    monkeypatch.setattr(journey, "runtime_checkout_path", lambda: runtime_checkout)
    monkeypatch.setattr(journey, "collect_revisions", lambda *_args: {})
    monkeypatch.setattr(journey, "select_reference_realm", lambda _reference: ("realm-test", realm))
    monkeypatch.setattr(journey, "machine_load", lambda: {"load_1m": 0.0, "memory_pressure": "normal"})
    monkeypatch.setattr(journey, "_doctor_ready", lambda _value: True)

    previous_handlers = {
        signum: signal.getsignal(signum) for signum in (signal.SIGTERM, signal.SIGINT)
    }
    calls: list[str] = []

    def command(_runner, phase, _argv, _env, **_kwargs):
        calls.append(phase)
        if phase == "down":
            assert signal.getsignal(signal.SIGTERM) == signal.SIG_IGN
            assert signal.getsignal(signal.SIGINT) == signal.SIG_IGN
        return {"healthy": True, "renderer_node": {"status": "ok"}}

    monkeypatch.setattr(journey.Runner, "command", command)
    monkeypatch.setattr(journey.Journey, "open_client", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(journey.Journey, "excerpt", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(journey.Journey, "host_pid", lambda *_args: 12345)

    def interrupt_during_replacement(_self, _old_host: int) -> int:
        handler = signal.getsignal(signal.SIGTERM)
        assert callable(handler)
        handler(signal.SIGTERM, None)
        raise AssertionError("SIGTERM handler should unwind the journey")

    monkeypatch.setattr(journey.Journey, "replace_host", interrupt_during_replacement)
    reaped: list[Path] = []
    monkeypatch.setattr(journey, "_reap_owned_groups", lambda root: reaped.append(root) or [])

    result = journey.run(
        reference, "journey/main", summary, backend=journey.FFMPEG_BACKEND,
        start=2.0, end=6.0,
    )

    assert result == 1
    assert source.read_bytes() == (original + b"\n# journey source-change probe\n" if restore_fails else original)
    assert "adopt" in calls and "up" in calls and "down" in calls
    assert len(reaped) == 1 and reaped[0] == private_root
    report = json.loads(summary.read_text(encoding="utf-8"))
    assert report["ok"] is False
    assert report["outcome"] == "failure"
    assert "SIGTERM" in report["error"]
    if restore_fails:
        assert "source_change: injected source restore failure" in report["error"]
    assert all(signal.getsignal(signum) == handler for signum, handler in previous_handlers.items())


def test_mismatched_declared_runtime_refuses_before_source_change(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "checkout" / "astrid" / "core" / "dev" / "journey.py"
    source.parent.mkdir(parents=True)
    original = b"# temporary journey source\n"
    source.write_bytes(original)
    reference = tmp_path / "reference"
    reference.mkdir()
    runtime_checkout = tmp_path / "runtime-checkout"
    runtime_checkout.mkdir()
    declared_runtime = tmp_path / "declared-runtime"
    declared_runtime.mkdir()
    summary = tmp_path / "summary.json"
    calls: list[str] = []

    monkeypatch.setattr(journey, "__file__", str(source))
    monkeypatch.setattr(journey, "runtime_checkout_path", lambda: runtime_checkout)
    monkeypatch.setenv("ASTRID_RUNTIME_CHECKOUT", str(declared_runtime))
    monkeypatch.setattr(journey.shutil, "disk_usage", lambda _path: SimpleNamespace(free=10**12))
    monkeypatch.setattr(journey, "disk_preflight", lambda *_args: {"ok": True})
    monkeypatch.setattr(journey.Runner, "command", lambda *_args, **_kwargs: calls.append("command"))

    assert journey.run(reference, "journey/main", summary, backend=journey.FFMPEG_BACKEND,
                       start=2.0, end=6.0) == 1
    assert source.read_bytes() == original
    assert calls == []
    report = json.loads(summary.read_text(encoding="utf-8"))
    assert report["ok"] is False
    assert "does not match imported Runtime" in report["error"]


@pytest.mark.posix_native  # POSIX-only process-group API
def test_reaper_escalates_only_after_fresh_group_ownership_scan(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "private-journey"
    own_group = 300
    mixed_group = 400
    current_group = journey.platform.process_group(journey.os.getpid())
    owned_rows = [
        {"pid": 101, "ppid": 1, "pgid": own_group, "command": "generic_host --root private"},
        {"pid": 201, "ppid": 1, "pgid": mixed_group, "command": "node --root private"},
        {"pid": 301, "ppid": 1, "pgid": current_group, "command": "runtime_protocol --root private"},
    ]
    # Group 400 is initially private, then gains an unrelated member before
    # escalation; the KILL ownership scan must catch that change.
    term_process_rows = list(owned_rows)
    kill_process_rows = [
        *owned_rows,
        {"pid": 202, "ppid": 2, "pgid": mixed_group, "command": "unrelated process"},
    ]
    process_scans = iter([owned_rows, owned_rows, owned_rows, []])
    monkeypatch.setattr(journey, "_processes", lambda _root: next(process_scans))
    ps_scans = iter([term_process_rows, kill_process_rows])
    monkeypatch.setattr(journey, "_ps_rows", lambda: next(ps_scans))
    signaled: list[tuple[int, int]] = []
    from astrid.core.execution import platform_helpers, process_group
    monkeypatch.setattr(process_group, "process_birth_identity", lambda pid: f"birth-{pid}")
    def signal_group(pgid, sig, *, leader_birth):
        assert leader_birth == f"birth-{pgid}"
        signaled.append((pgid, sig))
    monkeypatch.setattr(platform_helpers, "signal_observed_group", signal_group)

    waits = iter([False, True])

    def wait_for(predicate, _timeout):
        result = next(waits)
        if result:
            assert predicate()
        else:
            assert not predicate()
        return result

    monkeypatch.setattr(journey, "wait_for", wait_for)

    assert journey._reap_owned_groups(root) == []
    assert set(signaled) == {
        (own_group, signal.SIGTERM),
        (mixed_group, signal.SIGTERM),
        (own_group, signal.SIGKILL),
    }


@pytest.mark.parametrize("term_race", [False, True])
def test_runner_command_interruption_terminates_child_group(monkeypatch, term_race: bool) -> None:
    calls: list[tuple[int, int]] = []

    class FakeProcess:
        pid = 7654
        returncode = None

        def communicate(self, timeout=None):
            raise journey.JourneyFailure("interrupted", "received SIGTERM")

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            self.returncode = -signal.SIGTERM
            return self.returncode

    process = FakeProcess()
    monkeypatch.setattr(journey.subprocess, "Popen", lambda *args, **kwargs: process)

    def stop(child, *, grace):
        assert child is process and grace == 3
        calls.append((child.pid, signal.SIGTERM))
        # Runtime owns the exit-during-signal race and still reaps its child.
        if term_race:
            child.returncode = -signal.SIGTERM
        child.wait(timeout=grace)

    monkeypatch.setattr(journey.platform, "stop_tree", stop)
    runner = journey.Runner(Path("/private/journey"), Path("/tmp/summary"))

    with pytest.raises(journey.JourneyFailure, match="interrupted: received SIGTERM"):
        runner.command("up", ["fake"], {})

    assert calls == [(process.pid, signal.SIGTERM)]
    assert process.returncode == -signal.SIGTERM
