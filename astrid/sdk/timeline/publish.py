"""Checkout: publish, narration binding, and the document as it is saved (annotated) and read back (absorb)."""
from __future__ import annotations

import copy
import hashlib
import json
import re
import sys
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping

from astrid.core.timeline.cuts import (
    clip_duration,
)
from astrid.sdk import timeline_intent as intent
from astrid.sdk import (
    timeline_transcripts as _transcripts,  # module access: its functions resolve at call time
)
from astrid.sdk.timeline import journal as _journal
from astrid.sdk.timeline import store as _store
from astrid.sdk.timeline._common import EDITABLE_NOTE, TimelineEditError, _num, _r
from astrid.sdk.timeline.authoring_commit import response_data
from astrid.sdk.timeline.doc import _row_start, _set_length, _strip_derived
from astrid.sdk.timeline.model import Voice

_NARRATION_MEDIA_TYPE = "text/plain; charset=utf-8"


def _media_admission_inputs(base: Mapping[str, Any], candidate: Mapping[str, Any], client: Any) -> list[dict[str, Any]]:
    """Capture the Runtime inputs required by an ordinary authored candidate."""
    from astrid.core.timeline.authoring_bundle import compile_authoring_candidate
    from astrid.sdk.timeline._command_state import equal
    from astrid.sdk.timeline.native_media_admission import registry_selections

    source = base["source_mapping"]["_runtime_document"]
    publication = compile_authoring_candidate(candidate).publication
    inputs = [{"pin": copy.deepcopy(pin)} for pin in publication["dependency_manifest"]["media"]
              if pin not in source["dependency_manifest"]["media"]]
    from .commit import bundle_from_document

    old_selections = list(registry_selections(bundle_from_document(source)))
    for association in registry_selections(candidate):
        if association in old_selections:
            continue
        selected = response_data(client.get_project_media_selection(
            candidate["project_id"],
            {key: association[key] for key in ("asset", "generation_id", "variant_id")},
        ))
        if not equal({key: selected.get(key) for key in association}, association):
            raise TimelineEditError("Runtime catalog association differs from the candidate media")
        pin = {"media_id": selected["object_id"], "content_digest": selected["object_id"]}
        value = {"pin": pin, "selection": selected}
        inputs = [row for row in inputs if row != {"pin": pin}]
        if value not in inputs:
            inputs.append(value)
    return inputs


def _narration_descriptor(text: str) -> tuple[dict[str, Any], bytes]:
    """Return the exact managed-object descriptor and bytes for one narration line."""
    raw = text.encode("utf-8")
    digest = "sha256:" + hashlib.sha256(raw).hexdigest()
    return ({"object_id": digest, "digest": digest, "size": len(raw),
             "media_type": _NARRATION_MEDIA_TYPE}, raw)


def _narration_occurrence(bundle: Mapping[str, Any], shot_id: str) -> str:
    """Resolve a narration shot to its sole occurrence-owned binding target."""
    matches = [row for row in bundle.get("placements") or []
               if isinstance(row, Mapping) and row.get("shot_id") == shot_id]
    if len(matches) != 1 or not isinstance(matches[0].get("occurrence_id"), str):
        raise TimelineEditError(
            f"narration binding for shot {shot_id!r} requires one exclusive occurrence; "
            "the current Main shot binding has no commit-owned occurrence seam"
        )
    return "occurrence:" + matches[0]["occurrence_id"]


