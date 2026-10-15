"""SDK adapter for the shared value-based envelope producer."""

from astrid.core.timeline.audio_envelope import compile_audio_values
from astrid.sdk import timeline_intent as intent


def compile_clip_audio(clip) -> dict | None:
    return compile_audio_values(
        clip.data,
        fps=clip._tl.fps,
        words=list(clip._tl.words()),
        clip_at=clip.start,
        duck_rule=intent._get(clip.data, "duck"),
    )
