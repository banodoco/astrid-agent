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
def bundled_snapshot(tmp_path, monkeypatch):
    monkeypatch.setenv("ASTRID_RUNTIME_DATA_ROOT", str(tmp_path / "runtime-data"))
    from types import SimpleNamespace
    from astrid.core.execution.generic_host import _portable_pack_capture, _selected_pack_generation

    original = Path(__file__).resolve().parents[3] / "astrid" / "packs" / "rendering"
    record = SimpleNamespace(definition=SimpleNamespace(metadata={
        "source_pack": "rendering", "pack_root": str(original)}))
    with _portable_pack_capture(_selected_pack_generation(record)) as captured:
        snapshot = captured[2]
        selected = {"rendering": {"root": str(snapshot), "source_digest": pack_digest(snapshot)}}
        relocations = {"rendering": {"original_root": str(original), **selected["rendering"]}}
        yield snapshot, selected, relocations


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


@pytest.fixture
def personal_snapshot(tmp_path, monkeypatch):
    monkeypatch.setenv("ASTRID_RUNTIME_DATA_ROOT", str(tmp_path / "runtime-data"))
    from types import SimpleNamespace
    import astrid.packs
    from astrid.personal_packs_root import resolve_personal_packs_root
    from astrid.core.execution.generic_host import _portable_pack_capture, _selected_pack_generation

    collection = resolve_personal_packs_root()
    monkeypatch.setattr(astrid.packs, "_PERSONAL_ROOT", collection)
    original = collection / "personal"
    original.mkdir(parents=True)
    (original / "action.py").write_text("VALUE = 'personal'\n")
    record = SimpleNamespace(definition=SimpleNamespace(metadata={
        "source_pack": "personal", "pack_root": str(original)}))
    with _portable_pack_capture(_selected_pack_generation(record)) as captured:
        snapshot = captured[2]
        selected = {"personal": {"root": str(snapshot), "source_digest": pack_digest(snapshot)}}
        relocations = {"personal": {"original_root": str(original), **selected["personal"]}}
        yield original, snapshot, selected, relocations


def test_namespace_accepts_exact_canonical_personal_relocation(personal_snapshot, tmp_path):
    import astrid.packs

    original, snapshot, selected, relocations = personal_snapshot
    with pytest.raises(ImportError, match="reserved personal"):
        astrid.packs._check_collisions((snapshot.parent,))
    astrid.packs._check_collisions((original.parent, snapshot.parent), selected=selected,
                                  verified_relocations=relocations)
    shadow = tmp_path / "shadow"
    (shadow / "personal").mkdir(parents=True)
    with pytest.raises(ImportError, match="reserved personal"):
        astrid.packs._check_collisions((shadow,), selected=selected, verified_relocations=relocations)


def test_personal_snapshot_keeps_original_collection_identity(personal_snapshot):
    original, _snapshot, selected, relocations = personal_snapshot
    env = selected_pack_environment(selected, verified_relocations=relocations)
    assert env["ASTRID_PACKS_PATH"] == str(original.parent)
    assert json.loads(env["ASTRID_SELECTED_PACK_MAP"]) == selected


@pytest.mark.parametrize("verify_digests", [False, True])
def test_personal_relocation_always_rechecks_digest(personal_snapshot, verify_digests):
    _original, snapshot, selected, relocations = personal_snapshot
    (snapshot / "action.py").write_text("VALUE = 'forged'\n")
    with pytest.raises(ValueError, match="source digest changed"):
        validate_selected_pack_map(selected, verify_digests=verify_digests, verified_relocations=relocations)


@pytest.mark.parametrize("forgery", ["original", "selection", "digest", "reused", "outside", "public"])
def test_forged_personal_relocation_is_rejected(personal_snapshot, tmp_path, forgery):
    from runtime_protocol import platform
    import astrid.packs

    _original, snapshot, selected, relocations = personal_snapshot
    if forgery == "original":
        forged = tmp_path / "shadow" / "personal"
        forged.mkdir(parents=True)
        relocations["personal"]["original_root"] = str(forged)
    elif forgery == "selection":
        relocations["personal"]["root"] = str(tmp_path / "personal")
    elif forgery == "digest":
        relocations["personal"]["source_digest"] = "0" * 64
    elif forgery == "reused":
        # Reusing a different capture cannot authorize the new selection.
        other = tmp_path / "other-capture" / "personal"
        platform.private_directory(other.parent)
        shutil.copytree(snapshot, other)
        selected["personal"]["root"] = str(other)
    elif forgery == "outside":
        other = tmp_path / "outside" / "personal"
        platform.private_directory(other.parent)
        shutil.copytree(snapshot, other)
        selected["personal"]["root"] = relocations["personal"]["root"] = str(other)
    else:
        if sys.platform == "win32":
            pytest.skip("POSIX permission forgery; native Windows DACL is checked by platform")
        snapshot.parent.chmod(0o755)
    with pytest.raises(ValueError, match="relocation|personal|snapshot"):
        validate_selected_pack_map(selected, verified_relocations=relocations)
    with pytest.raises(ImportError, match="invalid verified relocation"):
        astrid.packs._check_collisions((snapshot.parent,), selected=selected, verified_relocations=relocations)
