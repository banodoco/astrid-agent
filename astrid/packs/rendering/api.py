"""Stable pack-authoring API for custom rendering backends.

This module exposes the frozen rendering protocol DTOs and the public SDK
helpers used by backend authors. Product rendering remains admitted through
the workspace runtime; this API does not expose a direct product-render call.
"""

from __future__ import annotations

import importlib as _importlib

VERSION = (1, 0)

_EXPORTS = {
    "Attachment": ("astrid.core.rendering.contracts", "Attachment"),
    "AudioOwnership": ("astrid.core.rendering.contracts", "AudioOwnership"),
    "FrameWindow": ("astrid.core.rendering.contracts", "FrameWindow"),
    "RenderContext": ("astrid.sdk.rendering", "RenderContext"),
    "RenderProfile": ("astrid.core.rendering.contracts", "RenderProfile"),
    "RenderRequest": ("astrid.core.rendering.contracts", "RenderRequest"),
    "RenderResult": ("astrid.core.rendering.contracts", "RenderResult"),
    "RendererError": ("astrid.core.rendering.contracts", "RendererError"),
    "SupportReport": ("astrid.core.rendering.contracts", "SupportReport"),
    "renderer_main": ("astrid.sdk.rendering", "renderer_main"),
    "support": ("astrid.sdk.rendering", "support"),
}

__all__ = ["VERSION", *sorted(_EXPORTS)]


def __getattr__(name: str):
    try:
        module, attribute = _EXPORTS[name]
    except KeyError:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from None
    return getattr(_importlib.import_module(module), attribute)


def __dir__():
    return sorted(set(globals()) | set(__all__))
