"""Selections: every collection of the timeline is one (API.md §4.1–§4.2).

A ``Selection`` is a ``list`` of clips, cuts, words or VO lines. Calling it filters it:
``tl.clips(chapter="02", role="label")`` is "every label in chapter 02". Filters are AND across keys;
a list value is OR within its key; a callable is a predicate; a single string is a selector
(``tl.clips("cut=c1 track=sprite")``). An unknown key or a value that names nothing raises ``SelectorError``.
A valid filter that matches nothing returns an empty Selection.
"""
from __future__ import annotations

import re
from typing import Any, Callable, Iterable

from astrid.core.timeline.astrid_ns import read_app
from astrid.sdk import timeline_intent as intent
from astrid.sdk.timeline._common import TimelineEditError, _norm

KINDS = ("clip", "cut", "word", "line")
NOUNS = {"clip": "clips", "cut": "cuts", "word": "words", "line": "lines"}
FILTER_KEYS = {
    "clip": ("element", "asset", "text", "track", "audio", "shot", "cut", "chapter", "role", "time"),
    "cut": ("chapter", "cut", "time", "has"),
    "word": ("text", "line", "time", "between"),
    "line": ("text", "time"),
}
# the vocabulary of ``role`` (API.md §4.2); an element may declare another, and a clip may carry one
ROLE_VOCABULARY = ("plate", "footage", "presenter", "sprite", "label", "title", "caption", "card", "chrome",
                   "transition", "voice", "music", "sfx")
_AUDIO_ROLE = {"vo": "voice", "music": "music", "sfx": "sfx"}
_EPS = 1e-6
_TOKEN = re.compile(r'(?:[^\s"]+|"[^"]*")+')


class SelectorError(TimelineEditError):
    """An unknown filter key, or a value that names nothing; the message lists the valid ones."""


# ----------------------------------------------------------------- the string form

def parse_selector(text: str) -> dict[str, Any]:
    """``'chapter=02 role=label text="the year"'`` → ``{"chapter": "02", "role": "label", "text": "the year"}``.

    ``cut=c06,c07`` → ``{"cut": ["c06", "c07"]}`` (a list is OR); ranges stay text (``"c06..c09a"``, ``"10..20"``).
    A token without ``=``, or with an empty key, is a SelectorError."""
    raw = str(text)
    if raw.count('"') % 2:
        raise SelectorError(f"unclosed quote in selector {raw!r}")
    out: dict[str, Any] = {}
    for token in _TOKEN.findall(raw):
        key, eq, value = token.partition("=")
        key = key.strip()
        if not eq or not key:
            raise SelectorError(f"{token!r} is not key=value in selector {raw!r}; "
                                f'write e.g. chapter=02 role=label text="the year"')
        if key in out:
            raise SelectorError(f"{key}= is given twice in {raw!r}; list the values instead ({key}=a,b)")
        if len(value) >= 2 and value.startswith('"') and value.endswith('"'):
            out[key] = value[1:-1]  # quoted: kept whole, no comma split
        elif '"' in value:
            raise SelectorError(f"quote the whole value of {key}= in selector {raw!r}")
        elif "," in value:
            parts = [p.strip() for p in value.split(",") if p.strip()]
            if not parts:
                raise SelectorError(f"{key}= has no value in selector {raw!r}")
            out[key] = parts
        elif value:
            out[key] = value
        else:
            raise SelectorError(f"{key}= has no value in selector {raw!r}")
    return out


# ----------------------------------------------------------------- the collection

