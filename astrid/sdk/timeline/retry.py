"""Retry durable timeline edits saved after a conflict."""
from __future__ import annotations

import copy
import hashlib
import json
import uuid
from pathlib import Path
from urllib.parse import quote

from . import clash
from ._command_state import ABSENT, equal, present
from ._commit_patch import canonical_site
from ._doc_patch import value_at
from .sync import _response, _settle_pending
from .outbox import Outbox


def _pin(value):
    return {"revision_id": value["revision_id"], "publication_id": value["publication_id"]}


def _record_digest(record):
    encoded = json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return hashlib.sha256(encoded).hexdigest()


def _record(timeline, ref):
    if not isinstance(ref, str):
        ref = getattr(ref, "pending_id", None)
    if not isinstance(ref, str) or not ref:
        raise ValueError("retry requires a pending id or key")
    actors = "self" if getattr(timeline.session, "_explicit_outbox", False) else "all"
    found = [(path, record) for path, record in timeline._pending_records_with_paths(actors)
             if record.get("id") == ref or record.get("key") == ref]
    if not found:
        raise ValueError(f"no pending change with id or key {ref!r}")
    if len(found) > 1:
        raise ValueError(f"pending key {ref!r} is ambiguous; use a pending id")
    return found[0]


def _close(path, record, session, entry, how):
    publication = entry.get("publication_id")
    receipt = entry.get("receipt", {}).get("receipt_id") or publication
    actor = entry.get("publisher")
    base = _pin(entry)
    outbox = session.outbox if Path(path) == Path(session.outbox.path) else Outbox(Path(path))
    with outbox.locked():
        state = outbox.read()
        saved = state.get("pending_changes", {}).get(record["id"]) if isinstance(state, dict) else None
        if saved is None:
            return record
        if _settle_pending(saved, expected_part=record.get("part", 0), actor_id=actor,
                           publication_id=publication, receipt_id=receipt, how=how, base=base):
            if saved.get("state") == "pending":
                saved["attempts"] = int(saved.get("attempts", 1))
            outbox.write(state)
        return copy.deepcopy(saved)


