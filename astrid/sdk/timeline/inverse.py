"""Publication inverse producer. Runtime history owns ancestry; journals own local commands.

No snapshot is a history link. Only a linked, accepted saved-COMMIT result can
supply commands, and exact Runtime closures must reproduce both ends before an
inverse is staged. The inverse is an ordinary guarded OpCall, published by the
existing saved-COMMIT/rebase route.
"""
from __future__ import annotations

import copy
import json
import re
from collections.abc import Mapping
from pathlib import Path

from astrid.sdk.timeline import commands, journal, store
from astrid.sdk.timeline._common import TimelineEditError


def publications(project, timeline):
    """Verified local acceptance records keyed by publication, never revision ID."""
    from astrid.sdk.timeline.saved_intent import _finite_json
    from astrid.sdk.timeline.saved_publish import _state

    path = journal.journal_path(project, timeline)
    with journal._locked(path):
        if not path.is_file():
            return {}
        if journal.verify(path):
            raise TimelineEditError("publication journal is damaged; run timelines log --verify")
        rows = [_finite_json(line) for line in path.read_text().splitlines()]
    grouped = {}
    for row in rows:
        if row.get("project") != project or row.get("timeline") != timeline:
            raise TimelineEditError("publication journal has wrong scope")
        grouped.setdefault(row["draft"], []).append(row)
    out = {}
    for draft, records in grouped.items():
        _state(records)  # validate intent/result/checkpoint identity and continuity
        intent = None
        for row in records:
            for item in row.get("evidence", []):
                if not isinstance(item, Mapping):
                    continue
                if item.get("kind") == "publish-intent-v1":
                    intent = item
                elif item.get("kind") == "publish-result-v1" and intent is not None:
                    result = item["result"]
                    body = intent["body"]
                    if (body.get("project_id"), body.get("timeline_id")) != (project, timeline):
                        raise TimelineEditError("publication intent has wrong scope")
                    pid = result.get("accepted_publication_id")
                    if result.get("status") == "accepted" and pid:
                        value = dict(body=body, result=result, draft=draft)
                        if pid in out and out[pid] != value:
                            raise TimelineEditError("publication has ambiguous local command evidence")
                        out[pid] = copy.deepcopy(value)
    return out


def _tokens(path):
    token = re.compile(r'(?:^|\.)([A-Za-z_][\w-]*)|\[(\d+|"(?:[^"\\]|\\.)*")\]')
    keys, offset = [], 0
    for match in token.finditer(path):
        if match.start() != offset:
            raise commands.CommandError(f"Invalid inverse property path {path!r}")
        offset = match.end()
        keys.append(match.group(1) if match.group(1) is not None else json.loads(match.group(2)))
    if offset != len(path) or not keys:
        raise commands.CommandError(f"Invalid inverse property path {path!r}")
    return keys


def _write(data, path, state):
    keys = _tokens(path)
    node = data
    for index, key in enumerate(keys[:-1]):
        if isinstance(node, dict) and key not in node:
            if state["state"] == "absent":
                return
            node[key] = [] if isinstance(keys[index + 1], int) else {}
        node = node[key]
    key = keys[-1]
    if state["state"] == "present":
        node[key] = copy.deepcopy(state["value"])
    elif isinstance(node, dict):
        node.pop(key, None)
    elif isinstance(key, int) and key < len(node):
        node.pop(key)


def _entity(tl, sid):
    if sid == "parent":
        return tl.bundle["parent"]
    if sid.startswith("cut:"):
        from astrid.sdk import timeline_intent as intent
        return intent._film_w(tl.bundle).setdefault("cut_notes", {}).setdefault(sid[4:], {})
    oid, _, tail = sid.removeprefix("occurrence:").partition("/")
    rows = [row for row in tl.bundle["placements"] if row.get("occurrence_id") == oid]
    if not sid.startswith("occurrence:") or len(rows) != 1:
        raise commands.CommandError(f"Unavailable inverse target {sid!r}")
    if not tail:
        return rows[0]
    shot = tl.bundle["shots"][rows[0]["shot_id"]]
    if tail == "shot":
        return shot["payload"]
    kind, _, identity = tail.partition(":")
    if kind not in {"clip", "track"}:
        raise commands.CommandError(f"Unavailable inverse target {sid!r}")
    matches = [row for row in shot["internal_timeline"][kind + "s"] if row["id"] == identity]
    if len(matches) != 1:
        raise commands.CommandError(f"Unavailable inverse target {sid!r}")
    return matches[0]