class Selection(list):
    """A list of timeline items (``kind``: clip, cut, word or line) that filters itself when called."""

    def __init__(self, items: Iterable[Any] = (), *, kind: str, tl: Any = None):
        if kind not in KINDS:
            raise ValueError(f"a Selection's kind is one of {', '.join(KINDS)}, not {kind!r}")
        super().__init__(items)
        self.kind = kind
        self.tl = tl

    def __repr__(self) -> str:
        return f"<Selection {self.kind} ×{len(self)}>"

    def _new(self, items: Iterable[Any]) -> "Selection":
        return Selection(items, kind=self.kind, tl=self.tl)

    def __call__(self, *preds: Any, **filters: Any) -> "Selection":
        return self.where(*preds, **filters)

    def where(self, *preds: Any, **filters: Any) -> "Selection":
        """The items that pass every filter: callables, selector strings and ``key=value`` (AND across keys)."""
        tests: list[Callable[[Any], bool]] = []
        for pred in preds:
            if isinstance(pred, str):
                for key, value in parse_selector(pred).items():
                    tests.append(_test(self, key, value))
            elif callable(pred):
                tests.append(pred)
            else:
                raise TypeError(f"a filter is a selector string or a callable, not {type(pred).__name__}")
        for key, value in filters.items():
            if value is None or (isinstance(value, str) and not value.strip()):
                continue  # not given (today's tl.clips(element=None) and friends)
            tests.append(_test(self, key, value))
        return self._new(x for x in self if all(test(x) for test in tests))

    def __getitem__(self, key: Any) -> Any:
        if isinstance(key, slice):
            return self._new(list.__getitem__(self, key))
        if isinstance(key, str):
            wanted = _cut_key(self, key)
            hits = [x for x in self if _matches_key(self, x, wanted)]
            if len(hits) == 1:
                return hits[0]
            if not hits:
                raise TimelineEditError(f"no {NOUNS[self.kind][:-1]} matches {key!r}; {_listing(self)}")
            raise TimelineEditError(f"{key!r} matches {len(hits)} {NOUNS[self.kind]}; name one: {_listing(self._new(hits))}")
        return list.__getitem__(self, key)

    def one(self) -> Any:
        """The only item; refuses (listing what there is) when there are none or several."""
        if len(self) != 1:
            found = f": {_listing(self)}" if self else ""
            raise TimelineEditError(f"expected exactly one {NOUNS[self.kind][:-1]}, found {len(self)}{found}")
        return list.__getitem__(self, 0)

    def sorted(self, key: "str | Callable[[Any], Any]" = "start", reverse: bool = False) -> "Selection":
        """A new Selection in order of an attribute (``"start"``, ``"end"``, ``"id"`` …) or of a function."""
        if callable(key):
            fn = key
        else:
            name = str(key)
            fn = lambda x: _attr(self, x, name)  # noqa: E731
        return self._new(sorted(self, key=fn, reverse=reverse))


# ----------------------------------------------------------------- helpers

def _tl(sel: Selection) -> Any:
    tl = sel.tl
    if tl is None and len(sel):
        tl = getattr(list.__getitem__(sel, 0), "_tl", None)
    if tl is None:
        raise SelectorError("this selection has no timeline to look its filter up in")
    return tl


def _listing(sel: Selection, limit: int = 12) -> str:
    names = [_label(sel, x) for x in sel]
    shown = ", ".join(names[:limit]) or "none"
    return shown + (f" … {len(names) - limit} more" if len(names) > limit else "")


def _label(sel: Selection, x: Any) -> str:
    if sel.kind == "clip":
        return x.address
    if sel.kind == "cut":
        return x.id or f"cut {x.n}"
    if sel.kind == "line":
        return x.segment
    return f"{x.id} {x.text!r}"


def _cut_key(sel: Selection, key: str) -> str:
    """``c1`` and ``c01`` name the same cut."""
    if sel.kind == "cut" and sel.tl is not None:
        return sel.tl._cut_id(key) or key
    return key


def _matches_key(sel: Selection, x: Any, key: str) -> bool:
    if sel.kind == "clip":
        return x.address == key or x.id == key
    if sel.kind == "cut":
        return x.id == key
    if sel.kind == "line":
        return x.segment == key
    return x.id == key or _norm(x.text) == _norm(key)


def _attr(sel: Selection, x: Any, name: str) -> Any:
    if sel.kind == "line" and name in ("start", "end"):
        return _line_span(x)[0 if name == "start" else 1]
    return getattr(x, name)


def _line_span(line: Any) -> tuple[float, float]:
    clips = line.clips
    return min(c.start for c in clips), max(c.end for c in clips)


def _span(sel: Selection) -> Callable[[Any], tuple[float, float]]:
    if sel.kind == "line":
        return _line_span
    return lambda x: (x.start, x.end)