def _landed(timeline, path, record):
    if record.get("state") == "landed":
        closed = record.get("closed_by") or {}
        publication = closed.get("publication_id")
        actor = closed.get("actor_id")
        label = record.get("label") or "Timeline edit"
        suffix = f" (by {actor})" if actor else ""
        data = {"publication_id": publication} if publication is not None else {}
        receipts = record.get("receipts") or []
        if receipts:
            data["receipt"] = {"receipt_id": receipts[-1]}
        return {"status": "landed", "pending_id": record["id"], "data": data,
                "summary": f'"{label}" already landed as {publication}{suffix}; '
                           f'{record["id"]} is closed. Nothing was sent.', "next": None,
                "record": copy.deepcopy(record)}
    # An acknowledged cross-actor flight identifies the exact owner and part;
    # its group may predate this pending record and is not a unique marker.
    with timeline.session.outbox.locked():
        state = timeline.session._state()
        flights = copy.deepcopy(list(state.get("acknowledged", {}).values()))
    for flight in flights:
        owner = flight.get("pending_path")
        entry = flight.get("pending_ack")
        if (flight.get("pending_id") == record["id"] and owner
                and Path(owner).resolve() == Path(path).resolve()
                and flight.get("pending_part") == record.get("part", 0)
                and isinstance(entry, dict) and entry.get("revision_id")
                and entry.get("publication_id") and entry.get("publisher") == timeline.session.identity["actor_id"]):
            closed = _close(path, record, timeline.session, entry, flight.get("pending_how", "retry"))
            publication = entry["publication_id"]
            if closed.get("state") != "landed":
                return {"status": "part_landed", "pending_id": record["id"],
                        "data": {"publication_id": publication, "part": closed.get("part")},
                        "summary": f'Part of "{record.get("label") or "Timeline edit"}" landed as {publication}; '
                                   f'{record["id"]} still has saved work.',
                        "next": f'tl.retry({record["id"]!r})', "record": closed}
            return {"status": "landed", "pending_id": record["id"],
                    "data": {"publication_id": publication, "receipt": copy.deepcopy(entry["receipt"])},
                    "summary": f'"{record.get("label") or "Timeline edit"}" already landed as {publication}; '
                               f'{record["id"]} is closed. Nothing was sent.',
                    "next": None, "record": closed}
    base = _pin(record["parts"][0]["base"])
    try:
        _, entries = clash._history(timeline.session, base)
    except Exception:  # noqa: BLE001 - history is only a recovery confirmation path
        return None
    index = record.get("part", 0)
    entry = None
    for flight in flights:
        owner = flight.get("pending_path")
        if (flight.get("pending_id") != record["id"] or not owner
                or Path(owner).resolve() != Path(path).resolve()
                or flight.get("pending_part") != index or not flight.get("key")):
            continue
        matches = [item for item in entries
                   if item.get("publisher") == timeline.session.identity["actor_id"]
                   and isinstance(item.get("receipt"), dict)
                   and item["receipt"].get("idempotency_key") == flight["key"]]
        if len(matches) == 1:
            entry = matches[0]
            break
    if entry is None:
        matches = [item for item in entries if item.get("group_id") == record["id"]]
        if type(index) is not int or index < 0 or len(matches) <= index:
            return None
        entry = matches[index]
    publication = entry.get("publication_id")
    if publication is None:
        publication = entry.get("receipt", {}).get("receipt_id")
    closed = _close(path, record, timeline.session, entry, "retry")
    actor = entry.get("publisher")
    label = record.get("label") or "Timeline edit"
    suffix = f" (by {actor})" if actor else ""
    if closed.get("state") != "landed":
        return {"status": "part_landed", "pending_id": record["id"],
                "data": {"publication_id": publication, "part": closed.get("part")},
                "summary": f'Part {index + 1} of "{label}" landed as {publication}{suffix}; '
                           f'{record["id"]} still has saved work.',
                "next": f'tl.retry({record["id"]!r})', "record": closed}
    data = {"publication_id": publication}
    receipt = entry.get("receipt")
    if isinstance(receipt, dict):
        data["receipt"] = copy.deepcopy(receipt)
    return {"status": "landed", "pending_id": record["id"], "data": data,
            "summary": f'"{label}" already landed as {publication}{suffix}; '
                       f'{record["id"]} is closed. Nothing was sent.', "next": None,
            "record": closed}


def _part(record):
    parts = record.get("parts") or []
    index = record.get("part", 0)
    if type(index) is not int or index < 0 or index >= len(parts):
        raise ValueError("pending change has no retryable part")
    return parts[index]


def _refresh_function_base(timeline, record):
    """Move an exhausted function retry's empty outbox to a verified head."""
    session = timeline.session
    with session.outbox.locked():
        state = session._state()
        # A flight may have an unknown outcome. Let commit() reconcile it; never
        # replace it with a fresh evaluation. Likewise, don't move a queued tail.
        if state.get("flight") is not None or state.get("pending"):
            return False
        base = _pin(_part(record)["base"])
        head, _ = clash._history(session, base)
        state["base"] = _pin(head)
        state.pop("conflict", None)
        state.pop("evaluation", None)
        session.outbox.write(state)
    return True


def _request(record, part):
    options = copy.deepcopy(part.get("commit_options") or {})
    base = _pin(part["base"])
    request = {"base_revision_id": base["revision_id"],
               "base_publication_id": base["publication_id"],
               "commands": copy.deepcopy(part["commands"]),
               "label": options.pop("label", record.get("label") or "Timeline edit"),
               **options}
    request.setdefault("merge", "fields")
    if "reads" not in request and request.get("merge") != "atomic":
        request["reads"] = []
    if part.get("rationale") is not None:
        request["rationale"] = copy.deepcopy(part["rationale"])
    if part.get("group_id") is not None:
        request["group_id"] = copy.deepcopy(part["group_id"])
    request["imports"] = copy.deepcopy(part.get("imports", []))
    request["source"] = {"via": "python"}
    return request


