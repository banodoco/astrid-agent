"""Declaration-only clip roles for structural editing.

``role_of`` is a pure query over supplied declaration metadata. ``element_coverage``
reads only local element manifests and caches their metadata per process. Neither
query imports the scene evaluator, renderer owner, compiler, or element registry.
The historical track convention and narrow frozen defaults remain compatible;
these roles do not establish pixel visibility, bounds, or opaque coverage.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

_PACKS_ROOT = Path(__file__).resolve().parents[2] / "packs"
_COVERAGE_CACHE: dict[str, dict] = {}
# Sealed S0's V3 rendering-element declarations omit these metadata values,
# though the frozen donor declarations used them to determine frame coverage.
# Preserve this narrow compatibility projection until those unowned declarations
# are joined; a declaration present in the V3 manifest always takes priority.
_COVERAGE_COMPAT = {
    "am-snap-plate": {"coverage": "picture", "background_unless": ["asset", "params.src"]},
    "am-footage": {"coverage": "picture"},
    "am-presenter": {"coverage": "picture"},
}


def element_coverage(element_ids: Iterable[str]) -> dict[str, dict]:
    """``metadata.coverage`` (+ ``background_unless``) per element id; ``{}`` when an element declares none.

    Reads each element's ``element.yaml`` directly (cached per process): loading the whole element
    registry cost 2-4 s per CLI process, and check() runs on every edit.
    """
    return {element_id: _coverage_of(element_id) for element_id in element_ids}


def _coverage_of(
    element_id: str, *, packs_root: Path | None = None, cache: dict[str, dict] | None = None,
) -> dict:
    # Optional roots/cache retain the historical onscreen test and caller seam.
    packs_root = _PACKS_ROOT if packs_root is None else packs_root
    cache = _COVERAGE_CACHE if cache is None else cache
    if element_id in cache:
        return cache[element_id]
    import yaml

    found: dict = {}
    # Receiving V3 packs put renderer-owned manifests under
    # ``<pack>/rendering/elements``; older packs use ``<pack>/elements``.
    paths = set(packs_root.glob(f"*/elements/**/{element_id}/element.yaml"))
    paths.update(packs_root.glob(f"*/rendering/elements/**/{element_id}/element.yaml"))
    for path in sorted(paths):
        try:
            doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except Exception:  # noqa: BLE001 - an unreadable manifest just means "declares nothing"
            continue
        meta = doc.get("metadata") if isinstance(doc.get("metadata"), Mapping) else {}
        if meta.get("coverage") is not None:
            found = {"coverage": meta["coverage"]}
            if "background_unless" in meta:
                found["background_unless"] = list(meta["background_unless"] or [])
        break
    if not found and element_id in _COVERAGE_COMPAT:
        found = dict(_COVERAGE_COMPAT[element_id])
    cache[element_id] = found
    return found


def _has_value(clip: Mapping[str, Any], path: str) -> bool:
    node: Any = clip
    for part in path.split("."):
        if not isinstance(node, Mapping) or part not in node:
            return False
        node = node[part]
    return node is not None and node != ""


def role_of(clip: Mapping[str, Any], track: Any, elements: Mapping[str, Mapping[str, Any]]) -> str:
    """picture | background | content | none, from the element's declared coverage, else the track convention."""
    meta = elements.get(clip.get("clipType")) or {}
    coverage = meta.get("coverage")
    if coverage is not None:
        if coverage == "picture" and "background_unless" in meta:
            unless = meta.get("background_unless") or []
            if not any(_has_value(clip, path) for path in unless):
                return "background"
        return str(coverage)
    track_id = track if isinstance(track, str) else (track or {}).get("id")
    return "picture" if track_id == "plate" else "content"