def _bool(value: Any, key: str) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in ("true", "false"):
        return text == "true"
    raise SelectorError(f"{key}= takes true or false, not {value!r}")


def _test(sel: Selection, key: str, value: Any) -> Callable[[Any], bool]:
    if key not in FILTER_KEYS[sel.kind]:
        raise SelectorError(f"no filter {key!r} for {NOUNS[sel.kind]}; the keys are: {', '.join(FILTER_KEYS[sel.kind])}")
    if isinstance(value, list):
        if not value:
            raise SelectorError(f"{key}= is an empty list")
        tests = [_single(sel, key, v) for v in value]
        return lambda x: any(test(x) for test in tests)
    return _single(sel, key, value)


def _single(sel: Selection, key: str, value: Any) -> Callable[[Any], bool]:
    return _BUILDERS[(sel.kind, key)](sel, value)


def _time_pred(sel: Selection, value: Any) -> Callable[[Any], bool]:
    """``"A"``: on screen (spoken) at A. ``"A..B"`` or ``(a, b)``: overlapping [A, B)."""
    tl, span = _tl(sel), _span(sel)
    if isinstance(value, str) and ".." in value:
        a, b = value.split("..", 1)
        return _overlap(tl.time(a), tl.time(b), span)
    if isinstance(value, tuple) and len(value) == 2:
        return _overlap(tl.time(value[0]), tl.time(value[1]), span)
    t = tl.time(value)
    return lambda x: span(x)[0] - _EPS <= t < span(x)[1] - _EPS


def _overlap(a: float, b: float, span: Callable[[Any], tuple[float, float]]) -> Callable[[Any], bool]:
    return lambda x: span(x)[0] < b - _EPS and span(x)[1] > a + _EPS


# ----------------------------------------------------------------- chapters

def _shot_name(tl: Any, shot_id: str) -> str:
    return str(((tl.bundle.get("shots", {}).get(shot_id) or {}).get("payload") or {}).get("name") or shot_id)


def _chapters(tl: Any) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """The chapter table, and each cut id's chapter index.

    With labels (``parent.config.chapters``): a cut's chapter is the last label at or before it, as the sheet
    does. Without labels, each shot is a chapter named by the shot. Consecutive equal names are ONE chapter."""
    groups = tl.cut_index.groups(tl)
    order = [g["id"] for g in groups]
    labels = {row.get("from"): row.get("name") for row in intent.chapters(tl.bundle)}
    names: dict[str, str] = {}
    if labels:
        last = None
        for cid in order:
            if labels.get(cid):
                last = labels[cid]
            if last:
                names[cid] = last
        keyed = [(cid, names.get(cid)) for cid in order]
    else:
        shot_of = {}
        for g in groups:
            first = g["picture"] or min(g["clips"], key=lambda c: c.start)
            shot_of[g["id"]] = first.shot_id
        keyed = [(sid, _shot_name(tl, sid)) for sid in tl._shot_ids()]
    runs: list[str] = []
    index: dict[str, int] = {}
    if labels:
        for cid, name in keyed:
            if name is None:
                continue
            if not runs or runs[-1] != name:
                runs.append(name)
            index[cid] = len(runs) - 1
    else:
        run_of_shot: dict[str, int] = {}
        for sid, name in keyed:
            if not runs or runs[-1] != name:
                runs.append(name)
            run_of_shot[sid] = len(runs) - 1
        for cid in order:
            if shot_of[cid] in run_of_shot:
                index[cid] = run_of_shot[shot_of[cid]]
    table = [{"index": k, "number": k + 1, "name": name, "id": (name.split() or [name])[0]}
             for k, name in enumerate(runs)]
    return table, index


def _resolve_chapter(table: list[dict[str, Any]], value: Any) -> int:
    text = str(value).strip()
    listing = " · ".join(f"{ch['id']} ({ch['name']})" for ch in table) or "none (this timeline has no chapters)"
    for ch in table:
        if ch["id"] == text:
            return ch["index"]
    if text.isdigit():
        for ch in table:
            if ch["id"].isdigit() and int(ch["id"]) == int(text):
                return ch["index"]
        if 1 <= int(text) <= len(table):
            return int(text) - 1
    for ch in table:
        if ch["name"].casefold() == text.casefold():
            return ch["index"]
    raise SelectorError(f"no chapter {value!r}; chapters are: {listing}")