def _probe(session, request):
    """Probe the saved request, preserving an intentionally omitted read set."""
    body = {key: copy.deepcopy(value) for key, value in request.items()
            if key not in {"base", "reads", "merge", "commands", "key", "origin", "imports"}}
    body.update(api_version=1, project_id=session.project_id, timeline_id=session.timeline_id,
                commands=copy.deepcopy(request["commands"]), merge=request["merge"], dry_run=True)
    if "reads" in request:
        body["reads"] = copy.deepcopy(request["reads"])
    body.setdefault("source", {"via": "python"})
    try:
        remote = getattr(session.client, "_remote", None)
        transport = getattr(remote, "_transport", None)
        generated = getattr(transport, "_generated", None)
        raw_request = getattr(generated, "_request", None)
        if callable(raw_request):
            # The generated CommitPreview DTO only admits accepted/merged
            # previews. A conflict preview is valid for retry and must reach
            # the re-derive branch instead of becoming invalid_response.
            path = (f"/v1/projects/{quote(session.project_id, safe='')}"
                    f"/timelines/{quote(session.timeline_id, safe='')}/commit")
            _, _, payload = raw_request(
                "POST", path,
                body=json.dumps(body, separators=(",", ":"), allow_nan=False).encode(),
                headers={"Content-Type": "application/json",
                         "Idempotency-Key": "clash-probe-" + uuid.uuid4().hex})
            return generated._json(payload)["data"]
        return _response(session.client.timelines.commit(
            session.project_id, session.timeline_id, body,
            idempotency_key="clash-probe-" + uuid.uuid4().hex))["data"]
    except Exception as exc:  # noqa: BLE001 - retain Runtime refusal details
        details = getattr(exc, "details", None)
        if isinstance(details, dict) and details:
            return exc.details
        raise


def _preview_diff(timeline, data, evaluated_head):
    """Diff the canonical preview document against its evaluated head."""
    document = data.get("canonical_document")
    if not isinstance(document, dict):
        raise ValueError("retry preview did not include canonical_document")
    from .commit import bundle_from_document
    from .emitter import _diff, _payload_for_emit, _root_pairs
    from .store import fetch_bundle

    before = fetch_bundle(timeline.session.project_id, timeline.session.timeline_id,
                          revision_id=evaluated_head["revision_id"],
                          publication_id=evaluated_head["publication_id"],
                          client=timeline.session.client)
    after = bundle_from_document(document)
    changes = []
    for ref, old, new in _root_pairs(before, after):
        _diff(_payload_for_emit(ref, old), _payload_for_emit(ref, new), ref, (), changes)
    if not changes:
        changes = [{"op": command["op"], "ref": command.get("select"),
                    "args": copy.deepcopy(command["args"])}
                   for command in data.get("commands", [])]
    return {"changes": changes}


def _summary_rederive(record, packet, session):
    label = record.get("label") or "Timeline edit"
    receipts = packet.get("receipts") or []
    receipt = next((item for item in receipts if item.get("touches_conflict")), None)
    if receipt is None and record.get("origin") == "function":
        receipt = next((item for item in reversed(receipts)
                        if item.get("publication_id")), None)
    conflicts = ((record.get("failure") or {}).get("details") or {}).get("conflicts", [])
    conflict = conflicts[0] if conflicts else {}
    try:
        site = clash.friendly_target(session, record, conflict)
    except Exception:  # noqa: BLE001 - keep a plain fallback when history is unavailable
        site = "the changed part"
    cause = "the timeline changed"
    if receipt:
        cause = f'"{receipt.get("label") or "Another edit"}"'
        if receipt.get("publisher"):
            cause += f' ({receipt["publisher"]})'
        cause += f' changed {site}'
    return (f'"{label}" can no longer apply as reviewed: {cause}. '
            f'Run what produced the list again on the newest timeline, preview it, then commit it '
            f'with pending="{record["id"]}".')


