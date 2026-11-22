"""Naming and publication helpers for generic execution."""

from __future__ import annotations

import hashlib
import mimetypes
import re
from pathlib import Path
from typing import Any, Mapping

from astrid.core.rendering.assets import managed_asset_digest
from astrid.core.execution.host_errors import HostError


_MANAGED_ASSET_LOCATOR_KEYS = frozenset({"url", "uri", "path", "source_path", "locator", "realm", "file"})


_VIDEO_SUFFIX_MEDIA_TYPES = {
    ".mp4": "video/mp4",
    ".mov": "video/quicktime",
    ".webm": "video/webm",
    ".mkv": "video/x-matroska",
}


_SUFFIX_MEDIA_TYPES = {
    **_VIDEO_SUFFIX_MEDIA_TYPES,
    ".wav": "audio/wav",
}


_RUNTIME_OUTPUT_NAMESPACES = frozenset(
    (
        "images",
        "videos",
        "video-windows",
        "audio",
        "tiles",
        "frames",
        "cache",
        "segments",
        "outputs",
        "artifacts",
        "agent-view",
        "filmstrip-view",
    )
)


def _managed_input_staging_name(port: str, digest: str, original_name: str | None) -> str:
    """Build a stable managed-input path without using the supplied filename."""

    suffix = Path(original_name or "").suffix
    if not re.fullmatch(r"\.[A-Za-z0-9]{1,16}", suffix):
        suffix = ""
    port_digest = hashlib.sha256(port.encode("utf-8")).hexdigest()[:16]
    return f"managed-{port_digest}-{digest}{suffix.lower()}"


def _managed_asset_identity(key: str, raw: Mapping[str, Any]) -> tuple[str, str]:
    """Validate a frozen registry asset and return its CAS ID and digest hex.

    A SHA-256 media_id is already a content identity, so it may supply the
    digest when no explicit digest field is present. Opaque media IDs still
    require an explicit digest. Display metadata such as thumbnailUrl and
    generationId is permitted; locator-shaped fields remain forbidden.
    """
    if not isinstance(raw, Mapping):
        raise HostError(f"managed asset {key!r} is invalid")
    if _MANAGED_ASSET_LOCATOR_KEYS.intersection(raw):
        raise HostError(f"managed asset {key!r} contains an unmanaged locator")
    object_id = raw.get("object_id") or raw.get("media_id")
    try:
        raw_digest = managed_asset_digest(raw)
    except ValueError as exc:
        raise HostError(f"managed asset {key!r}: {exc}") from exc
    if not isinstance(object_id, str) or not object_id.strip() or not isinstance(raw_digest, str):
        raise HostError(f"managed asset {key!r} requires object_id and digest")
    digest_hex = raw_digest.removeprefix("sha256:")
    if len(digest_hex) != 64 or any(char not in "0123456789abcdef" for char in digest_hex):
        raise HostError(f"invalid managed digest for {key}")
    return object_id, digest_hex


def _settlement_media_type(descriptor: Mapping[str, Any]) -> str:
    """Publish a MIME media type while retaining internal artifact semantics."""

    from astrid.core.contracts.artifact_types import ARTIFACT_TYPE_REGISTRY

    def transport_type(value: Any) -> bool:
        if not isinstance(value, str) or any(ord(char) < 32 for char in value):
            return False
        base_type = value.split(";", 1)[0].strip()
        if ARTIFACT_TYPE_REGISTRY.is_known(base_type.lower()):
            return False
        return bool(re.fullmatch(
            r"(?:application|audio|font|image|message|model|multipart|text|video)/[A-Za-z0-9!#$&^_.+\-]+",
            base_type,
            re.IGNORECASE,
        ))

    explicit = descriptor.get("media_type")
    if isinstance(explicit, str) and explicit.strip():
        explicit = explicit.strip()
        if transport_type(explicit) and explicit.lower() != "application/octet-stream":
            return explicit
    artifact_type = str(descriptor.get("artifact_type") or "")
    filename = descriptor.get("filename")
    if isinstance(filename, str) and filename:
        suffix_media_type = _SUFFIX_MEDIA_TYPES.get(Path(filename).suffix.lower())
        if suffix_media_type is not None:
            return suffix_media_type
        guessed_media_type = mimetypes.guess_type(filename)[0]
        if guessed_media_type and guessed_media_type.lower() != "application/octet-stream":
            return guessed_media_type
    return artifact_type if transport_type(artifact_type) else "application/octet-stream"


