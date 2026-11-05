"""PK-12: the retired ``local`` effects no longer ship; a personal copy still works.

Rewritten from the shipped-``local`` assertions (Plan 06 PK-12, Decision 8).
Before: "the end-spanning card effect ships in the wheel and is discovered
under ``local``". Now: it is absent from the wheel inputs and from discovery,
and the same bytes restored from Git history (the archive's parent commit)
into a ``tmp_path`` personal pack are discovered under ``personal`` and are
selected by the Remotion renderer for a timeline that uses the effect.
"""
from __future__ import annotations

import io
import re
import subprocess
import tarfile
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
ARCHIVE = ROOT / "docs" / "packs" / "local-effects-archive.md"
EFFECT = "end-spanning-layer"


def _parent_commit() -> str:
    match = re.search(r"Parent commit \(last commit that contains them\): `([0-9a-f]{40})`",
                      ARCHIVE.read_text(encoding="utf-8"))
    assert match, "archive names no parent commit"
    return match.group(1)


def test_local_pack_is_absent_from_the_tree_wheel_inputs_and_discovery() -> None:
    from astrid.core.pack.discovery import discover_pack_metadata

    assert not (ROOT / "astrid" / "packs" / "local").exists()
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert "packs/local" not in pyproject
    assert "local" not in {row.id for row in discover_pack_metadata(project_root=ROOT)}
    archive = ARCHIVE.read_text(encoding="utf-8")
    assert f"astrid/packs/local/rendering/elements/effects/{EFFECT}/component.tsx" in archive


def test_personal_copy_of_the_retired_effect_is_discovered_and_selected_for_render(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from astrid.core.element.registry import clear_default_registry_cache, load_default_registry
    from astrid.core.pack.discovery import discover_pack_metadata
    from astrid.core.pack.personal import ensure_personal_pack
    from astrid.packs.rendering.rendering.renderers.remotion.run import _timeline_element_selection

    parent = _parent_commit()
    if subprocess.run(["git", "-C", str(ROOT), "cat-file", "-e", f"{parent}^{{commit}}"],
                      capture_output=True).returncode != 0:
        pytest.skip(f"archive parent {parent} is not in this clone")
    home = tmp_path / "home"
    collection = home / "Astrid" / "packs"
    collection.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("ASTRID_PACKS_PATH", raising=False)
    personal = ensure_personal_pack(collection)

    prefix = "astrid/packs/local/"
    data = subprocess.check_output(["git", "-C", str(ROOT), "archive", parent,
                                    f"{prefix}rendering/elements/effects/{EFFECT}"])
    with tarfile.open(fileobj=io.BytesIO(data)) as archive:
        for member in archive.getmembers():
            if not member.isfile():
                continue
            target = personal / member.name[len(prefix):]
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(archive.extractfile(member).read())
    # The descriptor names its owning pack; a restored copy is owned by personal.
    descriptor = personal / "rendering" / "elements" / "effects" / EFFECT / "element.yaml"
    descriptor.write_text(descriptor.read_text(encoding="utf-8").replace("\npack_id: local\n", "\npack_id: personal\n"),
                          encoding="utf-8")
    old_manifest = yaml.safe_load(subprocess.check_output(
        ["git", "-C", str(ROOT), "show", f"{parent}:{prefix}pack.yaml"]))
    manifest = yaml.safe_load((personal / "pack.yaml").read_text(encoding="utf-8"))
    manifest["rendering"] = {f"effects/{EFFECT}": old_manifest["rendering"][f"effects/{EFFECT}"]}
    (personal / "pack.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")
    clear_default_registry_cache()

    rows = {row.id: row for row in discover_pack_metadata(project_root=ROOT)}
    assert "local" not in rows
    assert Path(rows["personal"].pack.root).resolve() == personal.resolve()
    registry = load_default_registry(project_root=ROOT, extra_pack_roots=(str(personal),))
    element = registry.get("effects", EFFECT)
    assert element.root.resolve().is_relative_to(personal.resolve())
    config = {"clips": [{"id": "end", "clipType": EFFECT, "at": 0, "hold": 4, "track": "v"}]}
    _resolved, effects, _, _ = _timeline_element_selection(config, registry=registry)
    selected = list(effects.values())
    assert [item.id for item in selected] == [EFFECT]
    assert registry._pack_id(selected[0]) == "personal"
    clear_default_registry_cache()
