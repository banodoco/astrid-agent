"""The host-owned model inventory document handed to one local_server child.

The host builds one JSON value, ``{"mode", "comfy_url", "models"}``, after it has
checked the bound install, and sets it in ``ASTRID_TARGET_MODEL_ASSETS``. The
child refuses it unless its ``comfy_url`` is its own. ``models`` are the rows
of the pod's verified install receipt (never the engine lock), as
``{name, subdir, size, sha256}``.
"""
from __future__ import annotations

import json
import posixpath
from collections.abc import Iterable, Mapping
from typing import Any

ENV = "ASTRID_TARGET_MODEL_ASSETS"
MODES = ("inventory", "trust")
_ROW_KEYS = {"name", "subdir", "size", "sha256"}


class InventoryError(ValueError):
    pass


def rows_from_receipt(models: Iterable[Any], root: str) -> tuple[dict[str, Any], ...]:
    """Split each verified receipt path into subdir and name under ``root/models``."""
    base = posixpath.join(root, "models") + "/"
    rows = []
    for model in models:
        path = model.path
        if not path.startswith(base):
            raise InventoryError("receipt model path is outside the engine model root")
        subdir, _, name = path[len(base):].rpartition("/")
        rows.append({"name": name, "subdir": subdir, "size": model.bytes,
                     "sha256": model.sha256.removeprefix("sha256:")})
    return tuple(rows)


def build_document(mode: str, comfy_url: str, models: Iterable[Mapping[str, Any]] = ()) -> str:
    if mode not in MODES:
        raise InventoryError("unknown inventory mode")
    return json.dumps({"mode": mode, "comfy_url": comfy_url, "models": [dict(row) for row in models]},
                      sort_keys=True, separators=(",", ":"), allow_nan=False)


def parse_document(raw: str, comfy_url: str) -> tuple[str, tuple[dict[str, Any], ...]]:
    """Strictly parse the document; refuse one made for another ComfyUI."""
    try:
        value = json.loads(raw)
    except ValueError as exc:
        raise InventoryError("target model inventory is not valid JSON") from exc
    if not isinstance(value, dict) or set(value) != {"mode", "comfy_url", "models"}:
        raise InventoryError("target model inventory has unexpected fields")
    if value["mode"] not in MODES:
        raise InventoryError("target model inventory has an unknown mode")
    if value["comfy_url"] != comfy_url:
        raise InventoryError("target model inventory was made for another ComfyUI URL")
    models = value["models"]
    if not isinstance(models, list) or (value["mode"] == "trust" and models):
        raise InventoryError("target model inventory models are malformed")
    rows = []
    for row in models:
        if (not isinstance(row, dict) or set(row) != _ROW_KEYS or not isinstance(row["name"], str)
                or not row["name"] or not isinstance(row["subdir"], str)
                or type(row["size"]) is not int or row["size"] < 0
                or not isinstance(row["sha256"], str) or len(row["sha256"]) != 64):
            raise InventoryError("target model inventory row is malformed")
        rows.append(dict(row))
    return value["mode"], tuple(rows)


def declared_rows(workflow: Any) -> tuple[dict[str, Any], ...]:
    """The workflow's own declared model rows, for ``--align trust``."""
    rows = []
    for asset in (getattr(workflow, "metadata", None) or {}).get("model_assets", []) or []:
        if not isinstance(asset, Mapping):
            continue
        name = asset.get("name", asset.get("filename"))
        digest, size = asset.get("sha256"), asset.get("size_bytes")
        if isinstance(name, str) and name and isinstance(digest, str) and type(size) is int:
            rows.append({"name": name, "subdir": str(asset.get("subdir", asset.get("directory", "")) or ""),
                         "size": size, "sha256": digest.removeprefix("sha256:")})
    return tuple(rows)
