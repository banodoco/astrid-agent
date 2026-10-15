"""Current timing evaluators, run only on the compiler-owned working copy."""
from __future__ import annotations

import copy
import json
import math
import statistics
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Mapping, Sequence

from astrid.core.timeline import moments as mo
from astrid.core.timeline.music_edit import plan_keep_end
from astrid.sdk import timeline_intent as intent
from astrid.core.timeline.cuts import (
    clip_duration,
)

from astrid.sdk.timeline._common import (LOGICAL_PX, TimelineEditError, _norm, _num, _r)
from astrid.sdk.timeline.doc import (_clock, _get_path, _post, _read_words, _row_start, _set_length, _set_path, _set_row_start, _split_audio, _trim_end, _trim_front)
from astrid.sdk.timeline.model import (Clip, Voice, Word, _MomentContext, music_keyword, music_policy, retime_scope)
from astrid.sdk.timeline.selection import Selection
from astrid.sdk.timeline.sequence_fit import allocate_fit_frames, fit_curve_progress
from astrid.sdk.timeline.units import (_unit_path, element_units, mark_offset_canvas)


_EPS = 1e-6


def _bar_len(bars: Sequence[float]) -> float:
    """The bed's bar: the median gap between its downbeats (source seconds), else 2.0 s."""
    pts = sorted(float(x) for x in bars)
    gaps = [b - a for a, b in zip(pts, pts[1:]) if b - a > _EPS]
    return statistics.median(gaps) if gaps else 2.0


def _bed_clusters(records: Sequence[tuple[float, float]], bar: float, frame: float) -> list[tuple[float, float]]:
    """The changes of one bed, as (film second, net seconds): neighbours less than a bar apart are one cut at the
    first one's place with their net delta; a net under a frame is no cut."""
    groups: list[list[tuple[float, float]]] = []
    for p, d in sorted(records, key=lambda r: r[0]):
        if groups and p - groups[-1][-1][0] < bar - _EPS:
            groups[-1].append((p, d))
        else:
            groups.append([(p, d)])
    out = []
    for group in groups:
        net = sum(d for _, d in group)
        if abs(net) >= frame - _EPS:
            out.append((group[0][0], net))
    return out


def _is_seconds(text: str) -> bool:
    """A bare number of seconds ("2.5", "2.5s"), which is a time, not a moment."""
    try:
        float(text.strip().rstrip("sS"))
    except ValueError:
        return False
    return True


