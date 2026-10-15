"""Live views of the document: Word, Clip, Cut, StepSequence, Voice, and what a moment resolves against."""
from __future__ import annotations

import copy
import difflib
import functools
import json
import math
import re
from contextlib import contextmanager
from dataclasses import dataclass, replace as _dc_replace
from typing import Any, Mapping, Sequence

from astrid.core.timeline import clip_fields as cf
from astrid.core.timeline import moments as mo
from astrid.sdk import timeline_intent as intent
from astrid.core.timeline.cuts import (
    clip_duration,
)

from astrid.sdk.timeline import commands as _identity_commands
from astrid.sdk.timeline import look as _look
from astrid.sdk.timeline import store as _store
from astrid.sdk.timeline._common import (TIME_RE, TimelineEditError, _Suggest, _length_seconds, _norm, _num, _r)
from astrid.sdk.timeline.selection import ROLE_VOCABULARY, Selection
from astrid.sdk.timeline.doc import (_get_path, _read_words, _set_length, _set_path)
from astrid.sdk.timeline.units import (element_units, formula_from_spec, from_canvas, param_unit, parse_formula_value, parse_moment_value, to_canvas)


# ----------------------------------------------------------------------- views

@dataclass(frozen=True)
class Word:
    """One spoken word. ``start``/``end`` are timeline seconds; ``id`` is ``<segment>:<index>``."""

    id: str
    text: str
    start: float
    end: float
    segment: str
    index: int
    clip_id: str
    shot_id: str

    def __float__(self) -> float:
        return self.start

    def __str__(self) -> str:
        return f"{self.text!r} {self.start:.3f}–{self.end:.3f} s [{self.id}]"


class _LazyParams(dict):
    """An empty params dict that attaches itself to the clip on the first write."""

    def __init__(self, data: dict[str, Any]):
        super().__init__()
        self._data = data

    def _attach(self) -> dict[str, Any]:
        return self._data.setdefault("params", {})

    def __setitem__(self, key, value):
        self._attach()[key] = value

    def update(self, *args, **kwargs):
        self._attach().update(*args, **kwargs)

    def setdefault(self, key, default=None):
        return self._attach().setdefault(key, default)


_PX_TEXT = re.compile(r"-?\d+(?:\.\d+)?px")
_WORDS_OF = re.compile(r"\s*(?:ƒ|f)\(\s*words of \S+\s*\)\s*")
_TYPE_WORDS = {"integer": "an integer", "number": "a number", "string": "a string", "boolean": "true or false",
               "array": "a list", "object": "an object"}


def _show(value: Any) -> str:
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return json.dumps(value, ensure_ascii=False, default=str)


def _matches_type(value: Any, kind: str) -> bool:
    if kind == "boolean":
        return isinstance(value, bool)
    if isinstance(value, bool):  # a bool is not a number (True == 1 in Python)
        return False
    if kind == "integer":
        return isinstance(value, int) or (isinstance(value, float) and value.is_integer())
    if kind == "number":
        return isinstance(value, (int, float))
    if kind == "string":
        return isinstance(value, str)
    if kind == "array":
        return isinstance(value, list)
    if kind == "object":
        return isinstance(value, Mapping)
    return True  # a type this check does not know is not refused here


def validate_param_value(element: str, key: str, value: Any, *, where: str | None = None) -> None:
    """Refuse an element param VALUE that its element.yaml schema does not allow: type, ``enum``, ``minimum``/``maximum``.

    Every write path calls this with the value as the user wrote it, in canvas units (before ``from_canvas``).
    Skipped: a formula or a moment (``ƒ(…)``, ``'"adapt" in w05c'``, ``ƒ(words of n05b)``); a key the element does not
    declare (the key check reports that); a nested path (``states[3].at``). A position on a logical grid
    (am-sprite x/y) is range-checked in its STORED units, because the schema describes stored values there
    (``metadata.units.logical``); the message says so."""
    from astrid.sdk.timeline_address import element_schema

    if re.search(r"[.\[]", key) or _is_formula_or_moment(value):
        return
    schema = element_schema(element)
    prop = (schema.get("properties") or {}).get(key)
    if not isinstance(prop, Mapping):
        return
    label = f"{where or element}.{key}"
    shown = _show(value)
    kind = prop.get("type")
    num = value
    if kind in ("number", "integer") and isinstance(value, str) and _PX_TEXT.fullmatch(value.strip()):
        num = float(value.strip()[:-2])  # "20px" is canvas px, as from_canvas reads it
    kinds = [kind] if isinstance(kind, str) else kind if isinstance(kind, list) else []
    if kinds and not any(_matches_type(num, k) if k != "null" else num is None for k in kinds):
        expected = " or ".join(_TYPE_WORDS.get(k, k) for k in kinds)
        hint = f"; use --clear {key} to remove it" if value is None else ""
        raise TimelineEditError(f"{label}: {shown} is not {expected}{hint}")
    enum = prop.get("enum")
    if isinstance(enum, list) and enum and not any(value == e and isinstance(value, bool) == isinstance(e, bool) for e in enum):
        raise TimelineEditError(f'{label}: {shown} is not allowed; {key} takes {" | ".join(str(e) for e in enum)}')
    if isinstance(num, (int, float)) and not isinstance(num, bool):
        checked, note = num, ""
        if key in element_units(element)[1]:  # the element's declared unit describes the stored grid value
            checked = from_canvas(element, key, num)
            note = f" (stored {_show(checked)} on the logical grid)"
        hi, lo = prop.get("maximum"), prop.get("minimum")
        if isinstance(hi, (int, float)) and checked > hi:
            raise TimelineEditError(f"{label}: {shown}{note} is above its maximum {_show(hi)}")
        if isinstance(lo, (int, float)) and checked < lo:
            raise TimelineEditError(f"{label}: {shown}{note} is below its minimum {_show(lo)}")


def _is_formula_or_moment(value: Any) -> bool:
    return isinstance(value, str) and (parse_formula_value(value) is not None or parse_moment_value(value) is not None
                                       or _WORDS_OF.fullmatch(value) is not None)


_LOOK_NOTE = ("{name} is a clip-level field: written with clip.look (clip.look.set({name}=…), CLI {flag})")
_LOOK_FLAG = {"volume": "--volume / --gain-db"}


