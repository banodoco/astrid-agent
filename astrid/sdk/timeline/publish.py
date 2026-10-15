"""Checkout: publish, narration binding, and the document as it is saved (annotated) and read back (absorb)."""
from __future__ import annotations

import copy
import re
import uuid
from pathlib import Path
from typing import Any, Mapping

from astrid.sdk import timeline_intent as intent
from astrid.core.timeline.cuts import (
    clip_duration,
)

from astrid.sdk.timeline import journal as _journal
from astrid.sdk.timeline import store as _store
from astrid.sdk.timeline._common import (EDITABLE_NOTE, TimelineEditError, _num, _r)
from astrid.sdk.timeline.doc import (_row_start, _set_length, _strip_derived, _timeline_stamp)
from astrid.sdk.timeline.model import Voice


class _PublishMixin:
    """Checkout: publish, narration binding, and the document as it is saved (annotated) and read back (absorb)."""

    def publish(self, message: str = "", *, idempotency_key: str | None = None, client: Any = None,
                force: bool = False) -> dict[str, Any]:
        """Check, then publish one revision.

        If someone published since this checkout, the edit is merged onto their head clip by
        clip (three-way: your base, their head, your candidate). Changes to different clips
        merge; a clip both sides changed refuses with a conflict list. ``force=True``
        overwrites their change, explicitly. Nothing is ever overwritten silently.
        """
        self.notes.extend(self._sync_film_length())  # the film's length follows its content, before the check
        report = self.check()
        if not report.valid:
            raise TimelineEditError("not publishing: " + "; ".join(report.problems or ["the candidate is invalid"]))
        key = idempotency_key or f"edit-{re.sub(r'[^a-z0-9]+', '-', message.lower())[:40].strip('-') or 'timeline'}-{uuid.uuid4().hex[:8]}"
        if self.edits().get("changes") or not self.narration_changes():
            receipt = _store.publish_bundle(self.document(), key, client=client, force=force)
        else:  # only the narration binding is behind (a retry after it failed): pin it, nothing else to publish
            receipt = {"old_head": self.base_revision, "new_head": self.base_revision, "structure": "unchanged"}
        receipt["narration_pinned"] = []
        if self.narration_changes():
            # A line's text changed (or the shots are new): bind each such shot's narration now that
            # the shots exist, then publish the pins as a second, small revision.
            try:
                receipt.update(self._publish_narration(key, client=client))
            except TimelineEditError as exc:  # the structure IS published: say so, and how to finish
                receipt["narration_error"] = (f"{exc}. The cut is published (head {receipt.get('new_head')}); only the "
                                              "narration binding is behind: check out the current timeline head, then "
                                              "publish a narration revision with a message")
        if self.path:  # publish resets the base: this working copy is the new head, with no changes (AUDIT bug 6)
            before = _journal.document_digest(self.document())
            self._reset_base(client=client)
            _journal.record_or_note(self, Path(self.path), kind="publish", outcome="changed", label="publish",
                                    reason=message or None, before=before,
                                    after=_journal.document_digest(self.document()),
                                    evidence=({"kind": "revision", "id": receipt.get("new_head")},))
        receipt["message"] = message
        return receipt

    def _reset_base(self, *, client: Any = None) -> None:
        """Re-read the new head as this working copy's bundle and save it with no undo step. Never fails the publish."""
        try:
            head = _store.fetch_bundle(str(self.bundle["project_id"]), str(self.bundle["timeline_id"]), client=client)
        except Exception as exc:  # noqa: BLE001 - the publish stands; the working copy keeps its old base
            self.notes.append(f"published, but this working copy could not re-read the new head ({exc}); "
                              "its base is still the old one: check out the current timeline head before editing again")
            return
        old, old_journal = self.bundle, list(self._journal)
        self.bundle, self._mcache, self._journal, self._disk = head, None, [], None
        try:
            target = Path(self.path)
            with _store._file_lock(target):
                self._save_locked(target, force=True, record=False)
        except Exception as exc:  # noqa: BLE001
            self.bundle, self._journal = old, old_journal
            self.notes.append(f"published, but this working copy could not be reset to the new head ({exc}); "
                              "its base is still the old one")

    def _publish_narration(self, key: str, *, client: Any = None) -> dict[str, Any]:
        def run(c: Any) -> dict[str, Any]:
            from astrid.sdk.timeline.checkout import Checkout
            head = Checkout(_store.fetch_bundle(str(self.bundle["project_id"]), str(self.bundle["timeline_id"]), client=c))
            pinned = head.pin_narration(c, idempotency_key=key)
            if not pinned:
                return {"narration_pinned": []}
            second = _store.publish_bundle(head.document(), f"{key}-narration", client=c)
            return {"narration_pinned": pinned, "new_head": second.get("new_head"), "narration_head": second.get("new_head")}

        if client is not None:
            return run(client)
        from astrid.sdk import AstridClient

        with AstridClient.open_from_launcher(start_pack_host=False) as c:
            return run(c)

    # ---- narration: the script text each shot is bound to ---------------------------
    def narration(self) -> dict[str, str]:
        """``{shot_id: text}``: each shot's lines' script text, in order (what the shot's
        voiceover_script binding should hold). Only shots whose lines all declare their text."""
        by_shot: dict[str, list[Voice]] = {}
        for line in self.lines():
            by_shot.setdefault(line.clips[0].shot_id, []).append(line)
        out = {}
        for sid, lines in by_shot.items():
            if all(intent.line_text(v.clips[0].data) is not None for v in lines):
                out[sid] = " ".join(v.text for v in lines) + "\n"
        return out

    def narration_changes(self) -> dict[str, str]:
        """Shots whose narration differs from their bound voiceover_script (by content hash)."""
        import hashlib

        changed = {}
        for sid, text in self.narration().items():
            payload = self.bundle["shots"][sid].setdefault("payload", {})
            bound = next((b for b in payload.get("text_bindings") or [] if b.get("kind") == "voiceover_script"), None)
            digest = "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()
            if not bound or bound.get("content_hash") != digest:
                changed[sid] = text
        return changed

    def pin_narration(self, client: Any, *, idempotency_key: str) -> list[str]:
        """Register the changed narration per shot and pin it in the document (publish does this)."""
        pinned = []
        for sid, text in self.narration_changes().items():
            payload = self.bundle["shots"][sid]["payload"]
            bound = next((b for b in payload.get("text_bindings") or [] if b.get("kind") == "voiceover_script"), None)
            expected = int((bound or {}).get("head") or 0)
            result = client.shots.set_text_binding(self.bundle["project_id"], shot_id=sid, kind="voiceover_script", text=text,
                                                    expected_head=expected,
                                                    idempotency_key=f"{idempotency_key}-narration-{sid}"[:120])
            pin = getattr(result, "data", result)
            if getattr(result, "ok", True) is False or not isinstance(pin, Mapping):
                raise TimelineEditError(f"could not register the narration for shot {sid}: {getattr(result, 'error', result)}")
            payload["text_bindings"] = [b for b in payload.get("text_bindings") or [] if b.get("kind") != "voiceover_script"] + [dict(pin)]
            (payload.get("metadata") or {}).pop("voiceover_script", None)
            pinned.append(sid)
        return pinned

    def document(self) -> dict[str, Any]:
        """The publishable candidate (owned checkout helpers removed)."""
        doc = _strip_derived(copy.deepcopy(self.bundle))
        intent.migrate(doc)  # the candidate carries intent under app.astrid; unchanged shots reuse their old revisions and the merged candidate is published without a further migration (old revisions are never rewritten)
        return doc

    def _annotated(self) -> dict[str, Any]:
        """The bundle plus derived, read-only helpers: an index and both clocks on every clip."""
        doc = _strip_derived(copy.deepcopy(self.bundle))
        intent.migrate(doc)
        index = {
            "note": EDITABLE_NOTE,
            "fps": self.fps,
            "duration_s": self.duration,
            "cuts": [{"n": c.n, "start": _r(c.start), "end": _r(c.end), "shot": c.shot,
                      "picture": c.picture.id if c.picture else None, "layers": [l.id for l in c.layers]} for c in self.cuts],
            "words": [[w.id, w.text, w.start, w.end] for w in self.words()],
        }
        for sid in self._shot_ids():
            start = self._shot_start(sid)
            for clip in doc["shots"][sid].get("internal_timeline", {}).get("clips", []):
                cs = start + _num(clip.get("at"))
                if "_timeline" not in clip:  # a malformed collision belongs to its original owner
                    clip["_timeline"] = {"start": _r(cs), "end": _r(cs + clip_duration(clip))}
        return {"_index": index, **doc}

    def _absorb(self, raw: dict[str, Any]) -> dict[str, Any]:
        """Apply hand edits made in `_timeline` (timeline seconds), then drop every derived field."""
        bundle = raw
        rows = {str(r.get("shot_id")): r for r in bundle.get("placements") or []}
        for sid, shot in (bundle.get("shots") or {}).items():
            row = rows.get(sid)
            if row is None:
                continue
            start = _row_start(row) / 1000.0
            for clip in (shot.get("internal_timeline") or {}).get("clips", []):
                stamp = clip.get("_timeline")
                if not _timeline_stamp(stamp):
                    continue
                cs = start + _num(clip.get("at"))
                ce = cs + clip_duration(clip)
                new_s, new_e = _num(stamp.get("start"), cs), _num(stamp.get("end"), ce)
                if abs(new_s - cs) > 1e-4 or abs(new_e - ce) > 1e-4:
                    if abs(new_s - cs) > 1e-4 and abs(new_e - ce) <= 1e-4:
                        new_e = new_s + (ce - cs)  # moved start only: keep the length
                    clip["at"] = _r(new_s - start)
                    _set_length(clip, new_e - new_s)
                    self.notes.append(f"{clip.get('id')}: applied your _timeline edit → at {clip['at']} (shot {sid})")
        bundle = _strip_derived(bundle)
        intent.migrate(bundle)  # checkout: legacy intent moves to app.astrid in the WORKING COPY (its pinned base copies are migrated too, so the move is not an edit)
        # Validate supplied music projection at the load boundary without
        # resolving or enrolling ordinary legacy content.
        from astrid.sdk.timeline.music_beds import validate_bundle
        validate_bundle(bundle)
        return bundle
