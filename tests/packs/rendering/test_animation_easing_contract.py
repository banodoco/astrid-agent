"""Keep bundled animation descriptor metadata in sync with its composition package."""

from __future__ import annotations

import json
import re
import tarfile
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
ANIMATION_DESCRIPTORS = (
    REPOSITORY_ROOT / "astrid/packs/rendering/rendering/elements/animations"
)
COMPOSITION_PACKAGE = (
    REPOSITORY_ROOT
    / "remotion/artifacts/banodoco-timeline-composition-0.0.19.tgz"
)

# Implementation audit from the pinned package's bundled component bodies.
# This is recorded for review; descriptor parity is checked against the
# package's declared contract, not inferred from whether a component reads
# the easing prop directly.
EASING_CONSUMPTION_AUDIT = {
    "fade": False,
    "fade-up": True,
    "scale-in": True,
    "slide-left": False,
    "slide-up": False,
    "type-on": True,
}


def _package_animation_meta() -> dict[str, dict[str, object]]:
    with tarfile.open(COMPOSITION_PACKAGE, "r:gz") as package:
        bundled = package.extractfile("package/dist/chunk-BEWVSWLL.js")
        assert bundled is not None
        source = bundled.read().decode("utf-8")

    match = re.search(r"var ANIMATION_META = (\{.*?\n\});", source, re.DOTALL)
    assert match is not None, "pinned composition artifact has no ANIMATION_META"
    return json.loads(match.group(1))


def test_bundled_animation_easing_declarations_match_composition_package() -> None:
    package_meta = _package_animation_meta()
    descriptor_paths = sorted(ANIMATION_DESCRIPTORS.glob("*/element.yaml"))
    descriptors = {}
    for path in descriptor_paths:
        descriptor = json.loads(path.read_text(encoding="utf-8"))
        descriptors[descriptor["id"]] = descriptor["metadata"]

    assert set(descriptors) == set(package_meta) == set(EASING_CONSUMPTION_AUDIT)
    for animation_id, metadata in descriptors.items():
        package_support = package_meta[animation_id].get("supportsEasing")
        descriptor_support = metadata.get("supportsEasing")
        assert descriptor_support == package_support, (
            f"{animation_id}: descriptor declares supportsEasing="
            f"{descriptor_support!r}; composition package declares "
            f"supportsEasing={package_support!r}"
        )
