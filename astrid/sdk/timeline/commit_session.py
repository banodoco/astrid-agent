"""Runtime-backed Python editing with bounded function recomputation."""
from __future__ import annotations

import copy
import hashlib
import json
import uuid
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any, cast

from ._common import PARENT_SHOT, TimelineEditError
from .checkout import Checkout
from .sync import CommitConflict, CommitRefused, TimelineSession, _OutboxChanged, _enrich_pending_diff, _pending_clash, _part_identity, _pin


def _function_rerun_conflict(session, original_base, current_base, before_bundle, after_bundle):
    """Return the existing conflict shape when a rerun rewrites intervening work."""
    from . import clash
    from ._doc_patch import Closure
    from .emitter import _diff, _payload_for_emit, _root_pairs

    if current_base == original_base:
        return None
    head, entries = clash._history(session, original_base)
    original = session._capture_bundle({"base": original_base, "pending": []})
    original_closure = Closure(original)
    before = Closure(before_bundle)
    writes = []
    for ref, left, right in _root_pairs(before_bundle, after_bundle):
        changes = []
        _diff(_payload_for_emit(ref, left), _payload_for_emit(ref, right), ref, (), changes)
        for change in changes:
            site = (change["ref"], copy.deepcopy(change.get("path", [])))
            if site not in writes:
                writes.append(site)
    if not writes or not entries:
        return None
    _, receipt_sites = clash._receipt_sites(entries, original_closure)
    conflicts = []
    for ref, path in writes:
        target = clash._addresses(ref, path, before)
        if any(clash._touches(receipts, target) for receipts in receipt_sites):
            conflicts.append({"ref": ref, "path": path})
    if not conflicts:
        return None
    return {"code": "intervening_write", "message": "function rerun overlaps an intervening write",
            "details": {"status": "conflict", "reason": "intervening_write",
                        "current": head, "conflicts": conflicts}}


def emit_document_commands(before, after):
    """G3 owns this lossless bundle-to-command emitter and its patch semantics."""
    try:
        from .emitter import emit_document_commands as emit
    except ModuleNotFoundError as exc:
        if exc.name != "astrid.sdk.timeline.emitter":
            raise
        raise TimelineEditError("free edits require the G3 document emitter; use command() until it is available") from exc
    return emit(before, after)


def _imports_in_commands(commands):
    """Disclose catalog media referenced by a reviewed fixed command list."""
    imports, seen = [], set()

    def visit(value):
        if isinstance(value, Mapping):
            if all(isinstance(value.get(key), str) and value[key]
                   for key in ("object_id", "generation_id", "variant_id")):
                identity = tuple(value[key] for key in ("object_id", "generation_id", "variant_id"))
                if identity not in seen:
                    seen.add(identity)
                    imports.append({"object_id": identity[0], "generation_id": identity[1],
                                    "variant_id": identity[2], "catalog_write": True,
                                    "retained_on_undo": True})
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(commands)
    return imports


def _list_steps(commands, label):
    return [{"id": f"step-{ordinal + 1}", "label": label, "intent": "", "tags": [],
             "command_ordinals": [ordinal],
             "patch": copy.deepcopy(command.get("args", {}).get("changes", []))
             if isinstance(command, Mapping) and command.get("op") == "doc.patch" else []}
            for ordinal, command in enumerate(commands)]


def _clip_interval(clip):
    """Return a clip's timeline interval in seconds when its timing is known."""
    start = clip.get("at", clip.get("start"))
    if not isinstance(start, (int, float)) or isinstance(start, bool):
        return None
    if isinstance(clip.get("hold"), (int, float)) and not isinstance(clip.get("hold"), bool):
        end = start + clip["hold"]
    elif all(isinstance(clip.get(key), (int, float)) and not isinstance(clip.get(key), bool)
             for key in ("from", "to")):
        end = start + clip["to"] - clip["from"]
    else:
        return None
    return (float(start), float(end)) if end > start else None


def _effect_intervals(clip, interval, fps):
    """Get the temporal windows represented by common clip effect fields."""
    start, end = interval
    windows = []

    def duration(value):
        if not isinstance(value, Mapping):
            return None
        frames = value.get("durationFrames")
        seconds = value.get("duration")
        result = (frames / fps if isinstance(frames, (int, float)) and not isinstance(frames, bool)
                  else seconds if isinstance(seconds, (int, float)) and not isinstance(seconds, bool)
                  else None)
        return min(end - start, float(result)) if result and result > 0 else None

    for key in ("entrance", "exit", "transition"):
        effect = clip.get(key)
        length = duration(effect)
        if length is not None:
            windows.append((start, start + length) if key == "entrance" else (end - length, end))
    for key in ("effects", "animations"):
        entries = clip.get(key)
        if isinstance(entries, list):
            for effect in entries:
                if not isinstance(effect, Mapping):
                    continue
                length = duration(effect)
                offset_frames = effect.get("startFrame")
                offset = (offset_frames / fps if isinstance(offset_frames, (int, float))
                          and not isinstance(offset_frames, bool) else effect.get("start", 0))
                if length is not None and isinstance(offset, (int, float)) and not isinstance(offset, bool):
                    lo = max(start, start + offset)
                    hi = min(end, lo + length)
                    if hi > lo:
                        windows.append((lo, hi))
    return windows


