"""Guarded timeline commands through the public Runtime client bridge.

The Runtime owns acceptance/history. This module retains only pending intent and
one exact request until its acknowledgement is durably applied.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
import shlex
import uuid
from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from astrid.core.receipts.canonical import canonical_json
from astrid.core.receipts.contract import CommandReceipt
from astrid.sdk.timeline._common import TimelineEditError
from astrid.sdk.timeline.outbox import Outbox, request_bytes
from astrid.sdk.workspace_client import WorkspaceClientError


@contextmanager
def _retry_outbox_locks(outbox, target_path):
    """Acquire distinct outboxes in canonical path order for reciprocal retries."""
    boxes = {outbox.path.resolve(): outbox}
    if target_path:
        target = Outbox(Path(target_path))
        boxes.setdefault(target.path.resolve(), target)
    with ExitStack() as stack:
        for path in sorted(boxes):
            stack.enter_context(boxes[path].locked())
        yield

class _CommitFailure(TimelineEditError):
    def __init__(self, failure, *, attempts=None, label=None, fixed_list=False,
                 timeline_id=None, project_id=None, pending_id=None, clash=None, retry_clash=False):
        self.failure = copy.deepcopy(failure)
        self.code = failure.get("code")
        self.details = copy.deepcopy(failure.get("details") or {})
        self.pending_id = pending_id
        self.clash = copy.deepcopy(clash)
        from ._commit_wording import present_failure

        self.presentation = present_failure(self.failure)
        if attempts is not None:
            self.presentation.update(
                headline=f'The timeline kept changing; "{label or "Timeline edit"}" was not committed',
                detail=f'Your function ran {attempts} times, re-reading the timeline each time, and each attempt met a newer change.',
                fix=(f'Run astrid timelines discard {timeline_id} --project {project_id}, '
                     'then call tl.commit(fn, ...) again.'),
            )
        elif fixed_list:
            self.presentation.update(
                headline="The timeline changed since this list was made; nothing was committed",
                detail=(self.failure.get("message") or
                        "The list was not re-run. Review the latest timeline before applying new choices."),
                fix=(f'Run astrid timelines discard {timeline_id} --project {project_id}, '
                     'preview again with dry_run=True, then commit the reviewed commands.'),
            )
        if pending_id is not None and self.failure.get("details", {}).get("status") != "conflict":
            self.presentation["fix"] = (f'Inspect the saved edit with tl.pending_show("{pending_id}"), '
                                        'fix the edit, then retry or drop it.')
            self.presentation.update(pending_id=pending_id, clash=copy.deepcopy(clash))
        elif pending_id is not None:
            retry_call = (f'tl.retry("{pending_id}", your_function)'
                          if isinstance(clash, dict) and clash.get("origin") == "function"
                          else f'tl.retry("{pending_id}")')
            self.presentation["fix"] = (
                f'Someone else changed this timeline after your edit was made, so "{label or "Timeline edit"}" '
                f'was not committed. Your edit is saved as {pending_id}; nothing is lost. '
                f'Retry it with {retry_call}.')
            self.presentation.update(pending_id=pending_id, clash=copy.deepcopy(clash))
            if retry_clash:
                self.presentation["headline"] = (
                    f'The timeline changed again before "{label or "Timeline edit"}" landed; '
                    f'nothing was committed. {pending_id} is still saved')
                self.presentation["fix"] = f'tl.retry("{pending_id}")'
        self.headline = self.presentation["headline"]
        self.detail = self.presentation["detail"]
        self.fix = self.presentation["fix"]
        self.outcome = self.presentation["outcome"]
        self.cause = self.presentation["cause"]
        if pending_id is not None and not retry_clash:
            message = (f'{self.headline.rstrip(".")}. {self.detail.rstrip(".")}. '
                       f'{self.fix}')
        else:
            message = f"{self.headline.rstrip('.')}. {self.fix}"
        super().__init__(message)


class CommitConflict(_CommitFailure):
    """A Runtime conflict that may be resolved by retrying the edit."""


class CommitRefused(_CommitFailure):
    """A Runtime refusal that requires inspecting and correcting the edit."""


class _OutboxChanged(TimelineEditError):
    """A different writer changed the outbox before this intent was queued."""


class CommitOutcomeUnknown(TimelineEditError, WorkspaceClientError):
    """A sent request whose outcome cannot yet be verified."""

    def __init__(self, cause, *, key=None):
        from ._commit_wording import present_failure

        failure = {"code": getattr(cause, "code", type(cause).__name__),
                   "message": str(cause), "details": copy.deepcopy(getattr(cause, "details", {}))}
        self.failure = failure
        self.presentation = present_failure(failure, unknown=True)
        self.headline = self.presentation["headline"]
        self.detail = self.presentation["detail"]
        self.fix = self.presentation["fix"]
        self.outcome = "unknown"
        self.cause = self.presentation["cause"]
        if key:
            self.fix = f'Call tl.commit(key="{key}") again with no other arguments to confirm the saved request.'
            self.presentation["fix"] = self.fix
        message = f"{self.headline}. {self.fix}"
        WorkspaceClientError.__init__(self, 0, "timeline_outcome_unknown", message,
                                      {**self.presentation, "cause": failure})


def _flight_kind(flight):
    """Return the processing kind without defaulting a present discriminator."""
    if flight is None:
        return None
    if not isinstance(flight, dict):
        raise TimelineEditError("invalid in-flight request kind; original intent retained")
    if "kind" not in flight:
        return "commit"
    if flight["kind"] == "assembly-admission":
        return "assembly-admission"
    raise TimelineEditError("invalid in-flight request kind; original intent retained")


def _display_key(flight):
    """The caller-visible key of a flight; generated keys are never shown as the caller's key."""
    if flight.get("logical_key") is not None:
        return flight["logical_key"]
    return flight["key"] if flight.get("explicit_key") else None


def _connection(client):
    transport = client._remote._transport
    handshake = transport._last_handshake or {}
    identity = {"realm_id": handshake.get("realm_id"), "actor_id": handshake.get("actor_id")}
    if not all(isinstance(value, str) and value for value in identity.values()):
        raise TimelineEditError("timeline commands require an authenticated Runtime realm and actor")
    return identity


def _response(result):
    if not result.ok:
        error = result.error
        raise WorkspaceClientError(0, error.code, error.message, dict(error.details))
    return result.as_dict()


def _pin(value):
    if not isinstance(value, dict) or not all(isinstance(value.get(key), str) and value[key]
                                             for key in ("revision_id", "publication_id")):
        raise TimelineEditError("timeline has no verified publication; editing is unavailable")
    return {key: value[key] for key in ("revision_id", "publication_id")}


def _native_command(command):
    from astrid.sdk.timeline.commands import operations, validate_command

    command = copy.deepcopy(command)
    validate_command(command)
    if command["op"] not in operations():
        raise TimelineEditError("queue/commit requires a registered native operation")
    return command


