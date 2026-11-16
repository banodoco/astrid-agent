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


class LiveSceneFilesUnavailable(LiveSceneHtmlError):
    """A render has no object store to read an html-less scene's entry HTML from."""

    def __init__(self, object_id: str, clip_id: object) -> None:
        self.clip_id = clip_id
        ValueError.__init__(
            self,
            f"live scene in clip {clip_id!r}: the scene's files (entry object {object_id!r}) are not "
            "available for a local file render, because they exist only in the project's object store; "
            "render the project's saved timeline (managed render) so the Runtime supplies them",
        )
        self.object_id = object_id
        self.reason = "is not available for a local file render"


def validate_package(package: object) -> None:
    """Validate only bytes and metadata; never unpack or execute package content."""
    if not isinstance(package, Mapping):
        raise ValueError("missing prepared scene package")

    revision = package.get("revision")
    source = package.get("source")
    package_body = package.get("packageBody")
    html = package.get("html")
    # ``html`` is optional as a KEY: a reference-only scene is verified against
    # its entry object by ``resolve_live_scene_html`` before any renderer
    # consumes it. A present key must be a non-empty string, exactly as the
    # Runtime requires, so ``"html": null`` is rejected rather than read as absent.
    has_html = "html" in package
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
    if "html" in package:
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


def _is_live_scene_clip(clip: object) -> bool:
    return isinstance(clip, Mapping) and clip.get("clipType") == "haus.astrid.liveScene"


def _scene_of(clip: Mapping[str, Any]) -> Mapping[str, Any] | None:
    app = clip.get("app")
    scene = app.get("liveScene") if isinstance(app, Mapping) else None
    return scene if isinstance(scene, Mapping) else None


def live_scene_entry_refs(config: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The entry objects of every html-less live-scene clip in ``config``.

    Each item names the clip, its package object and the entry object its package declares
    (``entry_object_id`` is the package's own ``entry.object_id``, the id a
    renderer later looks up). Clips with inline ``html`` need no object.
    """
    refs: list[dict[str, Any]] = []
    clips = config.get("clips") if isinstance(config, Mapping) else None
    for clip in clips if isinstance(clips, list) else ():
        scene = _scene_of(clip) if _is_live_scene_clip(clip) else None
        if scene is None or "html" in scene:
            continue
        validate_package(scene)
        entry = json.loads(str(scene["packageBody"]), parse_constant=_reject_json_constant)["entry"]
        refs.append({
            "clip_id": clip.get("id"),
            "package_object_id": str(scene["source"]["objectId"]),
            "entry_object_id": str(entry["object_id"]),
            "entry_size": entry["size"],
        })
    return refs


def fill_live_scene_html(
    timeline: Mapping[str, Any], materialized_objects: Mapping[str, Any] | None,
) -> Mapping[str, Any]:
    """Return ``timeline`` with ``html`` filled for its html-less live-scene clips.

    The bytes come from the host's ``materialized_objects`` handoff (object id
    or bare digest -> file path or bytes) and are verified against the digest
    and size the package declares. Clips that carry inline ``html`` are left
    untouched. With no handoff at all (a local render of a timeline file) an
    html-less clip fails with ``LiveSceneFilesUnavailable``; a handoff that
    lacks the entry, or whose bytes do not verify, fails with
    ``LiveSceneHtmlError``. Nothing is ever rendered blank.
    """
    from pathlib import Path

    clips = timeline.get("clips")
    if not isinstance(clips, list):
        return timeline

    def read_object(object_id: str) -> bytes | None:
        value = materialized_objects.get(object_id) if isinstance(materialized_objects, Mapping) else None
        if value is None:
            return None
        if isinstance(value, (bytes, bytearray)):
            return bytes(value)
        return Path(value).read_bytes()

    resolved: list[Any] = []
    for clip in clips:
        scene = _scene_of(clip) if _is_live_scene_clip(clip) else None
        if scene is not None and "html" not in scene:
            if not materialized_objects:
                validate_package(scene)
                entry = json.loads(str(scene["packageBody"]), parse_constant=_reject_json_constant)["entry"]
                raise LiveSceneFilesUnavailable(str(entry["object_id"]), clip.get("id"))
            app = clip["app"]
            clip = {**clip, "app": {**app, "liveScene": resolve_live_scene_html(scene, read_object)}}
        resolved.append(clip)
    return {**timeline, "clips": resolved}