class _TimingEvaluators:
    """Checkout: the voice track and re-flow, moments (resolve, retime, place), and time shifts inside shots."""

    _scope: list | None = None  # the changes of the open re-time scope (keep_end only): (film second, seconds, skip ids)

    @property
    def music(self) -> str:
        """What a re-time does to the music bed: ``keep_end`` (default: cut at a bar with a crossfade, so its
        ending stays on the film's end) or ``plays_on`` (the bed keeps playing unchanged)."""
        return self._music

    @music.setter
    def music(self, value: str) -> None:
        self._music = music_policy(value)

    @contextmanager
    def _music_as(self, music: str | None):
        """Set the bed policy for one call (None: keep the checkout's), restored afterwards."""
        if music is None:
            yield
            return
        saved = self._music
        self._music = music_policy(music)
        try:
            yield
        finally:
            self._music = saved

    # The film's length follows its last picture (_sync_film_length): element types that are full-frame pictures.
    FULL_FRAME = frozenset({"am-snap-plate", "am-footage", "media"})

    # ---- the voice track: lines back to back with declared gaps (re-flow) -------
    @property
    def lines(self) -> Selection:
        """The VO lines in order (a Selection: ``tl.lines()`` is all, ``tl.lines(text="viral")`` filters)."""
        seen: dict[str, float] = {}
        for clip, words in self._pieces():
            seen.setdefault(intent.line(clip.data) or clip.id, words[0].start)
        return Selection((Voice(self, seg) for seg, _t in sorted(seen.items(), key=lambda item: item[1])),
                         kind="line", tl=self)

    def _pieces(self) -> list[tuple[Clip, list[Word]]]:
        """The voice track: every VO clip with words, in speaking order. A line is one or more
        pieces (a tightened line is split into pieces with their own silences)."""
        by_clip: dict[str, list[Word]] = {}
        for w in self.words():
            by_clip.setdefault(w.clip_id, []).append(w)
        out = []
        for clip in self.clips(audio=True):
            if intent.line(clip.data) and by_clip.get(clip.id):
                out.append((clip, by_clip[clip.id]))
        return sorted(out, key=lambda item: item[1][0].start)

    def _mark(self, prev: Clip, nxt: Clip, nwords: Sequence[Word]) -> float:
        """Where the silence after ``prev`` ends: the next line's take begins (its in-point), or,
        inside a tightened line, the next word group's first word."""
        if intent.line(nxt.data) != intent.line(prev.data):
            speed = _num(nxt.data.get("speed"), 1.0) or 1.0
            return nxt.start - _num(nxt.data.get("from")) / speed
        return nwords[0].start

    def _piece_gaps(self) -> dict[str, float]:
        pieces = self._pieces()
        return {a.id: _r(self._mark(a, b, wb) - wa[-1].end) for (a, wa), (b, wb) in zip(pieces, pieces[1:])}

    def gaps(self) -> dict[str, float]:
        """The silence after each line as it is now: its last word ends → the next line's take begins
        (the ``gap_after_s`` of a VO script)."""
        lines = self.lines()
        out = {}
        for line, nxt in zip(lines, lines[1:]):
            first = nxt.clips[0]
            speed = _num(first.data.get("speed"), 1.0) or 1.0
            out[line.segment] = _r(first.start - _num(first.data.get("from")) / speed - line.speech_end)
        return out

    def declare_gaps(self) -> int:
        """Record every piece's current silence as its declared gap (once, when a timeline is migrated)."""
        n = 0
        measured = self._piece_gaps()
        for clip, _words in self._pieces():
            if intent.gap_after(clip.data) is None and clip.id in measured:
                intent.set_gap_after(clip.data, measured[clip.id])
                n += 1
        return n

    @music_keyword
    def reflow(self, *, gaps: Mapping[str, float] | None = None, points: Mapping[str, float] | None = None) -> list[str]:
        """Lay the voice track out again (each piece's speech end + its gap) and ripple everything downstream.

        This re-times the whole film after a script change: a longer or shorter take, an
        inserted or removed line, a new gap. Each piece's gap is the one declared on it, else
        the one in ``gaps`` (clip id → seconds, what it was before the change). Time opens or
        closes in the silence after a piece (at ``points[clip id]``, where its speech used to
        end, else where it ends now), so picture, overlays, SFX and music after it move as one
        and keep their sync; anchored clips are re-resolved. Each music bed is cut at a bar (``keep_end``)
        or left whole (``plays_on``), and every cut is reported.
        """
        report: list[str] = []
        gaps, points = dict(gaps or {}), dict(points or {})
        notes_before = len(self.notes)
        reason = self._orphan_reason
        self._orphan_reason = "the silence it sat in was closed"
        try:
            self._reflow_pieces(gaps, points, report)
        finally:
            self._orphan_reason = reason
        moved = self.retime()
        notes = list(dict.fromkeys(self.notes[notes_before:]))
        del self.notes[notes_before:]
        orphaned = [n for n in notes if " is not spoken" in n or "is spoken" in n]
        others = [n for n in notes if n not in orphaned]
        tail: list[str] = []
        if orphaned:
            tail.append(f"{len(orphaned)} moment(s) lost their word (orphans: re-home or remove; `timelines status` lists them):")
            tail += [f"  {n}" for n in orphaned]
        tail += others
        if moved:
            tail.append(f"{len(moved)} clip(s) on moments followed their words"
                        + (": " + ", ".join(f"{cid} {a:.2f}→{b:.2f}" for cid, a, b in moved[:6]) + (" …" if len(moved) > 6 else "")))
        music_end = max((c.end for c in self.clips(audio=True) if c.track == "music"), default=None)
        if music_end is not None and music_end < self.duration - 0.5 and self._music == "plays_on":
            tail.append(f"music: the bed ends at {music_end:.2f} s; the film now runs {self.duration:.2f} s "
                        f"({self.duration - music_end:.2f} s without music: extend or re-cue; --music plays-on kept the bed whole)")
        report += tail
        self.report = report
        return report

    def _reflow_pieces(self, gaps: dict[str, float], points: dict[str, float], report: list[str]) -> None:
        """The piece loop of ``reflow``: each declared silence is opened or closed (un-anchored clips inside a closed one
        become orphans, not deletions)."""
        i = 0
        while True:
            pieces = self._pieces()
            if i + 1 >= len(pieces):
                break
            (clip, words), (_nclip, nwords) = pieces[i], pieces[i + 1]
            i += 1
            declared = intent.gap_after(clip.data)
            gap = declared if declared is not None else gaps.get(clip.id)
            if gap is None:
                continue
            end, have = words[-1].end, self._mark(clip, _nclip, nwords)
            delta = self.quantize(end + gap - have)
            if abs(delta) < 0.5 / self.fps:
                continue
            point = points.get(clip.id, end)
            if delta > 0:
                self.insert_time(self.quantize(max(point, end - delta)), delta, skip=[clip.data])
            else:
                start = max(end, min(point, _nclip.start + delta))
                self.ripple_delete(self.quantize(start), self.quantize(start) - delta, skip=[clip.data], keep_inside=True)
            report.append(f"{nwords[0].segment} ({nwords[0].text!r}) {nwords[0].start:.3f} → {nwords[0].start + delta:.3f} s ({delta:+.3f} s, everything after it moved)")

    @music_keyword
    def insert_line(self, segment: str, media: Any, *, words: Any, after: str, gap_after: float | None = None,
                    text: str | None = None) -> list[str]:
        """Insert a new VO line after line ``after``; everything downstream moves later to make room."""
        prev = Voice(self, after)
        if segment in {v.segment for v in self.lines()}:
            raise TimelineEditError(f"there is already a line {segment!r}; pick a new id")
        new_words = _read_words(words)
        if not new_words:
            raise TimelineEditError("the new line has no words; pass its words.json")
        before = self._piece_gaps()
        last = prev.clips[-1]
        gap_prev = prev.gap_after if prev.gap_after is not None else before.get(last.id, 0.3)
        gap_new = gap_after if gap_after is not None else gap_prev
        start = self.quantize(prev.speech_end + gap_prev)  # the new take begins
        first = start + new_words[0][0]
        # open the room in the silence after the previous line: what was keyed to the next line moves with it
        self.insert_time(prev.speech_end, new_words[-1][1] + gap_new, skip=[c.data for c in prev.clips])
        sid = self._shot_at(start)
        new = {"id": f"vo-{segment}-0", "clipType": "media", "track": last.track,
               "at": _r(start - self._shot_start(sid)), "from": 0.0, "to": _r(new_words[-1][1] + 0.06)}
        for key in ("volume", "gain_db"):
            if key in last.data:
                new[key] = copy.deepcopy(last.data[key])
        intent.set_line(new, segment, [[_r(a), _r(b), t] for a, b, t in new_words])
        if text is not None:
            intent.set_line_text(new, text)
        if gap_after is not None:
            intent.set_gap_after(new, _r(gap_after))
        new["asset"] = self._register_asset(sid, media)
        self._internal(sid)["clips"].append(new)
        before[new["id"]] = gap_new
        out = [f"inserted line {segment} at {first:.3f} s ({new_words[-1][1] - new_words[0][0]:.3f} s of speech)"]
        out += self.reflow(gaps=before)
        holding = next((g["id"] for g in reversed(self._cut_groups()) if g["start"] <= first + 1e-6), None)
        if holding:
            out.append(f"  {segment} has no cut of its own yet: {holding}'s picture holds over it "
                       f"(re-home an orphan cut onto it: --cut cNN --on '\"{new_words[0][2]}\" in {segment}')")
        return out

    @music_keyword
    def remove_line(self, segment: str) -> list[str]:
        """Remove a VO line, what is on screen only for it, and its silence; everything after moves up."""
        line = Voice(self, segment)
        lines = self.lines()
        index = next(i for i, v in enumerate(lines) if v.segment == segment)
        before = self._piece_gaps()
        start = self._frame(line.clips[0].start)  # cuts open on the frame of the take's in-point
        if index + 1 < len(lines):
            end = self._frame(lines[index + 1].clips[0].start)
        else:
            end = line.speech_end + (line.gap_after or 0.0)
        for clip in line.clips:
            clip.remove()
        self._kept = []
        self._orphan_reason = f"line {segment} was removed"
        notes_before = len(self.notes)
        removed = self.ripple_delete(start, end, keep_inside=True)
        kept = list(self._kept)
        out = [f"removed line {segment} ({removed:.3f} s)"]
        if kept:
            out.append(f"kept {len(kept)} clip(s) that were on screen only for {segment}, where they were, as orphans "
                       "to re-home (--on) or remove")
        bed = [n for n in self.notes[notes_before:] if n.startswith("music ")]  # what the bed did: reported here
        self.notes[notes_before:] = [n for n in self.notes[notes_before:] if not n.startswith("music ")]
        return out + list(dict.fromkeys(bed)) + self.reflow(gaps=before)

    @music_keyword
    def apply_script(self, spec: Any, *, takes: Any = None, gaps: bool = False) -> list[str]:
        """Bring the voice track in line with a VO script: ``{"segments": [{id, text, gap_after_s}, …]}``
        (a path or a dict), with each take at ``<takes>/<id>.wav`` and its words at ``<takes>/<id>.words.json``
        (default: a ``vo`` folder next to the script, else the script's folder).

        A line whose words changed gets its new take; a new line is inserted after the one
        before it; a line no longer in the script is removed; then the film re-flows once.
        Declared gaps are kept unless ``gaps=True`` (then the script's ``gap_after_s`` wins). A new
        line without ``gap_after_s`` gets the script's ``default_gap_after_s`` (else 0.3 s), and the
        report names every such line. The script's ``voice``/``rate`` are TTS settings: re-flow reads
        only ``id``, ``text`` and ``gap_after_s`` (and the takes' words).
        """
        path = Path(spec).expanduser() if isinstance(spec, (str, Path)) else None
        data = json.loads(path.read_text(encoding="utf-8")) if path else dict(spec)
        folder = Path(takes).expanduser() if takes else (path.parent.parent / "vo" if path and (path.parent.parent / "vo").is_dir() else (path.parent if path else Path.cwd()))
        segments = [s for s in data.get("segments") or [] if s.get("id")]
        wanted = [str(s["id"]) for s in segments]
        default_gap = float(data.get("default_gap_after_s", 0.3))
        film_before = self.duration
        report: list[str] = []
        no_gap: list[str] = []
        have = {v.segment for v in self.lines()}
        for seg in sorted(have - set(wanted)):
            report += self.remove_line(seg)
        previous = None
        for item in segments:
            seg = str(item["id"])
            words_path = folder / f"{seg}.words.json"
            take = folder / f"{seg}.wav"
            new_words = _read_words(words_path) if words_path.is_file() else []
            if seg not in have:
                if not new_words or not take.is_file() or previous is None:
                    report.append(f"line {seg}: not added (needs {take.name}, {words_path.name} and a line before it)")
                else:
                    gap = item.get("gap_after_s")
                    if gap is None:
                        no_gap.append(seg)
                        gap = default_gap
                    report += self.insert_line(seg, take, words=new_words, after=previous, text=item.get("text"),
                                               gap_after=float(gap))
            else:
                line = Voice(self, seg)
                current = [(round(w.end - w.start, 3), _norm(w.text)) for w in line.words]
                fresh = [(round(e - s, 3), _norm(t)) for s, e, t in new_words]
                if new_words and take.is_file() and current != fresh:
                    line.replace(take, words=new_words, text=item.get("text"))
                    report += [f"line {seg}: new take"] + self.report
                elif item.get("text") and intent.line_text(line.clips[0].data) != item["text"]:
                    intent.set_line_text(line.clips[0].data, item["text"])
                    report.append(f"line {seg}: script text updated (narration re-pins on publish)")
                if gaps and item.get("gap_after_s") is None:
                    no_gap.append(f"{seg} (kept {line.gap_after if line.gap_after is not None else '?'} s)")
                if gaps and item.get("gap_after_s") is not None and line.gap_after != item["gap_after_s"]:
                    line.set_gap_after(float(item["gap_after_s"]), reflow=False)
                    report.append(f"line {seg}: gap after {item['gap_after_s']} s")
            previous = seg
        report += self.reflow()
        head = [f"film {_clock(film_before)} → {_clock(self.duration)} ({self.duration - film_before:+.1f} s)"]
        if no_gap:
            head.append(f"{len(no_gap)} line(s) had no gap_after_s: new ones got {default_gap:g} s "
                        f"(the script's default_gap_after_s): {', '.join(no_gap)}")
        report = head + report
        self.report = report
        return report

    def retime(self) -> list[tuple[str, float, float]]:
        """Resolve every moment (after a VO change, a gap, an edit). Returns what moved.

        Cuts first: each cut's picture starts on its ``on`` moment and runs to the next cut,
        so the picture track never gets a hole. Then every clip of a cut starts on its own
        ``on`` (or with its cut) and ends on its ``until``, its literal ``for``, or its cut's
        end. Clips without a cut only follow their ``on``/``until``. Moments floor to the frame."""
        moved: dict[int, tuple[str, float, float]] = {}
        self._moved_ends: dict[str, tuple[float, float]] = {}
        self._mcache = None
        groups = self._cut_groups()
        orphan_cuts: set[str] = set()
        sequences: dict[str, list[Clip]] = {}
        for clip in self.clips():
            tag = intent.sequence(clip.data)
            if tag and not intent.orphan(clip.data):
                sequences.setdefault(tag[0], []).append(clip)
        ratios = {sid: [max(1, int(round(c.duration * self.fps))) for c in sorted(steps, key=lambda c: intent.sequence(c.data)[1])]
                  for sid, steps in sequences.items()}  # before anything moves: the unit's shape
        for g in groups:  # 1. cut starts
            pic = g["picture"]
            on = intent.on(pic.data) if pic else None
            if not on:
                continue
            t = self._resolve_note(on, pic, in_point=True)
            if t is None:
                orphan_cuts.add(g["id"])  # its word is gone: the cut keeps its place and its length
                continue
            if self._move_within_shot(pic, t, keep_end=True, moved=moved):
                self._mcache = None
        cut_span = self._cut_spans(skip=orphan_cuts)
        for cid in orphan_cuts:
            group = next(g for g in self._cut_groups() if g["id"] == cid)
            cut_span[cid] = (group["start"], group["picture"].end if group["picture"] else None)
        self._orphan_cuts = orphan_cuts
        self._mcache = {"cuts": cut_span}
        pictures = {id(g["picture"].data): g["id"] for g in groups if g["picture"] is not None}
        picture_of = {g["id"]: g["picture"] for g in groups if g["picture"] is not None}
        for seq_id, steps in sequences.items():  # 1b. a cut's sequence fills its cut, as one unit, in its ratios
            steps = sorted(steps, key=lambda c: intent.sequence(c.data)[1])
            cid = pictures.get(id(steps[0].data))
            if cid is None or cid in orphan_cuts or cid not in cut_span:
                continue
            lo, hi = cut_span[cid]
            until = intent.until(steps[0].data)
            if until:
                hi = self._resolve_note(until, steps[0], start=lo)
            if hi is None:
                hi = max(c.end for c in steps)
            if int(round((hi - lo) * self.fps)) < len(steps):
                raise TimelineEditError(f"sequence {seq_id}: span is too short for {len(steps)} required steps")
            before = [(c.start, c.end) for c in steps]
            self._relay_steps([c.data for c in steps], lo - self._shot_start(steps[0].shot_id), hi - lo, ratios[seq_id])
            for c, (a, b) in zip(steps, before):
                if round(a * self.fps) != round(c.start * self.fps) or round(b * self.fps) != round(c.end * self.fps):
                    moved.setdefault(id(c.data), (c.address, a, c.start))
        self._mcache = {"cuts": cut_span}
        for clip in self.clips():  # 2. every clip on its moments
            cut = intent.cut_of(clip.data)
            if (cut in orphan_cuts and not intent.on(clip.data)) or intent.orphan(clip.data):
                continue  # an orphan keeps its time and length until it is re-homed
            seq = intent.sequence(clip.data)
            later_step = seq is not None and seq[1] > 0
            on, until, length = intent.on(clip.data), intent.until(clip.data), intent.for_s(clip.data)
            is_picture = any(g["picture"] is not None and g["picture"].data is clip.data for g in groups)
            start = clip.start
            if on and not is_picture:
                t = self._resolve_note(on, clip)
                if t is not None:
                    start = t
            elif cut and not is_picture and not later_step and not clip.is_audio and cut in cut_span:
                start = cut_span[cut][0]
            end = None
            if until:
                end = self._resolve_note(until, clip, start=start)
            elif length is not None:
                end = start + length
            elif cut and not clip.is_audio and not later_step and cut in cut_span and (seq is None or is_picture):
                end = cut_span[cut][1]
                if end is None and cut in picture_of:  # the last cut has no next cut: it runs to its picture's end
                    end = picture_of[cut].end
            if seq is not None:  # a sequence's steps were laid out as one unit (1b): never one by one
                start, end = clip.start, None
            self._place(clip, start, end, moved)
        self._mcache = None
        self._mirror_holds()  # a cut's hold follows its picture role
        self.notes.extend(self._sync_film_length())  # the film's length follows its content
        return list(moved.values())

    def _sync_film_length(self) -> list[str]:
        """The last shot follows its last picture: its placement length becomes the end of its last picture clip
        (a plate, or a full-frame element) minus the shot's start. Middle shots are not resized here. Returns the
        plain-words note when it changed something (the caller adds it to ``notes``)."""
        ids = self._shot_ids()
        if not ids:
            return []
        sid = ids[-1]
        pictures = [c for c in self.clips() if c.shot_id == sid and not c.is_audio
                    and (c.track == "plate" or c.element in self.FULL_FRAME)]
        if not pictures:
            return []
        row = self._row(sid)
        start = self._shot_start(sid)
        old_ms = _num(row.get("duration_ms"))
        new_ms = int(round((max(c.end for c in pictures) - start) * 1000))
        if abs(new_ms - old_ms) < 1:
            return []
        row["duration_ms"] = new_ms
        return [f"the film now runs {start + new_ms / 1000:.2f} s (was {start + old_ms / 1000:.2f} s): "
                "the last shot follows its last picture"]

    @music_keyword
    def film_end(self, moment: Any) -> list[str]:
        """Where the film ENDS, as a moment (W3): ``after "Astrid" in n27 +2.5s`` ends it 2.5 s after that word.

        The visuals that end with the film (its last picture, and any layer that ends with it) get ``until`` = the
        moment, so a re-flow keeps the new end. keep_end: the music bed repeats (later) or skips (earlier) at a bar
        before its ending, so its ending stays on the film's end. Refused before the film's last spoken word and for
        a plain number of seconds. Returns the plain-words notes."""
        if isinstance(moment, (int, float)) or (isinstance(moment, str) and _is_seconds(moment)):
            raise TimelineEditError('write a moment, e.g. after "Astrid" in n27 +2.5s (a number of seconds is not a moment)')
        text = self._moment_text(moment)
        t = self._frame(self._moment_time(text))
        frame = 1.0 / self.fps
        last = max((w.end for w in self._word_list()), default=None)
        if last is not None and t <= last + _EPS:
            raise TimelineEditError(f"the film can't end before its last word, which ends at {last:.2f} s")
        if t <= 0:
            raise TimelineEditError(f"the film can't end at {t:.2f} s")
        end = self.duration
        if abs(t - end) < frame - _EPS:
            return [f"film end: no change (the film already ends at {end:.2f} s)"]
        targets: list[Clip] = []
        seen: set[int] = set()
        for clip in self.clips():
            if clip.is_audio or abs(clip.end - end) >= frame - _EPS:
                continue
            seq = intent.sequence(clip.data)
            if seq is not None:  # a sequence is laid out as one unit: its first step carries the end
                clip = min(clip._sequence_mates(), key=lambda m: intent.sequence(m.data)[1])
            if id(clip.data) not in seen:
                seen.add(id(clip.data))
                targets.append(clip)
        if not targets:
            raise TimelineEditError(f"no picture ends with the film ({end:.2f} s): nothing to hold to the new end")
        for clip in targets:
            if t <= clip.start + 0.5 * frame:
                raise TimelineEditError(f"{clip.address} starts at {clip.start:.2f} s, after the new end ({t:.2f} s)")
        for clip in targets:
            intent.set_until(clip.data, text)
            intent.set_for(clip.data, None)
            _set_length(clip.data, t - clip.start)
        self.retime()
        if t > end:  # opened at the film's end (a bar before it, inside the bed): the bed repeats
            self._record_change(end - frame, end, t - end, set())
        else:  # removed from the new end to the old one: the bed skips
            self._record_change(t, end, t - end, set())
        names = [c.address for c in targets]
        shown = ", ".join(names[:4]) + (f" … (+{len(names) - 4})" if len(names) > 4 else "")
        out = [f"the film now ends at {t:.2f} s (was {end:.2f} s): {shown} hold to it"]
        if self._music == "plays_on":
            out.append("the music bed plays on unchanged (plays_on)")
        return out

    # ---- moments: the plumbing ------------------------------------------------
    def _frame(self, t: float) -> float:
        return _r(mo.floor_frame(t, self.fps))

    def _moment_text(self, moment: Any) -> str:
        """Canonical text for a moment given as text, a Word, a Moment or a number of seconds."""
        if isinstance(moment, Word):
            return mo.format_moment(mo.word_moment(moment, self.words()))
        if isinstance(moment, mo.Moment):
            return mo.format_moment(moment)
        if isinstance(moment, (int, float)):
            raise TimelineEditError("a number is a time, not a moment; use clip.enter_at(seconds) or a moment like +0.8s")
        text = str(moment).strip()
        if text.lower().startswith(("until ", "on ")):
            text = text.split(" ", 1)[1]
        try:
            return mo.format_moment(mo.parse(text))
        except mo.MomentError as exc:
            raise TimelineEditError(str(exc)) from None

    def _moment_time(self, text: str, clip: "Clip | None" = None, *, in_point: bool = False) -> float:
        try:
            return self._frame(mo.resolve(mo.parse(text), _MomentContext(self, clip), in_point=in_point))
        except mo.MomentError as exc:
            raise TimelineEditError(f"{clip.address + ': ' if clip else ''}{exc}") from None

    def _resolve_note(self, text: str, clip: "Clip", *, in_point: bool = False, start: float | None = None) -> float | None:
        try:
            return self._moment_time(text, clip, in_point=in_point)
        except TimelineEditError as exc:
            pinned = self._pin_nearest(text, clip)
            if pinned is not None:
                field = "on" if intent.on(clip.data) == text else "until"
                (intent.set_on if field == "on" else intent.set_until)(clip.data, pinned)
                self.notes.append(f"{clip.address}: {text} is now said more than once; pinned to {pinned}, the one nearest it")
                try:
                    return self._moment_time(pinned, clip, in_point=in_point)
                except TimelineEditError:
                    pass
            self.notes.append(str(exc))
            return None

    def _pin_nearest(self, text: str, clip: "Clip", at: float | None = None) -> str | None:
        """A word moment that became ambiguous (a new line says the same word): its scoped form for the
        occurrence nearest the clip's current time, or None if it is not an ambiguity."""
        try:
            moment = mo.parse(text)
        except mo.MomentError:
            return None
        if moment.kind in ("beat", "downbeat") and moment.base is not None and moment.base.kind == "word":
            # beat 2 after "Astrid": pin the word to the occurrence just before the clip
            base = moment.base
            if base.n is not None or base.line is not None:
                return None
            hits = mo.find_words(base, self.words())
            if len(hits) < 2:
                return None
            earlier = [r for r in hits if r[0].start <= clip.start + 1e-6] or hits
            run = max(earlier, key=lambda r: r[0].start)
            from astrid.sdk.timeline_address import _phrase_address

            pinned_base = mo.parse(_phrase_address(self, base, run))
            return mo.format_moment(mo.Moment(moment.kind, n=moment.n, direction=moment.direction, base=pinned_base,
                                              offset_s=moment.offset_s, offset_frames=moment.offset_frames))
        if moment.kind != "word" or moment.n is not None:
            return None
        hits = mo.find_words(moment, self.words())
        if len(hits) < 2:
            return None
        anchor_t = (at if at is not None else clip.start) - moment.offset_s - moment.offset_frames / self.fps
        run = min(hits, key=lambda r: abs((r[-1].end if moment.edge == "end" else r[0].start) - anchor_t))
        from astrid.sdk.timeline_address import _phrase_address

        return _phrase_address(self, moment, run)

    def _place_on(self, clip: "Clip", moment: Any) -> None:
        if isinstance(moment, (int, float)) and not isinstance(moment, bool):
            span = self._own_cut_span(clip)
            if span is not None:
                moment = mo.offset_text(self._frame(float(moment)) - span[0], self.fps) or None
            else:
                intent.set_on(clip.data, None)
                clip._set_start(self._frame(float(moment)))
                return
        if moment is None:
            intent.set_on(clip.data, None)
            span = self._own_cut_span(clip)
            if span is not None:
                clip._set_start(span[0])
            return
        text = self._moment_text(moment)
        is_picture = self._is_picture(clip)
        t = self._moment_time(text, clip, in_point=is_picture)
        intent.set_on(clip.data, text)
        if is_picture:
            self._move_within_shot(clip, t, keep_end=True, moved={})
        else:
            clip._set_start(t)

    def _own_cut_span(self, clip: "Clip") -> tuple[float, float] | None:
        cut = intent.cut_of(clip.data)
        if not cut:
            return None
        spans = (self._mcache or {}).get("cuts")
        if spans is None:
            spans = self._cut_spans()
        span = spans.get(cut)
        if span is None:
            return None
        return span[0], span[1] if span[1] is not None else self.duration

    def orphans(self) -> list[str]:
        """Moments that no longer resolve (their word was cut or rewritten), one readable line each:
        ``c22.rocket  on "viral": "viral" is not spoken in n20b; did you mean …``."""
        out = []
        self._mcache = None
        sequences_seen: set[str] = set()
        for clip in self.clips():
            reason = intent.orphan(clip.data)
            if reason:
                seq = intent.sequence(clip.data)
                if seq is not None:  # a time-lapse is one orphan, not one per step
                    if seq[0] in sequences_seen:
                        continue
                    sequences_seen.add(seq[0])
                    steps = [c for c in self.clips() if (intent.sequence(c.data) or ("",))[0] == seq[0]]
                    lo, hi = min(c.start for c in steps), max(c.end for c in steps)
                    out.append(f"{clip.address:<16} a sequence of {len(steps)} steps kept at {lo:.2f}–{hi:.2f} s: {reason} "
                               "(re-home its first step: --on MOMENT; --keep; or remove it)")
                    continue
                out.append(f"{clip.address:<16} kept at {clip.start:.2f}–{clip.end:.2f} s: {reason} "
                           "(re-home it: --on MOMENT; keep it as is: --keep; or remove it)")
                continue
            for field, text in (("on", intent.on(clip.data)), ("until", intent.until(clip.data))):
                if not text:
                    continue
                try:
                    mo.resolve(mo.parse(text), _MomentContext(self, clip))
                except mo.MomentError as exc:
                    why = ("the music has no beat there (the bed ends before it)" if "there is no beat" in str(exc)
                           or "there is no downbeat" in str(exc) else "its word is gone")
                    out.append(f"{clip.address:<16} {field} {text}: {exc} ({why}: re-home it or remove the clip)")
            for path, expr in intent.formulas(clip.data).items():
                if isinstance(expr, Mapping) and expr.get("moment"):
                    try:
                        mo.resolve(mo.parse(expr["moment"]), _MomentContext(self, clip))
                    except mo.MomentError as exc:
                        out.append(f"{clip.address:<16} {path} = {expr['moment']}: {exc}")
            fit = intent.sequence_fit(clip.data)
            if fit and isinstance(fit.get("land"), str):
                try:
                    mo.resolve(mo.parse(fit["land"]), _MomentContext(self, clip))
                except mo.MomentError as exc:
                    out.append(f"{clip.address:<16} fit until {fit['land']}: {exc}")
        return out

    def _move_within_shot(self, clip: "Clip", t: float, *, keep_end: bool, moved: dict) -> bool:
        """Move a cut's picture start to ``t`` (it must stay inside its shot); the picture before it follows."""
        if round(t * self.fps) == round(clip.start * self.fps):
            return False
        lo = self._shot_start(clip.shot_id)
        hi = lo + self._shot_length(clip.shot_id)
        if not lo - 1e-6 <= t < hi - 0.5 / self.fps:
            self.notes.append(f"{clip.address}: its moment is at {t:.3f} s, outside its chapter ({lo:.3f}–{hi:.3f} s); left in place")
            return False
        old = clip.start
        before = [c for c in self.clips(shot=clip.shot_id) if c.track == clip.track and c.data is not clip.data
                  and abs(c.end - old) < 1e-3 and not c.is_audio and not intent.orphan(c.data)]
        if not before and abs(old - lo) < 1e-3:
            # it opens its chapter: moving it would leave a hole at the chapter start (re-flow moves chapters)
            self.notes.append(f"{clip.address}: opens its chapter at {lo:.3f} s; its moment ({t:.3f} s) is kept for re-flow, not applied")
            return False
        end = clip.end
        tag = intent.sequence(clip.data)
        if tag is not None:  # a sequence's first step: the whole sequence moves, its shape intact (re-time fits it)
            delta = t - old
            for step in self.clips(shot=clip.shot_id):
                if (intent.sequence(step.data) or ("",))[0] == tag[0] and step.data is not clip.data:
                    step._set_start(step.start + delta)
            clip._set_start(t)
        else:
            clip._set_start(t)
            if keep_end:
                _set_length(clip.data, end - t)
        for prev in before:
            _set_length(prev.data, t - prev.start)
        moved[id(clip.data)] = (clip.address, old, t)
        return True

    def _place(self, clip: "Clip", start: float, end: float | None, moved: dict) -> None:
        old_start, old_end = clip.start, clip.end
        changed = False
        if round(start * self.fps) != round(old_start * self.fps):
            clip._set_start(self._frame(start + 1e-6))
            changed = True
        if end is not None and not clip.is_audio and "from" not in clip.data:
            length = self._frame(end + 1e-6) - clip.start
            if length > 0.5 / self.fps and round(length * self.fps) != round(clip.duration * self.fps):
                _set_length(clip.data, length)
                changed = True
        if changed and id(clip.data) not in moved:
            moved[id(clip.data)] = (clip.address, old_start, clip.start)
            self._moved_ends[clip.address] = (old_end, clip.end)

    def _fit_sequences(self) -> list[str]:
        """Re-lay every sequence that has a fit (see ``timeline_intent.sequence_fit``) so it lands on its word."""
        groups: dict[str, list[Clip]] = {}
        for clip in self.clips():
            tag = intent.sequence(clip.data)
            if tag:
                groups.setdefault(tag[0], []).append(clip)
        changes = []
        for seq_id, steps in groups.items():
            steps.sort(key=lambda c: intent.sequence(c.data)[1])
            fit = intent.sequence_fit(steps[0].data)
            if not fit or "land" not in fit:
                continue
            progress = fit_curve_progress(str(fit.get("curve", "linear")))
            spec = fit.get("land")
            start, end = steps[0].start, steps[-1].end
            total = int(round((end - start) * self.fps))
            if isinstance(spec, str):  # a moment: "lifetime"
                try:
                    land_t = mo.resolve(mo.parse(spec), _MomentContext(self, steps[0]))
                except mo.MomentError as exc:
                    self.notes.append(f"{steps[0].address}: fit until {spec}: {exc}; the sequence is left as it is")
                    continue
                land_name = spec
            else:  # older form {word, text}
                land = self._as_word(str((spec or {}).get("word") or "")) or self.word(str((spec or {}).get("text")))
                land_t, land_name = land.start, repr(land.text)
            life = int(round((land_t - start) * self.fps)) + int(fit.get("offset_frames") or 0)
            cycle = list(fit.get("cycle") or [steps[0].asset])
            lead = [int(n) for n in fit.get("lead") or []]
            race_frames = [int(n) for n in fit.get("race") or [2]]
            if any(n < 1 for n in lead + race_frames):
                raise TimelineEditError(f"sequence {seq_id}: lead and race steps need at least one frame")
            if life < sum(lead) or life >= total:
                raise TimelineEditError(f"sequence {seq_id}: span is too short for its lead, race and landing")
            plan = [(cycle[i % len(cycle)], n) for i, n in enumerate(lead)]
            left = life - sum(lead)
            race: list[int] = []
            k = 0
            while left > 0:
                n = race_frames[min(k, len(race_frames) - 1)]
                race.append(min(n, left))
                left -= race[-1]
                k += 1
            if race:
                race = allocate_fit_frames(race, life - sum(lead), progress)
            plan += [(cycle[(len(cycle) - 1 - (len(race) - 1 - i)) % len(cycle)], n) for i, n in enumerate(race)]
            plan.append((fit.get("then") or cycle[-1], total - life))
            current = [(c.asset, int(round(c.duration * self.fps))) for c in steps]
            if current == plan:
                continue
            template = copy.deepcopy(steps[0].data)
            sid = steps[0].shot_id
            rows = self._internal(sid)["clips"]
            at = _num(template.get("at"))
            for c in steps:
                c.remove()
            frame = 0
            for i, (asset, n) in enumerate(plan):
                new = copy.deepcopy(template)
                new["id"] = f"{seq_id}-{i:02d}"
                new["asset"] = self._register_asset(sid, asset)
                new["at"] = _r(at + frame / self.fps)
                new.pop("from", None), new.pop("to", None)
                new["hold"] = _r(n / self.fps)
                set_fit = intent.sequence_fit(new) if i == 0 else None
                intent.set_sequence_fit(new, set_fit)
                intent.set_sequence(new, seq_id, i)
                rows.append(new)
                frame += n
            changes.append(f"sequence {seq_id}: {len(steps)} → {len(plan)} steps, lands on {land_name} at frame {life}")
        return changes

    def resolve(self) -> list[str]:
        """Re-resolve every formula in the document (like a spreadsheet recalculating).

        An anchor puts a clip's start on a word (see ``retime``). A clip's formulas map a
        field path to an expression, and the resolved value is written to that field:

        - ``{"word": "n20b:20", "text": "viral", "offset_s": 0, "as": "clip_seconds"|"clip_frame"|"timeline_seconds"}``
          (``edge: "end"`` for the word's end; then, in order: ``offset_frames``, ``min``/``max``, ``step``
          (down to a multiple, e.g. 2 for stepFrames 2); ``snap: "floor"|"round"`` puts seconds on a frame)
        - ``{"words_of": "n20b"}``: ``[[start, end], …]`` of that line's words, clip-relative (presenter lip-sync)
        - ``{"mark": "B2-HAND", "axis": "x"|"y", "offset": -96, "unit": "canvas"}``: a slot's hand mark plus an
          offset in canvas px (older ``"logical"``/``"px"`` offsets still read)

        Paths look like ``params.words``, ``params.punchAt[0]``, ``params.keyframes[2].frame``. Returns what changed.
        """
        self._outside = []
        changes = [f"{cid}: start {old:.3f} → {new:.3f} s (anchored to its word)" for cid, old, new in self.retime()]
        changes += self._fit_sequences()
        words = self.words()
        by_id = {w.id: w for w in words}
        slots = self.slots
        for clip in self.clips():
            formulas = intent.formulas(clip.data)
            if not formulas:
                continue
            for path, expr in formulas.items():
                try:
                    value = self._evaluate(clip, expr, by_id, words, slots, path)
                except (TimelineEditError, mo.MomentError, ValueError, KeyError, TypeError) as exc:
                    previous_at = None  # a param's moment: pin to the occurrence nearest where the param was
                    current = _get_path(clip.data, path)
                    if isinstance(expr, Mapping) and isinstance(current, (int, float)) and not isinstance(current, bool):
                        previous_at = clip.start + (current / self.fps if expr.get("as") == "clip_frame" else
                                                    0.0 if expr.get("as") == "timeline_seconds" else current)
                        if expr.get("as") == "timeline_seconds":
                            previous_at = float(current)
                    pinned = self._pin_nearest(expr.get("moment"), clip, previous_at) if isinstance(expr, Mapping) and expr.get("moment") else None
                    if pinned is not None:
                        expr = {**expr, "moment": pinned}
                        intent.set_formula(clip.data, path, expr)
                        self.notes.append(f"{clip.address}.{path.removeprefix('params.')}: now pinned to {pinned}, the one nearest it")
                        try:
                            value = self._evaluate(clip, expr, by_id, words, slots, path)
                        except (TimelineEditError, mo.MomentError, ValueError, KeyError, TypeError) as again:
                            self.notes.append(f"{clip.address}.{path.removeprefix('params.')}: {again} (kept its last value)")
                            continue
                    else:
                        self.notes.append(f"{clip.address}.{path.removeprefix('params.')}: {exc} (kept its last value)")
                        continue
                unit = expr.get("as") if isinstance(expr, Mapping) else None
                length = clip.duration * (self.fps if unit == "clip_frame" else 1.0)
                if isinstance(expr, Mapping) and expr.get("moment") and unit in ("clip_frame", "clip_seconds") \
                        and isinstance(value, (int, float)) and not (-1e-6 <= value <= length + 1e-6):
                    self._outside.append(f"{clip.address}.{path.removeprefix('params.')} = {expr['moment']} → "
                                         f"{'frame' if unit == 'clip_frame' else 's'} {value}, outside the clip "
                                         f"(0–{length:g}): re-home it on a moment inside {clip.start:.2f}–{clip.end:.2f} s")
                if _set_path(clip.data, path, value):
                    changes.append(f"{clip.id}.{path} = {json.dumps(value)[:60]}")
        from astrid.sdk.timeline.loops import resolve_loops
        changes += resolve_loops(self)
        return changes

    def _evaluate(self, clip: Clip, expr: Any, by_id: Mapping[str, Word], words: Sequence[Word], slots: Mapping[str, Any],
                  path: str = "") -> Any:
        if not isinstance(expr, Mapping):
            raise TimelineEditError(f"a formula must be an object, got {expr!r}")
        if "moment" in expr:  # a moment in the grammar: "lifetime" -50f, beat 2 after "Astrid", c26 …
            try:
                t = mo.resolve(mo.parse(expr["moment"]), _MomentContext(self, clip))
            except mo.MomentError as exc:
                raise TimelineEditError(f"{expr['moment']}: {exc}") from None
            unit = expr.get("as", "clip_seconds")
            if unit == "clip_frame":
                return _post(int(math.floor((t - clip.start) * self.fps + 1e-6)), expr)
            value = t if unit == "timeline_seconds" else t - clip.start
            if expr.get("snap") == "floor":
                value = math.floor(value * self.fps + 1e-6) / self.fps
            elif expr.get("snap") == "round":
                value = round(value * self.fps) / self.fps
            return _r(_post(value, expr))
        if "word" in expr:
            word = by_id.get(str(expr["word"]))
            if word is None or (expr.get("text") and _norm(word.text) != _norm(expr["text"])):
                same = [w for w in words if _norm(w.text) == _norm(expr.get("text"))]
                if not same:
                    raise TimelineEditError(f"word {expr.get('text')!r} is no longer spoken")
                word = min(same, key=lambda w: abs(w.start - clip.start))
            t = (word.end if expr.get("edge") == "end" else word.start) + _num(expr.get("offset_s"))
            unit = expr.get("as", "clip_seconds")
            if unit == "clip_frame":
                return _post(int(round((t - clip.start) * self.fps)), expr)
            value = t if unit == "timeline_seconds" else t - clip.start
            if expr.get("snap") == "floor":  # down to a whole frame (a hold that must end before the word)
                value = math.floor(value * self.fps + 1e-9) / self.fps
            elif expr.get("snap") == "round":  # to the nearest frame
                value = round(value * self.fps) / self.fps
            return _r(_post(value, expr))
        if "words_of" in expr:
            segment = str(expr["words_of"])
            return [[_r(w.start - clip.start), _r(w.end - clip.start)] for w in words
                    if w.segment == segment and w.end > clip.start and w.start < clip.end]
        if "mark" in expr:
            slot = slots.get(str(expr["mark"])) or {}
            mark = slot.get("hand_mark") or {}
            axis = str(expr.get("axis", "x"))
            if axis not in mark:
                raise TimelineEditError(f"slot {expr['mark']!r} has no hand_mark.{axis}")
            # one arithmetic for every unit: canvas px (the mark + the offset), then the element's declared grid
            canvas = _num(mark[axis]) + mark_offset_canvas(expr)
            scale, paths = element_units(clip.element)
            if (_unit_path(path) if path else axis) in paths:
                return round(canvas / scale)
            if expr.get("unit") == "logical":  # an old logical formula on an element that declares no grid
                return round(canvas / LOGICAL_PX)
            return round(canvas)
        raise TimelineEditError(f"unknown formula {expr!r}")

    def _shift_after(self, t: float, delta: float, *, cut_window: tuple[float, float] | None,
                     skip: Sequence[Mapping[str, Any]] = ()) -> float:
        """Ripple core: one time change, inside the re-time scope (the music bed is cut at the scope's end)."""
        with retime_scope(self):
            return self._shift_now(t, delta, cut_window=cut_window, skip=skip)

    def _shift_now(self, t: float, delta: float, *, cut_window: tuple[float, float] | None,
                   skip: Sequence[Mapping[str, Any]] = ()) -> float:
        """``delta < 0`` removes ``cut_window`` (it may cross shots); ``delta > 0`` opens time at ``t``."""
        skip_ids = {id(c) for c in skip}
        rows = sorted(self.bundle.get("placements") or [], key=_row_start)
        if delta > 0:
            self._record_change(t, t, delta, skip_ids)
            for row in rows:
                s = _row_start(row) / 1000.0
                e = s + _num(row.get("duration_ms")) / 1000.0
                if s >= t - 1e-6:
                    _set_row_start(row, s + delta)
                elif e > t + 1e-6:
                    self._open_in_shot(str(row.get("shot_id")), t - s, delta, skip_ids)
                    row["duration_ms"] = int(round((e - s + delta) * 1000))
            self._shift_parent(t, delta)
            return _r(delta)
        a, b = cut_window or (t, t)
        self._record_change(a, b, -(b - a), skip_ids)
        removed = 0.0
        for row in reversed(rows):  # right to left, so earlier positions stay valid
            s = _row_start(row) / 1000.0
            e = s + _num(row.get("duration_ms")) / 1000.0
            if e <= a + 1e-6 or s >= b - 1e-6:
                continue
            ra, rb = max(a - s, 0.0), min(b - s, e - s)
            d = rb - ra
            self._close_in_shot(str(row.get("shot_id")), ra, rb, skip_ids)
            row["duration_ms"] = int(round((e - s - d) * 1000))
            for later in rows:
                if _row_start(later) / 1000.0 >= e - 1e-6 and later is not row:
                    _set_row_start(later, _row_start(later) / 1000.0 - d)
            removed += d
        self._shift_parent(b, -removed)
        return _r(removed)

    def _shot_sequences(self, sid: str) -> list[list[dict[str, Any]]]:
        """The sequences (a time-lapse's steps) of one shot, each in step order."""
        groups: dict[str, list[dict[str, Any]]] = {}
        for clip in self._internal(sid)["clips"]:
            tag = intent.sequence(clip)
            if tag and not intent.orphan(clip):
                groups.setdefault(tag[0], []).append(clip)
        return [sorted(steps, key=lambda c: intent.sequence(c)[1]) for steps in groups.values()]

    def _relay_steps(self, steps: list[dict[str, Any]], at: float, length: float, frames: list[int] | None = None) -> None:
        """Lay a sequence's steps end to end from ``at`` (shot seconds) over ``length``, keeping their
        frame ratios (``frames``, else their lengths now; each at least one frame): one unit."""
        recipe = intent.sequence_fit(steps[0]) or {}
        frames = (recipe.get("weights") if "land" not in recipe else None) or frames
        frames = frames or [max(1, int(round(clip_duration(c) * self.fps))) for c in steps]
        if len(frames) != len(steps):
            raise TimelineEditError("sequence fit weights must match its membership")
        # Generated recipes own a race-only curve in _fit_sequences. The
        # full-span relay preserves lead/landing and the already rounded race.
        progress = None if "land" in recipe else fit_curve_progress(str(recipe.get("curve", "linear")))
        total = int(round(length * self.fps))
        plan = allocate_fit_frames(frames, total, progress)
        frame = int(round(at * self.fps))
        for clip, n in zip(steps, plan):
            clip["at"] = _r(frame / self.fps)
            _set_length(clip, n / self.fps)
            frame += n

    def _sequences_across(self, sid: str, ra: float, rb: float, skip_ids: set[int]) -> list[list[dict[str, Any]]]:
        """Sequences a ripple at [ra, rb] would tear (it starts inside them): they move as units instead."""
        out = []
        for steps in self._shot_sequences(sid):
            if any(id(c) in skip_ids for c in steps):
                continue
            s0 = _num(steps[0].get("at"))
            e1 = max(_num(c.get("at")) + clip_duration(c) for c in steps)
            if s0 < ra - 0.5 / self.fps and e1 > ra + 0.5 / self.fps:
                out.append(steps)
        return out

    def _doomed_cuts(self, sid: str, ra: float, rb: float, skip_ids: set[int]) -> set[str]:
        """Cuts whose every picture clip in this shot lies wholly inside the window [ra, rb] and would be removed
        with it: an anchored clip on such a cut has nothing left to follow."""
        from astrid.core.timeline import onscreen
        from astrid.sdk.timeline.check import element_coverage_cached

        tol = 0.5 / self.fps
        dying: dict[str, bool] = {}
        clips = self._internal(sid)["clips"]
        elements = element_coverage_cached({c["clipType"] for c in clips if c.get("clipType")})
        for clip in clips:
            cid = intent.cut_of(clip)
            if not cid or self._is_audio(sid, clip):
                continue
            # a picture (or a background plate, which still covers the canvas), decided by element data
            if onscreen.role_of(clip, clip.get("track"), elements) not in ("picture", "background"):
                continue
            cs = _num(clip.get("at"))
            ce = cs + clip_duration(clip)
            dies = id(clip) not in skip_ids and not intent.orphan(clip) and cs >= ra - tol and ce <= rb + tol
            dying[cid] = dying.get(cid, True) and dies
        return {cid for cid, dies in dying.items() if dies}

    def _anchor_survives(self, sid: str, clip: dict[str, Any], doomed: set[str]) -> bool:
        """Whether a clip's anchor still exists now: its moment still resolves, else its cut is not doomed."""
        cut = intent.cut_of(clip)
        if cut and cut in doomed:  # its cut is going too: a moment relative to it cannot survive
            return False
        moment = intent.on(clip)
        if moment:
            try:
                self._moment_time(moment, Clip(self, sid, clip))
            except TimelineEditError:
                return False
            return True
        return bool(cut)

    def _close_in_shot(self, sid: str, ra: float, rb: float, skip_ids: set[int]) -> None:
        d = rb - ra
        doomed = self._doomed_cuts(sid, ra, rb, skip_ids)
        for steps in self._shot_sequences(sid):  # a window closing over part of a sequence shrinks it whole
            if any(id(c) in skip_ids for c in steps):
                continue
            s0 = _num(steps[0].get("at"))
            e1 = max(_num(c.get("at")) + clip_duration(c) for c in steps)
            overlap = max(0.0, min(e1, rb) - max(s0, ra))
            whole = s0 >= ra - 0.5 / self.fps and e1 <= rb + 0.5 / self.fps
            if overlap <= 0.5 / self.fps or whole:
                continue  # untouched (or wholly removed: the clip loop below handles it)
            start = s0 if s0 < ra else ra
            self._relay_steps(steps, start, max(len(steps) / self.fps, (e1 - s0) - overlap))
            skip_ids = skip_ids | {id(c) for c in steps}
        clips = self._internal(sid)["clips"]
        out = []
        for clip in clips:
            if id(clip) in skip_ids:
                out.append(clip)
                continue
            cs = _num(clip.get("at"))
            ce = cs + clip_duration(clip)
            audio = self._is_audio(sid, clip)
            if ce <= ra + 1e-6:
                out.append(clip)
            elif cs >= rb - 1e-6:
                clip["at"] = _r(cs - d)
                out.append(clip)
            elif intent.orphan(clip):  # an orphan waiting to be re-homed: never trimmed by a later edit
                out.append(clip)
            elif audio and self._music == "keep_end" and self._is_music(sid, clip) and cs < ra - 1e-6 and ce > ra + 1e-6:
                out.append(clip)  # keep_end: the bed is cut once, at the end of the re-time (_apply_bed_cuts)
            elif cs >= ra - 0.5 / self.fps and ce <= rb + 0.5 / self.fps:  # inside, to the frame (no sub-frame stubs)
                if self._anchor_survives(sid, clip, doomed):  # its anchor is still there: it follows it
                    clip["at"] = _r(ra)
                    out.append(clip)
                    continue
                if self._keep_inside:  # kept where it was, length and all: an orphan to re-home or remove
                    intent.set_orphan(clip, self._orphan_reason)
                    out.append(clip)
                    self._kept.append(str(clip.get("id")))
                continue  # wholly inside the removed window
            elif cs < ra and ce > rb:
                if audio and self._is_music(sid, clip) and self._music == "plays_on":
                    self.notes.append(f"music {clip.get('id')}: the bed plays on unchanged over the cut at "
                                      f"{self._shot_start(sid) + ra:.2f} s; what follows moved {-d:+.2f} s against its beats")
                    out.append(clip)
                elif audio:
                    out.extend(_split_audio(clip, ra - cs, cut=d))
                else:
                    _set_length(clip, (ce - cs) - d)
                    out.append(clip)
            elif cs < ra:  # ends inside the window
                _trim_end(clip, ra - cs)
                out.append(clip)
            else:  # starts inside the window
                _trim_front(clip, rb - cs)
                clip["at"] = _r(ra)
                out.append(clip)
        clips[:] = out

    def _open_in_shot(self, sid: str, ra: float, d: float, skip_ids: set[int]) -> None:
        for steps in self._sequences_across(sid, ra, ra, skip_ids):  # time opened inside a sequence stretches it whole
            s0 = _num(steps[0].get("at"))
            e1 = max(_num(c.get("at")) + clip_duration(c) for c in steps)
            self._relay_steps(steps, s0, (e1 - s0) + d)
            skip_ids = skip_ids | {id(c) for c in steps}
        clips = self._internal(sid)["clips"]
        out = []
        for clip in clips:
            if id(clip) in skip_ids:
                out.append(clip)
                continue
            cs = _num(clip.get("at"))
            ce = cs + clip_duration(clip)
            if cs >= ra - 1e-6:
                clip["at"] = _r(cs + d)
            elif ce > ra + 1e-6:
                if self._is_music(sid, clip):
                    if self._music == "plays_on":
                        self.notes.append(f"music {clip.get('id')}: the bed plays on unchanged over the {d:.2f} s opened at "
                                          f"{self._shot_start(sid) + ra:.2f} s; what follows moved {d:+.2f} s against its beats")
                    out.append(clip)  # keep_end: cut once at the end of the re-time (_apply_bed_cuts)
                    continue
                if self._is_audio(sid, clip):
                    if intent.line(clip):
                        out.extend(_split_audio(clip, ra - cs, gap=d))
                        continue
                    out.append(clip)  # a sound effect already playing finishes where it is
                    continue
                _set_length(clip, (ce - cs) + d)  # picture spanning the point holds longer
            out.append(clip)
        clips[:] = out

    def _is_music(self, sid: str, clip: Mapping[str, Any]) -> bool:
        return self._is_audio(sid, clip) and (str(clip.get("track") or "") == "music" or bool(intent.beat_sources(clip)))

    def _record_change(self, a: float, b: float, d: float, skip_ids: set[int]) -> None:
        """keep_end: note a time change in the open scope (film second ``a``; ``d`` seconds removed over ``a``..``b``
        when < 0, opened at ``a`` when > 0) and move the changes already noted into the timeline after it: one
        after the removed window moves back by ``d``, one inside it moves to ``a``, one after an opening moves on."""
        if self._scope is None or self._music != "keep_end" or abs(d) < 1e-9:
            return
        moved = []
        for p, dp, sk in self._scope:
            if d < 0 and p >= b - _EPS:
                p = p + d
            elif d < 0 and p > a + _EPS:
                p = a
            elif d > 0 and p > a + _EPS:
                p = p + d
            moved.append((p, dp, sk))
        moved.append((a, d, frozenset(skip_ids)))
        self._scope[:] = moved

    def _apply_bed_cuts(self, records: list[tuple[float, float, frozenset]]) -> list[str]:
        """keep_end, at the end of a re-time: each music bed is cut once per cluster of changes that spans it
        (changes less than a bar of the bed apart are one cut with their net delta, see ``_bed_clusters``), in
        ascending order, so the bed's ending lands on the film's end. The times are those of the finished film.
        Returns the notes (also appended to ``notes``)."""
        if not records:
            return []
        frame = 1.0 / self.fps
        rows = sorted(self.bundle.get("placements") or [], key=_row_start)
        out: list[str] = []
        for row in rows:
            sid = str(row.get("shot_id"))
            start = _row_start(row) / 1000.0  # a shot keeps its start: the bed's at is shot-relative
            clips = self._internal(sid)["clips"]
            for clip in list(clips):
                if not self._is_music(sid, clip):
                    continue
                bars = intent.beat_sources(clip, "downbeat")
                beats = intent.beat_sources(clip)
                mine = [(p, d) for p, d, sk in records if id(clip) not in sk]
                pieces = [dict(clip, at=_r(start + _num(clip.get("at"))))]  # film-timed while cutting
                cut_any = False
                for t, net in _bed_clusters(mine, _bar_len(bars), frame):
                    i = next((k for k, p in enumerate(pieces)
                              if _num(p.get("at")) < t < _num(p.get("at")) + clip_duration(p)), None)
                    if i is None:
                        continue  # the change is not inside this bed
                    cut = plan_keep_end(pieces[i], at=t, delta=net, bars=bars, beats=beats)
                    if not cut.overlap:
                        continue
                    pieces[i:i + 1] = cut.pieces
                    cut_any = True
                    out.append(f"music {clip.get('id')}: {cut.note.removeprefix('music: ')}")
                if cut_any:
                    idx = next(i for i, c in enumerate(clips) if c is clip)
                    # A new piece never takes an id another clip of the shot already has (an
                    # earlier re-flow's piece, an orphan kept from it): the shot would be invalid.
                    taken = {str(c.get("id")) for c in clips if c is not clip}
                    placed = []
                    for p in pieces:
                        p = {**p, "at": _r(_num(p.get("at")) - start)}
                        pid, n = str(p.get("id")), 2
                        while str(p.get("id")) in taken:
                            p["id"], n = f"{pid}-{n}", n + 1
                        taken.add(str(p.get("id")))
                        placed.append(p)
                    clips[idx:idx + 1] = placed
        self.notes.extend(out)
        return out

    def _shift_parent(self, after: float, delta: float) -> None:
        for clip in (self.bundle.get("parent") or {}).get("clips") or []:
            if _num(clip.get("at")) >= after - 1e-6:
                clip["at"] = _r(_num(clip.get("at")) + delta)