class Clip(_Suggest):
    """A live view of one clip. Reads and writes go straight to the checkout document."""

    def __init__(self, tl: "Checkout", shot_id: str, data: dict[str, Any]):  # noqa: F821
        self._tl = tl
        self.shot_id = shot_id
        self.data = data

    # identity -----------------------------------------------------------
    @property
    def id(self) -> str:
        return str(self.data.get("id") or "")

    @property
    def element(self) -> str:
        return str(self.data.get("clipType") or "media")

    @property
    def asset(self) -> str | None:
        value = self.data.get("asset")
        return str(value) if isinstance(value, str) else None

    @property
    def track(self) -> str:
        return str(self.data.get("track") or "")

    @property
    def params(self) -> dict[str, Any]:
        """The element params (live: changes write through). A clip without params reads as ``{}``
        and is not given an empty one just by looking (that would read as an edit)."""
        value = self.data.get("params")
        if isinstance(value, dict):
            return value
        return _LazyParams(self.data)

    @property
    def text(self) -> str | None:
        params = self.data.get("params")
        if isinstance(params, Mapping):
            for key in ("text", "title", "body", "name"):
                if isinstance(params.get(key), str) and params[key].strip():
                    return params[key]
        return None

    @property
    def is_audio(self) -> bool:
        return self._tl._is_audio(self.shot_id, self.data)

    @property
    def anchor(self) -> str | None:
        """The moment this clip starts on (``"viral"``, ``+0.8s`` …), or None."""
        return intent.on(self.data)

    @property
    def moments(self) -> dict[str, Any]:
        """``{on, until, for}``: when this clip starts and ends, as stored (None where unset)."""
        return {"on": intent.on(self.data), "until": intent.until(self.data), "for": intent.for_s(self.data)}

    @property
    def cut_id(self) -> str | None:
        return intent.cut_of(self.data)

    @property
    def cut(self) -> str | None:
        """The cut this clip belongs to (``c08``); an alias of ``cut_id``."""
        return self.cut_id

    @property
    def layer_name(self) -> str | None:
        return intent.layer_of(self.data)

    @property
    def address(self) -> str:
        """``c22.rocket`` when the clip has a cut and a layer name, else its clip id."""
        cut, layer = self.cut_id, self.layer_name
        return f"{cut}.{layer}" if cut and layer else self.id

    # clocks --------------------------------------------------------------
    @property
    def at(self) -> float:
        """Shot-relative start (the stored field). Prefer ``start``."""
        return _num(self.data.get("at"))

    @property
    def start(self) -> float:
        """Timeline seconds."""
        return _r(self._tl._shot_start(self.shot_id) + self.at)

    @property
    def duration(self) -> float:
        return _r(clip_duration(self.data))

    @property
    def end(self) -> float:
        return _r(self.start + self.duration)

    @property
    def start_frame(self) -> int:
        return int(round(self.start * self._tl.fps))

    @property
    def end_frame(self) -> int:
        return int(round(self.end * self._tl.fps))

    def __repr__(self) -> str:
        what = self.asset or (f'"{self.text[:28]}"' if self.text else "")
        anchor = f" on {self.anchor}" if self.anchor else ""
        return f"<Clip {self.address} {self.element} {what} {self.start:.3f}–{self.end:.3f} s{anchor}>"

    # edits ---------------------------------------------------------------
    def on(self, moment: Any) -> "Clip":
        """Start on a moment: ``"viral"``, ``after "Astrid"``, ``beat 2 after "Astrid"``, ``c22``, ``+0.8s``.

        Stored as written (seconds are only the cache), then resolved now. A plain number
        is a time: it becomes an offset from the clip's cut (or a fixed time without a cut)."""
        self._tl._note_bare_word(moment, self)
        orphan_cut = intent.cut_of(self.data) if intent.orphan(self.data) and self.track == "plate" else None
        old_start = self.start
        for clip in self._sequence_mates():  # a new moment re-homes an orphan (a whole sequence)
            intent.set_orphan(clip.data, None)
        if orphan_cut:
            # re-homing a cut's picture brings the whole cut: its other orphans move by the same amount
            text = self._tl._moment_text(moment)
            t = self._tl._moment_time(text, self, in_point=True)
            delta = t - old_start
            for clip in self._tl._cut_clips(orphan_cut):
                if clip.data is not self.data and intent.orphan(clip.data):
                    clip._set_start(self._tl._frame(clip.start + delta + 1e-6))
                    if intent.sequence(clip.data) or not intent.on(clip.data) or intent.on(clip.data).startswith(("+", "-")):
                        intent.set_orphan(clip.data, None)  # its place is relative to the cut: home again
            self._tl._mcache = None
        self._tl._place_on(self, moment)
        self._tl.retime()  # its end follows its rule (until / for / its cut) right away
        cut = intent.cut_of(self.data)
        span = self._tl._cut_spans().get(cut) if cut and not self._tl._is_picture(self) else None
        if span is not None:
            lo, hi = span[0], span[1] if span[1] is not None else self._tl.duration
            if not lo - 0.5 / self._tl.fps <= self.start < hi - 0.5 / self._tl.fps:
                there = self._tl._cut_on_screen(self.start)
                self._tl.notes.append(f"{self.address}: {intent.on(self.data)} = {self.start:.3f} s is outside its cut {cut} "
                                      f"({lo:.2f}–{hi:.2f} s)" + (f", in {there}" if there else "")
                                      + f"; move it to that cut's moments, or pick a moment inside {cut}")
        return self

    def keep(self) -> "Clip":
        """Accept an orphan where it is (a fixed time, no moment): it stops blocking publish. Params on a
        moment that no longer resolves keep their last value as a fixed one."""
        for clip in self._sequence_mates():
            intent.set_orphan(clip.data, None)
        on = intent.on(self.data)
        if on:
            try:
                self._tl._moment_time(on, self)
            except TimelineEditError:
                intent.set_on(self.data, None)
        for path, expr in intent.formulas(self.data).items():
            if isinstance(expr, Mapping) and expr.get("moment"):
                try:
                    self._tl._evaluate(self, expr, {}, self._tl.words(), self._tl.slots)
                except TimelineEditError:
                    intent.set_formula(self.data, path, None)  # keeps its last value
        return self

    def _sequence_mates(self) -> list["Clip"]:
        seq = intent.sequence(self.data)
        if seq is None:
            return [self]
        return [c for c in self._tl.clips() if (intent.sequence(c.data) or ("",))[0] == seq[0]]

    def remove_layer(self) -> list[str]:
        """Remove this layer (a whole sequence, when it is one of its steps). Returns the clip ids removed."""
        mates = self._sequence_mates()
        for clip in mates:
            clip.remove()
        return [c.id for c in mates]

    def until(self, moment: Any) -> "Clip":
        """End on a moment (``"Astrid"`` = when the word STARTS, ``after "viral"`` = when it ends, ``c26``,
        ``end of c30``, ``end`` = its cut's end); its start does not move. The sheet's ``for 0.6s`` works too."""
        if isinstance(moment, str) and re.match(r"^\s*for\s+", moment):
            return self.hold_for(moment.strip()[3:].strip())
        self._tl._note_bare_word(moment, self)
        text = self._tl._moment_text(moment)
        t = self._tl._moment_time(text, self)
        if t <= self.start + 0.5 / self._tl.fps:
            raise TimelineEditError(f"{self.address}: {text} is at {t:.3f} s, before the clip starts ({self.start:.3f} s)")
        if self._already_ends_with_cut(text, t):
            self._tl.notes.append(f"{self.address}: already ends with its cut (no change)")
            return self
        intent.set_until(self.data, text)
        intent.set_for(self.data, None)
        _set_length(self.data, self._tl._frame(t) - self.start)
        self._tl.retime()
        return self

    def enter_at(self, when: Any, *, offset: float = 0.0, anchor: bool = True) -> "Clip":
        """Start at a word or a time, keeping the length. A word becomes the clip's ``on`` moment."""
        word = self._tl._as_word(when)
        if word is not None and anchor:
            moment = mo.word_moment(word, self._tl.words())
            if offset:
                moment = mo.parse(f"{mo.format_moment(moment)} {mo.offset_text(offset, self._tl.fps)}")
            return self.on(moment)
        t = (word.start if word else self._tl.time(when)) + offset
        return self.on(t)

    def nudge(self, seconds: float = 0.0, *, frames: int = 0) -> "Clip":
        """Move by seconds and/or frames. A clip on a moment keeps it: the nudge becomes its offset (``"viral" +2f``)."""
        current = intent.on(self.data)
        if current:
            moment = mo.parse(current).with_offset(seconds=float(seconds), frames=int(frames))
            return self.on(moment)
        delta = float(seconds) + frames / self._tl.fps
        new_start = self._tl.quantize(self.start + delta)
        if abs(new_start - self.start) < 1e-9:  # a zero nudge: write nothing (no moment, no undo step)
            return self
        if intent.cut_of(self.data):
            # a cut's clip with no moment: keep it relative to its cut (as the sheet's "on +0.1s" does),
            # so re-timing keeps the nudge instead of snapping it back to the cut's start
            return self.on(new_start)
        self._set_start(new_start)
        return self

    def hold_for(self, seconds: Any) -> "Clip":
        """A literal length (no moment behind it): ``0.6``, ``"0.6s"``, ``"18f"``. Prefer ``until`` when the
        end means something."""
        return self.set_duration(_length_seconds(seconds, self._tl.fps))

    def for_(self, seconds: Any) -> "Clip":
        """The sheet's ``for 0.6s``: the same as ``hold_for``."""
        return self.hold_for(seconds)

    def _already_ends_with_cut(self, text: str, t: float) -> bool:
        """True when the clip has a cut, no ``until`` and no ``for`` (so it ends with its cut), and the moment
        is ``end`` / ``end of <its cut>`` resolving to that same end, to the frame."""
        cut = intent.cut_of(self.data)
        if not cut or intent.until(self.data) or intent.for_s(self.data) is not None:
            return False
        if text.strip().lower() not in ("end", f"end of {cut}".lower()):
            return False
        return abs(t - self.end) < 0.5 / self._tl.fps

    def set_duration(self, seconds: float) -> "Clip":
        if seconds <= 0:
            raise TimelineEditError(f"{self.address}: a duration must be positive (got {seconds})")
        new = self._tl.quantize(seconds)
        if intent.cut_of(self.data) and not intent.until(self.data) and intent.for_s(self.data) is None \
                and abs(new - self.duration) < 0.5 / self._tl.fps:
            # the length is unchanged and the only thing written would be an explicit ``for`` that changes nothing
            self._tl.notes.append(f"{self.address}: already ends with its cut (no change)")
            return self
        _set_length(self.data, new)
        if intent.cut_of(self.data) or intent.until(self.data):
            intent.set_until(self.data, None)
            intent.set_for(self.data, self._tl.quantize(seconds))
        return self

    def _source_duration(self) -> float:
        entry = self._tl._registry(self.shot_id).get(str(self.data.get("asset"))) or {}
        duration = _num(entry.get("duration")) or _store.media_seconds(
            str(self._tl.bundle.get("project_id") or ""), entry)
        if duration is None or duration <= 0:
            raise TimelineEditError(f"{self.address}: cannot read the source file's duration to validate this edit")
        return float(duration)

    def trim(self, in_s: float | None = None, out_s: float | None = None) -> "Clip":
        """Set the source in/out in seconds. The film start stays put and its length follows the source window."""
        if not self.is_audio and self.element != "media":
            raise TimelineEditError(f"{self.address}: trim applies to media clips")
        limit = self._source_duration()
        source_in = _num(self.data.get("from")) if in_s is None else float(in_s)
        source_out = (_num(self.data.get("to"), limit) if out_s is None else float(out_s))
        if not math.isfinite(source_in) or not math.isfinite(source_out) or source_in < 0 or source_out > limit or source_out <= source_in:
            raise TimelineEditError(f"{self.address}: source range {source_in:g}–{source_out:g} s must fit inside the file (0–{limit:g} s)")
        speed = _num(self.data.get("speed"), 1.0) or 1.0
        self.data["from"], self.data["to"] = _r(source_in), _r(source_out)
        self.data["hold"] = _r((source_out - source_in) / speed)
        intent.set_until(self.data, None)
        intent.set_for(self.data, None)
        self._tl._mcache = None
        return self

    def slip(self, seconds: float) -> "Clip":
        """Move the source window while keeping the clip's film position and duration fixed."""
        shift = float(seconds)
        if not math.isfinite(shift):
            raise TimelineEditError(f"{self.address}: slip must be finite seconds")
        limit = self._source_duration()
        original_in = _num(self.data.get("from"))
        source_in = original_in + shift
        source_out = _num(self.data.get("to"), original_in + self.duration) + shift
        if source_in < 0 or source_out > limit:
            raise TimelineEditError(f"{self.address}: slipping {shift:g} s gives source range {source_in:g}–{source_out:g} s; "
                                    f"the file limit is 0–{limit:g} s")
        self.data["from"], self.data["to"] = _r(source_in), _r(source_out)
        self._tl._mcache = None
        return self

    def extend(self, seconds: float = 0.0, *, frames: int = 0) -> "Clip":
        """Lengthen (or, negative, shorten) the clip at its end. Nothing else moves."""
        return self.set_duration(self.duration + float(seconds) + frames / self._tl.fps)

    def end_at(self, when: Any, *, offset: float = 0.0) -> "Clip":
        if self._tl._as_word(when) is not None or (isinstance(when, str) and not TIME_RE.match(when.strip())):
            moment = self._tl._moment_text(when)
            if offset:
                moment = f"{moment} {mo.offset_text(offset, self._tl.fps)}"
            return self.until(moment)
        return self.set_duration(self._tl.quantize(self._tl.time(when) + offset) - self.start)

    def get(self, key: str) -> Any:
        """A param as you would write it: positions (x, y) in CANVAS px for every element.

        (``clip.params`` is the stored dict; an ``am-sprite`` stores x/y on its 320×180 grid, ×6.)"""
        return to_canvas(self.element, key, self.params.get(key))

    def set(self, **params: Any) -> "Clip":
        """Update element params (``x``, ``size``, ``text`` …). Positions are canvas px for every element.

        A param computed by a formula (``x = ƒ(B2-HAND −42)``, ``states[3].at = ƒ("adapt" in w05c)``)
        is never silently recomputed over your value: a plain value replaces the formula (and
        ``changes()`` says so); ``x="ƒ(B2-HAND -60)"`` edits a mark, and a moment
        (``'"adapt" in w05c'``, ``'ƒ(beat 2 after "Astrid")'``) puts a time-valued param on that moment.
        Nested params take their address: ``clip.set(**{"states[3].at": '"adapt" in w05c'})``.

        A key the element does not declare (its element.yaml) is refused with the keys it takes; timing is
        not a param (``.hold_for(0.6)``, ``.until(…)``, ``.on(…)``). ``_allow_new=True`` adds one on purpose."""
        from astrid.sdk.timeline_address import element_schema

        allow_new = bool(params.pop("_allow_new", False))
        declared = set(element_schema(self.element).get("properties") or {})
        look: dict[str, Any] = {}
        for key in list(params):  # a clip-level field the element does not declare is written with clip.look
            if key in declared:
                continue
            if key in cf.FIELDS:
                look[key] = params.pop(key)
            elif cf.is_reserved(key):
                raise TimelineEditError(f"{self.address}: {key} is a clip-level field that is not available yet")
        if look:
            self.set_look(**look)  # validated and written before any element param: all-or-nothing across both
            for key in look:
                self._tl.notes.append(f"{self.address}: {_LOOK_NOTE.format(name=key, flag=_LOOK_FLAG.get(key, '--' + key))}")
        if not allow_new:
            self._check_param_keys(list(params))
        for key, value in params.items():  # every value is checked before anything is written (all-or-nothing)
            validate_param_value(self.element, key, value, where=self.address)
        for key, value in params.items():
            self.set_param(key, value)
        return self

    @property
    def look(self) -> "_look.Look":
        """The clip-level fields (opacity, entrance, exit, fade_in, fade_out, crop_*, speed, volume, transition):
        ``clip.look["volume"]``, ``clip.look.set(volume=0.5)``. See astrid/sdk/timeline/look.py."""
        return _look.Look(self)

    def set_look(self, **fields: Any) -> "Clip":
        """Write clip-level fields (all-or-nothing): each value is coerced and the result checked on a copy first."""
        audio = self.is_audio
        for name in fields:
            if name not in cf.FIELDS:
                raise TimelineEditError(f"{self.address}: {name!r} is not a clip-level field; allowed: {', '.join(sorted(cf.FIELDS))}")
            applies = cf.FIELDS[name].applies_to
            if (audio and applies == "visual") or (not audio and applies == "audio"):
                raise TimelineEditError(f"{self.address}: {name} does not apply to {'an audio' if audio else 'a visual'} clip")
        try:
            cf.coerce_many(fields, fps=self._tl.fps, audio=audio)
            trial = copy.deepcopy(self.data)
            for name, value in fields.items():
                cf.write(trial, name, value, fps=self._tl.fps, audio=audio)
        except cf.ClipFieldError as exc:
            raise TimelineEditError(f"{self.address}: {exc}") from None
        for name, value in fields.items():
            cf.write(self.data, name, value, fps=self._tl.fps, audio=audio)
        self._tl._mcache = None
        version = _look.composition_version()
        for name in fields:
            if cf.render_status(_look.render_kind(self), name, version) != "renders":
                self._tl.notes.append(_look._unrendered_line(self, name, version))
        return self

    def tag(self, **kv: Any) -> "Clip":
        """The validated setter for ``app.role``, ``app.style``, ``app.group`` and ``app.duck`` (None removes one)."""
        unknown = sorted(set(kv) - {"role", "style", "group", "duck"})
        if unknown or not kv:
            raise TimelineEditError(f"{self.address}: tag takes role, style, group or duck"
                                    + (f"; not {', '.join(unknown)}" if unknown else ""))
        for key, value in kv.items():
            if value is None:
                continue
            if key == "role" and value not in ROLE_VOCABULARY:
                raise TimelineEditError(f"{self.address}: role {value!r} is not in the vocabulary; allowed: {', '.join(ROLE_VOCABULARY)}")
            if key in ("style", "group") and (not isinstance(value, str) or not value.strip()):
                raise TimelineEditError(f"{self.address}: {key} must be a non-empty string; got {value!r}")
            if key == "duck" and not isinstance(value, Mapping):
                raise TimelineEditError(f"{self.address}: duck must be a mapping of the duck rule; got {value!r}")
        for key, value in kv.items():
            intent._set_or_drop(self.data, key, copy.deepcopy(dict(value)) if key == "duck" and value is not None else value)
        return self

    def move_to_cut(self, cut: Any, *, on: Any = None) -> "Clip":
        """Re-home this layer into cut ``cut`` (an id). Without ``on=`` its offset into its old cut is kept as an
        offset into the new one; with ``on=`` it starts on that moment. A cut's picture moves with the cut verbs."""
        if self._tl._is_picture(self):
            raise TimelineEditError(f"{self.address} is a cut's picture: move the cut's picture with the cut verbs")
        target = self._tl._cut_id(str(cut)) or str(cut)
        spans = self._tl._cut_spans()
        if target not in spans:
            raise TimelineEditError(self._tl._no_cut(target))
        old = self.cut_id
        offset = None
        if on is None:
            if old not in spans:
                raise TimelineEditError(f"{self.address} has no cut to keep its offset from; pass on= (a moment in {target})")
            offset = self.start - spans[old][0]
        if old == target and on is None:
            return self
        intent.set_cut(self.data, target)
        self.on(on if on is not None else f"+{offset:.2f}s")
        return self

    _TIMING_WORDS = {"for": ".hold_for(0.6) (or .for_(\"0.6s\"))", "hold": ".hold_for(0.6)", "duration": ".hold_for(0.6)",
                     "length": ".hold_for(0.6)", "lasting": ".hold_for(0.6)", "until": ".until(MOMENT)",
                     "on": ".on(MOMENT)", "at": ".on(MOMENT) or .enter_at(TIME)", "start": ".on(MOMENT)",
                     "end": ".until(MOMENT)", "asset": ".swap_asset(KEY)"}

    def _check_param_keys(self, keys: list[str]) -> None:
        from astrid.sdk.timeline_address import element_schema, unknown_params

        tops = [re.split(r"[.\[]", k, maxsplit=1)[0] for k in keys]
        timing = [k for k in tops if k in self._TIMING_WORDS and k not in (element_schema(self.element).get("properties") or {})]
        if timing:
            raise TimelineEditError(f"{self.address}: {', '.join(timing)} is not a param: use "
                                    + "; ".join(f"{k} → {self._TIMING_WORDS[k]}" for k in timing))
        bad = unknown_params(self.element, tops, existing=self.params)
        if bad:
            props = sorted(element_schema(self.element).get("properties") or {})
            hints = [f"{k} (did you mean {', '.join(difflib.get_close_matches(k, props, n=2))}?)"
                     if difflib.get_close_matches(k, props, n=2) else k for k in bad]
            raise TimelineEditError(f"{self.address} is an {self.element}, which has no param {', '.join(hints)}. It takes: "
                                    f"{', '.join(props)} (an undeclared param on purpose: --allow-new-params / .set(..., _allow_new=True))")

    def set_param(self, path: str, value: Any) -> "Clip":
        """Set one param by its address inside the clip (``x``, ``states[3].at``, ``stamp.at``)."""
        validate_param_value(self.element, path, value, where=self.address)
        top = re.split(r"[.\[]", path, maxsplit=1)[0]
        formulas = intent.formulas(self.data)
        full = f"params.{path}"
        current_expr = formulas.get(full) or {}
        spec = parse_formula_value(value)
        moment = None
        words_of = re.fullmatch(r"\s*(?:ƒ|f)\(\s*words of (\S+)\s*\)\s*", value) if isinstance(value, str) else None
        if words_of:  # ƒ(words of n05b): the line's words, clip-relative (presenter lip-sync)
            expr = {"words_of": words_of.group(1)}
            self._tl.voice(words_of.group(1)).clips  # an unknown line says so (and lists the lines)
            intent.set_formula(self.data, full, expr)
            _set_path(self.data, full, self._tl._evaluate(self, expr, {}, self._tl.words(), self._tl.slots, full))
            return self
        if spec is None:
            moment = parse_moment_value(value, current_expr)
        if spec is not None:  # a slot mark: ƒ(B2-HAND -42)
            expr = formula_from_spec(spec, self.element, top if path == top else path.rsplit(".", 1)[-1], current_expr)
            intent.set_formula(self.data, full, expr)
            _set_path(self.data, full, self._tl._evaluate(self, expr, {}, self._tl.words(), self._tl.slots, full))
            return self
        if moment is not None:  # a time-valued param on a moment
            current = _get_path(self.data, full)
            declared = param_unit(self.element, full)
            unit = current_expr.get("as") or {"clip frames": "clip_frame", "clip seconds": "clip_seconds"}.get(declared or "") \
                or ("clip_frame" if isinstance(current, int) and not isinstance(current, bool) else "clip_seconds")
            expr = {k: v for k, v in current_expr.items() if k in ("min", "max", "step", "snap", "offset_frames")} if current_expr.get("moment") else {}
            expr.update({"moment": moment, "as": unit})
            try:
                resolved = self._tl._evaluate(self, expr, {}, self._tl.words(), self._tl.slots)
            except TimelineEditError as exc:
                raise TimelineEditError(f"{self.address}.{path}: {exc}") from None
            intent.set_formula(self.data, full, expr)
            _set_path(self.data, full, resolved)
            return self
        for existing in [p for p in formulas if p == full or p.startswith((f"{full}[", f"{full}."))]:
            intent.set_formula(self.data, existing, None)  # a fixed value replaces the formula: never a silent revert
        if path == top:
            self.params[top] = copy.deepcopy(from_canvas(self.element, top, value))
        else:
            self.data.setdefault("params", {})
            _set_path(self.data, full, copy.deepcopy(value))
        return self

    def clear(self, *keys: str) -> "Clip":
        """Remove presence (including formula intent); the element default applies.

        Clearing an absent key is a no-op. Null remains a value, never a clear.
        """
        from astrid.sdk.timeline_address import element_schema
        declared = element_schema(self.element).get("properties") or {}
        for key in keys:
            if key not in declared and key in cf.FIELDS:
                self.set_look(**{key: None})
                continue
            self._check_param_keys([key])
            full = f"params.{key}"
            if re.search(r"[.\[]", key):
                raise TimelineEditError("clear takes a top-level param key")
            self.params.pop(key, None)
            for path in list(intent.formulas(self.data)):
                if path == full or path.startswith((full + ".", full + "[")):
                    intent.set_formula(self.data, path, None)
        return self

    def set_track(self, track: str) -> "Clip":
        """Move the clip to another track (stacking: chrome > type > fx > sprite > plate). Its timing does not
        change: a cut's picture on a line's first word opens at the take's in-point; as a layer it keeps that
        frame (its moment gets the offset that says so)."""
        if track == self.track:
            return self
        if not any(str(t.get("id")) == track for t in self._tl._internal(self.shot_id).get("tracks") or []):
            raise TimelineEditError(f"{self.address}: no track {track!r} in this shot")
        was_picture, start, on = self._tl._is_picture(self), self.start, intent.on(self.data)
        self.data["track"] = track
        self._tl._mcache = None
        if was_picture and on and not self._tl._is_picture(self):
            try:
                plain = self._tl._moment_time(on, self)
            except TimelineEditError:
                plain = start
            if abs(plain - start) >= 0.5 / self._tl.fps:
                moment = mo.parse(on).with_offset(frames=int(round((start - plain) * self._tl.fps)))
                intent.set_on(self.data, mo.format_moment(moment))
        return self

    def clear_asset(self) -> "Clip":
        """Take the asset off this clip (e.g. an am-footage slot that should draw its slot card again)."""
        self.data.pop("asset", None)
        return self

    def swap_asset(self, asset: Any) -> "Clip":
        """Point the clip at another asset: a registry key, a ``{media_id, …}`` entry, a local file (imported)
        or a media handle (``run:<id>/music``, ``ref:NAME``, ``sha256:…``).

        An audio clip that played its whole file plays the whole new one (and never runs past its end).
        A music clip takes the beat grid made with the new file: the ``beats`` output of the run that
        made it, or ``NAME.beats.json``/``beats.json`` beside a local file; every beat moment re-resolves.
        If no grid comes along, check blocks until you attach one (``set_beats``) or keep the old one.
        To change the media behind a key for every clip, use ``tl.replace_asset(KEY, HANDLE)``."""
        registry = self._tl._registry(self.shot_id)
        old_entry = copy.deepcopy(registry.get(str(self.data.get("asset"))) or {})
        key = self._tl._register_asset(self.shot_id, asset)
        self.data["asset"] = key
        new_entry = self._tl._registry(self.shot_id).get(key) or {}
        if self.is_audio and not intent.cut_of(self.data) and new_entry.get("media_id") != old_entry.get("media_id"):
            self._tl._follow_media(self, old_entry, new_entry, asset)
        return self

    def set_beats(self, source: Any) -> "Clip":
        """Attach this music clip's beat grid: a beats.json path, a media handle (``run:<id>/beats``), a
        dict like beats.json, or ``"keep"`` (the grid it has is right for the file it now plays).

        Every beat moment (``beat 2 after "Astrid"``, ``downbeat 1 before c30``) re-resolves on the new
        grid; what moved is in ``tl.notes`` and ``tl.changes()``."""
        if isinstance(source, str) and source.strip().lower() == "keep":
            value = intent._app(self.data).get("beats")
            if not value:
                raise TimelineEditError(f"{self.address} has no beat grid to keep; attach one: set_beats(PATH|HANDLE)")
            grid = dict(value) if isinstance(value, Mapping) else {"beats": intent.beat_sources(self.data), "time": "cue_seconds"}
            label = intent.beats_label(self.data) or "its grid"
        else:
            grid, label = _store.load_beats(str(self._tl.bundle.get("project_id") or ""), source)
        self._tl._attach_beats(self, grid, label)
        return self

    def keyframe_at(self, index: int, when: Any) -> "Clip":
        """Move element keyframe ``index`` to a word or time (clip frames, snapped to ``stepFrames``)."""
        keyframes = self.params.get("keyframes")
        if not isinstance(keyframes, list) or not 0 <= index < len(keyframes):
            raise TimelineEditError(f"{self.id} has no keyframe {index}; it has {len(keyframes or [])}")
        step = max(1, int(_num(self.params.get("stepFrames"), 1)))
        frame = int((self._tl.time(when) - self.start) * self._tl.fps + 1e-6) // step * step
        delta = frame - int(_num(keyframes[index].get("frame")))
        for key in keyframes[index:]:
            key["frame"] = int(_num(key.get("frame"))) + delta
        return self

    def detach_audio(self, *, track: str = "audio") -> "Clip":
        """Detach a media video's audio, preserving source range, speed and gain."""
        from astrid.sdk.timeline.edits import _media_kind
        tl = self._tl
        entry = tl._registry(self.shot_id).get(str(self.asset)) or {}
        if self.element != "media" or self.is_audio or _media_kind(entry) != "video":
            raise TimelineEditError("detach_audio requires a media video clip")
        if not any(t.get("id") == track and t.get("kind") in {"audio", "music", "voice"}
                   for t in tl._internal(self.shot_id).get("tracks") or []):
            raise TimelineEditError(f"no audio track {track!r} in shot {self.shot_id}")
        if self.data.get("volume") == 0:
            raise TimelineEditError("this video's audio is already muted/detached")
        data = copy.deepcopy(self.data)
        data["id"] = tl._new_id(f"{self.id}-audio")
        data["track"] = track
        # A detached audio layer has independent duration, rather than becoming
        # another picture member of the cut; its original anchor still follows.
        for key in ("cut", "layer", "sequence", "sequence_index"):
            intent._set_or_drop(data, key, None)
        self.data["volume"] = 0
        tl._internal(self.shot_id)["clips"].append(data)
        return Clip(tl, self.shot_id, data)

    def remove(self) -> None:
        clips = self._tl._internal(self.shot_id)["clips"]
        clips[:] = [c for c in clips if c is not self.data]

    def _set_start(self, seconds: float) -> None:
        self.data["at"] = _r(seconds - self._tl._shot_start(self.shot_id))


