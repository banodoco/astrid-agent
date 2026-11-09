"""Shared umask assertions for the installed-schema staging tests."""

from __future__ import annotations

import os
from pathlib import Path


def assert_stage_keeps_guarded_inputs_private(state, root: Path, schema: Path) -> None:
    """Stage under the ambient umask and assert guarded inputs stay private.

    Called directly by the normal test run and by the isolated umask 0002 subprocess.
    """
    from astrid.core.rendering._local_schema import READER_INPUTS
    source_before = {p: (p.read_bytes(), p.stat().st_mode) for p in
                     (root / schema, root / "runtime.lock.json", *(root / p for p in READER_INPUTS))}
    version = state.backend.stage(state.manifest, state.root, state.data)
    for relative in (schema, Path("runtime.lock.json"), *READER_INPUTS):
        path = state.product_root / relative
        for part in (path, *path.parents):
            assert not part.stat().st_mode & 0o022, part
            if part == state.product_root:
                break
    for relative, (cache, content, mode) in state.cached.items():
        assert (cache.read_bytes(), cache.stat().st_mode) == (content, mode)
        assert not os.path.samefile(cache, state.product_root / relative)
    assert {p: (p.read_bytes(), p.stat().st_mode) for p in source_before} == source_before
    assert state.backend._verify_local_timeline_schema(state.product_root) == state.product_root / schema
    assert version.profile.exists()
