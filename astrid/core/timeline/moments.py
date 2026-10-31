"""Moments: when something happens, said the way an editor says it.

A narrated film runs on its words, so a timeline stores *moments*, not seconds. The
seconds in the document (a clip's ``at`` and ``hold``) are a cache that resolving
recomputes, the way a spreadsheet keeps a formula and caches its value.

The grammar (one line, used by the sheet, the CLI, the API and the stored form)::

    "viral"                    the first word of the phrase starts
    after "Astrid"             the last word of the phrase ends
    "tool" in v20a #2          scope to a VO line, and pick the 2nd occurrence
    beat 2 after "Astrid"      the 2nd music beat after it (also: downbeat N after/before …)
    c22                        a cut starts (cut ids, not positions; c1 is c01)
    end of c30   c30.end       a cut ends (where the next cut starts)
    end                        the end of the clip's own cut (with no clip: the end of the film)
    +0.8s   -3f                an offset; alone, it is relative to the clip's own cut start
    "viral" +2f                any of the above, then an offset

Frame policy: a moment floors to its frame (the word's onset stays inside its frame);
a cut ``on`` the first word of a line opens at the take's in-point.

Pure: no runtime, no pack imports. ``resolve`` takes a small context object.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, replace
from typing import Any, Iterable, Protocol, Sequence

CUT_ID_RE = re.compile(r"^c\d+[a-z]?$")
_CUT_END_RE = re.compile(r"^(c\d+[a-z]?)\.end$")
FRAME_EPSILON = 0.02  # frames: times arrive through ms placements, so allow ~0.6 ms of rounding
_TOKEN_RE = re.compile(r'"[^"]*"|“[^”]*”|#\d+|[+-]\d+(?:\.\d+)?[fs]\b|[^\s"]+')
_OFFSET_RE = re.compile(r"^([+-])(\d+(?:\.\d+)?)([fs])$")


class MomentError(ValueError):
    """A moment that cannot be parsed or resolved; the message says what to write instead."""


@dataclass(frozen=True)
class Moment:
    kind: str                      # word | beat | downbeat | cut | cut_end | end | cut_start
    text: str = ""                 # the phrase (kind word)
    line: str | None = None        # scope: a VO line id
    n: int | None = None           # occurrence (#n) for words; the count for beats
    edge: str = "start"            # start | end (``after``)
    direction: str = "after"       # beats: after | before
    base: "Moment | None" = None   # beats: counted from this moment
    cut: str | None = None         # kind cut
    offset_s: float = 0.0
    offset_frames: int = 0

    def with_offset(self, *, seconds: float = 0.0, frames: int = 0) -> "Moment":
        return replace(self, offset_s=round(self.offset_s + seconds, 6), offset_frames=self.offset_frames + frames)

    def __str__(self) -> str:
        return format_moment(self)


class Word(Protocol):  # what resolve needs from a spoken word
    text: str
    segment: str
    index: int
    start: float
    end: float


class Context(Protocol):
    fps: float

    def words(self) -> Sequence[Word]: ...
    def beats(self, kind: str) -> Sequence[float]: ...       # timeline seconds, sorted
    def cut_start(self, cut_id: str) -> float: ...
    def cut_end(self, cut_id: str) -> float: ...
    def own_cut(self) -> tuple[float, float] | None: ...      # (start, end) of the clip's cut
    def line_in_point(self, line: str) -> float | None: ...


# ---------------------------------------------------------------- parse / format

def parse(text: Any) -> Moment:
    """Parse a moment. A bare word (``Astrid``) is read as a quoted one."""
    if isinstance(text, Moment):
        return text
    raw = str(text or "").strip()
    if not raw:
        raise MomentError("an empty moment; write e.g. \"viral\", after \"Astrid\", beat 2 after \"Astrid\", c22 or +0.8s")
    tokens = _TOKEN_RE.findall(raw)
    moment, rest = _parse_core(tokens, raw)
    moment = _parse_offsets(moment, rest, raw)
    return moment


def _parse_core(tokens: list[str], raw: str) -> tuple[Moment, list[str]]:
    if not tokens:
        raise MomentError(f"cannot read {raw!r}")
    head = tokens[0].lower()
    if _OFFSET_RE.match(tokens[0]):
        return Moment("cut_start"), tokens
    if head in ("beat", "downbeat"):
        if len(tokens) < 3 or not tokens[1].isdigit() or tokens[2].lower() not in ("after", "before"):
            raise MomentError(f"{raw!r}: write {head} N after \"word\" (or before)")
        base, rest = _parse_core(tokens[3:], raw)
        return Moment(head, n=int(tokens[1]), direction=tokens[2].lower(), base=base), rest
    if head == "after":
        inner, rest = _parse_core(tokens[1:], raw)
        if inner.kind != "word":
            raise MomentError(f"{raw!r}: after takes a word, e.g. after \"Astrid\"")
        return replace(inner, edge="end"), rest
    if head == "on":
        return _parse_core(tokens[1:], raw)
    if head == "end":
        if len(tokens) >= 3 and tokens[1].lower() == "of" and CUT_ID_RE.match(tokens[2].lower()):
            return Moment("cut_end", cut=tokens[2].lower()), tokens[3:]
        return Moment("end"), tokens[1:]
    if _CUT_END_RE.match(head):
        return Moment("cut_end", cut=_CUT_END_RE.match(head).group(1)), tokens[1:]
    if CUT_ID_RE.match(head):
        return Moment("cut", cut=head), tokens[1:]
    if tokens[0][0] in "\"“":
        text, rest = tokens[0][1:-1], tokens[1:]
    else:  # bare words up to a keyword or offset
        words, rest = [], list(tokens)
        while rest and not _OFFSET_RE.match(rest[0]) and rest[0].lower() != "in" and not rest[0].startswith("#"):
            words.append(rest.pop(0))
        text = " ".join(words)
    line = n = None
    while rest and (rest[0].lower() == "in" or rest[0].startswith("#")):
        if rest[0].lower() == "in":
            if len(rest) < 2:
                raise MomentError(f"{raw!r}: in needs a line id, e.g. \"tool\" in v20a")
            line, rest = rest[1], rest[2:]
        else:
            n, rest = int(rest[0][1:]), rest[1:]
    if not text.strip():
        raise MomentError(f"{raw!r}: which word? write it in quotes, e.g. \"viral\"")
    return Moment("word", text=text, line=line, n=n), rest


def _parse_offsets(moment: Moment, rest: list[str], raw: str) -> Moment:
    for token in rest:
        m = _OFFSET_RE.match(token)
        if not m:
            raise MomentError(f"{raw!r}: did not understand {token!r} (offsets look like +2f or -0.25s)")
        sign = -1 if m.group(1) == "-" else 1
        if m.group(3) == "f":
            moment = moment.with_offset(frames=sign * int(float(m.group(2))))
        else:
            moment = moment.with_offset(seconds=sign * float(m.group(2)))
    return moment


def format_moment(m: Moment) -> str:
    if m.kind == "word":
        core = f'"{m.text}"' + (f" in {m.line}" if m.line else "") + (f" #{m.n}" if m.n else "")
        core = ("after " if m.edge == "end" else "") + core
    elif m.kind in ("beat", "downbeat"):
        core = f"{m.kind} {m.n} {m.direction} {format_moment(replace(m.base, offset_s=0.0, offset_frames=0)) if m.base else ''}".strip()
    elif m.kind == "cut":
        core = str(m.cut)
    elif m.kind == "cut_end":
        core = f"end of {m.cut}"
    elif m.kind == "end":
        core = "end"
    else:
        core = ""
    return (core + " " + format_offset(m.offset_s, m.offset_frames)).strip()


def format_offset(seconds: float, frames: int = 0) -> str:
    out = []
    if frames:
        out.append(f"{frames:+d}f")
    if abs(seconds) > 1e-9:
        out.append(f"{'+' if seconds > 0 else '-'}{abs(seconds):g}s")
    return " ".join(out)


def offset_text(seconds: float, fps: float) -> str:
    """The plainest offset for a duration: ``+0.8s`` for tenths of a second, ``+2f`` for whole frames, else seconds."""
    if abs(seconds) < 1e-9:
        return ""
    frames = seconds * fps
    if abs(frames - round(frames)) < FRAME_EPSILON:
        whole = int(round(frames))
        tenth = fps / 10
        if abs(whole / tenth - round(whole / tenth)) < 1e-6:
            return format_offset(round(seconds, 1))
        return format_offset(0.0, whole)
    return format_offset(round(seconds, 3))


# ---------------------------------------------------------------- resolve

def _norm(text: str) -> str:
    return re.sub(r"[^\w']", "", str(text).lower())


def find_words(moment: Moment, words: Sequence[Word]) -> list[list[Word]]:
    """Every occurrence of the moment's phrase (consecutive words of one line), in time order."""
    want = [_norm(t) for t in moment.text.split() if _norm(t)]
    if not want:
        return []
    pool = [w for w in words if moment.line is None or w.segment == moment.line]
    hits = []
    for i in range(len(pool)):
        run = pool[i:i + len(want)]
        if len(run) == len(want) and all(_norm(w.text) == t for w, t in zip(run, want)) and len({w.segment for w in run}) == 1:
            hits.append(list(run))
    return hits