class Cut(_Suggest):
    """One picture cut (the numbering show/lint/visualize print)."""

    def __init__(self, tl: "Checkout", raw: Mapping[str, Any]):  # noqa: F821
        self._tl = tl
        self.raw = raw
        self.n = int(raw["index"])
        self.start = float(raw["start"])
        self.end = float(raw["end"])
        self.shot_id = str(raw["shot_id"])
        self.shot = str(raw.get("shot") or self.shot_id)

    @property
    def duration(self) -> float:
        return _r(self.end - self.start)

    @property
    def picture(self) -> Clip | None:
        clip = self.raw.get("clip")
        return Clip(self._tl, self.shot_id, clip) if isinstance(clip, dict) else None

    @property
    def steps(self) -> list[Clip]:
        """The picture clips of a sequence cut (a time-lapse), in order; one item otherwise."""
        sequence = self.raw.get("sequence") or {}
        ids = list(sequence.get("clip_ids") or [])
        if not ids:
            return [self.picture] if self.picture else []
        return [self._tl.clip(i) for i in ids]

    def step(self, n: int) -> Clip:
        steps = self.steps
        if not 1 <= n <= len(steps):
            raise TimelineEditError(f"cut {self.n} has {len(steps)} step(s); ask for 1–{len(steps)}")
        return steps[n - 1]

    @property
    def layers(self) -> list[Clip]:
        return [Clip(self._tl, self.shot_id, span["clip"]) for span in self.raw.get("layers") or []]

    @property
    def id(self) -> str | None:
        """The cut id of its picture (``c08``), or None."""
        pic = self.picture
        return intent.cut_of(pic.data) if pic else None

    @property
    def clips(self) -> Selection:
        return Selection(([self.picture] if self.picture else []) + self.layers, kind="clip", tl=self._tl)

    def layer(self, query: str) -> Clip:
        return self._tl._pick(self.clips, query, where=f"cut {self.n}")

    def set_note(self, *, why: str | None = None, hold: str | bool | None = None) -> dict[str, Any]:
        """This cut's notes (``why``, ``hold``): the cut-level form of ``tl.set_cut_note``."""
        if self.id is None:
            raise TimelineEditError(f"cut {self.n} has no id; it cannot keep notes")
        return self._tl.set_cut_note(self.id, why=why, hold=hold)

    def duplicate(self, at: float, *, id: str | None = None) -> "Cut":
        """Copy this cut and its layers at a timeline time, with fresh identities.

        Absolute placement is explicit; copied layers retain offsets, source
        ranges, keyframes and opaque data. The original cut remains untouched.
        """
        from astrid.sdk.timeline.identity import reserve_cut_id
        tl = self._tl
        if not self.id or not self.picture:
            raise TimelineEditError("duplicate requires a named picture cut")
        if intent.sequence(self.picture.data):
            raise TimelineEditError("duplicate a sequence's individual cut instead")
        members = tl._cut_clips(self.id)
        target = tl.quantize(at)
        sid = tl._shot_at(target)
        if target < 0:
            raise TimelineEditError("duplicate needs a nonnegative placement")
        new_id = id or tl._next_cut_id(self.id)
        reserve_cut_id(tl.bundle, new_id)
        # Cross-shot asset/track remapping belongs to add/import; fail closed.
        if sid != self.shot_id:
            raise TimelineEditError("duplicate within the same shot; cross-shot copy requires asset remapping")
        delta = target - self.start
        for clip in members:
            data = copy.deepcopy(clip.data)
            data["id"] = tl._new_id(f"{new_id}-{clip.id}")
            data["at"] = _r(clip.start + delta - tl._shot_start(sid))
            _set_length(data, clip.duration)
            for setter in (intent.set_on, intent.set_until, intent.set_orphan):
                setter(data, None)
            intent.set_for(data, clip.duration)
            intent.set_cut(data, new_id)
            tl._internal(sid)["clips"].append(data)
        tl._mcache = None
        return tl.cut(new_id)

    def split(self, on: Any, *, id: str | None = None, picture: Any = None) -> "Cut":
        """Split this cut at a moment (``'"Building" in v27'``, ``+1.2s`` into the cut): the new cut gets a
        copy of the picture from that point (footage continues where it was; ``picture=`` another asset),
        layers that start at or after it move with it, layers that span it carry over it (``until end of``
        the new cut), and layers already carried stay carried. The new id is the next free one after this
        cut's (c33 → c33a), or ``id=``. Returns the new cut."""
        return self._tl._split_cut(self, on, new_id=id, picture=picture)

    @property
    def words(self) -> Selection:
        return Selection((w for w in self._tl.words if self.start - 1e-6 <= w.start < self.end - 1e-6),
                         kind="word", tl=self._tl)

    def shorten(self, seconds: float, *, ripple: bool = True) -> "Cut":
        """Take ``seconds`` off the end of this cut; with ripple, everything after moves up."""
        if not 0 < seconds < self.duration:
            raise TimelineEditError(f"cut {self.n} is {self.duration:.3f} s; shorten by less than that")
        if ripple:
            self._tl.ripple_delete(self.end - seconds, self.end)
        else:
            for clip in self.clips:
                if abs(clip.end - self.end) < 1.0 / self._tl.fps:
                    clip.set_duration(clip.duration - seconds)
        return self

    def __repr__(self) -> str:
        pic = self.picture
        return f"<Cut {self.n} {self.start:.3f}–{self.end:.3f} s {self.shot} · {pic.asset if pic else '-'} · {len(self.layers)} layer(s)>"


