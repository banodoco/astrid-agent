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


def _members(tl, sid):
    """The collection named by a recorded occurrence identity, never an alias."""
    scope, sep, member = sid.rpartition("/")
    kind, colon, local = member.partition(":")
    if not sep or not colon or kind not in {"clip", "track"} or not scope.startswith("occurrence:"):
        raise commands.CommandError(f"inverse root target is not an occurrence clip/track: {sid!r}")
    rows = [r for r in tl.bundle["placements"] if r.get("occurrence_id") == scope[len("occurrence:"):]]
    if len(rows) != 1:
        raise commands.CommandError(f"inverse occurrence is unavailable or ambiguous: {scope}")
    return tl.bundle["shots"][rows[0]["shot_id"]]["internal_timeline"].setdefault(kind + "s", []), local


def _root(tl, write):
    sid, state = write["id"], write["after"]
    if sid.startswith("cut:"):
        from astrid.sdk import timeline_intent as intent
        notes = intent._film_w(tl.bundle).setdefault("cut_notes", {})
        if state["state"] == "absent":
            notes.pop(sid[4:], None)
        elif state["value"]:
            notes[sid[4:]] = copy.deepcopy(state["value"])
        return  # cut existence is owned by its explicitly recorded clip members
    members, local = _members(tl, sid)
    if state["state"] == "absent":
        members[:] = [row for row in members if row["id"] != local]
        return
    payload = state["value"]
    if not isinstance(payload, dict) or payload.get("id") != local:
        raise commands.CommandError(f"inverse payload identity does not match {sid}")
    members[:] = [row for row in members if row["id"] != local]
    order = write.get("order", [])
    if order:
        if len(order) != len(set(order)) or sid not in order or any(
                not isinstance(value, str) or value.rsplit(":", 1)[0] != sid.rsplit(":", 1)[0] for value in order):
            raise commands.CommandError("inverse membership order has invalid scoped identities")
        position = order.index(sid)
        present = {row["id"]: index for index, row in enumerate(members)}
        before = [present[value.rsplit(":", 1)[1]] for value in order[:position] if value.rsplit(":", 1)[1] in present]
        after = [present[value.rsplit(":", 1)[1]] for value in order[position + 1:] if value.rsplit(":", 1)[1] in present]
        if before and after and max(before) >= min(after):
            raise commands.SemanticConflict("dependent", sid, "order", order, list(present), payload)
        index = min(after) if after else max(before) + 1 if before else 0
    else:
        index = len(members)
    members.insert(index, copy.deepcopy(payload))


