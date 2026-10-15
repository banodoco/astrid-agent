"""Frozen public DTO surface for the rendering wire contract."""

from .contracts import (
    Attachment,
    AudioOwnership,
    BackendConfig,
    CompiledAudio,
    FrameWindow,
    RendererError,
    RenderPlan,
    RenderProfile,
    RenderRequest,
    RenderResult,
    SupportReport,
    VideoArtifact,
)

__all__ = [
    "RenderRequest",
    "CompiledAudio",
    "SupportReport",
    "RenderPlan",
    "FrameWindow",
    "RenderProfile",
    "AudioOwnership",
    "VideoArtifact",
    "RenderResult",
    "RendererError",
    "BackendConfig",
    "Attachment",
]