class StepSequence(_Suggest):
    """A stepped sequence (a time-lapse): its steps in order, moved and fitted as ONE unit."""

    def __init__(self, tl: "Checkout", seq_id: str):  # noqa: F821
        self._tl, self.id = tl, seq_id

    @property
    def steps(self) -> list[Clip]:
        steps = [c for c in self._tl.clips() if (intent.sequence(c.data) or ("",))[0] == self.id]
        return sorted(steps, key=lambda c: intent.sequence(c.data)[1])

    @property
    def frames(self) -> list[int]:
        return [max(1, int(round(c.duration * self._tl.fps))) for c in self.steps]

    def fit(self, on: Any = None, until: Any = None, *, shape: Sequence[int] | None = None, shape_from: Any = None,
            client: Any = None) -> "StepSequence":
        from astrid.sdk.timeline.checkout import Checkout
        """Fit the whole sequence into a span, keeping its shape (each step's share; its acceleration).

        ``on``: where it (and its cut) starts; ``until``: where it ends (``c19``, ``after "x" in w17``; it
        is kept, so a re-flow fits it again). ``shape``: frame counts per step; ``shape_from``: another
        timeline (``"almost-ready-round0"`` or ``"TL@REV"``) whose same sequence has the shape to restore
        (a sequence torn by an earlier re-flow). Default: the shape it has now."""
        steps = self.steps
        if not steps:
            raise TimelineEditError(f"no sequence {self.id!r}")
        frames = list(shape) if shape else None
        if shape_from is not None:
            name, _, rev = str(shape_from).partition("@")
            other = Checkout(_store.fetch_bundle(str(self._tl.bundle.get("project_id")), name,
                                          revision_id=rev if rev.startswith("authoring-parent-revision-") else None, client=client))
            frames = StepSequence(other, self.id).frames
            if not frames:
                raise TimelineEditError(f"{name} has no sequence {self.id!r} to take the shape from")
        if frames is not None and len(frames) != len(steps):
            raise TimelineEditError(f"the shape has {len(frames)} steps; {self.id} has {len(steps)}")
        if shape_from is not None:  # where its shape came from (show says so)
            other_rev = str((other.bundle.get("base_parent") or {}).get("revision_id") or "")
            intent._set_or_drop(steps[0].data, "sequence_shape_from",
                                f"{name}@{other_rev.removeprefix('authoring-parent-revision-')[:8]}" if other_rev else name)
        elif shape is not None:
            intent._set_or_drop(steps[0].data, "sequence_shape_from", "a given shape")
        if frames is not None:  # set the shape now: the re-lay keeps it
            first = steps[0]
            self._tl._relay_steps([c.data for c in steps], first.start - self._tl._shot_start(first.shot_id),
                                  sum(frames) / self._tl.fps, [int(n) for n in frames])
        head = steps[0]
        if on is not None:
            head.on(on)
        if until is not None:
            text = self._tl._moment_text(until)
            self._tl._moment_time(text, head)  # it must resolve
            intent.set_until(head.data, text)
            intent.set_for(head.data, None)
        self._tl.retime()
        return self

    def __repr__(self) -> str:
        steps = self.steps
        return (f"<StepSequence {self.id}: {len(steps)} steps {steps[0].start:.3f}–{steps[-1].end:.3f} s>" if steps
                else f"<Sequence {self.id}: none>")


