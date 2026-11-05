"""The Personal pack keeps current cards while declaring canonical visual inputs."""

from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[3]
PACK = ROOT / "astrid" / "packs" / "local"


def _yaml(path: Path) -> dict:
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def test_personal_pack_declares_helper_and_canonical_end_spanning_resources():
    pack = _yaml(PACK / "pack.yaml")
    rendering = pack["rendering"]
    animated = rendering["effects/animated-media-transform"]
    end_spanning = rendering["effects/end-spanning-layer"]

    assert pack["name"] == "Personal"
    assert "shared/readiness-image.tsx" in {row["path"] for row in animated["resources"]}
    paths = {row["path"] for row in end_spanning["resources"]}
    assert "rendering/elements/effects/end-spanning-layer/timing.ts" in paths
    assert {
        "rendering/elements/effects/end-spanning-layer/assets/card-0.png",
        "rendering/elements/effects/end-spanning-layer/assets/card-0-canonical-mink.png",
        "rendering/elements/effects/end-spanning-layer/assets/card-4.png",
        "rendering/elements/effects/end-spanning-layer/assets/card-4-canonical-mink.png",
        "rendering/elements/effects/end-spanning-layer/assets/card-5.png",
        "rendering/elements/effects/end-spanning-layer/assets/card-5-canonical-mink.png",
    } <= paths


def test_end_spanning_schema_and_canvas_adapter_are_declared():
    element = _yaml(PACK / "rendering/elements/effects/end-spanning-layer/element.yaml")
    properties = element["schema"]["properties"]
    assert {"cardAssets", "revealDelaySeconds", "revealDurationSeconds"} <= set(properties)
    assert element["runtime"] == {"adapter": "remotion"}

    component = (PACK / "rendering/elements/effects/end-spanning-layer/component.tsx").read_text(encoding="utf-8")
    assert "useVideoConfig" in component
    assert "fitEndSpanningCanvas" in component
    assert "END_SPANNING_CANVAS" in component


def test_animated_media_uses_the_declared_local_readiness_helper():
    component = (PACK / "rendering/elements/effects/animated-media-transform/component.tsx").read_text(encoding="utf-8")
    assert "../../../../shared/readiness-image" in component
    assert (PACK / "shared/readiness-image.tsx").is_file()
