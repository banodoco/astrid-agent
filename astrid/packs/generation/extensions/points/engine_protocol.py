"""Protocol of the generation.engine point (Plan 32 T2a).

requirements(entry, inputs) -> {engine, base, models[name, subdir, url, sha256, size], extra}
align(manifest, mode)       -> mode is "align" | "check" | "trust"
execute(job)                -> files written under job.out
probe(endpoint)             -> optional

Cancellation belongs to the running machine or local ComfyUI target, which owns
the process or submitted prompt identity; engines do not receive that handle.
"""

from __future__ import annotations

from typing import Any, Protocol


class Engine(Protocol):
    __optional__ = ("probe",)

    def requirements(self, entry: Any, inputs: Any) -> dict[str, Any]: ...

    def align(self, manifest: Any, mode: str) -> Any: ...

    def execute(self, job: Any) -> Any: ...

    def probe(self, endpoint: Any) -> Any: ...
