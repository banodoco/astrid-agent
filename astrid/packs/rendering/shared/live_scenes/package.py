"""Validate the immutable live-scene package before the renderer executes it."""
import hashlib
import json
import math
import re
from collections.abc import Mapping
from typing import Any

_SHA256_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_ENTRY_NAME = re.compile(r"[\w./-]+\.html\Z")


def _digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _metadata(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"invalid {label} project object metadata")
    object_id = value.get("object_id")
    digest = value.get("digest")
    media_type = value.get("media_type")
    size = value.get("size")
    filename = value.get("filename")
    if (
        not isinstance(object_id, str)
        or not object_id
        or not isinstance(digest, str)
        or not _SHA256_DIGEST.fullmatch(digest)
        or not isinstance(media_type, str)
        or not media_type
        or isinstance(size, bool)
        or not isinstance(size, int)
        or size < 0
        or (filename is not None and (not isinstance(filename, str) or not filename))
    ):
        raise ValueError(f"invalid {label} project object metadata")
    return value


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant {value}")


def validate_package(package: object) -> None:
    """Validate only bytes and metadata; never unpack or execute package content."""
    if not isinstance(package, Mapping):
        raise ValueError("missing prepared scene package")

    revision = package.get("revision")
    source = package.get("source")
    package_body = package.get("packageBody")
    html = package.get("html")
    if (
        not isinstance(revision, str)
        or not _SHA256_DIGEST.fullmatch(revision)
        or not isinstance(source, Mapping)
        or not isinstance(source.get("objectId"), str)
        or not source.get("objectId")
        or not isinstance(source.get("revision"), str)
        or not _SHA256_DIGEST.fullmatch(source["revision"])
        or not isinstance(package_body, str)
        or not isinstance(html, str)
        or not html.strip()
    ):
        raise ValueError("missing or invalid scene package envelope")
    if revision != source["revision"]:
        raise ValueError("scene package/source revision mismatch")

    try:
        package_bytes = package_body.encode("utf-8")
        html_bytes = html.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError("scene package contains invalid UTF-8 text") from exc
    if revision != _digest(package_bytes):
        raise ValueError("scene packageBody digest mismatch")

    try:
        body = json.loads(package_body, parse_constant=_reject_json_constant)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("scene packageBody is not valid JSON") from exc
    if not isinstance(body, Mapping):
        raise ValueError("scene packageBody must be an object")

    manifest = body.get("manifest")
    if not isinstance(manifest, Mapping) or manifest.get("formatVersion") != 1:
        raise ValueError("unsupported scene format")
    entry_name = manifest.get("entry")
    if (
        not isinstance(entry_name, str)
        or not _ENTRY_NAME.fullmatch(entry_name)
        or ".." in entry_name.split("/")
    ):
        raise ValueError("invalid HTML entry")
    for key in ("duration", "authoredFps"):
        value = manifest.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
            raise ValueError(f"invalid {key}")

    assets = body.get("assets")
    if assets != []:
        raise ValueError("prepared scene package assets must be an empty array")
    entry = _metadata(body.get("entry"), "entry")
    for index, asset in enumerate(assets):
        _metadata(asset, f"asset[{index}]")

    if entry["digest"] != _digest(html_bytes):
        raise ValueError("scene entry digest mismatch")
    if entry["size"] != len(html_bytes):
        raise ValueError("scene entry size mismatch")
