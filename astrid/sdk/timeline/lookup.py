"""Checkout: clocks, looking things up (cuts, clips, words), and reading the document's shots and cut groups."""
from __future__ import annotations

import difflib
import re
from typing import Any, Iterable, Mapping, Sequence

from astrid.core.timeline import moments as mo
from astrid.sdk import timeline_intent as intent
from astrid.core.timeline.cuts import (
    occurrences_from_bundle,
    picture_cuts,
)

from astrid.sdk.timeline._common import (AUDIO_KINDS, TIME_RE, TimelineEditError, WORD_ID_RE, _norm, _num, _r)
from astrid.sdk.timeline.doc import (_read_words, _row_start)
from astrid.sdk.timeline.model import (Clip, Cut, StepSequence, Voice, Word)
from astrid.sdk.timeline.selection import Selection


class _LookupMixin:
    """Checkout: clocks, looking things up (cuts, clips, words), and reading the document's shots and cut groups."""

    # ---- clocks -----------------------------------------------------------
    def quantize(self, seconds: float) -> float:
        return _r(round(float(seconds) * self.fps) / self.fps)

    def time(self, when: Any) -> float:
        """Timeline seconds of anything you can point at, in the same grammar as ``--on`` and ``--at``:
        a number, ``"1:02"``/``"62.5s"``/``"@62.5"``, a moment (``'"It" in s02'``, ``after "Astrid"``,
        ``beat 2 after "Astrid"``, ``c04``, ``c41 +1.8s``, ``end of c30``/``c30.end``, ``end`` = the film's end),
        an address (``c41.mink``: where it starts), a word id (``n20b:20``), a Word or a Clip.
        Moments floor to their frame, as a clip placed on them would start."""
        if isinstance(when, bool):
            raise TimelineEditError("a time cannot be a boolean")
        if isinstance(when, (int, float)):
            return float(when)
        if isinstance(when, Word):
            return when.start
        if isinstance(when, Clip):
            return when.start
        if isinstance(when, mo.Moment):
            when = mo.format_moment(when)
        text = str(when).strip()
        match = TIME_RE.match(text)
        if match:
            minutes = int(match.group(1) or 0)
            return minutes * 60 + float(match.group(2))
        if WORD_ID_RE.match(text):
            return self.word(text).start
        from astrid.sdk import timeline_address as ta

        try:
            target = ta.resolve(self, text, prefer="time")
        except ta.AddressError as exc:
            raise TimelineEditError(str(exc)) from None
        if target.kind == "range":
            raise TimelineEditError(f"{text!r} is a range ({target.start:.3f}–{target.end:.3f} s); a time is one moment")
        return float(target.start)

    def frame(self, seconds: Any) -> int:
        return int(round(self.time(seconds) * self.fps))

    def to_shot_seconds(self, shot_id: str, seconds: float) -> float:
        return _r(float(seconds) - self._shot_start(shot_id))

    # ---- look things up ---------------------------------------------------
    @property
    def duration(self) -> float:
        return _r(max((c.end for c in self.cuts), default=0.0))

    @property
    def cuts(self) -> Selection:
        """Every cut, in order (a Selection: ``tl.cuts(chapter="02")``, ``tl.cuts["c08"]``)."""
        return Selection((Cut(self, raw) for raw in picture_cuts(occurrences_from_bundle(self.bundle), fps=self.fps)),
                         kind="cut", tl=self)

    def cut(self, selector: Any) -> Cut:
        """A cut by number (1-based, as show prints), ``"@62.5"``/``"1:02"`` (the cut on screen then), or picture clip id."""
        cuts = self.cuts
        if isinstance(selector, int) or (isinstance(selector, str) and selector.strip().isdigit()):
            n = int(selector)
            if not 1 <= n <= len(cuts):
                raise TimelineEditError(f"cut {n} does not exist; this timeline has cuts 1–{len(cuts)}")
            return cuts[n - 1]
        text = str(selector).strip()
        if mo.CUT_ID_RE.match(text.lower()):
            text = self._cut_id(text) or text  # c1 is c01
            group = next((g for g in self._cut_groups() if g["id"] == text.lower()), None)
            if group is None:
                ids = [g["id"] for g in self._cut_groups()]
                raise TimelineEditError(f"no cut {text}; cuts are {', '.join(ids) or 'not named in this timeline'}")
            if group["picture"] is not None:
                for cut in cuts:
                    if any(step.data is group["picture"].data for step in cut.steps):
                        return cut
        if TIME_RE.match(text) or isinstance(selector, float):
            t = self.time(selector if not isinstance(selector, float) else selector)
            for cut in cuts:
                if cut.start - 1e-6 <= t < cut.end - 1e-6:
                    return cut
            raise TimelineEditError(f"no cut is on screen at {t:.3f} s (the timeline is {self.duration:.3f} s)")
        for cut in cuts:
            if any(c.id == text or c.id.startswith(text) for c in cut.steps):
                return cut
        raise TimelineEditError(f"no cut matches {selector!r}; pass 1–{len(cuts)}, a time like '1:02' or a picture clip id")

    @property
    def clips(self) -> Selection:
        """Every clip, in timeline order (a Selection). Call it to filter: ``tl.clips(role="label")``,
        ``tl.clips(chapter="02", element="am-type")``, ``tl.clips("cut=c1 track=sprite")``. ``tl.clips()`` is all."""
        pool = [Clip(self, sid, c) for sid in self._shot_ids() for c in self._internal(sid)["clips"]]
        return Selection(sorted(pool, key=lambda c: (c.start, c.id)), kind="clip", tl=self)

    def clip(self, query: str, *, cut: Any = None, near: Any = None) -> Clip:
        """One clip by id (or prefix), asset key, element id or on-screen text.

        If several match, ``near=`` (a word or time) picks the one on screen closest to it,
        ``cut=`` restricts to one cut; otherwise the error lists the choices.
        """
        if isinstance(query, str) and cut is None and near is None:
            from astrid.sdk.timeline.music_beds import collection, locate
            managed = collection(self).get(query)
            if isinstance(managed, dict) and isinstance(managed.get('source'), dict):
                sid, _ = locate(self, query, managed['source'])
                return Clip(self, sid, managed['source'])
            # one address, everywhere: c41.mink, a carried alias (c30.cover), a layer name, an asset key, …
            from astrid.sdk.timeline_address import candidates

            found = [t for t in candidates(self, query, prefer="thing") if t.kind == "clip"]
            if len(found) == 1:
                return found[0].clip
            if len(found) > 1:
                listing = "\n  ".join(str(t) for t in found[:12]) + (f"\n  … {len(found) - 12} more" if len(found) > 12 else "")
                raise TimelineEditError(f"{query!r} matches {len(found)} clips; name one:\n  {listing}")
        pool = self.cut(cut).clips if cut is not None else self.clips()
        if near is not None:
            point = self.time(near)
            try:
                return self._pick(pool, query, where="the timeline")
            except TimelineEditError:
                matches = [c for c in pool if self._matches(c, query)]
                if not matches:
                    raise
                return min(matches, key=lambda c: (0 if c.start - 1e-6 <= point < c.end else 1, abs(c.start - point)))
        return self._pick(pool, query, where=f"cut {cut}" if cut is not None else "the timeline")

    def _matches(self, c: Clip, query: str) -> bool:
        text = str(query).strip()
        return (c.id == text or c.address == text or c.layer_name == text or c.id.startswith(text) or (c.asset or "").lower() == text.lower()
                or c.element == text or bool(c.text and _norm(text) in _norm(c.text)))

    @property
    def words(self) -> Selection:
        """Every spoken word, timeline seconds, with a stable id ``<segment>:<index>`` (a Selection:
        ``tl.words(between=(a, b))``, ``tl.words(text="viral")``)."""
        return Selection(self._word_list(), kind="word", tl=self)

    def _word_list(self) -> list[Word]:
        found: list[tuple[float, float, str, str, str, int, str]] = []
        for sid in self._shot_ids():
            start = self._shot_start(sid)
            for clip in self._internal(sid)["clips"]:
                if not self._is_audio(sid, clip):
                    continue
                segment = intent.line(clip) or str(clip.get("id") or "")
                for i, entry in enumerate(_read_words({"words": intent.words(clip)}, relative=True)):
                    s, e, t = entry
                    at = start + _num(clip.get("at"))
                    found.append((at + s, at + e, t, segment, str(clip.get("id")), i, sid))
        found.sort(key=lambda row: (row[0], row[3], row[5]))
        counters: dict[str, int] = {}
        words = []
        for s, e, t, segment, clip_id, _i, sid in found:
            k = counters.get(segment, 0)
            counters[segment] = k + 1
            words.append(Word(f"{segment}:{k}", t, _r(s), _r(e), segment, k, clip_id, sid))
        return words

    def word(self, query: str, *, n: int | None = None, after: Any = None, near: Any = None) -> Word:
        """A word by text (``"viral"``, a phrase ``"Hugging Face"``) or id (``"n20b:20"``).

        If the text is spoken more than once, pass ``n=`` (1-based), ``after=`` or ``near=``
        (times or words); the error lists every occurrence with its id and time.
        """
        words = self.words()
        query = str(query).strip()
        if WORD_ID_RE.match(query):
            for w in words:
                if w.id == query:
                    return w
        parts = [_norm(p) for p in query.split() if _norm(p)]
        if not parts:
            raise TimelineEditError("name a word, e.g. tl.word('viral')")
        hits = [w for i, w in enumerate(words)
                if _norm(w.text) == parts[0]
                and all(i + k < len(words) and _norm(words[i + k].text) == p for k, p in enumerate(parts[1:], 1))]
        if after is not None:
            limit = self.time(after)
            hits = [w for w in hits if w.start >= limit - 1e-6]
        if near is not None:
            point = self.time(near)
            hits.sort(key=lambda w: abs(w.start - point))
            hits = hits[:1]
        if not hits:
            vocab = sorted({w.text for w in words})
            close = difflib.get_close_matches(query, vocab, n=5)
            raise TimelineEditError(f"nobody says {query!r} in this timeline" + (f"; did you mean {', '.join(map(repr, close))}?" if close else ""))
        if n is not None:
            if not 1 <= n <= len(hits):
                raise TimelineEditError(f"{query!r} is spoken {len(hits)} time(s); n must be 1–{len(hits)}")
            return hits[n - 1]
        if len(hits) > 1:
            listing = " · ".join(f"n={i} {w.start:.2f} s [{w.id}] cut {self._cut_at(w.start)}" for i, w in enumerate(hits, 1))
            raise TimelineEditError(f"{query!r} is spoken {len(hits)} times: {listing}. Pass n=, after= or the id.")
        return hits[0]

    def voice(self, segment: str) -> Voice:
        return Voice(self, segment)

    def at(self, when: Any) -> dict[str, Any]:
        """What is on screen and being said at a time: the cut, its clips there, and the words around."""
        t = self.time(when)
        cut = self.cut(f"@{t}")
        showing = [c for c in cut.clips if c.start - 1e-6 <= t < c.end - 1e-6]
        said = [w for w in self.words() if w.end >= t - 1.0 and w.start <= t + 1.0]
        speaking = next((w for w in self.words() if w.start - 1e-6 <= t <= w.end + 1e-6), None)
        return {"time": t, "cut": cut, "showing": showing, "speaking": speaking, "words_around": said}

    def sequence(self, address: Any) -> "StepSequence":
        """The sequence a step belongs to (``tl.sequence("c17.rt-01")``), or by its id (``"c17-seq0"``)."""
        text = str(address)
        if any((intent.sequence(c.data) or ("",))[0] == text for c in self.clips()):
            return StepSequence(self, text)
        clip = address if isinstance(address, Clip) else self.clip(text)
        tag = intent.sequence(clip.data)
        if tag is None:
            raise TimelineEditError(f"{clip.address} is not a step of a sequence")
        return StepSequence(self, tag[0])

    def find(self, query: str | None = None, *, text: str | None = None, asset: str | None = None) -> list[Any]:
        """What you hear or see → addresses and times (``timelines find``): ``tl.find("the conclusion")``."""
        from astrid.sdk.timeline_address import find

        return find(self, query, text=text, asset=asset)

    def cut_count(self) -> str:
        """One way to count cuts everywhere: the named cuts (what you address), and the pictures if a
        sequence (a time-lapse) shows several pictures in one cut."""
        named, cuts = len(self._cut_groups()), self.cuts
        pictures = len(cuts)
        if not named or pictures == named:
            return f"{named} cuts" if named else f"{pictures} cuts"
        ids = [intent.cut_of(c.picture.data) if c.picture else None for c in cuts]
        unnamed = sum(1 for i in ids if not i)
        steps = pictures - unnamed - len({i for i in ids if i})
        why = " · ".join(x for x in (f"{steps} more from sequence steps" if steps else "",
                                     f"{unnamed} without a cut id" if unnamed else "") if x)
        return f"{named} cuts ({pictures} pictures" + (f": {why})" if why else ")")

    def _cut_on_screen(self, t: float) -> str | None:
        """The cut id on screen at ``t`` (None when the timeline names no cuts)."""
        spans = sorted(((lo, hi if hi is not None else self.duration, cid) for cid, (lo, hi) in self._cut_spans().items()))
        return next((cid for lo, hi, cid in reversed(spans) if lo - 1e-6 <= t < hi - 1e-6), None)

    def _cut_clips(self, cut_id: str) -> list[Clip]:
        """Every clip of a cut, orphans included (an orphaned cut is not in the cut list until re-homed)."""
        return [c for c in self.clips() if intent.cut_of(c.data) == cut_id]

    def cut_picture(self, cut_id: str) -> Clip | None:
        """A cut's picture (its first bed clip), for live and orphaned cuts alike."""
        beds = sorted((c for c in self._cut_clips(cut_id) if c.track == "plate" and not c.is_audio), key=lambda c: (c.start, c.id))
        return beds[0] if beds else None

    def _cut_id(self, text: str) -> str | None:
        """The cut id as the sheet prints it: ``c1`` and ``c001`` are ``c01``; None when there is no such cut."""
        m = re.fullmatch(r"c0*(\d+)([a-z]?)", str(text).strip().lower())
        if not m:
            return None
        ids = [g["id"] for g in self._cut_groups()] + [cid for cid in {intent.cut_of(c.data) for c in self.clips()} if cid]
        if str(text).strip().lower() in ids:
            return str(text).strip().lower()
        return next((cid for cid in ids if (mm := re.fullmatch(r"c0*(\d+)([a-z]?)", cid))
                     and (mm.group(1), mm.group(2)) == (m.group(1), m.group(2))), None)

    def _no_cut(self, cut_id: str) -> str:
        ids = [g["id"] for g in self._cut_groups()]
        plain = [c for c in ids if re.fullmatch(r"c\d+", c)]
        extra = [c for c in ids if c not in plain]
        span = (f"{plain[0]}…{plain[-1]}" if len(plain) > 1 else ", ".join(plain)) + (f" (+ {', '.join(extra)})" if extra else "")
        return f"no cut {cut_id}; cuts are {span or 'not named in this timeline'}"

    def _cut_spans(self, skip: Iterable[str] = ()) -> dict[str, tuple[float, float | None]]:
        """``{cut id: (start, end)}``: a cut runs from its start to the next cut's start (end None: the last).
        Cuts in ``skip`` (orphans) don't end the cut before them."""
        skip = set(skip) | set(getattr(self, "_orphan_cuts", set()) or ())
        groups = [g for g in self._cut_groups() if g["id"] not in skip]
        return {g["id"]: (g["start"], groups[k + 1]["start"] if k + 1 < len(groups) else None) for k, g in enumerate(groups)}

    def _cut_groups(self) -> list[dict[str, Any]]:
        """Clips grouped by cut id, in time order; each group's picture is its bed clip (first step)."""
        groups: dict[str, list[Clip]] = {}
        for clip in self.clips():
            cid = intent.cut_of(clip.data)
            if cid:
                groups.setdefault(cid, []).append(clip)
        out = []
        for cid, clips in groups.items():
            if all(intent.orphan(c.data) for c in clips):
                continue  # a cut kept only as orphans is not a cut until it is re-homed
            beds = sorted((c for c in clips if c.track == "plate" and not c.is_audio), key=lambda c: (c.start, c.id))
            out.append({"id": cid, "clips": clips, "picture": beds[0] if beds else None,
                        "start": beds[0].start if beds else min(c.start for c in clips)})
        return sorted(out, key=lambda g: (g["start"], g["id"]))

    def _is_picture(self, clip: "Clip") -> bool:
        cut = intent.cut_of(clip.data)
        if not cut or clip.track != "plate" or clip.is_audio:
            return False
        seq = intent.sequence(clip.data)
        if seq is not None and seq[1] > 0:
            return False
        beds = [c for c in self.clips(shot=clip.shot_id) if intent.cut_of(c.data) == cut and c.track == "plate" and not c.is_audio]
        return bool(beds) and min(beds, key=lambda c: (c.start, c.id)).data is clip.data

    # ---- internals ----------------------------------------------------------
    def _shot_ids(self) -> list[str]:
        rows = sorted(self.bundle.get("placements") or [], key=lambda r: _row_start(r))
        return [str(r.get("shot_id")) for r in rows]

    def _row(self, shot_id: str) -> dict[str, Any]:
        for row in self.bundle.get("placements") or []:
            if str(row.get("shot_id")) == shot_id:
                return row
        raise TimelineEditError(f"no shot {shot_id!r} is placed in this timeline")

    def _shot_start(self, shot_id: str) -> float:
        return _row_start(self._row(shot_id)) / 1000.0

    def _shot_length(self, shot_id: str) -> float:
        return _num(self._row(shot_id).get("duration_ms")) / 1000.0

    def _shot_id(self, shot: Any) -> str:
        ids = self._shot_ids()
        if isinstance(shot, int) or (isinstance(shot, str) and shot.isdigit()):
            n = int(shot)
            if not 1 <= n <= len(ids):
                raise TimelineEditError(f"shot {n} does not exist; shots are 1–{len(ids)}")
            return ids[n - 1]
        text = str(shot)
        for sid in ids:
            name = str(((self.bundle["shots"].get(sid) or {}).get("payload") or {}).get("name") or "")
            if sid == text or sid.endswith(text) or _norm(name) == _norm(text) or _norm(text) in _norm(name):
                return sid
        raise TimelineEditError(f"no shot matches {shot!r}; shots: " + ", ".join(ids))

    def _internal(self, shot_id: str) -> dict[str, Any]:
        shot = self.bundle["shots"][shot_id]
        internal = shot.setdefault("internal_timeline", {})
        internal.setdefault("clips", [])
        return internal

    def _track_kind(self, shot_id: str, track: str) -> str:
        for t in self._internal(shot_id).get("tracks") or []:
            if str(t.get("id")) == track:
                return str(t.get("kind") or "").lower()
        return ""

    def _is_audio(self, shot_id: str, clip: Mapping[str, Any]) -> bool:
        track = str(clip.get("track") or "")
        return self._track_kind(shot_id, track) in AUDIO_KINDS or track in AUDIO_KINDS

    def _shot_at(self, t: float) -> str:
        for sid in self._shot_ids():
            row = self._row(sid)
            start = _row_start(row) / 1000.0
            if start - 1e-6 <= t < start + _num(row.get("duration_ms")) / 1000.0 - 1e-6:
                return sid
        raise TimelineEditError(f"{t:.3f} s is outside every shot (the timeline is {self.duration:.3f} s)")

    def _cut_at(self, t: float) -> int | str:
        for cut in self.cuts:
            if cut.start - 1e-6 <= t < cut.end - 1e-6:
                return cut.n
        return "-"

    def _as_word(self, when: Any) -> Word | None:
        if isinstance(when, Word):
            return when
        if isinstance(when, str) and not TIME_RE.match(when.strip()):
            return self.word(when)
        return None

    def _pick(self, pool: Sequence[Clip], query: str, *, where: str) -> Clip:
        text = str(query).strip()
        tiers = [
            [c for c in pool if c.id == text or c.address == text],
            [c for c in pool if c.layer_name == text],
            [c for c in pool if c.id.startswith(text + "-") or c.id.startswith(text)],
            [c for c in pool if (c.asset or "").lower() == text.lower()],
            [c for c in pool if c.element == text],
            [c for c in pool if c.text and _norm(text) in _norm(c.text)],
        ]
        for hits in tiers:
            unique = list({id(c.data): c for c in hits}.values())
            if len(unique) == 1:
                return unique[0]
            if len(unique) > 1:
                listing = "\n  ".join(f"{c.address:<18} {c.element:<14} {c.start:7.3f}–{c.end:.3f} s" for c in unique[:12])
                more = f"\n  … {len(unique) - 12} more" if len(unique) > 12 else ""
                raise TimelineEditError(f"{text!r} matches {len(unique)} clips in {where}; name one:\n  {listing}{more}")
        raise TimelineEditError(f"no clip in {where} matches {text!r} (by id, asset, element or text); "
                                f"list them with tl.clips() or `timelines show --as script`")

    def _new_id(self, element: str) -> str:
        existing = {c.id for c in self.clips()}
        k = 1
        while f"{element}-{k:02d}" in existing:
            k += 1
        return f"{element}-{k:02d}"
