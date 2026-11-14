"""Shared helpers for the generation and machines CLIs."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def _gpu_placement(placement, gpu_types, parser):
    """Choose GPUs for a claim quote."""
    from astrid.core.generation.placement import Placement, is_runpod_placement, request_from_selector

    if not gpu_types:
        return placement
    if not is_runpod_placement(placement):
        parser.error("--gpu requires --placement runpod")
    if any(not gpu.strip() for gpu in gpu_types):
        parser.error("--gpu values must be nonempty GPU type names")
    request = request_from_selector(placement)
    kind = request.kind if isinstance(request, Placement) else request["kind"]
    return {"kind": kind, "policy": {"gpu_types": list(gpu_types)}}


def _when(value: Any) -> str:
    if not isinstance(value, (int, float)):
        return "-"
    return datetime.fromtimestamp(value, timezone.utc).strftime("%Y-%m-%d %H:%M")