def resolve(moment: Moment | str, ctx: Context, *, in_point: bool = False) -> float:
    """Timeline seconds (not yet floored to a frame)."""
    m = parse(moment) if not isinstance(moment, Moment) else moment
    if m.kind == "word":
        hits = find_words(m, ctx.words())
        if not hits:
            raise MomentError(_missing(m, ctx.words()))
        if m.n is None and len(hits) > 1:
            where = "; ".join(f'"{m.text}" in {h[0].segment}' + (f" #{k}" if sum(1 for x in hits if x[0].segment == h[0].segment) > 1 else "")
                              + f" at {h[0].start:.2f} s" for k, h in enumerate(hits, 1))
            raise MomentError(f'"{m.text}" is spoken {len(hits)} times: {where}. Say which (add in <line> or #n).')
        if m.n is not None and not 1 <= m.n <= len(hits):
            raise MomentError(f'"{m.text}" is spoken {len(hits)} time(s){" in " + m.line if m.line else ""}; #{m.n} does not exist')
        run = hits[(m.n or 1) - 1]
        if in_point and m.edge == "start" and run[0].index == 0 and not m.offset_s and not m.offset_frames:
            t = ctx.line_in_point(run[0].segment)
            if t is not None:
                return t
        t = run[-1].end if m.edge == "end" else run[0].start
    elif m.kind in ("beat", "downbeat"):
        base = resolve(m.base, ctx) if m.base else 0.0
        beats = list(ctx.beats(m.kind))
        # a beat on the base's own frame is ON it, not after (or before) it: count from the next one
        near = 0.5 / float(ctx.fps or 30)
        pool = [b for b in beats if b > base + near] if m.direction == "after" else [b for b in reversed(beats) if b < base - near]
        if len(pool) < (m.n or 1):
            raise MomentError(f"there is no {m.kind} {m.n} {m.direction} {format_moment(m.base) if m.base else 'the start'} (the music has {len(beats)} {m.kind}s)")
        t = pool[(m.n or 1) - 1]
    elif m.kind == "cut":
        t = ctx.cut_start(str(m.cut))
    elif m.kind == "cut_end":
        t = ctx.cut_end(str(m.cut))
    elif m.kind in ("end", "cut_start"):
        own = ctx.own_cut()
        if own is None:
            raise MomentError(f"{format_moment(m)!r} is relative to the clip's cut, and this clip belongs to no cut")
        t = own[1] if m.kind == "end" else own[0]
    else:
        raise MomentError(f"unknown moment {m!r}")
    return t + m.offset_s + m.offset_frames / ctx.fps


