"""The installer secures the Runtime lock where the reader resolves it, in an installed layout."""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

import pytest

from astrid.core.rendering import _local_schema as reader
from astrid.installer.backend import _secure_installed_schema_inputs

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes")


def _populate_installed(root: Path) -> Path:
    """An installed layout: package data ships the lock under astrid/resources, not at the root."""
    for relative in (reader.SCHEMA_RELATIVE_PATH, *reader.READER_INPUTS):
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("{}\n" if relative == reader.SCHEMA_RELATIVE_PATH else "# x\n",
                          encoding="utf-8")
    schema = root / reader.SCHEMA_RELATIVE_PATH
    digest = "sha256:" + hashlib.sha256(schema.read_bytes()).hexdigest()
    lock = root / "astrid/resources/runtime.lock.json"
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text(json.dumps({"files": {
        reader.SCHEMA_RELATIVE_PATH.as_posix(): {
            "from": "contract/schemas/timeline-config.schema.json", "sha256": digest}}}),
        encoding="utf-8")
    assert not (root / "runtime.lock.json").exists()
    return schema


def test_installed_layout_lock_is_secured_where_the_reader_resolves_it(tmp_path):
    old = os.umask(0o002)
    try:
        folder = tmp_path / "version"
        root = folder / "py" / "lib" / "site-packages"
        schema = _populate_installed(root)
        _secure_installed_schema_inputs(folder, root)
        lock = root / "astrid/resources/runtime.lock.json"
        assert lock.stat().st_mode & 0o777 == 0o644
        assert reader.verified_schema_path(root, str(schema)) == schema
    finally:
        os.umask(old)
