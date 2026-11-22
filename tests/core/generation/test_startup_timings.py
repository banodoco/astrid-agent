from __future__ import annotations

import builtins
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest


REPO_ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.parametrize(
    "command",
    [
        [
            sys.executable,
            "-c",
            "from astrid.console import main; raise SystemExit(main())",
            "generate",
            "image",
            "--json",
            "--timings",
        ],
        [sys.executable, "-m", "astrid", "generate", "image", "--json", "--timings"],
    ],
    ids=["console-entry", "python-module"],
)
def test_fresh_generation_entrypoints_report_startup_on_early_refusal(command):
    env = os.environ.copy()
    env.pop("ASTRID_CLI_START_MONO", None)
    result = subprocess.run(
        command,
        cwd=REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 2
    assert "prompt is required" in result.stderr
    # The refusal happens during argument validation, before any generation
    # work, but the fresh process start is still included in its timing trace.
    assert "interpreter start:" in result.stderr
    assert "gateway imports:" in result.stderr
    assert "command dispatch:" in result.stderr


def test_console_process_start_uses_command_entry_when_os_probe_fails(monkeypatch):
    from astrid import console

    fallback = 123.5
    real_open = builtins.open

    def unavailable_proc(path, *args, **kwargs):
        if path == "/proc/self/stat":
            raise OSError("procfs unavailable")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(console.sys, "platform", "linux")
    monkeypatch.setattr(builtins, "open", unavailable_proc)
    assert console.process_started_at(fallback) == fallback


def test_repeated_embedded_gateway_calls_refresh_start_after_idle(monkeypatch):
    from astrid.core import gateway

    clock = SimpleNamespace(now=10.0)
    monkeypatch.setattr(gateway, "time", SimpleNamespace(monotonic=lambda: clock.now))
    monkeypatch.setattr(gateway, "_MAIN_CALLS", 1)
    observed = []

    def dispatch(_argv):
        from astrid.console import startup_marks
        observed.append(startup_marks())
        return 0

    monkeypatch.setattr(gateway, "_main_impl", dispatch)
    monkeypatch.setenv("ASTRID_CLI_START_MONO", "1.0")
    assert gateway.main(["embedded-one"]) == 0
    clock.now = 900.0  # model a long idle interval within the embedding process
    assert gateway.main(["embedded-two"]) == 0

    assert observed == [(10.0, "command entry", None, None),
                        (900.0, "command entry", None, None)]


def test_direct_generation_cli_ignores_stale_process_marker(monkeypatch):
    from astrid.core.generation import cli as generation_cli
    from astrid.sdk import command_timing

    clock = SimpleNamespace(monotonic=lambda: 900.0)
    monkeypatch.setattr(generation_cli, "time", clock)
    monkeypatch.setattr(command_timing, "time", clock)
    monkeypatch.setenv("ASTRID_CLI_START_MONO", "1.0")
    starts = []
    original_init = command_timing.TimingCollector.__init__

    def capture_init(self, *args, **kwargs):
        starts.append((kwargs.get("started"), kwargs.get("startup")))
        original_init(self, *args, **kwargs)

    monkeypatch.setattr(command_timing.TimingCollector, "__init__", capture_init)
    with pytest.raises(SystemExit) as excinfo:
        generation_cli.main(["image", "--json", "--timings"])

    assert excinfo.value.code == 2
    assert starts == [(900.0, (900.0, "command entry", None, None, 900.0))]
