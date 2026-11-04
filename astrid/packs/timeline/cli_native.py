"""Thin native-command CLI adapters; schemas and capture stay in the SDK."""
from __future__ import annotations

import copy
import json
import sys
from collections.abc import Mapping
from pathlib import Path

from astrid.core.cli.domain_output import DomainResult, print_result


def _commit_conflict_message(parsed, details: Mapping[str, object]) -> str:
    """Present a clash with the saved-edit recovery route."""
    current = details.get("current")
    base = details.get("base")
    current_revision = current.get("revision_id") if isinstance(current, Mapping) else None
    base_revision = base.get("revision_id") if isinstance(base, Mapping) else None
    if isinstance(current_revision, str) and current_revision.startswith("astrid.editor.timeline."):
        actor = "the editor"
    else:
        actor = "someone"
    from astrid.sdk.timeline.revisions import short_revision

    changed = []
    conflicts = details.get("conflicts")
    if isinstance(conflicts, list):
        for conflict in conflicts:
            if isinstance(conflict, Mapping):
                ref = conflict.get("ref")
                path = conflict.get("path")
                if isinstance(ref, str) and ref:
                    changed.append(ref)
                elif isinstance(path, list) and path:
                    changed.append("/".join(str(part) for part in path))
    changed_text = ", ".join(changed) if changed else "the changed timeline fields"
    pending_id = details.get("pending_id")
    if isinstance(pending_id, str) and pending_id:
        retry = f"astrid timelines retry {parsed.ref} {pending_id} --project {parsed.project}"
        return (f"not published: {actor} published {short_revision(current_revision)} after your checkout "
                f"({short_revision(base_revision)}), and you both changed:\n  {changed_text}\n"
                f"your edit is saved as {pending_id}; inspect it with `timelines pending show`, "
                f"then recover with:\n  {retry}\n"
                "force overwrite is refused; review the saved edit before retrying.")
    return (
        f"not published: {actor} published {short_revision(current_revision)} after your checkout "
        f"({short_revision(base_revision)}), and you both changed:\n  {changed_text}\n"
        f"next: timelines discard {parsed.ref} --project {parsed.project}, then timelines checkout "
        f"{parsed.ref} --project {parsed.project} --fresh, and re-apply your edit.\n"
        "  force overwrite is refused; discard the newer head first."
    )


def _document_error(parsed, exc: Exception, *, code: str | None = None) -> DomainResult:
    """Turn a detached-document refusal into the normal structured CLI error."""
    from astrid.sdk.contracts import ErrorObject

    details = dict(getattr(exc, "details", {}) or {})
    failure = getattr(exc, "failure", None)
    if isinstance(failure, Mapping):
        details = dict(failure.get("details") or details)
        code = code or str(failure.get("code") or "timeline_commit_error")
    pending_id = getattr(exc, "pending_id", None)
    if isinstance(pending_id, str):
        details["pending_id"] = pending_id
        if isinstance(getattr(exc, "clash", None), Mapping):
            details["clash"] = copy.deepcopy(exc.clash)
    details.setdefault("path", "/file")
    details.setdefault("hint", "re-check out the detached document and retry with its exact base pair")
    details.setdefault(
        "suggested_command",
        f"astrid timelines checkout {parsed.ref} --project {parsed.project}",
    )
    message = str(exc)
    if details.get("status") in {"conflict", "refused"} and isinstance(details.get("current"), Mapping):
        message = _commit_conflict_message(parsed, details)
    return DomainResult.failure(
        ErrorObject(code or str(getattr(exc, "code", "timeline_commit_error")), message, details),
        idempotency_key=parsed.idempotency_key or "",
    )


def _commit_envelope(raw: Mapping[str, object], key: str) -> DomainResult:
    """Preserve the complete Runtime v2 result returned by TimelineSession."""
    from astrid.core.receipts.contract import CommandReceipt

    receipt = raw.get("receipt")
    data = raw.get("data")
    committed = None
    if isinstance(receipt, Mapping):
        committed = CommandReceipt.from_dict(dict(receipt))
        if isinstance(committed.result, Mapping):
            enriched = dict(committed.result)
            if isinstance(data, Mapping):
                # ``TimelineSession.flush`` adds these presentation fields
                # after Runtime's canonical result is sealed in the receipt.
                for field in ("accepted_publication_id", "show_command", "summary", "undo",
                              "replayed", "confirmed", "attempts"):
                    if field in data:
                        enriched[field] = data[field]
            data = enriched
    if isinstance(data, Mapping) and raw.get('imports'):
        data = {**data, 'imports': copy.deepcopy(raw['imports'])}
    return DomainResult.success(data, receipt=committed, idempotency_key=key)