def _music_writes(tl, writes, states):
    """Translate the existing owner-scoped semantic view back to I's authority.

    The transaction owns rollback. Source dictionaries remain ordinary clips;
    generated output and checked projections are rebuilt by I, never restored
    as another writable source.
    """
    from astrid.sdk.timeline import music_beds
    carriers = music_beds.sources(tl)
    affected = {w["id"] for w in writes if w["id"] in carriers or
                w["path"] == "music_bed" or w["path"].startswith("music_bed.")}
    if not affected:
        return set()
    music_beds.validate(tl)
    desired = copy.deepcopy(states)
    handled = set()
    for index, write in enumerate(writes):
        if write["id"] not in affected:
            continue
        handled.add(index)
        sid, path, state = write["id"], write["path"], write["after"]
        if not path:
            if state["state"] == "absent":
                desired.pop(sid, None)
            else:
                metadata = desired.get(sid, {}).get("music_bed")
                desired[sid] = copy.deepcopy(state["value"])
                if metadata is not None:
                    desired[sid]["music_bed"] = metadata
        else:
            path = "app.astrid." + path[7:] if path.startswith("intent.") else path
            _write(desired[sid], path, state)
    music_beds.dematerialize(tl)
    ns = tl.bundle['parent']['config'].setdefault('app', {}).setdefault('astrid', {})
    ns.pop('bed', None)
    if 'music_beds' in ns:
        ns['music_beds'] = {}
    # Every formerly managed source is now present as an ordinary clip. This
    # also makes removing the last metadata field an exact storage transfer.
    restored_sources = {}
    referenced = {a['owner'] for data in desired.values() if isinstance(data, dict)
                  for a in data.get('music_bed', {}).get('arrangements', [])}
    for sid in affected | set(carriers) | referenced:
        if sid not in desired:
            raise commands.CommandError('remove adopted sources through their authored owner')
        source = copy.deepcopy(desired[sid])
        source.pop('music_bed', None)
        _, clips = music_beds.locate(tl, sid, source)
        matches = [c for c in clips if c.get('id') == source['id']]
        if len(matches) != 1:
            raise commands.CommandError(f'{sid}: inverse source is unavailable or ambiguous')
        matches[0].clear()
        matches[0].update(source)
        restored_sources[sid] = matches[0]
    for sid, data in desired.items():
        meta = data.get('music_bed') if isinstance(data, dict) else None
        if not meta:
            continue
        if 'arrangements' in meta:
            if 'bed' in ns or meta.get('owner') != sid:
                raise commands.CommandError('inverse requires one stable bed reference owner')
            arrangements = []
            for ref in meta['arrangements']:
                source = restored_sources[ref['owner']]
                arrangements.append({**ref, 'source': source, **meta['projections'][ref['id']]})
            ns['bed'] = dict(owner=sid, arrangements=arrangements,
                            switches=copy.deepcopy(meta['switches']),
                            ripple_basis=copy.deepcopy(meta['ripple_basis']),
                            outputs=copy.deepcopy(meta['outputs']))
        else:
            source = restored_sources[sid]
            ns.setdefault('music_beds', {})[sid] = dict(owner=sid, source=source,
                ripple_basis=copy.deepcopy(meta['ripple_basis']), outputs=copy.deepcopy(meta['outputs']))
    return handled


def apply_inverse(self, writes, publication):
    """Apply guarded authored properties, then let the normal compiler resolve consequences."""
    tl = self
    if not isinstance(publication, str) or not publication or not isinstance(writes, list) or not writes:
        raise commands.CommandError("inverse requires a named publication and authored writes")
    seen = set()
    states = commands.entity_states(tl)
    for write in writes:
        if not isinstance(write, dict) or set(write) - {"id", "path", "before", "after", "order"} or not {
                "id", "path", "before", "after"} <= set(write):
            raise commands.CommandError("invalid inverse write")
        sid, path = write["id"], write["path"]
        if not isinstance(sid, str) or not isinstance(path, str):
            raise commands.CommandError("inverse requires a recorded identity and property path")
        keys = _tokens(path) if path else []
        # Identity/provenance is never editable through a property inverse.
        if keys and keys[0] in {"id", "shot_id", "occurrence_id", "shot_revision_id"}:
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
    from astrid.sdk.timeline.identity import cut_reservations, retain_cut_reservation_mark
    reservations = cut_reservations(tl.bundle)
    handled = _music_writes(tl, writes, states)
    for index, write in enumerate(writes):
        if index in handled:
            continue
        path = write["path"]
        path = "app.astrid." + path[7:] if path.startswith("intent.") else path
        try:
            if not path:
                _root(tl, write)
            else:
                _write(_entity(tl, write["id"]), path, write["after"])
        except (KeyError, IndexError, TypeError, AttributeError) as exc:
            raise commands.CommandError(f"inverse path is unavailable: {write['id']}.{path}") from exc
    retain_cut_reservation_mark(tl.bundle, reservations)
    tl._refresh()
    if handled:
        # Other inverse writes may restore clocks/anchors consumed by the bed.
        # Rebuild once after all authored inputs are restored, in this transaction.
        from astrid.sdk.timeline import music_beds
        from astrid.sdk.timeline.bed_adoption import refresh_projections
        refresh_projections(tl)
        music_beds.project(tl)
        music_beds.validate(tl)


