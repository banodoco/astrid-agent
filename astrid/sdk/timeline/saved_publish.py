"""Saved-draft COMMIT producer. The existing journal owns its write-ahead state.

No lock crosses remote I/O. Preparation is optimistic, then rechecked under
one draft→journal lock pair; competing processes reuse the winner's envelope.
Runtime remains the authenticated, idempotent acceptance authority.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
from contextlib import contextmanager
from pathlib import Path

from astrid.sdk.authoring_revision_diff import retain_revision_closure
from astrid.sdk.timeline import journal, store
from astrid.sdk.timeline._common import TimelineEditError
from astrid.sdk.timeline.chapter_storage import compile_candidate as compile_authoring_candidate
from astrid.sdk.timeline.commit import RuntimeCommitService, _digest
from astrid.sdk.timeline.doc import _serialize
from astrid.sdk.timeline.saved_intent import (
    SavedIntentError, _finite_json, _read_saved_calls_locked, read_saved_calls,
)

_RETAINED = "accepted; newer local edits retained, refresh required"
_SCOPE = dict(target="parent", complete=[], projection_version=1)


def _workspace(client):
    # AstridClient -> RemoteAstridClient -> the existing generated facade.
    remote = getattr(client, "_remote", client)
    return getattr(remote, "_transport", remote)


def _authenticate(workspace):
    from astrid.sdk.client import _REQUIRED_SCOPES, _validate_health, _validate_handshake
    from banodoco_workspace_client.contract_metadata import PROTOCOL, SCHEMA_DIGEST
    from astrid.version import __version__

    _validate_health(workspace.health(), expected_protocol=PROTOCOL, expected_digest=SCHEMA_DIGEST)
    handshake = workspace.handshake("astrid", __version__, sorted(_REQUIRED_SCOPES))
    if not isinstance(handshake, dict):
        raise TimelineEditError("publish requires an authenticated Runtime handshake")
    _validate_handshake(handshake, expected_protocol=PROTOCOL, expected_digest=SCHEMA_DIGEST,
                        expected_realm=handshake.get("realm_id"), expected_actor=handshake.get("actor_id"))
    if not all(isinstance(handshake.get(key), str) and handshake[key].strip()
               for key in ("realm_id", "actor_id")):
        raise TimelineEditError("publish requires authenticated realm and principal")
    return dict(realm_id=handshake["realm_id"], principal=handshake["actor_id"]), handshake["session_id"]


class _ClosurePort:
    """Exact generated resource reads, using the same pins as open_bundle."""
    def __init__(self, workspace, project, timeline, *, accepted_transcripts=()):
        self.workspace, self.project, self.timeline = workspace, project, timeline
        self._accepted_transcripts = tuple(accepted_transcripts)

    def bind_transcript_request(self, request, base):
        from types import SimpleNamespace

        from astrid.sdk.timeline.transcripts import RequestBoundTranscriptResolver
        documents = SimpleNamespace(get_document=lambda document_id:
            self.workspace.get_document(self.project, document_id))
        objects = SimpleNamespace(get_object=self.workspace.get_object)
        self._transcript_resolver = RequestBoundTranscriptResolver(
            request, documents, objects, accepted_base=base, accepted_history=self._accepted_transcripts)

    def resolve_transcript_source(self, request, selection):
        return self._transcript_resolver.resolve_transcript_source(request, selection)

    def narration_take(self, refs):
        from astrid.sdk.timeline.narration import read_managed_take
        return read_managed_take(self.workspace, self.project, refs)

    def _read(self, operation, *args, **expected):
        row = getattr(self.workspace, operation)(self.project, *args)
        if not isinstance(row, dict) or any(row.get(key) != value for key, value in expected.items()):
            raise TimelineEditError("exact revision resource differs from its requested scope or pin")
        return row

    def _shot(self, shot_id, revision):
        return self._read("get_project_shot_revision", shot_id, revision,
                          project_id=self.project, shot_id=shot_id, revision_id=revision)

    def _internal(self, shot, revision):
        timeline = shot.get("internal_timeline_id")
        if not isinstance(timeline, str) or not timeline:
            raise TimelineEditError("exact shot resource omitted its internal timeline identity")
        return self._read("get_project_timeline_revision", timeline, revision,
                          project_id=self.project, timeline_id=timeline, revision_id=revision)

    def revision_closure(self, revision):
        w, p, t = self.workspace, self.project, self.timeline
        if isinstance(revision, dict):
            if "timeline_id" in revision:
                from astrid.sdk.timeline.graph_commands import duplicate_source_ref
                ref = duplicate_source_ref(revision)
                if ref["timeline_id"] == t:
                    raise TimelineEditError("duplicate source must be a distinct timeline")
                t, revision = ref["timeline_id"], ref["parent_revision_id"]
                page, cursor = w.list_project_parent_composition_revisions(p, t, limit=200)
                while not any(r.get("revision_id") == revision and r.get("publication_id") == ref["publication_id"] for r in page):
                    if not cursor:
                        raise TimelineEditError("duplicate source exact publication is unavailable")
                    page, cursor = w.list_project_parent_composition_revisions(p, t, limit=200, cursor=cursor)
            else:
                from astrid.sdk.timeline.graph_commands import source_ref
                ref = source_ref(revision)
                if "parent_revision_id" in ref:
                    raise TimelineEditError("direct shot selection cannot contain parent pins")
                shot = self._shot(ref["shot_id"], ref["shot_revision_id"])
                internal = self._internal(shot, ref["internal_revision_id"])
                return dict(shot_revision=shot, internal_timeline_revision=internal)
        parent = self._read("get_project_parent_composition_revision", t, revision,
                            project_id=p, timeline_id=t, revision_id=revision)
        shots, internals = {}, {}
        for occurrence in parent["payload"]["occurrences"]:
            sid, rid = occurrence["shot_id"], occurrence["shot_revision_id"]
            if rid not in shots:
                shots[rid] = self._shot(sid, rid)
            shot = shots[rid]
            irid = shot["payload"]["internal_timeline_revision_id"]
            if irid not in internals:
                internals[irid] = self._internal(shot, irid)
            elif internals[irid]["timeline_id"] != shot.get("internal_timeline_id"):
                raise TimelineEditError("shared internal revision pin has conflicting timeline identity")
        self.closure = dict(parent_revision=parent, shot_revisions=list(shots.values()),
                            internal_timeline_revisions=list(internals.values()))
        return self.closure


def _exact(workspace, body, revision, *, base_document=None):
    port = _ClosurePort(workspace, body["project_id"], body["timeline_id"])
    bundle = RuntimeCommitService._load_revision(port, body, revision)
    if isinstance(base_document, dict):
        # _load_revision marks fetched source shots as linked for authoring
        # identity checks. A saved base may intentionally have no linked flag;
        # restore that authored state before comparing/replaying the draft.
        saved_shots = base_document.get("source_mapping", {}).get("shots", {})
        for shot_id, source in bundle.get("source_mapping", {}).get("shots", {}).items():
            saved = saved_shots.get(shot_id, {}) if isinstance(saved_shots, dict) else {}
            if isinstance(saved, dict) and "linked" in saved:
                source["linked"] = saved["linked"]
            else:
                source.pop("linked", None)
    # Retain the same raw rows only after the exact closure has been validated.
    closure = port.closure
    return retain_revision_closure(
        bundle, closure["parent_revision"],
        {row["revision_id"]: row for row in closure["shot_revisions"]},
        {row["revision_id"]: row for row in closure["internal_timeline_revisions"]})


def _publication(workspace, body, anchor):
    known = anchor.get("publication_id")
    if known is not None:
        return known
    # This existing route is explicitly project/timeline scoped. One bounded
    # page is optional enrichment. A nonterminal page leaves identity unknown,
    # even if it happens to contain a matching event.
    page = workspace.list_project_parent_composition_revisions(
        body["project_id"], body["timeline_id"], limit=50)
    rows, cursor = page
    if cursor:
        return None
    matches = [row for row in rows if row.get("revision_id") == body["base_revision_id"]]
    if any(row.get("project_id", body["project_id"]) != body["project_id"] or
           row.get("timeline_id", body["timeline_id"]) != body["timeline_id"] for row in matches):
        raise TimelineEditError("exact-base publication history has wrong scope")
    return matches[0].get("publication_id") if len(matches) == 1 else None


def request_id(origin, body, saved_digest):
    value = dict(realm_id=origin["realm_id"], project_id=body["project_id"],
                 timeline_id=body["timeline_id"], base_revision_id=body["base_revision_id"],
                 scope=body["scope"], saved_draft_digest=saved_digest)
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    return "edit-v1-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _key(key):
    if key is not None and (not isinstance(key, str) or not key or any(ord(c) < 32 or ord(c) > 126 for c in key)):
        raise TimelineEditError("publish idempotency key must be a nonempty printable ASCII HTTP header value")
    return key


@contextmanager
def _locked(checkout):
    path = Path(checkout.path)
    log = journal.journal_path(checkout.bundle["project_id"], checkout.bundle["timeline_id"])
    with store._file_lock(path), journal._locked(log):
        rows = read_rows(checkout, log.read_text(encoding="utf-8") if log.is_file() else "")
        yield path, log, rows


def read_rows(checkout, text):
    """Validate a detached journal snapshot for this exact draft scope."""
    if not text or journal.verify_text(text):
        raise SavedIntentError("publish journal is missing or damaged; run timelines log --verify")
    rows = [_finite_json(line) for line in text.splitlines()]
    ids = [row.get("id") for row in rows if isinstance(row, dict)]
    if (len(ids) != len(rows) or len(set(ids)) != len(ids) or
            any(not isinstance(i, str) or not i for i in ids) or
            any(row.get("v") != journal.VERSION or not isinstance(row.get("draft"), str)
                or not row["draft"] for row in rows)):
        raise SavedIntentError("publish journal has ambiguous identities/version/draft scope")
    if any(row.get("project") != checkout.bundle["project_id"] or
           row.get("timeline") != checkout.bundle["timeline_id"] for row in rows):
        raise SavedIntentError("publish journal has wrong project/timeline scope")
    return [row for row in rows if row.get("draft") == Path(checkout.path).stem]



def _state(rows):
    intent = result = checkpoint = None
    for row in rows:
        for item in row.get("evidence", []):
            kind = item.get("kind") if isinstance(item, dict) else None
            if kind == "publish-intent-v1":
                if intent is not None and (result is None or (
                        result["result"]["status"] in {"accepted", "no_change"} and checkpoint is None)):
                    raise SavedIntentError("publish journal replaced an unresolved envelope")
                if (row.get("kind") != "publish" or row.get("calls") != [] or
                        item.get("request_hash") != _digest(item["body"])):
                    raise SavedIntentError("publish journal has invalid immutable envelope evidence")
                intent, result, checkpoint = item, None, None
            elif kind == "publish-result-v1":
                if intent is None or item.get("intent_id") != intent["intent_id"]:
                    raise SavedIntentError("publish result lacks its exact intent")
                if (item["result"].get("request_id") != intent["body"]["request_id"] or
                        item["result"].get("request_hash") != intent["request_hash"]):
                    raise SavedIntentError("publish result has wrong request identity/hash")
                result = item
            elif kind == "publish-checkpoint-v1":
                if result is None or item.get("intent_id") != intent["intent_id"]:
                    raise SavedIntentError("publish checkpoint lacks its exact result")
                if (item.get("sequence") != row["seq"] or item.get("after") != row.get("after") or
                        row.get("before") != intent["saved_draft_digest"]):
                    raise SavedIntentError("publish checkpoint has wrong continuity")
                bases = [e for e in row.get("evidence", []) if isinstance(e, dict) and e.get("kind") == "saved-base-v1"]
                accepted = result["result"]
                revision = accepted.get("accepted_revision_id") if accepted.get("status") == "accepted" else accepted.get("previous_revision_id")
                if (accepted.get("status") not in {"accepted", "no_change"} or len(bases) != 1 or
                        bases[0].get("document") != result.get("document") or
                        not isinstance(result.get("document"), dict) or
                        result["document"].get("base_parent", {}).get("revision_id") != revision or
                        journal.document_digest(result["document"]) != item["after"]):
                    raise SavedIntentError("publish checkpoint is not the acknowledged exact revision")
                checkpoint = item
    return intent, result, checkpoint


def _append(log, checkout, evidence, *, outcome="custody", before=None, after=None, reason=None):
    # Acceptance identity comes only from the verified Runtime receipt.
    actor = dict(kind="unknown", id="unknown", session=None)
    for item in evidence:
        if item.get("kind") == "publish-result-v1" and item.get("document") is not None:
            publisher = (item["result"].get("runtime_receipt") or {}).get("publisher")
            if isinstance(publisher, str) and publisher.startswith(("agent:", "human:")):
                kind, _, identity = publisher.partition(":")
                if identity:
                    actor = dict(kind=kind, id=identity, session=None)
    return journal._append_locked(
        log, project=checkout.bundle["project_id"], timeline=checkout.bundle["timeline_id"],
        draft=Path(checkout.path).stem, kind="publish", outcome=outcome, label="publish",
        # Local environment assertions never become publisher identity.
        actor=actor,
        source=dict(via="publish", detail="saved COMMIT"), before=before, after=after,
        evidence=evidence, reason=reason)


def _origin(intent, origin, explicit):
    if intent["origin"] != origin:
        raise TimelineEditError("pending publish origin differs from current authenticated realm/principal")
    if explicit is not None and explicit != intent["body"]["request_id"]:
        raise TimelineEditError("resolve the pending publish with its original request ID first")
    if intent["request_hash"] != _digest(intent["body"]):
        raise SavedIntentError("stored publish body does not match its complete request hash")


def _active(checkout, rows, path):
    intent, result, checkpoint = _state(rows)
    if intent is None:
        return None
    if result is None:
        return intent, None, None
    if result["result"]["status"] not in {"accepted", "no_change"}:
        return None
    # A saved-base checkpoint anchors subsequent commands. Until it exists,
    # the acknowledged result owns recovery of any interrupted replacement.
    text = path.read_bytes().decode("utf-8")
    _finite_json(text)
    digest = journal.digest(text)
    if checkpoint is None or digest in {intent["saved_draft_digest"], checkpoint["after"]}:
        return intent, result, checkpoint
    if any(row.get("kind") == "save" and row["seq"] > checkpoint["sequence"] for row in rows):
        return None
    raise SavedIntentError("draft changed after its acknowledged checkpoint without saved commands")


def _prepare(checkout, workspace, origin, session, message, explicit, dry_run):
    from astrid.sdk.timeline.checkout import Checkout
    with _locked(checkout) as (path, _log, rows):
        text = path.read_bytes().decode("utf-8")
        _finite_json(text)
        saved_digest = journal.digest(text)
        anchors = [row for row in rows if any(isinstance(e, dict) and e.get("kind") == "saved-base-v1"
                                             for e in row.get("evidence", []))]
        if not anchors:
            raise SavedIntentError("Saved intent has no initial checkout anchor")
        anchor = anchors[-1]
        generation = anchor["id"]
        base_evidence = next(e for e in anchor["evidence"] if e.get("kind") == "saved-base-v1")
    # Reload immutable speech context only for a fresh publication. Pending
    # envelopes take the recovery branch before _prepare and perform no reads.
    from astrid.sdk.timeline.narration import capture_takes
    take_port = _ClosurePort(workspace, checkout.bundle["project_id"], checkout.bundle["timeline_id"])
    # A refresh save carries both the new exact base and the pending calls
    # replayed onto it. Include that checkpoint row when rebuilding the draft.
    saved = [call for row in rows if row["seq"] >= anchor["seq"] and row.get("kind") == "save"
             for call in row.get("calls", [])]
    takes = capture_takes(take_port, base_evidence["document"], saved)
    checkout._narration_takes = takes
    calls = read_saved_calls(checkout)
    body = dict(api_version=1, project_id=checkout.bundle["project_id"], timeline_id=checkout.bundle["timeline_id"],
                session_id=session, session_sequence=1, base_revision_id=checkout.base_revision,
                base_publication_id=None, scope=copy.deepcopy(_SCOPE), change=dict(commands=list(calls)),
                source=dict(via="cli" if store._cli_running() else "python"), label=message, dry_run=dry_run)
    body["request_id"] = explicit or request_id(origin, body, saved_digest)
    exact = Checkout(_exact(workspace, body, body["base_revision_id"],
                            base_document=base_evidence["document"]))
    if exact.document() != base_evidence["document"]:
        raise SavedIntentError("saved base differs from the authenticated exact revision closure")
    replay = exact
    # Recreate each saved row's authenticated context, including inverse rows
    # whose source attachments are provenance only, not semantic COMMIT reads.
    from astrid.sdk.timeline.source_hydration import hydrate_sources, journal_sources
    port = _ClosurePort(workspace, body["project_id"], body["timeline_id"])
    for row in rows:
        if row["seq"] < anchor["seq"] or row.get("kind") != "save":
            continue
        document = replay.document()
        hydrate_sources(port, body, document, row.get("calls", []),
                        parent_loader=lambda rid: _exact(workspace, body, rid), include_base=False)
        journal_sources(workspace, body, document, row)
        document, _plan, _direct = RuntimeCommitService._apply(document, row.get("calls", []), narration_takes=takes)
        replay = Checkout(document)
        replay._sync_film_length()
    if _serialize(replay._annotated()) != text:
        raise SavedIntentError("exact-base commands do not reproduce the saved canonical draft")
    compile_authoring_candidate(replay.document())
    body["base_publication_id"] = _publication(workspace, body, base_evidence)
    return dict(kind="publish-intent-v1", intent_id=body["request_id"] + ":" + _digest(body),
                body=body, origin=origin, saved_draft_digest=saved_digest,
                generation=generation, saved_bytes_digest=hashlib.sha256(text.encode("utf-8")).hexdigest(),
                request_hash=_digest(body))


def _preview(workspace, intent):
    # Use the caller's authenticated COMMIT route for scene evaluation. Preview
    # neither reserves this request ID nor acknowledges the normal envelope.
    body = copy.deepcopy(intent["body"])
    body["dry_run"] = True
    result = workspace.commit_timeline(copy.deepcopy(body))
    if (not isinstance(result, dict) or result.get("request_id") != body["request_id"] or
            result.get("request_hash") != _digest(body) or
            result.get("base_revision_id") != body["base_revision_id"] or
            result.get("status") != "preview"):
        raise TimelineEditError("unverified COMMIT preview identity/hash/status; saved publish not prepared")
    return result


def _verify_result(workspace, intent, result):
    from astrid.sdk.timeline.checkout import Checkout
    body = intent["body"]
    if (not isinstance(result, dict) or result.get("request_id") != body["request_id"] or
            result.get("request_hash") != intent["request_hash"] or
            result.get("base_revision_id") != body["base_revision_id"]):
        raise TimelineEditError("unverified COMMIT result identity/hash; saved publish remains pending")
    status = result.get("status")
    if status not in {"accepted", "no_change"}:
        # Only explicitly uncommitted semantic outcomes are conclusive.
        error = result.get("error") or {}
        conclusive = status in {"conflict", "refused"} or error.get("code") in {
            "request_id_conflict", "bad_request", "base_not_found", "unsupported_feature"}
        if any(result.get(key) is not None for key in ("accepted_revision_id", "accepted_publication_id", "runtime_receipt")):
            conclusive = False
        return conclusive, None
    receipt = result.get("runtime_receipt")
    canonical = result.get("canonical_document")
    revision = result.get("accepted_revision_id") if status == "accepted" else result.get("previous_revision_id")
    if (not isinstance(receipt, dict) or receipt.get("publisher") != intent["origin"]["principal"] or
            not receipt.get("accepted_at") or not receipt.get("evidence_digest") or
            not isinstance(canonical, dict) or not isinstance(revision, str) or not revision or
            result.get("document_digest") != _digest(canonical) or
            canonical.get("project_id") != body["project_id"] or canonical.get("timeline_id") != body["timeline_id"] or
            (status == "accepted" and not result.get("accepted_publication_id"))):
        raise TimelineEditError("unverified COMMIT acceptance receipt/document; saved publish remains pending")
    exact = Checkout(_exact(workspace, body, revision))
    from astrid.sdk.timeline.duplicate_fidelity import verify_duplicate
    verify_duplicate(workspace, body, canonical, exact.document())
    # Canonical authored digest and saved-file digest are intentionally distinct.
    # Compare compiled parent pins and closure content, not old provenance fields.
    compiled = compile_authoring_candidate(canonical)
    accepted = compile_authoring_candidate(exact.document())
    if compiled.publication["parent_composition"] != accepted.publication["parent_composition"]:
        raise TimelineEditError("COMMIT accepted revision differs from its canonical document")
    return True, exact.document()


def _replace(path, text):
    temporary = path.with_name(f".{path.name}.{os.getpid()}.publish.tmp")
    try:
        with temporary.open("wb") as handle:
            handle.write(text.encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        journal._fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _finalize(checkout, intent, result_record):
    from astrid.sdk.timeline.checkout import Checkout
    result = copy.deepcopy(result_record["result"])
    document = result_record["document"]
    acknowledged = Checkout(copy.deepcopy(document))
    text = _serialize(acknowledged._annotated())
    after = journal.digest(text)
    with _locked(checkout) as (path, log, rows):
        current = path.read_bytes().decode("utf-8")
        _finite_json(current)
        active_intent, _result, checkpoint = _state(rows)
        if active_intent["intent_id"] != intent["intent_id"]:
            raise SavedIntentError("publish finalization no longer owns this journal generation")
        # A newer suffix is retained verbatim. It remains recoverable from the
        # durable result; automatic snapshot reset/rebase would lose intent.
        anchors = [r for r in rows if any(isinstance(e, dict) and e.get("kind") == "saved-base-v1"
                                         for e in r.get("evidence", []))]
        generation_matches = anchors[-1]["id"] == intent["generation"] if checkpoint is None else (
            any(e.get("kind") == "publish-checkpoint-v1" and e.get("intent_id") == intent["intent_id"]
                for e in anchors[-1].get("evidence", [])))
        unsaved = checkout._disk is None or _serialize(checkout._annotated()) != checkout._disk
        exact_saved_bytes = hashlib.sha256(current.encode("utf-8")).hexdigest() == intent["saved_bytes_digest"]
        if unsaved or not generation_matches or (not exact_saved_bytes and current != text):
            result["local_state"] = _RETAINED
            return result
        # The durable result already contains the acknowledged exact bundle.
        # Replace first, then append the saved-base checkpoint under the same
        # draft lock. A checkpoint must never precede the file it anchors: a
        # crash could otherwise let a normal writer save old-base commands
        # after it. Recovery can finish either side from the stored result.
        journal.sync(log)
        _replace(path, text)
        if checkpoint is None:
            meta = dict(kind="publish-checkpoint-v1", intent_id=intent["intent_id"], after=after,
                        sequence=journal._last_record(log)["seq"] + 1)
            _append(log, checkout, (meta, dict(kind="saved-base-v1", document=document,
                                              publication_id=result.get("accepted_publication_id"))),
                    outcome="changed", before=intent["saved_draft_digest"], after=after,
                    reason=intent["body"].get("label"))
        journal.sync(log)
        checkout.bundle = copy.deepcopy(document)
        checkout._disk, checkout._journal, checkout._pending_calls = text, [], []
        checkout._refresh()
    result["local_state"] = "finalized"
    return result


def publish_saved(checkout, message="", *, idempotency_key=None, client=None, force=False, dry_run=False):
    canonical_path = checkout.path is not None and Path(checkout.path).resolve() == store.draft_path(
        checkout.bundle["project_id"], checkout.bundle["timeline_id"], Path(checkout.path).stem).resolve()
    if force or not canonical_path:
        # An already supplied Astrid read facade can identify an unsupported
        # current head before local argument errors obscure that refusal. Valid
        # saved requests retain their exact recovery protocol, with no new head
        # lookup before replaying a pending immutable envelope.
        if client is not None and callable(getattr(getattr(client, "timelines", None), "open_composition", None)):
            store.require_writable_head(checkout.document(), client=client)
        if force:
            raise TimelineEditError("COMMIT preserves command expectations; resolve overlapping edits before publishing")
        raise SavedIntentError("publish requires the exact saved canonical draft path")
    explicit = _key(idempotency_key)
    if client is None:
        from astrid.sdk import AstridClient
        with AstridClient.open_from_launcher(start_pack_host=False) as opened:
            return publish_saved(checkout, message, idempotency_key=idempotency_key,
                                 client=opened, force=force, dry_run=dry_run)
    workspace = _workspace(client)
    origin, session = _authenticate(workspace)  # current auth, including on retries/recovery
    with _locked(checkout) as (path, log, rows):
        active = _active(checkout, rows, path)
        if active and not dry_run:
            _origin(active[0], origin, explicit)
            journal.sync(log)
    if dry_run or not active:
        prepared = _prepare(checkout, workspace, origin, session, message, explicit, dry_run)
        preview = _preview(workspace, prepared)
        if dry_run:
            return preview
        with _locked(checkout) as (path, log, rows):
            active = _active(checkout, rows, path)
            if active:
                _origin(active[0], origin, explicit)
            else:
                calls = _read_saved_calls_locked(checkout, path, log)
                current = path.read_bytes().decode("utf-8")
                anchors = [r for r in rows if any(isinstance(e, dict) and e.get("kind") == "saved-base-v1"
                                                for e in r.get("evidence", []))]
                if (journal.digest(current) != prepared["saved_draft_digest"] or
                        anchors[-1]["id"] != prepared["generation"] or
                        list(calls) != prepared["body"]["change"]["commands"]):
                    raise SavedIntentError("saved draft changed during preparation; reload and publish again")
                _append(log, checkout, (prepared,), reason=prepared["body"].get("label"))  # fsync before send
                active = prepared, None, None
            journal.sync(log)
    intent, recorded, _checkpoint = active
    if recorded is not None:
        from astrid.sdk.timeline.duplicate_fidelity import _leaves
        if any(call["op"] == "timeline.duplicate" for call in _leaves(intent["body"]["change"]["commands"])):
            # Recovery may load a result recorded by an older producer. Verify
            # the duplicate's source/output closure before returning success.
            _conclusive, verified = _verify_result(workspace, intent, recorded["result"])
            if _digest(recorded["document"]) != _digest(verified):
                raise SavedIntentError("recorded duplicate acknowledgement differs from its exact persisted closure")
        return _finalize(checkout, intent, recorded)
    result = workspace.commit_timeline(copy.deepcopy(intent["body"]))
    conclusive, document = _verify_result(workspace, intent, result)
    if not conclusive:
        return result  # runtime_unavailable/ambiguous error retains uncertainty
    recorded = dict(kind="publish-result-v1", intent_id=intent["intent_id"], result=copy.deepcopy(result), document=document)
    with _locked(checkout) as (_path, log, rows):
        current_intent, existing, _cp = _state(rows)
        if current_intent["intent_id"] != intent["intent_id"]:
            raise SavedIntentError("COMMIT result no longer owns the prepared envelope")
        if existing is None:
            _append(log, checkout, (recorded,), outcome="custody" if document is not None else "refused")
        elif existing != recorded:
            raise TimelineEditError("Runtime returned different results for one immutable request")
        journal.sync(log)
    return _finalize(checkout, intent, recorded) if document is not None else result