def _chapter_pred(sel: Selection, value: Any, cut_and_time: Callable[[Any], tuple[str | None, float]]) -> Callable[[Any], bool]:
    tl = _tl(sel)
    table, index = _chapters(tl)
    want = _resolve_chapter(table, value)

    def chapter_of(x: Any) -> int | None:
        cid, t = cut_and_time(x)
        found = index.get(cid) if cid else None
        if found is None:
            found = index.get(tl._cut_on_screen(t))
        return found

    return lambda x: chapter_of(x) == want


# ----------------------------------------------------------------- cuts

def _cut_index(tl: Any, text: str) -> int:
    cid = tl._cut_id(text)
    ids = [g["id"] for g in tl.cut_index.groups(tl)]
    if cid is None or cid not in ids:
        raise SelectorError(f"no cut {text!r}; cuts are {', '.join(ids) or 'not named in this timeline'}")
    return ids.index(cid)


def _cut_targets(sel: Selection, value: Any) -> list[Any]:
    """The Cut objects a ``cut=`` value names: one cut (``c08``, 17, ``@62.5``), a list or ``c06,c07`` (union),
    or ``c06..c09a`` (every named cut from c06 to c09a in cut order)."""
    tl = _tl(sel)
    try:
        if isinstance(value, str) and ".." in value:
            a, b = (part.strip() for part in value.split("..", 1))
            lo, hi = sorted((_cut_index(tl, a), _cut_index(tl, b)))
            ids = [g["id"] for g in tl.cut_index.groups(tl)][lo:hi + 1]
            return [tl.cut(cid) for cid in ids]
        if isinstance(value, str) and "," in value:
            value = [p.strip() for p in value.split(",") if p.strip()]
        items = value if isinstance(value, (list, tuple)) else [value]
        return [tl.cut(v) for v in items]
    except SelectorError:
        raise
    except TimelineEditError as exc:
        raise SelectorError(str(exc)) from None


def _role_of(tl: Any, clip: Any) -> str | None:
    """The clip's role: its own ``app.role``, else the element's declared role, else (audio) its track's role."""
    from astrid.sdk.timeline_address import element_schema

    app = read_app(clip.data)
    own = app.get("role")
    if own:
        return str(own)
    declared = element_schema(clip.element).get("role") if clip.element else None
    if declared:
        return str(declared)
    if clip.is_audio:
        return _AUDIO_ROLE.get(clip.track)
    return None


# ----------------------------------------------------------------- one builder per (kind, key)

def _clip_element(sel: Selection, value: Any) -> Callable[[Any], bool]:
    want = str(value)
    names = {want} if want.startswith("am-") else {want, f"am-{want}"}  # the sheet's short form: type = am-type
    return lambda c: c.element in names


def _clip_asset(sel: Selection, value: Any) -> Callable[[Any], bool]:
    want = str(value).lower()
    return lambda c: (c.asset or "").lower() == want


def _clip_text(sel: Selection, value: Any) -> Callable[[Any], bool]:
    if isinstance(value, bool) or str(value).strip().lower() in ("true", "false"):
        want = _bool(value, "text")
        return lambda c: bool(c.text) == want
    text = str(value)
    if len(text) >= 2 and text.startswith("/") and text.endswith("/"):
        try:
            pattern = re.compile(text[1:-1], re.IGNORECASE)
        except re.error as exc:
            raise SelectorError(f"text={text}: not a regular expression ({exc})") from None
        return lambda c: bool(pattern.search(c.text or ""))
    needle = _norm(text)
    return lambda c: needle in _norm(c.text or "")


def _clip_track(sel: Selection, value: Any) -> Callable[[Any], bool]:
    want = str(value)
    return lambda c: c.track == want


def _clip_audio(sel: Selection, value: Any) -> Callable[[Any], bool]:
    want = _bool(value, "audio")
    return lambda c: c.is_audio == want


def _clip_shot(sel: Selection, value: Any) -> Callable[[Any], bool]:
    tl = _tl(sel)
    try:
        shot_id = tl._shot_id(value)
    except TimelineEditError as exc:
        raise SelectorError(str(exc)) from None
    return lambda c: c.shot_id == shot_id


