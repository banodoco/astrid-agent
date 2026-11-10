"""Snapshot authorization cannot turn an ordinary shadow into a bundled pack."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from astrid.core.pack.identity import pack_digest
from astrid.core.pack.imports import install_pack_imports, selected_pack_environment, validate_selected_pack_map


@pytest.fixture
def bundled_snapshot(tmp_path):
    original = Path(__file__).resolve().parents[3] / "astrid" / "packs" / "rendering"
    snapshot = tmp_path / "rendering"
    shutil.copytree(original, snapshot)
    selected = {"rendering": {"root": str(snapshot), "source_digest": pack_digest(snapshot)}}
    relocations = {"rendering": {"original_root": str(original), **selected["rendering"]}}
    return snapshot, selected, relocations


def test_unverified_bundled_shadow_is_rejected(bundled_snapshot):
    snapshot, selected, _relocations = bundled_snapshot
    with pytest.raises(ValueError, match="collides with an in-tree pack"):
        validate_selected_pack_map(selected)
    with pytest.raises(ValueError, match="collides with an in-tree pack"):
        install_pack_imports("rendering", snapshot, selected_map=selected)


def test_only_verified_bundled_snapshot_keeps_bundled_collection_identity(bundled_snapshot):
    snapshot, selected, relocations = bundled_snapshot
    assert selected_pack_environment(selected)["ASTRID_PACKS_PATH"] == str(snapshot.parent)
    env = selected_pack_environment(selected, verified_relocations=relocations)
    assert env["ASTRID_PACKS_PATH"] == ""
    assert json.loads(env["ASTRID_SELECTED_PACK_MAP"]) == selected
    assert json.loads(env["ASTRID_VERIFIED_PACK_RELOCATIONS"]) == relocations


def test_namespace_accepts_only_the_verified_bundled_relocation(bundled_snapshot, tmp_path):
    import astrid.packs

    snapshot, selected, relocations = bundled_snapshot
    with pytest.raises(ImportError, match="collides"):
        astrid.packs._check_collisions((snapshot.parent,))
    astrid.packs._check_collisions((snapshot.parent,), selected=selected,
                                  verified_relocations=relocations)
    other = tmp_path / "other"
    (other / "rendering").mkdir(parents=True)
    with pytest.raises(ImportError, match="collides"):
        astrid.packs._check_collisions((snapshot.parent, other), selected=selected,
                                      verified_relocations=relocations)


def test_worker_imports_real_bundled_action_from_verified_snapshot(bundled_snapshot):
    snapshot, selected, relocations = bundled_snapshot
    script = """
import importlib, json, sys
from pathlib import Path
from astrid.core.pack.imports import install_pack_imports
selected, relocations = json.loads(sys.argv[1]), json.loads(sys.argv[2])
root = Path(selected['rendering']['root'])
install_pack_imports('rendering', root, selected_map=selected, verified_relocations=relocations)
action = importlib.import_module('astrid.packs.rendering.actions.timeline_storyboard.run')
assert callable(action.main)
print(action.__file__)
"""
    env = {**os.environ, **selected_pack_environment(selected, verified_relocations=relocations),
           "ASTRID_INTERNAL_INVOCATION": "1"}
    result = subprocess.run([sys.executable, "-c", script, json.dumps(selected), json.dumps(relocations)],
                            env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert Path(result.stdout.strip()).is_relative_to(snapshot)


@pytest.mark.parametrize("verify_digests", [False, True])
def test_verified_relocation_rejects_changed_snapshot(bundled_snapshot, verify_digests):
    snapshot, selected, relocations = bundled_snapshot
    (snapshot / "changed.py").write_text("raise RuntimeError('unadmitted')\n")
    with pytest.raises(ValueError, match="source digest changed"):
        validate_selected_pack_map(selected, verify_digests=verify_digests,
                                   verified_relocations=relocations)


@pytest.mark.parametrize("field", ["original_root", "root", "source_digest", "pack_id"])
def test_verified_relocation_must_match_exact_selection(bundled_snapshot, tmp_path, field):
    _snapshot, selected, relocations = bundled_snapshot
    if field == "pack_id":
        relocations["comfy_wrap"] = relocations.pop("rendering")
    elif field == "original_root":
        shadow = tmp_path / "shadow" / "rendering"
        shadow.mkdir(parents=True)
        relocations["rendering"][field] = str(shadow)
    else:
        relocations["rendering"][field] = "0" * 64
    with pytest.raises(ValueError, match="relocation|collides"):
        validate_selected_pack_map(selected, verified_relocations=relocations)
    with pytest.raises(ValueError, match="relocation|collides"):
        selected_pack_environment(selected, verified_relocations=relocations)