def floor_frame(seconds: float, fps: float) -> float:
    return math.floor(seconds * fps + FRAME_EPSILON) / fps


def _missing(m: Moment, words: Sequence[Word]) -> str:
    import difflib

    scope = [w for w in words if m.line is None or w.segment == m.line]
    close = difflib.get_close_matches(_norm(m.text), sorted({_norm(w.text) for w in scope}), n=4)
    where = f" in line {m.line}" if m.line else ""
    return f'"{m.text}" is not spoken{where}' + (f"; did you mean {', '.join(close)}?" if close else "")


def word_moment(word: Word, words: Sequence[Word], *, edge: str = "start") -> Moment:
    """The shortest moment that names exactly this word: "viral", "tool" in v20a, or "tool" in v20a #2."""
    film = [w for w in words if _norm(w.text) == _norm(word.text)]
    if len(film) == 1:
        return Moment("word", text=word.text.strip(".,;:!?…\"“”"), edge=edge)
    in_line = [w for w in film if w.segment == word.segment]
    n = None if len(in_line) == 1 else 1 + [w.index for w in in_line].index(word.index)
    return Moment("word", text=word.text.strip(".,;:!?…\"“”"), line=word.segment, n=n, edge=edge)


def same(a: Iterable[Any], b: Iterable[Any]) -> bool:
    return list(a) == list(b)
