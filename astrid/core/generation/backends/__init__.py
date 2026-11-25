"""Generation backend adapters — local, cloud, and Codex.

Backend dispatch goes through this package.  The executor imports
the adapter classes from here and calls ``.generate()`` without any
backend-specific branching.
"""

from typing import TYPE_CHECKING

from astrid.core.generation.backends.base import BackendAdapter, GenerationResult
from astrid.core.generation.backends.registry import (
    GenerationBackendDescriptor,
    GenerationBackendRegistry,
    discover_generation_backend_descriptors,
    load_default_generation_backend_registry,
)

if TYPE_CHECKING:
    from astrid.core.generation.backends.codex import CodexBackend
    from astrid.core.generation.backends.fal import FalBackend

# Load provider adapters only when their public names are requested. In
# particular, importing a Comfy submodule must not load Codex or fal.

__all__ = [
    "BackendAdapter",
    "VibeComfyEngine",
    "discover_generation_backend_descriptors",
    "FalBackend",
    "GenerationBackendDescriptor",
    "GenerationBackendRegistry",
    "GenerationResult",
    "load_default_generation_backend_registry",
    "VibeComfyBackend",
    "WavespeedBackend",
]


def __getattr__(name: str):
    if name == "CodexBackend":
        from astrid.core.generation.backends.codex import CodexBackend

        globals()[name] = CodexBackend
        return CodexBackend
    if name == "FalBackend":
        from astrid.core.generation.backends.fal import FalBackend

        globals()[name] = FalBackend
        return FalBackend
    if name in {"VibeComfyEngine", "VibeComfyBackend"}:
        from astrid.core.generation.backends.vibecomfy import (
            VibeComfyBackend,
            VibeComfyEngine,
        )

        return {
            "VibeComfyEngine": VibeComfyEngine,
            "VibeComfyBackend": VibeComfyBackend,
        }[name]
    if name == "WavespeedBackend":
        try:
            from astrid.core.generation.backends.wavespeed import WavespeedBackend

            return WavespeedBackend
        except ModuleNotFoundError as exc:
            raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from exc
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    # Keep the formerly eager public names discoverable without loading them.
    return sorted({*globals(), "CodexBackend", "FalBackend"})
