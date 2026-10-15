"""Checkout: structural edits (add, cuts, notes, ripple), slots and stand-ins, assets and beats."""
from __future__ import annotations

import copy
import difflib
import re
from pathlib import Path
from typing import Any, Mapping, Sequence

from astrid.core.timeline import moments as mo
from astrid.sdk import timeline_intent as intent

from astrid.sdk.timeline import store as _store
from astrid.sdk.timeline._common import (CANVAS, ELEMENT_TRACK, SAFE_MARGIN, TIME_RE, TimelineEditError, _num, _r)
from astrid.sdk.timeline.doc import (_free_key, _set_length)
from astrid.sdk.timeline.model import (Clip, Cut, Word, music_keyword, validate_param_value)

_ASSET_REGISTRY_FIELDS = ("media_id", "content_sha256", "type", "duration", "resolution", "size", "size_bytes",
                          "handle", "filename", "pixel_provenance")


def _media_kind(entry: Mapping[str, Any]) -> str | None:
    """Normalize registry MIME types and legacy kind names to image/audio/video."""
    value = str(entry.get("type") or "").strip().lower().split(";", 1)[0]
    if "/" in value:
        value = value.split("/", 1)[0]
    if value.startswith(("image", "audio", "video")):
        return value.split("-", 1)[0]
    return value if value in {"image", "audio", "video"} else None


