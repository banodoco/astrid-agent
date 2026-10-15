"""Checkout: publish, narration binding, and the document as it is saved (annotated) and read back (absorb)."""
from __future__ import annotations

import copy
from typing import Any, Mapping

from astrid.sdk import timeline_intent as intent
from astrid.core.timeline.cuts import (
    clip_duration,
)

from astrid.sdk.timeline._common import (EDITABLE_NOTE, TimelineEditError, _num, _r)
from astrid.sdk.timeline.doc import (_row_start, _set_length, _strip_derived, _timeline_stamp)
from astrid.sdk.timeline.model import Voice


class _PublishMixin:
    """Checkout: publish, narration binding, and the document as it is saved (annotated) and read back (absorb)."""

    def publish(self, message: str = "", *, idempotency_key: str | None = None, client: Any = None,
                force: bool = False, dry_run: bool = False) -> dict[str, Any]:
        """Publish verified saved commands through Runtime's single COMMIT route.

        Uncertain requests survive process restart in the existing draft journal.
        A retry authenticates currently and sends the original complete envelope.
        """
        from astrid.sdk.timeline.saved_publish import publish_saved
        return publish_saved(self, message, idempotency_key=idempotency_key,
                             client=client, force=force, dry_run=dry_run)

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
        """Register narration per shot as an explicit media preparation step."""
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
                      "picture": c.picture.id if c.picture else None, "layers": [layer.id for layer in c.layers]} for c in self.cuts],
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
        return bundle