def apply_inverse(self, writes, publication):
    """Apply guarded authored properties, then let the normal compiler resolve consequences."""
    tl = self
    if not isinstance(publication, str) or not publication or not isinstance(writes, list) or not writes:
        raise commands.CommandError("inverse requires a named publication and authored writes")
    seen = set()
    states = commands.entity_states(tl)
    for write in writes:
        if not isinstance(write, dict) or set(write) != {"id", "path", "before", "after"}:
            raise commands.CommandError("invalid inverse write")
        sid, path = write["id"], write["path"]
        if not isinstance(sid, str) or not isinstance(path, str) or not path:
            raise commands.CommandError("structural inverse requires an admitted identity producer; use timelines restore")
        keys = _tokens(path)
        # Identity/provenance is never editable through a property inverse.
        if keys[0] in {"id", "shot_id", "occurrence_id", "shot_revision_id"}:
            raise commands.CommandError("inverse cannot rewrite entity identity; use timelines restore")
        if (sid, path) in seen:
            raise commands.CommandError("duplicate inverse write")
        seen.add((sid, path))
        for state in (write["before"], write["after"]):
            if (not isinstance(state, dict) or state.get("state") not in {"absent", "present"}
                    or set(state) != ({"state", "value"} if state["state"] == "present" else {"state"})):
                raise commands.CommandError("invalid inverse presence state")
        current = commands.state_at(states, sid, path)
        if not commands._same_value(current, write["before"]):
            raise commands.SemanticConflict("same-property", sid, path, write["before"], current, write["after"])
    for write in writes:
        path = write["path"]
        path = "app.astrid." + path[7:] if path.startswith("intent.") else path
        try:
            _write(_entity(tl, write["id"]), path, write["after"])
        except (KeyError, IndexError, TypeError, AttributeError) as exc:
            raise commands.CommandError(f"inverse path is unavailable: {write['id']}.{path}") from exc
    tl._refresh()


def inverse_command(calls, before, after, publication):
    """Coalesce direct authored preconditions: first before, final after.

    Compiler consequences are deliberately recomputed. Repeated writes to one
    path and parent/child writes collapse to their first precondition, avoiding
    intermediate inverse compilation and preserving presence versus null.
    """
    from astrid.sdk.timeline.checkout import Checkout
    desired = {}
    for value in calls:
        call = commands.EditCommand.coerce(value)
        if any(getattr(call, key) is None for key in ("expect", "authorship", "bindings", "created")):
            raise commands.CommandError("publication lacks recorded command preconditions")
        for key, kind in call.authorship.items():
            if kind != "direct":
                continue
            if key not in call.expect:
                raise commands.CommandError("publication lacks an authored precondition")
            sid, path = commands.write_target(key, call.resolved)
            if not path:
                raise commands.CommandError("structural inverse requires an admitted identity producer; use timelines restore")
            desired.setdefault((sid, path), copy.deepcopy(call.expect[key]))
    original = commands.entity_states(Checkout(copy.deepcopy(before)))
    desired = {(sid, path): commands.state_at(original, sid, path) for sid, path in desired
               if not any(other_sid == sid and other != path and path.startswith((other + ".", other + "["))
                          for other_sid, other in desired)}
    states = commands.entity_states(Checkout(copy.deepcopy(after)))
    writes = [dict(id=sid, path=path, before=commands.state_at(states, sid, path), after=state)
              for (sid, path), state in sorted(desired.items())]
    if not writes:
        raise commands.CommandError("publication has no invertible authored writes; use timelines restore")
    return dict(op="timeline.inverse", select=None, args=dict(writes=writes, publication=publication))