MUSIC_POLICIES = ("keep_end", "plays_on")
_MUSIC_OLD_NAMES = {"whole": "plays_on", "seams": "keep_end"}  # the internal names old scripts still use


def music_policy(value: Any) -> str:
    """The music bed policy for a re-flow: ``keep_end`` (the default) or ``plays_on``. ``keep-end`` and
    ``plays-on`` (the CLI spelling) are accepted, and so are the old internal names ``seams`` and ``whole``."""
    name = str(value).strip().lower().replace("-", "_")
    name = _MUSIC_OLD_NAMES.get(name, name)
    if name not in MUSIC_POLICIES:
        raise TimelineEditError(f"music must be keep_end (the default: the bed is cut at a bar with a crossfade, so its "
                                f"ending stays on the film's end) or plays_on (the bed keeps playing unchanged); got {value!r}")
    return name


@contextmanager
def retime_scope(tl):
    """The scope of one re-time (W2b). The outermost re-time opens it; nested ones join it. While it is open a
    time shift does not cut the music bed: it notes the change, and the bed is cut once at the end (``_apply_bed_cuts``),
    so one edit makes one seam per place its time changed. Yields the list of cut notes, filled when the outermost
    scope closes. An exception leaves the bed untouched (no cut is made)."""
    cut_notes: list[str] = []
    if tl._scope is not None:
        yield cut_notes
        return
    tl._scope = []
    try:
        yield cut_notes
    except BaseException:
        tl._scope = None
        raise
    records, tl._scope = tl._scope, None
    cut_notes.extend(tl._apply_bed_cuts(records))