def _ack(result, body, flight, identity):
    """Check acceptance identity, leaving full closure validation to Runtime."""
    from astrid.sdk.timeline.commands import validate_command

    def require(condition):
        if not condition:
            raise ValueError("invalid acceptance identity")

    try:
        data, receipt = result["data"], result["receipt"]
        require(isinstance(data, dict))
        for key in ("base_revision_id", "base_publication_id", "provider_version", "compiler_version"):
            require(isinstance(data[key], str) and data[key])
        require(all(data[key] == body[key] for key in ("base_revision_id", "base_publication_id")))
        require(isinstance(data["commands"], list) and data["commands"])
        for command in data["commands"]:
            validate_command(command)
        require(isinstance(data["id_map"], dict))
        if any(not isinstance(key, str) or not key or key != value for key, value in data["id_map"].items()):
            raise TimelineEditError("Runtime returned a nonidentity id_map; request remains in flight")
        document = data["canonical_document"]
        require(isinstance(document, dict) and type(document["schema_version"]) is int and document["schema_version"] == 1)
        require(document["project_id"] == body["project_id"] and document["timeline_id"] == body["timeline_id"])
        parent = document["parent_revision"]
        require(isinstance(parent, dict) and isinstance(parent["revision_id"], str) and parent["revision_id"])
        require(isinstance(parent["payload"], dict))
        for digest in (data["document_digest"], parent["content_digest"]):
            require(isinstance(digest, str) and re.fullmatch(r"sha256:[0-9a-f]{64}", digest))
        for key in ("shot_revisions", "internal_timeline_revisions"):
            require(isinstance(document[key], list) and all(isinstance(row, dict) for row in document[key]))
        manifest = document["dependency_manifest"]
        require(isinstance(manifest, dict))
        for key in ("shots", "internal_timelines", "media", "documents"):
            require(isinstance(manifest[key], list) and all(isinstance(row, dict) for row in manifest[key]))
        if body.get("dry_run", False):
            require(data["status"] == "preview" and receipt is None)
            _pin(data["evaluated_head"])
            require(not any(key in data for key in ("accepted_revision_id", "publisher", "accepted_publication_id")))
        else:
            require(data["status"] in {"accepted", "merged", "rebased"})
            CommandReceipt.from_dict(receipt)
            require(receipt["command_kind"] == "timeline.commit")
            require(receipt["project_id"] == body["project_id"] and receipt["idempotency_key"] == flight["key"])
            require(receipt["result"] == data and data["publisher"] == identity["actor_id"])
            invocation = {"operation": "commitTimeline", "path": flight["path"],
                          "publisher": identity["actor_id"], "request": body}
            require(receipt["request_hash"] == hashlib.sha256(canonical_json(invocation).encode()).hexdigest())
            require(data["accepted_revision_id"] == parent["revision_id"])
            previous = _pin({"revision_id": data["previous_revision_id"], "publication_id": data["previous_publication_id"]})
            base = {"revision_id": body["base_revision_id"], "publication_id": body["base_publication_id"]}
            require(receipt["receipt_id"] not in {previous["publication_id"], base["publication_id"]})
            if data["status"] == "accepted":
                require(data["rebased_onto"] is None and previous == base)
            else:
                require(_pin(data["rebased_onto"]) == previous and previous != base)
    except TimelineEditError:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise TimelineEditError("invalid Runtime acknowledgement; request remains in flight") from exc
    return data, receipt


def _pending_diff(session, record):
    """Build the saved part's authored-field diff after its record is durable."""
    from ._command_state import equal
    from .emitter import _diff, _payload_for_emit, _root_pairs
    from .store import fetch_bundle

    part = record["parts"][record.get("part", 0)]
    base = part["base"]
    before = fetch_bundle(session.project_id, session.timeline_id,
                          revision_id=base["revision_id"], publication_id=base["publication_id"],
                          client=session.client)
    after = session._capture_bundle({"base": base, "pending": part["commands"]})
    changes = []
    for ref, before_value, after_value in _root_pairs(before, after):
        if not equal(before_value, after_value):
            _diff(_payload_for_emit(ref, before_value), _payload_for_emit(ref, after_value),
                  ref, (), changes)
    return changes


def _pending_clash(session, record):
    """Best-effort history enrichment for a durable pending conflict."""
    try:
        from .clash import explain

        packet = explain(session, record)
    except Exception:  # noqa: BLE001 - enrichment must not disturb the saved edit
        return copy.deepcopy(record)
    with session.outbox.locked():
        state = session._state()
        saved = state.get("pending_changes", {}).get(record["id"])
        if saved is None or saved.get("state") != "pending":
            return copy.deepcopy(saved or record)
        if saved.get("clash") != packet:
            saved["clash"] = copy.deepcopy(packet)
            saved["rev"] = int(saved.get("rev", 0)) + 1
            saved["updated_at"] = datetime.now(timezone.utc).isoformat()
            session.outbox.write(state)
        return copy.deepcopy(saved)


def _settle_pending(record, *, expected_part, actor_id, publication_id, how, base, receipt_id=None):
    """Apply an acknowledged part while retaining any unsent suffix."""
    if record is None or record.get("state") != "pending":
        return False
    index = record.get("part", 0)
    receipt_id = receipt_id or publication_id
    if expected_part is None or index != expected_part or receipt_id in record.get("receipts", []):
        return False
    record.setdefault("receipts", []).append(receipt_id)
    if index + 1 < len(record.get("parts", [])):
        record["part"] = index + 1
        record["parts"][index + 1]["base"] = copy.deepcopy(base)
    else:
        record["state"] = "landed"
        record["closed_by"] = {"actor_id": actor_id, "publication_id": publication_id, "how": how}
    record["rev"] = int(record.get("rev", 0)) + 1
    record["updated_at"] = datetime.now(timezone.utc).isoformat()
    return True


def _retry_conflict(record, replacement, *, expected_part=None):
    if (record is None or record.get("state") != "pending"
            or (expected_part is not None and record.get("part", 0) != expected_part)):
        raise TimelineEditError("saved edit is unavailable or has advanced; original flight retained")
    index = record.get("part", 0)
    record.setdefault("versions", []).append({key: copy.deepcopy(record.get(key))
                                               for key in ("failure", "head", "diff", "attempts", "flight", "parts", "part", "label", "origin")})
    record["parts"][index] = copy.deepcopy(replacement["parts"][0])
    record["parts"].extend(copy.deepcopy(replacement["parts"][1:]))
    record["label"] = replacement["label"]
    record["origin"] = replacement["origin"]
    record["attempts"] = int(record.get("attempts", 0)) + 1
    record["failure"] = replacement["failure"]
    record["flight"] = copy.deepcopy(replacement.get("flight"))
    record["head"] = replacement["head"]
    record["diff"] = None
    record.pop("clash", None)
    record["rev"] = int(record.get("rev", 0)) + 1
    record["updated_at"] = datetime.now(timezone.utc).isoformat()


