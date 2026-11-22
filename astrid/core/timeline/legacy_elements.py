"""Built-in timeline element IDs persisted by the Release 13 editor."""

from __future__ import annotations

import json
from pathlib import Path

from astrid.core.pack import ELEMENT_KIND_REGISTRY

# Shared with the Remotion host. These IDs describe saved renderer behavior
# and do not extend the authoring picker catalogs.
_R13_ELEMENTS = json.loads(
    Path(__file__).with_name("schema").joinpath("r13-legacy-elements.json").read_text(encoding="utf-8"),
)
LEGACY_TRANSITION_IDS: frozenset[str] = frozenset(_R13_ELEMENTS["transitionIds"])
LEGACY_PHASE_EFFECT_IDS: dict[str, frozenset[str]] = {
    slot: frozenset(ids) for slot, ids in _R13_ELEMENTS["phaseEffectIds"].items()
}

# The timeline-kind registry already declares the R13 spelling "crossfade".
_CROSS_FADE_ALIASES = ELEMENT_KIND_REGISTRY.descriptor("cross-fade", catalog="transition").aliases


def normalize_legacy_transition_id(identity: str) -> str:
    """Return the canonical spelling for the R13 cross-fade alias."""
    return "cross-fade" if identity in _CROSS_FADE_ALIASES else identity