def _changed_ranges(before, after):
    """Describe changed timeline time from exact accepted before/after bundles."""
    from ._command_state import equal

    def clips(bundle):
        parent = bundle.get("parent") if isinstance(bundle, Mapping) else None
        rows = parent.get("clips", []) if isinstance(parent, Mapping) else []
        return {str(row.get("id")): row for row in rows
                if isinstance(row, Mapping) and row.get("id") is not None}

    old, new = clips(before), clips(after)
    old_fps = before.get("parent", {}).get("config", {}).get("output", {}).get("fps", 30)
    new_fps = after.get("parent", {}).get("config", {}).get("output", {}).get("fps", 30)
    fps = float(old_fps) if isinstance(old_fps, (int, float)) and old_fps > 0 else 30.0
    if isinstance(new_fps, (int, float)) and new_fps > 0:
        fps = max(fps, float(new_fps))
    spans = []
    changed = set(old) | set(new)
    changed = {identity for identity in changed if not equal(old.get(identity), new.get(identity))}
    for identity in changed:
        for snapshot, peers in ((old, new), (new, old)):
            clip = snapshot.get(identity)
            if clip is None:
                continue
            interval = _clip_interval(clip)
            if interval is None:
                continue
            old_clip = old.get(identity, {})
            new_clip = new.get(identity, {})
            effect_keys = {"entrance", "exit", "transition", "effects", "animations"}
            effect_only = (old_clip.keys() | new_clip.keys()) and all(
                key in effect_keys or equal(old_clip.get(key), new_clip.get(key))
                for key in old_clip.keys() | new_clip.keys())
            if effect_only:
                before_effects = len(spans)
                for key in effect_keys:
                    if not equal(old_clip.get(key), new_clip.get(key)):
                        spans.extend(_effect_intervals(clip, interval, fps))
                if len(spans) == before_effects:
                    spans.append(interval)
            else:
                spans.append(interval)
            # A transition can affect the adjacent same-track clip as well.
            if any(not equal(old_clip.get(key), new_clip.get(key))
                   for key in ("exit", "transition", "effects", "animations")):
                affected = _effect_intervals(clip, interval, fps) or [interval]
                for neighbor in peers.values():
                    neighbor_interval = _clip_interval(neighbor)
                    if (neighbor_interval is not None and neighbor.get("track") == clip.get("track")
                            and neighbor.get("id") != identity):
                        for effect_start, effect_end in affected:
                            lo = max(effect_start, neighbor_interval[0])
                            hi = min(effect_end, neighbor_interval[1])
                            if hi > lo:
                                spans.append((lo, hi))
    def placements(bundle):
        rows = bundle.get("placements", []) if isinstance(bundle, Mapping) else []
        return {str(row.get("occurrence_id")): row for row in rows
                if isinstance(row, Mapping) and row.get("occurrence_id") is not None}

    old_placements, new_placements = placements(before), placements(after)
    for identity in old_placements.keys() | new_placements.keys():
        old_row, new_row = old_placements.get(identity), new_placements.get(identity)
        if equal(old_row, new_row):
            continue
        for row in (old_row, new_row):
            if row is None:
                continue
            placement = row.get("placement")
            start_ms = placement.get("start_ms") if isinstance(placement, Mapping) else None
            duration_ms = row.get("duration_ms")
            if (isinstance(start_ms, (int, float)) and not isinstance(start_ms, bool)
                    and isinstance(duration_ms, (int, float)) and not isinstance(duration_ms, bool)
                    and duration_ms > 0):
                spans.append((float(start_ms) / 1000, (float(start_ms) + float(duration_ms)) / 1000))
    merged = []
    for start, end in sorted((lo, hi) for lo, hi in spans if hi > lo):
        if merged and start <= merged[-1]["end"]:
            merged[-1]["end"] = max(merged[-1]["end"], end)
        else:
            merged.append({"start": start, "end": end})
    return merged


