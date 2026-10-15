"""Shared constants, the edit error, and small helpers used by every timeline module."""
from __future__ import annotations

import difflib
import math
import re
from typing import Any




# Fields of a checkout that are provenance, not content. They are written compactly
# (one line each) so a text edit of the content can never match them by accident.
BASE_KEYS = ("base_parent", "base_parent_payload", "base_placements", "source_mapping")
SHOT_BASE_PREFIX = "base_"
DERIVED_PREFIX = "_"          # derived, read-only helpers in the file: stripped before check/publish
EDITABLE_NOTE = (
    "Edit `at`/`hold` (shot-relative seconds) or `_timeline` (timeline seconds; check converts it). "
    "Keys starting with '_' are derived and refreshed on save; base_* and source_mapping are provenance."
)
ELEMENT_TRACK = {
    "am-pixel-wipe": "chrome", "am-type": "type", "am-callout": "fx", "am-discord": "fx", "am-flap": "fx",
    "am-ui-sketch": "fx", "am-burst": "fx", "am-tweet": "fx", "am-quote": "fx", "am-terminal": "fx",
    "am-orbit": "fx", "am-orgchart": "fx", "am-presenter": "sprite", "am-sprite": "sprite",
    "am-snap-plate": "plate", "am-churn": "plate", "am-footage": "plate", "am-droste": "plate", "am-seasons": "plate",
}
AUDIO_KINDS = {"audio", "vo", "music", "sfx", "voice", "voiceover"}
TIME_RE = re.compile(r"^@?(?:(\d+):)?(\d+(?:\.\d+)?)s?$")
WORD_ID_RE = re.compile(r"^([A-Za-z0-9_.-]+):(\d+)$")
CANVAS = (1920, 1080)
SAFE_MARGIN = (96, 54)        # title-safe at 1920x1080 (90%)


LOGICAL_PX = 6  # canvas px per logical px (the 320×180 grid of 1920×1080); older mark formulas use it
POSITION_KEYS = frozenset({"x", "y"})


class TimelineEditError(ValueError):
    """An edit that cannot be applied; the message says what to do instead."""


def _length_seconds(value: Any, fps: float) -> float:
    """``0.6`` · ``"0.6s"`` · ``"18f"`` → seconds."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*(s|f)?\s*", str(value))
    if not m:
        raise TimelineEditError(f"a length is seconds or frames: 0.6, \"0.6s\" or \"18f\" (got {value!r})")
    return float(m.group(1)) / fps if m.group(2) == "f" else float(m.group(1))


class _Suggest:
    """``AttributeError`` that names the nearest methods (and what timing words mean)."""

    _SYNONYMS = {"hold": "hold_for", "lasting": "hold_for", "length": "hold_for", "for_seconds": "hold_for",
                 "duration_s": "hold_for", "set_text": "set(text=…)", "text_": "set(text=…)", "move": "nudge / on / enter_at",
                 "start_at": "on / enter_at", "end": "until / end_at", "starts": "on", "ends": "until",
                 "delete": "remove", "rename": "the sheet's layer name (or add(layer=…))", "split_at": "split"}

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        names = sorted(n for n in dir(type(self)) if not n.startswith("_"))
        close = difflib.get_close_matches(name, names, n=3, cutoff=0.5)
        hint = self._SYNONYMS.get(name)
        said = (f"; for that use {hint}" if hint else "") + (f"; did you mean {', '.join(close)}?" if close else "")
        raise AttributeError(f"{type(self).__name__} has no {name!r}{said} "
                             "(check the timeline editing skill's SDK guidance)")


def _norm(text: Any) -> str:
    return re.sub(r"[^\w']", "", str(text or "").lower())


def _num(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _r(value: float) -> float:
    return round(float(value), 6)
