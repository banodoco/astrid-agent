"""Attached ComfyUI generation is refused early when Windows long paths are off."""

from __future__ import annotations

import subprocess
import sys
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from astrid.core.generation import cli, windows_long_paths
from astrid.core.generation.windows_long_paths import LONG_PATHS_MESSAGE, long_paths_enabled

ATTACHED = "http://127.0.0.1:8188"


def _raises(exc):
    def reader():
        raise exc
    return reader


def test_helper_is_unknown_off_windows():
    assert long_paths_enabled("linux", reader=lambda: pytest.fail("read off Windows")) is None
    assert long_paths_enabled("darwin", reader=lambda: pytest.fail("read off Windows")) is None


@pytest.mark.parametrize(("value", "expected"), [(1, True), (0, False)])
def test_helper_reads_the_registry_value(value, expected):
    assert long_paths_enabled("win32", reader=lambda: value) is expected


def test_missing_value_means_disabled():
    assert long_paths_enabled("win32", reader=_raises(FileNotFoundError())) is False


def test_unreadable_registry_is_unknown_and_does_not_block():
    assert long_paths_enabled("win32", reader=_raises(OSError("denied"))) is None


def test_module_imports_without_winreg():
    script = ("import sys; import astrid.core.generation.windows_long_paths as m; "
              "assert 'winreg' not in sys.modules; assert m.long_paths_enabled('linux') is None")
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr


def test_message_is_the_three_fixed_sentences():
    lines = LONG_PATHS_MESSAGE.split("\n")
    assert len(lines) == 3
    assert lines[0] == "Generating with your own ComfyUI on Windows currently needs Windows long paths to be enabled."
    assert "New-ItemProperty -Path 'HKLM:\\SYSTEM\\CurrentControlSet\\Control\\FileSystem' -Name LongPathsEnabled -Value 1 -PropertyType DWORD -Force" in lines[1]
    assert lines[2] == "Then restart Astrid (sign out and in, or restart the computer) and run the same command again."


def _facade(monkeypatch):
    calls = []

    def invoke(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(ok=True, to_dict=lambda: {"image_paths": ["dog.png"]})

    monkeypatch.setattr(cli, "_generate_facade", lambda: SimpleNamespace(image=invoke))
    monkeypatch.setattr(cli, "_open_client", lambda: nullcontext("runtime-client"))
    return calls


def test_attached_generation_is_refused_before_task_creation(monkeypatch):
    calls = _facade(monkeypatch)
    monkeypatch.setattr(windows_long_paths, "long_paths_enabled", lambda *a, **k: False)
    with pytest.raises(ValueError) as exc:
        cli.main(["image", "a dog", "--placement", ATTACHED])
    assert str(exc.value) == LONG_PATHS_MESSAGE
    assert calls == []


def test_attached_dry_run_reports_the_same_refusal(monkeypatch):
    calls = _facade(monkeypatch)
    monkeypatch.setattr(windows_long_paths, "long_paths_enabled", lambda *a, **k: False)
    with pytest.raises(ValueError, match="long paths"):
        cli.main(["image", "a dog", "--placement", ATTACHED, "--dry-run"])
    assert calls == []


@pytest.mark.parametrize("enabled", [None, True])
def test_unknown_or_enabled_long_paths_leave_generation_unchanged(monkeypatch, enabled, capsys):
    calls = _facade(monkeypatch)
    monkeypatch.setattr(windows_long_paths, "long_paths_enabled", lambda *a, **k: enabled)
    assert cli.main(["image", "a dog", "--placement", ATTACHED, "--json"]) == 0
    assert len(calls) == 1
    assert calls[0]["placement"] == ATTACHED
    assert "dog.png" in capsys.readouterr().out