def _already_applied(timeline, path, record, packet):
    """Close a reviewed edit when every Runtime conflict now has its desired value."""
    conflicts = ((record.get("failure") or {}).get("details") or {}).get("conflicts") or []
    changes = record.get("diff")
    if not conflicts or not isinstance(changes, list) or not changes:
        return None

    part = _part(record)
    base = _pin(part.get("base") or record.get("base"))
    try:
        head, entries = clash._history(timeline.session, base)
        base_closure, head_closure = clash._closures(timeline.session, base, head, entries)
    except Exception:  # noqa: BLE001 - same best-effort boundary as clash enrichment
        return None
    if head_closure is None:
        return None

    expected = {}
    for change in changes:
        if not isinstance(change, dict) or change.get("op") not in {"set", "remove"}:
            return None
        try:
            ref, site_path = canonical_site(base_closure, change["ref"], change.get("path", []))
            address = (ref, tuple(site_path))
            value = (present(change["value"]) if change["op"] == "set" else ABSENT)
        except Exception:  # noqa: BLE001 - malformed or stale enrichment must fall back safely
            return None
        expected[address] = value

    matching_receipts = [receipt for receipt in packet.get("receipts") or []
                         if receipt.get("touches_conflict") and receipt.get("publication_id")]
    if not matching_receipts:
        return None

    # Closing discards the whole part, so non-conflicting authored fields must
    # already be present too; checking conflicts alone could lose that work.
    for (ref, site_path), desired in expected.items():
        try:
            current = value_at(head_closure.images[ref]["payload"], ref, list(site_path))
        except Exception:  # noqa: BLE001 - unverifiable edits stay pending
            return None
        if not equal(desired, current):
            return None

    matched_fields = []
    for conflict in conflicts:
        try:
            ref, site_path = canonical_site(base_closure, conflict["ref"], conflict.get("path", []))
            site = (ref, tuple(site_path))
            desired = expected[site]
            current = value_at(head_closure.images[ref]["payload"], ref, site_path)
        except Exception:  # noqa: BLE001 - malformed or stale enrichment must fall back safely
            return None
        if not equal(desired, current):
            return None
        named = clash._target(conflict, base_closure)
        marker = " already has "
        matched_fields.append(named.partition(marker)[0] if marker in named else named)

    # The user-facing wording names the peer whose receipt is the verified
    # source of the already-applied value. Leave ambiguous multi-writer cases
    # on the ordinary re-derive path.
    receipt_ids = {receipt["publication_id"] for receipt in matching_receipts}
    actors = {receipt.get("publisher") for receipt in matching_receipts}
    if len(receipt_ids) != 1 or len(actors) != 1 or not next(iter(actors)):
        return None
    publication_id = next(iter(receipt_ids))
    entry = next((item for item in entries if item.get("publication_id") == publication_id), None)
    if entry is None:
        return None

    closed = _close(path, record, timeline.session, entry, "already_applied")
    if closed.get("state") != "landed":
        return None
    data = {"publication_id": publication_id}
    if isinstance(entry.get("receipt"), dict):
        data["receipt"] = copy.deepcopy(entry["receipt"])
    return {"status": "landed", "pending_id": record["id"], "data": data, "record": closed,
            "summary": (f"Nothing to do: {', '.join(matched_fields)} already has your value "
                        f"(set by {next(iter(actors))}); {record['id']} is closed."),
            "next": None}


def _refused(record, failure, data):
    from ._commit_wording import present_failure

    presentation = present_failure(failure)
    next_action = f'tl.pending_show({record["id"]!r})'
    return {"status": "refused", "pending_id": record["id"], "data": copy.deepcopy(data),
            "summary": (f'{presentation["headline"]}. {presentation["detail"]} '
                        'Inspect the saved edit and fix it or drop it.'),
            "next": next_action}


