"""Shared authored and compiled audio gain semantics.

Only deliberately authored envelopes persist. Derived narration/occurrence
projections, compiled_audio authority carriers and audio.render_source do not.

Times are clip-local timeline seconds, independent of source trim/playback rate.
Each point's curve controls the following segment. Descending segments use the
same curve backwards (equal-power pairs therefore preserve power). All leaves
are ordered keyframes; products compose fades, minima compose overlapping ducks.
"""
from __future__ import annotations

import copy
import math
import re
from collections.abc import Mapping, Sequence
from typing import Any

from . import audio_curves


class AudioEnvelopeError(ValueError):
    def __init__(self, message: str, *, duck: bool = False):
        self.code = "DUCK_UNRENDERED" if duck else "ENVELOPE_UNRENDERED"
        super().__init__(f"{self.code}: {message}")


def _number(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise AudioEnvelopeError("key times and gains must be finite numbers")
    return float(value)


def validate_authored_audio(audio: Any) -> None:
    """The strict document boundary; renderer-owned fields never enter storage."""
    if not isinstance(audio, Mapping) or set(audio) != {"envelope"}:
        raise AudioEnvelopeError("authored audio must contain only envelope")
    validate(audio["envelope"])


def validate(envelope: Mapping[str, Any], *, host: str = "ffmpeg") -> None:
    if not isinstance(envelope, Mapping):
        raise AudioEnvelopeError("invalid compiled envelope")
    if host not in {"ffmpeg", "composition-player"}:
        raise AudioEnvelopeError(f"unsupported sample consumer {host!r}", duck=bool(envelope.get("duck")))
    if not isinstance(envelope, Mapping) or set(envelope) - {"keyframes", "multiply", "minimum", "duck"}:
        raise AudioEnvelopeError("invalid compiled envelope")
    if "duck" in envelope and not isinstance(envelope["duck"], bool):
        raise AudioEnvelopeError("duck marker must be boolean")
    points = envelope.get("keyframes")
    if not isinstance(points, list) or not points:
        raise AudioEnvelopeError("envelope requires ordered keyframes")
    previous = -math.inf
    for point in points:
        if not isinstance(point, Mapping) or set(point) - {"at", "value", "curve"}:
            raise AudioEnvelopeError("invalid envelope keyframe")
        at, value = _number(point.get("at")), _number(point.get("value"))
        if at <= previous or value < 0:
            raise AudioEnvelopeError("key times must increase strictly and gains must be nonnegative")
        previous = at
        try:
            audio_curves.validate(point.get("curve", "linear"))
        except (ValueError, TypeError) as exc:
            raise AudioEnvelopeError(str(exc)) from exc
    for operation in ("multiply", "minimum"):
        children = envelope.get(operation, [])
        if not isinstance(children, list):
            raise AudioEnvelopeError(f"{operation} must be an array")
        for child in children:
            validate(child, host=host)


def keyframes(points: Sequence[tuple[float, float]], curve: str = "linear") -> dict:
    result = {"keyframes": [{"at": at, "value": value, "curve": curve} for at, value in points]}
    validate(result)
    return result


def evaluate(envelope: Mapping[str, Any], seconds: float) -> float:
    """Evaluate an already validated plan at a sample's timeline coordinate."""
    points = envelope["keyframes"]
    value = points[-1]["value"]
    if seconds <= points[0]["at"]:
        value = points[0]["value"]
    else:
        for left, right in zip(points, points[1:]):
            if seconds < right["at"]:
                u = (seconds - left["at"]) / (right["at"] - left["at"])
                a, b = left["value"], right["value"]
                curve = left.get("curve", "linear")
                value = (a + (b - a) * audio_curves.gain(curve, u) if b >= a
                         else b + (a - b) * audio_curves.gain(curve, u, fade_in=False))
                break
    for factor in envelope.get("multiply", []):
        value *= evaluate(factor, seconds)
    if envelope.get("minimum"):
        value *= min(evaluate(child, seconds) for child in envelope["minimum"])
    return value


def compile_legacy(clip: Mapping[str, Any], fps: float) -> dict | None:
    """Project the existing Player's rounded frame windows, exactly once.

    An existing authored or compiled audio.envelope takes precedence: legacy params remain
    readable but must never be multiplied again by either consumer.
    """
    if _number(fps) <= 0:
        raise AudioEnvelopeError("fps must be positive")
    audio = clip.get("audio", {})
    if not isinstance(audio, Mapping):
        raise AudioEnvelopeError("compiled audio must be an object")
    if "envelope" in audio:
        validate(audio["envelope"])
        return copy.deepcopy(audio["envelope"])
    params = clip.get("params") or {}
    if not isinstance(params, Mapping):
        raise AudioEnvelopeError("audio params must be an object")
    factors = []
    speed = _number(clip.get("speed", 1))
    if speed <= 0:
        raise AudioEnvelopeError("playback rate must be positive")
    source_duration = (_number(clip["hold"]) if "hold" in clip else
                       _number(clip.get("to", 0)) - _number(clip.get("from", 0)))
    duration = max(1, math.floor(source_duration / speed * fps + 0.5)) / fps
    for name, incoming in (("fadeIn", True), ("fadeOut", False)):
        raw = params.get(name, 0)
        if not isinstance(raw, (float, int)) or isinstance(raw, bool) or not math.isfinite(raw) or raw <= 0:
            continue  # legacy Player treats invalid/nonpositive durations as absent
        length = math.floor(raw * fps + 0.5) / fps
        if length <= 0:
            continue
        curve = params.get(name + "Curve", "linear")
        factors.append(keyframes([(0, 0), (length, 1)] if incoming else
                                 [(duration - length, 1), (duration, 0)], curve))
    if not factors:
        return None
    return {"keyframes": [{"at": 0, "value": 1}], "multiply": factors}


def compile_duck(spans: Sequence[Any], *, by_db: float, attack: float, release: float,
                 clip_at: float = 0) -> dict:
    """Consume resolved narration word spans (objects exposing start/end).

    Recompile after timing/reflow; no word identity or second clock is stored.
    Attack reaches attenuation at speech start; release begins at speech end.
    """
    if not all(math.isfinite(v) for v in (by_db, attack, release, clip_at)) or by_db > 0 or min(attack, release) <= 0:
        raise AudioEnvelopeError("duck needs attenuation and positive attack/release", duck=True)
    gain = 10 ** (by_db / 20)
    ducks = []
    for span in spans:
        start, end = float(span.start) - clip_at, float(span.end) - clip_at
        if not math.isfinite(start + end) or end <= start:
            raise AudioEnvelopeError("invalid resolved narration span", duck=True)
        ducks.append(keyframes([(start - attack, 1), (start, gain), (end, gain), (end + release, 1)]))
    return {"keyframes": [{"at": 0, "value": 1}], "minimum": ducks, "duck": True}


def ffmpeg_expression(envelope: Mapping[str, Any], time: str = "t") -> str:
    """Generate one aeval expression; FFmpeg's volume filter is frame based."""
    validate(envelope)
    def n(value):
        return format(float(value), ".17g")
    def curve(name, u):
        spec = audio_curves.registry()[name]
        kind = spec["kind"]
        if kind == "linear":
            return u
        if kind == "equal-power":
            return f"sin(({u})*PI/2)"
        if kind == "exponential":
            e = n(spec["exponent"])
            return f"((exp(({u})*({e}))-1)/(exp({e})-1))"
        if kind == "s-curve":
            return f"(({u})*({u})*(3-2*({u})))"
        raise AudioEnvelopeError(f"unsupported registry kind {kind}")
    points = envelope["keyframes"]
    expression = n(points[-1]["value"])
    for left, right in reversed(list(zip(points, points[1:]))):
        u = f"(({time}-{n(left['at'])})/{n(right['at']-left['at'])})"
        a, b = left["value"], right["value"]
        c = left.get("curve", "linear")
        segment = (f"({n(a)}+({n(b-a)})*({curve(c, u)}))" if b >= a else
                   f"({n(b)}+({n(a-b)})*({curve(c, f'(1-({u}))')}))")
        expression = f"if(lt({time},{n(right['at'])}),{segment},{expression})"
    expression = f"if(lte({time},{n(points[0]['at'])}),{n(points[0]['value'])},{expression})"
    for child in envelope.get("multiply", []):
        expression = f"({expression})*({ffmpeg_expression(child, time)})"
    minima = [ffmpeg_expression(child, time) for child in envelope.get("minimum", [])]
    if minima:
        minimum = minima[0]
        for child in minima[1:]:
            minimum = f"min({minimum},{child})"
        expression = f"({expression})*({minimum})"
    return expression


def compile_audio_values(clip, *, fps, words, clip_at, duck_rule=None):
    """Value-only producer shared by the SDK adapter and frozen render admission."""
    envelope = compile_legacy(clip, fps)
    if duck_rule is not None:
        if not isinstance(duck_rule, Mapping) or duck_rule.get("under") != "voice":
            raise AudioEnvelopeError("duck requires the existing voice narration selector", duck=True)
        def seconds(value):
            match = re.fullmatch(r"(\d+(?:\.\d+)?)([fs])", str(value))
            if not match:
                raise AudioEnvelopeError("duck attack/release require frame or second durations", duck=True)
            return float(match[1]) / (fps if match[2] == "f" else 1)
        by = re.fullmatch(r"(-\d+(?:\.\d+)?)dB", str(duck_rule.get("by")))
        if not by:
            raise AudioEnvelopeError("duck by requires negative dB", duck=True)
        if not words:
            raise AudioEnvelopeError("pinned narration word context is missing", duck=True)
        duck = compile_duck(words, by_db=float(by[1]), attack=seconds(duck_rule.get("attack")),
                            release=seconds(duck_rule.get("release")), clip_at=clip_at)
        if envelope is None:
            envelope = duck
        else:
            envelope.setdefault("multiply", []).append(duck)
            envelope["duck"] = True
    return {"envelope": envelope} if envelope is not None else None