def music_keyword(fn):
    """A re-flow method with a ``music=`` keyword: for this call only, the bed policy is ``music`` (None: the
    checkout's ``music``). The checkout's policy is restored afterwards. The call is a re-time scope (see
    ``retime_scope``): the bed is cut once, at the end of the outermost call, and the cut's notes join its report."""
    @functools.wraps(fn)
    def wrapper(self, *args, music=None, **kwargs):
        owner = self if hasattr(self, "_music_as") else self._tl
        with owner._music_as(music):
            with retime_scope(owner) as cut_notes:
                result = fn(self, *args, **kwargs)
            if cut_notes:  # the report: a list of notes (or the report the call just set), never the data it returns
                if isinstance(result, list) and all(isinstance(x, str) for x in result):
                    result.extend(cut_notes)
                elif isinstance(getattr(owner, "report", None), list):
                    owner.report.extend(cut_notes)
            return result
    return wrapper


def _follow_occurrences(tl: "Checkout", segment: str, said_before: Sequence[Any]) -> None:  # noqa: F821
    """After line ``segment`` gets new words, keep every word moment on the word it meant.

    ``"and" in m11 #1`` names an occurrence. A take that says the word once more before it
    ("front-end and back-end features, and share") would silently move the moment onto the
    new word. The line's old and new words are matched by a diff; a moment whose word is
    still said gets the occurrence number that now names it. A word the take no longer says
    keeps its moment, so the re-flow reports it as an orphan.
    """
    said_after = tl._word_list()

    def places(words: Sequence[Any]) -> dict[int, tuple[str, int]]:
        count: dict[str, int] = {}
        out = {}
        for w in words:
            out[id(w)] = (w.segment, count.get(w.segment, 0))
            count[w.segment] = count.get(w.segment, 0) + 1
        return out

    old_places, new_places = places(said_before), places(said_after)
    old_line = [_norm(w.text) for w in said_before if w.segment == segment]
    new_line = [_norm(w.text) for w in said_after if w.segment == segment]
    moved: dict[int, int] = {}
    for tag, i1, i2, j1, _j2 in difflib.SequenceMatcher(a=old_line, b=new_line, autojunk=False).get_opcodes():
        if tag == "equal":
            moved.update({i1 + k: j1 + k for k in range(i2 - i1)})

    def follow(m: Any) -> Any:
        if m.kind in ("beat", "downbeat") and m.base is not None:
            base = follow(m.base)
            return m if base is m.base else _dc_replace(m, base=base)
        if m.kind != "word":
            return m
        hits = mo.find_words(m, said_before)
        if not hits or (m.n is None and len(hits) > 1) or (m.n is not None and not 1 <= m.n <= len(hits)):
            return m
        seg, pos = old_places[id(hits[(m.n or 1) - 1][0])]
        if seg == segment:
            if pos not in moved:
                return m
            pos = moved[pos]
        now = mo.find_words(_dc_replace(m, n=None), said_after)
        k = next((k for k, run in enumerate(now, 1) if new_places[id(run[0])] == (seg, pos)), None)
        if k is None or (len(now) == 1 and m.n is None) or k == m.n:
            return m
        return _dc_replace(m, n=k)

    for clip in tl.clips():
        for read, write in ((intent.on, intent.set_on), (intent.until, intent.set_until)):
            text = read(clip.data)
            if not text:
                continue
            try:
                m = mo.parse(text)
            except mo.MomentError:
                continue
            new = follow(m)
            if new is not m:
                write(clip.data, mo.format_moment(new))
                tl.notes.append(f"{clip.address}: {text} → {mo.format_moment(new)} (the same spoken word in the new take)")
        for path, expr in intent.formulas(clip.data).items():
            if not isinstance(expr, Mapping) or not expr.get("moment"):
                continue
            try:
                m = mo.parse(expr["moment"])
            except mo.MomentError:
                continue
            new = follow(m)
            if new is not m:
                intent.set_formula(clip.data, path, {**expr, "moment": mo.format_moment(new)})
                tl.notes.append(f"{clip.address}.{path.removeprefix('params.')}: {expr['moment']} → "
                                f"{mo.format_moment(new)} (the same spoken word in the new take)")


