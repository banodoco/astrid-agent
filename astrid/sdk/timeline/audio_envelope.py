"""Project existing authored duck tags and resolved words into runtime audio.

The caller attaches this result to a compiled clip, never the authored checkout.
No bed authoring or arrangement clock is introduced here.
"""
from __future__ import annotations

import re

from astrid.core.timeline.audio_envelope import AudioEnvelopeError, compile_duck, compile_legacy
from astrid.sdk import timeline_intent as intent


def compile_clip_audio(clip) -> dict | None:
    """Consume a current SDK Clip; re-run after any timing compilation/reflow."""
    fps = clip._tl.fps
    envelope = compile_legacy(clip.data, fps)
    rule = intent._get(clip.data, "duck")
    if rule is not None:
        if not isinstance(rule, dict) or rule.get("under") != "voice":
            raise AudioEnvelopeError("duck requires the existing voice narration selector", duck=True)
        def seconds(value):
            match = re.fullmatch(r"(\d+(?:\.\d+)?)([fs])", str(value))
            if not match:
                raise AudioEnvelopeError("duck attack/release require frame or second durations", duck=True)
            return float(match[1]) / (fps if match[2] == "f" else 1)
        by = re.fullmatch(r"(-\d+(?:\.\d+)?)dB", str(rule.get("by")))
        if not by:
            raise AudioEnvelopeError("duck by requires negative dB", duck=True)
        duck = compile_duck(list(clip._tl.words()), by_db=float(by[1]),
                            attack=seconds(rule.get("attack")), release=seconds(rule.get("release")),
                            clip_at=clip.start)
        if envelope is None:
            envelope = duck
        else:
            envelope.setdefault("multiply", []).append(duck)
            envelope["duck"] = True
    return {"envelope": envelope} if envelope is not None else None