def _runtime_output_filename(value: str) -> str:
    """Map a safe staged output name to Runtime's direct-leaf wire name.

    Producers keep known namespaces in their private attempt spool (for
    example ``images/output_000.png`` or ``agent-view/structure.md``).
    Runtime's managed output contract carries only a direct filename, so
    strip exactly one known producer namespace at the upload boundary.
    Arbitrary nesting is not a filename mapping mechanism and remains
    rejected.
    """
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 512
        or any(ord(char) < 32 for char in value)
        or "\\" in value
        or Path(value).is_absolute()
        or ".." in Path(value).parts
        or Path(value).as_posix() != value
    ):
        raise HostError("generated output has an invalid managed filename")
    parts = Path(value).parts
    if not parts or parts[-1] in {".", ".."}:
        raise HostError("generated output has an invalid managed filename")
    if len(parts) == 1:
        return parts[0]
    if len(parts) == 2 and parts[0] in _RUNTIME_OUTPUT_NAMESPACES:
        return parts[1]
    raise HostError("generated output has an invalid managed filename")


def _generation_output_port(record: Any, intent: Mapping[str, Any] | None) -> str | None:
    """Resolve the primary generated output port from the admitted schema."""
    if not isinstance(intent, Mapping):
        return None
    modality = intent.get("modality")
    if modality not in {"image", "video", "audio"}:
        return None
    expected = {"image": "generated_images", "video": "generated_videos", "audio": "generated_audio"}[modality]
    definition = getattr(record, "definition", None)
    metadata = getattr(definition, "metadata", {})
    publication = metadata.get("generation_publication") if isinstance(metadata, Mapping) else None
    if isinstance(publication, Mapping) and "output_port" in publication:
        port = publication["output_port"]
        selected = [output for output in (getattr(definition, "outputs", ()) or ()) if output.name == port]
        if (type(publication.get("version")) is not int or publication["version"] != 1
                or publication.get("modality") != modality
                or not isinstance(port, str) or not port.strip() or port != port.strip()
                or len(selected) != 1 or selected[0].type != "file"
                or port == "manifest" or port.endswith("_manifest")
                or getattr(selected[0], "artifact_type", None) == "metadata/result-manifest"):
            raise HostError("generation publication has an invalid explicit output declaration")
        return port
    if publication is not None and not isinstance(publication, Mapping):
        raise HostError("generation publication metadata must be a mapping")

    candidates = [
        output.name for output in (getattr(getattr(record, "definition", None), "outputs", ()) or ())
        if getattr(output, "type", None) == "file"
        and not str(getattr(output, "name", "")).endswith("_manifest")
        and getattr(output, "artifact_type", None)
    ]
    if candidates.count(expected) == 1:
        return expected
    if len(candidates) == 1:
        return candidates[0]
    untyped_candidates = [
        output.name for output in (getattr(getattr(record, "definition", None), "outputs", ()) or ())
        if getattr(output, "type", None) == "file"
        and not str(getattr(output, "name", "")).endswith("_manifest")
    ]
    if len(untyped_candidates) == 1:
        return untyped_candidates[0]
    raise HostError(
        f"generation capability must declare exactly one primary {modality!r} output"
    )


def _generation_selector_declarations(
    record: Any,
    intent: Mapping[str, Any] | None,
    *, output_port_resolver=_generation_output_port,
) -> tuple[str | None, tuple[dict[str, Any], ...]]:
    """Flatten admitted selector declarations without changing their order."""
    output_port = output_port_resolver(record, intent)
    if output_port is None:
        return None, ()
    declarations: list[dict[str, Any]] = []
    for group in intent["groups"]:
        for selector in group["selectors"]:
            declarations.append({
                "group_key": group["group_key"],
                "selector": selector["selector"],
                "ordinal": selector["ordinal"],
                "variant_key": selector["variant_key"],
                "output_port": output_port,
                **({"required": selector["required"]} if "required" in selector else {}),
            })
    return output_port, tuple(declarations)