def _capture_narration_commands(
    bundle: dict[str, Any], changes: Mapping[str, str]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Capture narration changes as native commands and update a local preview.

    Runtime re-verifies each descriptor at commit admission.  The local preview
    only supplies the exact command guards; it never authorizes a film write.
    """
    from astrid.sdk.timeline.commands import execute
    from astrid.sdk.timeline.native_text_binding import capture_text_binding_set

    local = copy.deepcopy(bundle)
    commands: list[dict[str, Any]] = []
    verified: list[dict[str, Any]] = []
    for shot_id, text in changes.items():
        owner = _narration_occurrence(local, shot_id)
        descriptor, raw = _narration_descriptor(text)
        verified_descriptor = {**descriptor, "utf8": True}
        command = capture_text_binding_set(
            local, owner, {"kind": "voiceover_script", "slot": None},
            text=descriptor, verified_text=[verified_descriptor],
        )
        commands.append(command)
        verified.append(verified_descriptor)
        local = execute(local, [command], verified_media=[verified_descriptor]).bundle
        # Keep the bytes alive through this helper's boundary.  The descriptor
        # is the custody witness; Runtime performs the authoritative read.
        if len(raw) != descriptor["size"]:
            raise TimelineEditError(f"narration source bytes changed for shot {shot_id!r}")
    return commands, verified


class _PublishMixin:
    """Checkout: publish, narration binding, and the document as it is saved (annotated) and read back (absorb)."""

    bundle: dict[str, Any]  # set by Checkout.__init__; declared so this mixin's readers are typed
    _journal: list[tuple[str, str | None]]

    if TYPE_CHECKING:
        def _retire_draft_history(self) -> None: ...

    def publish(self, message: str = "", *, idempotency_key: str | None = None, client: Any = None,
                force: bool = False, restructure: bool = False) -> dict[str, Any]:
        """Commit this checkout through Runtime, then reconcile narration and transcripts."""
        if force:
            raise TimelineEditError("publication uses guarded commits; force is unavailable")
        self.notes.extend(self._sync_film_length())  # the film's length follows its content, before the check
        # Publication is guarded by the structural/core validator only.  The
        # full check may import the renderer and evaluate visual diagnostics,
        # neither of which is part of the writer's safety decision.
        if restructure:
            from astrid.sdk.timeline.checkout import Checkout

            report = Checkout(copy.deepcopy(self.bundle))._check_core()
        else:
            checked = copy.copy(self)
            checked.bundle = copy.deepcopy(self.bundle)
            report = checked._check_core()
        if not report.valid:
            raise TimelineEditError("not publishing: " + "; ".join(report.problems or ["the candidate is invalid"]))
        key = idempotency_key or f"edit-{re.sub(r'[^a-z0-9]+', '-', message.lower())[:40].strip('-') or 'timeline'}-{uuid.uuid4().hex[:8]}"
        stored = self.stored_document(restructure=restructure)  # chapters as shots only for a film that opted in
        if restructure:
            receipt = self._commit_restructure(message, idempotency_key=idempotency_key, client=client, force=force)
            key = receipt.pop("_restructure_key")
            receipt.update(idempotency_key=key, structure="changed", merged=None)
        else:
            receipt = self._commit_document(stored, message, idempotency_key=idempotency_key, client=client)
            key = receipt.get("idempotency_key", key)
        receipt["narration_pinned"] = []
        if restructure or receipt.pop("_narration_pending", False) or self.narration_changes():
            from astrid.sdk.workspace_client import WorkspaceClientError

            # A line's text changed (or the shots are new): bind each such shot's narration now that
            # the shots exist, then publish the pins as a second, small revision.
            try:
                receipt.update(self._publish_narration(key, client=client))
            except (TimelineEditError, WorkspaceClientError) as exc:  # the cut is already durable
                receipt["narration_error"] = (f"{exc}. The cut is published (head {receipt.get('new_head')}); only the "
                                              "narration binding is behind: retry the original publish to reconcile "
                                              "the durable narration commit")
        # Media is ingested by _register_asset before this point; its registry
        # entry now carries the stable object id. One document covers each take.
        receipt["transcripts"] = []
        project_id = str(stored["project_id"])  # the published document names its project
        try:
            transcript_plan = _transcripts.plan_narration_transcripts(self)
            if any(item["status"] == "planned" for item in transcript_plan):
                if client is None:
                    from astrid.sdk import AstridClient

                    with AstridClient.open_from_launcher(start_pack_host=False) as transcript_client:
                        receipt["transcripts"] = _transcripts.sync_narration_transcripts(self, transcript_client, project_id)
                else:
                    receipt["transcripts"] = _transcripts.sync_narration_transcripts(self, client, project_id)
        except Exception as exc:  # noqa: BLE001 - containment: publication already succeeded
            print(f"note: narration transcript registration failed: {exc}", file=sys.stderr)
        for item in receipt["transcripts"]:
            if item["status"] == "skipped":
                print(f"note: narration transcript {item['label']}: {item.get('reason', 'skipped')}", file=sys.stderr)
        if self.path:  # publish resets the base: this working copy is the new head, with no changes (AUDIT bug 6)
            before = _journal.document_digest(self.document())
            self._reset_base(client=client)
            _journal.record_or_note(self, Path(self.path), kind="publish", outcome="changed", label="publish",
                                    reason=message or None, before=before,
                                    after=_journal.document_digest(self.document()),
                                    evidence=({"kind": "revision", "id": receipt.get("new_head")},))
        receipt["message"] = receipt.pop("_message", message)
        return receipt

    def _commit_document(self, stored: Mapping[str, Any], message: str, *,
                         idempotency_key: str | None, client: Any) -> dict[str, Any]:
        from astrid.sdk.timeline._command_state import equal
        from astrid.sdk.timeline.emitter import emit_document_commands
        from astrid.sdk.timeline.sync import TimelineSession

        if client is None:
            from astrid.sdk import AstridClient

            with AstridClient.open_from_launcher(start_pack_host=False) as opened:
                return self._commit_document(stored, message, idempotency_key=idempotency_key, client=opened)
        project, timeline = str(stored["project_id"]), str(stored["timeline_id"])
        session = TimelineSession.open(project, timeline, client=client)
        with session.outbox.locked():
            state = session._state()
        base_revision = self.base_revision
        changed = self.edits().get("changes")
        flight = state["flight"]
        key = idempotency_key
        accepted = None
        authored = {"parent": stored["parent"], "placements": stored["placements"],
                    "shots": {identity: {field: shot[field] for field in ("payload", "internal_timeline")}
                              for identity, shot in stored["shots"].items()}}
        candidate_digest = hashlib.sha256(json.dumps(authored, sort_keys=True).encode()).hexdigest()
        entries = []
        cursor, seen = None, set()
        while True:
            history = client.timelines.history(project, timeline, cursor=cursor, limit=200)
            if not history.ok:
                raise TimelineEditError(f"could not read publication history: {history.error}")
            entries.extend(history.data["items"])
            cursor = history.data.get("next_cursor")
            if cursor is None:
                break
            if cursor in seen:
                raise TimelineEditError("publication history cursor repeated")
            seen.add(cursor)
        if flight is not None:
            body = json.loads(flight["body"])
            if body.get("label") == "Bind narration":
                key = flight["key"].removesuffix("-narration")
            elif (state.get("evaluation") or {}).get("kind") == "checkout-publish":
                if message and message != body["label"]:
                    raise TimelineEditError("in-flight request is immutable; retry its original label")
                if (candidate_digest != state["evaluation"]["candidate_digest"]
                        and (base_revision == body["base_revision_id"] or changed)):
                    raise TimelineEditError("local edits differ from the original request; reopen to reconcile it")
                result = session.flush(idempotency_key=idempotency_key)
                return {"old_head": result["data"].get("previous_revision_id"),
                        "new_head": result["data"]["accepted_revision_id"],
                        "receipt": result["receipt"], "idempotency_key": flight["key"],
                        "structure": "changed", "merged": result["data"]["status"] == "merged",
                        "_message": body["label"]}
            else:
                raise TimelineEditError("reconcile the original timeline request before publishing")
        if key is not None:
            for entry in entries:
                receipt = entry.get("receipt") or {}
                if receipt.get("idempotency_key") == key:
                    accepted = entry
                    if (receipt["result"].get("label") != (message or "Timeline edit")
                            or not all(record["command"]["op"] in {"doc.patch", "media.admit"}
                                       for record in (entry.get("records") or {}).get("commands", []))):
                        raise TimelineEditError("the idempotency key belongs to a different timeline commit")
        if flight is not None and accepted is not None and not changed:
            receipt = accepted["receipt"]
            return {"old_head": receipt["result"].get("previous_revision_id"),
                    "new_head": accepted["revision_id"], "receipt": receipt,
                    "idempotency_key": key, "structure": "unchanged", "_narration_pending": True}
        if state["pending"] or flight is not None:
            raise TimelineEditError("flush pending commands before publishing")
        original_publication = stored["base_parent"].get("publication_id")
        matches = [entry for entry in entries if entry["revision_id"] == base_revision
                   and (original_publication is None or entry["publication_id"] == original_publication)]
        if len(matches) != 1:
            raise TimelineEditError("checkout has no unique publication base; reopen the timeline")
        base_pair = {"revision_id": base_revision, "publication_id": matches[0]["publication_id"]}
        base = _store.fetch_bundle(project, timeline, revision_id=base_revision,
                                   publication_id=base_pair["publication_id"], client=client)
        provenance = _strip_derived(base)
        if (stored["base_parent"] != base["base_parent"]
                or set(stored["shots"]) != set(base["shots"])
                or not equal(stored["placements"], base["placements"])
                or any(not equal(stored.get(field), provenance.get(field))
                       for field in ("source_mapping", "base_parent_payload", "base_placements"))
                or any(not equal({field: value for field, value in shot.items()
                                  if field not in {"payload", "internal_timeline"}},
                                 {field: value for field, value in provenance["shots"][identity].items()
                                  if field not in {"payload", "internal_timeline"}})
                       for identity, shot in stored["shots"].items())):
            raise TimelineEditError("closure membership changes require a semantic command")
        candidate = copy.deepcopy(base)
        candidate["parent"] = copy.deepcopy(stored["parent"])
        for identity, shot in stored["shots"].items():
            for field in ("payload", "internal_timeline"):
                candidate["shots"][identity][field] = copy.deepcopy(shot[field])
        commands = emit_document_commands(base, candidate)
        captured_helpers = _journal.helper_batch(self, base, candidate)
        if captured_helpers is not None:
            commands = captured_helpers[0]
        authored_command_count = len(commands)
        media_inputs: list[dict[str, Any]] = []
        if commands:
            prior_commands = ((accepted or {}).get("receipt") or {}).get("result", {}).get("commands", [])
            prior_media = [copy.deepcopy(call) for call in prior_commands if call.get("op") == "media.admit"]
            if prior_media:
                commands.extend(prior_media)
                media_inputs = copy.deepcopy(prior_media[-1]["args"]["inputs"])
            else:
                media_inputs = _media_admission_inputs(base, candidate, client)
            if media_inputs and not prior_media:
                commands.append({"schema_version": 1, "op": "media.admit", "version": "1",
                                 "select": None, "args": {"inputs": media_inputs}})
        # Acknowledged cuts can still have an unstarted narration follow-up.
        # Recover their identity from Runtime history, never a second journal.
        for entry in entries:
            prior = entry.get("receipt") or {}
            result = prior.get("result") or {}
            captured = result.get("commands") or []
            if (commands and entry.get("previous") == base_pair
                    and result.get("label") == (message or "Timeline edit")
                    and len(captured) == len(commands)
                    and all(equal({field: record.get(field) for field in call}, call)
                            for call, record in zip(commands, captured))
                    and (key is None or key == prior.get("idempotency_key"))):
                return {"old_head": result.get("previous_revision_id"),
                        "new_head": entry["revision_id"], "receipt": prior,
                        "idempotency_key": prior["idempotency_key"], "structure": "unchanged"}
        key = key or f"timeline-edit-{uuid.uuid4().hex}"
        if not commands:
            if accepted is not None:
                receipt = accepted["receipt"]
                return {"old_head": receipt["result"].get("previous_revision_id"),
                        "new_head": accepted["revision_id"], "receipt": receipt,
                        "idempotency_key": key, "structure": "unchanged"}
            return {"old_head": base_revision, "new_head": base_revision,
                    "structure": "unchanged", "idempotency_key": key}
        steps = captured_helpers[1] if captured_helpers is not None else [
            {"id": "edit", "label": message or "Timeline edit", "intent": message,
             "tags": [], "command_ordinals": list(range(authored_command_count)),
             "patch": [copy.deepcopy(change) for call in commands if call["op"] == "doc.patch"
                       for change in call["args"]["changes"]],
             "reads": [{"kind": "scope", "ref": "timeline", "path": []}]}
        ]
        if media_inputs:
            steps.append({"id": "media", "label": "Capture timeline media", "intent": "",
                          "tags": [], "command_ordinals": [authored_command_count], "patch": []})
        result = session.submit(commands, label=message or "Timeline edit",
            merge="fields" if captured_helpers is not None else "atomic",
            reads=captured_helpers[1][0]["reads"] if captured_helpers is not None else None,
            steps=steps,
            expected_state=state, base=base_pair, idempotency_key=key,
            evaluation={"kind": "checkout-publish", "count": 0, "candidate_digest": candidate_digest})
        return {"old_head": result["data"].get("previous_revision_id"),
                "new_head": result["data"]["accepted_revision_id"], "receipt": result["receipt"],
                "idempotency_key": key, "structure": "changed", "merged": result["data"]["status"] == "merged"}

    def _commit_restructure(self, message: str, *, idempotency_key: str | None,
                            client: Any, force: bool) -> dict[str, Any]:
        """Commit the cut once; an interrupted follow-up keeps its original cut receipt."""
        from astrid.sdk.timeline.emitter import emit_document_commands
        from astrid.sdk.timeline.sync import TimelineSession
        from astrid.sdk.timeline_restructure import STRUCTURE_KEY

        if force:
            raise TimelineEditError("restructure uses guarded commits; force is unavailable")
        if client is None:
            from astrid.sdk import AstridClient

            with AstridClient.open_from_launcher(start_pack_host=False) as opened:
                return self._commit_restructure(message, idempotency_key=idempotency_key,
                                                client=opened, force=False)
        project, timeline = str(self.bundle["project_id"]), str(self.bundle["timeline_id"])
        session = TimelineSession.open(project, timeline, client=client)
        with session.outbox.locked():
            state = session._state()
        flight = state.get("flight")
        key = idempotency_key or "restructure-" + hashlib.sha256(
            json.dumps([project, timeline, self.bundle["base_parent"], message], sort_keys=True).encode()
        ).hexdigest()[:64]
        if flight is not None:
            body = json.loads(flight["body"])
            if body.get("label") == "Bind narration":
                key = flight["key"].removesuffix("-narration")
            elif any(call.get("op") == "timeline.restructure" for call in body.get("commands", [])):
                key = flight["key"]
            else:
                raise TimelineEditError("reconcile the original request before restructuring")
        # History is Runtime-owned. It also covers a process restart between
        # accepting the cut and staging its narration, without a second journal.
        cursor = None
        seen: set[str] = set()
        narration_flight = flight is not None and body.get("label") == "Bind narration"
        while True:
            history = client.timelines.history(project, timeline, cursor=cursor, limit=200)
            if not history.ok:
                raise TimelineEditError(f"could not read restructure history: {history.error}")
            for entry in history.data["items"]:
                receipt = entry.get("receipt") or {}
                cut = any(record['command']['op'] == 'timeline.restructure'
                          for record in (entry.get('records') or {}).get('commands', []))
                if (flight is None and idempotency_key is None and STRUCTURE_KEY in self.bundle
                        and entry['publication_id'] == state['base']['publication_id'] and cut):
                    key = receipt['idempotency_key']
                if receipt.get("idempotency_key") == key and (flight is None or narration_flight):
                    if not cut:
                        raise TimelineEditError("the idempotency key belongs to a different timeline commit")
                    return {"old_head": receipt["result"].get("previous_revision_id"),
                            "new_head": entry["revision_id"], "receipt": receipt, "_restructure_key": key}
            cursor = history.data.get("next_cursor")
            if cursor is None:
                break
            if cursor in seen:
                raise TimelineEditError("restructure history cursor repeated")
            seen.add(cursor)
        if narration_flight:
            raise TimelineEditError("the original restructure receipt is unavailable; reconcile narration directly")
        if flight is not None:
            result = session.flush(idempotency_key=key)
        else:
            if state["pending"]:
                raise TimelineEditError("flush pending commands before restructuring")
            base = session._capture_bundle(state)
            local = self.document()
            # Detached checkouts may have been opened by revision alone. Bind
            # that original revision through the verified public pair reader;
            # retain local pins and provenance so mismatches still refuse.
            original = _store.fetch_bundle(
                project, timeline, revision_id=local["base_parent"]["revision_id"],
                publication_id=local["base_parent"].get("publication_id", state["base"]["publication_id"]),
                client=client,
            )
            if any(original["base_parent"][field] != state["base"][field]
                   for field in ("revision_id", "publication_id")):
                raise TimelineEditError("original revision/publication pair changed")
            local["base_parent"] = {**original["base_parent"], **local["base_parent"]}
            local["source_mapping"] = {**original["source_mapping"], **local["source_mapping"]}
            if (local["base_parent"] != base["base_parent"]
                    or set(local["shots"]) != set(base["shots"])):
                raise TimelineEditError("restructure requires its exact current closure; reopen the timeline")
            candidate = copy.deepcopy(base)
            candidate["parent"] = local["parent"]
            for identity, shot in local["shots"].items():
                for field in ("payload", "internal_timeline"):
                    candidate["shots"][identity][field] = shot[field]
            from astrid.sdk.timeline.checkout import Checkout

            checked = Checkout(copy.deepcopy(candidate))
            checked._sync_film_length()
            if local["placements"] != checked.bundle["placements"]:
                raise TimelineEditError("restructure placement edits require a semantic command")
            commands = emit_document_commands(base, candidate)
            commands.append({"schema_version": 1, "op": "timeline.restructure", "version": "1",
                             "select": "parent", "args": {}})
            from astrid.sdk.timeline.commands import execute

            commands[-1] = execute(candidate, [commands[-1]]).commands[0]
            result = session.submit(commands, label=message or "Restructure chapters", merge="atomic",
                reads=[{"kind": "scope", "ref": "timeline", "path": []}],
                steps=[{"id": f"restructure-{index}", "label": message or "Restructure chapters",
                        "intent": "Store chapters as shots", "tags": ["restructure"],
                        "command_ordinals": [index],
                        "patch": copy.deepcopy(call['args']['changes']) if call['op'] == 'doc.patch' else []}
                       for index, call in enumerate(commands)],
                expected_state=state, idempotency_key=key)
        return {"old_head": result["data"].get("previous_revision_id"),
                "new_head": result["data"]["accepted_revision_id"], "receipt": result["receipt"],
                "_restructure_key": key}

    def _reset_base(self, *, client: Any = None) -> None:
        """Re-read the new head as this working copy's bundle and save it with no undo step. Never fails the publish."""
        try:
            head = _store.fetch_bundle(str(self.bundle["project_id"]), str(self.bundle["timeline_id"]), client=client)
        except Exception as exc:  # noqa: BLE001 - the publish stands; the working copy keeps its old base
            self._retire_draft_history()
            self.notes.append(f"published, but this working copy could not re-read the new head ({exc}); "
                              "its base is still the old one: timelines checkout TL --fresh")
            return
        from astrid.sdk.timeline_restructure import flatten

        head = flatten(head)
        old, old_journal = self.bundle, list(self._journal)
        self.bundle, self._mcache, self._journal, self._disk = head, None, [], None
        try:
            target = Path(self.path)
            with _store._file_lock(target):
                self._save_locked(target, force=True, record=False)
        except Exception as exc:  # noqa: BLE001
            self.bundle, self._journal = old, old_journal
            self._retire_draft_history()
            self.notes.append(f"published, but this working copy could not be reset to the new head ({exc}); "
                              "its base is still the old one")
            return
        self._retire_draft_history()

    def _publish_narration(self, key: str, *, client: Any = None) -> dict[str, Any]:
        def run(c: Any) -> dict[str, Any]:
            from astrid.sdk.timeline.checkout import Checkout
            from astrid.sdk.timeline.sync import TimelineSession

            project_id = str(self.bundle["project_id"])
            timeline_id = str(self.bundle["timeline_id"])
            session = TimelineSession.open(project_id, timeline_id, client=c)
            with session.outbox.locked():
                state = session._state()
            flight = state.get("flight")
            commit_key = f"{key}-narration"[:120]
            pinned: list[str] = []
            if flight is not None:
                try:
                    flight_body = json.loads(flight.get("body", "{}"))
                except (AttributeError, TypeError, ValueError):
                    flight_body = {}
                flight_commands = flight_body.get("commands")
                if (flight_body.get("label") != "Bind narration"
                        or not isinstance(flight_commands, list)
                        or not flight_commands
                        or not all(isinstance(command, Mapping)
                                   and command.get("op") == "text-binding.set"
                                   for command in flight_commands)):
                    raise TimelineEditError(
                        "another timeline request is awaiting reconciliation; retry that request before narration"
                    )
                # The durable flight owns the original key and body. A later
                # publish invocation may generate a new outer key, but must
                # still retry this exact narration request.
                occurrence_shots = {
                    str(row["occurrence_id"]): str(row["shot_id"])
                    for row in self.bundle.get("placements") or []
                    if isinstance(row, Mapping)
                    and isinstance(row.get("occurrence_id"), str)
                    and isinstance(row.get("shot_id"), str)
                }
                pinned = [
                    occurrence_shots.get(str(command["select"]).removeprefix("occurrence:"),
                                         str(command["select"]).removeprefix("occurrence:"))
                    for command in flight_commands
                    if isinstance(command.get("select"), str)
                ]
                result = session.flush(idempotency_key=flight["key"])
            else:
                if state.get("pending"):
                    raise TimelineEditError(
                        "another timeline request is pending; flush it before narration binding"
                    )
                bundle = session._bundle(state)
                changes = Checkout(bundle).narration_changes()
                if not changes:
                    return {"narration_pinned": []}
                pinned = list(changes)

                # Ingest the exact UTF-8 source bytes as ordinary project
                # media; the film-affecting association is made only by the
                # commit below.
                for shot_id, text in changes.items():
                    descriptor, raw = _narration_descriptor(text)
                    imported = c.media.import_bytes(
                        project=project_id, data=raw,
                        filename=f"narration-{shot_id}.txt",
                        media_type=_NARRATION_MEDIA_TYPE,
                        idempotency_key=f"{key}-narration-media-{shot_id}"[:120],
                    )
                    if getattr(imported, "ok", True) is False:
                        raise TimelineEditError(
                            f"could not retain narration source bytes for shot {shot_id}: "
                            f"{getattr(imported, 'error', imported)}"
                        )
                    returned = getattr(imported, "data", imported)
                    if isinstance(returned, Mapping):
                        returned_id = returned.get("object_id") or returned.get("digest")
                        if returned_id is not None and returned_id != descriptor["object_id"]:
                            raise TimelineEditError(
                                f"narration source custody mismatch for shot {shot_id}: "
                                f"Runtime retained {returned_id!r}, expected {descriptor['object_id']!r}"
                            )
                commands, _ = _capture_narration_commands(bundle, changes)
                steps = [{
                    "id": f"step-{index}",
                    "label": f"Bind narration for {shot_id}",
                    "intent": "bind narration script text",
                    "tags": ["narration", "voiceover_script"],
                    "command_ordinals": [index],
                    "patch": [],
                } for index, shot_id in enumerate(changes)]
                result = session.submit(
                    commands, label="Bind narration", steps=steps, merge="fields", reads=[],
                    idempotency_key=commit_key, expected_state=state, base=state["base"],
                    evaluation={"kind": "label", "count": 0},
                )
            accepted = result["data"]["accepted_revision_id"]
            return {"narration_pinned": pinned, "new_head": accepted, "narration_head": accepted}

        if client is not None:
            return run(client)
        from astrid.sdk import AstridClient

        with AstridClient.open_from_launcher(start_pack_host=False) as c:
            return run(c)

    # ---- narration: the script text each shot is bound to ---------------------------
    def _chapter_shots(self) -> list[tuple[str, float, float]]:
        """``(shot id, start, end)`` of the stored chapter shots this flat film was read from (empty: not a chapter film)."""
        from astrid.sdk.timeline_restructure import STRUCTURE_KEY, _rows

        stored = self.bundle.get(STRUCTURE_KEY)
        return [(str(r["shot_id"]), _row_start(r) / 1000.0, (_row_start(r) + _num(r.get("duration_ms"))) / 1000.0)
                for r in (_rows(stored) if stored else [])]

    def _narration_shot(self, line: Voice) -> str:
        """The shot a line's script belongs to: its chapter's shot in a chapter film, else the one shot."""
        chapters = self._chapter_shots()
        if not chapters:
            return line.clips[0].shot_id
        start = line.clips[0].start
        return next((sid for sid, a, _b in reversed(chapters) if a <= start + 1e-6), chapters[0][0])

    def _shot_payload(self, sid: str) -> dict[str, Any]:
        from astrid.sdk.timeline_restructure import STRUCTURE_KEY

        shots = self.bundle["shots"] if sid in self.bundle["shots"] else self.bundle[STRUCTURE_KEY]["shots"]
        return shots[sid].setdefault("payload", {})

    def narration(self) -> dict[str, str]:
        """``{shot_id: text}``: each shot's lines' script text, in order (what the shot's
        voiceover_script binding should hold). Only shots whose lines all declare their text."""
        by_shot: dict[str, list[Voice]] = {}
        for line in self.lines():
            by_shot.setdefault(self._narration_shot(line), []).append(line)
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
            payload = self._shot_payload(sid)
            bound = next((b for b in payload.get("text_bindings") or [] if b.get("kind") == "voiceover_script"), None)
            digest = "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()
            if not bound or bound.get("content_hash") != digest:
                changed[sid] = text
        return changed

    def _matching_binding(self, client: Any, sid: str, text: str, bound: Mapping[str, Any] | None) -> dict[str, Any] | None:
        """The runtime's voiceover_script binding for ``sid`` iff its content hash is exactly ``text``'s (and it is the
        document's own binding when the document carries one); None otherwise, so the conflict is reported."""
        import hashlib

        from astrid.sdk.pagination import page_pair

        listed = client.shots.list_text_bindings(self.bundle["project_id"], shot_id=sid, kind="voiceover_script")
        page = page_pair(getattr(listed, "data", None)) if getattr(listed, "ok", False) else None
        if page is None or page[1] is not None:
            return None
        digest = "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()
        for row in page[0]:
            if (isinstance(row, Mapping) and row.get("shot_id") == sid and row.get("kind") == "voiceover_script"
                    and row.get("content_hash") == digest
                    and (not bound or not bound.get("binding_id") or row.get("binding_id") == bound.get("binding_id"))
                    and row.get("slot") == (bound or {}).get("slot")):
                return dict(row)
        return None

    def pin_narration(self, client: Any, *, idempotency_key: str) -> list[str]:
        """Register the changed narration per shot and pin it in the document (publish does this)."""
        pinned = []
        for sid, text in self.narration_changes().items():
            payload = self._shot_payload(sid)
            bound = next((b for b in payload.get("text_bindings") or [] if b.get("kind") == "voiceover_script"), None)
            expected = int((bound or {}).get("head") or 0)
            result = client.shots.set_text_binding(self.bundle["project_id"], shot_id=sid, kind="voiceover_script", text=text,
                                                    expected_head=expected,
                                                    idempotency_key=f"{idempotency_key}-narration-{sid}"[:120])
            err = getattr(result, "error", None)
            if getattr(result, "ok", True) is False and getattr(err, "code", None) == "conflict":
                # Adopt (never overwrite) a binding the document does not carry when the runtime already holds
                # exactly this text for this shot (an earlier pin whose document write was lost).
                adopted = self._matching_binding(client, sid, text, bound)
                if adopted is not None:
                    result = adopted
            pin = getattr(result, "data", result)
            if getattr(result, "ok", True) is False or not isinstance(pin, Mapping):
                raise TimelineEditError(f"could not register the narration for shot {sid}: {getattr(result, 'error', result)}")
            payload["text_bindings"] = [b for b in payload.get("text_bindings") or [] if b.get("kind") != "voiceover_script"] + [dict(pin)]
            (payload.get("metadata") or {}).pop("voiceover_script", None)
            pinned.append(sid)
        return pinned

    def document(self) -> dict[str, Any]:
        """The publishable candidate (derived `_` fields removed)."""
        doc = _strip_derived(copy.deepcopy(self.bundle))
        intent.migrate(doc)  # the candidate carries intent under app.astrid; unchanged shots reuse their old revisions and the merged candidate is published without a further migration (old revisions are never rewritten)
        return doc

    def stored_document(self, *, restructure: bool = False) -> dict[str, Any]:
        """The candidate as it is stored.

        Splitting into chapter shots is opt-in per film: only `timelines restructure`
        (``restructure=True``) or a film already stored as chapters (read back flat, so it
        carries its stored form) is split. Every other film is stored exactly as edited.
        """
        from astrid.sdk.timeline_restructure import STRUCTURE_KEY, split

        doc = self.document()
        return split(doc) if restructure or STRUCTURE_KEY in doc else doc

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
                if not isinstance(stamp, Mapping):
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
