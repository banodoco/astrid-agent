"""Stable pack-authoring API for VibeComfy.

External packs should import request compilers, portable request types, and
managed-asset helpers from this module. Runtime and worker implementation
details remain private to the VibeComfy pack.
"""

from __future__ import annotations

import importlib as _importlib

VERSION = (1, 0)

_EXPORTS = {
    "AssetManifestError": ("astrid.packs.vibecomfy.asset_manifest", "AssetManifestError"),
    "CANONICAL_CAPABILITIES": ("astrid.packs.vibecomfy.direct_image.compiler", "CANONICAL_CAPABILITIES"),
    "CanonicalWorkflowBinding": ("astrid.packs.vibecomfy.direct_image.compiler", "CanonicalWorkflowBinding"),
    "CharacterAnimationRequest": ("astrid.packs.vibecomfy.media.compiler", "CharacterAnimationRequest"),
    "CompiledImageRequest": ("astrid.packs.vibecomfy.direct_image.compiler", "CompiledImageRequest"),
    "CompiledVibeMedia": ("astrid.packs.vibecomfy.media.compiler", "CompiledVibeMedia"),
    "ImageCompileError": ("astrid.packs.vibecomfy.direct_image.compiler", "ImageCompileError"),
    "ImageRequest": ("astrid.packs.vibecomfy.direct_image.compiler", "ImageRequest"),
    "MediaCompileError": ("astrid.packs.vibecomfy.media.compiler", "MediaCompileError"),
    "PROFILE_CONFIGS": ("astrid.packs.vibecomfy.direct_image.compiler", "PROFILE_CONFIGS"),
    "ProfileConfig": ("astrid.packs.vibecomfy.direct_image.compiler", "ProfileConfig"),
    "ResolvedAssetManifest": ("astrid.packs.vibecomfy.asset_manifest", "ResolvedAssetManifest"),
    "VibeProfileSemantics": ("astrid.packs.vibecomfy.media.compiler", "VibeProfileSemantics"),
    "VideoEnhanceRequest": ("astrid.packs.vibecomfy.media.compiler", "VideoEnhanceRequest"),
    "WanI2VRequest": ("astrid.packs.vibecomfy.media.compiler", "WanI2VRequest"),
    "WanT2IRequest": ("astrid.packs.vibecomfy.media.compiler", "WanT2IRequest"),
    "build_asset_manifest": ("astrid.packs.vibecomfy.asset_manifest", "build_asset_manifest"),
    "canonical_workflow_binding": ("astrid.packs.vibecomfy.direct_image.compiler", "canonical_workflow_binding"),
    "compile_character_animation": ("astrid.packs.vibecomfy.media.compiler", "compile_character_animation"),
    "compile_image_request": ("astrid.packs.vibecomfy.direct_image.compiler", "compile_image_request"),
    "compile_video_enhance": ("astrid.packs.vibecomfy.media.compiler", "compile_video_enhance"),
    "compile_wan_2_2_i2v": ("astrid.packs.vibecomfy.media.compiler", "compile_wan_2_2_i2v"),
    "compile_wan_2_2_t2i": ("astrid.packs.vibecomfy.media.compiler", "compile_wan_2_2_t2i"),
    "normalize_capability_id": ("astrid.packs.vibecomfy.direct_image.compiler", "normalize_capability_id"),
    "portable_execution_digest": ("astrid.packs.vibecomfy.direct_image.compiler", "portable_execution_digest"),
    "profile_semantics": ("astrid.packs.vibecomfy.media.compiler", "profile_semantics"),
    "read_archive": ("astrid.packs.vibecomfy.asset_manifest", "read_archive"),
    "read_archive_bytes": ("astrid.packs.vibecomfy.asset_manifest", "read_archive_bytes"),
    "resolve_inputs": ("astrid.packs.vibecomfy.asset_manifest", "resolve_inputs"),
}

__all__ = ["VERSION", *sorted(_EXPORTS)]


def __getattr__(name: str):
    try:
        module, attribute = _EXPORTS[name]
    except KeyError:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from None
    return getattr(_importlib.import_module(module), attribute)


def __dir__():
    return sorted(set(globals()) | set(__all__))