def _detached_document(parsed):
    """Load the existing editable bundle format and verify its exact public scope."""
    from astrid.sdk.timeline._common import TimelineEditError
    from astrid.sdk.timeline.store import resolve_ids
    from astrid.sdk.timeline_checkout import Checkout

    checkout = Checkout.load(parsed.file)
    bundle = checkout.stored_document()
    project = bundle.get("project_id")
    timeline = bundle.get("timeline_id")
    base = bundle.get("base_parent")
    if not isinstance(project, str) or not project or not isinstance(timeline, str) or not timeline:
        raise TimelineEditError("unsupported detached document: project_id and timeline_id are required")
    if not isinstance(base, Mapping) or not all(
        isinstance(base.get(key), str) and base[key] for key in ("revision_id", "publication_id")
    ):
        raise TimelineEditError("detached document has no exact base revision/publication pair")
    if not isinstance(bundle.get("parent"), Mapping) or not isinstance(bundle.get("placements"), list):
        raise TimelineEditError("unsupported detached document: expected parent and placements")
    if not isinstance(bundle.get("shots"), Mapping):
        raise TimelineEditError("unsupported detached document: expected a shot map")

    resolved_project, resolved_timeline, _head = resolve_ids(
        parsed.project, parsed.ref, client=parsed.client
    )
    if (resolved_project, resolved_timeline) != (project, timeline):
        raise TimelineEditError(
            "commit scope does not match the detached document "
            f"(file {project}/{timeline}, command {resolved_project}/{resolved_timeline})"
        )
    return checkout, bundle, {key: base[key] for key in ("revision_id", "publication_id")}


def _normalized_document_diff(bundle):
    """Normalize only the supported document/diff boundary.

    ``Checkout`` creates an empty parent ``config.app`` while opening a
    detached file.  Runtime's canonical bundle omits that empty container.
    Treating those two representations as different would turn a clean
    checkout into an authored ``config.app = {}`` patch.  The existing
    timeline intent normalizer preserves all non-empty and unknown data; the
    tidy step removes only an empty film app namespace for comparison.
    """
    from astrid.core.timeline import astrid_ns

    normalized = astrid_ns.normalized(bundle)
    for root in ("parent", "base_parent_payload"):
        payload = normalized.get(root)
        config = payload.get("config") if isinstance(payload, Mapping) else None
        if isinstance(config, dict):
            astrid_ns.tidy_film(config)
    return normalized


def _preview_detached_document(checkout, bundle, base, parsed, *, dry_run: bool):
    """Use the public G2 session commit with the G3 emitter."""
    from astrid.sdk.timeline.commit_session import Timeline
    from astrid.sdk.timeline.sync import TimelineSession

    session = TimelineSession.open(
        bundle["project_id"], bundle["timeline_id"], client=parsed.client,
        outbox_path=parsed.file.with_suffix(".outbox.json"),
        base_revision_id=base["revision_id"], base_publication_id=base["publication_id"],
    )
    with session.outbox.locked():
        state = session._state()
    # The editable bundle omits Runtime's private canonical document image.
    # Document patch emission needs that image to materialize a complete v2
    # request, so use the authenticated capture inside this CLI boundary.
    authenticated_capture = _normalized_document_diff(session._capture_bundle(state))
    authenticated_base = _normalized_document_diff(session._bundle(state))
    candidate = _normalized_document_diff(checkout.bundle)
    if candidate == authenticated_base:
        return {"data": {"status": "preview" if dry_run else "unchanged",
                          **({"would_status": "unchanged"} if dry_run else {}),
                          "project_id": bundle["project_id"], "timeline_id": bundle["timeline_id"],
                          "label": parsed.message, "commands": [], "steps": [],
                          "merge_mode": "atomic"}, "receipt": None}
    working = Timeline(session, authenticated_capture, base)
    working.bundle = candidate
    working._refresh()
    return working.commit(
        parsed.message, merge="atomic", dry_run=dry_run,
        idempotency_key=parsed.idempotency_key,
    )


