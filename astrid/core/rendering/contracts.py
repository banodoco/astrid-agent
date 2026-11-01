"""Language-neutral rendering protocol data transfer objects.

The JSON Schemas in :mod:`astrid.core.rendering.schemas.v1` are the wire
source of truth.  These frozen dataclasses are the small Python projection of
that contract; they deliberately contain no discovery, transport, or backend
execution behavior.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, fields, is_dataclass
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import Any, ClassVar, Literal, NoReturn, TypeAlias

from astrid.core.foundation.hash import canonical_json_digest, sha256_file


SCHEMA_VERSION = 1

#: Qualified ids of the two renderers a host may choose between by readiness.
REMOTION_RENDERER_ID = "rendering.remotion"
FFMPEG_RENDERER_ID = "rendering.ffmpeg"

BackendConfig: TypeAlias = dict[str, dict[str, Any]]
RendererErrorKind: TypeAlias = Literal[
    "protocol",
    "unsupported",
    "binary_missing",
    "timeout",
    "interrupted",
    "invalid_artifact",
    "internal",
]

_QUALIFIED_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*(?:\.[a-z0-9][a-z0-9_-]*)+$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_OUTPUT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_KIND_RE = re.compile(r"^[a-z][a-z0-9-]*$")

# ECMAScript \s whitespace set, spelled as explicit characters so it is
# identical in the DTO and the JSON Schemas (Python str.strip() has no
# range syntax and differs from ECMAScript on \u0085 and \uFEFF).
_ECMA_WHITESPACE = (
    " \t\n\r\f\v\u00a0\u1680"
    "\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a"
    "\u2028\u2029\u202f\u205f\u3000\ufeff"
)

RENDER_RESULT_CORE_KEYS = frozenset(
    {
        "schema_version",
        "video",
        "backend_fragments",
        "audio_ownership",
        "normalization",
        "logs",
        "metadata",
    }
)

PROVENANCE_V2_CORE_KEYS = frozenset(
    {
        "schema_version",
        "engine",
        "output",
        "timeline",
        "assets_registry",
        "request_digest",
        "requested_policy",
        "planner",
        "segments_v2",
        "artifact_profiles",
        "audio_ownership",
        "normalization",
        "finalizer",
        "attachments",
        "backend_fragments",
    }
)

_RETIRED_PROVENANCE_V2_KEYS = frozenset(
    {
        "resolved_backend",
        "source_pack",
        "alias_chain",
        "override",
        "trust_eligibility",
        "manifest_digest",
        "support_decision",
        "input_hashes",
    }
)

RESERVED_BACKEND_FRAGMENT_KEYS = frozenset(
    RENDER_RESULT_CORE_KEYS
    | PROVENANCE_V2_CORE_KEYS
    | _RETIRED_PROVENANCE_V2_KEYS
)


def _json_safe(value: Any) -> Any:
    """Return a recursively JSON-safe copy, rejecting non-wire values."""

    if isinstance(value, Enum):
        return _json_safe(value.value)
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise TypeError("JSON numbers must be finite")
        return value
    if isinstance(value, Path):
        return str(value)
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        return _json_safe(to_dict())
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if is_dataclass(value):
        return {
            dataclass_field.name: _json_safe(getattr(value, dataclass_field.name))
            for dataclass_field in fields(value)
        }
    raise TypeError(f"value of type {type(value).__name__} is not JSON-safe")


def _json_safe_mapping(value: Any, *, label: str = "value") -> dict[str, Any]:
    payload = _json_safe(value)
    if not isinstance(payload, dict):
        raise TypeError(f"{label} must be a JSON object")
    return payload


def _require_mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{label} must be an object")
    return value


def _validate_object_keys(
    payload: Mapping[str, Any],
    *,
    required: set[str] | frozenset[str],
    allowed: set[str] | frozenset[str],
    label: str,
) -> None:
    missing = sorted(required - payload.keys())
    if missing:
        raise ValueError(f"{label} is missing required fields: {', '.join(missing)}")
    unknown = sorted(payload.keys() - allowed)
    if unknown:
        raise ValueError(f"{label} has unknown fields: {', '.join(unknown)}")


def _require_int(value: Any, label: str, *, minimum: int | None = None) -> int:
    if type(value) is not int:
        raise TypeError(f"{label} must be an integer")
    if minimum is not None and value < minimum:
        raise ValueError(f"{label} must be >= {minimum}")
    return value


def _require_number(value: Any, label: str, *, exclusive_minimum: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{label} must be a number")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{label} must be finite")
    if exclusive_minimum is not None and number <= exclusive_minimum:
        raise ValueError(f"{label} must be > {exclusive_minimum:g}")
    return number


def compute_request_digest(request: Mapping[str, Any]) -> str:
    """Deterministic SHA-256 of a canonical, JSON-normalized render request.

    Uses sorted keys and compact separators so the digest is stable across
    Python versions and dict insertion orders; replay verifies the request
    against this digest.
    """
    return canonical_json_digest(_json_safe_mapping(request, label="render request"))


def _require_string(value: Any, label: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{label} must be a string")
    if "\x00" in value:
        raise ValueError(f"{label} must not contain NUL")
    if not allow_empty and not value.strip(_ECMA_WHITESPACE):
        raise ValueError(f"{label} must not be empty")
    return value


def _require_optional_string(value: Any, label: str) -> str | None:
    if value is None:
        return None
    return _require_string(value, label)


def _require_qualified_id(value: Any, label: str) -> str:
    result = _require_string(value, label)
    if not _QUALIFIED_ID_RE.fullmatch(result):
        raise ValueError(
            f"{label} must be a qualified id '<pack>.<name>' whose dot-separated "
            "segments use lowercase letters, digits, and hyphens"
        )
    return result


def _require_sha256(value: Any, label: str) -> str:
    result = _require_string(value, label)
    if not _SHA256_RE.fullmatch(result):
        raise ValueError(f"{label} must be a lowercase 64-character SHA-256 digest")
    return result


def _require_override(value: Any, *, capability_id: str, label: str) -> dict[str, Any]:
    """Validate an override record: ``{from, to}`` with ``to`` equal to the
    resolution id (the override is what selected this implementation)."""
    mapping = _json_safe_mapping(value, label=label)
    required = {"from", "to"}
    if set(mapping) != required:
        raise ValueError(f"{label} must contain exactly 'from' and 'to'")
    _require_qualified_id(mapping["from"], f"{label} 'from'")
    resolved = _require_qualified_id(mapping["to"], f"{label} 'to'")
    if resolved != capability_id:
        raise ValueError(f"{label} 'to' must equal the resolved capability id {capability_id!r}")
    return mapping


def _require_string_list(value: Any, label: str) -> list[str]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise TypeError(f"{label} must be an array of strings")
    return [_require_string(item, f"{label}[{index}]") for index, item in enumerate(value)]


def _require_string_mapping(value: Any, label: str) -> dict[str, str]:
    mapping = _require_mapping(value, label)
    return {
        _require_string(key, f"{label} key"): _require_string(item, f"{label}[{key!r}]")
        for key, item in mapping.items()
    }


def _require_hash_mapping(value: Any, label: str) -> dict[str, str]:
    mapping = _require_mapping(value, label)
    return {
        _require_string(key, f"{label} key"): _require_sha256(item, f"{label}[{key!r}]")
        for key, item in mapping.items()
    }


def _require_schema_version(value: Any, label: str) -> int:
    if type(value) is not int or value != SCHEMA_VERSION:
        _protocol_failure(
            f"unknown or malformed {label} schema_version {value!r}; "
            f"expected integer {SCHEMA_VERSION}",
            details={"received": value, "supported": [SCHEMA_VERSION]},
        )
    return value


def _require_rational(value: Any, label: str) -> tuple[int, int]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence) or len(value) != 2:
        raise TypeError(f"{label} must be a two-item [numerator, denominator] array")
    numerator = _require_int(value[0], f"{label}[0]", minimum=1)
    denominator = _require_int(value[1], f"{label}[1]", minimum=1)
    return numerator, denominator


def _require_frame_range(value: Any, label: str) -> tuple[int, int]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence) or len(value) != 2:
        raise TypeError(f"{label} must be a two-item [start_frame, end_frame] array")
    start = _require_int(value[0], f"{label}[0]", minimum=0)
    end = _require_int(value[1], f"{label}[1]", minimum=1)
    if end <= start:
        raise ValueError(f"{label} must be half-open with end_frame > start_frame")
    return start, end


def _require_workspace_relative_path(value: Any, label: str) -> str:
    raw = _require_string(value, label)
    if "\\" in raw:
        raise ValueError(f"{label} must be a normalized workspace path using forward slashes")
    normalized = raw.replace("\\", "/")
    if normalized.startswith("/") or re.match(r"^[A-Za-z]:", normalized):
        raise ValueError(f"{label} must be relative to the invocation workspace")
    if normalized.startswith("//"):
        raise ValueError(f"{label} must not be a UNC path")
    raw_parts = normalized.split("/")
    parts = PurePosixPath(normalized).parts
    if not parts or any(part in {"", ".", ".."} for part in raw_parts):
        raise ValueError(f"{label} must be a normalized contained workspace path")
    if any(not part.strip(_ECMA_WHITESPACE) for part in raw_parts):
        raise ValueError(f"{label} must not contain empty or whitespace-only path components")
    return raw


def _relative_file_path(path: str | Path, workspace_root: str | Path, label: str) -> tuple[str, Path]:
    root = Path(workspace_root).resolve()
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = root / candidate
    resolved = candidate.resolve(strict=True)
    try:
        relative = resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"{label} escapes invocation workspace {root}") from exc
    return relative.as_posix(), resolved


def _protocol_failure(message: str, *, details: Mapping[str, Any] | None = None) -> NoReturn:
    from .errors import raise_protocol_error

    raise_protocol_error(
        backend="astrid.core",
        message=message,
        details=dict(details or {}),
    )


class AudioOwnership(str, Enum):
    """Who is responsible for audio in a returned primary video."""

    RENDERED = "rendered"
    PASSTHROUGH = "passthrough"
    NONE = "none"


def _coerce_audio_ownership(value: Any, label: str, *, nullable: bool) -> AudioOwnership | None:
    if value is None and nullable:
        return None
    if isinstance(value, AudioOwnership):
        return value
    if isinstance(value, str):
        try:
            return AudioOwnership(value)
        except ValueError as exc:
            raise ValueError(
                f"{label} must be one of: {', '.join(item.value for item in AudioOwnership)}"
            ) from exc
    raise TypeError(f"{label} must be an audio ownership string")


@dataclass(frozen=True)
class FrameWindow:
    """A half-open integer frame window ``[start_frame, end_frame)``."""

    start_frame: int
    end_frame: int
    fps_rational: tuple[int, int]
    source_range: tuple[int, int] | None = None
    speed: float | None = None

    def __post_init__(self) -> None:
        start = _require_int(self.start_frame, "start_frame", minimum=0)
        end = _require_int(self.end_frame, "end_frame", minimum=1)
        if end <= start:
            raise ValueError("end_frame must be greater than start_frame")
        object.__setattr__(self, "start_frame", start)
        object.__setattr__(self, "end_frame", end)
        object.__setattr__(self, "fps_rational", _require_rational(self.fps_rational, "fps_rational"))
        if self.source_range is not None:
            object.__setattr__(
                self,
                "source_range",
                _require_frame_range(self.source_range, "source_range"),
            )
        if self.speed is not None:
            object.__setattr__(
                self,
                "speed",
                _require_number(self.speed, "speed", exclusive_minimum=0),
            )

    @property
    def duration_frames(self) -> int:
        return self.end_frame - self.start_frame

    def to_dict(self) -> dict[str, Any]:
        return _json_safe_mapping(
            {
                "start_frame": self.start_frame,
                "end_frame": self.end_frame,
                "fps_rational": self.fps_rational,
                "source_range": self.source_range,
                "speed": self.speed,
            }
        )

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> FrameWindow:
        data = _require_mapping(payload, "frame window")
        _validate_object_keys(
            data,
            required={"start_frame", "end_frame", "fps_rational"},
            allowed={"start_frame", "end_frame", "fps_rational", "source_range", "speed"},
            label="frame window",
        )
        return cls(
            start_frame=data["start_frame"],
            end_frame=data["end_frame"],
            fps_rational=data["fps_rational"],
            source_range=data.get("source_range"),
            speed=data.get("speed"),
        )


@dataclass(frozen=True)
class RenderProfile:
    """Resolved media profile used to validate and finalize artifacts."""

    width: int
    height: int
    fps_rational: tuple[int, int]
    time_base: tuple[int, int]
    video_codec: str
    pixel_format: str
    video_profile: str | None = None
    video_level: str | None = None
    container: str = "mp4"
    audio_codec: str | None = None
    audio_sample_rate: int | None = None
    audio_channel_layout: str | None = None
    duration_tolerance: int = 1

    def __post_init__(self) -> None:
        object.__setattr__(self, "width", _require_int(self.width, "width", minimum=1))
        object.__setattr__(self, "height", _require_int(self.height, "height", minimum=1))
        object.__setattr__(self, "fps_rational", _require_rational(self.fps_rational, "fps_rational"))
        object.__setattr__(self, "time_base", _require_rational(self.time_base, "time_base"))
        object.__setattr__(self, "video_codec", _require_string(self.video_codec, "video_codec"))
        object.__setattr__(self, "pixel_format", _require_string(self.pixel_format, "pixel_format"))
        object.__setattr__(
            self,
            "video_profile",
            _require_optional_string(self.video_profile, "video_profile"),
        )
        object.__setattr__(
            self,
            "video_level",
            _require_optional_string(self.video_level, "video_level"),
        )
        object.__setattr__(self, "container", _require_string(self.container, "container"))
        audio_values = (
            self.audio_codec,
            self.audio_sample_rate,
            self.audio_channel_layout,
        )
        if any(value is not None for value in audio_values) and not all(
            value is not None for value in audio_values
        ):
            raise ValueError(
                "audio_codec, audio_sample_rate, and audio_channel_layout must be "
                "provided together or all omitted"
            )
        if self.audio_codec is not None:
            object.__setattr__(self, "audio_codec", _require_string(self.audio_codec, "audio_codec"))
            object.__setattr__(
                self,
                "audio_sample_rate",
                _require_int(self.audio_sample_rate, "audio_sample_rate", minimum=1),
            )
            object.__setattr__(
                self,
                "audio_channel_layout",
                _require_string(self.audio_channel_layout, "audio_channel_layout"),
            )
        object.__setattr__(
            self,
            "duration_tolerance",
            _require_int(self.duration_tolerance, "duration_tolerance", minimum=0),
        )

    @property
    def has_audio(self) -> bool:
        return self.audio_codec is not None

    def to_dict(self) -> dict[str, Any]:
        return _json_safe_mapping(
            {
                "width": self.width,
                "height": self.height,
                "fps_rational": self.fps_rational,
                "time_base": self.time_base,
                "container": self.container,
                "video_codec": self.video_codec,
                "video_profile": self.video_profile,
                "video_level": self.video_level,
                "pixel_format": self.pixel_format,
                "audio_codec": self.audio_codec,
                "audio_sample_rate": self.audio_sample_rate,
                "audio_channel_layout": self.audio_channel_layout,
                "duration_tolerance": self.duration_tolerance,
            }
        )

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> RenderProfile:
        data = _require_mapping(payload, "render profile")
        required = {
            "width",
            "height",
            "fps_rational",
            "time_base",
            "container",
            "video_codec",
            "video_profile",
            "video_level",
            "pixel_format",
            "duration_tolerance",
        }
        allowed = required | {"audio_codec", "audio_sample_rate", "audio_channel_layout"}
        _validate_object_keys(data, required=required, allowed=allowed, label="render profile")
        return cls(
            width=data["width"],
            height=data["height"],
            fps_rational=data["fps_rational"],
            time_base=data["time_base"],
            container=data["container"],
            video_codec=data["video_codec"],
            video_profile=data["video_profile"],
            video_level=data["video_level"],
            pixel_format=data["pixel_format"],
            audio_codec=data.get("audio_codec"),
            audio_sample_rate=data.get("audio_sample_rate"),
            audio_channel_layout=data.get("audio_channel_layout"),
            duration_tolerance=data["duration_tolerance"],
        )


def _validate_artifact_audio(
    profile: RenderProfile,
    ownership: AudioOwnership | None,
    label: str,
) -> None:
    """Keep probed media audio and ownership semantically aligned.

    ``rendered`` means the artifact itself contains audio and therefore has a
    populated audio profile. ``passthrough`` and ``none`` describe visual-only
    artifacts; the former asks the host/finalizer to supply canonical audio.
    """

    if profile.has_audio:
        if ownership is not AudioOwnership.RENDERED:
            raise ValueError(f"{label} with an audio profile must declare audio='rendered'")
    elif ownership is AudioOwnership.RENDERED:
        raise ValueError(f"{label} with audio='rendered' must have an audio profile")


@dataclass(frozen=True)
class Attachment:
    """A named, opaque artifact preserved alongside the primary video."""

    name: str
    path: str
    kind: str
    sha256: str

    def __post_init__(self) -> None:
        name = _require_string(self.name, "attachment name")
        if not _OUTPUT_NAME_RE.fullmatch(name):
            raise ValueError("attachment name must be a portable basename")
        kind = _require_string(self.kind, "attachment kind")
        if not _KIND_RE.fullmatch(kind):
            raise ValueError("attachment kind must be a lowercase hyphenated token")
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "path", _require_workspace_relative_path(self.path, "attachment path"))
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "sha256", _require_sha256(self.sha256, "attachment sha256"))

    def to_dict(self) -> dict[str, Any]:
        return _json_safe_mapping(
            {"name": self.name, "path": self.path, "kind": self.kind, "sha256": self.sha256}
        )

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> Attachment:
        data = _require_mapping(payload, "attachment")
        required = {"name", "path", "kind", "sha256"}
        _validate_object_keys(data, required=required, allowed=required, label="attachment")
        return cls(
            name=data["name"],
            path=data["path"],
            kind=data["kind"],
            sha256=data["sha256"],
        )

    @classmethod
    def from_file(
        cls,
        *,
        name: str,
        path: str | Path,
        kind: str,
        workspace_root: str | Path,
    ) -> Attachment:
        relative, resolved = _relative_file_path(path, workspace_root, "attachment path")
        return cls(name=name, path=relative, kind=kind, sha256=sha256_file(resolved))


def _coerce_attachment_mapping(value: Any, label: str) -> dict[str, Attachment]:
    mapping = _require_mapping(value, label)
    result: dict[str, Attachment] = {}
    seen_names: set[str] = set()
    for raw_key, raw_attachment in mapping.items():
        key = _require_string(raw_key, f"{label} key")
        attachment = (
            raw_attachment
            if isinstance(raw_attachment, Attachment)
            else Attachment.from_dict(_require_mapping(raw_attachment, f"{label}[{key!r}]"))
        )
        if attachment.name != key:
            raise ValueError(
                f"{label} key {key!r} must match attachment.name {attachment.name!r}"
            )
        if attachment.name in seen_names:
            raise ValueError(f"duplicate attachment name: {attachment.name}")
        seen_names.add(attachment.name)
        result[key] = attachment
    return result


@dataclass(frozen=True)
class VideoArtifact:
    """The required primary video produced by a renderer or finalizer."""

    path: str
    profile: RenderProfile
    sha256: str
    duration_frames: int
    audio: AudioOwnership | None = None
    attachments: dict[str, Attachment] = field(default_factory=dict)

    def __post_init__(self) -> None:
        profile = (
            self.profile
            if isinstance(self.profile, RenderProfile)
            else RenderProfile.from_dict(_require_mapping(self.profile, "video profile"))
        )
        object.__setattr__(self, "path", _require_workspace_relative_path(self.path, "video path"))
        object.__setattr__(self, "profile", profile)
        object.__setattr__(self, "sha256", _require_sha256(self.sha256, "video sha256"))
        object.__setattr__(
            self,
            "duration_frames",
            _require_int(self.duration_frames, "duration_frames", minimum=1),
        )
        audio = _coerce_audio_ownership(self.audio, "video audio", nullable=True)
        _validate_artifact_audio(profile, audio, "video artifact")
        object.__setattr__(self, "audio", audio)
        object.__setattr__(
            self,
            "attachments",
            _coerce_attachment_mapping(self.attachments, "video attachments"),
        )

    def to_dict(self) -> dict[str, Any]:
        return _json_safe_mapping(
            {
                "path": self.path,
                "profile": self.profile,
                "sha256": self.sha256,
                "duration_frames": self.duration_frames,
                "audio": self.audio,
                "attachments": self.attachments,
            }
        )

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> VideoArtifact:
        data = _require_mapping(payload, "video artifact")
        required = {"path", "profile", "sha256", "duration_frames"}
        allowed = required | {"audio", "attachments"}
        _validate_object_keys(data, required=required, allowed=allowed, label="video artifact")
        return cls(
            path=data["path"],
            profile=RenderProfile.from_dict(data["profile"]),
            sha256=data["sha256"],
            duration_frames=data["duration_frames"],
            audio=data.get("audio"),
            attachments=data.get("attachments", {}),
        )

    @classmethod
    def from_file(
        cls,
        *,
        path: str | Path,
        workspace_root: str | Path,
        profile: RenderProfile,
        duration_frames: int,
        audio: AudioOwnership | None = None,
        attachments: Mapping[str, Attachment] | None = None,
    ) -> VideoArtifact:
        relative, resolved = _relative_file_path(path, workspace_root, "video path")
        return cls(
            path=relative,
            profile=profile,
            sha256=sha256_file(resolved),
            duration_frames=duration_frames,
            audio=audio,
            attachments=dict(attachments or {}),
        )


def _coerce_profile(value: Any, label: str, *, nullable: bool) -> RenderProfile | None:
    if value is None and nullable:
        return None
    if isinstance(value, RenderProfile):
        return value
    return RenderProfile.from_dict(_require_mapping(value, label))


def _coerce_window(value: Any, label: str, *, nullable: bool) -> FrameWindow | None:
    if value is None and nullable:
        return None
    if isinstance(value, FrameWindow):
        return value
    return FrameWindow.from_dict(_require_mapping(value, label))


def _coerce_namespaced_backend_config(value: Any, label: str) -> BackendConfig:
    mapping = _require_mapping(value, label)
    result: BackendConfig = {}
    for raw_backend, raw_config in mapping.items():
        backend = _require_qualified_id(raw_backend, f"{label} key")
        result[backend] = _json_safe_mapping(raw_config, label=f"{label}[{backend!r}]")
    return result


@dataclass(frozen=True)
class RenderRequest:
    """Backend-neutral request shared by render, support, and plan operations."""

    schema_version: int
    timeline_path: str
    output_name: str
    assets_registry_path: str | None = None
    window: FrameWindow | None = None
    audio: AudioOwnership | None = None
    profile: RenderProfile | None = None
    backend_config: BackendConfig = field(default_factory=dict)
    metadata: dict[str, str] = field(default_factory=dict)
    # Explicit host-owned execution handoff. Values are attempt-local paths;
    # they are never authority or durable media locators.
    materialized_root: str | None = None
    materialized_objects: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != SCHEMA_VERSION:
            _protocol_failure(
                f"unknown or malformed render request schema_version "
                f"{self.schema_version!r}; expected integer {SCHEMA_VERSION}",
                details={"received": self.schema_version, "supported": [SCHEMA_VERSION]},
            )
        version = self.schema_version
        object.__setattr__(self, "schema_version", version)
        object.__setattr__(self, "timeline_path", _require_string(self.timeline_path, "timeline_path"))
        object.__setattr__(
            self,
            "assets_registry_path",
            _require_optional_string(self.assets_registry_path, "assets_registry_path"),
        )
        output_name = _require_string(self.output_name, "output_name")
        if not _OUTPUT_NAME_RE.fullmatch(output_name) or output_name in {".", ".."}:
            raise ValueError("output_name must be a portable basename without path separators")
        object.__setattr__(self, "output_name", output_name)
        object.__setattr__(self, "window", _coerce_window(self.window, "window", nullable=True))
        audio = _coerce_audio_ownership(self.audio, "audio", nullable=True)
        profile = _coerce_profile(self.profile, "profile", nullable=True)
        if audio is not None and profile is not None:
            _validate_artifact_audio(profile, audio, "render request")
        object.__setattr__(self, "audio", audio)
        object.__setattr__(self, "profile", profile)
        object.__setattr__(
            self,
            "backend_config",
            _coerce_namespaced_backend_config(self.backend_config, "backend_config"),
        )
        object.__setattr__(self, "metadata", _require_string_mapping(self.metadata, "metadata"))
        object.__setattr__(self, "materialized_root", _require_optional_string(self.materialized_root, "materialized_root"))
        handoff = _require_mapping(self.materialized_objects, "materialized_objects")
        normalized_handoff: dict[str, str] = {}
        for raw_key, raw_value in handoff.items():
            key = _require_string(raw_key, "materialized_objects key")
            normalized_handoff[key] = _require_string(raw_value, f"materialized_objects[{key!r}]")
        if normalized_handoff and self.materialized_root is None:
            raise ValueError("materialized_root is required with materialized_objects")
        object.__setattr__(self, "materialized_objects", normalized_handoff)

    def to_dict(self) -> dict[str, Any]:
        return _json_safe_mapping(
            {
                "schema_version": self.schema_version,
                "timeline_path": self.timeline_path,
                "assets_registry_path": self.assets_registry_path,
                "output_name": self.output_name,
                "window": self.window,
                "audio": self.audio,
                "profile": self.profile,
                "backend_config": self.backend_config,
                "metadata": self.metadata,
                **(
                    {"materialized_root": self.materialized_root, "materialized_objects": self.materialized_objects}
                    if self.materialized_root is not None or self.materialized_objects else {}
                ),
            }
        )

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> RenderRequest:
        try:
            data = _require_mapping(payload, "render request")
            allowed = {
                "schema_version",
                "timeline_path",
                "assets_registry_path",
                "output_name",
                "window",
                "audio",
                "profile",
                "backend_config",
                "metadata",
                "materialized_root",
                "materialized_objects",
            }
            _validate_object_keys(
                data,
                required={"schema_version", "timeline_path", "output_name"},
                allowed=allowed,
                label="render request",
            )
            version = data["schema_version"]
            if type(version) is not int or version != SCHEMA_VERSION:
                _protocol_failure(
                    f"unknown or malformed render request schema_version {version!r}; "
                    f"expected integer {SCHEMA_VERSION}",
                    details={"received": version, "supported": [SCHEMA_VERSION]},
                )
            return cls(
                schema_version=version,
                timeline_path=data["timeline_path"],
                assets_registry_path=data.get("assets_registry_path"),
                output_name=data["output_name"],
                window=data.get("window"),
                audio=data.get("audio"),
                profile=data.get("profile"),
                backend_config=data.get("backend_config", {}),
                metadata=data.get("metadata", {}),
                materialized_root=data.get("materialized_root"),
                materialized_objects=data.get("materialized_objects", {}),
            )
        except Exception as exc:
            from .errors import RendererException

            if isinstance(exc, RendererException):
                raise
            _protocol_failure(
                f"malformed render request: {exc}",
                details={"error_type": type(exc).__name__},
            )

    def for_backend(self, backend: str) -> RenderRequest:
        """Return the request projection visible to one selected backend."""

        qualified = _require_qualified_id(backend, "backend")
        selected = self.backend_config.get(qualified)
        return RenderRequest(
            schema_version=self.schema_version,
            timeline_path=self.timeline_path,
            assets_registry_path=self.assets_registry_path,
            output_name=self.output_name,
            window=self.window,
            audio=self.audio,
            profile=self.profile,
            backend_config={qualified: selected} if selected is not None else {},
            metadata=self.metadata,
            materialized_root=self.materialized_root,
            materialized_objects=self.materialized_objects,
        )


@dataclass(frozen=True)
class SupportReport:
    """Request-sensitive support evidence returned by an implementation."""

    schema_version: int
    supported: bool
    reasons: list[str]
    features: dict[str, bool | str]
    alternatives: list[str]
    backend: str
    backend_version: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "schema_version",
            _require_schema_version(self.schema_version, "support report"),
        )
        if not isinstance(self.supported, bool):
            raise TypeError("supported must be a boolean")
        object.__setattr__(self, "reasons", _require_string_list(self.reasons, "reasons"))
        feature_mapping = _require_mapping(self.features, "features")
        features: dict[str, bool | str] = {}
        for raw_key, raw_value in feature_mapping.items():
            key = _require_string(raw_key, "feature key")
            if not isinstance(raw_value, (bool, str)):
                raise TypeError(f"features[{key!r}] must be a boolean or string")
            features[key] = raw_value
        object.__setattr__(self, "features", features)
        alternatives = [
            _require_qualified_id(item, f"alternatives[{index}]")
            for index, item in enumerate(_require_string_list(self.alternatives, "alternatives"))
        ]
        if len(alternatives) != len(set(alternatives)):
            raise ValueError("alternatives must not contain duplicate backend ids")
        object.__setattr__(self, "alternatives", alternatives)
        object.__setattr__(self, "backend", _require_qualified_id(self.backend, "backend"))
        object.__setattr__(
            self,
            "backend_version",
            _require_optional_string(self.backend_version, "backend_version"),
        )

    def to_dict(self) -> dict[str, Any]:
        return _json_safe_mapping(
            {
                "schema_version": self.schema_version,
                "supported": self.supported,
                "reasons": self.reasons,
                "features": self.features,
                "alternatives": self.alternatives,
                "backend": self.backend,
                "backend_version": self.backend_version,
            }
        )

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> SupportReport:
        try:
            data = _require_mapping(payload, "support report")
            required = {
                "schema_version",
                "supported",
                "reasons",
                "features",
                "alternatives",
                "backend",
                "backend_version",
            }
            _validate_object_keys(
                data,
                required=required,
                allowed=required,
                label="support report",
            )
            return cls(
                schema_version=data["schema_version"],
                supported=data["supported"],
                reasons=data["reasons"],
                features=data["features"],
                alternatives=data["alternatives"],
                backend=data["backend"],
                backend_version=data["backend_version"],
            )
        except Exception as exc:
            from .errors import RendererException

            if isinstance(exc, RendererException):
                raise
            _protocol_failure(
                f"malformed support report: {exc}",
                details={"error_type": type(exc).__name__},
            )


@dataclass(frozen=True)
class PlannerResolution:
    """Resolved planner identity and trust evidence frozen into a plan."""

    id: str
    source_pack: dict[str, Any]
    manifest_digest: str
    trust_eligibility: dict[str, Any]
    alias_chain: list[str] = field(default_factory=list)
    override: dict[str, Any] | None = None
    support_decision: SupportReport | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _require_qualified_id(self.id, "planner id"))
        object.__setattr__(
            self,
            "source_pack",
            _json_safe_mapping(self.source_pack, label="planner source_pack"),
        )
        object.__setattr__(
            self,
            "manifest_digest",
            _require_sha256(self.manifest_digest, "planner manifest_digest"),
        )
        object.__setattr__(
            self,
            "trust_eligibility",
            _json_safe_mapping(
                self.trust_eligibility,
                label="planner trust_eligibility",
            ),
        )
        object.__setattr__(
            self,
            "alias_chain",
            [
                _require_string(item, f"planner alias_chain[{index}]")
                for index, item in enumerate(_require_string_list(self.alias_chain, "planner alias_chain"))
            ],
        )
        if self.override is not None:
            object.__setattr__(
                self,
                "override",
                _require_override(
                    self.override,
                    capability_id=self.id,
                    label="planner override",
                ),
            )
        if self.support_decision is not None:
            support = (
                self.support_decision
                if isinstance(self.support_decision, SupportReport)
                else SupportReport.from_dict(
                    _require_mapping(
                        self.support_decision, "planner support_decision"
                    )
                )
            )
            if support.backend != self.id:
                raise ValueError("planner support_decision.backend must match planner id")
            object.__setattr__(self, "support_decision", support)

    def to_dict(self) -> dict[str, Any]:
        return _json_safe_mapping(
            {
                "id": self.id,
                "source_pack": self.source_pack,
                "manifest_digest": self.manifest_digest,
                "trust_eligibility": self.trust_eligibility,
                "alias_chain": list(self.alias_chain),
                "override": self.override,
                "support_decision": self.support_decision,
            }
        )

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> PlannerResolution:
        data = _require_mapping(payload, "planner resolution")
        required = {
            "id",
            "source_pack",
            "manifest_digest",
            "trust_eligibility",
            "alias_chain",
            "override",
            "support_decision",
        }
        _validate_object_keys(data, required=required, allowed=required, label="planner resolution")
        return cls(
            id=data["id"],
            source_pack=data["source_pack"],
            manifest_digest=data["manifest_digest"],
            trust_eligibility=data["trust_eligibility"],
            alias_chain=data["alias_chain"],
            override=data["override"],
            support_decision=data["support_decision"],
        )


@dataclass(frozen=True)
class RendererResolution:
    """Resolved renderer identity and request-sensitive routing evidence."""

    id: str
    source_pack: dict[str, Any]
    manifest_digest: str
    alias_chain: list[str]
    override: dict[str, Any] | None
    support_decision: SupportReport
    trust_eligibility: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        renderer_id = _require_qualified_id(self.id, "renderer id")
        support = (
            self.support_decision
            if isinstance(self.support_decision, SupportReport)
            else SupportReport.from_dict(
                _require_mapping(self.support_decision, "renderer support_decision")
            )
        )
        if support.backend != renderer_id:
            raise ValueError("renderer support_decision.backend must match renderer id")
        object.__setattr__(self, "id", renderer_id)
        object.__setattr__(
            self,
            "source_pack",
            _json_safe_mapping(self.source_pack, label="renderer source_pack"),
        )
        object.__setattr__(
            self,
            "manifest_digest",
            _require_sha256(self.manifest_digest, "renderer manifest_digest"),
        )
        object.__setattr__(
            self,
            "trust_eligibility",
            _json_safe_mapping(
                self.trust_eligibility,
                label="renderer trust_eligibility",
            ),
        )
        aliases = [
            _require_string(alias, f"renderer alias_chain[{index}]")
            for index, alias in enumerate(_require_string_list(self.alias_chain, "renderer alias_chain"))
        ]
        if len(aliases) != len(set(aliases)):
            raise ValueError("renderer alias_chain must not contain duplicates")
        object.__setattr__(self, "alias_chain", aliases)
        object.__setattr__(
            self,
            "override",
            None
            if self.override is None
            else _require_override(
                self.override,
                capability_id=renderer_id,
                label="renderer override",
            ),
        )
        object.__setattr__(self, "support_decision", support)

    def to_dict(self) -> dict[str, Any]:
        return _json_safe_mapping(
            {
                "id": self.id,
                "source_pack": self.source_pack,
                "manifest_digest": self.manifest_digest,
                "alias_chain": self.alias_chain,
                "override": self.override,
                "support_decision": self.support_decision,
                "trust_eligibility": self.trust_eligibility,
            }
        )

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> RendererResolution:
        data = _require_mapping(payload, "renderer resolution")
        required = {
            "id",
            "source_pack",
            "manifest_digest",
            "alias_chain",
            "override",
            "support_decision",
            "trust_eligibility",
        }
        _validate_object_keys(data, required=required, allowed=required, label="renderer resolution")
        return cls(
            id=data["id"],
            source_pack=data["source_pack"],
            manifest_digest=data["manifest_digest"],
            alias_chain=data["alias_chain"],
            override=data["override"],
            support_decision=SupportReport.from_dict(data["support_decision"]),
            trust_eligibility=data["trust_eligibility"],
        )


@dataclass(frozen=True)
class FinalizerResolution:
    """Resolved finalizer identity pinned for standalone finalization."""

    id: str
    source_pack: dict[str, Any]
    manifest_digest: str
    alias_chain: list[str] = field(default_factory=list)
    override: dict[str, Any] | None = None
    trust_eligibility: dict[str, Any] = field(default_factory=dict)
    support_decision: SupportReport | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _require_qualified_id(self.id, "finalizer id"))
        object.__setattr__(
            self,
            "source_pack",
            _json_safe_mapping(self.source_pack, label="finalizer source_pack"),
        )
        object.__setattr__(
            self,
            "manifest_digest",
            _require_sha256(self.manifest_digest, "finalizer manifest_digest"),
        )
        object.__setattr__(
            self,
            "trust_eligibility",
            _json_safe_mapping(
                self.trust_eligibility,
                label="finalizer trust_eligibility",
            ),
        )
        object.__setattr__(
            self,
            "alias_chain",
            [
                _require_string(item, f"finalizer alias_chain[{index}]")
                for index, item in enumerate(_require_string_list(self.alias_chain, "finalizer alias_chain"))
            ],
        )
        if self.override is not None:
            object.__setattr__(
                self,
                "override",
                _require_override(
                    self.override,
                    capability_id=self.id,
                    label="finalizer override",
                ),
            )
        if self.support_decision is not None:
            support = (
                self.support_decision
                if isinstance(self.support_decision, SupportReport)
                else SupportReport.from_dict(
                    _require_mapping(
                        self.support_decision, "finalizer support_decision"
                    )
                )
            )
            if support.backend != self.id:
                raise ValueError("finalizer support_decision.backend must match finalizer id")
            object.__setattr__(self, "support_decision", support)

    def to_dict(self) -> dict[str, Any]:
        return _json_safe_mapping(
            {
                "id": self.id,
                "source_pack": self.source_pack,
                "manifest_digest": self.manifest_digest,
                "alias_chain": list(self.alias_chain),
                "override": self.override,
                "trust_eligibility": self.trust_eligibility,
                "support_decision": self.support_decision,
            }
        )

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> FinalizerResolution:
        data = _require_mapping(payload, "finalizer resolution")
        required = {
            "id",
            "source_pack",
            "manifest_digest",
            "alias_chain",
            "override",
            "trust_eligibility",
            "support_decision",
        }
        _validate_object_keys(data, required=required, allowed=required, label="finalizer resolution")
        return cls(
            id=data["id"],
            source_pack=data["source_pack"],
            manifest_digest=data["manifest_digest"],
            alias_chain=data["alias_chain"],
            override=data["override"],
            trust_eligibility=data["trust_eligibility"],
            support_decision=data["support_decision"],
        )


def _normalize_requested_policy(value: Any, label: str = "requested_policy") -> str | dict[str, Any]:
    if isinstance(value, str):
        return _require_string(value, label)
    return _json_safe_mapping(value, label=label)


@dataclass(frozen=True)
class LayerRef:
    """One z-layer: a renderer-owned contiguous visual-track range.

    ``z=0`` is the bottom layer and the per-layer tiling key; segments on
    distinct layers may overlap in time.  v1 ships src-over + alpha only:
    ``blend`` must be exactly ``"normal"`` and ``opacity`` must be in
    ``(0, 1]`` (the compositor applies it as ``aa=``).
    """

    z: int
    tracks: tuple[str, ...]
    blend: str = "normal"
    opacity: float = 1.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "z", _require_int(self.z, "layer z", minimum=0))
        if isinstance(self.tracks, (str, bytes)) or not isinstance(self.tracks, Sequence):
            raise TypeError("layer tracks must be an array of strings")
        tracks = tuple(
            _require_string(track, f"layer tracks[{index}]")
            for index, track in enumerate(self.tracks)
        )
        if not tracks:
            raise ValueError("layer tracks must contain at least one track id")
        object.__setattr__(self, "tracks", tracks)
        blend = _require_string(self.blend, "layer blend")
        if blend != "normal":
            raise ValueError(
                f"layer blend {blend!r} is not supported; v1 accepts only 'normal'"
            )
        object.__setattr__(self, "blend", blend)
        opacity = _require_number(self.opacity, "layer opacity", exclusive_minimum=0)
        if opacity > 1:
            raise ValueError("layer opacity must be <= 1")
        object.__setattr__(self, "opacity", opacity)

    def to_dict(self) -> dict[str, Any]:
        return _json_safe_mapping(
            {
                "z": self.z,
                "tracks": self.tracks,
                "blend": self.blend,
                "opacity": self.opacity,
            }
        )

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> LayerRef:
        data = _require_mapping(payload, "layer")
        _validate_object_keys(
            data,
            required={"z", "tracks"},
            allowed={"z", "tracks", "blend", "opacity"},
            label="layer",
        )
        return cls(
            z=data["z"],
            tracks=data["tracks"],
            blend=data.get("blend", "normal"),
            opacity=data.get("opacity", 1.0),
        )


@dataclass(frozen=True)
class RenderSegment:
    """One complete temporal window assigned to one qualified backend.

    ``layer`` optionally pins the segment to a z-layer; segments on distinct
    layers may overlap in time (stacking), while segments on the same layer
    (or the default layer, ``None``) must tile exactly.
    """

    window: FrameWindow
    renderer: RendererResolution
    input_hashes: dict[str, str] = field(default_factory=dict)
    layer: LayerRef | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "window", _coerce_window(self.window, "segment window", nullable=False))
        renderer = (
            self.renderer
            if isinstance(self.renderer, RendererResolution)
            else RendererResolution.from_dict(_require_mapping(self.renderer, "segment renderer"))
        )
        object.__setattr__(self, "renderer", renderer)
        object.__setattr__(
            self,
            "input_hashes",
            _require_hash_mapping(self.input_hashes, "segment input_hashes"),
        )
        if self.layer is not None:
            layer = (
                self.layer
                if isinstance(self.layer, LayerRef)
                else LayerRef.from_dict(_require_mapping(self.layer, "segment layer"))
            )
            object.__setattr__(self, "layer", layer)

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "window": self.window,
            "renderer": self.renderer,
            "input_hashes": self.input_hashes,
        }
        if self.layer is not None:
            payload["layer"] = self.layer
        return _json_safe_mapping(payload)

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> RenderSegment:
        data = _require_mapping(payload, "render segment")
        required = {"window", "renderer", "input_hashes"}
        allowed = {"window", "renderer", "input_hashes", "layer"}
        _validate_object_keys(data, required=required, allowed=allowed, label="render segment")
        return cls(
            window=FrameWindow.from_dict(data["window"]),
            renderer=RendererResolution.from_dict(data["renderer"]),
            input_hashes=data["input_hashes"],
            layer=LayerRef.from_dict(data["layer"]) if data.get("layer") is not None else None,
        )


@dataclass(frozen=True)
class RenderPlan:
    """A deterministic temporal plan plus its explicit finalizer."""

    schema_version: int
    request_digest: str
    requested_policy: str | dict[str, Any]
    planner: PlannerResolution
    segments: list[RenderSegment]
    finalizer: FinalizerResolution
    profile: RenderProfile
    total_frames: int
    reasons: dict[str, str]
    window: FrameWindow | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "schema_version",
            _require_schema_version(self.schema_version, "render plan"),
        )
        object.__setattr__(
            self,
            "request_digest",
            _require_sha256(self.request_digest, "request_digest"),
        )
        object.__setattr__(
            self,
            "requested_policy",
            _normalize_requested_policy(self.requested_policy),
        )
        planner = (
            self.planner
            if isinstance(self.planner, PlannerResolution)
            else PlannerResolution.from_dict(_require_mapping(self.planner, "planner"))
        )
        object.__setattr__(self, "planner", planner)
        if isinstance(self.segments, (str, bytes)) or not isinstance(self.segments, Sequence):
            raise TypeError("segments must be an array")
        segments = [
            item
            if isinstance(item, RenderSegment)
            else RenderSegment.from_dict(_require_mapping(item, f"segments[{index}]"))
            for index, item in enumerate(self.segments)
        ]
        object.__setattr__(self, "segments", segments)
        finalizer = (
            self.finalizer
            if isinstance(self.finalizer, FinalizerResolution)
            else FinalizerResolution.from_dict(_require_mapping(self.finalizer, "finalizer"))
        )
        object.__setattr__(self, "finalizer", finalizer)
        profile = _coerce_profile(self.profile, "plan profile", nullable=False)
        object.__setattr__(self, "profile", profile)
        total_frames = _require_int(self.total_frames, "total_frames", minimum=0)
        object.__setattr__(self, "total_frames", total_frames)
        window = _coerce_window(self.window, "plan window", nullable=True)
        object.__setattr__(self, "window", window)
        if window is not None:
            if window.fps_rational != profile.fps_rational:
                raise ValueError("plan window FPS must exactly match the canonical profile FPS")
            if window.end_frame > total_frames:
                raise ValueError("plan window must not extend beyond total_frames")
        if total_frames == 0:
            if window is not None or segments:
                raise ValueError("a zero-frame plan must have no window or segments")
        else:
            if not segments:
                raise ValueError("a positive-frame plan must contain at least one segment")
            target_start = window.start_frame if window is not None else 0
            target_end = window.end_frame if window is not None else total_frames
            explicit_layer = segments[0].layer is not None
            for index, segment in enumerate(segments):
                if (segment.layer is not None) != explicit_layer:
                    raise ValueError(
                        "segments must either all carry an explicit layer or all use the "
                        f"default layer: segments[0] has layer "
                        f"{'set' if explicit_layer else 'None'} but segments[{index}] has "
                        f"layer {'set' if segment.layer is not None else 'None'}"
                    )
            cursors: dict[int | None, int] = {}  # layer.z -> expected_start, None -> default layer
            for index, segment in enumerate(segments):
                if segment.window.fps_rational != profile.fps_rational:
                    raise ValueError(
                        f"segments[{index}] FPS must exactly match the canonical profile FPS"
                    )
                layer_key = segment.layer.z if segment.layer is not None else None
                expected_start = cursors.setdefault(layer_key, target_start)
                actual_start = segment.window.start_frame
                if actual_start != expected_start:
                    relation = "overlaps or is out of order" if actual_start < expected_start else "leaves a gap"
                    if segment.layer is None:
                        raise ValueError(f"segments[{index}] {relation} at frame {expected_start}")
                    raise ValueError(
                        f"segments[{index}] layer z={segment.layer.z} {relation} at frame {expected_start}"
                    )
                if segment.window.end_frame > target_end:
                    raise ValueError(f"segments[{index}] extends beyond the plan target window")
                cursors[layer_key] = segment.window.end_frame
            for layer_key, expected_start in cursors.items():
                # The default (layer=None) layer must cover the whole target
                # window — today's exact behavior, unchanged.  Explicit z
                # layers tile CONTIGUOUSLY (enforced by the cursor loop above)
                # but may legitimately end early: a top overlay (e.g. text)
                # only covers part of the timeline, and the compositor's
                # background fill handles the rest.
                if layer_key is None and expected_start != target_end:
                    raise ValueError("plan segments leave a trailing gap")
        reasons = _require_string_mapping(self.reasons, "reasons")
        expected_reason_keys = {str(index) for index in range(len(segments))}
        if set(reasons) != expected_reason_keys:
            raise ValueError(
                "plan reasons must contain exactly one entry per segment, keyed by zero-based index"
            )
        object.__setattr__(self, "reasons", reasons)

    def to_dict(self) -> dict[str, Any]:
        return _json_safe_mapping(
            {
                "schema_version": self.schema_version,
                "request_digest": self.request_digest,
                "requested_policy": self.requested_policy,
                "planner": self.planner,
                "segments": self.segments,
                "finalizer": self.finalizer,
                "profile": self.profile,
                "total_frames": self.total_frames,
                "reasons": self.reasons,
                "window": self.window,
            }
        )

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> RenderPlan:
        try:
            data = _require_mapping(payload, "render plan")
            required = {
                "schema_version",
                "request_digest",
                "requested_policy",
                "planner",
                "segments",
                "finalizer",
                "profile",
                "total_frames",
                "reasons",
                "window",
            }
            _validate_object_keys(data, required=required, allowed=required, label="render plan")
            raw_segments = data["segments"]
            if isinstance(raw_segments, (str, bytes)) or not isinstance(raw_segments, Sequence):
                raise TypeError("segments must be an array")
            return cls(
                schema_version=data["schema_version"],
                request_digest=data["request_digest"],
                requested_policy=data["requested_policy"],
                planner=PlannerResolution.from_dict(data["planner"]),
                segments=[RenderSegment.from_dict(item) for item in raw_segments],
                finalizer=FinalizerResolution.from_dict(data["finalizer"]),
                profile=RenderProfile.from_dict(data["profile"]),
                total_frames=data["total_frames"],
                reasons=data["reasons"],
                window=FrameWindow.from_dict(data["window"]) if data["window"] is not None else None,
            )
        except Exception as exc:
            from .errors import RendererException

            if isinstance(exc, RendererException):
                raise
            _protocol_failure(
                f"malformed render plan: {exc}",
                details={"error_type": type(exc).__name__},
            )


def _validate_backend_fragments(value: Any) -> dict[str, dict[str, Any]]:
    mapping = _require_mapping(value, "backend_fragments")
    fragments: dict[str, dict[str, Any]] = {}
    for raw_namespace, raw_fragment in mapping.items():
        namespace = _require_qualified_id(raw_namespace, "backend fragment namespace")
        fragment = _json_safe_mapping(raw_fragment, label=f"backend_fragments[{namespace!r}]")
        conflicts = sorted(set(fragment) & RESERVED_BACKEND_FRAGMENT_KEYS)
        if conflicts:
            raise ValueError(
                f"backend fragment {namespace!r} attempts to overwrite core-owned keys: "
                f"{', '.join(conflicts)}"
            )
        fragments[namespace] = fragment
    return fragments


@dataclass(frozen=True)
class RenderResult:
    """Successful renderer/finalizer result written to the authoritative path."""

    schema_version: int
    video: VideoArtifact
    audio_ownership: AudioOwnership
    backend_fragments: dict[str, dict[str, Any]] = field(default_factory=dict)
    normalization: list[str] = field(default_factory=list)
    logs: list[str] = field(default_factory=list)
    metadata: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        version = _require_schema_version(self.schema_version, "render result")
        video = (
            self.video
            if isinstance(self.video, VideoArtifact)
            else VideoArtifact.from_dict(_require_mapping(self.video, "video"))
        )
        ownership = _coerce_audio_ownership(
            self.audio_ownership,
            "audio_ownership",
            nullable=False,
        )
        if video.audio is None or video.audio != ownership:
            raise ValueError("video.audio must be present and match result audio_ownership")
        object.__setattr__(self, "schema_version", version)
        object.__setattr__(self, "video", video)
        object.__setattr__(self, "backend_fragments", _validate_backend_fragments(self.backend_fragments))
        object.__setattr__(self, "audio_ownership", ownership)
        object.__setattr__(
            self,
            "normalization",
            _require_string_list(self.normalization, "normalization"),
        )
        object.__setattr__(self, "logs", _require_string_list(self.logs, "logs"))
        object.__setattr__(self, "metadata", _require_string_mapping(self.metadata, "metadata"))

    @property
    def attachments(self) -> dict[str, Attachment]:
        """The sole authoritative attachment map, owned by the primary video."""

        return self.video.attachments

    def to_dict(self) -> dict[str, Any]:
        return _json_safe_mapping(
            {
                "schema_version": self.schema_version,
                "video": self.video,
                "backend_fragments": self.backend_fragments,
                "audio_ownership": self.audio_ownership,
                "normalization": self.normalization,
                "logs": self.logs,
                "metadata": self.metadata,
            }
        )

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> RenderResult:
        try:
            data = _require_mapping(payload, "render result")
            allowed = set(RENDER_RESULT_CORE_KEYS)
            _validate_object_keys(
                data,
                required={"schema_version", "video", "audio_ownership"},
                allowed=allowed,
                label="render result",
            )
            version = _require_schema_version(data["schema_version"], "render result")
            return cls(
                schema_version=version,
                video=VideoArtifact.from_dict(data["video"]),
                audio_ownership=data["audio_ownership"],
                backend_fragments=data.get("backend_fragments", {}),
                normalization=data.get("normalization", []),
                logs=data.get("logs", []),
                metadata=data.get("metadata", {}),
            )
        except Exception as exc:
            from .errors import RendererException

            if isinstance(exc, RendererException):
                raise
            _protocol_failure(
                f"malformed render result: {exc}",
                details={"error_type": type(exc).__name__},
            )


@dataclass(frozen=True)
class RendererError:
    """Language-neutral structured renderer failure payload."""

    schema_version: int
    kind: RendererErrorKind
    backend: str
    message: str
    recovery_command: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    KINDS: ClassVar[frozenset[str]] = frozenset(
        {
            "protocol",
            "unsupported",
            "binary_missing",
            "timeout",
            "interrupted",
            "invalid_artifact",
            "internal",
        }
    )

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "schema_version",
            _require_schema_version(self.schema_version, "renderer error"),
        )
        kind = _require_string(self.kind, "renderer error kind")
        if kind not in self.KINDS:
            raise ValueError(f"unknown renderer error kind: {kind}")
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "backend", _require_qualified_id(self.backend, "error backend"))
        object.__setattr__(self, "message", _require_string(self.message, "error message"))
        object.__setattr__(
            self,
            "recovery_command",
            _require_optional_string(self.recovery_command, "recovery_command"),
        )
        object.__setattr__(self, "details", _json_safe_mapping(self.details, label="error details"))

    def to_dict(self) -> dict[str, Any]:
        return _json_safe_mapping(
            {
                "schema_version": self.schema_version,
                "kind": self.kind,
                "backend": self.backend,
                "message": self.message,
                "recovery_command": self.recovery_command,
                "details": self.details,
            }
        )

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> RendererError:
        try:
            data = _require_mapping(payload, "renderer error")
            required = {
                "schema_version",
                "kind",
                "backend",
                "message",
                "recovery_command",
                "details",
            }
            _validate_object_keys(data, required=required, allowed=required, label="renderer error")
            return cls(
                schema_version=data["schema_version"],
                kind=data["kind"],
                backend=data["backend"],
                message=data["message"],
                recovery_command=data["recovery_command"],
                details=data["details"],
            )
        except Exception as exc:
            from .errors import RendererException

            if isinstance(exc, RendererException):
                raise
            _protocol_failure(
                f"malformed renderer error: {exc}",
                details={"error_type": type(exc).__name__},
            )


@dataclass(frozen=True)
class FinalizeRequest:
    """Wire request consumed by the ``finalize`` operation."""

    schema_version: int
    plan: RenderPlan
    artifacts: list[VideoArtifact]
    output_name: str
    backend_config: BackendConfig = field(default_factory=dict)
    metadata: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        version = _require_schema_version(self.schema_version, "finalize request")
        plan = (
            self.plan
            if isinstance(self.plan, RenderPlan)
            else RenderPlan.from_dict(_require_mapping(self.plan, "plan"))
        )
        if isinstance(self.artifacts, (str, bytes)) or not isinstance(self.artifacts, Sequence):
            raise TypeError("artifacts must be an array")
        artifacts = [
            artifact
            if isinstance(artifact, VideoArtifact)
            else VideoArtifact.from_dict(_require_mapping(artifact, f"artifacts[{index}]"))
            for index, artifact in enumerate(self.artifacts)
        ]
        if len(artifacts) != len(plan.segments):
            raise ValueError("finalize artifacts must correspond one-for-one with plan segments")
        if plan.total_frames == 0:
            raise ValueError("an empty render plan must not be finalized")
        attachment_names: set[str] = set()
        for index, artifact in enumerate(artifacts):
            duplicates = sorted(attachment_names & set(artifact.attachments))
            if duplicates:
                raise ValueError(
                    "duplicate attachment names across segment artifacts at "
                    f"artifacts[{index}]: {', '.join(duplicates)}"
                )
            attachment_names.update(artifact.attachments)
        output_name = _require_string(self.output_name, "output_name")
        if not _OUTPUT_NAME_RE.fullmatch(output_name) or output_name in {".", ".."}:
            raise ValueError("output_name must be a portable basename without path separators")
        object.__setattr__(self, "schema_version", version)
        object.__setattr__(self, "plan", plan)
        object.__setattr__(self, "artifacts", artifacts)
        object.__setattr__(self, "output_name", output_name)
        backend_config = _coerce_namespaced_backend_config(
            self.backend_config,
            "backend_config",
        )
        unexpected_config = sorted(set(backend_config) - {plan.finalizer.id})
        if unexpected_config:
            raise ValueError(
                "finalize backend_config may contain only the selected finalizer namespace "
                f"{plan.finalizer.id!r}"
            )
        object.__setattr__(self, "backend_config", backend_config)
        object.__setattr__(self, "metadata", _require_string_mapping(self.metadata, "metadata"))

    @property
    def expected_attachments(self) -> dict[str, Attachment]:
        """Return the globally unique attachments a finalizer must preserve."""

        return {
            name: attachment
            for artifact in self.artifacts
            for name, attachment in artifact.attachments.items()
        }

    def validate_final_result(
        self,
        result: RenderResult | Mapping[str, Any],
    ) -> RenderResult:
        """Validate attachment preservation on a standalone finalizer response.

        Finalizers may add new attachments, but every input attachment must be
        present under the same name with the exact same descriptor and digest.
        """

        final_result = (
            result
            if isinstance(result, RenderResult)
            else RenderResult.from_dict(_require_mapping(result, "final result"))
        )
        missing = sorted(set(self.expected_attachments) - set(final_result.attachments))
        if missing:
            raise ValueError("finalizer dropped attachments: " + ", ".join(missing))
        changed = sorted(
            name
            for name, expected in self.expected_attachments.items()
            if final_result.attachments[name] != expected
        )
        if changed:
            raise ValueError("finalizer changed attachments: " + ", ".join(changed))
        return final_result

    def to_dict(self) -> dict[str, Any]:
        return _json_safe_mapping(
            {
                "schema_version": self.schema_version,
                "plan": self.plan,
                "artifacts": self.artifacts,
                "output_name": self.output_name,
                "backend_config": self.backend_config,
                "metadata": self.metadata,
            }
        )

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> FinalizeRequest:
        try:
            data = _require_mapping(payload, "finalize request")
            allowed = {
                "schema_version",
                "plan",
                "artifacts",
                "output_name",
                "backend_config",
                "metadata",
            }
            _validate_object_keys(
                data,
                required={"schema_version", "plan", "artifacts", "output_name"},
                allowed=allowed,
                label="finalize request",
            )
            version = _require_schema_version(data["schema_version"], "finalize request")
            return cls(
                schema_version=version,
                plan=RenderPlan.from_dict(data["plan"]),
                artifacts=[VideoArtifact.from_dict(item) for item in data["artifacts"]],
                output_name=data["output_name"],
                backend_config=data.get("backend_config", {}),
                metadata=data.get("metadata", {}),
            )
        except Exception as exc:
            from .errors import RendererException

            if isinstance(exc, RendererException):
                raise
            _protocol_failure(
                f"malformed finalize request: {exc}",
                details={"error_type": type(exc).__name__},
            )


_PERMISSIONS = frozenset(
    {"project_files", "network", "subprocess", "environment", "accelerator", "external_services"}
)


def _manifest_capability_object(
    value: Any,
    *,
    label: str,
    allowed: frozenset[str],
) -> dict[str, Any]:
    capabilities = _json_safe_mapping(value, label=label)
    unknown = sorted(set(capabilities) - allowed)
    if unknown:
        raise ValueError(f"{label} has unknown fields: {', '.join(unknown)}")
    return capabilities


def _manifest_string_array(value: Any, label: str) -> list[str]:
    items = _require_string_list(value, label)
    if len(items) != len(set(items)):
        raise ValueError(f"{label} must not contain duplicates")
    return items


def _manifest_features(value: Any, label: str) -> dict[str, bool | str]:
    raw = _require_mapping(value, label)
    result: dict[str, bool | str] = {}
    for raw_key, raw_value in raw.items():
        key = _require_string(raw_key, f"{label} key")
        if isinstance(raw_value, bool):
            result[key] = raw_value
        elif isinstance(raw_value, str):
            result[key] = _require_string(raw_value, f"{label}[{key!r}]")
        else:
            raise TypeError(f"{label}[{key!r}] must be a boolean or string")
    return result


def _manifest_boolean(value: Any, label: str) -> bool:
    if not isinstance(value, bool):
        raise TypeError(f"{label} must be a boolean")
    return value


@dataclass(frozen=True)
class _CommandManifest:
    schema_version: int
    id: str
    name: str
    version: str
    protocol_version: int
    command: tuple[str, ...]
    operations: tuple[str, ...]
    description: str | None = None
    capabilities: dict[str, Any] = field(default_factory=dict)
    required_permissions: tuple[str, ...] = ()
    required_binaries: tuple[str, ...] = ()
    timeout_seconds: int | None = None
    metadata: dict[str, str] = field(default_factory=dict)

    REQUIRED_OPERATION: ClassVar[str]
    ALLOWED_OPERATIONS: ClassVar[frozenset[str]]
    LABEL: ClassVar[str]

    @classmethod
    def _normalize_capabilities(cls, value: Any) -> dict[str, Any]:
        return _json_safe_mapping(value, label=f"{cls.LABEL} capabilities")

    def __post_init__(self) -> None:
        version = _require_int(self.schema_version, "schema_version")
        if version != SCHEMA_VERSION:
            _protocol_failure(
                f"unknown {self.LABEL} schema_version {version}; expected {SCHEMA_VERSION}",
                details={"received": version, "supported": [SCHEMA_VERSION]},
            )
        object.__setattr__(self, "schema_version", version)
        object.__setattr__(self, "id", _require_qualified_id(self.id, f"{self.LABEL} id"))
        object.__setattr__(self, "name", _require_string(self.name, f"{self.LABEL} name"))
        object.__setattr__(self, "version", _require_string(self.version, f"{self.LABEL} version"))
        protocol_version = _require_int(self.protocol_version, "protocol_version")
        if protocol_version != SCHEMA_VERSION:
            _protocol_failure(
                f"unsupported {self.LABEL} protocol_version {protocol_version}; "
                f"expected {SCHEMA_VERSION}",
                details={"received": protocol_version, "supported": [SCHEMA_VERSION]},
            )
        object.__setattr__(self, "protocol_version", protocol_version)
        command = tuple(_require_string_list(self.command, "command"))
        if not command:
            raise ValueError("command must contain at least one argument")
        object.__setattr__(self, "command", command)
        operations = tuple(_require_string_list(self.operations, "operations"))
        if self.REQUIRED_OPERATION not in operations:
            raise ValueError(f"{self.LABEL} operations must include {self.REQUIRED_OPERATION!r}")
        unknown_operations = sorted(set(operations) - self.ALLOWED_OPERATIONS)
        if unknown_operations:
            raise ValueError(
                f"{self.LABEL} has unsupported operations: {', '.join(unknown_operations)}"
            )
        if len(operations) != len(set(operations)):
            raise ValueError("operations must not contain duplicates")
        object.__setattr__(self, "operations", operations)
        object.__setattr__(
            self,
            "description",
            _require_optional_string(self.description, "description"),
        )
        object.__setattr__(
            self,
            "capabilities",
            self._normalize_capabilities(self.capabilities),
        )
        permissions = tuple(_require_string_list(self.required_permissions, "required_permissions"))
        unknown_permissions = sorted(set(permissions) - _PERMISSIONS)
        if unknown_permissions:
            raise ValueError(f"unknown required permissions: {', '.join(unknown_permissions)}")
        if len(permissions) != len(set(permissions)):
            raise ValueError("required_permissions must not contain duplicates")
        object.__setattr__(self, "required_permissions", permissions)
        binaries = tuple(_require_string_list(self.required_binaries, "required_binaries"))
        if len(binaries) != len(set(binaries)):
            raise ValueError("required_binaries must not contain duplicates")
        object.__setattr__(self, "required_binaries", binaries)
        if self.timeout_seconds is not None:
            object.__setattr__(
                self,
                "timeout_seconds",
                _require_int(self.timeout_seconds, "timeout_seconds", minimum=1),
            )
        object.__setattr__(self, "metadata", _require_string_mapping(self.metadata, "metadata"))

    def to_dict(self) -> dict[str, Any]:
        return _json_safe_mapping(
            {
                "schema_version": self.schema_version,
                "id": self.id,
                "name": self.name,
                "version": self.version,
                "protocol_version": self.protocol_version,
                "command": self.command,
                "operations": self.operations,
                "description": self.description,
                "capabilities": self.capabilities,
                "required_permissions": self.required_permissions,
                "required_binaries": self.required_binaries,
                "timeout_seconds": self.timeout_seconds,
                "metadata": self.metadata,
            }
        )

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> _CommandManifest:
        try:
            data = _require_mapping(payload, cls.LABEL)
            allowed = {
                "schema_version",
                "id",
                "name",
                "version",
                "protocol_version",
                "command",
                "operations",
                "description",
                "capabilities",
                "required_permissions",
                "required_binaries",
                "timeout_seconds",
                "metadata",
            }
            _validate_object_keys(
                data,
                required={
                    "schema_version",
                    "id",
                    "name",
                    "version",
                    "protocol_version",
                    "command",
                    "operations",
                },
                allowed=allowed,
                label=cls.LABEL,
            )
            return cls(
                schema_version=data["schema_version"],
                id=data["id"],
                name=data["name"],
                version=data["version"],
                protocol_version=data["protocol_version"],
                command=data["command"],
                operations=data["operations"],
                description=data.get("description"),
                capabilities=data.get("capabilities", {}),
                required_permissions=data.get("required_permissions", ()),
                required_binaries=data.get("required_binaries", ()),
                timeout_seconds=data.get("timeout_seconds"),
                metadata=data.get("metadata", {}),
            )
        except Exception as exc:
            from .errors import RendererException

            if isinstance(exc, RendererException):
                raise
            _protocol_failure(
                f"malformed {cls.LABEL}: {exc}",
                details={"error_type": type(exc).__name__},
            )


@dataclass(frozen=True)
class RendererManifest(_CommandManifest):
    REQUIRED_OPERATION: ClassVar[str] = "render"
    ALLOWED_OPERATIONS: ClassVar[frozenset[str]] = frozenset({"render", "support"})
    LABEL: ClassVar[str] = "renderer manifest"

    @classmethod
    def _normalize_capabilities(cls, value: Any) -> dict[str, Any]:
        capabilities = _manifest_capability_object(
            value,
            label="renderer capabilities",
            allowed=frozenset(
                {
                    "clip_types",
                    "track_types",
                    "features",
                    "supports_full_timeline",
                    "supports_windows",
                    "output_profiles",
                    "audio_ownership",
                }
            ),
        )
        result: dict[str, Any] = {}
        for key in ("clip_types", "track_types", "output_profiles"):
            if key in capabilities:
                result[key] = _manifest_string_array(capabilities[key], key)
        if "features" in capabilities:
            result["features"] = _manifest_features(capabilities["features"], "features")
        for key in ("supports_full_timeline", "supports_windows"):
            if key in capabilities:
                result[key] = _manifest_boolean(capabilities[key], key)
        if "audio_ownership" in capabilities:
            audio_modes = _manifest_string_array(
                capabilities["audio_ownership"],
                "audio_ownership",
            )
            for index, mode in enumerate(audio_modes):
                _coerce_audio_ownership(mode, f"audio_ownership[{index}]", nullable=False)
            result["audio_ownership"] = audio_modes
        return result


@dataclass(frozen=True)
class PlannerManifest(_CommandManifest):
    REQUIRED_OPERATION: ClassVar[str] = "plan"
    ALLOWED_OPERATIONS: ClassVar[frozenset[str]] = frozenset({"plan", "support"})
    LABEL: ClassVar[str] = "planner manifest"

    @classmethod
    def _normalize_capabilities(cls, value: Any) -> dict[str, Any]:
        capabilities = _manifest_capability_object(
            value,
            label="planner capabilities",
            allowed=frozenset({"policies", "supports_fallback", "features"}),
        )
        result: dict[str, Any] = {}
        if "policies" in capabilities:
            result["policies"] = _manifest_string_array(capabilities["policies"], "policies")
        if "supports_fallback" in capabilities:
            result["supports_fallback"] = _manifest_boolean(
                capabilities["supports_fallback"],
                "supports_fallback",
            )
        if "features" in capabilities:
            result["features"] = _manifest_features(capabilities["features"], "features")
        return result


@dataclass(frozen=True)
class FinalizerManifest(_CommandManifest):
    REQUIRED_OPERATION: ClassVar[str] = "finalize"
    ALLOWED_OPERATIONS: ClassVar[frozenset[str]] = frozenset({"finalize", "support"})
    LABEL: ClassVar[str] = "finalizer manifest"

    @classmethod
    def _normalize_capabilities(cls, value: Any) -> dict[str, Any]:
        capabilities = _manifest_capability_object(
            value,
            label="finalizer capabilities",
            allowed=frozenset(
                {"containers", "preserves_attachments", "audio_ownership", "features"}
            ),
        )
        result: dict[str, Any] = {}
        if "containers" in capabilities:
            result["containers"] = _manifest_string_array(capabilities["containers"], "containers")
        if "preserves_attachments" in capabilities:
            result["preserves_attachments"] = _manifest_boolean(
                capabilities["preserves_attachments"],
                "preserves_attachments",
            )
        if "audio_ownership" in capabilities:
            audio_modes = _manifest_string_array(
                capabilities["audio_ownership"],
                "audio_ownership",
            )
            for index, mode in enumerate(audio_modes):
                _coerce_audio_ownership(mode, f"audio_ownership[{index}]", nullable=False)
            result["audio_ownership"] = audio_modes
        if "features" in capabilities:
            result["features"] = _manifest_features(capabilities["features"], "features")
        return result


def parse_wire_result(payload: Mapping[str, Any]) -> RenderResult | RendererError:
    """Parse the authoritative result file as success or structured failure."""

    try:
        data = _require_mapping(payload, "wire result")
        if "kind" in data or "backend" in data or "message" in data:
            return RendererError.from_dict(data)
        return RenderResult.from_dict(data)
    except Exception as exc:
        from .errors import RendererException

        if isinstance(exc, RendererException):
            raise
        _protocol_failure(
            f"malformed renderer result: {exc}",
            details={"error_type": type(exc).__name__},
        )


__all__ = [
    "Attachment",
    "AudioOwnership",
    "BackendConfig",
    "compute_request_digest",
    "FinalizeRequest",
    "FinalizerResolution",
    "FinalizerManifest",
    "FrameWindow",
    "PlannerManifest",
    "PlannerResolution",
    "RenderPlan",
    "RenderProfile",
    "RenderRequest",
    "RenderResult",
    "RenderSegment",
    "RendererError",
    "RendererManifest",
    "RendererResolution",
    "SupportReport",
    "VideoArtifact",
    "parse_wire_result",
]