class TimelineSession:
    def __init__(self, client, outbox, identity, project_id, timeline_id):
        self.client, self.outbox, self.identity = client, outbox, identity
        self.project_id, self.timeline_id = project_id, timeline_id
        self.path = f"/v1/projects/{quote(project_id, safe='')}/timelines/{quote(timeline_id, safe='')}/commit"

    @classmethod
    def open(cls, project, timeline, *, client, outbox_path=None,
             base_revision_id=None, base_publication_id=None):
        from astrid.sdk.storage_root import resolve_runtime_data_root
        from astrid.sdk.timeline.store import resolve_ids

        base = None if base_revision_id is None and base_publication_id is None else _pin({
            "revision_id": base_revision_id, "publication_id": base_publication_id})
        identity = _connection(client)
        project_id, timeline_id, _ = resolve_ids(project, timeline, client=client)
        scope = {**identity, "project_id": project_id, "timeline_id": timeline_id}
        explicit_outbox = outbox_path is not None
        if outbox_path is None:
            name = hashlib.sha256(request_bytes(scope)).hexdigest() + ".json"
            outbox_path = resolve_runtime_data_root() / "timeline-outbox" / name
        session = cls(client, Outbox(outbox_path), identity, project_id, timeline_id)
        session._explicit_outbox = explicit_outbox
        while True:
            with session.outbox.locked():
                state = session.outbox.read()
                if state is not None:
                    if state["scope"] != scope:
                        raise TimelineEditError("outbox belongs to a different realm, actor, project or timeline")
                    from ._pending import migrate
                    if migrate(state):
                        session.outbox.write(state)
                    if state["pending"] or state["flight"] is not None:
                        session._state()
                        if base is not None and state["base"] != base:
                            raise TimelineEditError("pending intent belongs to a different original base")
                        return session
            head = base or _pin(_response(client.timelines.history(project_id, timeline_id, limit=1))["data"]["head"])
            with session.outbox.locked():
                if _connection(client) != identity:
                    raise TimelineEditError("outbox identity changed while reading its head")
                if session.outbox.read() != state:
                    continue
                state = state or {"scope": scope, "pending": [], "flight": None}
                state["base"] = head
                session.outbox.write(state)
                break
        return session

    def _state(self):
        identity = _connection(self.client)
        state = self.outbox.read()
        _flight_kind(state.get("flight"))
        if identity != self.identity or state["scope"] != {
            **identity, "project_id": self.project_id, "timeline_id": self.timeline_id,
        }:
            raise TimelineEditError("outbox identity changed; reconnect to its original realm and actor")
        return state

    def _bundle(self, state, extra_commands=()):
        """Project an authenticated capture without exposing its canonical cache."""
        bundle = self._capture_bundle(state, extra_commands)
        bundle.get("source_mapping", {}).pop("_runtime_document", None)
        return bundle

    def _capture_bundle(self, state, extra_commands=()):
        """Keep the complete exact-pair input private to capture and execution."""
        from astrid.sdk.timeline.commands import execute
        from astrid.sdk.timeline.native_source_inputs import materialize_transcript_inputs
        from astrid.sdk.timeline.store import fetch_bundle

        commands = [*state["pending"], *extra_commands]
        # Hydrate every captured closure from the authenticated revision /
        # publication pair.  Detached document preflight has no pending
        # command, so conditioning this pin on doc.patch would silently turn
        # that read into revision-only hydration.
        source = {"publication_id": state["base"]["publication_id"]}
        bundle = fetch_bundle(self.project_id, self.timeline_id,
                              revision_id=state["base"]["revision_id"], client=self.client, **source)
        materialize_transcript_inputs(
            bundle, commands,
            client=self.client.timelines._client, project=self.project_id)
        return execute(bundle, state["pending"]).bundle if state["pending"] else bundle

    def queue_move(self, select, on):
        """Capture once against the original state plus earlier local commands."""
        from astrid.sdk.timeline.capture import capture_move

        return self._queue(lambda state: capture_move(self._capture_bundle(state), select, on))

    def admit_assembly(self, child_outputs, *, replace_clips, assembler_digest, idempotency_key,
                       rationale="Assemble ordered child visuals"):
        """Freeze the original closure before registered assembler work."""
        from astrid.core.contracts.runtime_orchestration import build_assembler_admission
        from astrid.sdk.timeline.assembly import admission_body, capture_assembly, retry_assembly

        child_outputs, replace_clips = copy.deepcopy(list(child_outputs)), list(replace_clips)
        with self.outbox.locked():
            state = self._state()
            flight = state["flight"]
            if _flight_kind(flight) == "assembly-admission":
                retry_assembly(self, state, flight, child_outputs, replace_clips,
                               assembler_digest, idempotency_key, rationale)
            elif state["pending"] or flight is not None:
                raise TimelineEditError("flush pending intent before admitting assembly")
        if flight is None:
            capture = capture_assembly(self, state, child_outputs,
                                       replace_clips=replace_clips, rationale=rationale)
            admission = build_assembler_admission(capture=capture, assembler_digest=assembler_digest,
                                                  idempotency_key=idempotency_key)
            body = admission.body
            prepared = _response(self.client.tasks.prepare_admission(
                project_id=body["project"], capability=body["capability_id"],
                capability_digest=body["capability_digest"], spec=body["spec"],
                input_manifest=body["input_object_ids"], storage_estimate=body["storage_estimate"],
                idempotency_key=admission.idempotency_key))["data"]
            flight = {"kind": "assembly-admission",
                      "body": request_bytes(admission_body(prepared)).decode(),
                      "key": prepared["idempotency_key"], "fence": uuid.uuid4().hex}
            with self.outbox.locked():
                if self._state() != state:
                    raise TimelineEditError("outbox changed during assembly capture; nothing was admitted")
                state["flight"] = flight
                self.outbox.write(state)
        body = json.loads(flight["body"])
        arguments = {key: value for key, value in body.items() if key != "project"}
        arguments.update(project_id=body["project"], idempotency_key=flight["key"])
        result = _response(self.client.tasks.dispatch_admission(arguments))
        with self.outbox.locked():
            state = self._state()
            if state["flight"] == flight:
                state["flight"] = None
                self.outbox.write(state)
        return result

    def queue_assembly(self, task_id, *, attempt_id):
        """Read one exact settled native proposal, then queue it normally."""
        from astrid.sdk.timeline.assembly import read_assembly_proposal

        with self.outbox.locked():
            self._state()
        proposal = read_assembly_proposal(self, task_id, attempt_id)
        return self.queue_proposal(proposal)

    def queue_proposal(self, proposal):
        """Persist the settled batch unchanged at its original admitted base."""
        from astrid.sdk.timeline.producer import build_producer_batch

        batch = build_producer_batch(proposal)
        if (batch['project_id'], batch['timeline_id']) != (self.project_id, self.timeline_id):
            raise TimelineEditError('proposal belongs to a different project or timeline')
        with self.outbox.locked():
            state = self._state()
            if state['pending'] or state['flight'] is not None:
                raise TimelineEditError('flush pending intent before queueing a proposal')
            state['base'] = {'revision_id': batch['base_revision_id'],
                             'publication_id': batch['base_publication_id']}
            state['pending'], state['rationale'] = batch['commands'], batch['rationale']
            self.outbox.write(state)
        return copy.deepcopy(batch['commands'])

    def queue(self, command):
        """Capture native intent over the original base plus pending commands.

        Supplied capture claims are verified by execute, never refreshed.
        """
        return self.queue_many([command])[0]

    def queue_many(self, commands):
        """Atomically capture a batch on the original base, staging its own prefix.

        Commands are registered EditCommands; supplied claims are never refreshed.
        Missing capture inputs fail before any command is added to the outbox.
        """
        from astrid.sdk.timeline.commands import execute

        commands = [_native_command(command) for command in commands]
        return self._stage(lambda state: execute(self._capture_bundle(state, commands), commands).commands)

    def commit(self, command, *, idempotency_key=None, dry_run=False, via="python", verified_media=(),
               label=None, steps=None, merge="fields", reads=None):
        """Capture and flush one native command, or retry its original flight."""
        return self.commit_many([command], idempotency_key=idempotency_key, dry_run=dry_run,
                                via=via, verified_media=verified_media, label=label, steps=steps,
                                merge=merge, reads=reads)

    def commit_many(
        self, commands, *, idempotency_key=None, dry_run=False, via="python", base=None,
        verified_media=(), label=None, steps=None, merge="fields", reads=None,
        _omit_empty_reads=False,
    ):
        """Capture and flush a batch, or retry its exact flight before a suffix."""
        from astrid.sdk.timeline._command_state import equal
        from astrid.sdk.timeline.commands import execute

        commands = [_native_command(command) for command in commands]
        metadata_requested = label is not None or steps is not None or merge != "fields" or reads is not None
        if reads is None and merge == "fields" and not _omit_empty_reads:
            reads = []
        label = label if label is not None else "Timeline edit"
        if not isinstance(label, str) or not label.strip():
            raise TimelineEditError("commit label must be a nonempty string")
        if merge not in {"fields", "atomic"}:
            raise TimelineEditError("merge must be fields or atomic")
        if steps is None:
            steps = []
            for ordinal, command in enumerate(commands):
                patch = (copy.deepcopy(command.get("args", {}).get("changes", []))
                         if command.get("op") == "doc.patch" else [])
                steps.append({"id": f"step-{ordinal + 1}", "label": label,
                              "intent": "", "tags": [], "command_ordinals": [ordinal],
                              "patch": patch})
        else:
            steps = copy.deepcopy(steps)
        options = {"label": label, "steps": steps, "merge": merge}
        if reads is not None:
            options["reads"] = copy.deepcopy(reads)
        retried = self.retry(
            idempotency_key,
            matches=lambda body: len(body['commands']) == len(commands) and all(
                all(equal(saved.get(key), value) for key, value in command.items())
                for saved, command in zip(body['commands'], commands)) and (
                    not metadata_requested or all(equal(body.get(key), value)
                                                  for key, value in options.items())),
            dry_run=dry_run, via=via,
        )
        if retried is not None:
            return retried
        runtime_capture = any(command['op'] == 'occurrence.place-shared'
                              and 'source_ref' in command['args'] for command in commands)
        # Foreign child authority is available only inside Runtime's request port.
        # Freeze the submitted intent; its ordinary provider captures every guard.
        self._stage(lambda state: copy.deepcopy(commands) if runtime_capture else execute(
                        self._capture_bundle(state, commands), commands, verified_media=verified_media
                    ).commands,
                    lambda pending: len(pending) == len(commands) and all(
                        all(equal(saved.get(key), value) for key, value in command.items() if key != "intent")
                        and all(equal(saved.get("intent", {}).get(key), value)
                                for key, value in command.get("intent", {}).items())
                        for saved, command in zip(pending, commands)), base=base)
        with self.outbox.locked():
            state = self._state()
            existing = state.get("commit_options")
            if metadata_requested and existing is not None and existing != options:
                raise TimelineEditError("pending commit metadata differs; retry the original request")
            if state.get("flight") is None and (metadata_requested or existing is None):
                state["commit_options"] = copy.deepcopy(options)
                self.outbox.write(state)
        return self.flush(idempotency_key=idempotency_key, dry_run=dry_run, via=via)

    def _queue(self, capture, matches=None):
        return self._stage(lambda state: [capture(state)], matches, retry_capture=True)[0]

    def _stage(self, capture, matches=None, *, retry_capture=False, base=None):
        from astrid.sdk.timeline._command_state import equal

        while True:
            with self.outbox.locked():
                state = self._state()
                if base is not None and state["base"] != _pin(base):
                    raise TimelineEditError("outbox no longer has the original sheet basis")
                if any(call['op'] == 'producer.guard' for call in state['pending']):
                    raise TimelineEditError('flush the producer proposal before another edit')
                if matches is not None and state["pending"]:
                    flight = state["flight"]
                    prefix = (state["pending"][:flight["count"]]
                              if flight and _flight_kind(flight) != "assembly-admission"
                              else state["pending"])
                    if not matches(prefix):
                        raise TimelineEditError("pending intent differs; flush the original request before another edit")
                    return copy.deepcopy(prefix)
            commands = capture(state)
            with self.outbox.locked():
                if not equal(self._state(), state):
                    if retry_capture:  # Preserve the existing move callback concurrency contract.
                        continue
                    raise TimelineEditError("outbox changed during capture; original intent was not queued")
                state["pending"].extend(copy.deepcopy(commands))
                self.outbox.write(state)
                return copy.deepcopy(commands)

    def move(self, select, on, *, idempotency_key=None, dry_run=False, via="python"):
        # A repeated one-shot CLI/SDK invocation retries original intent, never
        # recaptures guards from a possibly changed head after a lost ACK.
        from astrid.sdk.timeline.capture import capture_move

        retried = self.retry(idempotency_key, matches=lambda body: len(body['commands']) == 1
                             and body['commands'][0]['op'] == 'clip.move'
                             and body['commands'][0]['select'] == select
                             and body['commands'][0]['args'] == {'on': on},
                             dry_run=dry_run, via=via)
        if retried is not None:
            return retried
        self._queue(lambda state: capture_move(self._capture_bundle(state), select, on),
                    lambda pending: len(pending) == 1 and pending[0]["op"] == "clip.move"
                    and pending[0]["select"] == select and pending[0]["args"] == {"on": on})
        return self.flush(idempotency_key=idempotency_key, dry_run=dry_run, via=via)

    def trim(self, select, *, in_s=None, out_s=None, idempotency_key=None, dry_run=False, via="python"):
        """Capture a literal source trim once, retrying its original flight."""
        from astrid.sdk.timeline._trim_commands import capture_clip_trim

        retried = self.retry(idempotency_key, matches=lambda body: len(body['commands']) == 1
                             and body['commands'][0]['op'] == 'clip.trim'
                             and body['commands'][0]['select'] == select
                             and body['commands'][0]['args'] == {'in_s': in_s, 'out_s': out_s},
                             dry_run=dry_run, via=via)
        if retried is not None:
            return retried
        self._queue(lambda state: capture_clip_trim(self._capture_bundle(state), select, in_s=in_s, out_s=out_s),
                    lambda pending: len(pending) == 1 and pending[0]["op"] == "clip.trim"
                    and pending[0]["select"] == select
                    and pending[0]["args"] == {"in_s": in_s, "out_s": out_s})
        return self.flush(idempotency_key=idempotency_key, dry_run=dry_run, via=via)

    def add_media(self, source, *, on, duration, track=None, clip_id=None, values=None,
                  idempotency_key=None, dry_run=False, via='python'):
        """Add verified media, choosing an audio or visual lane when omitted."""
        from .media_add import add_media

        return add_media(self, source, on=on, duration=duration, track=track, clip_id=clip_id,
                         values=values, idempotency_key=idempotency_key, dry_run=dry_run, via=via)

    def retry(self, key, *, matches, dry_run=False, via='python'):
        """Resend a previously acknowledged explicit key, without advancing the local base.

        Retain only original request bytes; Runtime remains the receipt authority.
        This also permits a retry after later edits have advanced the timeline.
        """
        if key is None:
            return None
        if not isinstance(key, str) or not key.strip():
            raise TimelineEditError('idempotency key must be nonempty')
        with self.outbox.locked():
            state = self._state()
            flight = copy.deepcopy(state.get('acknowledged', {}).get(key))
        if flight is None:
            return None
        body = json.loads(flight['body'])
        logical_key = flight.get('logical_key', flight['key'])
        if (flight['path'] != self.path or logical_key != key
                or body['project_id'] != self.project_id or body['timeline_id'] != self.timeline_id
                or body.get('dry_run', False) != dry_run or body['source'] != {'via': via}
                or not matches(body)):
            raise TimelineEditError('idempotency key belongs to another edit; use a fresh key for new intent')
        result = _response(self.client.timelines.commit(
            self.project_id, self.timeline_id, body, idempotency_key=flight['key']))
        data, receipt = _ack(result, body, flight, self.identity)
        return self._present(result, data, receipt, imports=flight.get('imports', ()), replayed=True,
                             label=body.get("label"))

    def resolve_acknowledged(self, key, *, dry_run=False, via='python'):
        """Return a keyed function's acknowledged receipt without evaluating it.

        The logical key is bound to the final accepted attempt, even when the
        function was reevaluated after conflicts. This is a read-only replay:
        it does not move the local base to the historical receipt.
        """
        if key is None:
            return None
        if not isinstance(key, str) or not key.strip():
            raise TimelineEditError('idempotency key must be nonempty')
        with self.outbox.locked():
            state = self._state()
            flight = copy.deepcopy(state.get('acknowledged', {}).get(key))
        if flight is None:
            return None
        evaluation = flight.get('evaluation')
        if not isinstance(evaluation, dict) or evaluation.get('kind') != 'function':
            raise TimelineEditError('idempotency key belongs to another edit; use a fresh key for new intent')
        return self.retry(key, matches=lambda body: True, dry_run=dry_run, via=via)

    def undo(self, publications, *, steps=None, idempotency_key=None, dry_run=False, via="python"):
        publications = list(publications)
        if not publications or len(set(publications)) != len(publications) or not all(isinstance(p, str) and p for p in publications):
            raise TimelineEditError("undo requires a nonempty unique list of publication IDs, oldest first")
        if steps is not None:
            valid_steps = (
                isinstance(steps, dict)
                and set(steps) == set(publications)
                and all(
                    isinstance(step_ids, list)
                    and step_ids
                    and all(isinstance(step_id, str) and step_id for step_id in step_ids)
                    and len(set(step_ids)) == len(step_ids)
                    for step_ids in steps.values()
                )
            )
            if not valid_steps:
                raise TimelineEditError(
                    "undo steps must map every publication to a nonempty unique list of step IDs"
                )
        command = {"schema_version": 1, "op": "timeline.inverse", "version": "1",
                   "select": None, "args": {"publications": publications}}
        if steps is not None:
            command["args"]["steps"] = copy.deepcopy(steps)

        def prepare(state):
            if _flight_kind(state["flight"]) == "assembly-admission":
                raise TimelineEditError("retry the original assembly admission before undo")
            if state["pending"] and state["pending"] != [command]:
                raise TimelineEditError("flush pending intent before undo")
            state["pending"] = [command]
            # Freeze v2 metadata with new intent. An existing flight, including
            # one persisted by an older caller, must keep its original body.
            if state["flight"] is None and "commit_options" not in state:
                state["commit_options"] = {
                    "label": "Undo timeline edit", "merge": "fields",
                    "steps": [{"id": "inverse", "label": "Undo timeline edit", "intent": "",
                               "tags": [], "command_ordinals": [0], "patch": []}],
                }

        return self._flush(idempotency_key=idempotency_key, dry_run=dry_run, via=via, prepare=prepare)

    def restore(self, revision_id, content_digest, *, idempotency_key=None, dry_run=False, via="python"):
        """Restore one exact retained revision through the native commit path.

        The revision and digest are caller-supplied historical identity pins;
        neither is inferred from a legacy numeric version.  Staging and flush
        retain the normal outbox draft and exact-retry guarantees.
        """
        if not isinstance(revision_id, str) or not revision_id:
            raise TimelineEditError("restore requires an exact nonempty revision_id")
        if not isinstance(content_digest, str) or not content_digest:
            raise TimelineEditError("restore requires the exact nonempty content_digest")
        command = {
            "schema_version": 1,
            "op": "timeline.restore",
            "version": "1",
            "select": None,
            "args": {"revision_id": revision_id, "content_digest": content_digest},
        }
        with self.outbox.locked():
            state = self._state()
            if state["pending"] and state["pending"] != [command]:
                raise TimelineEditError("flush pending intent before restore")
            state["pending"] = [command]
            self.outbox.write(state)
        return self.flush(idempotency_key=idempotency_key, dry_run=dry_run, via=via)

    def flush(self, *, idempotency_key=None, dry_run=False, via="python", release_conflict=True):
        """Send/retry exactly one prefix; leave newer local commands untouched."""
        return self._flush(idempotency_key=idempotency_key, dry_run=dry_run, via=via,
                           release_conflict=release_conflict)

    def save_interrupted(self, commands, *, base, label, steps, merge, reads, key, evaluation,
                         failure=None, release_flight=None, release_dry_run=False,
                         pending_id=None, pending_path=None, pending_part=None):
        """Save captured intent without changing another writer's queue or flight."""
        from ._pending import clash, snapshot

        if key is not None and (not isinstance(key, str) or not key.strip()):
            raise TimelineEditError("idempotency key must be nonempty")
        body = {"api_version": 1, "project_id": self.project_id, "timeline_id": self.timeline_id,
                "base_revision_id": base["revision_id"], "base_publication_id": base["publication_id"],
                "commands": copy.deepcopy(commands), "source": {"via": "python"}, "dry_run": False,
                "label": label, "steps": copy.deepcopy(steps), "merge": merge}
        if reads is not None:
            body["reads"] = copy.deepcopy(reads)
        flight = {"body": request_bytes(body).decode(), "count": len(commands),
                  "key": key or "timeline-edit-" + uuid.uuid4().hex,
                  "explicit_key": key is not None, "evaluation": copy.deepcopy(evaluation)}
        flight["imports"] = [copy.deepcopy(item) for entry in evaluation.get("media_cache", {}).values()
                             if isinstance(entry, dict) for item in entry.get("imports", [])]
        if key is not None:
            flight["logical_key"] = key
        with _retry_outbox_locks(self.outbox, pending_path if pending_id else None):
            state = self._state()
            failure = copy.deepcopy(failure) if failure is not None else {
                "code": "local_outbox_changed", "message": "session base changed",
                "details": {"status": "conflict", "reason": "local_outbox_changed",
                            "current": copy.deepcopy(state["base"]), "conflicts": []}}
            captured = {"base": copy.deepcopy(base), "pending": copy.deepcopy(commands)}
            record = snapshot(captured, flight, failure)
            owned = (release_flight is not None and state.get("flight") == release_flight
                     and state.get("conflict", {}).get("details", {}).get("status") == "conflict"
                     and (state.get("evaluation") or {}).get("kind") == "function")
            if owned:
                self._flight_body(state, release_flight, None, release_dry_run)
                record["parts"].extend(snapshot(state, release_flight, failure)["parts"][1:])
            external = pending_id and pending_path and Path(pending_path).resolve() != self.outbox.path.resolve()
            target_outbox = Outbox(Path(pending_path)) if external else self.outbox
            target = target_outbox.read() if external else state
            records = target.setdefault("pending_changes", {})
            matches = [item for item in records.values()
                       if item.get("state") == "pending" and
                       (item.get("id") == pending_id if pending_id is not None else
                        key is not None and item.get("key") == key)]
            if len(matches) > 1:
                raise TimelineEditError(f"pending key {key!r} is ambiguous; use a pending id")
            if pending_id is not None and (not matches or (pending_part is not None
                                                          and matches[0].get("part", 0) != pending_part)):
                raise TimelineEditError("saved edit is unavailable or has advanced; original flight retained")
            if matches:
                replacement = record
                record = matches[0]
                _retry_conflict(record, replacement)
            else:
                records[record["id"]] = record
            if external:
                target_outbox.write(target)
            if owned:
                state["base"] = _pin(failure["details"]["current"])
                state["pending"] = []
                state["flight"] = None
                for field in ("conflict", "commit_options", "evaluation", "rationale", "group_id",
                              "media_input", "media_imports", "pending_close", "pending_close_path", "pending_how", "pending_part"):
                    state.pop(field, None)
            self.outbox.write(state)
        try:
            diff = _pending_diff(self, record)
        except Exception:  # noqa: BLE001 - the saved intent remains usable without enrichment
            diff = None
        if diff is not None:
            with self.outbox.locked():
                state = self._state()
                saved = state.get("pending_changes", {}).get(record["id"])
                if saved is not None and saved.get("state") == "pending":
                    saved["diff"] = diff
                    saved["rev"] = int(saved.get("rev", 0)) + 1
                    saved["updated_at"] = datetime.now(timezone.utc).isoformat()
                    self.outbox.write(state)
                    record = copy.deepcopy(saved)
        record = _pending_clash(self, record)
        error = CommitConflict(failure, label=label, timeline_id=self.timeline_id,
                               project_id=self.project_id, pending_id=record["id"],
                               clash=record.get("clash") or clash(record))
        return error

    def submit(self, commands, *, label, steps, merge="fields", reads=None,
               idempotency_key=None, dry_run=False, expected_state, base=None, evaluation=None,
               pending_id=None, pending_path=None, pending_part=None, pending_how="retry", group_id=None,
               logical_key=None, rationale=None, imports=()):
        """Freeze an already captured v2 batch, without recapturing its guards.

        A replacement is allowed only after an explicit conflict. Transport
        retries use flush(), which retains the entire original request.
        """
        commands = [_native_command(command) for command in commands]
        options = {"label": label, "steps": copy.deepcopy(steps), "merge": merge}
        if reads is not None:
            options["reads"] = copy.deepcopy(reads)

        def prepare(state):
            if state != expected_state:
                raise _OutboxChanged("outbox changed during evaluation; nothing was submitted")
            if state["pending"] or state["flight"] is not None:
                if state.get("conflict", {}).get("details", {}).get("status") != "conflict":
                    raise TimelineEditError("retry the original in-flight request before another edit")
                if state["flight"]["count"] != len(state["pending"]):
                    raise TimelineEditError("pending dependent commands require revalidation before recomputing")
                previous_logical_key = state["flight"].get("logical_key")
                if previous_logical_key is None and state["flight"].get("explicit_key"):
                    previous_logical_key = state["flight"]["key"]
                if previous_logical_key is not None:
                    if idempotency_key != previous_logical_key:
                        raise TimelineEditError("conflict reevaluation must preserve its original idempotency key")
                    state["next_logical_key"] = previous_logical_key
            state["base"] = _pin(base or state["base"])
            state["pending"] = copy.deepcopy(commands)
            state["flight"] = None
            state.pop("conflict", None)
            state["commit_options"] = options
            state["evaluation"] = copy.deepcopy(evaluation)
            if rationale is not None:
                state["rationale"] = rationale
            else:
                state.pop("rationale", None)
            if group_id is not None or pending_id is not None:
                state["group_id"] = group_id or pending_id
            else:
                state.pop("group_id", None)
            if logical_key is not None or (pending_id is not None and idempotency_key is not None):
                state["next_logical_key"] = logical_key or idempotency_key
            if imports:
                state["media_imports"] = copy.deepcopy(list(imports))
            if pending_id is not None:
                state["pending_close"] = pending_id
                state["pending_close_path"] = pending_path
                state["pending_how"] = pending_how
                state["pending_part"] = pending_part

        release_conflict = (not isinstance(evaluation, dict)
                            or evaluation.get("kind") != "function"
                            or evaluation.get("count", 0) >= 3)
        return self._flush(idempotency_key=idempotency_key, dry_run=dry_run, prepare=prepare,
                           release_conflict=release_conflict)

    def _flush(self, *, idempotency_key=None, dry_run=False, via="python", prepare=None,
               release_conflict=False):
        with self.outbox.locked():
            state = self._state()
            if prepare is not None:
                prepare(state)
            flight = state["flight"]
            if _flight_kind(flight) == "assembly-admission":
                raise TimelineEditError("retry the original assembly admission before flushing commands")
            confirmed = flight is not None
            if flight is None:
                if not state["pending"]:
                    raise TimelineEditError("no pending timeline commands")
                body = {"api_version": 1, "project_id": self.project_id, "timeline_id": self.timeline_id,
                        "base_revision_id": state["base"]["revision_id"],
                        "base_publication_id": state["base"]["publication_id"],
                        "commands": copy.deepcopy(state["pending"]), "source": {"via": via}, "dry_run": dry_run}
                if 'rationale' in state:
                    body['rationale'] = state['rationale']
                if 'group_id' in state:
                    body['group_id'] = state['group_id']
                body.update(copy.deepcopy(state.get("commit_options", {})))
                logical_key = state.pop("next_logical_key", None)
                conflict_retry = logical_key is not None
                if logical_key is None and idempotency_key is not None:
                    logical_key = idempotency_key
                key = (str(uuid.uuid4()) if state.get("pending_close") is not None
                       else f"timeline-edit-{uuid.uuid4().hex}" if conflict_retry
                       else idempotency_key if idempotency_key is not None
                       else f"timeline-edit-{uuid.uuid4().hex}")
                flight = {"path": self.path, "body": request_bytes(body).decode(), "key": key,
                          "count": len(state["pending"]), "fence": uuid.uuid4().hex,
                          "explicit_key": logical_key is not None}
                if state.get("pending_close") is not None:
                    flight["pending_id"] = state["pending_close"]
                    if state.get("pending_close_path") is not None:
                        flight["pending_path"] = state["pending_close_path"]
                    flight["pending_how"] = state.get("pending_how", "retry")
                    flight["pending_part"] = state.get("pending_part")
                if logical_key is not None:
                    flight["logical_key"] = logical_key
                    if conflict_retry:
                        idempotency_key = logical_key
                if state.get("evaluation") is not None:
                    flight["evaluation"] = copy.deepcopy(state["evaluation"])
                    cache = state["evaluation"].get("media_cache", {})
                    if isinstance(cache, dict):
                        imports, seen = [], set()
                        for entry in cache.values():
                            if not isinstance(entry, dict):
                                continue
                            for item in entry.get("imports", ()):
                                if not isinstance(item, dict):
                                    continue
                                object_id = item.get("object_id")
                                if object_id not in seen:
                                    seen.add(object_id)
                                    imports.append(copy.deepcopy(item))
                        flight["imports"] = imports
                if 'media_input' in state:
                    flight['media_input'] = copy.deepcopy(state['media_input'])
                    flight['imports'] = [*flight.get('imports', ()),
                                         *copy.deepcopy(state.get('media_imports', []))]
                elif state.get('media_imports'):
                    flight['imports'] = copy.deepcopy(state['media_imports'])
                state["flight"] = flight
                self.outbox.write(state)
            body = self._flight_body(state, flight, idempotency_key, dry_run)
            if "fence" not in flight:  # Preserve requests persisted before fencing was added.
                flight["fence"] = uuid.uuid4().hex
                self.outbox.write(state)
            flight = copy.deepcopy(flight)
        try:
            result = _response(self.client.timelines.commit(
                self.project_id, self.timeline_id, body, idempotency_key=flight["key"],
            ))
        except Exception as exc:
            details = getattr(exc, "details", {})
            if isinstance(details, dict) and details.get("status") in {"conflict", "refused", "invalid"}:
                failure = {"code": getattr(exc, "code", "conflict"), "message": str(getattr(exc, "message", exc)), "details": details}
                save_pending = release_conflict or details.get("status") != "conflict"
                record = None
                external_conflict = None
                target_path = flight.get("pending_path") if save_pending and flight.get("pending_id") else None
                with _retry_outbox_locks(self.outbox, target_path):
                    state = self._state()
                    if state["flight"] == flight:
                        self._flight_body(state, flight, idempotency_key, dry_run)
                        if save_pending:
                            from ._pending import snapshot
                            replacement = snapshot(state, flight, failure)
                            pending_id = flight.get("pending_id")
                            target_path = flight.get("pending_path")
                            if pending_id and target_path and Path(target_path).resolve() != self.outbox.path.resolve():
                                target_outbox = Outbox(Path(target_path))
                                target = target_outbox.read()
                                saved = target.get("pending_changes", {}).get(pending_id)
                                if saved is None or saved.get("state") != "pending":
                                    raise TimelineEditError("saved edit is unavailable; original flight retained")
                                _retry_conflict(saved, replacement, expected_part=flight.get("pending_part"))
                                target_outbox.write(target)
                                record = copy.deepcopy(saved)
                                external_conflict = (target_path, pending_id, replacement)
                            elif pending_id and pending_id in state.get("pending_changes", {}):
                                record = state["pending_changes"][pending_id]
                                _retry_conflict(record, replacement, expected_part=flight.get("pending_part"))
                                record = copy.deepcopy(record)
                            elif pending_id:
                                raise TimelineEditError("saved edit is unavailable; original flight retained")
                            else:
                                record = replacement
                                state.setdefault("pending_changes", {})[record["id"]] = record
                            if record is not None and details.get("status") != "conflict":
                                from ._pending import clash
                                record["clash"] = clash(record)
                                record["clash"]["next"] = f'tl.pending_show("{record["id"]}")'
                                if external_conflict is None:
                                    state["pending_changes"][record["id"]] = copy.deepcopy(record)
                            state["pending"] = []
                            state["flight"] = None
                            state.pop("conflict", None)
                            state.pop("commit_options", None)
                            state.pop("evaluation", None)
                            state.pop("rationale", None)
                            state.pop("group_id", None)
                            state.pop("media_input", None)
                            state.pop("media_imports", None)
                        else:
                            # Function commits retain the known conflict only
                            # while bounded reevaluation remains available.
                            state["conflict"] = copy.deepcopy(failure)
                        if release_conflict or details.get("status") != "conflict":
                            state.pop("pending_close", None)
                            state.pop("pending_close_path", None)
                            state.pop("pending_how", None)
                            state.pop("pending_part", None)
                        self.outbox.write(state)
                if record is not None:
                    try:
                        diff = _pending_diff(self, record)
                    except Exception:  # noqa: BLE001 - enrichment failure must not undo the saved record
                        diff = None
                    if diff is not None:
                        with self.outbox.locked():
                            latest = self._state()
                            saved = latest.get("pending_changes", {}).get(record["id"])
                            if saved is not None and saved.get("state") == "pending" and saved.get("diff") is None:
                                saved["diff"] = diff
                                saved["rev"] = int(saved.get("rev", 0)) + 1
                                saved["updated_at"] = datetime.now(timezone.utc).isoformat()
                                self.outbox.write(latest)
                                record = copy.deepcopy(saved)
                    if external_conflict is None and details.get("status") == "conflict":
                        record = _pending_clash(self, record)
                from ._pending import clash
                error_type = CommitConflict if details.get("status") == "conflict" else CommitRefused
                raise error_type(failure, fixed_list=release_conflict and details.get("status") == "conflict",
                                     timeline_id=self.timeline_id, project_id=self.project_id,
                                     label=record["label"] if record else None,
                                     pending_id=record["id"] if record else None,
                                     retry_clash=bool(details.get("status") == "conflict" and flight.get("pending_id") and record),
                                     clash=record.get("clash") or clash(record) if record else None) from exc
            raise CommitOutcomeUnknown(exc, key=_display_key(flight)) from exc  # Keep the durable flight byte-for-byte retryable.
        try:
            data, receipt = _ack(result, body, flight, self.identity)
        except TimelineEditError as exc:
            raise CommitOutcomeUnknown(exc, key=_display_key(flight)) from exc
        external_close = None
        with self.outbox.locked():
            state = self._state()
            # A duplicate send may finish after another send acknowledged this
            # prefix and even started the next flight. Its response is read-only.
            if state["flight"] == flight:
                self._flight_body(state, flight, idempotency_key, dry_run)
                if not dry_run:
                    external_pending = (flight.get('pending_id') is not None
                                        and flight.get('pending_path') is not None
                                        and Path(flight['pending_path']).resolve() != self.outbox.path.resolve())
                    if flight.get('explicit_key', idempotency_key is not None) or external_pending:
                        acknowledged = state.setdefault('acknowledged', {})
                        logical_key = flight.get('logical_key', flight['key'])
                        acknowledged[logical_key] = copy.deepcopy(flight)
                        if external_pending:
                            acknowledged[logical_key]['pending_ack'] = {
                                'revision_id': data['accepted_revision_id'],
                                'publication_id': receipt['receipt_id'],
                                'publisher': self.identity['actor_id'],
                                'receipt': copy.deepcopy(receipt),
                            }
                    state["base"] = _pin({"revision_id": data["accepted_revision_id"],
                                           "publication_id": receipt["receipt_id"]})
                    del state["pending"][:flight["count"]]
                    state.pop('rationale', None)
                    state.pop('group_id', None)
                    state.pop('media_input', None)
                    state.pop('media_imports', None)
                if "commit_options" in state:
                    if dry_run:
                        del state["pending"][:flight["count"]]
                    state.pop("commit_options", None)
                    state.pop("evaluation", None)
                state.pop("preview_key", None)
                state["flight"] = None
                state.pop("conflict", None)
                pending_id = flight.get("pending_id")
                if pending_id is not None and not dry_run:
                    target_path = flight.get("pending_path")
                    if target_path is not None and target_path != str(self.outbox.path):
                        external_close = (target_path, pending_id, flight.get("pending_how", "retry"))
                    else:
                        record = state.get("pending_changes", {}).get(pending_id)
                        if record is not None and record.get("state") == "pending":
                            _settle_pending(record, expected_part=flight.get("pending_part"), actor_id=self.identity["actor_id"],
                                            publication_id=receipt["receipt_id"],
                                            how=flight.get("pending_how", "retry"), base=state["base"])
                if pending_id is not None:
                    state.pop("pending_close", None)
                    state.pop("pending_close_path", None)
                    state.pop("pending_how", None)
                    state.pop("pending_part", None)
                self.outbox.write(state)
        if external_close is not None:
            target_path, pending_id, how = external_close
            try:
                target_outbox = Outbox(Path(target_path))
                with target_outbox.locked():
                    target = target_outbox.read()
                    record = target.get("pending_changes", {}).get(pending_id) if isinstance(target, dict) else None
                    if record is not None and record.get("state") == "pending":
                        _settle_pending(record, expected_part=flight.get("pending_part"), actor_id=self.identity["actor_id"],
                                        publication_id=receipt["receipt_id"], how=how,
                                        base={"revision_id": data["accepted_revision_id"],
                                              "publication_id": receipt["receipt_id"]})
                        target_outbox.write(target)
            except Exception:  # noqa: BLE001 - the accepted receipt stands if local close bookkeeping fails
                pass
        return self._present(result, data, receipt, imports=flight.get('imports', ()),
                             confirmed=confirmed, label=body.get("label"))

    def _present(self, result, data, receipt, *, imports=(), replayed=False, confirmed=False, label=None):
        result = copy.deepcopy(result)
        result["data"] = copy.deepcopy(data)
        if imports:
            result['imports'] = copy.deepcopy(list(imports))
        status = data.get("status")
        # Runtime previews carry no label; the caller's label is the one to show.
        label = data.get("label") or label or "Timeline edit"
        receipt_id = receipt.get("receipt_id") if isinstance(receipt, dict) else None
        if replayed and receipt_id:
            summary = f'Already committed as {receipt_id}; nothing was run or changed now.'
        elif confirmed and receipt_id:
            summary = f'Confirmed "{label}" as {receipt_id}; the earlier request was applied once.'
        elif status == "preview":
            summary = f'Preview of "{label}": nothing committed or reserved.'
            if imports:
                summary += f" Media: {len(imports)} file(s) added to the catalog now; they stay even if you never apply."
        elif status in {"accepted", "merged", "rebased"} and receipt_id:
            commands = data.get("commands", [])
            inverse = commands[0] if commands and isinstance(commands[0], dict) else {}
            publications = inverse.get("args", {}).get("publications", []) if inverse.get("op") == "timeline.inverse" else []
            if publications:
                summary = f'Undid {publications[0]} as {receipt_id}.'
            else:
                summary = f'Committed "{label}" as {receipt_id}.'
                if status in {"merged", "rebased"}:
                    summary = f'Committed "{label}" as {receipt_id}, combined with newer changes to the timeline.'
            if imports:
                summary += f" Media: {len(imports)} file(s) in the catalog; undo keeps them."
        else:
            summary = f'Timeline result: {status or "completed"}.'
        undo = {"publications": [receipt_id]} if receipt_id else None
        result.update(status=status, receipt=copy.deepcopy(receipt), undo=undo,
                      summary=summary, replayed=bool(replayed), confirmed=bool(confirmed))
        result["data"]["summary"] = summary
        if imports:
            result["data"]["imports"] = copy.deepcopy(list(imports))
        result["data"]["replayed"] = bool(replayed)
        result["data"]["confirmed"] = bool(confirmed)
        if undo is not None:
            result["data"]["undo"] = copy.deepcopy(undo)
        if receipt is not None:
            result["data"]["accepted_publication_id"] = receipt["receipt_id"]
            result["data"]["show_command"] = shlex.join([
                "astrid", "timelines", "show", self.timeline_id, "--project", self.project_id,
                "--published", "--revision-id", data["accepted_revision_id"],
            ])
        return result

    def _flight_body(self, state, flight, key, dry_run):
        from astrid.sdk.timeline._command_state import equal

        try:
            body = json.loads(flight["body"])
            count = flight["count"]
            valid = (type(count) is int and 0 < count <= len(state["pending"])
                     and isinstance(flight["key"], str) and bool(flight["key"])
                     and flight["path"] == self.path
                     and equal(body["commands"], state["pending"][:count])
                     and request_bytes(body).decode() == flight["body"]
                     and body["project_id"] == self.project_id and body["timeline_id"] == self.timeline_id
                     and body["base_revision_id"] == state["base"]["revision_id"]
                     and body["base_publication_id"] == state["base"]["publication_id"]
                     and (key is None or key in (flight["key"], flight.get("logical_key")))
                     and body.get("dry_run", False) == dry_run)
        except (KeyError, TypeError, ValueError) as exc:
            raise TimelineEditError("invalid in-flight request; original intent retained") from exc
        if not valid:
            raise TimelineEditError("in-flight request is immutable; retry its original payload and key")
        return body
