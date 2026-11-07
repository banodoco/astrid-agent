"""Installed schema trees are umask independent; the reader still refuses 0775."""

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


def _populate(root: Path) -> Path:
    files = (reader.SCHEMA_RELATIVE_PATH, *reader.READER_INPUTS)
    for relative in files:
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("{}\n" if relative == reader.SCHEMA_RELATIVE_PATH else "# x\n",
                          encoding="utf-8")
    schema = root / reader.SCHEMA_RELATIVE_PATH
    digest = "sha256:" + hashlib.sha256(schema.read_bytes()).hexdigest()
    (root / "runtime.lock.json").write_text(json.dumps({"files": {
        reader.SCHEMA_RELATIVE_PATH.as_posix(): {
            "from": "contract/schemas/timeline-config.schema.json", "sha256": digest}}}),
        encoding="utf-8")
    return schema


def test_fresh_install_verifies_under_umask_0002(tmp_path):
    old = os.umask(0o002)
    try:
        folder = tmp_path / "version"
        root = folder / "py" / "lib" / "site-packages"
        schema = _populate(root)
        assert (root.stat().st_mode & 0o777) == 0o775  # the umask really bites
        _secure_installed_schema_inputs(folder, root)
        for path in (root, schema.parent, root / "astrid"):
            assert path.stat().st_mode & 0o777 == 0o755
        assert schema.stat().st_mode & 0o777 == 0o644
        assert reader.verified_schema_path(root, str(schema)) == schema
    finally:
        os.umask(old)


def test_user_chosen_0775_root_is_refused_with_fix(tmp_path):
    old = os.umask(0o002)
    try:
        root = tmp_path / "chosen"
        schema = _populate(root)
        # Only the user-chosen root is group-writable; the rest is private, so the
        # refusal must name the root itself.
        for path in (p for p in root.rglob("*") if p.is_dir()):
            path.chmod(0o755)
        for path in root.rglob("*"):
            if path.is_file():
                path.chmod(0o644)
        root.chmod(0o775)
        with pytest.raises(ValueError) as caught:
            reader.verified_schema_path(root, str(schema))
        message = str(caught.value)
        assert "foreign owner/mode" in message
        assert str(root) in message
        assert f"chmod 755 {root}" in message
    finally:
        os.umask(old)
