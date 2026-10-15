"""Astrid's fixed in-process semantic provider for Runtime's commit port.

Runtime alone authenticates and accepts. This module only edits detached
checkouts; it never opens a client, publishes, or persists a local draft.
The v1 port currently lacks pinned child reads. Shot-bearing revisions are
therefore an explicit refusal, rather than a fabricated authoring closure.
"""
from __future__ import annotations

import copy
import hashlib
import json
import threading
from collections.abc import Mapping

from astrid.core.timeline.authoring_bundle import (
    AuthoringBundleError,
    compile_authoring_candidate,
    open_authoring_bundle,
)
from astrid.core.timeline.compatibility import (
    TimelineCompatibilityError,
    require_writable_candidate,
    require_writable_parent,
)
from astrid.sdk.timeline import commands, store
from astrid.sdk.timeline._common import TimelineEditError
from astrid.sdk.timeline.checkout import Checkout
from astrid.sdk.timeline.commands import CommandError
from astrid.sdk.timeline.transaction import edit_transaction
from astrid.version import __version__
from banodoco_workspace_client.contract_metadata import SCHEMA_DIGEST
from banodoco_workspace_client.generated import CommitResult


def _digest(value):
    # CONTRACT-COMMIT hashes the complete original body, including dry_run.
    return "sha256:" + hashlib.sha256(json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        allow_nan=False,
    ).encode()).hexdigest()


def _result(request, request_hash):
    return dict(status="error", request_id=request["request_id"], request_hash=request_hash,
                base_revision_id=request["base_revision_id"], accepted_revision_id=None,
                accepted_publication_id=None, previous_revision_id=None, rebased_onto=None,
                canonical_document=None, projection_version=1, document_digest=None, id_map={},
                change=dict(changes=[], commands=[], direct_writes=[], consequences=[], affected=[]),
                conflicts=[], choices=[], provenance=None, evidence=None, schema_report=None,
                runtime_receipt=None, error=None)


class _Refusal(ValueError):
    pass


# Only the registry lookup/creation holds this mutex. All Runtime I/O and
# compilation is under the individual timeline lock, shared by factory instances.
_registry_lock = threading.Lock()
_timeline_locks = {}


def _timeline_lock(project, timeline):
    with _registry_lock:
        return _timeline_locks.setdefault((project, timeline), threading.Lock())


