"""Request-bound immutable transcript reads; no source-document write authority."""
from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from astrid.sdk.managed_transcript import (
    TRANSCRIPT_DOCUMENT_KIND,
    canonical_transcript_json,
    transcript_document_id,
    validate_transcript_record,
)
from astrid.sdk.timeline._common import TimelineEditError

if TYPE_CHECKING:
    from banodoco_workspace_client.generated import CommitRequest


def digest_bytes(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _json(value):
    from astrid.sdk.timeline.commit import _digest
    # The command contract owns canonical selection serialization.
    return _digest(value)


@dataclass(frozen=True)
class TranscriptSourceSelection:
    """A locator/precondition, never an authenticated receipt."""
    project_id: str
    descriptor_json: str

    def __post_init__(self):
        try:
            descriptor = json.loads(self.descriptor_json)
        except (TypeError, ValueError) as exc:
            raise TimelineEditError("transcript descriptor must be JSON") from exc
        if not isinstance(self.project_id, str) or not self.project_id or not isinstance(descriptor, dict) or set(descriptor) != {
                "schema_version", "document", "snapshot", "subject", "source", "clock"}:
            raise TimelineEditError("transcript selection requires the exact v1 source descriptor")
        if any(not isinstance(descriptor[k], dict) for k in ("document", "snapshot", "subject", "source", "clock")):
            raise TimelineEditError("transcript descriptor pins must be objects")
        if (type(descriptor["schema_version"]) is not int or descriptor["schema_version"] != 1
                or set(descriptor["document"]) != {"id", "version", "content_digest"}
                or type(descriptor["document"]["version"]) is not int or descriptor["document"]["version"] < 1
                or set(descriptor["snapshot"]) != {"object_id", "sha256"}):
            raise TimelineEditError("invalid transcript document/snapshot preconditions")
        doc, snapshot = descriptor["document"], descriptor["snapshot"]
        import re
        if (not isinstance(doc["content_digest"], str)
                or not re.fullmatch(r"sha256:[0-9a-f]{64}", doc["content_digest"])
                or snapshot["object_id"] != doc["content_digest"]
                or snapshot["sha256"] != doc["content_digest"].removeprefix("sha256:")):
            raise TimelineEditError("transcript snapshot identity differs from content digest")
        if set(descriptor["source"]) != {"producer_id", "source_id", "lineage_id", "revision_id", "producer_digest"}:
            raise TimelineEditError("transcript source descriptor requires only immutable identity pins")
        # Validate the source descriptor with the single record codec.
        try:
            validate_transcript_record(dict(schema_version=1, subject=descriptor["subject"],
                source=descriptor["source"], clock=descriptor["clock"], words=[], segments=[]))
        except ValueError as exc:
            raise TimelineEditError(str(exc)) from exc
        if doc["id"] != transcript_document_id(self.project_id, descriptor["subject"], descriptor["source"]):
            raise TimelineEditError("transcript document identity differs from project/media/source")

    @classmethod
    def from_reference(cls, project_id, reference):
        if not isinstance(reference, dict):
            raise TimelineEditError("transcript reference must be an object")
        descriptor = {k: copy.deepcopy(reference[k]) for k in (
            "schema_version", "document", "snapshot", "subject", "source", "clock") if k in reference}
        return cls(project_id, json.dumps(descriptor, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False))

    def descriptor(self):
        return json.loads(self.descriptor_json)

    @property
    def key(self):
        return _json(dict(project_id=self.project_id, **self.descriptor()))


def verify_snapshot(selection, raw):
    if not isinstance(selection, TranscriptSourceSelection) or not isinstance(raw, bytes):
        raise TimelineEditError("typed transcript selection and immutable bytes required")
    d = selection.descriptor()
    if digest_bytes(raw) != d["snapshot"]["object_id"]:
        raise TimelineEditError("missing or changed immutable transcript object")
    try:
        record = validate_transcript_record(json.loads(raw))
    except ValueError as exc:
        raise TimelineEditError("invalid immutable transcript snapshot") from exc
    if canonical_transcript_json(record).encode("utf-8") != raw:
        raise TimelineEditError("transcript snapshot is not the canonical helper bytes")
    if (any(record[k] != d[k] for k in ("subject", "clock"))
            or any(record["source"].get(k) != v for k, v in d["source"].items())):
        raise TimelineEditError("transcript snapshot media/source/clock mismatch")
    return record


_CAPTURE = object()


@dataclass(frozen=True, init=False)
class FrozenTranscriptSource:
    selection: TranscriptSourceSelection
    snapshot_bytes: bytes
    request_digest: str

    def __init__(self, selection, snapshot_bytes, request_digest, *, _authority=None):
        if _authority is not _CAPTURE:
            raise TypeError("transcript captures are minted only by the request-bound resolver")
        verify_snapshot(selection, snapshot_bytes)
        object.__setattr__(self, "selection", selection)
        object.__setattr__(self, "snapshot_bytes", snapshot_bytes)
        object.__setattr__(self, "request_digest", request_digest)

    def entry(self):
        return dict(selection=dict(project_id=self.selection.project_id, **self.selection.descriptor()),
                    canonical_record_json=self.snapshot_bytes.decode("utf-8"))


class TranscriptCommitPort(Protocol):
    def resolve_transcript_source(self, request: CommitRequest,
                                  selection: TranscriptSourceSelection) -> FrozenTranscriptSource: ...


def request_selections(request, calls):
    """Only explicit transcript commands (including batches) extend the read port."""
    from astrid.sdk.timeline.commands import EditCommand
    result = {}
    for value in calls:
        call = EditCommand.coerce(value)
        if call.op == "timeline.batch":
            result.update(request_selections(request, call.args.get("commands", [])))
        if call.op in {"transcript.bind", "transcript.reference"}:
            ref = call.args.get("next")
            if ref != {"op": "clear"}:
                selection = TranscriptSourceSelection.from_reference(request["project_id"], ref)
                result[selection.key] = selection
    return result


def base_selections(project_id, base):
    """Only an authenticated accepted base may supply these retained locators."""
    from astrid.sdk.timeline.checkout import Checkout
    from astrid.sdk.timeline.commands import entity_states
    result = {}
    states = entity_states(Checkout(copy.deepcopy(base)))
    app = states["parent"].get("config", {}).get("app")
    declaration = ({} if app is None else app).get("transcript", {})
    if not isinstance(declaration, dict):
        raise TimelineEditError("invalid accepted transcript declaration")
    references = declaration.get("references", {})
    if not isinstance(references, dict):
        raise TimelineEditError("invalid accepted transcript reference index")
    if references and (type(declaration.get("v")) is not int or declaration["v"] != 1):
        raise TimelineEditError("accepted transcript references require namespace v=1")
    refs = list(references.values())
    for sid, state in states.items():
        if "/clip:" not in sid:
            continue
        app = state.get("app")
        namespace = ({} if app is None else app).get("transcript", {})
        if not isinstance(namespace, dict):
            raise TimelineEditError("invalid accepted transcript binding namespace")
        if "binding" in namespace:
            if type(namespace.get("v")) is not int or namespace["v"] != 1:
                raise TimelineEditError("accepted transcript binding requires namespace v=1")
            refs.append(namespace["binding"])
    for ref in refs:
        if ref is not None:
            selection = TranscriptSourceSelection.from_reference(project_id, ref)
            result[selection.key] = selection
    return result


class RequestBoundTranscriptResolver:
    """A's COMMIT adapter delegates here with its authenticated restricted ports.

    accepted_base is supplied by the existing exact revision closure, never a
    journal. That closure permits CAS-only reads for old accepted bindings.
    """
    def __init__(self, request, documents, objects, *, accepted_base, publication=None, accepted_history=()):
        if (accepted_base.get("project_id"), accepted_base.get("timeline_id")) != (request["project_id"], request["timeline_id"]):
            raise TimelineEditError("accepted transcript base has wrong project/timeline scope")
        self._request = copy.deepcopy(request)
        self._documents, self._objects = documents, objects
        self._accepted = base_selections(request["project_id"], accepted_base)
        declared = request_selections(request, request["change"]["commands"])
        inverse_reads = inverse_selections(request["project_id"], request["change"]["commands"])
        for reference in accepted_history:
            selection = TranscriptSourceSelection.from_reference(request["project_id"], reference)
            if selection.key in declared or selection.key in inverse_reads:
                self._accepted[selection.key] = selection
        self._allowed = {**self._accepted, **declared}
        self._publication = publication

    def resolve_transcript_source(self, request: CommitRequest,
                                  selection: TranscriptSourceSelection) -> FrozenTranscriptSource:
        if _json(request) != _json(self._request) or not isinstance(selection, TranscriptSourceSelection):
            raise TimelineEditError("transcript source request mismatch")
        if selection.project_id != request["project_id"] or self._allowed.get(selection.key) != selection:
            raise TimelineEditError("transcript source is outside the request selection allowlist")
        d = selection.descriptor()
        if selection.key not in self._accepted:
            # Same-invocation capture is supported by the publisher's internal
            # result; serializable journal data never takes this branch.
            captured = False
            if self._publication is not None:
                from astrid.sdk.transcript_publication_types import ResolvedTranscriptPublication
                if not isinstance(self._publication, ResolvedTranscriptPublication):
                    raise TimelineEditError("external transcript receipt is not an internal capture")
                from astrid.sdk.transcript_publication import publication_matches_selection
                captured = publication_matches_selection(self._publication, request, selection)
            if not captured:
                row = self._documents.get_document(d["document"]["id"])
                if not isinstance(row, dict) or (
                        row.get("project_id"), row.get("document_id"), row.get("version"), row.get("kind")) != (
                        selection.project_id, d["document"]["id"], d["document"]["version"], TRANSCRIPT_DOCUMENT_KIND):
                    raise TimelineEditError("transcript current document advanced or has wrong identity")
                if digest_bytes(canonical_transcript_json(row["content"]).encode("utf-8")) != d["document"]["content_digest"]:
                    raise TimelineEditError("transcript current document content mismatch")
        try:
            response = self._objects.get_object(d["snapshot"]["object_id"])
        except Exception as exc:
            if getattr(exc, "code", None) != "not_found":
                raise
            raise TimelineEditError("accepted transcript CAS object is missing") from exc
        if not isinstance(response, dict) or response.get("status") != 200:
            raise TimelineEditError("transcript CAS did not return complete authenticated bytes")
        return FrozenTranscriptSource(selection, response.get("data"), _json(request), _authority=_CAPTURE)


def inverse_selections(project_id, calls):
    """Inverse locators still require accepted-history authority in the port."""
    from astrid.sdk.timeline.commands import EditCommand
    result = {}
    def walk(value):
        if isinstance(value, dict):
            if {"schema_version", "document", "snapshot", "subject", "source", "clock"} <= set(value):
                selection = TranscriptSourceSelection.from_reference(project_id, value)
                result[selection.key] = selection
            else:
                for child in value.values():
                    walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)
    for value in calls:
        call = EditCommand.coerce(value)
        if call.op == "timeline.batch":
            result.update(inverse_selections(project_id, call.args.get("commands", [])))
        elif call.op == "timeline.inverse":
            for write in call.args.get("writes", []):
                # Only authored transcript paths/containers; never arbitrary
                # source_receipt/journal attachments supplied by the caller.
                path = write.get("path", "")
                if "transcript" in path or path in {"", "app", "config", "config.app"}:
                    for state in (write.get("before", {}), write.get("after", {})):
                        if state.get("state") == "present":
                            walk(state.get("value"))
    return result