def _named_raw_publication_port(task: Mapping[str, Any]) -> str | None:
    """Read the raw-stage marker reserved and populated by Runtime admission."""
    spec = task.get("spec")
    if not isinstance(spec, Mapping) or not isinstance(spec.get("delegated_parent"), Mapping):
        return None
    source = spec.get("verified_publication_source")
    if (not isinstance(spec.get("delegated_stage"), str) or not isinstance(source, Mapping)
            or set(source) != {"publication_name", "output_port"}
            or source.get("publication_name") != "raw"):
        return None
    port = source.get("output_port")
    return port if isinstance(port, str) and port else None


def _task_generation_intent(task: Mapping[str, Any]) -> Mapping[str, Any] | None:
    intent = task.get("generation_intent")
    # Named child effects are frozen by Runtime from the parent's policy.
    # Reconstruct the host's existing selector view from that effect, not
    # from the worker or from the ordering or media type of its outputs.
    spec = task.get("spec")
    source = spec.get("verified_publication_source") if isinstance(spec, Mapping) else None
    effect = task.get("expected_effect")
    if (not isinstance(spec, Mapping) or not isinstance(spec.get("delegated_parent"), Mapping)
            or not isinstance(source, Mapping) or source.get("publication_name") not in {"raw", "final"}):
        return intent if isinstance(intent, Mapping) else None
    if (not isinstance(effect, Mapping) or effect.get("effect_type") != "generation.publish_v1"
            or not isinstance(effect.get("payload"), Mapping)):
        raise HostError("named publication has no admitted generation effect")
    payload = effect["payload"]
    groups = payload.get("groups")
    if not isinstance(groups, list):
        raise HostError("named publication effect has no selector groups")
    from astrid.sdk.exceptions import CapabilityValidationError
    from astrid.sdk.invocation import _validate_generation_intent
    try:
        projected = [{"group_key": group["group_key"], "selectors": [
            {key: value for key, value in selector.items() if key != "output_port"}
            for selector in group["selectors"]]} for group in groups]
        if any(selector.get("output_port") != source.get("output_port")
               for group in groups for selector in group["selectors"]):
            raise HostError("named publication effect selects another port")
        return _validate_generation_intent({"version": payload["version"], "modality": payload["modality"],
            "partial_success_policy": payload["partial_success_policy"], "groups": projected},
            modality=payload["modality"])
    except (KeyError, TypeError, ValueError, CapabilityValidationError) as exc:
        raise HostError("named publication effect has an invalid selector") from exc


def _bind_named_parent_output(task: Mapping[str, Any], outputs: list[dict[str, Any]]) -> None:
    spec = task.get("spec")
    policy = spec.get("child_delegation") if isinstance(spec, Mapping) else None
    publications = policy.get("publications") if isinstance(policy, Mapping) else None
    if publications is None:
        return
    try:
        final = publications["final"]
        groups = final["effect"]["payload"]["groups"]
        if len(groups) != 1 or len(groups[0]["selectors"]) != 1:
            raise ValueError("expected one final selector")
        group, selector = groups[0], groups[0]["selectors"][0]
        port = final["output_port"]
        if selector["output_port"] != port:
            raise ValueError("final selector port differs")
        matches = [row for row in outputs if row.get("name") == port
                   and row.get("ordinal") == selector["ordinal"]]
        if len(matches) != 1:
            raise ValueError("final output missing or ambiguous")
        matches[0].update(output_port=port, group_key=group["group_key"],
                          variant_key=selector["variant_key"],
                          selector={"group_key": group["group_key"], "variant_key": selector["variant_key"]})
    except (KeyError, TypeError, ValueError) as exc:
        raise HostError("named parent final selector cannot be bound") from exc