def commit_document(parsed) -> int:
    """Commit a detached document through the durable G2 outbox route."""
    from astrid.sdk.timeline._common import TimelineEditError
    from astrid.sdk.workspace_client import WorkspaceClientError

    if not isinstance(parsed.message, str) or not parsed.message.strip():
        result = _document_error(parsed, TimelineEditError("commit message must be nonempty"), code="validation_error")
        return print_result(result, as_json=parsed.json)
    if not isinstance(parsed.idempotency_key, str) or not parsed.idempotency_key.strip():
        result = _document_error(parsed, TimelineEditError("idempotency key must be nonempty"), code="validation_error")
        return print_result(result, as_json=parsed.json)
    try:
        checkout, bundle, base = _detached_document(parsed)
        summary = None
        if not parsed.json and not parsed.dry_run:
            edits = checkout.edits()
            summary = (
                len(edits.get("changes") or []) if isinstance(edits, Mapping) else 0,
                len(checkout.changed_cut_ids()),
            )
        raw = _preview_detached_document(checkout, bundle, base, parsed, dry_run=parsed.dry_run)
        result = _commit_envelope(raw, parsed.idempotency_key)
    except (TimelineEditError, WorkspaceClientError, OSError, ValueError, TypeError, KeyError) as exc:
        result = _document_error(parsed, exc)
    return print_result(
        result,
        as_json=parsed.json,
        human_renderer=lambda value: _render_commit_result(
            value, dry_run=parsed.dry_run, label=parsed.message, summary=summary,
            project=parsed.project, ref=parsed.ref, include_revision=True,
        ),
    )


def _render_commit_result(
    value: object,
    *,
    dry_run: bool,
    label: str | None = None,
    summary: tuple[int, int] | None = None,
    project: str | None = None,
    ref: str | None = None,
    include_revision: bool = False,
) -> str:
    if not isinstance(value, DomainResult) or not isinstance(value.data, Mapping):
        raise TypeError("native commit renderer received an invalid result envelope")
    data = value.data
    if isinstance(data.get("summary"), str):
        lines = [data["summary"]]
        if include_revision and data.get("accepted_revision_id"):
            publication = data.get("accepted_publication_id")
            receipt = f" · publication {publication}" if publication else ""
            lines.append(f"accepted revision {data['accepted_revision_id']}{receipt} · {label or 'Timeline edit'}")
        if summary is not None and not dry_run:
            clip_word = "clip" if summary[0] == 1 else "clips"
            cut_word = "cut" if summary[1] == 1 else "cuts"
            lines.append(f"changed: {summary[0]} {clip_word} in {summary[1]} {cut_word}")
            lines.append(f"next: timelines edit {ref or '<timeline>'} --project {project or '<project>'}")
        if data.get("show_command"):
            lines.append(data["show_command"])
        return "\n".join(lines)
    if dry_run:
        return f"preview only; no publication accepted{f' · {label}' if label else ''}"
    suffix = f" · {label}" if label else ""
    if summary is not None:
        from astrid.sdk.timeline.revisions import short_revision

        previous = short_revision(data.get("previous_revision_id"))
        accepted = short_revision(data.get("accepted_revision_id"))
        clip_word = "clip" if summary[0] == 1 else "clips"
        cut_word = "cut" if summary[1] == 1 else "cuts"
        return (
            f"published {previous} → {accepted}: {label or 'Timeline edit'}\n"
            f"changed: {summary[0]} {clip_word} in {summary[1]} {cut_word}\n"
            f"next: timelines edit {ref or '<timeline>'} --project {project or '<project>'}"
        )
    return (f"accepted revision {data['accepted_revision_id']} · publication {data['accepted_publication_id']}"
            f"{suffix}\n"
            f"{data['show_command']}")


