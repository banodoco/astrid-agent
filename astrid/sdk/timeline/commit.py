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
from astrid.sdk.timeline._common import TimelineEditError
from astrid.sdk.timeline.checkout import Checkout
from astrid.sdk.timeline.commands import CommandError
from astrid.sdk.timeline.rebase import rebase
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
                from astrid.sdk.timeline.narration import capture_takes, candidate_narration
                self._check_calls(request["change"]["commands"])
                takes = capture_takes(runtime_port, base, request["change"]["commands"])
                from astrid.sdk.timeline.source_hydration import hydrate_sources
                imports, pinned_shots = hydrate_sources(runtime_port, request, base, request["change"]["commands"])
                captured = self._capture_media(runtime_port, request, request["change"]["commands"])
                apply_context = {"narration_takes": takes} if takes else {}
                candidate, plan, _direct = self._apply(base, captured, **apply_context)
                self._guard_scope(occurrence, plan)
                # Apply once at the exact base. Never repeat relative commands
                # on a newer head or generate new IDs during a CAS retry.
                for attempt in range(3):  # initial acceptance + at most two StaleHead retries
                    head = runtime_port.head()
                    result["rebased_onto"] = None
                    onto = base if head["revision_id"] == request["base_revision_id"] else self._load_revision(
                        runtime_port, request, head["revision_id"])
                    if imports:
                        onto["source_mapping"]["imports"] = copy.deepcopy(imports)
                    if pinned_shots:
                        onto["source_mapping"]["pinned_shots"] = copy.deepcopy(pinned_shots)
                    require_writable_candidate(onto)
                    if head["revision_id"] != request["base_revision_id"]:
                        result["rebased_onto"] = head["revision_id"]
                    inverse_conflict = self._inverse_history(runtime_port, request, plan.calls, head, onto, result)
                    if inverse_conflict is not None and inverse_conflict["conflicts"][0]["kind"] != "dependent":
                        return inverse_conflict
                    current = copy.deepcopy(candidate)
                    if head["revision_id"] != request["base_revision_id"]:
                        result["rebased_onto"] = head["revision_id"]
                        # Reuse the immutable prepared take; rebase never synthesizes.
                        rebased = rebase(plan.calls, onto, narration_takes=takes)
                        if rebased.conflicts:
                            conflict = self._conflict(result, request, rebased.conflicts, base, onto, candidate)
                            if any(call.op == "timeline.inverse" for call in plan.calls):
                                self._history_choice(conflict, head["publication_id"], None)
                            return conflict
                        current, current_plan = rebased.document, rebased.plan
                        try:
                            self._guard_scope(occurrence, current_plan)
                        except _Refusal as exc:
                            return self._conflict(result, request, [str(exc)], base, onto, candidate, kind="dependent")
                        result["id_map"] = rebased.id_map
                    else:
                        current_plan = plan
                    if inverse_conflict is not None:
                        return inverse_conflict
                    before = Checkout(copy.deepcopy(onto)).document()
                    after = Checkout(current).document()
                    from astrid.sdk.timeline.check import _require_complete_evaluation
                    _require_complete_evaluation(Checkout(after))
                    compilation = compile_authoring_candidate(after)
                    compiled = compilation.publication
                    for call in current_plan.calls:
                        if call.op == "occurrence.add":
                            oid = call.args["identities"]["occurrence_id"]
                            mapped = compilation.identity_mapping["placements"][oid]
                            prefix = "occurrence:" + oid
                            result["id_map"].update({sid: sid for sid in call.created})
                            result["id_map"].update({prefix: prefix, prefix + "/shot": mapped["shot_id"],
                                                     prefix + "::shot_revision_id": mapped["shot_revision_id"]})
                            shot = next(v for v in compilation.identity_mapping["shots"].values() if v["shot_id"] == mapped["shot_id"] and v["revision_id"] == mapped["shot_revision_id"])
                            result["id_map"][prefix + "::internal_revision_id"] = shot["internal_timeline_revision_id"]
                    from astrid.sdk.timeline.shot_groups import identity, groups
                    group_results = []
                    states = commands.entity_states(Checkout(current))
                    for index, call in enumerate(current_plan.calls):
                        if call.op not in {"shot-group.set", "shot-group.remove"}:
                            continue
                        old = identity(call.args["group"])
                        resulting = [sid for key, kind in call.authorship.items()
                                     for sid, path in [commands.write_target(key, call.resolved)]
                                     if kind == "direct" and sid.startswith("parent/shot-group:") and sid in states]
                        selected_group = states[resulting[0]] if resulting else None
                        members = selected_group.get("memberSelectors", []) if selected_group else []
                        intent = call.args.get("intent")
                        created = [sid for sid in call.created if "/clip:" in sid]
                        restored = members if intent == "images" else []
                        reused = [sid for sid in members if sid not in created] if intent != "images" else []
                        group_results.append(dict(operation_index=index,
                            group={k: selected_group[k] for k in ("shotId", "trackId")} if selected_group else None,
                            created=created if intent != "images" else [], reused=reused, restored=restored))
                        result["id_map"].update({sid: sid for sid in (*created, *members)})
                        if resulting:
                            result["id_map"][old] = resulting[0]
                    if group_results:
                        result["shot_groups"] = group_results
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
                    if takes or any(c.op == "timeline.apply-script" for c in current_plan.calls):
                        prepared["narration"] = candidate_narration(after, compiled, takes)
                    try:
                        # Return exactly Runtime's durable result; never fill receipts locally.
                        return runtime_port.accept(prepared)["stored_result"]
                    except (ConnectionError, TimeoutError):
                        raise  # unknown acceptance outcome: retry the identical request
                    except Exception as exc:
                        details = getattr(exc, "details", None)
                        if getattr(exc, "code", None) == "unavailable":
                            return self._error(result, "runtime_unavailable", str(exc))
                        if (any(call.op.startswith("shot-group.") for call in current_plan.calls)
                                and getattr(exc, "code", None) == "conflict" and (
                                not isinstance(details, Mapping) or details.get("code") != "StaleHead")):
                            return self._conflict(result, request, [str(exc)], base, onto, candidate, kind="dependent")
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

    @classmethod
    def _capture_media(cls, port, request, values):
        """Detach exact project selections once, before base application/rebase.

        Runtime independently checks the original request selections at atomic
        acceptance. Recorded input is evidence, never a fresh catalog selector.
        """
        if not isinstance(values, (list, tuple)):
            raise CommandError("commands must be an array")
        captured = []
        for value in values:
            call = commands.EditCommand.coerce(value)
            if "media_record" in call.args:
                raise _Refusal("Submitted commands cannot supply trusted media_record")
            args = copy.deepcopy(dict(call.args))
            if call.op == "timeline.batch":
                args["commands"] = cls._capture_media(port, request, args.get("commands", []))
            if call.op in {"timeline.add", "shot-group.set"} and "media" in args:
                media = args["media"]
                if not isinstance(media, Mapping) or set(media) != {"asset", "generation_id", "variant_id", "object_id", "catalog_digest"}:
                    raise _Refusal("Exact project media selection is required")
                import re
                if not isinstance(media.get("object_id"), str) or re.fullmatch(r"sha256:[0-9a-f]{64}", media["object_id"]) is None:
                    raise _Refusal("Selected media requires an exact object digest")
                selection = copy.deepcopy(dict(media))
                if any(not isinstance(v, str) or not v for v in media.values()):
                    raise _Refusal("Media identity fields must be nonempty strings")
                read = getattr(port, "selected_media", None)
                if not callable(read):
                    raise _Refusal("Request-bound selected_media capability is required")
                try:
                    selected = copy.deepcopy(read(selection))
                except Exception as exc:
                    if call.op == "shot-group.set" and getattr(exc, "code", None) in {"not_found", "validation_error", "conflict"}:
                        raise _Refusal("Selected project media is unavailable or changed: " + str(exc)) from exc
                    raise
                fields = {"realm_id", "project_id", "asset", "generation_id", "variant_id", "object_id",
                          "generation", "variant", "entry", "catalog_digest"}
                if not isinstance(selected, Mapping) or set(selected) != fields:
                    raise _Refusal("Invalid detached selected_media result")
                if any(selected.get(k) != v for k, v in selection.items()) or selected.get("project_id") != request["project_id"]:
                    raise _Refusal("Selected media identity/project membership mismatch")
                if not isinstance(selected["realm_id"], str) or not selected["realm_id"]:
                    raise _Refusal("Selected media realm binding is missing")
                record = {k: v for k, v in selected.items() if k != "catalog_digest"}
                if _digest(record) != media["catalog_digest"]:
                    raise _Refusal("Selected media catalog digest changed")
                if any(not isinstance(selected[k], Mapping) for k in ("generation", "variant", "entry")):
                    raise _Refusal("Selected media metadata must be detached objects")
                if selected["entry"].get("object_id") != media["object_id"]:
                    raise _Refusal("Selected entry object identity mismatch")
                args["media_record"] = selected
            captured.append({**call.to_json(), "args": args})
        return captured

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
        bundle = open_authoring_bundle(revision, shot_revisions=shots, internal_timeline_revisions=internals)
        # Independent authoring identities isolate edits; untouched occurrences
        # still point at their exact immutable source revisions at acceptance.
        for source in bundle["source_mapping"]["shots"].values():
            source["linked"] = True
        return bundle

    @staticmethod
    def _load_duplicate_source(port, request, ref):
        from astrid.sdk.timeline.graph_commands import duplicate_source_ref
        ref = duplicate_source_ref(ref)
        if ref["timeline_id"] == request["timeline_id"]:
            raise _Refusal("duplicate source must be a distinct timeline")
        # An explicit source branch: _load_revision's destination scope stays
        # strict. The port authorizes this exact selector and publication.
        class ExactSource:
            def revision_closure(self, rid):
                if rid != ref["parent_revision_id"]:
                    raise _Refusal("duplicate source revision mismatch")
                return port.revision_closure(copy.deepcopy(ref))
        scoped = dict(request, timeline_id=ref["timeline_id"])
        try:
            bundle = RuntimeCommitService._load_revision(ExactSource(), scoped, ref["parent_revision_id"])
        except Exception as exc:
            if getattr(exc, "code", None) in {"conflict", "not_found"}:
                raise _Refusal(f"duplicate source dependency unavailable: {exc}") from exc
            raise
        bundle["source_mapping"]["duplicate_source"] = copy.deepcopy(ref)
        return bundle

    @staticmethod
    def _load_shot_source(port, request, ref):
        from astrid.core.timeline.authoring_bundle import _verify_digest
        closure = port.revision_closure(copy.deepcopy(ref))
        if not isinstance(closure, Mapping) or set(closure) != {"shot_revision", "internal_timeline_revision"}:
            raise _Refusal("Invalid request-bound exact shot closure")
        shot, internal = closure["shot_revision"], closure["internal_timeline_revision"]
        if (not isinstance(shot, Mapping) or not isinstance(internal, Mapping)
                or (shot.get("project_id"), shot.get("shot_id"), shot.get("revision_id"), shot.get("internal_timeline_revision_id")) !=
                (request["project_id"], ref["shot_id"], ref["shot_revision_id"], ref["internal_revision_id"])
                or (internal.get("project_id"), internal.get("revision_id")) != (request["project_id"], ref["internal_revision_id"])
                or not isinstance(shot.get("payload"), Mapping) or not isinstance(internal.get("payload"), Mapping)
                or shot["payload"].get("internal_timeline_revision_id") != ref["internal_revision_id"]
                or not isinstance(internal.get("timeline_id"), str)):
            raise _Refusal("Exact shot closure scope or internal pins mismatch")
        _verify_digest(shot["payload"], shot.get("content_digest"), "shot.content_digest")
        _verify_digest(internal["payload"], internal.get("content_digest"), "internal.content_digest")
        return dict(project_id=request["project_id"], timeline_id=request["timeline_id"], selection=copy.deepcopy(ref), shot=dict(shot_id=ref["shot_id"], base_payload=copy.deepcopy(shot["payload"]), payload=copy.deepcopy(shot["payload"]),
                              base_internal_timeline=copy.deepcopy(internal["payload"]), internal_timeline=copy.deepcopy(internal["payload"])),
                    source=dict(shot_id=ref["shot_id"], revision_id=ref["shot_revision_id"], content_digest=shot["content_digest"],
                                internal_timeline_id=internal["timeline_id"], internal_timeline_revision_id=ref["internal_revision_id"],
                                internal_timeline_content_digest=internal["content_digest"], item_id_map={}, linked=True),
                    placement=dict(placement=dict(start_ms=0), duration_ms=1, track=None, source_offset=0, speed=1,
                                   transform={}, gain=1, muted=False, provenance={}))

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
                ("cut", intent.cut_of(clip.data)),
                ("line", None if intent.line_take(clip.data) else intent.line(clip.data))) if value}
        own = anchors(c for c in tl.clips() if c.shot_id == selected_shot)
        other = anchors(c for c in tl.clips() if c.shot_id != selected_shot)
        def payload_only(values):
            return bool(values) and all(value.get("op") == "shot.set" or
                (value.get("op") == "timeline.batch" and payload_only(value.get("args", {}).get("commands", [])))
                for value in values)
        if own & other and not payload_only(request["change"].get("commands", [])):
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
            # The selected placement's revision link is a compiler consequence
            # of this-occurrence materialization, never a parent-scope mutation.
            link = (write["id"] == prefix[:-1] and write["path"] in {"shot_id", "shot_revision_id"}
                    and write not in plan.direct_writes)
            if not write["id"].startswith(prefix) and not link:
                raise _Refusal(f"Occurrence edit affects unsupported outside scope {write['id']!r}")

    @staticmethod
    def _check_calls(values):
        # Media acquisition/file adapters need a separately admitted scoped
        # producer. They must never obtain ambient clients or paths here.
        if not isinstance(values, (list, tuple)):
            raise CommandError("commands must be an array")
        external = {"timeline.insert-line", "voice.replace", "timeline.adopt",
                    "timeline.fill-slot", "timeline.fill-standins", "timeline.replace-asset",
                    "clip.swap-asset", "timeline.edit-flags"}
        for value in values:
            call = commands.EditCommand.coerce(value)
            if call.op == "timeline.operation":
                raise _Refusal("Local operations must publish their expanded guarded commands, never contribution code")
            if call.op == "timeline.apply-script":
                from astrid.sdk.timeline.narration import validate_spec
                if set(call.args) - {"spec", "gaps", "takes", "music"} or call.args.get("takes") is not None:
                    raise _Refusal("apply-script requires prepared managed refs, never paths or clients")
                validate_spec(call.args.get("spec"))
            local_adoption = (call.op == "timeline.adopt" and call.args.get("sources") is not None
                              and call.args.get("timeline") is None and call.args.get("revision_id") is None)
            if (call.op in external and not local_adoption) or "client" in call.args:
                raise _Refusal(f"{call.op} requires an admitted scoped media/adapter producer")
            if call.op == "timeline.add" and any(call.args.get(key) is not None for key in ("asset", "standin")):
                raise _Refusal("timeline.add with media requires an admitted scoped media/adapter producer")
            if call.op in {"cut.split", "timeline.add-cut"} and call.args.get("picture") is not None and not isinstance(call.args["picture"], str):
                raise _Refusal(f"{call.op} with picture replacement requires an admitted scoped media/adapter producer")
            if call.op == "clip.set-beats" and not (isinstance(call.args.get("source"), Mapping)
                    or call.args.get("source") == "keep"):
                raise _Refusal("Beat files/handles require an admitted scoped media producer")
            if call.op == "sequence.fit" and call.args.get("shape_from") is not None:
                raise _Refusal("shape_from requires an admitted scoped exact-timeline read")
            if call.op == "timeline.batch":
                RuntimeCommitService._check_calls(call.args.get("commands", []))

    @classmethod
    def _apply(cls, bundle, calls, *, narration_takes=None):
        require_writable_candidate(bundle)
        cls._check_calls(calls)
        tl = Checkout(copy.deepcopy(bundle))
        if narration_takes is not None:
            tl._narration_takes = copy.deepcopy(narration_takes)
        cls._check_detached_media(tl)
        with edit_transaction(tl, "Runtime commit", quiet=True, persist=False) as tx:
            tx.canonical = True
            def execute(value):
                call = commands.EditCommand.coerce(value)
                if call.op == "timeline.batch":
                    for child in call.args.get("commands", []):
                        execute(child)
                    return
                if call.op in {"cut.split", "timeline.add-cut"} and call.args.get("picture") is not None:
                    if call.op == "cut.split":
                        source = commands.recorded_owner(tl, call, "cut") if call.resolved else tl.cut(call.select)
                    else:
                        source = tl.cut(tl._cut_on_screen(tl._moment_time(tl._split_moment(call.args["on"], None))))
                    if call.args["picture"] not in tl._registry(source.shot_id):
                        raise _Refusal(f"{call.op} picture must be an existing pinned asset key")
                cls._check_media_duration(tl, call)
                commands.execute(tl, call)
            for call in calls:
                execute(call)
            # Inventory is collected after actual calls and before the compiler.
            direct = commands.direct_writes(tl, tx.calls)
        for call in tx.plan.calls:
            if call.op in {"occurrence.set", "occurrence.add"}:
                from astrid.sdk.timeline.graph_commands import Owner, _placement
                owner = Owner(tl, call.select)
                _placement(tl, owner.row, {})
        if tx.plan.blocking or (tx.report is not None and not tx.report.valid):
            raise _Refusal("; ".join(tx.plan.blocking or tx.report.problems or ["Invalid candidate"]))
        return tl.document(), tx.plan, direct

    @staticmethod
    def _check_detached_media(tl):
        for shot_id in (None, *tl.bundle.get("shots", {})):
            internal = (tl.bundle.get("parent", {}) if shot_id is None
                        else tl.bundle["shots"][shot_id].get("internal_timeline", {}))
            assets = internal.get("registry", {}).get("assets", {})
            if any(isinstance(entry, Mapping) and any(isinstance(entry.get(k), str)
                   for k in ("local_path", "path")) for entry in assets.values()):
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
    def _history_choice(result, publication, label):
        result["choices"] = [{"for": conflict["path"], "options": [dict(
            label=f"Later publication {publication}" + (f" ({label})" if label else "") +
                  "; use timelines restore for an explicit historical replacement", commands=[])]}
            for conflict in result["conflicts"]]

    @classmethod
    def _inverse_history(cls, port, request, calls, head, onto, result):
        """Guard publication writes, including ABA and same-revision republication.

        C's provider summaries/evidence can prove COMMIT edges. They exclude
        restore/App events: an unprovable edge is a dependent conflict, never
        silently treated as disjoint. Public-history staging handles those
        events; a race after staging still fails closed here.
        """
        from astrid.sdk.timeline.inverse import authored_paths, overlaps
        inverse_calls = [call for call in calls if call.op == "timeline.inverse"]
        for call in inverse_calls:
            target = call.args["publication"]
            if head["publication_id"] == target:
                continue
            rows, before, seen = {}, None, set()
            while target not in rows:
                read = getattr(port, "revisions", None)
                page = read(dict(limit=200, **({"before": before} if before else {}))) if callable(read) else []
                if not page:
                    break
                rows.update((row["publication_id"], row) for row in page if row.get("publication_id"))
                before = page[-1].get("publication_id")
                if before is None or before in seen:
                    break
                seen.add(before)
            node, visited = head["publication_id"], set()
            while node != target:
                row = rows.get(node)
                if row is None or node in visited or not callable(getattr(port, "evidence", None)):
                    conflict = cls._conflict(result, request, ["publication ancestry/evidence unavailable"],
                                             None, onto, None, kind="dependent")
                    cls._history_choice(conflict, head["publication_id"], "unproven history edge")
                    return conflict
                visited.add(node)
                evidence = port.evidence(row["evidence_digest"])
                if _digest(evidence) != row["evidence_digest"]:
                    raise _Refusal("publication evidence digest mismatch")
                paths = authored_paths(evidence["commands"]) | {(w["id"], w["path"]) for w in evidence["direct_writes"]}
                for write in call.args["writes"]:
                    if any(overlaps((write["id"], write["path"]), path) for path in paths):
                        states = commands.entity_states(Checkout(copy.deepcopy(onto)))
                        path = dict(kind="same-property", id=write["id"], path=write["path"],
                                    base=write["before"], theirs=commands.state_at(states, write["id"], write["path"]),
                                    ours=write["after"])
                        conflict = cls._conflict(result, request, [path], None, onto, None)
                        cls._history_choice(conflict, node, row.get("label"))
                        return conflict
                previous = rows.get(row.get("previous_publication_id"))
                if previous is not None and previous["revision_id"] != row.get("previous_revision_id"):
                    raise _Refusal("inconsistent publication ancestry")
                node = row.get("previous_publication_id")
        return None

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
