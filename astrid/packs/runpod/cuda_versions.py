"""The default CUDA host allow-list for RTX 5090 launches.

The claim script, the H3 release provider and the 5090 watcher import it from
here, so the recipe minimum and the newest 13.x minor are changed in one place.
"""

from __future__ import annotations

# The recipe requires CUDA 13.0 or newer. RunPod matches the allow-list exactly, so every
# 13.x minor from that minimum through the newest one below must be listed explicitly.
RECIPE_MINIMUM_CUDA_VERSION = "13.0"
NEWEST_CUDA_13X_MINOR = 3


def _cuda_versions_from_minimum(minimum: str, newest_minor: int) -> tuple[str, ...]:
    major, _, minor = minimum.partition(".")
    return tuple(f"{major}.{n}" for n in range(int(minor), newest_minor + 1))


DEFAULT_ALLOWED_CUDA_VERSIONS = _cuda_versions_from_minimum(
    RECIPE_MINIMUM_CUDA_VERSION, NEWEST_CUDA_13X_MINOR
)
