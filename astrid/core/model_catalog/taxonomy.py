"""Generation taxonomy registry and built-in compatibility constants.

The generation stack treats features, modes, and backend ids as
runtime-validated string taxonomies rather than closed ``Literal`` types.
Built-in tuples stay available for compatibility, while a registry can add
pack-declared ids discovered from ``pack.extensions.generation`` metadata.

These definitions live in ``model_catalog`` (below ``generation``) because the
model registry validates against them. The pack-scanning factory functions that
build a populated registry stay in ``astrid.core.generation.features`` and
import these definitions from here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

Feature = str

IMAGE_MODALITY = "image"
VIDEO_MODALITY = "video"
AUDIO_MODALITY = "audio"

LOCAL_BACKEND_ID = "local"
CLOUD_BACKEND_ID = "cloud"
CODEX_BACKEND_ID = "codex"
WAVESPEED_BACKEND_ID = "wavespeed"

IMAGE_FEATURES: tuple[Feature, ...] = (
    "prompt",
    "negative_prompt",
    "seed",
    "count",
    "size",
    "image_ref",
    "mask_ref",
    "strength",
    "guidance_scale",
    "steps",
    "upscale_mode",
    "upscale_factor",
    "target_resolution",
    "noise_scale",
)

VIDEO_FEATURES: tuple[Feature, ...] = (
    "prompt",
    "negative_prompt",
    "seed",
    "count",
    "size",
    "image_ref",
    "image_end_ref",
    "video_ref",
    "strength",
    "guidance_scale",
    "steps",
    "frames",
    "fps",
    "duration",
    "resolution",
    "loras",
    "shift",
    "enable_safety_checker",
    "enable_prompt_expansion",
    "acceleration",
    "driving_type",
    "subject_type",
    # Upscale-mode features (shared image/video registry key).
    "upscale_mode",
    "upscale_factor",
    "target_resolution",
    "noise_scale",
)

AUDIO_FEATURES: tuple[Feature, ...] = (
    "prompt",
    "negative_prompt",
    "seed",
    "count",
    "duration",
    "guidance_scale",
    "steps",
    "lyrics_prompt",
    "instrumental",
    "output_format",
)

CANONICAL_IMAGE_MODES: tuple[str, ...] = (
    "t2i",
    "i2i",
    "edit",
    "inpaint",
    "outpaint",
    "upscale",
)

CANONICAL_VIDEO_MODES: tuple[str, ...] = (
    "t2v",
    "i2v",
    "flf",
    "v2v",
    "video-edit",
)

CANONICAL_AUDIO_MODES: tuple[str, ...] = (
    "tts",
    "music",
    "sfx",
)

BUILTIN_GENERATION_BACKEND_IDS: tuple[str, ...] = (
    CLOUD_BACKEND_ID,
    CODEX_BACKEND_ID,
    LOCAL_BACKEND_ID,
    WAVESPEED_BACKEND_ID,
)


@dataclass(frozen=True)
class GenerationFeatureDescriptor:
    id: str
    label: str = ""
    description: str = ""


@dataclass(frozen=True)
class GenerationModeDescriptor:
    id: str
    modalities: tuple[str, ...] = ()
    label: str = ""
    description: str = ""


@dataclass(frozen=True)
class GenerationBackendIdDescriptor:
    id: str
    label: str = ""
    description: str = ""


class GenerationTaxonomyRegistry:
    """Runtime registry for known generation features, modes, and backends."""

    def __init__(
        self,
        *,
        feature_descriptors: Iterable[GenerationFeatureDescriptor] = (),
        mode_descriptors: Iterable[GenerationModeDescriptor] = (),
        backend_descriptors: Iterable[GenerationBackendIdDescriptor] = (),
        include_audio_features: bool = True,
    ) -> None:
        self._feature_descriptors: dict[str, GenerationFeatureDescriptor] = {}
        self._mode_descriptors: dict[str, GenerationModeDescriptor] = {}
        self._backend_descriptors: dict[str, GenerationBackendIdDescriptor] = {}
        self.register_features(_builtin_feature_descriptors(include_audio=include_audio_features))
        self.register_modes(_builtin_mode_descriptors())
        self.register_backends(_builtin_backend_descriptors())
        self.register_features(feature_descriptors)
        self.register_modes(mode_descriptors)
        self.register_backends(backend_descriptors)

    def register_feature(self, descriptor: GenerationFeatureDescriptor) -> None:
        feature_id = self._require_token(descriptor.id, field_name="generation feature")
        if feature_id in self._feature_descriptors:
            raise ValueError(f"duplicate generation feature {feature_id!r}")
        self._feature_descriptors[feature_id] = GenerationFeatureDescriptor(
            id=feature_id,
            label=descriptor.label,
            description=descriptor.description,
        )

    def register_features(self, descriptors: Iterable[GenerationFeatureDescriptor]) -> None:
        for descriptor in descriptors:
            self.register_feature(descriptor)

    def register_mode(self, descriptor: GenerationModeDescriptor) -> None:
        mode_id = self._require_token(descriptor.id, field_name="generation mode")
        modalities = tuple(
            self._require_modality(modality)
            for modality in (descriptor.modalities or ())
        )
        if mode_id in self._mode_descriptors:
            raise ValueError(f"duplicate generation mode {mode_id!r}")
        self._mode_descriptors[mode_id] = GenerationModeDescriptor(
            id=mode_id,
            modalities=modalities,
            label=descriptor.label,
            description=descriptor.description,
        )

    def register_modes(self, descriptors: Iterable[GenerationModeDescriptor]) -> None:
        for descriptor in descriptors:
            self.register_mode(descriptor)

    def register_backend(self, descriptor: GenerationBackendIdDescriptor) -> None:
        backend_id = self._require_token(descriptor.id, field_name="generation backend id")
        if backend_id in self._backend_descriptors:
            raise ValueError(f"duplicate generation backend id {backend_id!r}")
        self._backend_descriptors[backend_id] = GenerationBackendIdDescriptor(
            id=backend_id,
            label=descriptor.label,
            description=descriptor.description,
        )

    def register_backends(self, descriptors: Iterable[GenerationBackendIdDescriptor]) -> None:
        for descriptor in descriptors:
            self.register_backend(descriptor)

    def feature_ids(self) -> tuple[str, ...]:
        return tuple(self._feature_descriptors)

    def feature_descriptors(self) -> tuple[GenerationFeatureDescriptor, ...]:
        return tuple(self._feature_descriptors.values())

    def backend_ids(self) -> tuple[str, ...]:
        return tuple(self._backend_descriptors)

    def backend_descriptors(self) -> tuple[GenerationBackendIdDescriptor, ...]:
        return tuple(self._backend_descriptors.values())

    def mode_ids(self, modality: str) -> tuple[str, ...]:
        required_modality = self._require_modality(modality)
        mode_ids: list[str] = []
        for descriptor in self._mode_descriptors.values():
            if descriptor.modalities and required_modality not in descriptor.modalities:
                continue
            if descriptor.id not in mode_ids:
                mode_ids.append(descriptor.id)
        return tuple(mode_ids)

    def mode_descriptors(self) -> tuple[GenerationModeDescriptor, ...]:
        return tuple(self._mode_descriptors.values())

    def require_feature(self, value: object, *, path: str) -> Feature:
        feature = self._require_token(value, field_name=path)
        if feature not in self._feature_descriptors:
            raise ValueError(
                f"{path}: {feature!r} is not a recognised Feature; "
                f"allowed: {sorted(self._feature_descriptors)}"
            )
        return feature

    def require_mode(self, modality: str, value: object, *, path: str) -> str:
        mode = self._require_token(value, field_name=path)
        allowed = self.mode_ids(modality)
        if mode not in allowed:
            raise ValueError(
                f"{path}: unknown {modality} mode {mode!r}; "
                f"canonical {modality} modes are: {', '.join(allowed)}"
            )
        return mode

    @staticmethod
    def _require_token(value: object, *, field_name: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{field_name}: must be a non-empty string")
        return value.strip()

    @staticmethod
    def _require_modality(value: str) -> str:
        modality = GenerationTaxonomyRegistry._require_token(
            value,
            field_name="generation modality",
        )
        if modality not in {IMAGE_MODALITY, VIDEO_MODALITY, AUDIO_MODALITY}:
            raise ValueError(
                f"generation modality must be one of "
                f"[{IMAGE_MODALITY}, {VIDEO_MODALITY}, {AUDIO_MODALITY}]"
            )
        return modality


def _builtin_feature_descriptors(*, include_audio: bool = True) -> tuple[GenerationFeatureDescriptor, ...]:
    feature_ids = list(dict.fromkeys((*IMAGE_FEATURES, *VIDEO_FEATURES)))
    if include_audio:
        for feature_id in AUDIO_FEATURES:
            if feature_id not in feature_ids:
                feature_ids.append(feature_id)
    return tuple(GenerationFeatureDescriptor(id=feature_id) for feature_id in feature_ids)


def _builtin_mode_descriptors() -> tuple[GenerationModeDescriptor, ...]:
    return tuple(
        [
            GenerationModeDescriptor(id=mode_id, modalities=(IMAGE_MODALITY,))
            for mode_id in CANONICAL_IMAGE_MODES
        ]
        + [
            GenerationModeDescriptor(id=mode_id, modalities=(VIDEO_MODALITY,))
            for mode_id in CANONICAL_VIDEO_MODES
        ]
        + [
            GenerationModeDescriptor(id=mode_id, modalities=(AUDIO_MODALITY,))
            for mode_id in CANONICAL_AUDIO_MODES
        ]
    )


def _builtin_backend_descriptors() -> tuple[GenerationBackendIdDescriptor, ...]:
    return tuple(
        GenerationBackendIdDescriptor(id=backend_id)
        for backend_id in BUILTIN_GENERATION_BACKEND_IDS
    )


GENERATION_TAXONOMY = GenerationTaxonomyRegistry(include_audio_features=False)