def validate_bound_dependencies(tl):
    from astrid.sdk.timeline.transcript_commands import dependency
    for selection in base_selections(tl.bundle["project_id"], tl.bundle).values():
        dependency(tl, selection.descriptor())


def verify_pinned_entries(project_id, entries):
    """Integrity-only offline verification; journal bytes confer no authority."""
    if not isinstance(entries, dict):
        raise TimelineEditError("invalid pinned transcript closure")
    result = {}
    for key, entry in entries.items():
        if not isinstance(entry, dict) or set(entry) != {"selection", "canonical_record_json"}:
            raise TimelineEditError("invalid pinned transcript dependency")
        ref = entry["selection"]
        if not isinstance(ref, dict) or ref.get("project_id") != project_id:
            raise TimelineEditError("pinned transcript dependency has wrong project")
        selection = TranscriptSourceSelection.from_reference(project_id, ref)
        if selection.key != key:
            raise TimelineEditError("pinned transcript dependency has wrong selection digest")
        text = entry.get("canonical_record_json")
        if not isinstance(text, str):
            raise TimelineEditError("pinned transcript snapshot is missing")
        verify_snapshot(selection, text.encode("utf-8"))
        result[key] = copy.deepcopy(entry)
    return result


def install_timing_dependencies(tl):
    """Feed D's verified closure to F's single evaluator when its join is present."""
    entries = tl.bundle.get("source_mapping", {}).get("pinned_transcripts", {})
    if not entries:
        return False
    verified = verify_pinned_entries(tl.bundle["project_id"], entries)
    try:
        from astrid.sdk.timeline.transcript_binding import FrozenTranscript
    except ModuleNotFoundError as exc:
        if exc.name != "astrid.sdk.timeline.transcript_binding":
            raise
        return False  # This admitted pin predates F's independently owned join.
    tl._transcript_dependencies = tuple(FrozenTranscript(
        TranscriptSourceSelection.from_reference(tl.bundle["project_id"], entry["selection"]).descriptor(),
        entry["canonical_record_json"].encode("utf-8")) for entry in verified.values())
    return True
