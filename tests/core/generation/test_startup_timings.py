from __future__ import annotations

import builtins
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest


REPO_ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.parametrize(
    "command",
    [
        [
            sys.executable,
            "-c",
            "from astrid.console import entrypoint; raise SystemExit(entrypoint())",
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
    assert "prompt is required" in result.stdout
    # The refusal happens during argument validation, before any generation
    # work, but the fresh process start is still included in its timing trace.
    assert "interpreter start:" in result.stderr
    assert "gateway imports:" in result.stderr


def test_installed_console_executable_reports_fresh_process_start():
    executable = Path(sys.executable).with_name("astrid")
    assert executable.is_file(), f"expected installed Astrid console beside {sys.executable}"
    env = os.environ.copy()
    env.pop("ASTRID_CLI_START_MONO", None)
    result = subprocess.run(
        [str(executable), "generate", "image", "--json", "--timings"],
        cwd=REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 2
    assert "prompt is required" in result.stdout
    assert "interpreter start:" in result.stderr


def test_unavailable_start_probe_falls_back_and_returns_bounded_timing_json(tmp_path):
    # sitecustomize runs before the module entrypoint. Force the same fallback
    # as an unavailable OS process-start API and make generation refuse at the
    # facade boundary so the CLI returns a timing-enriched JSON error.
    (tmp_path / "sitecustomize.py").write_text(
        "import builtins, sys\n"
        "sys.platform = 'linux'\n"
        "_open = builtins.open\n"
        "def _no_proc(path, *args, **kwargs):\n"
        "    if path == '/proc/self/stat':\n"
        "        raise OSError('procfs unavailable')\n"
        "    return _open(path, *args, **kwargs)\n"
        "builtins.open = _no_proc\n"
        "from astrid.core.generation import cli\n"
        "class RefusingFacade:\n"
        "    def image(self, **kwargs):\n"
        "        raise ValueError('deterministic startup timing refusal')\n"
        "cli._generate_facade = lambda: RefusingFacade()\n",
        encoding="utf-8",
    )
    env = os.environ.copy()
    env.pop("ASTRID_CLI_START_MONO", None)
    env["PYTHONPATH"] = os.pathsep.join((str(tmp_path), str(REPO_ROOT), env.get("PYTHONPATH", "")))
    started = time.monotonic()
    result = subprocess.run(
        [sys.executable, "-m", "astrid", "generate", "image", "a dog", "--dry-run", "--json", "--timings"],
        cwd=REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    observed_span = time.monotonic() - started

    assert result.returncode == 2
    payload = json.loads(result.stdout)
    assert "deterministic startup timing refusal" in payload["issues"][0]["message"]
    assert payload["start"] == "command entry"
    rows = payload["timings"]
    assert rows
    assert "interpreter start" not in {row["stage"] for row in rows}
    assert "command dispatch" in {row["stage"] for row in rows}
    assert all(math.isfinite(row["seconds"]) and row["seconds"] >= 0 for row in rows)
    assert sum(row["seconds"] for row in rows) <= observed_span * 1.1


def test_module_entrypoint_preserves_user_facing_generation_refusal():
    env = os.environ.copy()
    env.pop("ASTRID_CLI_START_MONO", None)
    result = subprocess.run(
        [sys.executable, "-m", "astrid", "generate", "image", "--json", "--timings"],
        cwd=REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 2
    assert "prompt is required" in result.stdout
    assert "Traceback" not in result.stderr


def test_long_lived_first_console_call_uses_command_entry_when_gateway_is_unimported():
    script = (
        "import sys, time\n"
        "from astrid import console\n"
        "assert 'astrid.core.gateway' not in sys.modules\n"
        "console.process_started_at = lambda fallback: fallback - 1000\n"
        "time.sleep(0.05)\n"
        "raise SystemExit(console.main())\n"
    )
    env = os.environ.copy()
    env.pop("ASTRID_CLI_START_MONO", None)
    result = subprocess.run(
        [sys.executable, "-c", script, "generate", "image", "--json", "--timings"],
        cwd=REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 2
    assert "prompt is required" in result.stdout
    assert "command dispatch:" in result.stderr
    assert "interpreter start:" not in result.stderr


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
    result = generation_cli.main(["image", "--json", "--timings"])

    assert result == 2
    assert starts == [(900.0, (900.0, "command entry", None, None, 900.0))]
