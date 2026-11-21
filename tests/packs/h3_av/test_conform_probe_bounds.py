"""The coverage probe must remain bounded on every supported platform."""
import builtins
import importlib.util
import subprocess
import sys
from fractions import Fraction
from pathlib import Path

import pytest

from astrid.packs.h3_av.shared import conform


def _producer(monkeypatch, script):
    real_popen = subprocess.Popen
    processes = []

    def start(_args, **kwargs):
        process = real_popen([sys.executable, "-c", script], **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(conform.subprocess, "Popen", start)
    return processes


def test_import_without_resource_accepts_valid_probe(monkeypatch):
    real_import = builtins.__import__

    def no_resource(name, *args, **kwargs):
        if name == "resource":
            raise ModuleNotFoundError("No module named 'resource'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_resource)
    spec = importlib.util.spec_from_file_location(
        "astrid.packs.h3_av.shared._conform_no_resource", conform.__file__
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    processes = _producer(monkeypatch, "print('{\"packets\":[{\"pts\":0,\"duration\":1}]}')")
    assert module._video_packet_end(Path("source"), {"time_base": "1/1"}, Fraction(0), Fraction(1)) == 1
    assert processes[0].poll() == 0


def test_probe_rejects_output_over_limit(monkeypatch):
    monkeypatch.setattr(conform, "_MAX_PROBE_BYTES", 1024)
    processes = _producer(monkeypatch, "import os; os.write(1, b'{}' + b' ' * 65536)")
    with pytest.raises(ValueError, match="exceeded size limit"):
        conform._bounded_probe([])
    assert processes[0].poll() is not None


def test_probe_times_out_and_reaps_child(monkeypatch):
    monkeypatch.setattr(conform, "_PROBE_TIMEOUT_SECONDS", 0.3)
    processes = _producer(monkeypatch, "import time; time.sleep(10)")
    with pytest.raises(subprocess.TimeoutExpired):
        conform._bounded_probe([])
    assert processes[0].poll() is not None