def inverse_command(calls, before, after, publication, *, consequences=()):
    """Coalesce direct authored preconditions: first before, final after.

    Compiler consequences are deliberately recomputed. Repeated writes to one
    path and parent/child writes collapse to their first precondition, avoiding
    intermediate inverse compilation and preserving presence versus null.
    """
    from astrid.sdk.timeline.checkout import Checkout
    desired = {}
    materializers = set()
    for index, value in enumerate(calls):
        call = commands.EditCommand.coerce(value)
        if any(getattr(call, key) is None for key in ("expect", "authorship", "bindings", "created")):
            raise commands.CommandError("publication lacks recorded command preconditions")
        if call.op in {"timeline.reflow", "timeline.retime", "timeline.resolve"}:
            materializers.add(index)
        for key, state in call.expect.items():
            sid, path = commands.write_target(key, call.resolved)
            if (path == "music_bed" and (call.op == "timeline.inverse" or
                    (call.op == "timeline.adopt" and call.args.get("sources")))) or path == "music_bed.ripple_basis":
                desired.setdefault((sid, path), copy.deepcopy(state))
        for key, kind in call.authorship.items():
            if kind != "direct":
                continue
            if key not in call.expect:
                raise commands.CommandError("publication lacks an authored precondition")
            sid, path = commands.write_target(key, call.resolved)
            desired.setdefault((sid, path), copy.deepcopy(call.expect[key]))
    original_tl = Checkout(copy.deepcopy(before))
    original = commands.entity_states(original_tl)
    for effect in consequences:
        if effect.get("cause") not in materializers:
            continue
        sid, path = effect["id"], effect["path"]
        old = original.get(sid, {})
        if path in {"at", "hold"} and old.get("app", {}).get("astrid", {}).get("on"):
            continue  # anchored clocks are recomputed from the restored authored inputs
        desired.setdefault((sid, path), copy.deepcopy(effect["before"]))
    # Every created entity carries a scoped ID. A following set/remove can
    # consume it; its absence before this publication still owns the inverse.
    for value in calls:
        call = commands.EditCommand.coerce(value)
        for sid in call.created:
            desired.setdefault((sid, ""), dict(state="absent"))
    # Reconstruct overlapping guards from their recorded tagged states. The
    # exact before closure validates this reconstruction; it never supplies a
    # missing operation, identity or deleted payload.
    order_of_guard = {key: index for index, key in enumerate(desired)}
    for sid, path in list(desired):
        parents = [other for other_sid, other in desired if other_sid == sid and other != path and
                   (not other or path.startswith((other + ".", other + "[")))]
        if parents:
            parent = min(parents, key=len)
            state = desired[(sid, parent)]
            if state["state"] == "present" and order_of_guard[(sid, path)] < order_of_guard[(sid, parent)]:
                relative = path[len(parent):].lstrip(".") if parent else path
                _write(state["value"], relative, desired[(sid, path)])
    desired = {(sid, path): state for (sid, path), state in desired.items()
               if not any(other_sid == sid and other != path and
                   (not other or path.startswith((other + ".", other + "["))) for other_sid, other in desired)}
    for (sid, path), state in desired.items():
        if not commands._same_value(commands.state_at(original, sid, path), state):
            raise commands.CommandError(f"recorded inverse precondition does not reconstruct {sid}.{path}")
    # Reservation maxima belong to IDENTITY, not reversible authored content.
    desired = {(sid, path): state for (sid, path), state in desired.items() if not (
        sid == "parent" and path.startswith("config.app.astrid.cut_id_high_water"))}
    states = commands.entity_states(Checkout(copy.deepcopy(after)))
    writes = [dict(id=sid, path=path, before=commands.state_at(states, sid, path), after=state)
              for (sid, path), state in sorted(desired.items())]
    writes = [w for w in writes if not (w["before"] == w["after"] == {"state": "absent"})]
    for write in writes:
        if write["path"] == "" and write["after"]["state"] == "present" and "/" in write["id"]:
            members, _ = _members(original_tl, write["id"])
            prefix = write["id"].rsplit(":", 1)[0] + ":"
            write["order"] = [prefix + row["id"] for row in members]
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
    inverse = inverse_command(actual_calls, before, after, publication, consequences=result["change"]["consequences"])
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
            for key, kind in (call.authorship or {}).items() if kind == "direct" or call.op in {
                "timeline.reflow", "timeline.retime", "timeline.resolve"}}


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