def ordinary_trim_requested(parsed):
    from astrid.sdk.timeline import store

    if (parsed.client is None or parsed.file or parsed.draft
            or (parsed.trim_in is None and parsed.trim_out is None)):
        return False
    # Route only this family. Compare parser defaults so a new flag cannot
    # silently disappear when an older native adapter sees it.
    accepted = {'project', 'ref', 'clip', 'cut', 'near', 'trim_in', 'trim_out',
                'dry_run', 'idempotency_key', 'json', 'no_sweep'}
    if not all(name in accepted or getattr(parsed, name) == default
               for name, default in parsed.native_trim_defaults.items() if name != 'help'):
        return False
    # Detached work remains authoritative until that ingress is migrated.
    return store.find_draft(parsed.project, parsed.ref, client=parsed.client) is None


def ordinary_move_requested(parsed):
    from astrid.sdk.timeline import store

    if (parsed.client is None or parsed.file or parsed.draft
            or parsed.on is None):
        return False
    try:
        float(parsed.on)
    except (TypeError, ValueError):
        # Symbolic moments (for example, ``after \"Astrid\"``) still need
        # the detached checkout's authored-word resolution.
        return False
    accepted = {'project', 'ref', 'clip', 'cut', 'near', 'on', 'dry_run',
                'idempotency_key', 'json', 'no_sweep'}
    if not all(name in accepted or getattr(parsed, name) == default
               for name, default in parsed.native_trim_defaults.items() if name != 'help'):
        return False
    return store.find_draft(parsed.project, parsed.ref, client=parsed.client) is None


def dispatch_ordinary_edit(parsed):
    """Dispatch the supported ordinary native edit, or leave legacy edits alone."""
    if parsed.client is not None and not parsed.file and not parsed.draft and parsed.add_layer == 'media':
        return commit_edit(parsed, edit=commit_add_media)
    if ordinary_move_requested(parsed):
        result = commit_edit(parsed, edit=commit_move)
        if result is not NotImplemented:
            return result

    if ordinary_trim_requested(parsed):
        result = commit_edit(parsed, edit=commit_trim)
        if result is not NotImplemented:
            return result
    return NotImplemented


def commit_add_media(session, parsed, options):
    from astrid.packs.timeline.cli import _parse_set
    from astrid.sdk.timeline import store
    from astrid.sdk.timeline._common import TimelineEditError

    accepted = {'project', 'ref', 'add_layer', 'layer_asset', 'on', 'for_seconds', 'until',
                'track', 'clip_id', 'layer', 'set_values', 'dry_run', 'idempotency_key', 'json', 'no_sweep'}
    unsupported = [name for name, default in parsed.native_trim_defaults.items()
                   if name != 'help' and name not in accepted and getattr(parsed, name) != default]
    if unsupported:
        raise TimelineEditError('media add does not support ' + ', '.join('--' + name.replace('_', '-')
                                for name in unsupported) + '; add the clip first, then edit it')
    if store.find_draft(parsed.project, parsed.ref, client=parsed.client) is not None:
        raise TimelineEditError('a detached working copy is pending; commit it first, or run '
                                f'astrid timelines discard {parsed.ref} --project {parsed.project}')
    if not parsed.layer_asset:
        raise TimelineEditError('--add-layer media needs --layer-asset FILE|HANDLE|KEY')
    try:
        on = float(parsed.on)
        duration = (float(parsed.until) - on if parsed.until is not None else parsed.for_seconds)
    except (TypeError, ValueError) as exc:
        raise TimelineEditError('media add needs literal seconds; for example: '
                                '--on 0 --for 3 --track V1') from exc
    if duration is None or parsed.until is not None and parsed.for_seconds is not None:
        raise TimelineEditError('media add needs one of --for SECONDS or --until SECONDS; '
                                'for example: --on 0 --for 3 --track V1')
    values = _parse_set(parsed.set_values)
    reserved = {'id', 'clipType', 'track', 'asset', 'at', 'hold', 'from', 'to', 'speed'}
    if reserved.intersection(values):
        raise TimelineEditError('set media identity and timing with --id, --track, --on and --for')
    if parsed.layer:
        values['label'] = parsed.layer
    return session.add_media(parsed.layer_asset, on=on, duration=duration, track=parsed.track,
                             clip_id=parsed.clip_id, values=values, **options)