class Timeline(Checkout):
    """A local edit session whose only publication boundary is ``commit``.

    ``command`` stages registered semantic commands and their local preview.
    Checkout helpers/free bundle edits are captured by G3's document emitter.
    A commit function receives a fresh Timeline at each conflict head. It must
    keep external side effects out of its body because it can run three times.
    """

    def __init__(self, session, bundle, base, *, evaluating=False, media_cache=None,
                 identity_cache=None):
        # Initialize helper state without migrating/flattening the pinned bundle.
        super().__init__({})
        self.session = session
        self._basis = copy.deepcopy(base)
        self._evaluating = evaluating
        self._media_cache = copy.deepcopy(media_cache or {})
        self._identity_cache = copy.deepcopy(identity_cache or {})
        self._identity_ordinal = 0
        self._verified_media = []
        self._retry_functions = {}
        self._adopt(bundle)

    def _register_asset(self, shot_id, asset, *, replace=False):
        """Keep local replacement media verified when a commit function reruns."""
        from pathlib import Path

        local_path = Path(asset).expanduser() if isinstance(asset, (str, Path)) else None
        if local_path is None or not local_path.is_file():
            return super()._register_asset(shot_id, asset, replace=replace)

        from .media_add import _selection
        from astrid.sdk.timeline_media_replacement import _verified

        transport = self.session.client._remote._transport
        source = str(local_path.absolute())
        cached = next((item for item in self._media_cache.values()
                       if source in item.get("sources", [])), None)
        if cached is not None:
            pin = copy.deepcopy(cached["pin"])
            from astrid.core.media import MediaProbeError
            from astrid.sdk.authoring_media import _metadata

            try:
                metadata = _metadata(local_path)
                payload = local_path.read_bytes()
            except (MediaProbeError, OSError, ValueError) as exc:
                raise TimelineEditError(f"invalid local media {source!r}: {exc}; use a readable image, video, or audio file") from exc
            if "sha256:" + hashlib.sha256(payload).hexdigest() != pin.get("object_id") \
                    or metadata.get("digest") != pin.get("object_id"):
                raise TimelineEditError("local media changed during import; retry after the file is saved")
            pin, verified = _verified(transport, self.session.project_id, pin)
        else:
            pin, verified = _selection(self.session, self.bundle, local_path)
        object_id = pin["object_id"]
        if not any(row.get("selection") == verified.get("selection") for row in self._verified_media):
            self._verified_media.append({**copy.deepcopy(verified), "object_id": object_id,
                                         "pin": {"media_id": object_id, "content_digest": object_id}})
        media_entry = {"key": pin["asset"], "media_id": object_id,
                       "content_sha256": object_id, "type": verified.get("media_type"),
                       "generationId": pin["generation_id"], "variantId": pin["variant_id"]}
        if verified.get("duration_seconds") is not None:
            media_entry["duration"] = verified["duration_seconds"]
        if verified.get("width") and verified.get("height"):
            media_entry["resolution"] = f'{verified["width"]}x{verified["height"]}'
        key = super()._register_asset(shot_id, media_entry, replace=replace)

        cache = self._media_cache.setdefault(object_id, {
            "pin": copy.deepcopy(pin), "sources": [],
            "imports": [{"object_id": pin["object_id"], "generation_id": pin["generation_id"],
                         "variant_id": pin["variant_id"], "catalog_write": True,
                         "retained_on_undo": True}],
        })
        if source not in cache.setdefault("sources", []):
            cache["sources"].append(source)
        return key

    @classmethod
    def open(cls, project=None, timeline=None, *, client=None, outbox_path=None):
        return open(project, timeline, client=client, outbox_path=outbox_path)

    def _adopt(self, bundle):
        self.bundle = copy.deepcopy(bundle)
        self._base_bundle = copy.deepcopy(bundle)
        self._runtime_document = self.bundle.get("source_mapping", {}).pop("_runtime_document", None)
        self._command_bundle = copy.deepcopy(self.bundle)
        self._commands: list[dict[str, Any]] = []
        self._steps: list[dict[str, Any]] = []
        self._free_edits = False
        self._journal.clear()
        self._refresh()

    def add(self, source, *, at, hold=None, duration=None, track=None, id=None, clip_id=None, **options):
        """Stage media, retaining its exact content selection across reruns."""
        identity_ordinal = self._identity_ordinal
        if self._evaluating:
            self._identity_ordinal += 1
        from pathlib import Path

        local_path = Path(source).expanduser() if isinstance(source, (str, Path)) else None
        source_path = str(local_path.absolute()) if local_path is not None else None
        cached = next((entry for entry in (self._media_cache.values() if self._evaluating else ())
                       if source_path is not None and source_path in entry.get("sources", ())), None)
        native_media = (self._evaluating and
                        (isinstance(source, (Mapping, Path)) or cached is not None
                         or bool(local_path and local_path.is_file())
                         or bool(local_path and not options
                                 and (local_path.suffix or len(local_path.parts) > 1))
                         or duration is not None or id is not None))
        if not native_media:
            if duration is not None:
                options["duration"] = duration
            if id is not None:
                options["id"] = id
            if clip_id is not None:
                options["clip_id"] = clip_id
            return super().add(source, at=at, hold=hold, track=track, **options)
        if options:
            raise TimelineEditError("flat media add accepts only at, hold, track and id options")

        from astrid.sdk.timeline_media_replacement import _verified

        from .capture import number
        from .media_add import _placement_commands, _selection, existing_selectors
        from .model import Clip

        if id is not None and clip_id is not None:
            raise TimelineEditError("use only one of id or clip_id")
        clip_id = id if id is not None else clip_id
        if clip_id is not None and (not isinstance(clip_id, str) or not clip_id.strip()):
            raise TimelineEditError("media clip id must be a nonempty string")
        if clip_id is None and self._evaluating:
            slot = str(identity_ordinal)
            clip_id = self._identity_cache.setdefault(slot, "clip-" + uuid.uuid4().hex)
        on = number(at, "at")
        if on < 0:
            raise TimelineEditError("at must not be negative")
        if track is not None and (not isinstance(track, str) or not track.strip()):
            raise TimelineEditError("media track must be a nonempty track ID")
        if duration is not None and hold is not None:
            raise TimelineEditError("use only one of hold or duration")
        requested_duration = duration if duration is not None else hold
        if requested_duration is not None:
            requested_duration = number(requested_duration, "hold", positive=True)
        cache_key = cached["pin"]["object_id"] if cached is not None else None
        if cached is None and local_path is not None and local_path.is_file():
            from astrid.core.media import MediaProbeError
            from astrid.sdk.authoring_media import _metadata

            try:
                metadata = _metadata(local_path)
                payload = local_path.read_bytes()
            except (MediaProbeError, OSError, ValueError) as exc:
                raise TimelineEditError(f"invalid local media {str(local_path)!r}: {exc}; "
                                        "use a readable image, video, or audio file") from exc
            cache_key = metadata["digest"]
            if "sha256:" + hashlib.sha256(payload).hexdigest() != cache_key:
                raise TimelineEditError("local media changed during import; retry after the file is saved")
            cached = self._media_cache.get(cache_key)
        reused = None
        if cached is None and cache_key is not None:
            # Reuse media this project already imported (keyed by project + digest).
            transport = self.session.client._remote._transport
            reused = existing_selectors(transport, self.session.project_id, cache_key)
        if reused is not None:
            from astrid.sdk.timeline_media_replacement import _plain

            pin = _plain(self.session.client._remote._transport.get_project_media_selection(
                self.session.project_id, reused))
            pin, verified = _verified(self.session.client._remote._transport, self.session.project_id, pin)
        elif cached is None:
            pin, verified = _selection(self.session, self.bundle, source)
        else:
            pin = copy.deepcopy(cached["pin"])
            pin, verified = _verified(self.session.client._remote._transport, self.session.project_id, pin)
        if cache_key is not None and pin.get("object_id") != cache_key:
            raise TimelineEditError("local media changed during import; retry after the file is saved")
        kind = verified.get("media_type", "").split("/", 1)[0]
        if kind not in {"image", "video", "audio"}:
            raise TimelineEditError("verified media must be an image, video, or audio file")
        asset_alias = None
        if self._evaluating:
            identity = "\0".join(pin[key] for key in ("object_id", "generation_id", "variant_id"))
            identity_key = "asset:" + hashlib.sha256(identity.encode("utf-8")).hexdigest()
            asset_alias = self._identity_cache.setdefault(identity_key, "media-" + uuid.uuid4().hex)
        length = requested_duration if requested_duration is not None else number(
            verified.get("duration_seconds") or 1.0, "hold", positive=True)

        self._capture_free_edits()
        commands = _placement_commands(self.bundle, pin, verified, on=on, duration=length,
                                       track=track, clip_id=clip_id, asset_alias=asset_alias)
        from .commands import execute

        verified = {**copy.deepcopy(verified), "object_id": pin["object_id"],
                    "pin": {"media_id": pin["object_id"], "content_digest": pin["object_id"]}}
        capture_bundle = (self._execution_bundle(self._command_bundle)
                          if self._runtime_document is not None else self._command_bundle)
        commands = execute(capture_bundle, commands,
                           verified_media=[*self._verified_media, copy.deepcopy(verified)]).commands
        self._verified_media.append(copy.deepcopy(verified))
        try:
            self._append(commands, label="Add media")
        except Exception:
            self._verified_media.pop()
            raise
        if cache_key is not None:
            entry = self._media_cache.setdefault(cache_key, {
                "pin": copy.deepcopy(pin), "sources": [],
                "imports": [{"object_id": pin["object_id"], "generation_id": pin["generation_id"],
                             "variant_id": pin["variant_id"], "catalog_write": True,
                             "retained_on_undo": True}] if reused is None else [],
            })
            if source_path not in entry.setdefault("sources", []):
                entry["sources"].append(source_path)
        identity = commands[-1]["args"]["row"]["id"]
        added = next((candidate for candidate in self._parent_clips() if candidate.get("id") == identity), None)
        if added is None:
            raise TimelineEditError("media capture did not create the requested clip")
        return Clip(self, PARENT_SHOT, added)

    def _execution_bundle(self, bundle):
        """Supply private canonical input from this evaluation's exact base pair."""
        if self._runtime_document is None:
            from .store import fetch_bundle

            source = fetch_bundle(
                self.session.project_id, self.session.timeline_id,
                revision_id=self._basis["revision_id"], publication_id=self._basis["publication_id"],
                client=self.session.client,
            )
            self._runtime_document = source["source_mapping"]["_runtime_document"]
        result = copy.deepcopy(bundle)
        result["source_mapping"]["_runtime_document"] = copy.deepcopy(self._runtime_document)
        return result

    @property
    def head(self):
        return copy.deepcopy(self._basis)

    def undo(self, steps: int | dict[str, list[str]] | None = 1, *,
             publications: list[str] | None = None, client: Any = None, **options: Any):
        """Undo local draft steps or dispatch an accepted-publication inverse.

        ``Checkout.undo`` owns the detached numeric draft history.  A Runtime
        timeline instead sends publication inverses through its already-open
        session, including the v2 publication-to-step selector.
        """
        if publications is None:
            return super().undo(cast(int, steps), client=client, **options)
        if steps is not None and not isinstance(steps, dict) and steps != 1:
            raise TimelineEditError("publication undo cannot be combined with draft steps")
        return self._accepted(self.session.undo(publications, steps=None if steps == 1 else steps, **options))

    def _capture_free_edits(self):
        if self.bundle != self._command_bundle:
            before, after = copy.deepcopy(self._command_bundle), copy.deepcopy(self.bundle)
            if self._runtime_document is not None or before.get("shots") != after.get("shots"):
                before, after = self._execution_bundle(before), self._execution_bundle(after)
            commands = emit_document_commands(before, after)
            if not commands:
                raise TimelineEditError("document emitter omitted local edits")
            from .publish import _media_admission_inputs

            inputs = _media_admission_inputs(
                before, after, self.session.client._remote._transport,
            )
            preview_media = self._verified_media
            if inputs:
                from astrid.sdk.timeline_media_replacement import _verified

                preview_media = [row for row in self._verified_media
                                 if not any(row.get("pin") == item["pin"] for item in inputs)]
                for item in inputs:
                    if "selection" in item:
                        _, verified = _verified(self.session.client._remote._transport,
                                                self.session.project_id, item["selection"])
                        row = {**copy.deepcopy(verified), "object_id": item["pin"]["media_id"],
                               "pin": copy.deepcopy(item["pin"])}
                    else:
                        row = next((row for row in self._verified_media if row.get("pin") == item["pin"]), None)
                    if row is not None and row not in preview_media:
                        preview_media.append(row)
                commands.append({"schema_version": 1, "op": "media.admit", "version": "1",
                                 "select": None, "args": {"inputs": inputs}})
            self._append(commands, label="Edit document", verified_media=preview_media)
            self._free_edits = True

    def _append(self, commands, *, label, intent="", tags=(), reads=None, verified_media=None):
        from .commands import CommandError, execute

        try:
            bundle = (self._execution_bundle(self._command_bundle)
                      if self._runtime_document is not None or any(command.get("op") == "doc.patch" for command in commands)
                      else self._command_bundle)
            plan = execute(bundle, commands, verified_media=self._verified_media if verified_media is None else verified_media)
        except CommandError as exc:
            if exc.kind == "unsupported_operation" and any(command.get("op") == "doc.patch" for command in commands):
                raise TimelineEditError(
                    "free edits require the G3 document emitter and the R1 doc.patch provider"
                ) from exc
            raise
        start = len(self._commands)
        patch = [copy.deepcopy(change) for command in commands if command["op"] == "doc.patch"
                 for change in command["args"]["changes"]]
        step = {"id": f"step-{len(self._steps) + 1}", "label": label, "intent": intent,
                "tags": list(tags), "command_ordinals": list(range(start, start + len(plan.commands))),
                "patch": patch}
        if reads is not None:
            step["reads"] = copy.deepcopy(reads)
        # Preserve caller guards; preview-generated captures omit the final
        # request/step reads. The provider captures against the pinned base.
        self._commands.extend(copy.deepcopy(commands))
        self._steps.append(step)
        self.bundle = copy.deepcopy(plan.bundle)
        self._runtime_document = self.bundle.get("source_mapping", {}).pop("_runtime_document", self._runtime_document)
        self._command_bundle = copy.deepcopy(self.bundle)
        self._refresh()

    def command(self, command, *, label="Timeline edit", intent="", tags=(), reads=None):
        """Stage one command/step without sending or writing local history.

        ``reads`` here declares dependencies at this step's sequential input;
        ``commit(reads=...)`` declares dependencies at batch entry.
        """
        if not self._evaluating:
            with self.session.outbox.locked():
                state = self.session._state()
                if state["pending"] or state["flight"] is not None:
                    raise TimelineEditError("reconcile the original request before another edit")
                if state["base"] != self._basis:
                    raise TimelineEditError("session base changed; reopen before editing")
        self._capture_free_edits()
        self._append([copy.deepcopy(command)], label=label, intent=intent, tags=tags, reads=reads)
        return self

    def move(self, select, on):
        """Stage a native move; commit the whole session with one label."""
        return self.command({"schema_version": 1, "op": "clip.move", "version": "1",
                             "select": select, "args": {"on": on}}, label="Move clip")

    def _accepted(self, result):
        from .commit import bundle_from_document

        if result["data"]["status"] != "preview":
            data = result["data"]
            previous_revision = data.get("previous_revision_id")
            previous_publication = data.get("previous_publication_id")
            accepted_revision = data.get("accepted_revision_id")
            if all(isinstance(value, str) and value for value in
                   (previous_revision, previous_publication, accepted_revision)):
                from .store import fetch_bundle

                if previous_revision == self._basis.get("revision_id"):
                    before = self._base_bundle
                else:
                    before = fetch_bundle(self.session.project_id, self.session.timeline_id,
                                          revision_id=previous_revision,
                                          publication_id=previous_publication,
                                          client=self.session.client)
                after = bundle_from_document(data["canonical_document"])
                ranges = _changed_ranges(before, after)
                data["changed_ranges"] = ranges
                result["changed_ranges"] = copy.deepcopy(ranges)
            # Adopt the entire landed document, including the other writer's edits.
            bundle = bundle_from_document(data["canonical_document"])
            bundle["base_parent"]["publication_id"] = result["receipt"]["receipt_id"]
            self._adopt(bundle)
            self._basis = {"revision_id": data["accepted_revision_id"],
                           "publication_id": result["receipt"]["receipt_id"]}
        return result

    def commit(self, label_or_fn="Timeline edit", *, label=None, merge="fields", dry_run=False,
               reads=None, key=None, idempotency_key=None, pending=None,
               _original_base=None, _overwrite=False):
        """Accept staged edits, a fixed command list, or evaluate ``fn`` up to three times.

        Only an explicit conflict permits reevaluation. Calling again after a
        transport/unknown outcome retries the durable original body/key first,
        even after reopening. Refused/invalid results never rerun the function.
        A command list is submitted as captured and never recaptured. Its
        default empty read set allows guarded disjoint edits to merge. Preview
        never becomes this session's base.
        """
        if self._evaluating:
            raise TimelineEditError("a commit function cannot commit its own evaluation")
        if isinstance(label_or_fn, Mapping) and label_or_fn.get("status") == "re_derive":
            raise TimelineEditError("a re-derive result is not a preview; follow r['next']")
        if isinstance(label_or_fn, Mapping) and isinstance(label_or_fn.get("data"), Mapping) \
                and isinstance(label_or_fn["data"].get("retry"), Mapping):
            if (label is not None or merge != "fields" or dry_run or reads is not None
                    or key is not None or idempotency_key is not None or pending is not None):
                raise TimelineEditError("retry preview commits do not accept additional commit options")
            from . import retry

            return retry.commit_preview(self, label_or_fn)
        if key is not None and idempotency_key is not None:
            raise TimelineEditError("pass only one of key or idempotency_key")
        if key is not None:
            idempotency_key = key
        if merge not in {"fields", "atomic"}:
            raise TimelineEditError("merge must be fields or atomic")
        if dry_run:
            idempotency_key = None
        function = label_or_fn if callable(label_or_fn) else None
        commands = label_or_fn if isinstance(label_or_fn, list) else None
        if function:
            commit_label = label or getattr(function, "__name__", "Timeline edit")
        elif commands is not None:
            commit_label = label or "Timeline edit"
        else:
            if label is not None and label_or_fn != "Timeline edit":
                raise TimelineEditError("pass the commit label either positionally or by keyword")
            commit_label = label or label_or_fn
        if not isinstance(commit_label, str) or not commit_label.strip():
            raise TimelineEditError("commit requires a nonempty label or a function")
        if commands is not None and idempotency_key is not None and not dry_run:
            actors = "self" if getattr(self.session, "_explicit_outbox", False) else "all"
            landed = [record for _, record in self._pending_records_with_paths(actors)
                      if pending is not None
                      and (record.get("id") == pending or record.get("key") == pending)
                      and record.get("state") == "landed"]
            landed_group_id = landed[0].get("id") if len(landed) == 1 else None
            steps = _list_steps(commands, commit_label)
            list_reads = [] if reads is None and merge == "fields" else reads
            with self.session.outbox.locked():
                active_flight = self.session._state().get("flight") is not None
            replay = self.session.retry(
                idempotency_key,
                matches=lambda body: ((landed_group_id is None
                                      or body.get("group_id") == landed_group_id)
                                      and body.get("commands") == commands
                                      and body.get("label") == commit_label
                                      and body.get("steps") == steps
                                      and body.get("merge") == merge
                                      and body.get("reads") == list_reads),
            )
            if replay is not None:
                return self._accepted(replay) if active_flight else replay
        pending_target = (self._resolve_pending(pending) if pending is not None else
                          self._resolve_pending_key(idempotency_key) if idempotency_key is not None else None)
        pending_id, pending_path, pending_part, pending_identity = pending_target if pending_target is not None else (None, None, None, None)
        if function:
            replay = self.session.resolve_acknowledged(idempotency_key, dry_run=dry_run)
            if replay is not None:
                return replay
        if commands is not None:
            return self._commit_commands(commands, label=commit_label, merge=merge, dry_run=dry_run,
                                         reads=reads, idempotency_key=idempotency_key,
                                         pending_id=pending_id, pending_path=pending_path, pending_part=pending_part,
                                         pending_identity=pending_identity)
        with self.session.outbox.locked():
            state = self.session._state()
        if _original_base is None and pending_id is not None:
            actors = "self" if getattr(self.session, "_explicit_outbox", False) else "all"
            pending_record = next((record for _, record in self._pending_records_with_paths(actors)
                                   if record.get("id") == pending_id), None)
            parts = pending_record.get("parts", []) if pending_record else []
            part_index = pending_record.get("part", 0) if pending_record else 0
            if isinstance(parts, list) and type(part_index) is int and 0 <= part_index < len(parts):
                _original_base = parts[part_index].get("base")
        original_base = _pin(_original_base or (self._basis if function else state["base"]))
        evaluation = state.get("evaluation") or {}
        count = evaluation.get("count", 0)
        if function and state["flight"] is not None and evaluation.get("kind") == "function":
            flight = state["flight"]
            logical_key = flight.get("logical_key", flight["key"] if flight.get("explicit_key") else None)
            if logical_key is not None:
                if idempotency_key is not None and idempotency_key != logical_key:
                    raise TimelineEditError("retry the original function idempotency key")
                idempotency_key = logical_key
        if state["pending"] or state["flight"] is not None:
            clash = None
            # A known conflict may be recomputed; every other outcome must first
            # reconcile the exact original request, without invoking user code.
            conflict = state.get("conflict")
            if not conflict or conflict.get("details", {}).get("status") != "conflict":
                if (self.bundle != self._command_bundle
                        or self._commands and self._commands != state["pending"]):
                    raise TimelineEditError("local edits differ from the original request; reopen to reconcile it")
                try:
                    result = self.session.flush(idempotency_key=idempotency_key, dry_run=dry_run,
                                                release_conflict=not function)
                except CommitRefused as exc:
                    if function:
                        exc.retry_fn = function
                        self._retry_functions[exc.pending_id] = function
                    raise
                except CommitConflict as exc:
                    conflict = exc.failure
                    pending_id, clash = getattr(exc, "pending_id", None), getattr(exc, "clash", None)
                    with self.session.outbox.locked():
                        state = self.session._state()
                        evaluation = state.get("evaluation") or evaluation
                else:
                    return self._accepted(result)
            if conflict.get("details", {}).get("status") != "conflict":
                error = CommitConflict(conflict, pending_id=pending_id, clash=clash)
                if function:
                    error.retry_fn = function
                raise error
            if not function or evaluation.get("kind") != "function" or count >= 3:
                error = CommitConflict(conflict, attempts=count if function and count >= 3 else None,
                                       label=commit_label, timeline_id=self.session.timeline_id,
                                       project_id=self.session.project_id,
                                       pending_id=pending_id, clash=clash)
                if function and count >= 3:
                    error.retry_fn = function
                raise error
            base = _pin(conflict["details"]["current"])
        else:
            base = self._basis if not function and state["base"] != self._basis else state["base"]
            count = 0

        while True:
            if function:
                if self._commands or self.bundle != self._command_bundle:
                    raise TimelineEditError("commit(fn) requires a clean local session")
                from .store import fetch_bundle

                bundle = fetch_bundle(self.session.project_id, self.session.timeline_id,
                                      revision_id=base["revision_id"], publication_id=base["publication_id"],
                                      client=self.session.client)
                before_bundle = copy.deepcopy(bundle)
                saved_cache = evaluation.get("media_cache", {})
                saved_identities = evaluation.get("identity_cache", {})
                working = Timeline(self.session, bundle, base, evaluating=True,
                                   media_cache=saved_cache, identity_cache=saved_identities)
                function(working)
                count += 1
            else:
                working = self
            working._capture_free_edits()
            if not working._commands:
                raise TimelineEditError("no pending timeline edits")
            steps = copy.deepcopy(working._steps)
            effective_merge = merge
            if reads is None and (function or working._free_edits
                                  or any(command["op"] == "doc.patch" for command in working._commands)):
                effective_merge = "atomic"
                # Arbitrary Python may consult any part of the loaded closure.
                # Keep omitted batch reads distinct from an empty declaration,
                # and retain this conservative dependency at each step boundary.
                for step in steps:
                    step.setdefault("reads", []).append({"kind": "scope", "ref": "timeline", "path": []})
            evaluation = {"kind": "function" if function else "label", "count": count,
                          "media_cache": copy.deepcopy(working._media_cache),
                          "identity_cache": copy.deepcopy(working._identity_cache)}
            if function and not _overwrite:
                overlap = _function_rerun_conflict(
                    self.session, original_base, base, before_bundle, working.bundle)
                if overlap is not None:
                    error = self.session.save_interrupted(
                        working._commands, base=original_base, label=commit_label, steps=steps,
                        merge=effective_merge, reads=reads, key=idempotency_key,
                        evaluation=evaluation, failure=overlap, release_flight=state.get("flight"),
                        release_dry_run=dry_run, pending_id=pending_id,
                        pending_path=pending_path, pending_part=pending_part, pending_identity=pending_identity)
                    error.retry_fn = function
                    self._retry_functions[error.pending_id] = function
                    raise error
            if not function and state["base"] != self._basis:
                if dry_run or pending_id is not None:
                    raise TimelineEditError("session base changed; reopen before committing")
                error = self.session.save_interrupted(working._commands, base=base, label=commit_label,
                                                      steps=steps, merge=effective_merge, reads=reads,
                                                      key=idempotency_key, evaluation=evaluation)
                self._adopt(self._base_bundle)
                raise error
            try:
                result = self.session.submit(
                    working._commands, label=commit_label, steps=steps, merge=effective_merge, reads=reads,
                    idempotency_key=idempotency_key, dry_run=dry_run, expected_state=state, base=base,
                    evaluation=evaluation,
                    pending_id=pending_id,
                    pending_path=pending_path,
                    pending_part=pending_part,
                    pending_identity=pending_identity,
                    pending_how="rerun" if function else "rederive",
                )
            except _OutboxChanged:
                if dry_run or (pending_id is not None and not function):
                    raise
                if function and count < 3:
                    with self.session.outbox.locked():
                        latest = self.session._state()
                    if (not latest["pending"] and latest["flight"] is None
                            and latest["base"] != state["base"]):
                        # A peer using this actor's shared outbox completed while
                        # the function was evaluating. Re-read its new head and
                        # let the normal overlap guard protect the re-evaluation.
                        state = latest
                        base = _pin(latest["base"])
                        continue
                error = self.session.save_interrupted(working._commands, base=original_base if function else base, label=commit_label,
                                                      steps=steps, merge=effective_merge, reads=reads,
                                                      key=idempotency_key, evaluation=evaluation,
                                                      pending_id=pending_id, pending_path=pending_path,
                                                      pending_part=pending_part, pending_identity=pending_identity)
                if function:
                    error.retry_fn = function
                    self._retry_functions[error.pending_id] = function
                if working is self:
                    self._adopt(self._base_bundle)
                raise error
            except CommitRefused as exc:
                if function:
                    exc.retry_fn = function
                    self._retry_functions[exc.pending_id] = function
                raise
            except CommitConflict as exc:
                if exc.pending_id is not None and working is self:
                    self._adopt(self._base_bundle)
                retryable = exc.failure["details"].get("status") == "conflict"
                if not function or not retryable or count >= 3:
                    if function and not retryable:
                        exc.retry_fn = function
                        self._retry_functions[exc.pending_id] = function
                    if function and retryable and count >= 3:
                        exhausted = CommitConflict(exc.failure, attempts=count, label=commit_label,
                                                   timeline_id=self.session.timeline_id,
                                                   project_id=self.session.project_id,
                                                   pending_id=getattr(exc, "pending_id", None),
                                                   clash=getattr(exc, "clash", None))
                        exhausted.retry_fn = function
                        self._retry_functions[exhausted.pending_id] = function
                        raise exhausted from exc
                    raise
                base = _pin(exc.failure["details"]["current"])
                with self.session.outbox.locked():
                    state = self.session._state()
                    evaluation = state.get("evaluation") or evaluation
                continue
            if dry_run:
                self._add_preview_diff(result, working.bundle)
            if function and count > 1:
                result["attempts"] = count
                result["data"]["attempts"] = count
                summary = result.get("summary", "")
                summary_base, separator, media = summary.partition(" Media:")
                result["summary"] = summary_base.rstrip(".") + f" after re-reading the timeline ({count} attempts)."
                if separator:
                    result["summary"] += " Media:" + media
                result["data"]["summary"] = result["summary"]
            return self._accepted(result)

    def retry(self, err_or_ref, fn=None, *, overwrite=False):
        """Retry a saved edit, retaining an exhausted function when available."""
        if isinstance(err_or_ref, CommitConflict) and not err_or_ref.pending_id:
            return {"status": "refused", "pending_id": None,
                    "data": copy.deepcopy(err_or_ref.failure),
                    "summary": str(err_or_ref), "next": None}
        ref = getattr(err_or_ref, "pending_id", None) or err_or_ref
        retry_fn = fn if fn is not None else getattr(err_or_ref, "retry_fn", None)
        if retry_fn is None and overwrite:
            retry_fn = self._retry_functions.get(ref)
        from . import retry

        return retry.retry(self, ref, retry_fn, overwrite=overwrite)

    def _pending_records_with_paths(self, actors):
        if actors not in {"self", "all"}:
            raise TimelineEditError("actors must be 'self' or 'all'")
        with self.session.outbox.locked():
            state = self.session._state()
            scope = {key: state["scope"][key] for key in ("realm_id", "project_id", "timeline_id")}
            paths = [self.session.outbox.path]
            if actors == "all" and not getattr(self.session, "_explicit_outbox", False):
                paths = sorted(self.session.outbox.path.parent.glob("*.json"))
        visible, seen = [], set()
        for path in paths:
            from astrid.sdk.timeline.outbox import Outbox

            outbox = self.session.outbox if path == self.session.outbox.path else Outbox(path)
            with outbox.locked():
                saved = outbox.read()
                if not isinstance(saved, dict) or not isinstance(saved.get("scope"), dict):
                    continue
                if any(saved["scope"].get(key) != value for key, value in scope.items()):
                    continue
                for record in saved.get("pending_changes", {}).values():
                    if not isinstance(record, dict) or record.get("id") in seen:
                        continue
                    seen.add(record.get("id"))
                    visible.append((path, copy.deepcopy(record)))
        return visible

    def _resolve_pending(self, ref):
        if not isinstance(ref, str) or not ref:
            raise TimelineEditError("pending must be a pending id or key")
        actors = "self" if getattr(self.session, "_explicit_outbox", False) else "all"
        matches = [(path, record) for path, record in self._pending_records_with_paths(actors)
                   if record.get("id") == ref or record.get("key") == ref]
        if not matches:
            raise TimelineEditError(f"no pending change with id or key {ref!r}")
        if len(matches) > 1:
            raise TimelineEditError(f"pending key {ref!r} is ambiguous; use a pending id")
        path, record = matches[0]
        if record.get("state") != "pending":
            raise TimelineEditError(f"cannot close pending change in state {record.get('state')!r}")
        return record["id"], str(path), record.get("part", 0), _part_identity(record)

    def _resolve_pending_key(self, key):
        """Find a pending edit created with this commit key, if it is unique."""
        actors = "self" if getattr(self.session, "_explicit_outbox", False) else "all"
        matches = [(path, record) for path, record in self._pending_records_with_paths(actors)
                   if record.get("state") == "pending" and record.get("key") == key]
        if len(matches) > 1:
            raise TimelineEditError(f"pending key {key!r} is ambiguous; use pending= with a pending id")
        if not matches:
            return None
        path, record = matches[0]
        return record["id"], str(path), record.get("part", 0), _part_identity(record)

    def _commit_commands(self, commands, *, label, merge, dry_run, reads, idempotency_key,
                         pending_id=None, pending_path=None, pending_part=None, pending_identity=None):
        """Submit an already captured list without rebuilding its guards or media pins."""
        if not commands:
            raise TimelineEditError("commit requires a nonempty command list")
        if self._commands or self.bundle != self._command_bundle:
            raise TimelineEditError("commit(command_list) requires a clean local session")
        with self.session.outbox.locked():
            state = self.session._state()
        steps = _list_steps(commands, label)
        try:
            list_reads = [] if reads is None and merge == "fields" else reads
            flight = state.get("flight")
            if flight is not None and pending_id is not None:
                body = json.loads(flight["body"])
                if (flight.get("pending_id") != pending_id or body.get("commands") != commands
                        or body.get("label") != label or body.get("steps") != steps
                        or body.get("merge") != merge or body.get("reads") != list_reads
                        or body.get("dry_run", False) != dry_run):
                    raise TimelineEditError("retry the original in-flight request before another edit")
                return self._accepted(self.session.flush(idempotency_key=idempotency_key,
                                                         dry_run=dry_run, release_conflict=True))
            if state["base"] != self._basis:
                if dry_run or pending_id is not None:
                    raise TimelineEditError("session base changed; reopen before committing")
                raise self.session.save_interrupted(commands, base=self._basis, label=label,
                                                    steps=steps, merge=merge, reads=list_reads,
                                                    key=idempotency_key, evaluation={"kind": "list"})
            result = self.session.submit(commands, label=label, steps=steps, merge=merge, reads=list_reads,
                                         idempotency_key=idempotency_key, dry_run=dry_run,
                                         expected_state=state, base=state["base"],
                                         pending_id=pending_id, pending_path=pending_path, pending_part=pending_part,
                                         pending_how="rederive", pending_identity=pending_identity, imports=_imports_in_commands(commands))
        except _OutboxChanged:
            if dry_run or pending_id is not None:
                raise
            raise self.session.save_interrupted(commands, base=self._basis, label=label,
                                                steps=steps, merge=merge, reads=list_reads,
                                                key=idempotency_key, evaluation={"kind": "list"})
        except CommitConflict as exc:
            if exc.failure.get("code") == "local_outbox_changed":
                raise
            if exc.failure.get("details", {}).get("status") == "conflict":
                raise CommitConflict(exc.failure, label=label, fixed_list=True,
                                     timeline_id=self.session.timeline_id,
                                     project_id=self.session.project_id,
                                     pending_id=getattr(exc, "pending_id", None),
                                     clash=getattr(exc, "clash", None)) from exc
            raise
        if not result.get("imports"):
            imports = _imports_in_commands(commands)
            if imports:
                result["imports"] = imports
                result["data"]["imports"] = copy.deepcopy(imports)
                summary = result.get("summary", "")
                result["summary"] = summary + f" Media: {len(imports)} file(s) in the catalog; undo keeps them."
                result["data"]["summary"] = result["summary"]
        if dry_run:
            self._add_preview_diff(result, self.bundle)
        return self._accepted(result)

    @staticmethod
    def _add_preview_diff(result, before_bundle):
        """Pair Runtime's preview commands with a diff of authored document fields."""
        from .commit import bundle_from_document
        from .emitter import _diff, _payload_for_emit, _root_pairs

        data = result.get("data", {})
        document = data.get("canonical_document")
        if isinstance(document, Mapping):
            after_bundle = bundle_from_document(document)
            changes = []
            for ref, before, after in _root_pairs(before_bundle, after_bundle):
                _diff(_payload_for_emit(ref, before), _payload_for_emit(ref, after), ref, (), changes)
            if not changes:
                changes = [{"op": command["op"], "ref": command.get("select"),
                            "args": copy.deepcopy(command["args"])}
                           for command in data.get("commands", [])]
            data.setdefault("diff", {"changes": changes})

    def pending_list(self, *, all=False, actors="self"):
        """List this timeline's saved refused edits."""
        records = self._pending_records_with_paths(actors)
        visible = [record for _, record in records
                   if all or record.get("state") == "pending"]
        return sorted(visible, key=lambda record: (record.get("created_at", ""), record.get("id", "")))

    def _pending_record(self, ref, state):
        records = state.get("pending_changes", {})
        if ref in records:
            return records[ref]
        matches = [record for record in records.values() if record.get("key") == ref]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise TimelineEditError(f"pending key {ref!r} is ambiguous; use a pending id")
        raise TimelineEditError(f"no pending change with id or key {ref!r}")

    def pending_show(self, ref):
        """Return a visible saved record, enriching conflicts only in this actor's outbox."""
        actors = "self" if getattr(self.session, "_explicit_outbox", False) else "all"
        matches = [(path, record) for path, record in self._pending_records_with_paths(actors)
                   if record.get("id") == ref or record.get("key") == ref]
        if not matches:
            raise TimelineEditError(f"no pending change with id or key {ref!r}")
        if len(matches) > 1:
            raise TimelineEditError(f"pending key {ref!r} is ambiguous; use a pending id")
        path, record = matches[0]
        if path == self.session.outbox.path and record.get("state") == "pending" and record.get("diff") is None:
            record = _enrich_pending_diff(self.session, record)
        if (path == self.session.outbox.path and record.get("state") == "pending"
                and (record.get("failure", {}).get("details") or {}).get("status") == "conflict"
                and (not record.get("clash") or record["clash"].get("receipts") is None)):
            record = _pending_clash(self.session, record)
        return record

    def pending_drop(self, ref, *, reason=None):
        """Mark a saved pending edit dropped without deleting its record."""
        if reason is not None and not isinstance(reason, str):
            raise TimelineEditError("pending drop reason must be text")
        with self.session.outbox.locked():
            state = self.session._state()
            record = self._pending_record(ref, state)
            if record.get("state") not in {"pending", "dropped"}:
                raise TimelineEditError(f"cannot drop pending change in state {record.get('state')!r}")
            if record.get("state") != "dropped":
                record["state"] = "dropped"
                record["rev"] = int(record.get("rev", 0)) + 1
                record["updated_at"] = datetime.now(timezone.utc).isoformat()
                record["dropped_reason"] = reason
                self.session.outbox.write(state)
            return copy.deepcopy(record)

    def save(self, *args, **kwargs):
        raise TimelineEditError("use commit(label) to save a Runtime timeline")

    def publish(self, *args, **kwargs):
        raise TimelineEditError("use commit(label) to publish a Runtime timeline")

    def duplicate(self, *, as_):
        """Create a named copy of this timeline, returning setup and copy results separately."""
        from astrid.sdk.timeline_duplicate import _duplicate_key, duplicate_timeline

        if not isinstance(as_, str) or not as_.strip():
            raise TimelineEditError("duplicate requires a nonempty destination name")
        return duplicate_timeline(
            self.session.project_id,
            self.session.timeline_id,
            slug=as_,
            client=self.session.client,
            idempotency_key=_duplicate_key(self.session.project_id, self.session.timeline_id, as_),
        )


def open(project=None, timeline=None, *, client=None, outbox_path=None):
    """Open a Runtime head, retaining any durable request awaiting reconciliation.

    With no explicit client, use the configured launcher connection. With no
    project/timeline, use Runtime's current project and normal timeline resolver.
    The optional outbox path isolates independent sessions in the same scope.
    """
    from astrid.sdk import AstridClient

    from .store import fetch_bundle

    client = client if client is not None else AstridClient.open_from_launcher(start_pack_host=False)
    session = TimelineSession.open(project, timeline, client=client, outbox_path=outbox_path)
    with session.outbox.locked():
        base = session._state()["base"]
    bundle = fetch_bundle(session.project_id, session.timeline_id,
                          revision_id=base["revision_id"], publication_id=base["publication_id"], client=client)
    return Timeline(session, bundle, base)