def retry(timeline, ref, fn=None, *, overwrite=False):
    """Retry a saved record, returning a reviewable preview or terminal state."""
    if fn is None and callable(getattr(ref, "retry_fn", None)):
        fn = ref.retry_fn
    path, record = _record(timeline, ref)
    if record.get("state") not in {"pending", "landed"}:
        return {"status": "refused", "pending_id": record.get("id"),
                "summary": f'Cannot retry a pending change in state {record.get("state")!r}.',
                "next": None}
    landed = _landed(timeline, path, record)
    if landed is not None:
        return landed
    if overwrite and fn is None:
        raise ValueError("overwrite requires the original edit function")
    failure = record.get("failure") or {}
    if ((failure.get("details") or {}).get("status") in {"refused", "invalid"}
            and (record.get("origin") != "function" or fn is None)):
        return _refused(record, failure, failure)
    captured_function_list = record.get("origin") == "function" and fn is None
    if fn is not None:
        refreshed = _refresh_function_base(timeline, record)
        options = {"_overwrite": True} if overwrite else {}
        result = timeline.commit(fn, label=record.get("label"), key=record.get("key"),
                                 pending=record["id"], **options)
        receipt = result.get("receipt") if isinstance(result, dict) else None
        receipt_id = receipt.get("receipt_id") if isinstance(receipt, dict) else None
        if receipt_id and result.get("status") in {"accepted", "merged", "rebased"} \
                and (refreshed or overwrite):
            label = record.get("label") or "Timeline edit"
            summary = (f'Committed "{label}" as {receipt_id} after re-running it on the '
                       f'newest timeline; {record["id"]} is closed.')
            if overwrite:
                result["status"] = "rebased"
                conflicts = ((record.get("failure") or {}).get("details") or {}).get("conflicts", [])
                sites = [f'{item.get("ref", "?")} (/{"/".join(map(str, item.get("path", [])))})'
                         for item in conflicts]
                summary = (f'Rebased "{label}" as {receipt_id}; overwrote '
                           f'{", ".join(sites) if sites else "the saved conflicting edits"}. '
                           f'{record["id"]} is closed.')
            result["summary"] = summary
            if isinstance(result.get("data"), dict):
                result["data"]["summary"] = result["summary"]
        return result

    failure_details = ((record.get("failure") or {}).get("details") or {})
    if failure_details.get("status") == "conflict":
        packet = record.get("clash") or clash.explain(timeline.session, record)
        already_applied = _already_applied(timeline, path, record, packet)
        if already_applied is not None:
            return already_applied

    part = _part(record)
    request = _request(record, part)
    data = _probe(timeline.session, request)
    details = data.get("details", data) if isinstance(data, dict) else {}
    status = data.get("would_status", data.get("status", details.get("status")))
    if status in {"accepted", "merged"}:
        head = data.get("evaluated_head")
        if not isinstance(head, dict) or not head.get("revision_id") or not head.get("publication_id"):
            raise ValueError("retry preview did not include an evaluated_head")
        head = _pin(head)
        token_data = json.dumps([record["id"], record.get("attempts", 1), head],
                                sort_keys=True, separators=(",", ":")).encode()
        token = hashlib.sha256(token_data).hexdigest()
        label = record.get("label") or "Timeline edit"
        result = {"status": "preview", "pending_id": record["id"],
                  "data": {"commands": copy.deepcopy(part["commands"]),
                           "diff": _preview_diff(timeline, data, head), "head": head,
                           "retry": {"pending_id": record["id"], "token": token}},
                  "summary": f'"{label}" still applies to the newest timeline. Show this diff to '
                             "whoever approves it, then commit.",
                  "next": "tl.commit(r)",
                  "_retry_request": request, "_retry_path": str(path), "_retry_token": token,
                  "_retry_record_rev": record.get("rev"), "_retry_attempts": record.get("attempts", 1),
                  "_retry_record_digest": _record_digest(record)}
        if captured_function_list:
            result["data"]["captured_list_requires_approval"] = True
            result["summary"] = (
                f'"{label}" is available as its saved captured command list. Review this diff '
                "and explicitly commit the preview to approve it; the edit function was not rerun."
            )
        return result
    if status == "conflict":
        packet = clash.explain(timeline.session, record)
        already_applied = _already_applied(timeline, path, record, packet)
        if already_applied is not None:
            return already_applied
        result = {"status": "re_derive", "pending_id": record["id"],
                  "data": {"conflicts": copy.deepcopy(details.get("conflicts", [])),
                           "receipts": copy.deepcopy(packet.get("receipts"))},
                  "summary": _summary_rederive(record, packet, timeline.session),
                  "next": (f'# make_edit is your function; re-run it on the newest timeline and review the preview.\n'
                         f'preview = tl.commit(make_edit, label={record.get("label")!r}, dry_run=True)\n'
                         f'tl.commit(preview["data"]["commands"], label={record.get("label")!r},\n'
                         f'          idempotency_key={record.get("key")!r}, pending={record["id"]!r})')}
        if captured_function_list:
            result["data"]["captured_list_requires_approval"] = True
            result["summary"] += (
                " The saved function edit is only available as captured commands, so they were "
                "not replayed; re-run the original function on the newest timeline and review "
                "the result."
            )
            result["next"] = (
                f'# The saved function edit is only a captured command list. It conflicts with '
                f'the current timeline and was not submitted. If available, re-run the original '
                f'function on the newest timeline.\n'
                f'tl.retry({record["id"]!r}, your_function)'
            )
        return result
    failure = {"message": data.get("message") or details.get("message"), "details": details}
    return _refused(record, failure, data)