class RuntimeCommitService:
    provider_id = "astrid-v1"
    interface_version = 1
    contract_digest = SCHEMA_DIGEST

    def commit(self, request_snapshot, runtime_port) -> CommitResult:
        request = copy.deepcopy(request_snapshot)
        request_hash = _digest(request)
        result = _result(request, request_hash)
        with _timeline_lock(request["project_id"], request["timeline_id"]):
            # A dry run never replays a durable acceptance or reserves an ID.
            if not request["dry_run"]:
                saved = runtime_port.lookup_request()
                if saved is not None:
                    if saved["request_hash"] == request_hash:
                        return copy.deepcopy(saved["result"])
                    return self._error(result, "request_id_conflict", "RequestIdConflict: request ID has different content")
            try:
                if request["scope"]["target"] != "parent":
                    raise _Refusal("Occurrence identity/fork semantics require the IDENTITY producer")
                if "commands" not in request["change"]:
                    raise _Refusal("Document reconciliation and verified draft journal commands require the adapt-save producer")
                base = self._load_revision(runtime_port, request, request["base_revision_id"])
                candidate, plan, direct = self._apply(base, request["change"]["commands"])
                # Apply once at the exact base. Never repeat relative commands
                # on a newer head or generate new IDs during a CAS retry.
                for attempt in range(3):  # initial acceptance + at most two StaleHead retries
                    head = runtime_port.head()
                    result["rebased_onto"] = None
                    onto = base if head["revision_id"] == request["base_revision_id"] else self._load_revision(
                        runtime_port, request, head["revision_id"])
                    require_writable_candidate(onto)
                    current = copy.deepcopy(candidate)
                    if head["revision_id"] != request["base_revision_id"]:
                        result["rebased_onto"] = head["revision_id"]
                        current, conflicts = store.three_way(base, onto, candidate, force=False)
                        if conflicts:
                            return self._conflict(result, request, conflicts, base, onto, candidate,
                                                  kind=self._merge_conflict_kind(base, onto, candidate, direct))
                        try:
                            self._validate_merged(current, direct)
                        except (CommandError, AuthoringBundleError, TimelineEditError) as exc:
                            return self._conflict(result, request, [str(exc)], base, onto, candidate, kind="dependent")
                    before = Checkout(copy.deepcopy(onto)).document()
                    after = Checkout(current).document()
                    compiled = compile_authoring_candidate(after).publication
                    before_compiled = compile_authoring_candidate(before).publication
                    changed = any(compiled[k] != before_compiled[k] for k in (
                        "parent_composition", "shot_revisions", "internal_timeline_revisions"))
                    result.update(canonical_document=after, document_digest=_digest(after))
                    # These are recorder output, not submitted command JSON.
                    result["change"].update(changes=list(plan.changes),
                                            commands=[c.to_json() for c in plan.calls],
                                            direct_writes=self._wire_writes(base, after, direct))
                    if request["dry_run"]:
                        result["status"] = "preview"
                        return result
                    result["status"] = "accepted" if changed else "no_change"
                    prepared = dict(project_id=request["project_id"], timeline_id=request["timeline_id"],
                                    request_id=request["request_id"], request_hash=request_hash,
                                    expected_head_revision_id=head["revision_id"],
                                    publication={k: compiled[k] for k in (
                                        "parent_composition", "shot_revisions", "internal_timeline_revisions"
                                    )} if changed else None,
                                    result=copy.deepcopy(result), source=copy.deepcopy(request["source"]),
                                    evidence=dict(commands=copy.deepcopy(result["change"]["commands"]),
                                                  direct_writes=copy.deepcopy(result["change"]["direct_writes"]),
                                                  digests=dict(before=_digest(before), after=_digest(after)),
                                                  toolchain=dict(astrid_sha="unavailable",
                                                                 compiler_version=f"Checkout.resolve/{__version__}")))
                    for key in ("asserted_actor", "label", "reason"):
                        if key in request:
                            prepared[key] = copy.deepcopy(request[key])
                    try:
                        # Return exactly Runtime's durable result; never fill receipts locally.
                        return runtime_port.accept(prepared)["stored_result"]
                    except (ConnectionError, TimeoutError):
                        raise  # unknown acceptance outcome: retry the identical request
                    except Exception as exc:
                        details = getattr(exc, "details", None)
                        if getattr(exc, "code", None) == "unavailable":
                            return self._error(result, "runtime_unavailable", str(exc))
                        if (getattr(exc, "code", None) != "conflict" or not isinstance(details, Mapping)
                                or details.get("code") != "StaleHead"):
                            raise
                        if attempt == 2:
                            return self._conflict(result, request, ["StaleHead retry bound exhausted"],
                                                  base, onto, candidate, kind="dependent")
            except TimelineCompatibilityError as exc:
                return self._error(result, "unsupported_feature", str(exc))
            except _BaseNotFound as exc:
                return self._error(result, "base_not_found", str(exc))
            except _Refusal as exc:
                return self._error(result, "bad_request", str(exc), status="refused")
            except (CommandError, AuthoringBundleError, TimelineEditError) as exc:
                return self._error(result, "bad_request", str(exc), status="refused")
            except (ConnectionError, TimeoutError):
                raise

    @staticmethod
    def _load_revision(port, request, revision_id):
        try:
            revision = port.revision(revision_id)
        except Exception as exc:
            # Preserve typed Runtime authorization/transport errors. Only a
            # typed missing base is converted to the public base_not_found code.
            if getattr(exc, "code", None) == "not_found" and revision_id == request["base_revision_id"]:
                raise _BaseNotFound(str(exc)) from exc
            raise
        if (revision["revision_id"], revision["project_id"], revision["timeline_id"]) != (
                revision_id, request["project_id"], request["timeline_id"]):
            raise _Refusal("Revision identity does not match the request-bound exact read")
        require_writable_parent(revision["payload"])
        if revision["payload"].get("occurrences"):
            raise _Refusal("Pinned shot/internal-timeline closure is unavailable on RuntimeCommitPort v1; exact child reads are required")
        return open_authoring_bundle(revision, shot_revisions=[], internal_timeline_revisions=[])

    @staticmethod
    def _check_calls(values):
        # Media acquisition/file adapters need a separately admitted scoped
        # producer. They must never obtain ambient clients or paths here.
        if not isinstance(values, (list, tuple)):
            raise CommandError("commands must be an array")
        external = {"timeline.add", "timeline.apply-script", "timeline.insert-line", "voice.replace", "timeline.adopt",
                    "timeline.fill-slot", "timeline.fill-standins", "timeline.replace-asset",
                    "clip.swap-asset", "timeline.edit-flags", "cut.split", "timeline.add-cut"}
        for value in values:
            call = commands.EditCommand.coerce(value)
            if call.op in external or "client" in call.args:
                raise _Refusal(f"{call.op} requires an admitted scoped media/adapter producer")
            if call.op == "clip.set-beats" and not (isinstance(call.args.get("source"), Mapping)
                    or call.args.get("source") == "keep"):
                raise _Refusal("Beat files/handles require an admitted scoped media producer")
            if call.op == "sequence.fit" and call.args.get("shape_from") is not None:
                raise _Refusal("shape_from requires an admitted scoped exact-timeline read")
            if call.op == "timeline.batch":
                RuntimeCommitService._check_calls(call.args.get("commands", []))

    @classmethod
    def _apply(cls, bundle, calls):
        require_writable_candidate(bundle)
        cls._check_calls(calls)
        tl = Checkout(copy.deepcopy(bundle))
        cls._check_detached_media(tl)
        with edit_transaction(tl, "Runtime commit", quiet=True, persist=False) as tx:
            tx.canonical = True
            for call in calls:
                cls._check_media_duration(tl, call)
                commands.execute(tl, call)
            # Inventory is collected after actual calls and before the compiler.
            direct = commands.direct_writes(tl, tx.calls)
        if tx.plan.blocking or (tx.report is not None and not tx.report.valid):
            raise _Refusal("; ".join(tx.plan.blocking or tx.report.problems or ["Invalid candidate"]))
        return tl.document(), tx.plan, direct

    @staticmethod
    def _check_detached_media(tl):
        for shot_id in tl.bundle.get("shots", {}):
            if any(isinstance(entry, Mapping) and any(isinstance(entry.get(k), str)
                   for k in ("local_path", "path")) for entry in tl._registry(shot_id).values()):
                raise _Refusal("Local media paths require an admitted detached inspection producer")

    @staticmethod
    def _check_media_duration(tl, value):
        call = commands.EditCommand.coerce(value)
        if call.op == "timeline.batch":
            for child in call.args.get("commands", []):
                RuntimeCommitService._check_media_duration(tl, child)
        elif call.op in {"clip.trim", "clip.slip"}:
            clip = tl.clip(call.select)
            entry = tl._registry(clip.shot_id).get(str(clip.data.get("asset"))) or {}
            duration = entry.get("duration")
            if not isinstance(duration, (int, float)) or isinstance(duration, bool) or duration <= 0:
                raise _Refusal("Trim/slip requires a pinned source duration; ambient media lookup is unavailable")

    @staticmethod
    def _validate_merged(bundle, direct):
        tl = Checkout(bundle)
        RuntimeCommitService._check_detached_media(tl)
        require_writable_candidate(tl.document())
        # Resolve derived consequences once on the merged intent, without
        # replaying a relative command or recording compiler writes as authored.
        tl._command_depth = 1
        tl._edit_local_only = True
        try:
            tl.resolve()
            commands.guard_direct_writes(tl, direct)
            report = tl.check()
            commands.guard_direct_writes(tl, direct)
        finally:
            tl._command_depth = 0
        if not report.valid or report.blocking:
            raise _Refusal("; ".join(report.blocking or report.problems or ["Invalid merged candidate"]))

    @staticmethod
    def _wire_writes(base, after, direct):
        old = {c.id: c for c in Checkout(copy.deepcopy(base)).clips()}
        new = {c.id: c for c in Checkout(copy.deepcopy(after)).clips()}
        writes = {}
        for clip_id, path in sorted(direct):
            if clip_id not in old or clip_id not in new:
                raise _Refusal("Created/deleted direct-write identity requires the IDENTITY producer")
            before_exists, before = commands._path_state(old[clip_id].data, path)
            after_exists, value = commands._path_state(new[clip_id].data, path)
            if not before_exists or not after_exists:
                # The current result has no ABSENT codec. Describe the actual
                # containing JSON object instead: omission stays omission and
                # explicit null stays null, with no synthetic sentinel.
                path = "params"
                before_exists, before = commands._path_state(old[clip_id].data, path)
                after_exists, value = commands._path_state(new[clip_id].data, path)
                if not before_exists or not after_exists:
                    raise _Refusal("Absent params container requires the CONTRACT-COMMAND ABSENT producer")
            writes[(clip_id, path)] = dict(id=clip_id, path=path, before=before, after=value)
        return [write for (cid, path), write in writes.items()
                if path == "params" or (cid, "params") not in writes]

    @staticmethod
    def _merge_conflict_kind(base, head, candidate, direct):
        # Classify the shared helper's conservative unit conflict from actual
        # direct-write facts. Different properties of one clip are dependent,
        # not a claim that their literal property paths overlapped.
        old, theirs, ours = [{c.id: c for c in Checkout(copy.deepcopy(doc)).clips()}
                             for doc in (base, head, candidate)]
        for clip_id, path in direct:
            if clip_id in old and clip_id not in theirs:
                return "delete-vs-edit"
            if clip_id in old and clip_id in theirs and clip_id in ours:
                b, h, c = [commands._path_state(table[clip_id].data, path)
                           for table in (old, theirs, ours)]
                if b != h and c != h:
                    return "same-property"
        return "dependent"

    @staticmethod
    def _error(result, code, message, *, status="error"):
        result.update(status=status, error=dict(code=code, message=message,
                      hint="No durable acceptance was reported", next_action="Resolve the named dependency or retry the original request unchanged"))
        return result

    @staticmethod
    def _conflict(result, request, paths, base, head, candidate, *, kind="same-property"):
        result.update(status="conflict", error=None, conflicts=[dict(
            id=request["timeline_id"], path=path, base=copy.deepcopy(base),
            theirs=copy.deepcopy(head), ours=copy.deepcopy(candidate), kind=kind,
        ) for path in paths])
        return result


class _BaseNotFound(AuthoringBundleError):
    pass


def create_runtime_commit_service():
    """Construct the one fixed provider; no discovery, credentials or startup I/O."""
    return RuntimeCommitService()
