"""Astrid entry adapter for VibeComfy's pinned whole-media transcription template."""
from __future__ import annotations


def build():
    """Load the canonical workflow from the recipe-pinned VibeComfy checkout."""
    from vibecomfy.registry.ready import workflow_from_ready

    return workflow_from_ready("audio/transcribe")
