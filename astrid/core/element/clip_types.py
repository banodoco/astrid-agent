"""Shared clip-type aliases for installed rendering effects.

Callers supply the effective inventory, so aliases never admit an absent provider.
Exact effect IDs take precedence over aliases at resolution time.
"""
from collections.abc import Mapping
from typing import Any


def clip_type_aliases(effects: Mapping[str, Mapping[str, Any]]) -> dict[str, str]:
    """Map supported clip spellings to the winning installed effect IDs."""
    aliases = {"text": "text-card"} if "text-card" in effects else {}
    for effect_id, metadata in effects.items():
        raw_aliases = metadata.get("clipTypeAliases")
        if isinstance(raw_aliases, list):
            for alias in raw_aliases:
                if isinstance(alias, str) and alias:
                    aliases[alias] = effect_id
    return aliases
