"""Capability matrix loading for the generic execution host."""

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from astrid.core.execution.host_errors import HostError


def load_matrix(
    path: Path | None, *, ledger: Mapping[str, Any] | None = None,
    load_capability_ledger: Callable[[Path], Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    if path is None:
        return {}
    try:
        payload = ledger if ledger is not None else load_capability_ledger(path)
    except ValueError as exc:
        raise HostError(
            "capability matrix could not be loaded",
            safe_message=f"capability matrix could not be loaded; field=capability_matrix path={path}",
        ) from exc
    if payload.get("schema_version") != 1 or not isinstance(payload.get("capabilities"), list):
        raise HostError(
            "capability matrix requires schema_version 1 and a capabilities list",
            safe_message=f"capability matrix schema or capabilities list is invalid; field=capability_matrix path={path}",
        )
    allowed = {"required", "optional", "unsupported", "retired"}
    result = {}
    for entry in payload["capabilities"]:
        if not isinstance(entry, dict) or not isinstance(entry.get("id"), str):
            raise HostError(
                "capability matrix entries require an id",
                safe_message=f"capability matrix entry is missing an id; field=capabilities path={path}",
            )
        if entry.get("disposition") not in allowed:
            raise HostError(
                f"invalid capability disposition for {entry.get('id')!r}",
                safe_message=f"capability disposition is invalid; field=disposition path={path}",
            )
        if not entry.get("evidence_reason"):
            raise HostError(
                f"capability matrix entry {entry['id']!r} needs evidence_reason",
                safe_message=f"capability evidence reason is missing; field=evidence_reason path={path}",
            )
        if entry["id"] in result:
            raise HostError(
                f"duplicate capability matrix entry {entry['id']!r}",
                safe_message=f"capability matrix has a duplicate id; field=id path={path}",
            )
        result[entry["id"]] = entry
    return result
