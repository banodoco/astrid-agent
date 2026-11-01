"""Current-frame cache joins for filmstrip receipts and contact tiles."""
from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from fractions import Fraction


def attach_frame_addresses(receipt: dict, snapshot: Mapping, cards: list, *, frame_provider=None) -> dict:
    """``addresses`` on each frame-index row: the layers on screen at that frame, top first (``c08.loras``).

    ``receipt['cards']`` is the compact projection of ``cards`` in the same order (compact_render_receipt)."""
    from .overlays.labels import addresses_at, canvas_of

    config = snapshot.get("config") or {}
    fps = float(Fraction(*snapshot["fps_rational"]))
    canvas = canvas_of(config)
    for row, card in zip(receipt.get("cards") or [], cards):
        frame = int(card["frame"])
        capture = getattr(frame_provider, "scene_for_frame", lambda frame: None)(frame)
        if capture is None:
            row["addresses"] = addresses_at(config, frame, fps, canvas)
        else:
            from .overlays.labels import MIN_OPACITY

            labels, _canvas = capture.labels(frame, source_identity=frame_provider.source_identity)
            row["addresses"] = [item.address for item in labels
                                if item.opacity is None or item.opacity >= MIN_OPACITY]
    return receipt


def _contact_overlay_for_layers(
    snapshot: Mapping, layer_names: Sequence[str], fps: float,
    highlight: str | None = None, *, frame_provider=None,
):
    """Compose the selected bounds and address overlays for contact tiles."""
    selected = set(layer_names or ())
    if not selected.intersection({"bounds", "labels"}):
        return None

    contact_elements = None
    if "bounds" in selected:
        from .motion.sheet import snapshot_elements

        _occurrences, contact_elements = snapshot_elements(snapshot)
    config = snapshot.get("config") if isinstance(snapshot.get("config"), Mapping) else None

    def overlay(image, seconds):
        result = image
        if contact_elements is not None:
            from .layers.builtin import overlay_bounds

            result = overlay_bounds(result, contact_elements, seconds, fps, labels=False, highlight=highlight)
        if "labels" in selected and config is not None:
            from .overlays.labels import overlay_labels_frame

            frame = int(math.floor(float(seconds) * fps + 0.5))
            capture = getattr(frame_provider, "scene_for_frame", lambda frame: None)(frame)
            result = overlay_labels_frame(result, config, frame, fps, capture=capture,
                                          source_identity=getattr(frame_provider, "source_identity", ""))
        return result

    return overlay
