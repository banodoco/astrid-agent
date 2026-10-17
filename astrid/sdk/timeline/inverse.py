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

from astrid.sdk.timeline import commands, journal
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
    if sid.startswith("parent/"):
        members, local = _members(tl, sid)
        matches = [row for row in members if row["id"] == local]
        if len(matches) != 1:
            raise commands.CommandError(f"Unavailable inverse target {sid!r}")
        return matches[0]
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


def _members(tl, sid, *, create=False):
    """The collection named by a recorded parent/occurrence identity, never an alias."""
    scope, sep, member = sid.rpartition("/")
    kind, colon, local = member.partition(":")
    if (not sep or not colon or kind not in {"clip", "track"}
            or not commands.is_scoped_id(sid)
            or (scope != "parent" and not scope.startswith("occurrence:"))):
        raise commands.CommandError(f"inverse root target is not a parent/occurrence clip/track: {sid!r}")
    if scope == "parent":
        if kind == "track":
            return tl._tracks(None, create=create), local
        parent = tl.bundle["parent"]
        return (parent.setdefault("clips", []) if create else parent.get("clips", [])), local
    rows = [r for r in tl.bundle["placements"] if r.get("occurrence_id") == scope[len("occurrence:"):]]
    if len(rows) != 1:
        raise commands.CommandError(f"inverse occurrence is unavailable or ambiguous: {scope}")
    shot_id = rows[0]["shot_id"]
    if kind == "track":
        return tl._tracks(shot_id, create=create), local
    internal = tl.bundle["shots"][shot_id]["internal_timeline"]
    return (internal.setdefault("clips", []) if create else internal.get("clips", [])), local


def _root(tl, write):
    sid, state = write["id"], write["after"]
    if sid.startswith("cut:"):
        from astrid.sdk import timeline_intent as intent
        if state["state"] == "absent":
            # A generated cut need not have authored notes. Undoing its root
            # must not manufacture an empty notes container in that case.
            intent._film(tl.bundle).get("cut_notes", {}).pop(sid[4:], None)
        elif state["value"]:
            intent._film_w(tl.bundle).setdefault("cut_notes", {})[sid[4:]] = copy.deepcopy(state["value"])
        return  # cut existence is owned by its explicitly recorded clip members
    if sid.startswith("occurrence:") and "/" not in sid:
        oid = sid.split(":", 1)[1]
        rows = tl.bundle["placements"]
        if state["state"] == "absent":
            rows[:] = [r for r in rows if r["occurrence_id"] != oid]
            return
        payload = copy.deepcopy(state["value"])
        if not isinstance(payload, dict) or payload.get("occurrence_id") != oid:
            raise commands.CommandError("inverse occurrence identity mismatch")
        from astrid.sdk.timeline.graph_commands import _source, install_source, source_ref
        ref = source_ref(write.get("source"))
        _source(tl, ref)  # request-bound authority even when a local shot survives
        shot_id = payload.get("shot_id")
        existing = tl.bundle["source_mapping"]["shots"].get(shot_id)
        if existing is not None:
            if (existing["shot_id"], existing["revision_id"], existing["internal_timeline_revision_id"]) != (ref["shot_id"], ref["shot_revision_id"], ref["internal_revision_id"]):
                if any(r["occurrence_id"] != oid and r["shot_id"] == shot_id for r in rows):
                    raise commands.CommandError("inverse occurrence source identity collision")
                del tl.bundle["shots"][shot_id]
                del tl.bundle["source_mapping"]["shots"][shot_id]
                install_source(tl, ref, oid, shot_id=shot_id)
        else:
            install_source(tl, ref, oid, shot_id=shot_id)
        rows[:] = [r for r in rows if r["occurrence_id"] != oid]
        rows.append(payload)
        order = write.get("order", [])
        if order:
            if (not isinstance(order, list) or any(not isinstance(key, str) or not re.fullmatch(r"occurrence:[^/]+", key) for key in order)
                    or len(order) != len(set(order)) or sid not in order):
                raise commands.CommandError("inverse occurrence order has invalid scoped identities")
            positions = {key: i for i, key in enumerate(order)}
            rows.sort(key=lambda r: positions.get("occurrence:" + r["occurrence_id"], len(positions)))
        return
    members, local = _members(tl, sid, create=state["state"] == "present")
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
    # A recorded absent basis is the unenrolled state of a legacy source.
    # Outputs/projection belong to the removed enrollment, not to the clip.
    # Only that guarded transition may discard them; adopted BED authority
    # still requires its complete metadata and stable reference owner.
    unenrolling = {w["id"] for w in writes
                   if w["path"] == "music_bed.ripple_basis" and w["after"]["state"] == "absent"}
    for sid in unenrolling:
        data = desired.get(sid, {})
        meta = data.get("music_bed", {})
        if "arrangements" not in meta and "ripple_basis" not in meta:
            data.pop("music_bed", None)
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
        if not matches and sid not in states and any(w["id"] == sid and w["path"] == "" and w["after"]["state"] == "present" for w in writes):
            # Restore a removed authored owner solely from its guarded root
            # write. The compiler recreates any derived outputs afterward.
            clips.append(copy.deepcopy(source))
            matches = [clips[-1]]
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
            # Redo of first enrollment records the basis, not derived outputs.
            # The restored ordinary source is the sole initial physical member;
            # I's projector allocates all output IDs from that authored source.
            enrolling = sid not in carriers and any(
                w["id"] == sid and w["path"] == "music_bed.ripple_basis"
                and w["before"]["state"] == "absent" and w["after"]["state"] == "present"
                for w in writes)
            outputs = [source['id']] if enrolling else meta['outputs']
            ns.setdefault('music_beds', {})[sid] = dict(owner=sid, source=source,
                ripple_basis=copy.deepcopy(meta['ripple_basis']), outputs=copy.deepcopy(outputs))
    # Collection presence is restored by the recorded parent write, after the
    # owner-scoped transfer. An empty collection may be explicitly authored.
    return handled



