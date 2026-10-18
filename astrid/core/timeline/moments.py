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
    clip-start "occurrence:opening/clip:title"  that exact authored clip starts
    clip-end "parent/clip:bed"  that exact authored clip ends
    source-word "doc" "lineage" "revision" "occurrence:opening/clip:vo" "digest" "id"
                               that exact source word in that frozen occurrence
    end                        the end of the clip's own cut (with no clip: the end of the film)
    +0.8s   -3f                an offset; alone, it is relative to the clip's own cut start
    "viral" +2f                any of the above, then an offset

Frame policy: a moment floors to its frame (the word's onset stays inside its frame);
a cut ``on`` the first word of a line opens at the take's in-point.

Pure: no runtime, no pack imports. ``resolve`` takes a small context object.
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, replace
from typing import Any, Iterable, Protocol, Sequence

CUT_ID_RE = re.compile(r"^c\d+[a-z]?$")
_CUT_END_RE = re.compile(r"^(c\d+[a-z]?)\.end$")
FRAME_EPSILON = 0.02  # frames: times arrive through ms placements, so allow ~0.6 ms of rounding
_TOKEN_RE = re.compile(r'"(?:\\.|[^"\\])*"|“[^”]*”|#\d+|[+-]\d+(?:\.\d+)?[fs]\b|[^\s"]+')
_OFFSET_RE = re.compile(r"^([+-])(\d+(?:\.\d+)?)([fs])$")


class MomentError(ValueError):
    """A moment that cannot be parsed or resolved; the message says what to write instead."""


@dataclass(frozen=True)
class Moment:
    kind: str                      # word | beat | downbeat | cut | cut_end | end | cut_start | clip_start | clip_end
    text: str = ""                 # the phrase (kind word)
    line: str | None = None        # scope: a VO line id
    n: int | None = None           # occurrence (#n) for words; the count for beats
    edge: str = "start"            # start | end (``after``)
    direction: str = "after"       # beats: after | before
    base: "Moment | None" = None   # beats: counted from this moment
    cut: str | None = None         # kind cut
    offset_s: float = 0.0
    offset_frames: int = 0
    clip: str | None = None        # exact authored ScopedId, kind clip_start / clip_end

    source: tuple[str, str, str, str, str] | None = None  # document, lineage, revision, exact clip ScopedId, snapshot digest
    word_id: str | None = None  # opaque source identity, independent of spelling

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
    def clip_edge(self, scoped_id: str, edge: str) -> float: ...
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
    # Reserved clip syntax is intentionally stricter than the forgiving speech grammar.
    if re.match(r"^(?:on\s+)?clip-(?:start|end)\b", raw, re.I):
        match = re.fullmatch(r'(?:on\s+)?(clip-start|clip-end)\s+"(occurrence:[^"/\s]+/clip:[^"/\s]+|parent/clip:[^"/\s]+)"((?:\s+[+-]\d+(?:\.\d+)?[fs])*)', raw, re.I)
        if not match:
            raise MomentError('clip-start/clip-end require an exact quoted clip ScopedId followed only by offsets')
        return _parse_offsets(Moment(match[1].lower().replace('-', '_'), clip=match[2]), match[3].split(), raw)
    tokens = _TOKEN_RE.findall(raw)
    if any(t.lower() in {"clip-start", "clip-end", "source-word"} for t in tokens):
        # The permissive speech tokenizer skips stray quote characters. Reserved
        # forms must account for every non-whitespace character, also in beat bases.
        tail = 0
        for token in _TOKEN_RE.finditer(raw):
            if raw[tail:token.start()].strip():
                raise MomentError("Malformed reserved clip anchor syntax")
            tail = token.end()
        if raw[tail:].strip():
            raise MomentError("Malformed reserved clip anchor syntax")
    moment, rest = _parse_core(tokens, raw)
    moment = _parse_offsets(moment, rest, raw)
    return moment


def _parse_core(tokens: list[str], raw: str) -> tuple[Moment, list[str]]:
    if not tokens:
        raise MomentError(f"cannot read {raw!r}")
    head = tokens[0].lower()
    if _OFFSET_RE.match(tokens[0]):
        return Moment("cut_start"), tokens
    if head == "source-word":
        try:
            values = tuple(json.loads(token) for token in tokens[1:7])
        except (ValueError, TypeError):
            raise MomentError("source-word requires six exact quoted identities") from None
        if len(values) != 6 or any(not isinstance(v, str) or not v for v in values):
            raise MomentError("source-word requires document, lineage, revision, clip, snapshot and word identities")
        return Moment("word", source=values[:5], word_id=values[5]), tokens[7:]
    if head in ("clip-start", "clip-end"):
        # Also used for a beat's nested base; run the same strict parser.
        if len(tokens) < 2:
            raise MomentError('clip anchor requires a quoted ScopedId')
        return parse(" ".join(tokens[:2])), tokens[2:]
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
    if m.kind == "word" and m.source is not None:
        core = "source-word " + " ".join(json.dumps(v, ensure_ascii=False) for v in (*m.source, m.word_id))
        core = ("after " if m.edge == "end" else "") + core
    elif m.kind == "word":
        core = f'"{m.text}"' + (f" in {m.line}" if m.line else "") + (f" #{m.n}" if m.n else "")
        core = ("after " if m.edge == "end" else "") + core
    elif m.kind in ("beat", "downbeat"):
        core = f"{m.kind} {m.n} {m.direction} {format_moment(replace(m.base, offset_s=0.0, offset_frames=0)) if m.base else ''}".strip()
    elif m.kind in ("clip_start", "clip_end"):
        core = f'clip-{m.kind.split("_")[1]} "{m.clip}"'
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


def bound_word(moment: Moment) -> Moment | None:
    """Identity-bearing word at the base of a TimeRef, if present."""
    while moment.base is not None:
        moment = moment.base
    return moment if moment.source is not None else None


def find_words(moment: Moment, words: Sequence[Word], *, tokenizer=None) -> list[list[Word]]:
    """Every consecutive same-line match; optional whole-word tokenizer for copy consumers.

    The default retains TimeRef's historical normalization exactly.
    """
    if moment.source is not None:
        return [[w] for w in words if getattr(w, "source", None) == moment.source
                and getattr(w, "id", None) == moment.word_id]
    if tokenizer is None:
        want = [_norm(t) for t in moment.text.split() if _norm(t)]
        pool = [w for w in words if moment.line is None or w.segment == moment.line]
        return [list(run) for i in range(len(pool))
                if (run := pool[i:i + len(want)]) and len(run) == len(want)
                and all(_norm(w.text) == t for w, t in zip(run, want))
                and len({w.segment for w in run}) == 1] if want else []
    want = tuple(tokenizer(moment.text))
    if not want:
        return []
    pool = [w for w in words if moment.line is None or w.segment == moment.line]
    hits = []
    for i, word in enumerate(pool):
        if not tokenizer(word.text):
            continue
        got = ()
        for j in range(i, len(pool)):
            if pool[j].segment != word.segment:
                break
            got += tuple(tokenizer(pool[j].text))
            if got == want:
                hits.append(list(pool[i:j + 1]))
                break
            if len(got) >= len(want) or got != want[:len(got)]:
                break
    return hits


def resolve(moment: Moment | str, ctx: Context, *, in_point: bool = False) -> float:
    """Timeline seconds (not yet floored to a frame)."""
    m = parse(moment) if not isinstance(moment, Moment) else moment
    if m.kind == "word":
        words = ctx.words_for(m) if hasattr(ctx, "words_for") else ctx.words()
        hits = find_words(m, words)
        if not hits:
            raise MomentError(_missing(m, words))
        if m.n is None and len(hits) > 1:
            seen, choices = {}, []
            for h in hits:
                line = h[0].segment
                seen[line] = seen.get(line, 0) + 1
                choice = f'"{m.text}" in {line}'
                if sum(1 for x in hits if x[0].segment == line) > 1:
                    choice += f" #{seen[line]}"
                choices.append(choice + f" at {h[0].start:.2f} s")
            where = "; ".join(choices)
            raise MomentError(f'"{m.text}" is spoken {len(hits)} times: {where}. Say which (add in <line> or #n).')
        if m.n is not None and not 1 <= m.n <= len(hits):
            raise MomentError(f'"{m.text}" is spoken {len(hits)} time(s){" in " + m.line if m.line else ""}; #{m.n} does not exist')
        run = hits[(m.n or 1) - 1]
        if m.source is None and in_point and m.edge == "start" and run[0].index == 0 and not m.offset_s and not m.offset_frames:
            t = ctx.line_in_point(run[0].segment)
            if t is not None:
                return t
        t = run[-1].end if m.edge == "end" else run[0].start
    elif m.kind in ("clip_start", "clip_end"):
        t = ctx.clip_edge(m.clip, m.kind.split("_")[1])
    elif m.kind in ("beat", "downbeat"):
        base = resolve(m.base, ctx) if m.base else 0.0
        # A checkout can select the authored bed covering the resolved base.
        # Standalone contexts already supply a single grid.
        scoped = getattr(ctx, "beats_at", None)
        beats = list(scoped(m.kind, base) if scoped is not None else ctx.beats(m.kind))
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

    if m.source is not None:
        return f"ORPHAN: source word {m.word_id!r} is unavailable in {m.source!r}"
    scope = [w for w in words if m.line is None or w.segment == m.line]
    close = difflib.get_close_matches(_norm(m.text), sorted({_norm(w.text) for w in scope}), n=4)
    where = f" in line {m.line}" if m.line else ""
    return f'"{m.text}" is not spoken{where}' + (f"; did you mean {', '.join(close)}?" if close else "")


def word_moment(word: Word, words: Sequence[Word], *, edge: str = "start") -> Moment:
    """The shortest moment that names exactly this word: "viral", "tool" in v20a, or "tool" in v20a #2."""
    if getattr(word, "source", None) is not None:
        return Moment("word", source=word.source, word_id=word.id, edge=edge)
    film = [w for w in words if _norm(w.text) == _norm(word.text)]
    if len(film) == 1:
        return Moment("word", text=word.text.strip(".,;:!?…\"“”"), edge=edge)
    in_line = [w for w in film if w.segment == word.segment]
    n = None if len(in_line) == 1 else 1 + [w.index for w in in_line].index(word.index)
    return Moment("word", text=word.text.strip(".,;:!?…\"“”"), line=word.segment, n=n, edge=edge)


def same(a: Iterable[Any], b: Iterable[Any]) -> bool:
    return list(a) == list(b)
