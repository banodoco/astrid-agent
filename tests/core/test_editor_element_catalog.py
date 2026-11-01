"""Coverage for the deterministic editor-facing Astrid element projection."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from astrid.core.element import catalog
from astrid.core.element.registry import load_default_registry

ROOT = Path(__file__).resolve().parents[2]
GENERATOR = ROOT / "scripts" / "gen_element_catalog.py"


def test_runtime_projection_uses_registry_and_shipped_projection_is_core_only() -> None:
    descriptors = catalog.list_element_descriptors()
    by_kind = {
        kind: {descriptor["id"] for descriptor in descriptors if descriptor["kind"] == kind}
        for kind in ("effect", "animation", "transition")
    }
    registry = load_default_registry()

    assert by_kind["effect"] == {item.id for item in registry.list(kind="effects")}
    assert by_kind["animation"] == {item.id for item in registry.list(kind="animations")}
    assert by_kind["transition"] == {item.id for item in registry.list(kind="transitions")}
    assert {"scrolling-guide", "audio-reactive-colour"}.issubset(by_kind["effect"])
    shipped = (ROOT / "astrid/packs/rendering/elements/catalog.ts").read_text(encoding="utf-8")
    assert '"packId": "rendering"' in shipped
    assert '"packId": "local"' not in shipped
    assert '"packId": "astrid_motion"' not in shipped


def test_projection_is_path_free_and_revision_stable() -> None:
    first = catalog.list_element_descriptors()
    second = catalog.list_element_descriptors()

    assert first == second
    assert all(descriptor["revision"].startswith("sha256:") for descriptor in first)
    assert all("/Users/" not in repr(descriptor) for descriptor in first)
    duration = next(item for item in first if item["id"] == "cross-fade")
    assert duration["parameters"] == [
        {
            "name": "durationFrames",
            "label": "Duration Frames",
            "description": "",
            "type": "number",
            "default": 8,
            "min": 1,
        }
    ]


def test_checked_in_typescript_projection_is_current() -> None:
    result = subprocess.run(
        [sys.executable, str(GENERATOR), "--check"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
