"""Regression cases for the three Agent-only LS-6 review findings."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from astrid.sdk.remote import RemoteMedia


class _Runtime:
    def __init__(self, payload: bytes, filename: str = "output.bin") -> None:
        self.payload = payload
        self.filename = filename
        self.reference = "sha256:" + hashlib.sha256(payload).hexdigest()

    def list_project_objects(self, project: str, *, cursor=None, limit=50):
        assert project == "demo"
        return [[{
            "object_id": self.reference,
            "digest": self.reference,
            "media_type": "application/octet-stream",
            "size": len(self.payload),
            "filename": self.filename,
        }], None]

    def get_object(self, object_id: str):
        assert object_id == self.reference
        return {"data": self.payload}


def test_open_reuses_verified_fallback_and_preserves_real_conflicts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from astrid.sdk import storage_root

    monkeypatch.setattr(storage_root, "resolve_runtime_data_root", lambda: tmp_path)
    runtime = _Runtime(b"independent caller view")
    media = RemoteMedia(runtime)
    first = media.open("demo", runtime.reference, materialize=True)
    assert first.ok
    original = Path(first.data["local_path"])
    backup = tmp_path / "backup.bin"
    os.link(original, backup)

    second = media.open("demo", runtime.reference, materialize=True)
    assert second.ok
    fallback = Path(second.data["local_path"])
    assert fallback != original
    assert fallback.read_bytes() == runtime.payload
    assert not os.path.samefile(fallback, backup)
    third = media.open("demo", runtime.reference, materialize=True)
    assert third.ok
    assert Path(third.data["local_path"]) == fallback
    assert third.data["local_path_storage"] == "verified-reuse"

    fallback.write_bytes(b"caller edit")
    with pytest.raises(FileExistsError, match="media-open view path collision"):
        media.open("demo", runtime.reference, materialize=True)
    assert fallback.read_bytes() == b"caller edit"
    assert original.read_bytes() == backup.read_bytes() == runtime.payload

    # Matching bytes alone must not make a fallback hardlink reusable.
    fallback.unlink()
    os.link(backup, fallback)
    with pytest.raises(FileExistsError, match="media-open view path collision"):
        media.open("demo", runtime.reference, materialize=True)
    assert os.path.samefile(fallback, backup)
    assert backup.read_bytes() == runtime.payload


def test_open_accepts_a_long_explicit_cache_filename(tmp_path: Path) -> None:
    filename = "a" * 244 + ".bin"
    runtime = _Runtime(b"long filename output", filename)
    cache = tmp_path / "views"
    cache.mkdir()
    target = cache / filename
    # Establish that the destination component itself is legal on this volume.
    target.write_bytes(b"prior caller bytes")

    result = RemoteMedia(runtime).open(
        "demo", runtime.reference, materialize=True, cache_root=cache,
    )

    assert result.ok
    assert Path(result.data["local_path"]) == target
    assert target.read_bytes() == runtime.payload
    assert list(cache.iterdir()) == [target]


def test_open_honors_the_callers_umask(tmp_path: Path) -> None:
    # Umask is process-global. Exercise real creation modes in a child so this
    # test cannot change permissions on files created by other test threads.
    script = """
import json
import os
import stat
import sys
from pathlib import Path

from astrid.sdk.remote import RemoteMedia
from tests.sdk.test_ls6_materialization_regressions import _Runtime

root = Path(sys.argv[1])
observed = []
for mask in (0o022, 0o027, 0o077):
    cache = root / str(mask)
    cache.mkdir(parents=True)
    runtime = _Runtime(b"umask output")
    previous = os.umask(mask)
    try:
        control = cache / "ordinary-file.bin"
        control.write_bytes(runtime.payload)
        result = RemoteMedia(runtime).open(
            "demo", runtime.reference, materialize=True, cache_root=cache,
        )
    finally:
        os.umask(previous)
    assert result.ok
    view = Path(result.data["local_path"])
    assert view.read_bytes() == runtime.payload
    observed.append([mask, stat.S_IMODE(control.stat().st_mode), stat.S_IMODE(view.stat().st_mode)])
print(json.dumps(observed))
"""
    completed = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path / "views")],
        check=True, capture_output=True, text=True,
    )
    observed = json.loads(completed.stdout)
    assert len(observed) == 3
    for mask, ordinary_mode, view_mode in observed:
        assert view_mode == ordinary_mode
        if os.name == "posix":
            assert view_mode == 0o666 & ~mask
