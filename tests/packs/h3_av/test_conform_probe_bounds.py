"""The coverage probe must remain bounded on every supported platform."""
import builtins
import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

from astrid.packs.h3_av.shared import conform
from astrid.packs.h3_av.shared.errors import PreparationError


def test_import_without_resource_refuses_probe(monkeypatch):
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
    monkeypatch.setattr(module.subprocess, "run", lambda *a, **kw: pytest.fail("probe started"))
    with pytest.raises(PreparationError, match="bounded probe output is unavailable"):
        module._video_packet_end(Path("source"), {}, 0, 1)


def test_probe_rejects_output_over_limit(monkeypatch):
    if conform.resource is None:
        pytest.skip("RLIMIT_FSIZE is unavailable")
    real_run = subprocess.run
    monkeypatch.setattr(conform, "_MAX_PROBE_BYTES", 1024)

    def producer(_args, **kwargs):
        return real_run([sys.executable, "-c", "import os; os.write(1, b'{}' + b' ' * 4096)"], **kwargs)

    monkeypatch.setattr(conform.subprocess, "run", producer)
    with pytest.raises((subprocess.CalledProcessError, ValueError)):
        conform._bounded_probe([])
