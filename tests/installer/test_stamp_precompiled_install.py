"""Prove production staging precompiles a previously unimported wheel module."""

from __future__ import annotations

import sys

import pytest

from astrid.installer import core
from tests.test_installer_installed_schema import (
    _installed_script,
    release_wheel,  # noqa: F401 - pytest fixture dependency
    staged_installer,  # noqa: F401 - pytest fixture
)

pytestmark = pytest.mark.linux_native


def test_first_import_after_stage_uses_precompiled_bytecode(staged_installer, monkeypatch):  # noqa: F811
    state = staged_installer
    declare = state.backend._declare_services

    def declare_and_add_probe(**kwargs):
        result = declare(**kwargs)
        probe = state.product_root / "astrid/_seal_first_import_probe.py"
        probe.write_text("VALUE = 42\n", encoding="utf-8")
        return result

    monkeypatch.setattr(state.backend, "_declare_services", declare_and_add_probe)
    version = state.backend.stage(state.manifest, state.root, state.data)
    package = state.product_root / "astrid"
    path = package / "_seal_first_import_probe.py"
    pyc = path.parent / "__pycache__" / (
        "_seal_first_import_probe." + sys.implementation.cache_tag + ".pyc")
    doc = core.read_json(version.root / "install-stamp.json")
    assert pyc.is_file()
    header = pyc.read_bytes()[:16]
    assert int.from_bytes(header[4:8], "little") == 0
    assert int.from_bytes(header[8:12], "little") == int(path.stat().st_mtime)
    assert int.from_bytes(header[12:16], "little") == path.stat().st_size
    assert pyc.relative_to(version.root).as_posix() in doc["files"]
    assert not pyc.stat().st_mode & 0o222
    assert not pyc.parent.stat().st_mode & 0o222
    before = core.install_stamp(package)
    assert before[0] and before[1] is None
    result = _installed_script(state, version, r'''
import importlib.machinery, importlib.util, json, pathlib, sys
from astrid.installer.core import install_stamp
source = pathlib.Path(sys.argv[1]) / "astrid/_seal_first_import_probe.py"
assert "astrid._seal_first_import_probe" not in sys.modules
original = importlib.machinery.SourceFileLoader.source_to_code
def refuse_compile(self, data, path, *args, **kwargs):
    if pathlib.Path(path) == source:
        raise AssertionError("probe was recompiled after seal")
    return original(self, data, path, *args, **kwargs)
importlib.machinery.SourceFileLoader.source_to_code = refuse_compile
from astrid import _seal_first_import_probe
print(json.dumps({
    "value": _seal_first_import_probe.VALUE,
    "cache": _seal_first_import_probe.__cached__,
    "stamp": install_stamp(),
}))
''')
    assert result["value"] == 42
    assert result["cache"] == str(pyc)
    assert result["stamp"] == list(before)
    assert core.install_stamp(package) == before