def show_native(parsed):
    """Handle the retained native sheet projection, or leave ordinary show alone."""
    if not getattr(parsed, "native", False):
        return NotImplemented
    if getattr(parsed, "presentation", None) != "sheet":
        from astrid.sdk.contracts import ErrorObject

        return print_result(DomainResult.failure(ErrorObject(
            "validation_error", "--native requires --as sheet", {})), as_json=parsed.json)
    from astrid.packs.timeline.cli_show import print_native_sheet

    return print_native_sheet(parsed)


def _trim_selector(bundle, parsed):
    from astrid.core.timeline.cuts import bundle_fps
    from astrid.core.timeline.duration import clip_timeline_duration
    from astrid.sdk.timeline._command_state import State
    from astrid.sdk.timeline_checkout import Checkout

    state = State(bundle)
    if isinstance(parsed.clip, str) and '/clip:' in parsed.clip:
        state.entity(parsed.clip)
        return parsed.clip
    # Reuse the ordinary lookup grammar on a read-only view. Bind each view
    # row to its raw owner explicitly; display IDs never encode ownership.
    view = Checkout({})
    view.bundle = copy.deepcopy(bundle)
    # The lookup mixin also exposes parent-native clips. The synthetic views
    # below represent every owner as a shot, so retain no second parent source.
    view.bundle.update(parent={}, shots={}, placements=[])
    owners = ['parent', *('occurrence:' + row['occurrence_id'] for row in bundle.get('placements', []))]
    bindings = {}
    for owner in owners:
        config = copy.deepcopy(state.config(owner))
        config['clips'] = copy.deepcopy(state.collection(owner, 'clip'))
        for row in config['clips']:
            bindings[id(row)] = owner + '/clip:' + row['id']
        placement = {'start_ms': 0} if owner == 'parent' else copy.deepcopy(state.occurrence(owner).get('placement', {}))
        duration = max((row.get('at', 0) + clip_timeline_duration(row) for row in config['clips']), default=0)
        view.bundle['placements'].append({'shot_id': owner, 'occurrence_id': owner,
                                           'placement': placement, 'duration_ms': duration * 1000})
        view.bundle['shots'][owner] = {'payload': {}, 'internal_timeline': config}
    view.fps = bundle_fps(bundle) or 30
    if parsed.clip is not None:
        clip = view.clip(parsed.clip, near=parsed.near, cut=parsed.cut)
    else:
        cut = int(parsed.cut) if str(parsed.cut).isdigit() else parsed.cut
        clip = view.cut_picture(cut) if isinstance(cut, str) else view.cut(cut).picture
    return bindings[id(clip.data)] if clip is not None else None


def commit_trim(session, parsed, options):
    from astrid.sdk.timeline._command_state import CommandError
    from astrid.sdk.timeline._trim_commands import capture_clip_trim

    with session.outbox.locked():
        state = session._state()
    # Resolve aliases at the original base on retry, never at the newer head
    # or on the already-trimmed local prefix.
    bundle = session._bundle({**state, 'pending': []})
    select = _trim_selector(bundle, parsed)
    if select is None or not select.startswith('parent/clip:'):
        return NotImplemented
    if not state['pending']:
        try:
            capture_clip_trim(bundle, select, in_s=parsed.trim_in, out_s=parsed.trim_out)
        except CommandError as exc:
            if parsed.clip == select or exc.kind == 'unsupported_media':
                raise
            return NotImplemented
    return session.trim(select, in_s=parsed.trim_in, out_s=parsed.trim_out, **options)


def commit_move(session, parsed, options):
    from astrid.sdk.timeline._command_state import CommandError

    with session.outbox.locked():
        state = session._state()
    # Resolve aliases against the pinned original head. The native command
    # stores the parent owner in its selection; it never infers ownership from
    # a display id or recaptures it after a lost acknowledgement.
    bundle = session._bundle({**state, 'pending': []})
    select = _trim_selector(bundle, parsed)
    if select is None:
        return NotImplemented
    try:
        return session.move(select, float(parsed.on), **options)
    except CommandError as exc:
        # Legacy occurrence timelines can carry source maps that the native
        # clip.move recipe deliberately does not support.  Let the detached
        # editor retain its owner-resolution path for those maps only; native
        # guard, identity, timing, and authority failures must remain visible.
        if select.startswith('occurrence:') and exc.kind == 'unsupported_map':
            return NotImplemented
        raise