def commit_preview(timeline, result):
    """Durably send the exact base-B request represented by a retry preview."""
    if not isinstance(result, dict) or result.get("status") != "preview" or "_retry_request" not in result:
        raise ValueError("commit_preview requires a retry preview returned by retry()")
    request = copy.deepcopy(result["_retry_request"])
    pending_id = result["pending_id"]
    path = result["_retry_path"]
    token = result.get("data", {}).get("retry", {}).get("token")
    if token != result.get("_retry_token"):
        raise ValueError("retry preview token is stale or invalid")
    current = next((record for record_path, record in timeline._pending_records_with_paths("all")
                    if record.get("id") == pending_id and str(record_path) == path), None)
    if current is not None:
        landed = _landed(timeline, Path(path), current)
        if landed is not None:
            return landed
    if (current is None or current.get("state") != "pending"
            or current.get("rev") != result.get("_retry_record_rev")
            or current.get("attempts", 1) != result.get("_retry_attempts")
            or _record_digest(current) != result.get("_retry_record_digest")):
        raise ValueError("pending change changed after this retry preview; preview it again")
    check = json.dumps([pending_id, result["_retry_attempts"], result["data"]["head"]],
                       sort_keys=True, separators=(",", ":")).encode()
    if hashlib.sha256(check).hexdigest() != token:
        raise ValueError("retry preview token is stale or invalid")
    with timeline.session.outbox.locked():
        expected = timeline.session._state()
    part = _part(current)
    expected_request = _request(current, part)
    if request != expected_request:
        raise ValueError("retry preview request no longer matches its saved part; preview it again")
    options = {key: copy.deepcopy(request[key]) for key in ("merge", "reads") if key in request}
    group_id = request.get("group_id", current["id"])
    try:
        submitted = timeline.session.submit(
            request["commands"], label=request["label"], steps=request.get("steps", []),
            merge=options.get("merge", "fields"), reads=options.get("reads"),
            idempotency_key=str(uuid.uuid4()),
            logical_key=current["key"], group_id=group_id,
            rationale=copy.deepcopy(request.get("rationale")),
            imports=copy.deepcopy(part.get("imports", [])),
            expected_state=expected, base={"revision_id": request["base_revision_id"],
                                            "publication_id": request["base_publication_id"]},
            pending_id=pending_id, pending_path=path, pending_part=current.get("part", 0), pending_how="retry")
        return timeline._accepted(submitted)
    except Exception as exc:
        # Timeline.commit() reconciles durable unknown outcomes on the next call.
        if getattr(exc, "failure", {}).get("details", {}).get("status") != "conflict":
            raise
        landed = _landed(timeline, Path(path), current)
        if landed is not None:
            return landed
        raise
