"""Validate the immutable live-scene package before the renderer executes it."""
import hashlib
import json
import math
import re
from collections.abc import Callable, Mapping
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


class LiveSceneHtmlError(ValueError):
    """The entry HTML object of an html-less live scene cannot be resolved."""

    def __init__(self, object_id: str, reason: str) -> None:
        self.object_id = object_id
        self.reason = reason
        super().__init__(f"live scene entry object {object_id!r} {reason}")


def validate_package(package: object) -> None:
    """Validate only bytes and metadata; never unpack or execute package content."""
    if not isinstance(package, Mapping):
        raise ValueError("missing prepared scene package")

    revision = package.get("revision")
    source = package.get("source")
    package_body = package.get("packageBody")
    html = package.get("html")
    # ``html`` is optional: a reference-only scene is verified against its entry
    # object by ``resolve_live_scene_html`` before any renderer consumes it.
    has_html = html is not None
    if (
        not isinstance(revision, str)
        or not _SHA256_DIGEST.fullmatch(revision)
        or not isinstance(source, Mapping)
        or not isinstance(source.get("objectId"), str)
        or not source.get("objectId")
        or not isinstance(source.get("revision"), str)
        or not _SHA256_DIGEST.fullmatch(source["revision"])
        or not isinstance(package_body, str)
        or (has_html and (not isinstance(html, str) or not html.strip()))
    ):
        raise ValueError("missing or invalid scene package envelope")
    if revision != source["revision"]:
        raise ValueError("scene package/source revision mismatch")

    try:
        package_bytes = package_body.encode("utf-8")
        html_bytes = html.encode("utf-8") if has_html else None
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

    if html_bytes is None:
        return
    if entry["digest"] != _digest(html_bytes):
        raise ValueError("scene entry digest mismatch")
    if entry["size"] != len(html_bytes):
        raise ValueError("scene entry size mismatch")


def resolve_live_scene_html(
    package: Mapping[str, Any], read_bytes: Callable[[str], bytes | None]
) -> Mapping[str, Any]:
    """Return ``package`` with ``html`` filled from its entry object.

    A scene that already carries ``html`` is returned unchanged and never
    touches ``read_bytes``. Otherwise the entry object named by ``packageBody``
    is read by id and verified against the digest and size the package declares.
    ``read_bytes`` returns the object's bytes, or ``None`` when it is absent;
    a raised exception from it is reported as an unreadable object.
    """
    validate_package(package)
    if package.get("html") is not None:
        return package
    body = json.loads(str(package["packageBody"]), parse_constant=_reject_json_constant)
    entry = body["entry"]
    object_id = str(entry["object_id"])
    try:
        data = read_bytes(object_id)
    except Exception as exc:  # noqa: BLE001 - every reader failure fails the render
        raise LiveSceneHtmlError(object_id, f"is unreadable: {exc}") from exc
    if data is None:
        raise LiveSceneHtmlError(object_id, "is missing from the object store")
    if not isinstance(data, (bytes, bytearray)):
        raise LiveSceneHtmlError(object_id, "did not return bytes")
    data = bytes(data)
    if _digest(data) != entry["digest"]:
        raise LiveSceneHtmlError(object_id, "digest does not match the scene package")
    if len(data) != entry["size"]:
        raise LiveSceneHtmlError(object_id, "size does not match the scene package")
    try:
        html = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise LiveSceneHtmlError(object_id, "is not valid UTF-8 HTML") from exc
    return {**package, "html": html}
