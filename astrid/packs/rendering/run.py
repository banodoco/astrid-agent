#!/usr/bin/env python3
"""Pack-root launcher for rendering raw-command adapters.

Rendering protocol commands execute with their owning pack as the working
directory.  Built-in manifests intentionally keep the portable
``[python3, run.py]`` command, so this launcher bridges that lifecycle to the
implementation stored beside each manifest.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

def _checkout_root() -> Path:
    candidate = Path(__file__).resolve().parent
    while candidate.parent != candidate:
        if (candidate / "astrid").is_dir():
            return candidate
        candidate = candidate.parent
    raise RuntimeError("could not locate the Astrid source package for the raw renderer")


_CHECKOUT_ROOT = _checkout_root()
if str(_CHECKOUT_ROOT) not in sys.path:
    sys.path.insert(0, str(_CHECKOUT_ROOT))


def _request_path(argv: Sequence[str]) -> Path | None:
    try:
        index = argv.index("--request")
        return Path(argv[index + 1])
    except (ValueError, IndexError):
        return None


def _selects_compositor() -> bool:
    """Route the transport-selected layer compositor without shape guessing."""

    return _transport_selected_backend() == "rendering.ffmpeg-compositor"


def _selects_finalizer(argv: Sequence[str]) -> bool:
    """Route finalize and explicitly-namespaced support operations."""

    selected = _transport_selected_backend()
    if selected is not None:
        # The transport-selected backend id is authoritative over request
        # content: a remotion invocation must never route to the finalizer
        # merely because the request carries a finalizer namespace.
        return selected == "rendering.ffmpeg-finalizer"
    if argv and argv[0] == "finalize":
        return True
    if not argv or argv[0] != "support":
        return False
    request_path = _request_path(argv)
    if request_path is None:
        return False
    try:
        payload = json.loads(request_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(payload, Mapping):
        return False
    backend_config = payload.get("backend_config")
    return isinstance(backend_config, Mapping) and ("rendering.ffmpeg-finalizer" in backend_config)


def _transport_selected_backend() -> str | None:
    """The transport sets ASTRID_RENDER_BACKEND to the qualified backend id
    it selected; this is authoritative over any request content."""
    value = os.environ.get("ASTRID_RENDER_BACKEND")
    if isinstance(value, str) and value:
        return value
    return None


def _selects_ffmpeg(argv: Sequence[str]) -> bool:
    """Select FFmpeg from the transport-selected backend id or the request's
    backend-config namespace.

    The launcher never guesses from timeline shape: a shape guess can route a
    Remotion request to FFmpeg or vice versa.
    """

    selected = _transport_selected_backend()
    if selected is not None:
        return selected == "rendering.ffmpeg"
    request_path = _request_path(argv)
    if request_path is None:
        return False
    try:
        payload = json.loads(request_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(payload, Mapping):
        return False
    backend_config = payload.get("backend_config")
    if not isinstance(backend_config, Mapping):
        return False
    if "rendering.ffmpeg" in backend_config:
        return True
    return False


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    selected = _transport_selected_backend()
    if selected is not None and selected not in {
        "rendering.ffmpeg",
        "rendering.remotion",
        "rendering.threejs",
        "rendering.ffmpeg-compositor",
        "rendering.ffmpeg-finalizer",
    }:
        raise ValueError(f"unsupported rendering backend id {selected!r}")
    if _selects_compositor():
        from astrid.packs.rendering.rendering.finalizers.compositor.run import (
            main as backend_main,
        )
    elif _selects_finalizer(args):
        from astrid.packs.rendering.rendering.finalizers.ffmpeg.run import (
            main as backend_main,
        )
    elif _selects_ffmpeg(args):
        from astrid.packs.rendering.rendering.renderers.ffmpeg.run import main as backend_main
    elif selected == "rendering.threejs":
        from astrid.packs.rendering.rendering.renderers.threejs.run import main as backend_main
    else:
        from astrid.packs.rendering.rendering.renderers.remotion.run import main as backend_main

    return backend_main(args)


if __name__ == "__main__":
    raise SystemExit(main())
