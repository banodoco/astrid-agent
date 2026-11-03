"""Static element metadata used to classify authored clips on screen.

This is deliberately separate from the frame evaluator: it reads only the
coverage declarations needed to assign a clip's role. Registry discovery and
the compatibility declarations retain the semantics of the legacy projection.
"""

from __future__ import annotations

import sys
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

_PACKS_ROOT = Path(__file__).resolve().parents[2] / "packs"
_COVERAGE_CACHE: dict[str, dict[str, Any]] = {}
# Sealed S0's V3 rendering-element declarations omit these metadata values,
# though the frozen donor declarations used them to determine frame coverage.
# Keep this narrow semantic projection here until those unowned declarations
# are joined; a declaration present in the V3 manifest always takes priority.
_COVERAGE_COMPAT: dict[str, dict[str, Any]] = {
    # Generic media fills the canvas on any track; clearing its asset leaves an empty background.
    "media": {"coverage": "picture", "background_unless": ["asset"]},
    "am-snap-plate": {"coverage": "picture", "background_unless": ["asset", "params.src"]},
    "am-footage": {"coverage": "picture"},
    "am-presenter": {"coverage": "picture"},
}


def element_coverage(element_ids: Iterable[str]) -> dict[str, dict]:
    """``metadata.coverage`` (+ ``background_unless``) per element id; ``{}`` when an element declares none.

    Reads each element's ``element.yaml`` directly (cached per process): loading the whole element
    registry cost 2-4 s per CLI process, and check() runs on every edit.
    """
    # ``onscreen`` historically owned the two patch points below. Respect
    # overrides left by callers while they migrate to this helper.
    legacy = sys.modules.get("astrid.core.timeline.onscreen")
    if legacy is None:
        return {element_id: _coverage_of(element_id) for element_id in element_ids}
    global _PACKS_ROOT, _COVERAGE_CACHE, _COVERAGE_COMPAT
    original = _PACKS_ROOT, _COVERAGE_CACHE, _COVERAGE_COMPAT
    legacy_root = getattr(legacy, "_PACKS_ROOT", _PACKS_ROOT)
    legacy_cache = getattr(legacy, "_COVERAGE_CACHE", _COVERAGE_CACHE)
    legacy_compat = getattr(legacy, "_COVERAGE_COMPAT", _COVERAGE_COMPAT)
    if legacy_root is getattr(legacy, "_LEGACY_PACKS_ROOT", legacy_root):
        legacy_root = _PACKS_ROOT
    if legacy_cache is getattr(legacy, "_LEGACY_COVERAGE_CACHE", legacy_cache):
        legacy_cache = _COVERAGE_CACHE
    if legacy_compat is getattr(legacy, "_LEGACY_COVERAGE_COMPAT", legacy_compat):
        legacy_compat = _COVERAGE_COMPAT
    _PACKS_ROOT = legacy_root
    _COVERAGE_CACHE = legacy_cache
    _COVERAGE_COMPAT = legacy_compat
    try:
        return {element_id: _coverage_of(element_id) for element_id in element_ids}
    finally:
        _PACKS_ROOT, _COVERAGE_CACHE, _COVERAGE_COMPAT = original


def _coverage_of(element_id: str) -> dict:
    if element_id in _COVERAGE_CACHE:
        return _COVERAGE_CACHE[element_id]
    import yaml

    found: dict = {}
    # Receiving V3 packs put renderer-owned manifests under
    # ``<pack>/rendering/elements``; older packs use ``<pack>/elements``.
    paths = set(_PACKS_ROOT.glob(f"*/elements/**/{element_id}/element.yaml"))
    paths.update(_PACKS_ROOT.glob(f"*/rendering/elements/**/{element_id}/element.yaml"))
    for path in sorted(paths):
        try:
            doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except Exception:  # noqa: BLE001 - an unreadable manifest just means "declares nothing"
            continue
        raw_meta = doc.get("metadata")
        meta: Mapping[str, Any] = raw_meta if isinstance(raw_meta, Mapping) else {}
        if meta.get("coverage") is not None:
            found = {"coverage": meta["coverage"]}
            if "background_unless" in meta:
                found["background_unless"] = list(meta["background_unless"] or [])
        break
    if not found and element_id in _COVERAGE_COMPAT:
        found = dict(_COVERAGE_COMPAT[element_id])
    _COVERAGE_CACHE[element_id] = found
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
    clip_type = clip.get("clipType")
    meta = {} if clip_type is None else elements.get(clip_type) or {}
    coverage = meta.get("coverage")
    if coverage is not None:
        if coverage == "picture" and "background_unless" in meta:
            unless = meta.get("background_unless") or []
            if not any(_has_value(clip, path) for path in unless):
                return "background"
        return str(coverage)
    track_id = track if isinstance(track, str) else (track or {}).get("id")
    return "picture" if track_id == "plate" else "content"
