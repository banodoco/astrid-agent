"""The coverage probe must remain bounded on every supported platform."""
import builtins
import importlib.util
import subprocess
import sys
import threading
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


@pytest.mark.parametrize("interrupt_at", ["wait", "join"])
def test_probe_interruption_reaps_child_and_reader(monkeypatch, interrupt_at):
    real_popen = subprocess.Popen
    real_thread = threading.Thread
    processes = []
    readers = []

    def start(_args, **kwargs):
        process = real_popen([sys.executable, "-c", "import time; time.sleep(10)"], **kwargs)
        processes.append(process)
        if interrupt_at == "wait":
            real_wait = process.wait
            def interrupt_wait(*args, **kwargs):
                process.wait = real_wait
                raise KeyboardInterrupt
            process.wait = interrupt_wait
        return process

    def make_reader(*args, **kwargs):
        reader = real_thread(*args, **kwargs)
        readers.append(reader)
        if interrupt_at == "join":
            real_join = reader.join
            def interrupt_join(*args, **kwargs):
                reader.join = real_join
                raise KeyboardInterrupt
            reader.join = interrupt_join
        return reader

    if interrupt_at == "join":
        # Make the first wait return while the child is alive, to reach join.
        def start_for_join(_args, **kwargs):
            process = start(_args, **kwargs)
            real_wait = process.wait
            def first_wait(*args, **kwargs):
                process.wait = real_wait
            process.wait = first_wait
            return process
        monkeypatch.setattr(conform.subprocess, "Popen", start_for_join)
    else:
        monkeypatch.setattr(conform.subprocess, "Popen", start)
    monkeypatch.setattr(conform.threading, "Thread", make_reader)

    with pytest.raises(KeyboardInterrupt):
        conform._bounded_probe([])
    assert processes[0].poll() is not None
    assert not readers[0].is_alive()
