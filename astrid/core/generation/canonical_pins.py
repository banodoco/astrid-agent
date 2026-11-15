"""The one declaration of the engine pins: ``astrid/packs/runpod/pins.json``.

VibeComfy, ComfyUI and runpod-lifecycle are declared there and nowhere else
that the Agent can read at runtime. The RunPod recipe binds the file by digest,
so reading it through that binding is the single point where a pin is resolved.
Files that must stay literal (``pyproject.toml``, the second pin file, the App
install manifest) are checked against it by
``tests/core/generation/test_pin_single_source.py``; the App checks the manifest
with ``scripts/installer/pin.mjs --check``, which reads this same file.
"""
from __future__ import annotations

import json
from typing import Any

from .engine_recipe import _read_ref, load_recipe

CANONICAL_PINS_PATH = "astrid/packs/runpod/pins.json"


def canonical_pin(name: str) -> dict[str, Any]:
    """Return ``{"repository", "revision"[, "version"]}`` for one named source."""
    recipe = load_recipe("comfyui", "linux-x86_64-cu130")
    _, payload = _read_ref(recipe.pins)  # digest-verified read of the canonical file
    return dict(json.loads(payload)["sources"][name])
