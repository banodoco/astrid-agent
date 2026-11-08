"""ComfyUI contribution to generation.engine (Plan 32 T2a).

Declared and loadable only: no production caller uses it yet, so every method refuses
until a later task wires it to the existing ComfyUI path.
"""

from __future__ import annotations

from typing import Any


class ComfyUIEngine:
    def requirements(self, entry: Any, inputs: Any) -> dict[str, Any]:
        raise NotImplementedError("generation.engine comfyui is not wired to a caller yet")

    def align(self, manifest: Any, mode: str) -> Any:
        raise NotImplementedError("generation.engine comfyui is not wired to a caller yet")

    def execute(self, job: Any) -> Any:
        raise NotImplementedError("generation.engine comfyui is not wired to a caller yet")

    def cancel(self, job: Any) -> Any:
        raise NotImplementedError("generation.engine comfyui is not wired to a caller yet")


ENGINE = ComfyUIEngine()