def _verify_commit_selection(session, parsed):
    from types import SimpleNamespace

    from astrid.core.receipts.contract import CommandReceipt
    from astrid.sdk.timeline.revisions import RuntimeRevisions
    from astrid.sdk.workspace_client import WorkspaceClientError

    entries = {}

    def read(project, timeline, **options):
        result = session.client.timelines.history(project, timeline, **options)
        if result.ok and isinstance(result.data, dict):
            entries.update((row["publication_id"], row) for row in result.data["items"])
        return result

    # Reuse the public reader's pinned pagination and complete ancestry checks.
    history = RuntimeRevisions(SimpleNamespace(history=read)).list(
        session.project_id, session.timeline_id, limit=sys.maxsize)
    head = ({"revision_id": history[0].id, "publication_id": history[0].publication_id}
            if history else None)

    def require(condition, message):
        if not condition:
            raise WorkspaceClientError(0, "history_unavailable", message, {
                "path": "/commands/0/args/publications", "head": head,
            })

    ordered = [revision.publication_id for revision in reversed(history or [])
               if revision.publication_id in parsed.commit]
    require(ordered == parsed.commit, "select exact accepted commit receipts, oldest first")
    for publication in parsed.commit:
        entry = entries[publication]
        receipt = CommandReceipt.from_dict(entry["receipt"])
        data, records = receipt.result, entry["records"]
        require(entry["intent"] == "recorded" and isinstance(records, dict)
                and isinstance(data, dict), "selected commit has no guarded accepted history")
        require(receipt.receipt_id == publication and receipt.project_id == session.project_id
                and receipt.command_kind == "timeline.commit"
                and data.get("accepted_revision_id") == entry["revision_id"]
                and data.get("publisher") == entry["publisher"]
                and all(data.get(key) == entry[key] for key in ("provider_version", "compiler_version"))
                and {"revision_id": data.get("previous_revision_id"),
                     "publication_id": data.get("previous_publication_id")} == entry["previous"],
                "selected history does not match its exact accepted receipt")
        document = data.get("canonical_document", {})
        require(document.get("project_id") == session.project_id
                and document.get("timeline_id") == session.timeline_id
                and document.get("parent_revision", {}).get("revision_id") == entry["revision_id"],
                "selected receipt belongs to a different canonical timeline closure")
        require(all(key in data and data[key] == records.get(key) == entry.get(key)
                    for key in ("label", "steps", "merge_mode")),
                "selected label and steps do not match accepted receipt history")
        if parsed.command == "redo":
            require(bool(records.get("undo_of")), "redo requires an accepted undo commit receipt")


