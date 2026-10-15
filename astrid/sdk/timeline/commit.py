"""Astrid's fixed in-process semantic provider for Runtime's commit port.

Runtime alone authenticates and accepts. This module only edits detached
checkouts; it never opens a client, publishes, or persists a local draft.
Exact pinned child bytes come only from the request-bound revision closure.
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
from astrid.sdk.timeline import commands
from astrid.sdk.timeline.rebase import rebase
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
                if "commands" not in request["change"]:
                    raise _Refusal("Document reconciliation and verified draft journal commands require the adapt-save producer")
                base = self._load_revision(runtime_port, request, request["base_revision_id"])
                occurrence = self._scope_occurrence(request, base)
                candidate, plan, _direct = self._apply(base, request["change"]["commands"])
                self._guard_scope(occurrence, plan)
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
                        rebased = rebase(plan.calls, onto)
                        if rebased.conflicts:
                            return self._conflict(result, request, rebased.conflicts, base, onto, candidate)
                        current, current_plan = rebased.document, rebased.plan
                        try:
                            self._guard_scope(occurrence, current_plan)
                        except _Refusal as exc:
                            return self._conflict(result, request, [str(exc)], base, onto, candidate, kind="dependent")
                        result["id_map"] = rebased.id_map
                    else:
                        current_plan = plan
                    before = Checkout(copy.deepcopy(onto)).document()
                    after = Checkout(current).document()
                    compiled = compile_authoring_candidate(after).publication
                    before_compiled = compile_authoring_candidate(before).publication
                    changed = any(compiled[k] != before_compiled[k] for k in (
                        "parent_composition", "shot_revisions", "internal_timeline_revisions"))
                    result.update(canonical_document=after, document_digest=_digest(after))
                    # These are recorder output, not submitted command JSON.
                    result["change"].update(changes=list(current_plan.changes),
                                            commands=[c.to_json() for c in current_plan.calls],
                                            direct_writes=copy.deepcopy(list(current_plan.direct_writes)),
                                            consequences=copy.deepcopy(list(current_plan.consequences)))
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
            if not callable(getattr(port, "revision_closure", None)):
                raise _Refusal("Exact pinned revision_closure capability is required")
            closure = port.revision_closure(revision_id)
            if not isinstance(closure, Mapping) or set(closure) != {
                    "parent_revision", "shot_revisions", "internal_timeline_revisions"}:
                raise _Refusal("Invalid exact revision closure shape")
            revision = closure["parent_revision"]
        except Exception as exc:
            # Preserve typed Runtime authorization/transport errors. Only a
            # typed missing base is converted to the public base_not_found code.
            if getattr(exc, "code", None) == "not_found" and revision_id == request["base_revision_id"]:
                raise _BaseNotFound(str(exc)) from exc
            raise
        if not isinstance(revision, Mapping) or not isinstance(revision.get("payload"), Mapping):
            raise _Refusal("Exact parent revision resource is missing its payload")
        if (revision.get("revision_id"), revision.get("project_id"), revision.get("timeline_id")) != (
                revision_id, request["project_id"], request["timeline_id"]):
            raise _Refusal("Revision identity does not match the request-bound exact read")
        require_writable_parent(revision["payload"])
        shots, internals = closure["shot_revisions"], closure["internal_timeline_revisions"]
        occurrences = revision["payload"].get("occurrences", [])
        if (not isinstance(occurrences, list) or not isinstance(shots, list) or not isinstance(internals, list)
                or any(not isinstance(row, Mapping) for row in occurrences + shots + internals)):
            raise _Refusal("Pinned closure children and occurrences must be detached object arrays")
        pins = {(row.get("shot_id"), row.get("shot_revision_id")) for row in occurrences}
        for row in shots:
            if (row.get("project_id") != request["project_id"] or not isinstance(row.get("payload"), Mapping)
                    or row["payload"].get("internal_timeline_revision_id") != row.get("internal_timeline_revision_id")):
                raise _Refusal("Shot closure scope or exact internal revision pin mismatch")
        if any(row.get("project_id") != request["project_id"] for row in internals):
            raise _Refusal("Internal closure project scope mismatch")
        if {(row.get("shot_id"), row.get("revision_id")) for row in shots} != pins or len(shots) != len(pins):
            raise _Refusal("Shot closure differs from exact parent occurrence pins")
        internal_pins = {row.get("internal_timeline_revision_id") for row in shots}
        if {row.get("revision_id") for row in internals} != internal_pins or len(internals) != len(internal_pins):
            raise _Refusal("Internal closure differs from exact pinned shot dependencies")
        return open_authoring_bundle(revision, shot_revisions=shots, internal_timeline_revisions=internals)

    @staticmethod
    def _scope_occurrence(request, base):
        scope = request["scope"]
        target = scope["target"]
        if target == "parent":
            return None
        if not isinstance(target, str) or not target.startswith("occurrence:"):
            raise _Refusal("Unsupported authoring scope")
        if scope.get("content", "this-occurrence") != "this-occurrence":
            raise _Refusal("Shared-shot editing requires an explicit admitted identity producer")
        occurrence = target.split(":", 1)[1]
        rows = [row for row in base.get("placements", []) if row.get("occurrence_id") == occurrence]
        if len(rows) != 1:
            raise _Refusal(f"Unavailable or ambiguous occurrence {occurrence!r}")
        from astrid.sdk import timeline_intent as intent
        tl = Checkout(copy.deepcopy(base))
        selected_shot = rows[0]["shot_id"]
        def anchors(clips):
            return {(kind, value) for clip in clips for kind, value in (
                ("cut", intent.cut_of(clip.data)), ("line", intent.line(clip.data))) if value}
        own = anchors(c for c in tl.clips() if c.shot_id == selected_shot)
        other = anchors(c for c in tl.clips() if c.shot_id != selected_shot)
        if own & other:
            raise _Refusal("Ambiguous occurrence timing identities: " +
                           ", ".join(f"{kind}:{value}" for kind, value in sorted(own & other)) +
                           "; exact occurrence timing support is required")
        source = base.get("source_mapping", {}).get("shots", {}).get(rows[0]["shot_id"], {})
        for key, field in (("shot_id", "shot_id"), ("shot_revision_id", "revision_id"),
                           ("internal_revision_id", "internal_timeline_revision_id")):
            if key in scope and scope[key] != source.get(field):
                raise _Refusal(f"Occurrence {occurrence!r} exact base {key} mismatch")
        return occurrence

    @staticmethod
    def _guard_scope(occurrence, plan):
        if occurrence is None:
            return
        prefix = f"occurrence:{occurrence}/"
        for call in plan.calls:
            if not call.resolved or any(not sid.startswith(prefix) for sid in call.resolved):
                raise _Refusal("Occurrence scope requires exact targets within that occurrence")
        for write in (*plan.direct_writes, *plan.consequences):
            if not write["id"].startswith(prefix):
                raise _Refusal(f"Occurrence edit affects unsupported outside scope {write['id']!r}")

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
            clip = commands.recorded_owner(tl, call, "clip") if call.resolved else tl.clip(call.select)
            entry = tl._registry(clip.shot_id).get(str(clip.data.get("asset"))) or {}
            duration = entry.get("duration")
            if not isinstance(duration, (int, float)) or isinstance(duration, bool) or duration <= 0:
                raise _Refusal("Trim/slip requires a pinned source duration; ambient media lookup is unavailable")

    @staticmethod
    def _error(result, code, message, *, status="error"):
        result.update(accepted_revision_id=None, accepted_publication_id=None, previous_revision_id=None,
                      runtime_receipt=None, provenance=None, evidence=None)
        result.update(status=status, error=dict(code=code, message=message,
                      hint="No durable acceptance was reported", next_action="Resolve the named dependency or retry the original request unchanged"))
        return result

    @staticmethod
    def _conflict(result, request, paths, base, head, candidate, *, kind="same-property"):
        result.update(status="conflict", error=None, accepted_revision_id=None, accepted_publication_id=None,
                      previous_revision_id=None, runtime_receipt=None, provenance=None, evidence=None,
                      conflicts=[copy.deepcopy(path) if isinstance(path, Mapping) else dict(
                          id=request["timeline_id"], path=path, base=None, theirs=None, ours=None, kind=kind,
                      ) for path in paths])
        return result


class _BaseNotFound(AuthoringBundleError):
    pass


def create_runtime_commit_service():
    """Construct the one fixed provider; no discovery, credentials or startup I/O."""
    return RuntimeCommitService()
