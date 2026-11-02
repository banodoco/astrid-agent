"""Build universal result receipts for the pack's command-backed actions."""

from __future__ import annotations

import mimetypes
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from astrid.pack_api import build_manifest, write_manifest


def write_action_receipt(
    output_root: str | Path,
    *,
    kind: str,
    inputs: Mapping[str, Any],
    output_paths: Mapping[str, str],
    primary_port: str | None = None,
    result_ports: Sequence[str] = (),
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Write a receipt whose names match the action's declared output ports.

    Output paths are relative to the assigned action directory and can name
    files or directories. Directory entries retain one declared port name in
    the universal receipt; files outside these named outputs remain local
    attempt evidence and are not published as undeclared ports.
    """

    root = Path(output_root).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    if primary_port is not None and primary_port not in output_paths:
        raise ValueError("the primary port must be present in output_paths")
    declared_results = set(result_ports)
    if primary_port is not None:
        declared_results.add(primary_port)
    if not declared_results.issubset(output_paths):
        raise ValueError("result_ports must be present in output_paths")
    outputs: list[dict[str, Any]] = []
    seen_paths: set[str] = set()
    for name, relative in sorted(output_paths.items()):
        if (type(name) is not str or not name or type(relative) is not str or not relative
                or Path(relative).is_absolute() or "\\" in relative
                or any(part in {"", ".", ".."} for part in relative.split("/"))):
            raise ValueError("output port names and paths must be safe relative identities")
        candidate = root.joinpath(*relative.split("/"))
        if candidate.is_symlink():
            raise ValueError(f"declared output {name!r} is a symlink")
        resolved = candidate.resolve()
        if not resolved.is_relative_to(root):
            raise ValueError(f"declared output {name!r} escapes the action directory")
        if not candidate.exists():
            continue
        if not (candidate.is_file() or candidate.is_dir()):
            raise ValueError(f"declared output {name!r} is not a file or directory")
        if relative == "manifest.json":
            raise ValueError("manifest.json is the receipt, not an action output")
        if relative in seen_paths:
            raise ValueError(f"action output path {relative!r} is declared more than once")
        seen_paths.add(relative)
        is_primary = name == primary_port
        row: dict[str, Any] = {
            "name": name,
            "path": relative,
            "type": "directory" if candidate.is_dir() else "file",
            "role": "result" if name in declared_results else "auxiliary",
            "is_primary": is_primary,
        }
        media_type, _encoding = mimetypes.guess_type(relative)
        if candidate.is_file() and media_type:
            row["media_type"] = media_type
        outputs.append(row)
    if primary_port is not None and not any(row["name"] == primary_port for row in outputs):
        raise ValueError(f"primary output port {primary_port!r} is missing")
    manifest = build_manifest(
        kind=kind,
        inputs=dict(inputs),
        outputs=outputs,
        created=datetime.now(timezone.utc).isoformat(),
        **dict(extra or {}),
    )
    return write_manifest(root / "manifest.json", manifest)