class Voice(_Suggest):
    """One VO line (all its audio clips), with its words and the silence declared after it."""

    def __init__(self, tl: "Checkout", segment: str):  # noqa: F821
        self._tl = tl
        self.segment = segment

    @property
    def clips(self) -> list[Clip]:
        found = [c for c in self._tl.clips() if c.is_audio and intent.line(c.data) == self.segment]
        if not found:
            known = sorted({intent.line(c.data) for c in self._tl.clips() if c.is_audio and intent.line(c.data)})
            raise TimelineEditError(f"no VO line {self.segment!r}; lines are: {', '.join(known)}")
        return sorted(found, key=lambda c: c.start)

    @property
    def words(self) -> Selection:
        return Selection((w for w in self._tl.words if w.segment == self.segment), kind="word", tl=self._tl)

    @property
    def text(self) -> str:
        """The line's script text (as declared; else its words joined)."""
        declared = intent.line_text(self.clips[0].data)
        return declared if declared is not None else " ".join(w.text for w in self.words)

    @music_keyword
    def replace(self, media: Any, *, words: Any, ripple: bool = True, text: str | None = None) -> list[tuple[str, float, float]]:
        """Swap this line for another take; anchored clips follow their words.

        ``media`` is a registry key, a ``{media_id, …}`` entry or a local WAV (imported).
        ``words`` is a words.json path, ``{"words": [...]}`` or ``[[start, end, text], …]``
        (seconds from the take's start). With ``ripple`` the timeline after the line
        opens or closes by the length difference, so the next line does not overlap.
        Returns the anchored clips that moved: ``[(clip_id, old_start, new_start)]``.
        """
        pieces = self.clips
        first = pieces[0]
        before = self._tl._piece_gaps()
        if pieces[-1].id in before:  # one take now: the line's last silence follows it
            before[first.id] = before[pieces[-1].id]
        old_end = self.speech_end
        anchored = [(c, c.start) for c in self._tl.clips() if c.anchor]
        said_before = self._tl._word_list()
        new_words = _read_words(words)
        if not new_words:
            raise TimelineEditError("the new take has no words; pass its words.json")
        tail = 0.06
        length = new_words[-1][1] + tail
        for piece in pieces[1:]:
            piece.remove()
        key = self._tl._register_asset(first.shot_id, media)
        data = first.data
        data["asset"] = key
        data["from"], data["to"] = 0.0, _r(length)
        data.pop("hold", None)
        intent.set_line(data, self.segment, [[_r(s), _r(e), t] for s, e, t in new_words])
        if text is not None:
            intent.set_line_text(data, text)
        elif intent.line_text(data) is not None and _norm(intent.line_text(data)) != _norm(" ".join(t for _s, _e, t in new_words)):
            self._tl.notes.append(f"line {self.segment}: the new take's words differ from its script text; pass text= to update the narration")
        _follow_occurrences(self._tl, self.segment, said_before)
        self._tl.report = self._tl.reflow(gaps=before, points={first.id: old_end}) if ripple else []
        self._tl.retime()
        return [(c.id, old, c.start) for c, old in anchored if abs(c.start - old) > 1e-6]

    @property
    def speech_end(self) -> float:
        words = self.words
        return words[-1].end if words else self.clips[-1].end

    @property
    def gap_after(self) -> float | None:
        """The declared silence after this line, if any."""
        return intent.gap_after(self.clips[-1].data)

    @music_keyword
    def set_gap_after(self, seconds: float, *, reflow: bool = True) -> "Voice":
        """Declare the silence after this line; with ``reflow`` everything after it moves to honour it."""
        intent.set_gap_after(self.clips[-1].data, _r(seconds))
        if reflow:
            self._tl.reflow()
        return self