class _EditMixin:
    """Checkout: structural edits (add, cuts, notes, ripple), slots and stand-ins, assets and beats."""

    # ---- structural edits ---------------------------------------------------
    def add(self, element: str, *, at: Any, hold: float | None = None, asset: Any = None, params: Mapping[str, Any] | None = None,
            track: str | None = None, corner: str | None = None, anchor: bool = True, clip_id: str | None = None,
            standin: Any = None, layer: str | None = None, cut: str | None = None) -> Clip:
        """Add a layer at a moment or a time: ``at='"Building" in v27'`` (any moment: it starts ON it and follows
        it on re-flow), ``at=93.5``, or a Word. It joins the cut on screen then (or ``cut=``) under ``layer=``
        (default: its asset or element name, made unique), so its address is ``c41.NAME`` and the sheet shows it.
        ``hold=`` is a literal length (``for``); without it the layer ends with its cut.
        ``corner`` (top-right …) places it inside title-safe, off a centred face.

        ``standin``: if ``asset`` is not available yet (a file still to be made), use this asset
        meanwhile and remember the real one; ``fill_standins()`` swaps it in once it exists."""
        for key, value in dict(params or {}).items():  # every value is checked before the clip exists (all-or-nothing)
            validate_param_value(element, key, value)
        word = at if isinstance(at, Word) else None
        moment = None
        if word is None and isinstance(at, str) and not TIME_RE.match(at.strip()):
            try:
                moment = mo.format_moment(mo.parse(at))
            except mo.MomentError as exc:
                raise TimelineEditError(f"at={at!r}: {exc}") from None
        start = self.quantize(word.start if word else self._moment_time(moment) if moment else self.time(at))
        sid = self._shot_at(start)
        internal = self._internal(sid)
        new = {"id": clip_id or self._new_id(element), "clipType": element, "track": track or ELEMENT_TRACK.get(element, "fx"),
               "at": 0.0, "hold": self.quantize(hold if hold is not None else 1.0), "params": {}}
        if not any(str(t.get("id")) == new["track"] for t in internal.get("tracks") or []):
            raise TimelineEditError(f"shot {sid} has no track {new['track']!r}; tracks: {', '.join(str(t.get('id')) for t in internal.get('tracks') or [])}")
        if asset is not None:
            try:
                new["asset"] = self._register_asset(sid, asset)
            except TimelineEditError:
                if standin is None:
                    raise
                new["asset"] = self._register_asset(sid, standin)
                intent.set_standin(new, str(asset), {"element": element, "params": copy.deepcopy(dict(params or {}))})
            if element == "media" and self._is_audio(sid, new):
                entry = self._registry(sid).get(str(new.get("asset"))) or {}
                source_duration = _num(entry.get("duration")) or None
                if isinstance(asset, (str, Path)) and Path(str(asset)).expanduser().is_file():
                    source_duration = _store.media_seconds(str(self.bundle.get("project_id") or ""), asset)
                if source_duration:
                    entry["duration"] = source_duration
                    source_out = source_duration if hold is None else float(hold) * (_num(new.get("speed"), 1.0) or 1.0)
                    if source_out > source_duration + 0.5 / self.fps:
                        raise TimelineEditError(f"{new['id']}: requested source end {source_out:.3f} s exceeds the file limit "
                                                f"{source_duration:.3f} s")
                    new["from"], new["to"] = 0.0, self.quantize(source_out)
                    new["hold"] = self.quantize(source_out / (_num(new.get("speed"), 1.0) or 1.0))
        cut_id = (self._cut_id(cut) or cut) if cut else (None if self._is_audio(sid, new) else self._cut_on_screen(start))
        name = layer
        if cut_id:
            taken = {intent.layer_of(c.data) for c in self.clips() if intent.cut_of(c.data) == cut_id}
            if layer and layer in taken:
                raise TimelineEditError(f"{cut_id} already has a layer named {layer!r}; give it another name")
            base = layer or re.sub(r"[^a-z0-9]+", "-", str(new.get("asset") or element.removeprefix("am-")).lower()).strip("-") or "layer"
            name, k = base, 2
            while name in taken:
                name, k = f"{base}-{k}", k + 1
        internal["clips"].append(new)
        self._mcache = None
        clip = Clip(self, sid, new)
        clip._check_param_keys(list(params or {}))
        for key, value in dict(params or {}).items():  # the same path as clip.set: canvas px, formulas, moments
            clip.set_param(key, value)
        if corner:
            new["params"].update(self._corner(element, new["params"], new.get("asset"), sid, corner))
        if moment and anchor:  # placed first, then it joins its cut (a time is not read as an offset into it)
            clip.on(moment)
        else:
            clip.enter_at(word if word and anchor else start)
        if cut_id:
            intent.set_cut(new, cut_id)
            if hold is not None:
                intent.set_for(new, self.quantize(hold))
        if name:
            intent.set_layer(new, name)
        self._mcache = None
        if layer and intent.layer_of(new) != layer:
            self.notes.append(f"added as {clip.address} (not {layer!r})")
        return clip

    def add_cut(self, on: Any, *, after: str | None = None, id: str | None = None, picture: Any = None) -> "Cut":
        """A new cut on a moment: it splits the cut on screen there (see ``Cut.split``). ``after=`` names the
        cut you expect it to follow (checked); ``picture=`` gives it its own picture asset."""
        moment = self._split_moment(on, None)
        t = self._moment_time(moment, None, in_point=True)
        host = self._cut_on_screen(t)
        if host is None:
            raise TimelineEditError(f"no named cut is on screen at {t:.3f} s to split")
        if after and (self._cut_id(after) or after) != host:
            raise TimelineEditError(f"{moment} ({t:.3f} s) is inside {host}, not {after}: the new cut would follow {host}")
        return self.cut(host).split(moment, id=id, picture=picture)

    def _split_moment(self, on: Any, cut_id: str | None) -> str:
        if isinstance(on, Word):
            return mo.format_moment(mo.word_moment(on, self.words()))
        if isinstance(on, (int, float)) or (isinstance(on, str) and TIME_RE.match(on.strip())):
            t = self.time(on)  # a time: say it against the cut it splits, so it moves with it
            host = cut_id or self._cut_on_screen(t)
            if host is None:
                raise TimelineEditError(f"no cut is on screen at {t:.3f} s")
            lo = self._cut_spans()[host][0]
            return f"{host} {mo.offset_text(t - lo, self.fps)}".strip()
        try:
            return mo.format_moment(mo.parse(on))
        except mo.MomentError as exc:
            raise TimelineEditError(f"{on!r}: {exc}") from None

    def _next_cut_id(self, cut_id: str) -> str:
        m = re.fullmatch(r"(c\d+)([a-z]?)", cut_id)
        if not m:
            raise TimelineEditError(f"{cut_id!r} is not a cut id like c33")
        taken = {g["id"] for g in self._cut_groups()} | {intent.cut_of(c.data) for c in self.clips() if intent.cut_of(c.data)}
        for code in range(ord(m.group(2)) + 1 if m.group(2) else ord("a"), ord("z") + 1):
            if f"{m.group(1)}{chr(code)}" not in taken:
                return f"{m.group(1)}{chr(code)}"
        raise TimelineEditError(f"no free cut id after {cut_id}; name it with id=")

    def _split_cut(self, cut: "Cut", on: Any, *, new_id: str | None, picture: Any) -> "Cut":
        pic = cut.picture
        cid = intent.cut_of(pic.data) if pic is not None else None
        if pic is None or not cid:
            raise TimelineEditError(f"cut {cut.n} has no named picture to split (import its intent first)")
        if intent.sequence(pic.data):
            raise TimelineEditError(f"{cid} is a sequence (a time-lapse): split one of its steps' cuts instead")
        moment = self._split_moment(on, cid)
        t = self._frame(self._moment_time(moment, None, in_point=True))
        lo, hi = self._cut_spans()[cid]
        hi = hi if hi is not None else self.duration
        if not lo + 1.0 / self.fps <= t <= hi - 1.0 / self.fps:
            raise TimelineEditError(f"{moment} is at {t:.3f} s, not inside {cid} ({lo:.3f}–{hi:.3f} s): a split needs a "
                                    "moment inside the cut")
        new_id = new_id or self._next_cut_id(cid)
        if new_id in {g["id"] for g in self._cut_groups()}:
            raise TimelineEditError(f"there is already a cut {new_id}")
        old_picture_end = pic.end  # before anything changes: what the new picture must not run past
        data = copy.deepcopy(pic.data)
        data["id"] = self._new_id(f"{new_id}-00-{pic.element}")
        for setter in (intent.set_until, intent.set_why, intent.set_orphan):
            setter(data, None)
        intent.set_for(data, None)
        intent.set_cut(data, new_id)
        if "from" in data or "to" in data:  # footage continues where it was at the split
            speed = _num(data.get("speed"), 1.0) or 1.0
            data["from"] = _r(_num(data.get("from")) + (t - pic.start) * speed)
        if picture is not None:
            data["asset"] = self._register_asset(pic.shot_id, picture)
        data["at"] = _r(t - self._shot_start(pic.shot_id))
        _set_length(data, old_picture_end - t)  # what remains of the old picture: a split never adds length
        self._internal(pic.shot_id)["clips"].append(data)
        self._mcache = None
        new_pic = Clip(self, pic.shot_id, data)
        moved, carried = [], []
        for clip in self.clips():
            if clip.data is pic.data or clip.data is data or intent.cut_of(clip.data) != cid or intent.orphan(clip.data):
                continue
            if clip.start >= t - 0.5 / self.fps:
                intent.set_cut(clip.data, new_id)
                moved.append(clip.address)
            elif clip.end > t + 0.5 / self.fps and not intent.until(clip.data) and intent.for_s(clip.data) is None \
                    and not clip.is_audio:
                intent.set_until(clip.data, f"end of {new_id}")  # it spans the split: carried over the new cut
                carried.append(clip.address)
        intent.set_on(data, moment)
        self._mcache = None
        self.retime()
        self.notes.append(f"split {cid} at {moment} ({t:.3f} s): new cut {new_id} ({new_pic.address})"
                          + (f"; moved {', '.join(moved)}" if moved else "")
                          + (f"; carried over it: {', '.join(carried)}" if carried else ""))
        return self.cut(new_id)

    def set_cut_note(self, cut: Any, *, why: str | None = None, hold: str | bool | None = None) -> dict[str, Any]:
        """A cut's notes, kept on the CUT (whatever clip is its picture): ``why`` (why the cut is there; it
        remembers the cut's layers then, so check can say when it may be stale) and ``hold``
        (``"deliberate"``/True: lint's HOLD and STILL leave it alone; ``"off"``/False clears it)."""
        cid = self._cut_id(str(cut)) or str(cut)
        if not any(intent.cut_of(c.data) == cid for c in self.clips()):
            raise TimelineEditError(self._no_cut(cid))
        current = self.cut_note(cid)  # an older note kept on a clip moves to the cut now
        intent.set_cut_note(self.bundle, cid, why=current.get("why"), why_layers=current.get("why_layers"),
                            hold=current.get("hold"))
        for clip in self.clips():
            if intent.cut_of(clip.data) == cid:
                intent.set_why(clip.data, None)
                intent._set_or_drop(clip.data, "why_layers", None)
        if why is not None:
            names = sorted(intent.layer_of(c.data) or c.id for c in self.clips() if intent.cut_of(c.data) == cid)
            intent.set_cut_note(self.bundle, cid, why=why or None, why_layers=names if why else None)
        if hold is not None:
            intent.set_cut_note(self.bundle, cid, hold="deliberate" if hold in (True, "deliberate", "on", "yes") else None)
        self._mirror_holds()
        return self.cut_note(cid)

    def cut_note(self, cut: Any) -> dict[str, Any]:
        """``{why, why_layers, hold}`` of a cut ({} when it has none)."""
        cid = self._cut_id(str(cut)) or str(cut)
        return self.cut_notes_all().get(cid, {})

    def cut_notes_all(self) -> dict[str, dict[str, Any]]:
        """Every cut's notes: the cut's own (``parent.config.cut_notes``), else an older note on one of its clips
        (picture first). Read-only: nothing moves until a note is written."""
        notes = intent.cut_notes(self.bundle)
        out = {cid: dict(note) for cid, note in notes.items()}
        for g in self._cut_groups():
            cid = g["id"]
            if cid in notes:
                continue
            pic = g["picture"]
            for clip in sorted(g["clips"], key=lambda c: (pic is None or c.data is not pic.data, c.start)):
                why, layers, held = intent.why(clip.data), intent.why_layers(clip.data), intent.deliberate(clip.data)
                note = out.setdefault(cid, {})
                if why and "why" not in note:
                    note["why"] = why
                    if layers:
                        note["why_layers"] = layers
                if held:
                    note["hold"] = "deliberate"
            if not out.get(cid):
                out.pop(cid, None)
        return out

    def _mirror_holds(self) -> None:
        """Lint reads a deliberate hold from a cut's picture: put the cut's hold on whichever clip is its picture
        now, and on no other clip of the cut."""
        notes = self.cut_notes_all()
        for g in self._cut_groups():
            held = notes.get(g["id"], {}).get("hold") == "deliberate"
            for clip in g["clips"]:
                on_it = held and g["picture"] is not None and clip.data is g["picture"].data
                if intent.deliberate(clip.data) != on_it:
                    intent.set_deliberate(clip.data, on_it)

    @music_keyword
    def ripple_delete(self, start: Any, end: Any, *, skip: Sequence[Mapping[str, Any]] = (), keep_inside: bool = False) -> float:
        """Remove the window [start, end) from the whole timeline and close it up.

        Clips after it move up by its length; picture clips that span it get shorter;
        audio that spans it (music, a VO tail) is split, so everything after stays in
        sync with its picture. Clips wholly inside the window are removed.
        Returns the seconds removed.
        """
        a, b = self.quantize(self.time(start)), self.quantize(self.time(end))
        if b <= a:
            raise TimelineEditError("the window to remove must have start < end")
        self._keep_inside = keep_inside
        try:
            return self._shift_after(a, -(b - a), cut_window=(a, b), skip=skip)
        finally:
            self._keep_inside = False

    @music_keyword
    def close_gap(self, *, before: Any, keep: float = 0.0) -> float:
        """Close the silence before a word (and ripple everything after it). Returns the seconds removed."""
        word = self._as_word(before) or self.word(str(before))
        previous = [w for w in self.words() if w.end <= word.start + 1e-6 and w.id != word.id]
        if not previous:
            raise TimelineEditError(f"nothing is said before {word.text!r}; there is no gap to close")
        gap_start = max(w.end for w in previous) + keep
        if word.start - gap_start < 1.0 / self.fps:
            raise TimelineEditError(f"the gap before {word.text!r} is already {max(0.0, word.start - gap_start + keep):.3f} s")
        reason = self._orphan_reason
        self._orphan_reason = "the silence it sat in was closed"
        try:
            removed = self.ripple_delete(gap_start, word.start, keep_inside=True)
        finally:
            self._orphan_reason = reason
        self.retime()  # kept clips land on their cut's new start
        return removed

    @music_keyword
    def insert_time(self, at: Any, seconds: float, *, skip: Sequence[Mapping[str, Any]] = ()) -> float:
        """Open ``seconds`` at a time and ripple everything after it later (picture spanning it is held longer)."""
        t = self.quantize(self.time(at))
        return self._shift_after(t, self.quantize(seconds), cut_window=None, skip=skip)

    # ---- slots (real footage to film) and stand-ins ---------------------------
    @property
    def slots(self) -> dict[str, Any]:
        """The timeline's real-footage slots: ``{id: {kind, spec, face_zone, hand_mark, media}}``."""
        return intent.slots(self.bundle)

    def slot_clips(self, slot_id: str) -> list[Clip]:
        return [c for c in self.clips() if intent.slot(c.data) == slot_id]

    def fill_slot(self, slot_id: str, media: Any, *, start_in_take: float = 0.0) -> list[str]:
        """Put the filmed take into a slot: every placeholder tagged with it becomes one ``am-footage`` clip
        per cut, with the same crop (zoom and focus); overlays stay where they are."""
        if slot_id not in self.slots:
            raise TimelineEditError(f"no slot {slot_id!r}; slots: {', '.join(self.slots) or 'none'}")
        slot = self.slots[slot_id]
        changed = []
        by_cut: dict[int, list[Clip]] = {}
        for clip in self.slot_clips(slot_id):
            cut = self.cut(f"@{clip.start + 1e-3}").n
            by_cut.setdefault(cut, []).append(clip)
        for n, placeholders in sorted(by_cut.items()):
            plate = next((c for c in placeholders if c.element in ("am-snap-plate", "am-footage")), placeholders[0])
            key = self._register_asset(plate.shot_id, media)
            zoom = _num(plate.params.get("zoom"), 1.0) or 1.0
            focus = plate.params.get("focus") or {}
            fx = _num(focus.get("x"), 160) / 320 if plate.element != "am-footage" else _num(focus.get("x"), 0.5)
            fy = _num(focus.get("y"), 90) / 180 if plate.element != "am-footage" else _num(focus.get("y"), 0.5)
            params = {"slot": slot_id, "kind": slot.get("kind", ""), "placeholder": False, "zoom": zoom,
                      "focus": {"x": round(fx, 4), "y": round(fy, 4)}, "in": _r(start_in_take + (plate.start - placeholders[0].start))}
            for k in ("mosaicIn", "mosaicOut", "faceZone", "handMark"):
                if k in plate.params:
                    params[k] = copy.deepcopy(plate.params[k])
            if isinstance(plate.params.get("push"), Mapping):
                params["push"] = {"to": _num(plate.params["push"].get("to"), zoom)}
            new = {"id": f"{plate.id}-real", "clipType": "am-footage", "track": plate.track, "asset": key,
                   "at": plate.data.get("at"), "hold": _r(plate.duration), "params": params}
            intent.set_slot(new, slot_id, replaces=[c.id for c in placeholders])
            for c in placeholders:
                c.remove()
            self._internal(plate.shot_id)["clips"].append(new)
            changed.append(f"cut {n}: {', '.join(c.id for c in placeholders)} → {new['id']}")
        slot["media"] = key if isinstance(key, str) else str(key)
        return changed

    def fill_standins(self) -> list[str]:
        """Swap every stand-in whose real asset now exists for the clip it stands in for."""
        changed = []
        for clip in self.clips():
            pending = intent.standin(clip.data)
            if not pending:
                continue
            wanted, intended = pending
            try:
                key = self._register_asset(clip.shot_id, wanted)
            except TimelineEditError:
                continue
            clip.data["clipType"] = intended.get("element", clip.element)
            clip.data["asset"] = key
            if isinstance(intended.get("params"), Mapping):
                clip.data["params"] = copy.deepcopy(dict(intended["params"]))
            intent.clear_standin(clip.data)
            changed.append(f"{clip.id}: stand-in → {key}")
        return changed

    def _registry(self, shot_id: str) -> dict[str, Any]:
        registry = self._internal(shot_id).setdefault("registry", {})
        return registry.setdefault("assets", {})

    def assets(self, key: str | None = None) -> list[dict[str, Any]]:
        """Registry assets with their timeline clip addresses, in key order."""
        entries: dict[str, dict[str, Any]] = {}
        for sid in self._shot_ids():
            for name, entry in self._registry(sid).items():
                if key is not None and name != key:
                    continue
                row = entries.setdefault(str(name), {"key": str(name), "media": copy.deepcopy(entry), "clips": []})
                if not row["clips"] and not row["media"]:
                    row["media"] = copy.deepcopy(entry)
        for clip in self.clips():
            if clip.asset in entries:
                entries[clip.asset]["clips"].append(clip.address)
        return [entries[name] for name in sorted(entries)]

    def replace_asset(self, key: str, asset: Any) -> list[str]:
        """Repoint every clip using ``key`` to new media while retaining the key."""
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", str(key)):
            raise TimelineEditError(f"asset key {key!r}: use letters, digits, - and _ (e.g. BROOM)")
        from astrid.sdk.media_handles import is_media_handle

        if not isinstance(asset, str) or not is_media_handle(asset):
            raise TimelineEditError("replacement media must be a project media handle (run:…, task:…, ref:…, sha256:…)")
        occurrences = [clip for clip in self.clips() if clip.asset == key]
        if not occurrences:
            raise TimelineEditError(f"no clips use asset key {key!r}")
        old_by_shot = {clip.shot_id: copy.deepcopy(self._registry(clip.shot_id).get(key) or {}) for clip in occurrences}
        if any(not old for old in old_by_shot.values()):
            raise TimelineEditError(f"asset key {key!r} is missing from a clip's shot registry")
        entry = _store.resolve_handle_entry(str(self.bundle.get("project_id")), asset)
        entry.pop("key", None)
        new_entry = {k: v for k, v in entry.items()
                     if k in ("media_id", "content_sha256", "type", "resolution", "size", "size_bytes")}
        new_entry["source"] = asset
        new_kind = _media_kind(new_entry)
        for old in old_by_shot.values():
            old_kind = _media_kind(old)
            if old_kind and new_kind and old_kind != new_kind:
                raise TimelineEditError(f"asset key {key!r} is {old_kind}; replacement media is {new_kind}")
            if not old_kind or not new_kind:
                raise TimelineEditError(f"cannot verify media kind for asset key {key!r}; refusing replacement")
        for sid in old_by_shot:
            self._register_asset(sid, {"key": key, **new_entry}, replace=True)
        changed = []
        for clip in occurrences:
            self._follow_media(clip, old_by_shot[clip.shot_id], new_entry, asset)
            changed.append(clip.address)
        return changed

    def _follow_media(self, clip: "Clip", old: Mapping[str, Any], new: Mapping[str, Any], asset: Any) -> None:
        """After an audio swap: the clip's length follows the new file, and a music clip's beats come along."""
        project = str(self.bundle.get("project_id") or "")
        local = Path(str(asset)).expanduser() if isinstance(asset, (str, Path)) and Path(str(asset)).expanduser().is_file() else None
        data = clip.data
        if ("from" in data or "to" in data) and not intent.until(data) and intent.for_s(data) is None:
            new_s = _store.media_seconds(project, local or new)
            old_s = _store.media_seconds(project, old) if old else None
            src0, speed = _num(data.get("from")), _num(data.get("speed"), 1.0) or 1.0
            to = _num(data.get("to"))
            whole = old_s is not None and src0 <= 1e-3 and abs(to - old_s) <= 1.0 / self.fps
            before = clip.duration
            if new_s is None:
                self.notes.append(f"{clip.address}: could not read the new file's length; it keeps {before:.3f} s "
                                  "(--duration S to change it)")
            elif whole or to > new_s + 1e-3:
                _set_length(data, mo.floor_frame((new_s - src0) / speed, self.fps))
                self.notes.append(f"{clip.address}: plays the new file {'whole' if whole else 'to its end'}: "
                                  f"{before:.3f} → {clip.duration:.3f} s")
            elif abs(new_s - (to - src0)) > 1.0 / self.fps:
                self.notes.append(f"{clip.address}: kept its length {before:.3f} s (it was a part of the old file); "
                                  f"the new file is {new_s:.3f} s (--duration {new_s:.3f} plays it all)")
        if not (clip.track == "music" or intent.beat_sources(data)):
            return
        from astrid.sdk.media_handles import is_media_handle

        found = None
        if local is not None:
            found = _store.file_beats(local)
        elif isinstance(asset, str) and is_media_handle(asset):
            found = _store.handle_beats(project, asset)
        if found is not None:
            self._attach_beats(clip, *found)
            return
        value = intent._app(data).get("beats")
        if value:  # the grid it has was made for the old file: say so until it is replaced or kept
            grid = dict(value) if isinstance(value, Mapping) else {"beats": intent.beat_sources(data), "time": "cue_seconds"}
            grid["media_id"] = old.get("media_id") or grid.get("media_id") or "the old file"
            intent.set_beats(data, grid)
        self.notes.append(f"{clip.address}: no beat grid came with the new file; " + (
            f"its beats are still the old file's ({intent.beats_label(data)}): attach the new grid with "
            "--beats FILE|run:<id>/beats (or --beats keep)" if value else "attach one with --beats FILE|run:<id>/beats"))

    def _attach_beats(self, clip: "Clip", grid: Mapping[str, Any], label: str) -> None:
        grid = dict(grid)
        media = (self._registry(clip.shot_id).get(str(clip.asset)) or {}).get("media_id") if clip.asset else None
        if media:
            grid["media_id"] = media
        else:
            grid.pop("media_id", None)
        intent.set_beats(clip.data, grid)
        moved = [line for line in self.resolve() if line]
        detail = f"{len(grid.get('beats') or [])} beats" + (f", {grid['bpm']:g} bpm" if isinstance(grid.get("bpm"), (int, float)) else "")
        heard = _num(clip.data.get("to")) if "to" in clip.data else None
        if isinstance(grid.get("duration_s"), (int, float)) and heard is not None and heard > float(grid["duration_s"]) + 0.5:
            self.notes.append(f"{clip.address}: the beats file covers {grid['duration_s']:g} s but the clip plays to "
                              f"{heard:.3f} s of its file: is it the grid for this music?")
        self.notes.append(f"{clip.address}: beats from {label} ({detail}); " + (
            f"{len(moved)} beat moment(s) re-resolved: " + "; ".join(moved[:8]) + (f"; … {len(moved) - 8} more" if len(moved) > 8 else "")
            if moved else "no moment moved"))

    def _stale_beats(self) -> list[str]:
        """Music clips whose beat grid was made for another file (after a swap)."""
        out = []
        for clip in self.clips(audio=True):
            value = intent._app(clip.data).get("beats")
            if not isinstance(value, Mapping) or not value.get("media_id"):
                continue
            media = (self._registry(clip.shot_id).get(str(clip.asset)) or {}).get("media_id")
            if media and value["media_id"] != media:
                out.append(f"{clip.address}: its beats ({intent.beats_label(clip.data)}) were made for "
                           f"{str(value['media_id'])[:19]}…, not the file it plays ({str(media)[:19]}…): attach this "
                           "file's grid (--beats FILE|run:<id>/beats) or keep the old one (--beats keep)")
        return out

    def _register_asset(self, shot_id: str, asset: Any, *, replace: bool = False) -> str:
        """Make ``asset`` resolvable in this shot; return its registry key."""
        assets = self._registry(shot_id)
        if isinstance(asset, Path):
            asset = str(asset)
        if isinstance(asset, str) and asset in assets:
            return asset
        from astrid.sdk.media_handles import is_media_handle

        if isinstance(asset, str) and "=" in asset:  # KEY=HANDLE names the new asset (CLI --swap-asset BROOM=run:…#0)
            name, _, ref = asset.partition("=")
            if re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", name) and is_media_handle(ref):
                asset = {"key": name, "ref": ref}
        if isinstance(asset, Mapping) and "ref" in asset:  # {"key": "BROOM", "ref": "run:…/sprites#0"}
            name = str(asset.get("key") or "")
            if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", name):
                raise TimelineEditError(f"asset key {name!r}: use letters, digits, - and _ (e.g. BROOM)")
            entry = _store.resolve_handle_entry(str(self.bundle.get("project_id")), str(asset["ref"]))
            entry.pop("key", None)
            if _free_key(assets, name, entry) != name and not replace:
                raise TimelineEditError(f"asset key {name!r} already names other media in this timeline; choose another key")
            if replace:
                old_kind, new_kind = _media_kind(assets.get(name) or {}), _media_kind(entry)
                if not old_kind or not new_kind or old_kind != new_kind:
                    raise TimelineEditError(f"asset key {name!r} cannot be replaced by different or unknown media kind")
            assets[name] = {k: v for k, v in entry.items() if k in _ASSET_REGISTRY_FIELDS}
            assets[name]["source"] = str(asset["ref"])
            return name
        if isinstance(asset, str):
            for sid in self._shot_ids():
                entry = self._registry(sid).get(asset)
                if entry is not None:
                    assets[asset] = copy.deepcopy(entry)
                    return asset
            path = Path(asset).expanduser()
            if path.is_file():
                entry = _store.import_media(path, self.bundle.get("project_id"))
                key = _free_key(assets, re.sub(r"[^A-Za-z0-9_-]+", "-", path.stem).strip("-") or "asset", entry)
                entry["source"] = str(path)
                assets[key] = entry
                return key
            if is_media_handle(asset):  # run:…#n, task:…, ref:NAME, sha256:… (media already in the project)
                entry = _store.resolve_handle_entry(str(self.bundle.get("project_id")), asset)
                key = _free_key(assets, str(entry.pop("key")), entry)
                assets[key] = {k: v for k, v in entry.items() if k in _ASSET_REGISTRY_FIELDS}
                assets[key]["source"] = asset
                return key
            known = sorted({k for sid in self._shot_ids() for k in self._registry(sid)})
            close = difflib.get_close_matches(asset, known, n=5)
            raise TimelineEditError(f"no asset {asset!r} in this timeline" + (f"; did you mean {', '.join(close)}?" if close else "")
                                    + " (or a local file to import, or a media handle: run:RUN/PORT#n, ref:NAME, sha256:…)")
        if isinstance(asset, Mapping):
            entry = dict(asset)
            media_id = entry.get("media_id") or entry.get("digest") or entry.get("object_id")
            if not media_id:
                raise TimelineEditError("an asset entry needs media_id (or digest)")
            entry.setdefault("media_id", media_id)
            entry.setdefault("content_sha256", entry.get("digest") or media_id)
            key = str(entry.pop("key", None) or entry.get("name") or str(media_id)[-12:])
            if replace and key in assets and assets[key].get("media_id") != entry.get("media_id"):
                old_kind, new_kind = _media_kind(assets[key]), _media_kind(entry)
                if not old_kind or not new_kind or old_kind != new_kind:
                    raise TimelineEditError(f"asset key {key!r} cannot be replaced by different or unknown media kind")
            assets[key] = {k: v for k, v in entry.items() if k in _ASSET_REGISTRY_FIELDS}
            if entry.get("source"):
                assets[key]["source"] = entry["source"]
            return key
        raise TimelineEditError(f"cannot use {asset!r} as an asset")

    def _corner(self, element: str, params: Mapping[str, Any], asset: str | None, shot_id: str, corner: str) -> dict[str, Any]:
        corner = corner.replace("_", "-").lower()
        if corner not in ("top-right", "top-left", "bottom-right", "bottom-left"):
            raise TimelineEditError("corner must be top-right, top-left, bottom-right or bottom-left")
        width, height = 320, 120
        if element == "am-sprite" and asset:
            res = str((self._registry(shot_id).get(asset) or {}).get("resolution") or "")
            m = re.match(r"^(\d+)x(\d+)$", res)
            scale = int(_num(params.get("scale"), 6))
            frames = params.get("frames") or {}
            if frames.get("frameWidth"):
                width, height = int(frames["frameWidth"]) * scale, int(frames["frameHeight"]) * scale
            elif m:
                width, height = int(m.group(1)) * scale, int(m.group(2)) * scale
        elif isinstance(params.get("width"), (int, float)):
            width = int(params["width"])
        x = SAFE_MARGIN[0] if "left" in corner else CANVAS[0] - SAFE_MARGIN[0] - width
        y = SAFE_MARGIN[1] if "top" in corner else CANVAS[1] - SAFE_MARGIN[1] - height
        if element == "am-sprite":  # logical px on the 6 px grid
            return {"x": int(x // 6), "y": int(y // 6)}
        return {"x": int(x), "y": int(y)}
