"""PK-10 (new): the element catalog of a core-only tree.

A tree holding only the four core packs (no optional packs, no ``local``, no
``astrid_motion``) still yields a working element catalog, served entirely by
the core ``rendering`` pack. Nothing from ``local`` or ``astrid_motion`` is
listed, even though the source checkout still ships them.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from tests.stage1.test_core_pack_boundary import _CORE, _core_tree

ROOT = Path(__file__).resolve().parents[3]
EXCLUDED = ("local", "astrid_motion")


def _element_ids(pack: str) -> set[str]:
    base = ROOT / "astrid" / "packs" / pack
    ids = set()
    for manifest in base.rglob("element.yaml"):
        if "__pycache__" in manifest.parts:
            continue
        ids.add(manifest.parent.name)
    return ids


def test_core_only_catalog_is_served_by_rendering_and_excludes_local_and_astrid_motion(tmp_path):
    root = _core_tree(tmp_path)
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    runtime = Path(os.environ["ASTRID_RUNTIME_CHECKOUT"])
    env = {key: os.environ[key] for key in ("PATH", "TMPDIR", "SYSTEMROOT", "LANG") if key in os.environ}
    env.update(PYTHONPATH=os.pathsep.join((str(root), str(runtime))), HOME=str(home),
               ASTRID_RUNTIME_CHECKOUT=str(runtime), ASTRID_PACKS_PATH="", PYTHONDONTWRITEBYTECODE="1")
    script = r'''
import json, pathlib, sys
root = pathlib.Path.cwd()
import astrid
assert pathlib.Path(astrid.__file__).is_relative_to(root), astrid.__file__
from astrid.core.element import catalog
from astrid.core.element.registry import load_default_registry
from astrid.core.pack.discovery import discover_pack_metadata
packs = sorted(p.id for p in discover_pack_metadata(project_root=root))
registry = load_default_registry(project_root=root)
sources = sorted({item.source for kind in ("effects", "animations", "transitions") for item in registry.list(kind=kind)})
print(json.dumps({"packs": packs, "sources": sources,
                  "effects": sorted(catalog.list_effect_ids()),
                  "animations": sorted(item.id for item in registry.list(kind="animations")),
                  "transitions": sorted(item.id for item in registry.list(kind="transitions")),
                  "modules": sorted(n for n in sys.modules if n.startswith(("astrid.packs.local", "astrid.packs.astrid_motion")))}))
'''
    result = subprocess.run([sys.executable, "-c", script], cwd=root, env=env, text=True,
                            capture_output=True, timeout=180)
    assert result.returncode == 0, result.stderr[-4000:]
    proof = json.loads(result.stdout.strip().splitlines()[-1])
    # Core Hivemind (astrid/core/hivemind) is core-owned, not an optional pack (S13).
    assert set(proof["packs"]) <= set(_CORE) | {"hivemind"}
    assert not set(proof["packs"]) & set(EXCLUDED)
    assert proof["sources"] == ["pack:rendering"]
    assert "text-card" in proof["effects"]
    assert {"fade", "type-on"} <= set(proof["animations"])
    assert "cross-fade" in proof["transitions"]
    excluded_ids = set().union(*(_element_ids(pack) for pack in EXCLUDED)) - _element_ids("rendering")
    # Once PK-12/PK-15 remove these packs from the checkout there is nothing
    # left to compare; the pack/source assertions above still hold.
    listed = set(proof["effects"]) | set(proof["animations"]) | set(proof["transitions"])
    assert not listed & excluded_ids
    assert proof["modules"] == []