class _MomentContext:
    """What a moment resolves against: the words, the music's beats, the cuts, and the clip's own cut."""

    def __init__(self, tl: "Checkout", clip: Clip | None):  # noqa: F821
        self.tl, self.clip, self.fps = tl, clip, tl.fps

    def words(self):
        cache = tl_cache(self.tl)
        if "words" not in cache:
            cache["words"] = self.tl.words()
        return cache["words"]

    def beats(self, kind: str):
        out = []
        for clip in self.tl.clips(audio=True):
            if clip.track != "music" and not intent.beat_sources(clip.data):
                continue
            speed = _num(clip.data.get("speed"), 1.0) or 1.0
            src0 = _num(clip.data.get("from"))
            for b in intent.beat_sources(clip.data, kind):
                t = clip.start + (b - src0) / speed
                if clip.start - 1e-6 <= t < clip.end - 1e-6:
                    out.append(t)
        return sorted(out)

    def cut_start(self, cut_id: str) -> float:
        cut_id = self.tl._cut_id(cut_id) or cut_id
        for g in self.tl._cut_groups():
            if g["id"] == cut_id:
                return g["start"]
        raise mo.MomentError(self.tl._no_cut(cut_id))

    def cut_end(self, cut_id: str) -> float:
        cut_id = self.tl._cut_id(cut_id) or cut_id
        spans = self.tl._cut_spans()
        if cut_id not in spans:
            raise mo.MomentError(self.tl._no_cut(cut_id))
        end = spans[cut_id][1]
        return float(end) if end is not None else self.tl.duration

    def own_cut(self):
        """The clip's cut; a moment asked of the film itself (``tl.time("end")``) is relative to the whole film."""
        return self.tl._own_cut_span(self.clip) if self.clip is not None else (0.0, self.tl.duration)

    def line_in_point(self, line: str):
        """Where the take begins on the timeline (its file's zero: a trimmed or tightened take starts later)."""
        try:
            first = Voice(self.tl, line).clips[0]
        except TimelineEditError:
            return None
        speed = _num(first.data.get("speed"), 1.0) or 1.0
        return first.start - _num(first.data.get("from")) / speed


def tl_cache(tl: "Checkout") -> dict[str, Any]:  # noqa: F821
    if tl._mcache is None:
        tl._mcache = {}
    return tl._mcache


# Identity-owned public mutators use the same explicit registry and runner as
# the original model methods; checkout's lifecycle registration stays untouched.
_identity_commands.register(Clip, "clip", ("detach_audio",))
_identity_commands.register(Cut, "cut", ("duplicate",))
