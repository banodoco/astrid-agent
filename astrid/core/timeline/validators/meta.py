"""Validation for opaque ``meta`` at authored timeline seams."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from typing import Any

from astrid.core.receipts.canonical import CanonicalizationError, canonical_bytes

META_MAX_NAMESPACES = 64
META_MAX_NAMESPACE_BYTES = 65_536
META_MAX_BYTES = 262_144
META_MAX_CONTAINER_DEPTH = 32
META_NAMESPACE_RE = re.compile(r"^[a-z][a-z0-9]*(?:[.-][a-z0-9]+)*$")


class MetaValidationError(ValueError):
    """A structured validation failure for one admitted ``meta`` location."""

    def __init__(self, code: str, path: str, hint: str) -> None:
        self.code = code
        self.path = path
        self.hint = hint
        self.message = f"{path}: {hint}"
        self.details = {"code": code, "path": path, "hint": hint}
        super().__init__(self.message)


def _invalid(path: str, hint: str) -> MetaValidationError:
    return MetaValidationError("invalid_meta", path, hint)


def _limited(path: str, hint: str) -> MetaValidationError:
    return MetaValidationError("meta_limit_exceeded", path, hint)


def _validate_descendant(value: Any, path: str, *, depth: int) -> None:
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise _invalid(path, "must contain only finite JSON numbers")
        return
    if isinstance(value, dict):
        if depth > META_MAX_CONTAINER_DEPTH:
            raise _limited(
                path,
                f"nested containers must be at most {META_MAX_CONTAINER_DEPTH} levels below the namespace root",
            )
        for key, child in value.items():
            if not isinstance(key, str):
                raise _invalid(f"{path}.{key!r}", "object keys must be strings")
            _validate_descendant(child, f"{path}.{key}", depth=depth + 1)
        return
    if isinstance(value, list):
        if depth > META_MAX_CONTAINER_DEPTH:
            raise _limited(
                path,
                f"nested containers must be at most {META_MAX_CONTAINER_DEPTH} levels below the namespace root",
            )
        for index, child in enumerate(value):
            _validate_descendant(child, f"{path}[{index}]", depth=depth + 1)
        return
    raise _invalid(path, "must contain only finite JSON values")


def _canonical_size(value: Any, path: str) -> int:
    try:
        # The validator only needs to distinguish values at or below the V1
        # total limit from values above it. A one-byte-over probe keeps the
        # shared helper's canonical encoding while preserving the required
        # ``meta_limit_exceeded`` classification for very large input.
        return len(canonical_bytes(value, max_bytes=META_MAX_BYTES + 1))
    except CanonicalizationError as exc:
        if str(exc).startswith("canonical JSON exceeds"):
            return META_MAX_BYTES + 1
        raise _invalid(path, "must contain only finite JSON values") from exc


def validate_meta(meta: Any, *, path: str = "meta") -> None:
    """Validate one optional authored ``meta`` object without changing it."""

    if not isinstance(meta, dict):
        raise _invalid(path, "must be an object; omit it or remove it explicitly to clear it")
    if len(meta) > META_MAX_NAMESPACES:
        raise _limited(path, f"must contain at most {META_MAX_NAMESPACES} namespaces")

    for namespace, value in meta.items():
        namespace_path = f"{path}.{namespace!s}"
        if not isinstance(namespace, str):
            raise _invalid(namespace_path, "namespace names must be strings")
        try:
            namespace.encode("ascii")
        except UnicodeEncodeError as exc:
            raise _invalid(namespace_path, "namespace must be 1–128 ASCII characters") from exc
        if not 1 <= len(namespace) <= 128 or META_NAMESPACE_RE.fullmatch(namespace) is None:
            raise _invalid(
                namespace_path,
                "namespace must match ^[a-z][a-z0-9]*(?:[.-][a-z0-9]+)*$ and be 1–128 ASCII characters",
            )
        if not isinstance(value, dict):
            raise _invalid(namespace_path, "namespace value must be an object and must not be null")
        _validate_descendant(value, namespace_path, depth=1)
        if _canonical_size(value, namespace_path) > META_MAX_NAMESPACE_BYTES:
            raise _limited(
                namespace_path,
                f"namespace value must be at most {META_MAX_NAMESPACE_BYTES} canonical UTF-8 bytes",
            )

    if _canonical_size(meta, path) > META_MAX_BYTES:
        raise _limited(path, f"meta must be at most {META_MAX_BYTES} canonical UTF-8 bytes")


def validate_timeline_meta_locations(config: Mapping[str, Any], *, path: str = "config") -> None:
    """Validate only timeline, track, and clip ``meta`` locations."""

    if "meta" in config:
        validate_meta(config["meta"], path=f"{path}.meta")
    for collection in ("tracks", "clips"):
        rows = config.get(collection)
        if not isinstance(rows, list):
            continue
        for index, row in enumerate(rows):
            if isinstance(row, Mapping) and "meta" in row:
                validate_meta(row["meta"], path=f"{path}.{collection}[{index}].meta")


__all__ = [
    "META_MAX_BYTES",
    "META_MAX_CONTAINER_DEPTH",
    "META_MAX_NAMESPACES",
    "META_MAX_NAMESPACE_BYTES",
    "META_NAMESPACE_RE",
    "MetaValidationError",
    "validate_meta",
    "validate_timeline_meta_locations",
]
