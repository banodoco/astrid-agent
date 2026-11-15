"""Access the executable spec in a Runtime task response."""

from collections.abc import Mapping
from typing import Any


def task_executable_spec(task: Mapping[str, Any]) -> Mapping[str, Any] | None:
    envelope = task.get("spec")
    spec = envelope.get("spec") if isinstance(envelope, Mapping) else None
    return spec if isinstance(spec, Mapping) else None
