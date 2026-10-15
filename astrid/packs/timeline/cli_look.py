"""The look verbs: --fade-in, --fade-out, --entrance, --exit, --opacity, --volume on `timelines edit`.

They write the clip-level fields through astrid.core.timeline.clip_fields (the one stored shape the
renderer reads), never element params. Pure apart from the Clip it is given.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from astrid.core.timeline import audio_curves
from astrid.core.timeline import clip_fields as cf

LOOK_FLAGS = ("entrance", "exit", "fade_in", "fade_out", "opacity", "volume")
FALLBACK_COMPOSITION = "0.0.6"  # sealed S0's receiving pin
_PACKAGE = Path("remotion") / "node_modules" / "@banodoco" / "timeline-composition" / "package.json"
_VISUAL_KINDS = frozenset({"media", "audio"})


class _Tokens(argparse.Action):
    """Up to `max_tokens` words (--entrance NAME [DURATION] [ease=E]), joined into the human form."""

    def __init__(self, option_strings, dest, *, max_tokens: int, usage: str, **kwargs):
        self.max_tokens = max_tokens
        self.usage = usage
        super().__init__(option_strings, dest, **kwargs)

    def __call__(self, parser, namespace, values, option_string=None):  # noqa: ARG002
        if not 1 <= len(values) <= self.max_tokens:
            parser.error(f"{option_string} takes {self.usage}, e.g. {option_string} {_EXAMPLES[option_string]}")
        setattr(namespace, self.dest, " ".join(values))


_EXAMPLES = {"--entrance": "fade 12f ease=out", "--exit": "fade 12f ease=in", "--fade-in": "8f ease=out",
             "--fade-out": "8f ease=in"}


def add_look_flags(parser: argparse.ArgumentParser) -> None:
    group = parser.add_argument_group("look (clip-level; see clip_fields)")
    usage_entrance = "NAME [DURATION] [ease=E]"
    group.add_argument("--entrance", nargs="+", action=_Tokens, default=None, metavar=usage_entrance,
                       max_tokens=3, usage=usage_entrance,
                       help="An entrance: 'fade 8f', 'fade 12f ease=out', 'fade-up 0.4s', 'scale-in 18f', "
                            "'slide-left 12f', or none. ease= is linear (default), in, out, in-out or step.")
    group.add_argument("--exit", nargs="+", action=_Tokens, default=None, metavar=usage_entrance,
                       max_tokens=3, usage=usage_entrance,
                       help="An exit: 'fade 12f', 'fade 12f ease=in' or 'slide-up 12f'.")
    usage_fade = "DURATION [ease=E|curve=NAME]"
    curves = ", ".join(audio_curves.names())
    group.add_argument("--fade-in", dest="fade_in", nargs="+", action=_Tokens, default=None, metavar=usage_fade,
                       max_tokens=2, usage=usage_fade,
                       help=f"A fade in, e.g. '8f' or '0.4s'; audio also accepts curve=NAME ({curves}).")
    group.add_argument("--fade-out", dest="fade_out", nargs="+", action=_Tokens, default=None, metavar=usage_fade,
                       max_tokens=2, usage=usage_fade,
                       help=f"A fade out, e.g. '8f' or '0.4s'; audio also accepts curve=NAME ({curves}).")
    group.add_argument("--opacity", default=None, metavar="VALUE", help="Opacity 0..1, e.g. '0.6'.")
    volume = group.add_mutually_exclusive_group()
    volume.add_argument("--volume", default=None, metavar="GAIN",
                        help="Gain or dB, e.g. '0.5', 'mute' or '+2dB' (audio and footage). "
                             "A negative dB needs `--volume=-3dB` or `--gain-db -3`.")
    volume.add_argument("--gain-db", dest="gain_db", type=float, default=None, metavar="DB",
                        help="Volume in dB, e.g. '-3' (same as `--volume=-3dB`; audio and footage).")


def has_look(parsed: argparse.Namespace) -> bool:
    return any(getattr(parsed, name, None) is not None for name in (*LOOK_FLAGS, "gain_db"))


def look_values(parsed: argparse.Namespace) -> dict[str, str]:
    values = {name: getattr(parsed, name) for name in LOOK_FLAGS if getattr(parsed, name, None) is not None}
    gain_db = getattr(parsed, "gain_db", None)
    if gain_db is not None:
        values["volume"] = f"{gain_db:g}dB"  # the raw clip_fields value; coerce stays the one codec
    return values


def composition_version(project_dir: Path | None = None) -> str:
    root = project_dir or Path(__file__).resolve().parents[3]
    try:
        lock = json.loads((root / "remotion" / "package-lock.json").read_text(encoding="utf-8"))
        version = lock.get("packages", {}).get("node_modules/@banodoco/timeline-composition", {}).get("version")
    except (OSError, ValueError, AttributeError):
        return FALLBACK_COMPOSITION
    return str(version) if version else FALLBACK_COMPOSITION


def _label(name: str, audio: bool) -> str:
    """The stored field a flag writes, as the user sees it in describe()."""
    if not audio and name == "fade_in":
        return "entrance"
    if not audio and name == "fade_out":
        return "exit"
    return name


def _kind(clip: Any) -> str:
    if clip.is_audio:
        return "audio"
    return clip.element


def _render_entry(name: str, kind: str) -> Mapping[str, Any] | None:
    """The render entry for a kind, as render_status looks it up (exact clip type, then its kind)."""
    spec = cf.FIELDS[name]
    entry = spec.render.get(kind)
    if entry is None:
        generic = kind if kind in _VISUAL_KINDS else "element"
        entry = spec.render.get(generic)
    return entry


def _check(clip: Any, name: str, version: str) -> None:
    kind = _kind(clip)
    label = _label(name, clip.is_audio).replace("_", "-")
    if cf.render_status(kind, name, version) == "renders":
        return
    entry = _render_entry(name, kind)
    if entry is None or entry.get("status") == "ignored":
        raise ValueError(f"{clip.address}: {label} is not honoured on {clip.element}; it does not apply here")
    article = "an" if clip.element[:1] in "aeiou" else "a"
    raise ValueError(f"{clip.address}: {label} does not render on {article} {clip.element} with composition "
                     f"{version}; it needs {entry.get('since', FALLBACK_COMPOSITION)}")


def apply_look(tl: Any, clip: Any, values: Mapping[str, str]) -> list[str]:
    audio = clip.is_audio
    fps = tl.fps
    before = cf.describe(clip.data, fps=fps)
    clip.set_look(**dict(values))
    after = cf.describe(clip.data, fps=fps)
    lines: list[str] = []
    for name in values:
        label = _label(name, audio)
        if before.get(label) == after.get(label):
            lines.append(f"{clip.address} {label}: {before.get(label) or 'none'} (unchanged)")
        else:
            lines.append(f"{clip.address} {label}: {before.get(label) or 'none'} → {after.get(label) or 'none'}")
    return lines