def commit_edit(parsed, *, sheet=False, edit=None, inverse=False):
    from astrid.sdk.contracts import ErrorObject
    from astrid.sdk.timeline._common import TimelineEditError
    from astrid.sdk.timeline.sync import CommitConflict, TimelineSession

    try:
        if inverse:
            if not parsed.commit:
                raise TimelineEditError("commit options require --commit RECEIPT_ID")
            if parsed.draft or parsed.steps is not None or isinstance(parsed.steps_option, int):
                raise TimelineEditError("--commit cannot be combined with a local draft or edit count")
            if (any(not value.strip() for value in parsed.commit)
                    or len(set(parsed.commit)) != len(parsed.commit)):
                raise TimelineEditError("--commit requires unique exact receipt IDs, oldest first")
            if parsed.steps_option is not None and set(parsed.steps_option) != set(parsed.commit):
                raise TimelineEditError("--steps must map every selected commit to its step IDs")
        if sheet and parsed.draft:
            raise TimelineEditError("a sheet commit cannot be combined with a local draft")
        session = TimelineSession.open(parsed.project, parsed.ref, client=parsed.client)
        options = {"idempotency_key": parsed.idempotency_key, "dry_run": parsed.dry_run, "via": "cli"}
        if inverse:
            _verify_commit_selection(session, parsed)
            result = session.undo(parsed.commit, steps=parsed.steps_option, **options)
        elif edit is not None:
            result = edit(session, parsed, options)
            if result is NotImplemented:
                return NotImplemented
        elif sheet:
            from astrid.sdk.timeline_sheet import NativeSheet

            result = NativeSheet.load(session).apply(
                Path(parsed.sheet_file).read_text(encoding="utf-8"), session=session,
                dry_run=parsed.dry_run, idempotency_key=parsed.idempotency_key)
            if parsed.dry_run or not isinstance(result, dict):
                return print_result(DomainResult.success({
                    "status": "preview" if parsed.dry_run else "unchanged", "commands": result.commands,
                }), as_json=parsed.json, human_renderer=lambda _: (
                    "preview only; no publication accepted" if parsed.dry_run else "no change (the sheet matches its original export)"))
        else:
            result = session.move(parsed.clip, parsed.on, **options)
        shown = _commit_envelope(result, result["idempotency_key"])
    except Exception as exc:  # noqa: BLE001 - preserve pending work and render transport/semantic failures
        presentation = getattr(exc, "presentation", None)
        message = (presentation["headline"] if isinstance(presentation, Mapping)
                   else str(exc))
        if isinstance(presentation, Mapping):
            message += f"\nfix: {presentation['fix']}"
        message += f"; edited sheet kept at {parsed.sheet_file}" if sheet else ""
        if isinstance(exc, CommitConflict):
            details = {**exc.failure["details"], **presentation} if isinstance(presentation, Mapping) else exc.failure["details"]
            error = ErrorObject(exc.failure["code"], message, details)
        else:
            details = copy.deepcopy(getattr(exc, "details", {}) or {})
            if isinstance(presentation, Mapping):
                details.update(presentation)
            error = ErrorObject(getattr(exc, "code", "timeline_edit_error"), message, details)
        if inverse:
            import shlex

            details = dict(error.details)
            details.setdefault("path", "/commands/0/args/publications")
            details.setdefault("head", details.get("current"))
            details.setdefault("hint", "Read accepted history and select exact receipt IDs, oldest first; retry ambiguous outcomes with the original key.")
            details.setdefault("suggested_command", shlex.join([
                "astrid", "timelines", "history", parsed.ref, "--project", parsed.project, "--json",
            ]))
            error = ErrorObject(error.code, error.message, details)
        shown = DomainResult.failure(error, idempotency_key=parsed.idempotency_key or "")
    return print_result(shown, as_json=parsed.json,
                        human_renderer=lambda value: _render_commit_result(value, dry_run=parsed.dry_run))
def configure_edit_options(subparser):
    from astrid.packs.timeline.cli import _add_idempotency_key, _add_json_flag

    subparser.description = ("Media layer adds, literal clip moves and trims commit through the durable timeline session. "
                             "Other flags edit a legacy detached working copy.")
    subparser.add_argument("--dry-run", action="store_true", help="Preview a media add, move or trim without accepting a publication.")
    _add_idempotency_key(subparser)
    _add_json_flag(subparser, default=False)
    subparser.set_defaults(native_trim_defaults={action.dest: action.default for action in subparser._actions})


def configure_apply_options(subparser):
    from astrid.packs.timeline.cli import _add_idempotency_key, _add_json_flag

    subparser.add_argument("--dry-run", action="store_true", help="Preview the sheet commit without publishing.")
    _add_idempotency_key(subparser)
    _add_json_flag(subparser, default=False)
    subparser.set_defaults(native=False)


def history_steps(value):
    import argparse

    from astrid.packs.timeline.cli import _positive_int

    if not value.lstrip().startswith("{"):
        return _positive_int(value)
    try:
        steps = json.loads(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("steps must be a JSON map of receipt IDs to step-ID lists") from exc
    if (not steps or not all(isinstance(key, str) and key.strip()
                             and isinstance(ids, list) and ids
                             and all(isinstance(step, str) and step.strip() for step in ids)
                             and len(set(ids)) == len(ids) for key, ids in steps.items())):
        raise argparse.ArgumentTypeError("steps must map each receipt ID to nonempty unique step IDs")
    return steps


def configure_history_options(subparser):
    from astrid.packs.timeline.cli import _add_idempotency_key, _add_json_flag

    subparser.add_argument("--commit", action="append", metavar="RECEIPT_ID",
                           help="Reverse an accepted Runtime commit; for redo select the accepted undo. Repeat oldest first.")
    subparser.add_argument("--dry-run", action="store_true", help="Preview the inverse without accepting a commit.")
    _add_idempotency_key(subparser)
    _add_json_flag(subparser, default=False)