def _clip_cut(sel: Selection, value: Any) -> Callable[[Any], bool]:
    members = {id(c.data) for cut in _cut_targets(sel, value) for c in cut.clips}
    return lambda c: id(c.data) in members


def _clip_chapter(sel: Selection, value: Any) -> Callable[[Any], bool]:
    return _chapter_pred(sel, value, lambda c: (c.cut_id, c.start))


def _clip_role(sel: Selection, value: Any) -> Callable[[Any], bool]:
    tl = _tl(sel)
    used = {_role_of(tl, c) for c in tl.clips()} - {None}
    valid = set(ROLE_VOCABULARY) | used
    if value not in valid:
        raise SelectorError(f"no role {value!r}; the roles are: {', '.join(sorted(valid))}")
    return lambda c: _role_of(tl, c) == value


def _cut_chapter(sel: Selection, value: Any) -> Callable[[Any], bool]:
    return _chapter_pred(sel, value, lambda cut: (cut.id, cut.start))


def _cut_cut(sel: Selection, value: Any) -> Callable[[Any], bool]:
    numbers = {cut.n for cut in _cut_targets(sel, value)}
    return lambda cut: cut.n in numbers


def _cut_has(sel: Selection, value: Any) -> Callable[[Any], bool]:
    if not isinstance(value, str):
        raise SelectorError('has= takes a selector string, e.g. has="role=label"')
    matched = _tl(sel).clips.where(**parse_selector(value))
    members = {id(c.data) for c in matched}
    return lambda cut: any(id(c.data) in members for c in cut.clips)


def _word_text(sel: Selection, value: Any) -> Callable[[Any], bool]:
    """The word or phrase, as ``tl.word`` matches it: the words that START it (normalized)."""
    parts = [_norm(p) for p in str(value).split() if _norm(p)]
    if not parts:
        return lambda w: False
    seq = list(_tl(sel).words)
    starts = set()
    for i, w in enumerate(seq):
        if _norm(w.text) == parts[0] and all(i + k < len(seq) and _norm(seq[i + k].text) == p
                                              for k, p in enumerate(parts[1:], 1)):
            starts.add(w.id)
    return lambda w: w.id in starts


def _word_line(sel: Selection, value: Any) -> Callable[[Any], bool]:
    want = str(value)
    return lambda w: w.segment == want


def _word_between(sel: Selection, value: Any) -> Callable[[Any], bool]:
    tl = _tl(sel)
    if isinstance(value, str) and ".." in value:
        a, b = value.split("..", 1)
        lo, hi = tl.time(a), tl.time(b)
    elif isinstance(value, (tuple, list)) and len(value) == 2:
        lo, hi = tl.time(value[0]), tl.time(value[1])
    else:
        raise SelectorError(f"between= takes (a, b) or 'a..b', not {value!r}")
    return lambda w: lo - _EPS <= w.start < hi


def _line_text(sel: Selection, value: Any) -> Callable[[Any], bool]:
    needle = _norm(value)
    return lambda ln: needle in _norm(ln.text or "")


def _time(sel: Selection, value: Any) -> Callable[[Any], bool]:
    return _time_pred(sel, value)


_BUILDERS: dict[tuple[str, str], Callable[[Selection, Any], Callable[[Any], bool]]] = {
    ("clip", "element"): _clip_element,
    ("clip", "asset"): _clip_asset,
    ("clip", "text"): _clip_text,
    ("clip", "track"): _clip_track,
    ("clip", "audio"): _clip_audio,
    ("clip", "shot"): _clip_shot,
    ("clip", "cut"): _clip_cut,
    ("clip", "chapter"): _clip_chapter,
    ("clip", "role"): _clip_role,
    ("clip", "time"): _time,
    ("cut", "chapter"): _cut_chapter,
    ("cut", "cut"): _cut_cut,
    ("cut", "time"): _time,
    ("cut", "has"): _cut_has,
    ("word", "text"): _word_text,
    ("word", "line"): _word_line,
    ("word", "time"): _time,
    ("word", "between"): _word_between,
    ("line", "text"): _line_text,
    ("line", "time"): _time,
}