def stage(checkout, *, client, publication=None):
    from astrid.core.timeline.authoring_bundle import compile_authoring_candidate
    from astrid.sdk.timeline.checkout import Checkout
    from astrid.sdk.timeline.commit import RuntimeCommitService
    from astrid.sdk.timeline.rebase import rebase
    from astrid.sdk.timeline.revisions import RuntimeRevisions
    from astrid.sdk.timeline.saved_intent import read_saved_calls
    from astrid.sdk.timeline.saved_publish import _exact, _workspace

    if read_saved_calls(checkout):
        raise TimelineEditError("publication undo requires a clean working copy")
    project, timeline = checkout.bundle["project_id"], checkout.bundle["timeline_id"]
    records = publications(project, timeline)
    if publication is None:
        own = [(pid, value) for pid, value in records.items() if value["draft"] == Path(checkout.path).stem]
        if not own:
            raise TimelineEditError("no own accepted publication to undo")
        publication = own[-1][0]
    if publication not in records:
        raise TimelineEditError(f"no verified local commands for publication {publication!r}; use timelines restore")
    record = records[publication]
    result, body = record["result"], record["body"]
    workspace = _workspace(client)
    before = _exact(workspace, body, result["previous_revision_id"])
    after = _exact(workspace, body, result["accepted_revision_id"])
    actual_calls = result["change"]["commands"]
    replay, _, _ = RuntimeCommitService._apply(before, actual_calls)
    def payload(document):
        return compile_authoring_candidate(document).publication["parent_composition"]
    if payload(replay) != payload(after):
        raise TimelineEditError("publication commands do not reproduce the exact accepted closure")
    inverse = inverse_command(actual_calls, before, after, publication)
    # Record at the exact post-publication revision. This is the original
    # inverse's base; neither a newer draft nor newer head may redefine it.
    _, plan, _ = RuntimeCommitService._apply(after, [inverse])
    history = RuntimeRevisions(client.timelines).list(project, timeline, limit=200)
    chain = publication_chain(history, publication)
    if chain[-1].id != result["accepted_revision_id"] or chain[-1].parent != result["previous_revision_id"]:
        raise TimelineEditError("publication journal disagrees with Runtime ancestry")
    current = _exact(workspace, body, chain[0].id)
    rebased = rebase(plan.calls, current)
    # Recorded authored writes also catch ABA edits, whose final values happen
    # to equal the original publication. Restore explicitly replaces the closure.
    overlap = None
    for event in chain[:-1]:
        later = records.get(event.publication_id)
        paths = authored_paths((later or {}).get("result", {}).get("change", {}).get("commands", []))
        for write in inverse["args"]["writes"]:
            if event.source == "restore" or any(overlaps((write["id"], write["path"]), path) for path in paths):
                overlap = (event, dict(kind="same-property", id=write["id"], path=write["path"]))
                break
        if overlap:
            break
    if rebased.conflicts or overlap:
        conflict = overlap[1] if overlap else rebased.conflicts[0]
        # Attribute from explicit publication edges, including Runtime restore.
        for event in chain[:-1]:
            left = commands.entity_states(Checkout(_exact(workspace, body, event.parent)))
            right = commands.entity_states(Checkout(_exact(workspace, body, event.id)))
            if (overlap and event is overlap[0]) or not commands._same_value(commands.state_at(left, conflict["id"], conflict["path"]),
                                        commands.state_at(right, conflict["id"], conflict["path"])):
                label = (records.get(event.publication_id, {}).get("body") or {}).get("label")
                raise TimelineEditError(f"{conflict['kind']}: {conflict['id']}.{conflict['path']} conflicts with later "
                                        f"publication {event.publication_id} ({event.source}{': ' + label if label else ''}); "
                                        "use timelines restore for an explicit historical replacement")
        raise TimelineEditError(f"{conflict['kind']}: current publication {chain[0].publication_id}; use timelines restore")
    # Stage against this saved copy's original base; publishing rechecks head
    # through D COMMIT. A clean but unrelated/fresh draft cannot borrow a base.
    local = rebase(plan.calls, checkout.document())
    if local.conflicts:
        raise TimelineEditError("inverse does not apply to this clean working copy; check out the publication or current head")
    label = f"Undo publication {publication}"
    with checkout.edit(label, quiet=True) as tx:
        tx.canonical = True
        for call in plan.calls:
            commands.execute(checkout, call)
    return [label]


def authored_paths(calls):
    return {commands.write_target(key, call.resolved)
            for value in calls for call in [commands.EditCommand.coerce(value)]
            for key, kind in (call.authorship or {}).items() if kind == "direct"}


def overlaps(left, right):
    sid, path = left
    other_sid, other = right
    return sid == other_sid and (not path or not other or path == other or
        path.startswith((other + ".", other + "[")) or other.startswith((path + ".", path + "[")))


def publication_chain(history, target):
    """Current head → named event, solely by the two explicit ancestry links."""
    if not history or history[0].is_current_head is False or not history[0].publication_id:
        raise TimelineEditError("Runtime publication head/ancestry is unavailable")
    by_id = {event.publication_id: event for event in history if event.publication_id}
    chain, node, seen = [], history[0], set()
    while node.publication_id not in seen:
        seen.add(node.publication_id)
        chain.append(node)
        if node.publication_id == target:
            return chain
        previous = by_id.get(node.previous_publication_id)
        if previous is None or node.parent != previous.id:
            break
        node = previous
    raise TimelineEditError(f"publication {target!r} has no explicit ancestry from head; use an exact restore")