def _parent_write(tl, path, state):
    """Restore a parent property without overwriting owner-scoped carriers.

    The semantic parent records only collection presence. Its source members
    have already been restored by _music_writes, under their own exact guards.
    """
    collection = ["config", "app", "astrid", "music_beds"]
    keys = _tokens(path)
    if keys == collection[:len(keys)]:
        from astrid.sdk.timeline import music_beds
        carriers = music_beds.collection(tl)
        value = copy.deepcopy(state)
        node = value.get("value") if value["state"] == "present" else None
        remaining = collection[len(keys):]
        for key in remaining:
            node = node.get(key) if isinstance(node, dict) else None
        if node == {}:
            # {} here is a recorded shell, not a replacement for the members.
            if remaining:
                owner = value["value"]
                for key in remaining[:-1]:
                    owner = owner[key]
                owner[remaining[-1]] = copy.deepcopy(carriers)
            else:
                value["value"] = copy.deepcopy(carriers)
        elif carriers:
            raise commands.SemanticConflict("dependent", "parent", path,
                                            state, {"state": "present", "value": copy.deepcopy(carriers)}, state)
        state = value
    _write(tl.bundle["parent"], path, state)


def apply_inverse(self, writes, publication):
    """Apply guarded authored properties, then let the normal compiler resolve consequences."""
    tl = self
    if not isinstance(publication, str) or not publication or not isinstance(writes, list) or not writes:
        raise commands.CommandError("inverse requires a named publication and authored writes")
    seen = set()
    states = commands.entity_states(tl)
    for write in writes:
        if not isinstance(write, dict) or set(write) - {"id", "path", "before", "after", "order", "source", "group_containers"} or not {
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
    # Keep the exact guarded owner before a collection-absence write can remove
    # its storage. Delete tracks only after the inverse has removed/moved its
    # own clips; any remaining authored users belong to work it cannot discard.
    deleted_tracks = {w["id"]: (w, commands.track_owner(tl, w["id"])) for w in writes
                      if not w["path"] and "/track:" in w["id"]
                      and w["before"]["state"] == "present" and w["after"]["state"] == "absent"}
    handled = _music_writes(tl, writes, states)
    for index, write in enumerate(writes):
        if index in handled:
            continue
        path = write["path"]
        path = "app.astrid." + path[7:] if path.startswith("intent.") else path
        try:
            from astrid.sdk.timeline.shot_groups import restore_entity
            if restore_entity(tl, write):
                continue
            if path == "track_order" and "/track:" in write["id"]:
                continue  # restore collection members before their guarded order
            elif not path:
                if write["id"] not in deleted_tracks:
                    _root(tl, write)
            elif write["id"] == "parent" and path == "storage_policy":
                if write["after"]["state"] == "absent":
                    tl.bundle.pop("storage_policy", None)
                elif write["after"]["value"] == "chapters":
                    tl.bundle["storage_policy"] = "chapters"
                else:
                    raise commands.CommandError("invalid inverse storage policy")
            elif write["id"] == "parent":
                _parent_write(tl, path, write["after"])
            else:
                _write(_entity(tl, write["id"]), path, write["after"])
        except (KeyError, IndexError, TypeError, AttributeError) as exc:
            raise commands.CommandError(f"inverse path is unavailable: {write['id']}.{path}") from exc
    for write, owner in deleted_tracks.values():
        users = commands.track_users(tl, owner)
        if owner.shot_id is None:
            # Child clips (including zero-output music sources) can inherit a
            # canonical parent lane without a duplicate child track row.
            current = commands.entity_states(tl)
            users.extend(sid for sid, payload in current.items()
                         if sid.startswith("occurrence:") and "/clip:" in sid
                         and payload.get("track") == owner.id
                         and sid.split("/clip:", 1)[0] + "/track:" + owner.id not in current)
        if users:
            raise commands.CommandError(f"Track {owner.scoped_id} is occupied by {', '.join(sorted(set(users)))}; inverse rolled back")
        _root(tl, write)
    from astrid.sdk.timeline.transcripts import validate_bound_dependencies
    validate_bound_dependencies(tl)
    for write in writes:
        if write["path"] != "track_order" or "/track:" not in write["id"]:
            continue
        state = write["after"]
        if state["state"] != "present" or not isinstance(state["value"], list) or not state["value"]:
            raise commands.CommandError("inverse track order must be a complete present permutation")
        # All before guards were checked before any mutation. Root restoration
        # above is the intended preceding batch state for this final permutation.
        owner = commands.track_owner(tl, state["value"][0])
        actual = commands.state_at(commands.entity_states(tl), owner.scoped_id, "track_order")["value"]
        commands.reorder_tracks(owner, order=state["value"], expected_order=actual)
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
    presence_guards = set()
    for index, value in enumerate(calls):
        call = commands.EditCommand.coerce(value)
        if any(getattr(call, key) is None for key in ("expect", "authorship", "bindings", "created")):
            raise commands.CommandError("publication lacks recorded command preconditions")
        presence_guards.update(commands._position_reset_guards(call))
        if call.op in {"timeline.reflow", "timeline.retime", "timeline.resolve"}:
            materializers.add(index)
        for key, state in call.expect.items():
            sid, path = commands.write_target(key, call.resolved)
            if (path == "music_bed" and (call.op == "timeline.inverse" or
                    (call.op == "timeline.adopt" and call.args.get("sources")))) or path == "music_bed.ripple_basis":
                desired.setdefault((sid, path), copy.deepcopy(state))
            if (call.op == "timeline.add" and isinstance(call.args.get("track"), Mapping)
                    and call.select.startswith("parent/track:") and sid == "parent"
                    and path == "config.tracks" and state == {"state": "absent"}):
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
    created_tracks = {sid for value in calls for sid in commands.EditCommand.coerce(value).created if "/track:" in sid}
    for (sid, path), state in list(desired.items()):
        if path != "track_order":
            continue
        if state["state"] != "present" or not isinstance(state["value"], list):
            raise commands.CommandError("publication lacks a captured track permutation")
        captured = [key for key in state["value"] if key not in created_tracks]
        original_order = commands.state_at(original, sid, path)["value"]
        # Added rows were guarded absent; deleted rows carry guarded payloads.
        # The exact closure supplies their placement just as _root's order does.
        if captured != [key for key in original_order if key in captured] or any(
                key not in captured and desired.get((key, ""), {}).get("state") != "present" for key in original_order):
            raise commands.CommandError("track permutation cannot reconstruct guarded original membership")
        desired[(sid, path)] = dict(state="present", value=original_order)
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
    # Creating/replacing an occurrence owns its whole child closure. Child
    # membership is a consequence of that one root, not separate inverse roots.
    graph_roots = {sid for sid, path in desired if sid.startswith("occurrence:") and "/" not in sid and not path}
    desired = {(sid, path): value for (sid, path), value in desired.items()
               if not any(sid.startswith(root + "/") for root in graph_roots)}
    # Reconstruct overlapping guards from their recorded tagged states. The
    # exact before closure validates this reconstruction; it never supplies a
    # missing operation, identity or deleted payload.
    order_of_guard = {key: index for index, key in enumerate(desired)}
    for sid, path in list(desired):
        parents = [other for other_sid, other in desired if other_sid == sid and other != path and not (not other and (path.startswith("music_bed") or path == "track_order")) and
                   (not other or path.startswith((other + ".", other + "[")))]
        if parents:
            parent = min(parents, key=len)
            state = desired[(sid, parent)]
            if state["state"] == "present" and order_of_guard[(sid, path)] < order_of_guard[(sid, parent)]:
                relative = path[len(parent):].lstrip(".") if parent else path
                _write(state["value"], relative, desired[(sid, path)])
    desired = {(sid, path): state for (sid, path), state in desired.items()
               if not any(other_sid == sid and other != path and not (not other and (path.startswith("music_bed") or path == "track_order")) and
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
    writes = [w for w in writes if (w["id"], w["path"]) in presence_guards or
              not (w["before"] == w["after"] == {"state": "absent"})]
    from astrid.sdk.timeline.shot_groups import PREFIX, groups, identity
    group_order = [identity({k: r[k] for k in ("shotId", "trackId")}) for r in groups(original_tl)]
    for write in writes:
        if write["id"].startswith(PREFIX):
            write["order"] = group_order
            write["group_containers"] = dict(config="config" in before["parent"], groups="pinnedShotGroups" in before["parent"].get("config", {}))
        if not write["path"] and write["id"].startswith("occurrence:") and "/" not in write["id"] and write["after"]["state"] == "present":
            oid = write["id"].split(":", 1)[1]
            row = next(r for r in before["placements"] if r["occurrence_id"] == oid)
            src = before["source_mapping"]["shots"][row["shot_id"]]
            write["source"] = dict(parent_revision_id=before["base_parent"]["revision_id"], occurrence_id=oid,
                                   shot_id=src["shot_id"], shot_revision_id=src["revision_id"],
                                   internal_revision_id=src["internal_timeline_revision_id"])
            write["order"] = ["occurrence:" + r["occurrence_id"] for r in before["placements"]]
        if write["path"] == "" and write["after"]["state"] == "present" and ("/clip:" in write["id"] or "/track:" in write["id"]):
            members, _ = _members(original_tl, write["id"])
            prefix = write["id"].rsplit(":", 1)[0] + ":"
            write["order"] = [prefix + row["id"] for row in members]
    if not writes:
        raise commands.CommandError("publication has no invertible authored writes; use timelines restore")
    value = dict(op="timeline.inverse", select=None, args=dict(writes=writes, publication=publication))
    if graph_roots:
        # A graph root owns its child closure. Deleting/replacing it must not
        # discard an intervening child edit whose placement row stayed equal.
        value["expect"] = {sid + "::": dict(state="present", value=copy.deepcopy(payload))
                           for sid, payload in states.items()
                           if any(sid.startswith(root + "/") for root in graph_roots)}
    return value


def stage(checkout, *, client, publication=None):
    from astrid.sdk.timeline.chapter_storage import compile_candidate as compile_authoring_candidate
    from astrid.sdk.timeline.checkout import Checkout
    from astrid.sdk.timeline.commit import RuntimeCommitService
    from astrid.sdk.timeline.rebase import rebase
    from astrid.sdk.timeline.revisions import RuntimeRevisions
    from astrid.sdk.timeline.saved_intent import read_saved_calls
    from astrid.sdk.timeline.saved_publish import _ClosurePort, _exact, _workspace

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
    from astrid.sdk.timeline.source_hydration import hydrate_sources
    from astrid.sdk.timeline.transcripts import base_selections
    # Both revisions were just read through the authenticated exact closure.
    # Their bindings, unlike journal text, can authorize CAS-only old sources.
    accepted_transcripts = tuple(selection.descriptor() for document in (before, after)
        for selection in base_selections(project, document).values())
    source_imports, pinned_shots = hydrate_sources(
        _ClosurePort(workspace, project, timeline, accepted_transcripts=accepted_transcripts), body, before,
        result["change"]["commands"], parent_loader=lambda rid: _exact(workspace, body, rid))
    if source_imports:
        before["source_mapping"]["imports"] = copy.deepcopy(source_imports)
        after["source_mapping"]["imports"] = copy.deepcopy(source_imports)
    if pinned_shots:
        before["source_mapping"]["pinned_shots"] = copy.deepcopy(pinned_shots)
        after["source_mapping"]["pinned_shots"] = copy.deepcopy(pinned_shots)
    pinned_transcripts = copy.deepcopy(before["source_mapping"].get("pinned_transcripts", {}))
    if pinned_transcripts:
        after["source_mapping"]["pinned_transcripts"] = copy.deepcopy(pinned_transcripts)
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
    if source_imports:
        current["source_mapping"]["imports"] = copy.deepcopy(source_imports)
    if pinned_shots:
        current["source_mapping"]["pinned_shots"] = copy.deepcopy(pinned_shots)
    if pinned_transcripts:
        current["source_mapping"]["pinned_transcripts"] = copy.deepcopy(pinned_transcripts)
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
    local_document = checkout.document()
    if source_imports:
        local_document["source_mapping"]["imports"] = copy.deepcopy(source_imports)
    if pinned_transcripts:
        local_document["source_mapping"]["pinned_transcripts"] = copy.deepcopy(pinned_transcripts)
    if pinned_shots:
        local_document["source_mapping"]["pinned_shots"] = copy.deepcopy(pinned_shots)
    local = rebase(plan.calls, local_document)
    if local.conflicts:
        raise TimelineEditError("inverse does not apply to this clean working copy; check out the publication or current head")
    label = f"Undo publication {publication}"
    with checkout.edit(label, quiet=True) as tx:
        if source_imports:
            checkout.bundle["source_mapping"]["imports"] = copy.deepcopy(source_imports)
        if pinned_transcripts:
            checkout.bundle["source_mapping"]["pinned_transcripts"] = copy.deepcopy(pinned_transcripts)
        if pinned_shots:
            checkout.bundle["source_mapping"]["pinned_shots"] = copy.deepcopy(pinned_shots)
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
