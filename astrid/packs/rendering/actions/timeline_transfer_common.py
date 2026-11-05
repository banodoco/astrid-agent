"""Shared task-artifact helpers for timeline transfer actions."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from astrid.pack_api import build_manifest, write_manifest
from astrid.pack_api import run_pack_main
from astrid.pack_api import utc_now_iso

MAX_MANIFEST_INLINE_BYTES = 256 * 1024
MAX_METADATA_FILE_BYTES = 64 * 1024 * 1024


def read_json_file(path_value: str, field: str, *, maximum: int = MAX_METADATA_FILE_BYTES) -> Any:
    path = Path(path_value).expanduser()
    try:
        size = path.stat().st_size
        if size > maximum:
            raise ValueError(f"{field} file exceeds the {maximum}-byte metadata limit")
        raw = path.read_bytes()
    except OSError as exc:
        raise ValueError(f"cannot read {field} file {path}: {exc}") from exc
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{field} file must contain valid UTF-8 JSON: {exc}") from exc


def decode_manifest(raw_json: str | None, file_path: str | None) -> Mapping[str, Any]:
    if bool(raw_json) == bool(file_path):
        raise ValueError("provide exactly one of manifest or manifest_file")
    if file_path:
        manifest = read_json_file(file_path, "manifest")
    else:
        if raw_json is None:
            raise ValueError("manifest is required")
        try:
            encoded = raw_json.encode("utf-8")
            if len(encoded) > MAX_MANIFEST_INLINE_BYTES:
                raise ValueError(
                    f"manifest exceeds {MAX_MANIFEST_INLINE_BYTES} encoded bytes; pass it as manifest_file"
                )
            manifest = json.loads(raw_json)
        except json.JSONDecodeError as exc:
            raise ValueError(f"manifest must be valid JSON: {exc}") from exc
    if not isinstance(manifest, Mapping):
        raise ValueError("manifest must be a JSON object")
    return manifest


def output_root(path_value: str) -> Path:
    raw_root = Path(path_value).expanduser()
    if raw_root.is_symlink():
        raise ValueError("task output directory must not be a symlink")
    root = raw_root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def write_artifact(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")


def write_receipt(root: Path, *, kind: str, inputs: Mapping[str, Any], outputs: list[dict[str, Any]]) -> None:
    manifest = build_manifest(kind=kind, inputs=inputs, outputs=outputs, created=utc_now_iso())
    write_manifest(root / "manifest.json", manifest)


def run_cli(capability_id: str, runner, argv: list[str] | None = None) -> int:
    return run_pack_main(capability_id, runner, argv=argv)


def open_worker_runtime_client():
    """Compose the read-only product facade over the host-issued worker connection.

    GenericPackHost authenticates the worker and injects its explicit Runtime
    endpoint and worker credential into command children. Do not invoke
    the neutral launcher here: it selects an owner workspace, which is a
    different authority from the Runtime task this action is serving.
    """
    from astrid.sdk.remote import RemoteAstridClient
    from astrid.sdk.workspace_client import WorkspaceClient, resolve_runtime_connection

    endpoint, token = resolve_runtime_connection()
    return RemoteAstridClient(WorkspaceClient(endpoint, token))
