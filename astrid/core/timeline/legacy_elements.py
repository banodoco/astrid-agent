"""Built-in timeline element IDs persisted by the Release 13 editor."""

from __future__ import annotations

import json
from pathlib import Path

from astrid.core.pack import ELEMENT_KIND_REGISTRY

# Shared with the Remotion host. The transitions are still offered by the
# editor. The phase IDs describe saved effects without adding picker choices.
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


def uses_editor_incoming_transition(identity: str, *, editor_type: bool) -> bool:
    """Recognize the editor's incoming owner convention, including detached alias copies."""
    return (
        identity in LEGACY_TRANSITION_IDS
        or normalize_legacy_transition_id(identity) != identity
        or (editor_type and identity == "cross-fade")
    )


def is_r13_host_transition(kind: str, identity: str, source: str) -> bool:
    """Identify rendering-pack transitions drawn by the host's incoming layer."""
    return kind == "transitions" and source == "pack:rendering" and identity in LEGACY_TRANSITION_IDS
