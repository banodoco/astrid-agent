"""Prepare local media in detached timelines through the project import catalog.

Local paths are authoring inputs only. Planning is read-only; importing uses the
existing Runtime media-import operation and publication remains parent CAS.
"""

from __future__ import annotations

import copy
import hashlib
import json
import mimetypes
import re
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit

from astrid.core.media import ffprobe_metadata_strict
from astrid.core.path_utils import file_uri_to_path, has_url_scheme, is_native_absolute_path
from astrid.core.timeline.authoring_bundle import validate_authoring_candidate
from astrid.core.timeline.parent_registry import effective_parent_registry
from astrid.sdk.pagination import paged_rows

_DIGEST = re.compile(r"^(?:sha256:)?[0-9a-f]{64}$")
_PATH_FIELDS = ("local_path", "file", "path", "src")
_ID_FIELDS = ("media_id", "object_id", "digest", "content_digest", "content_sha256")
_MAX_BYTES = 64 * 1024 * 1024


def _identity(value: Any) -> str | None:
    if isinstance(value, str) and _DIGEST.fullmatch(value):
        return "sha256:" + value.removeprefix("sha256:")
    return None


def _parent_assets(parent: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    # Use the compiler/renderer's one effective parent namespace, not a union
    # of baseline or sibling registries.
    registry = effective_parent_registry(parent)
    selected = registry.get("assets", {})
    return {
        key: entry
        for key, entry in (selected.items() if isinstance(selected, Mapping) else ())
        if isinstance(key, str) and isinstance(entry, Mapping)
    }


def _effective_assets(
    node: Mapping[str, Any], parent_assets: Mapping[str, Mapping[str, Any]]
) -> dict[str, Any]:
    registry = node.get("registry")
    local = registry.get("assets") if isinstance(registry, Mapping) else None
    assets = dict(parent_assets)
    if isinstance(local, Mapping):
        assets.update(local)  # Current local entries shadow parent authority.
    return assets


def _path(value: str, directory: Path) -> Path:
    parsed = urlsplit(value)
    if parsed.scheme == "file":
        if (
            not (value.startswith("file:///") or value.startswith("file://localhost/"))
            or parsed.netloc not in {"", "localhost"}
            or not parsed.path.startswith("/")
            or parsed.path.startswith("//")
            or parsed.query
            or parsed.fragment
            or re.search(r"%(?![0-9A-Fa-f]{2})", parsed.path)
        ):
            raise ValueError(f"Local media requires a canonical absolute file URI: {value}")
        path = file_uri_to_path(value)
        if not is_native_absolute_path(path):
            raise ValueError(f"Local media file URI must resolve to an absolute path: {value}")
        return path.expanduser().resolve(strict=True)
    elif has_url_scheme(value):
        raise ValueError(f"Local media requires a file path, not a URL: {value}")
    path = Path(value).expanduser()
    return (path if path.is_absolute() else directory / path).resolve(strict=True)


def _source(entry: Mapping[str, Any], baseline: Mapping[str, Any], directory: Path) -> str | None:
    # An unchanged managed entry may retain a filename used for presentation.
    # Only new/changed path fields, or explicit local_path, request an import.
    del directory
    changed: list[str] = []
    explicit: str | None = None
    for field in _PATH_FIELDS:
        if field not in entry:
            continue
        value = entry[field]
        if value is None or value == "":
            continue
        if not isinstance(value, str):
            raise ValueError(f"Malformed media locator in registry asset field {field}")
        if field == "local_path":
            # Explicit local intent stays active even when inherited unchanged.
            explicit = value
        elif value != baseline.get(field):
            changed.append(value)
    if explicit is not None:
        return explicit
    if len(set(changed)) > 1:
        raise ValueError("Ambiguous changed registry media locators; set local_path explicitly")
    return changed[0] if changed else None


def _metadata(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ValueError(f"Local media must be a regular file: {path}")
    if path.stat().st_size > _MAX_BYTES:
        raise ValueError(f"Local media exceeds Runtime's 64 MiB limit: {path}")
    media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    kind = media_type.split("/", 1)[0]
    if kind not in {"image", "video", "audio"}:
        raise ValueError(f"Unsupported local media type {media_type}: {path}")
    metadata: dict[str, Any] = {"filename": path.name, "media_type": media_type}
    if kind == "image":
        from PIL import Image

        with Image.open(path) as image:
            image.verify()
        with Image.open(path) as image:
            metadata.update(width=image.width, height=image.height)
            decoded_type = Image.MIME.get(image.format)
            if decoded_type != media_type:
                raise ValueError(f"Image bytes disagree with filename type {media_type}: {path}")
    else:
        probe = ffprobe_metadata_strict(path)
        if kind == "audio" and (not probe.has_audio_stream or probe.has_video_stream):
            raise ValueError(f"Audio media must contain audio without a video stream: {path}")
        if kind == "video" and not probe.has_video_stream:
            raise ValueError(f"Video media has no video stream: {path}")
        if not probe.duration_seconds or probe.duration_seconds <= 0:
            raise ValueError(f"Media has no positive duration: {path}")
        metadata["duration_seconds"] = probe.duration_seconds
        if kind == "video":
            metadata.update(width=probe.width, height=probe.height)
    payload = path.read_bytes()
    if len(payload) > _MAX_BYTES:
        raise ValueError(f"Local media exceeds Runtime's 64 MiB limit: {path}")
    metadata["digest"] = "sha256:" + hashlib.sha256(payload).hexdigest()
    metadata["size"] = len(payload)
    return metadata


def _reject_unconsumed_media_locators(candidate: Mapping[str, Any]) -> None:
    """Refuse only explicit live media locators left by the supported rewrite.

    Embedded baselines are trusted operator comparison context, not an
    authenticated closure. Historical/opaque fields are deliberately excluded.
    """

    def mapping(value: Any) -> Mapping[str, Any]:
        return value if isinstance(value, Mapping) else {}

    def rows(value: Any) -> list[Any]:
        return value if isinstance(value, list) else []

    def identity(row: Mapping[str, Any], keys: tuple[str, ...]) -> str | None:
        for key in keys:
            value = row.get(key)
            if isinstance(value, str) and value:
                return value
        return None

    def carries_locator(row: Mapping[str, Any]) -> bool:
        source = mapping(row.get("source"))
        return (
            any(key in row or key in source for key in _PATH_FIELDS)
            or any(
                isinstance(row.get(key), str) and _identity(row[key]) is None
                for key in ("media_id", "object_id")
            )
        )

    def matched_rows(
        current: Any, baseline: Any, keys: tuple[str, ...], path: str
    ) -> list[tuple[Mapping[str, Any], Mapping[str, Any], str]]:
        old: dict[str, Mapping[str, Any]] = {}
        ambiguous: set[str] = set()
        for raw in rows(baseline):
            if not isinstance(raw, Mapping):
                continue
            key = identity(raw, keys)
            if key is not None:
                if key in old:
                    ambiguous.add(key)
                old[key] = raw
        current_rows = rows(current)
        counts: dict[str, int] = {}
        for raw in current_rows:
            if isinstance(raw, Mapping):
                key = identity(raw, keys)
                if key is not None:
                    counts[key] = counts.get(key, 0) + 1
        result = []
        for index, raw in enumerate(current_rows):
            if not isinstance(raw, Mapping):
                continue
            key = identity(raw, keys)
            if carries_locator(raw) and (
                key is None or counts.get(key, 0) > 1 or key in ambiguous
            ):
                raise ValueError(f"Ambiguous media locator baseline at {path}[{index}]")
            result.append((raw, old.get(key, {}), f"{path}[{key!r}]"))
        return result

    def fields(value: Mapping[str, Any], baseline: Mapping[str, Any], path: str) -> None:
        for field in _PATH_FIELDS:
            if field not in value:
                continue
            locator = value[field]
            if locator is None or locator == "":
                continue
            if not isinstance(locator, str):
                raise ValueError(f"Malformed media locator at {path}.{field}")
            if field == "local_path" or locator != baseline.get(field):
                raise ValueError(f"Unconsumed media locator at {path}.{field}")

    def identities(value: Mapping[str, Any], baseline: Mapping[str, Any], path: str) -> None:
        for field in _ID_FIELDS:
            if field not in value:
                continue
            locator = value[field]
            if locator is None or locator == "":
                continue
            if not isinstance(locator, str):
                raise ValueError(f"Malformed media locator at {path}.{field}")
            if _identity(locator) is None and locator != baseline.get(field):
                raise ValueError(f"Unconsumed media locator at {path}.{field}")

    def descriptor(value: Mapping[str, Any], baseline: Mapping[str, Any], path: str) -> None:
        fields(value, baseline, path)
        identities(value, baseline, path)
        source = value.get("source")
        if isinstance(source, Mapping):
            fields(source, mapping(baseline.get("source")), path + ".source")
            identities(source, mapping(baseline.get("source")), path + ".source")

    def timeline(
        value: Mapping[str, Any], baseline: Mapping[str, Any], path: str,
        inherited_assets: Mapping[str, Mapping[str, Any]],
    ) -> None:
        local_assets = mapping(mapping(value.get("registry")).get("assets"))
        assets = _effective_assets(value, inherited_assets)
        old_assets = mapping(mapping(baseline.get("registry")).get("assets"))
        for key, entry in local_assets.items():
            if isinstance(entry, Mapping):
                # Top-level asset path fields are consumed by _source/clean.
                identities(entry, mapping(old_assets.get(key)),
                           f"{path}.registry.assets[{key!r}]")
                source = entry.get("source")
                if isinstance(source, Mapping):
                    old_source = mapping(mapping(old_assets.get(key)).get("source"))
                    fields(source, old_source, f"{path}.registry.assets[{key!r}].source")
                    identities(source, old_source,
                               f"{path}.registry.assets[{key!r}].source")
        for clip, old, row_path in matched_rows(
            value.get("clips"), baseline.get("clips"), ("id",), path + ".clips"
        ):
            if (
                clip.get("clipType") == "media"
                or old.get("clipType") == "media"
                or any(key in clip or key in old for key in
                       ("asset", "asset_id", "media_id", "object_id"))
            ):
                fields(clip, old, row_path)
                # Classify every surviving finite selector, including a secondary
                # field hidden behind a valid primary selector in the compiler.
                for field in ("asset", "asset_id", "media_id", "object_id"):
                    if field not in clip:
                        continue
                    selector = clip[field]
                    if selector is None or selector == "":
                        continue
                    if not isinstance(selector, str):
                        raise ValueError(f"Malformed media selector at {row_path}.{field}")
                    if _identity(selector) or selector in assets:
                        continue
                    if selector != old.get(field):
                        raise ValueError(f"Unconsumed media selector at {row_path}.{field}")
        for asset, old, row_path in matched_rows(
            value.get("assets"), baseline.get("assets"),
            ("asset_id", "id"), path + ".assets"
        ):
            descriptor(asset, old, row_path)
        if isinstance(value.get("config"), Mapping):
            timeline(value["config"], mapping(baseline.get("config")),
                     path + ".config", assets)

    parent = mapping(candidate.get("parent"))
    parent_assets = _parent_assets(parent)
    timeline(parent, mapping(candidate.get("base_parent_payload")),
             "candidate.parent", parent_assets)
    for shot_id, shot in mapping(candidate.get("shots")).items():
        if not isinstance(shot, Mapping):
            continue
        path = f"candidate.shots[{shot_id!r}]"
        timeline(mapping(shot.get("internal_timeline")),
                 mapping(shot.get("base_internal_timeline")),
                 path + ".internal_timeline", parent_assets)
        payload = mapping(shot.get("payload"))
        base_payload = mapping(shot.get("base_payload"))
        for item, old, row_path in matched_rows(
            payload.get("items"), base_payload.get("items"),
            ("item_id", "id"), path + ".payload.items"
        ):
            fields(item, old, row_path)
        for asset, old, row_path in matched_rows(
            payload.get("assets"), base_payload.get("assets"),
            ("asset_id", "id"), path + ".payload.assets"
        ):
            descriptor(asset, old, row_path)
        for binding, old, row_path in matched_rows(
            payload.get("audio_bindings"), base_payload.get("audio_bindings"),
            ("track_id", "id"), path + ".payload.audio_bindings"
        ):
            fields(binding, old, row_path)
            for field in ("media_id", "object_id"):
                if field not in binding:
                    continue
                value = binding[field]
                if value is None or value == "":
                    continue
                if not isinstance(value, str):
                    raise ValueError(f"Malformed media locator at {row_path}.{field}")
                if _identity(value) is None and value != old.get(field):
                    raise ValueError(f"Unconsumed media locator at {row_path}.{field}")


def plan_authoring_media(
    candidate: Mapping[str, Any], *, base_directory: str | Path
) -> dict[str, Any]:
    """Resolve/probe every local reference and validate a detached durable copy.

    No Runtime reads or mutations occur. The returned candidate uses calculated
    digests for validation only; call import_authoring_media before publication.
    Relative paths are resolved against the checkout document's directory.
    """
    original = copy.deepcopy(dict(candidate))
    work = copy.deepcopy(original)
    directory = Path(base_directory).resolve()
    imports: dict[str, dict[str, Any]] = {}
    by_path: dict[Path, dict[str, Any]] = {}

    def source(value: str) -> dict[str, Any]:
        path = _path(value, directory)
        if path not in by_path:
            details = _metadata(path)
            # Stable across publication keys and retries. Distinct imported
            # names/types keep distinct provenance even when bytes match.
            fingerprint = hashlib.sha256(json.dumps(details, sort_keys=True).encode()).hexdigest()
            row = {**details, "path": str(path), "import_key": "timeline-media-" + fingerprint}
            imports.setdefault(row["import_key"], row)
            by_path[path] = row
        return by_path[path]

    def clean(entry: dict[str, Any], row: Mapping[str, Any]) -> None:
        had_catalog_binding = bool(entry.get("generationId") and entry.get("variantId"))
        for field in (*_PATH_FIELDS, *_ID_FIELDS):
            entry.pop(field, None)
        # Source locators are a projection of the former selected object, not
        # reusable authority for the newly imported file.
        entry.pop("source", None)
        entry["media_id"] = row["digest"]
        entry["type"] = row["media_type"].split("/", 1)[0]
        # Catalog-bound rebinds retain authored labels and opaque fields.
        # Unmarked aliases receive the imported media metadata.
        if not had_catalog_binding:
            entry["media_type"] = row["media_type"]
            entry["filename"] = row["filename"]
            width, height = row.get("width"), row.get("height")
            if width is not None and height is not None:
                entry["resolution"] = f"{width}x{height}"
            else:
                entry.pop("resolution", None)
            if "duration_seconds" in row:
                entry["duration"] = row["duration_seconds"]
            else:
                entry.pop("duration", None)
            for field in ("width", "height", "duration_seconds"):
                entry.pop(field, None)

    def rewrite_registry(
        node: dict[str, Any], base: Mapping[str, Any], *, explicit_only: bool = False
    ) -> None:
        local_assets = node.get("registry", {}).get("assets", {})
        base_assets = base.get("registry", {}).get("assets", {})
        for key, entry in local_assets.items():
            if not isinstance(entry, dict):
                continue
            if explicit_only and (
                "local_path" not in entry
                or entry["local_path"] is None
                or entry["local_path"] == ""
            ):
                continue
            value = _source(entry, base_assets.get(key, {}), directory)
            if value is not None:
                clean(entry, source(value))

    def timeline(
        node: dict[str, Any], base: Mapping[str, Any], path: str,
        inherited_assets: Mapping[str, Mapping[str, Any]],
    ) -> None:
        rewrite_registry(node, base)
        assets = _effective_assets(node, inherited_assets)
        for clip in node.get("clips", []):
            for field in ("asset", "asset_id", "media_id", "object_id"):
                value = clip.get(field)
                if field in clip and value is not None and value != "" and not isinstance(value, str):
                    raise ValueError(
                        f"Malformed media selector at {path}.clips[{clip.get('id')!r}].{field}"
                    )
                if not isinstance(value, str) or not value or _identity(value) or value in assets:
                    continue
                # Missing asset aliases are compiler errors, not file paths.
                if not (
                    "/" in value
                    or "\\" in value
                    or mimetypes.guess_type(value)[0]
                    or (directory / value).exists()
                ):
                    continue
                row = source(value)
                if field in {"media_id", "object_id"}:
                    clip[field] = row["digest"]
                else:
                    key = "import-" + row["import_key"].removeprefix("timeline-media-")
                    entry = {}
                    clean(entry, row)
                    node.setdefault("registry", {}).setdefault("assets", {})[key] = entry
                    assets[key] = entry
                    clip[field] = key
        if isinstance(node.get("config"), dict):
            timeline(node["config"], base.get("config", {}), path + ".config", assets)

    def selected(clip: Mapping[str, Any], node: Mapping[str, Any]) -> str | None:
        for field in ("asset", "asset_id", "media_id", "object_id"):
            value = clip.get(field)
            direct = _identity(value)
            if direct:
                return direct
            entry = (
                node.get("registry", {}).get("assets", {}).get(value, {})
                if isinstance(value, str)
                else {}
            )
            for key in _ID_FIELDS:
                if _identity(entry.get(key)):
                    return _identity(entry[key])
        return None

    def mirror_assets(node: dict[str, Any], replacements: Mapping[str, set[str]]) -> None:
        for asset in node.get("assets", []):
            if not isinstance(asset, dict):
                continue
            old = next(
                (_identity(asset.get(key)) for key in _ID_FIELDS if _identity(asset.get(key))), None
            )
            options = replacements.get(old, set())
            if len(options) > 1:
                raise ValueError("Ambiguous asset mirror; update its object identity explicitly")
            if not options:
                continue
            new = next(iter(options))
            for key in _ID_FIELDS:
                if key in asset:
                    asset[key] = new.removeprefix("sha256:") if key == "content_sha256" else new
            details = next((row for row in imports.values() if row["digest"] == new), None)
            if isinstance(asset.get("source"), dict):
                source_descriptor = asset["source"]
                for key in _PATH_FIELDS:
                    source_descriptor.pop(key, None)
                for key in _ID_FIELDS:
                    if key in source_descriptor:
                        source_descriptor[key] = (
                            new.removeprefix("sha256:") if key == "content_sha256" else new
                        )
                if details:
                    source_descriptor["type"] = details["media_type"]
                    if "duration_seconds" in details:
                        source_descriptor["duration"] = details["duration_seconds"]
                    else:
                        source_descriptor.pop("duration", None)
            if details and asset.get("role") in {"image", "video", "audio"}:
                asset["role"] = details["media_type"].split("/", 1)[0]
            if details:
                asset["media_type"] = details["media_type"]

    parent = work["parent"]
    base_parent = work["base_parent_payload"]
    # Explicit local intent may remove stale descriptors that make the two
    # parent registry declarations unequal before projection.
    rewrite_registry(parent, base_parent, explicit_only=True)
    if isinstance(parent.get("config"), dict):
        rewrite_registry(parent["config"], base_parent.get("config", {}), explicit_only=True)
    parent_assets = _parent_assets(parent)
    timeline(parent, base_parent, "candidate.parent", parent_assets)
    parent_assets = _parent_assets(work["parent"])  # Use the projected parent for child aliases.
    for shot_id, shot in work["shots"].items():
        internal, base_internal = shot["internal_timeline"], shot["base_internal_timeline"]
        timeline(internal, base_internal, f"candidate.shots[{shot_id!r}].internal_timeline",
                 parent_assets)
        replacements: dict[str, set[str]] = {}
        before = {clip["id"]: clip for clip in base_internal.get("clips", [])}
        for clip in internal.get("clips", []):
            old = selected(before.get(clip["id"], {}), base_internal)
            new = selected(clip, internal)
            if old and new and old != new:
                replacements.setdefault(old, set()).add(new)
        # Mirrored selected items follow the clip replacement. Explicit item
        # path changes are also supported. Historical generation inputs stay pinned.
        for item in shot["payload"].get("items", []):
            value = item.get("media_id")
            if "media_id" in item and value is not None and value != "" and not isinstance(value, str):
                raise ValueError(
                    f"Malformed media selector at candidate.shots[{shot_id!r}]."
                    f"payload.items[{item.get('item_id', item.get('id'))!r}].media_id"
                )
            if isinstance(value, str) and value and not _identity(value):
                item["media_id"] = source(value)["digest"]
            elif _identity(value) in replacements:
                options = replacements[_identity(value)]
                if len(options) != 1:
                    raise ValueError(
                        "Ambiguous mirrored media replacement; set each item's media_id explicitly"
                    )
                item["media_id"] = next(iter(options))
        for binding in shot["payload"].get("audio_bindings", []):
            for field in ("media_id", "object_id"):
                old = _identity(binding.get(field))
                if old in replacements and len(replacements[old]) == 1:
                    binding[field] = next(iter(replacements[old]))
        mirror_assets(shot["payload"], replacements)
        mirror_assets(internal, replacements)
    _reject_unconsumed_media_locators(work)
    validation = validate_authoring_candidate(work)
    return {
        "candidate": work,
        "imports": list(imports.values()),
        "validation": validation,
        "input_candidate": original,
        "base_directory": str(directory),
    }


def _existing_imports(transport: Any, project_id: str) -> list[dict[str, Any]]:
    generations = paged_rows(transport.list_generations, project_id)
    if generations is None:
        raise ValueError("Cannot inspect project catalog before importing media")
    result = []
    for generation in generations:
        metadata = generation.get("metadata", {})
        provenance = metadata.get("provenance", {})
        if provenance.get("origin") != "imported" or provenance.get("source") != "external_upload":
            continue
        variants = paged_rows(transport.list_variants, generation["generation_id"])
        if variants is None:
            raise ValueError("Cannot inspect imported catalog variants")
        for variant in variants:
            if variant.get("variant_type") == "original":
                result.append(
                    {
                        "generation": generation,
                        "variant": variant,
                        "details": metadata.get("params", {}),
                    }
                )
    return result


def import_authoring_media(plan: Mapping[str, Any], transport: Any) -> dict[str, Any]:
    """Import a preflighted plan using existing catalog publication, then validate.

    Imports commit independently of parent publication. A later CAS conflict
    leaves useful catalog media; stable keys and catalog lookup make retries safe.
    """
    source_candidate = plan.get("input_candidate")
    base_directory = plan.get("base_directory")
    if not isinstance(source_candidate, Mapping) or not isinstance(base_directory, str):
        raise ValueError("Media plan is missing its input candidate or checkout directory")
    expected = plan_authoring_media(source_candidate, base_directory=base_directory)
    if expected["imports"] != plan.get("imports"):
        raise ValueError("Local media changed after check or plan modified; check again")
    if any(expected[key] != plan.get(key) for key in
           ("candidate", "validation", "input_candidate", "base_directory")):
        raise ValueError("Media plan changed after preflight; check again")
    work = copy.deepcopy(expected["candidate"])
    # Read and verify ALL files before the first mutation (including TOCTOU).
    payloads = []
    for row in expected["imports"]:
        payload = Path(row["path"]).read_bytes()
        if (
            len(payload) > _MAX_BYTES
            or "sha256:" + hashlib.sha256(payload).hexdigest() != row["digest"]
        ):
            raise ValueError(f"Local media changed after check; check again: {row['path']}")
        payloads.append(payload)
    catalog = _existing_imports(transport, work["project_id"]) if payloads else []
    receipts = []
    for row, payload in zip(expected["imports"], payloads):
        existing = next(
            (
                entry
                for entry in catalog
                if entry["variant"].get("object_id") == row["digest"]
                and entry["details"].get("mime_type") == row["media_type"]
                and entry["details"].get("filename") == row["filename"]
                and all(
                    entry["details"].get(key) == row.get(key)
                    for key in ("width", "height", "duration_seconds")
                )
            ),
            None,
        )
        if existing:
            receipts.append(
                {
                    "asset_id": row["digest"],
                    "generation_id": existing["generation"]["generation_id"],
                    "variant_id": existing["variant"]["variant_id"],
                    "reused": True,
                }
            )
            continue
        response = transport.import_project_media(
            work["project_id"],
            payload,
            media_type=row["media_type"],
            filename=row["filename"],
            expected_digest=row["digest"],
            idempotency_key=row["import_key"],
            **{key: row[key] for key in ("width", "height", "duration_seconds") if key in row},
        )
        resource = response.get("data", response)
        if (
            resource.get("status") != "completed"
            or resource.get("asset_id") != row["digest"]
            or not resource.get("generation_id")
            or not resource.get("variant_id")
        ):
            raise ValueError("Runtime import did not return a completed managed catalog pair")
        receipts.append(dict(resource))
    # Runtime owns the exact generation/variant pair for every imported object;
    # preserve it on the detached alias so native capture can authenticate it.
    receipts_by_object = {row["asset_id"]: row for row in receipts}
    for shot in work.get("shots", {}).values():
        internal = shot.get("internal_timeline", {})
        assets = internal.get("registry", {}).get("assets", {})
        if not isinstance(assets, Mapping):
            continue
        for entry in assets.values():
            if not isinstance(entry, dict):
                continue
            receipt = receipts_by_object.get(entry.get("media_id"))
            if receipt is not None:
                entry["generationId"] = receipt["generation_id"]
                entry["variantId"] = receipt["variant_id"]
    validate_authoring_candidate(work)
    return {"candidate": work, "imports": receipts}


__all__ = ["plan_authoring_media", "import_authoring_media"]
