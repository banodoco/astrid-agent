"""SDK adapter for the shared value-based envelope producer."""

from astrid.core.timeline.audio_envelope import compile_audio_values, validate_authored_audio
from astrid.sdk import timeline_intent as intent


def validate_bundle_audio(bundle) -> None:
    """Validate authored rows only, leaving opaque metadata and base images alone."""
    owners = [bundle.get("parent", {})]
    owners.extend(shot.get("internal_timeline", {}) for shot in bundle.get("shots", {}).values())
    for owner in owners:
        # Parent payloads may expose config.clips as well as the authoring
        # adapter's direct rows. Neither representation may bypass validation.
        rows = [*owner.get("clips", []), *owner.get("config", {}).get("clips", [])]
        for clip in rows:
            if "audio" in clip:
                validate_authored_audio(clip["audio"])


def compile_clip_audio(clip) -> dict | None:
    return compile_audio_values(
        clip.data,
        fps=clip._tl.fps,
        words=list(clip._tl.words()),
        clip_at=clip.start,
        duck_rule=intent._get(clip.data, "duck"),
    )
