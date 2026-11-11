"""Wan2GP contribution to generation.engine: carries its recipe row; execution is not wired yet."""

from __future__ import annotations

from typing import Any


class Wan2GPEngine:
    name = "wan2gp"

    def requirements(self, entry: Any, inputs: Any) -> dict[str, Any]:
        raise NotImplementedError("generation.engine wan2gp is not wired to a caller yet")

    def align(self, manifest: Any, mode: str) -> Any:
        raise NotImplementedError("generation.engine wan2gp is not wired to a caller yet")

    def execute(self, job: Any) -> Any:
        raise NotImplementedError("generation.engine wan2gp is not wired to a caller yet")



ENGINE = Wan2GPEngine()
