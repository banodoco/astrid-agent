"""Lossless bundle-to-``doc.patch`` translation.

The emitter is deliberately a small, structural adapter.  It does not execute
patches, resolve closure aliases, or guess semantic commands; those jobs belong
to the provider.  In particular, changes to closure membership and provenance
are refused instead of being represented as a root replacement.
"""
from __future__ import annotations

import copy
from collections.abc import Mapping
from typing import Any

from ._command_state import present
from ._common import TimelineEditError


class DocumentPatchError(TimelineEditError):
    """A document change cannot be represented as a safe generic patch."""


_MISSING = object()


def _same(left: Any, right: Any) -> bool:
    from .commit import _json

    if left is _MISSING or right is _MISSING:
        return left is right
    return _json(left) == _json(right)


def _state(value: Any) -> dict[str, Any]:
    if value is _MISSING:
        return {"state": "absent"}
    return present(value)


def _path(path: tuple[str, ...] | list[str]) -> list[str]:
    if not all(isinstance(token, str) for token in path):
        raise DocumentPatchError("doc.patch paths contain string tokens only")
    return list(path)


def _change(op: str, ref: str, path: tuple[str, ...], before: Any, value: Any = _MISSING) -> dict[str, Any]:
    change: dict[str, Any] = {"op": op, "ref": ref, "path": _path(path), "before": _state(before)}
    if op == "set":
        if value is _MISSING:
            raise DocumentPatchError("doc.patch set requires a value")
        change["value"] = copy.deepcopy(value)
    return change


def _list_changes(before: list[Any], after: list[Any], ref: str, path: tuple[str, ...], out: list[dict[str, Any]]) -> None:
    from ._doc_patch import identities, identity_key

    key = identity_key(ref, path)
    if key is None:
        out.append(_change("set", ref, path, before, after))
        return
    identities(before, key, ref=ref, path=path)
    identities(after, key, ref=ref, path=path)

    old = {row[key]: row for row in before}
    new = {row[key]: row for row in after}
    current = list(before)
    current_ids = [row[key] for row in current]
    target_ids = [row[key] for row in after]

    # Remove from the end so every order_before is the exact sequential order.
    for identity in reversed(current_ids):
        if identity not in new:
            order = [row[key] for row in current]
            out.append({"op": "list.remove", "ref": ref, "path": _path(path), "id": identity,
                        "before": present(copy.deepcopy(old[identity])), "order_before": order})
            current.pop(current_ids.index(identity))
            current_ids.remove(identity)

    # Insert in target order.  The predecessor is already present because the
    # target prefix is built left-to-right.
    for index, identity in enumerate(target_ids):
        if identity in current_ids:
            continue
        order = [row[key] for row in current]
        after_id = target_ids[index - 1] if index else None
        out.append({"op": "list.insert", "ref": ref, "path": _path(path), "id": identity,
                    "before": {"state": "absent"}, "order_before": order,
                    "after_id": after_id, "value": copy.deepcopy(new[identity])})
        insert_at = current_ids.index(after_id) + 1 if after_id is not None else 0
        current.insert(insert_at, copy.deepcopy(new[identity]))
        current_ids.insert(insert_at, identity)

    # Then make order match.  Move operations compare the complete item at its
    # current position, not the target item after a later content edit.
    for index, identity in enumerate(target_ids):
        if current_ids[index] == identity:
            continue
        old_index = current_ids.index(identity)
        row = copy.deepcopy(current[old_index])
        order = list(current_ids)
        out.append({"op": "list.move", "ref": ref, "path": _path(path), "id": identity,
                    "before": present(row), "order_before": order,
                    "after_id": target_ids[index - 1] if index else None})
        moved = current.pop(old_index)
        current_ids.pop(old_index)
        insert_at = current_ids.index(target_ids[index - 1]) + 1 if index else 0
        current.insert(insert_at, moved)
        current_ids.insert(insert_at, identity)

    # Compare item contents after structural operations.  The identity token is
    # not writable, so it is deliberately omitted from the recursive path.
    for identity in target_ids:
        if identity not in old:
            continue
        _diff(old[identity], new[identity], ref,
              path + (f"{key}:{identity}",), out)


def _diff(before: Any, after: Any, ref: str, path: tuple[str, ...], out: list[dict[str, Any]]) -> None:
    if _same(before, after):
        return
    if before is _MISSING or after is _MISSING:
        if after is _MISSING:
            out.append(_change("remove", ref, path, before))
        else:
            out.append(_change("set", ref, path, before, after))
        return
    if isinstance(before, Mapping) and isinstance(after, Mapping):
        for key in sorted(set(before) | set(after)):
            if not isinstance(key, str):
                raise DocumentPatchError("doc.patch object keys must be strings")
            _diff(before.get(key, _MISSING), after.get(key, _MISSING), ref,
                  path + (key,), out)
        return
    if isinstance(before, list) and isinstance(after, list):
        _list_changes(before, after, ref, path, out)
        return
    out.append(_change("set", ref, path, before, after))


def _outside(bundle: Mapping[str, Any]) -> dict[str, Any]:
    value = copy.deepcopy(dict(bundle))
    value.pop("parent", None)
    shots = value.get("shots")
    if isinstance(shots, dict):
        for shot in shots.values():
            if isinstance(shot, dict):
                shot.pop("payload", None)
                shot.pop("internal_timeline", None)
    return value


def _payload_for_emit(ref: str, payload: Any) -> Any:
    """Validate schema collections before hiding redundant parent mirrors."""
    from ._doc_patch import validate_payload

    validate_payload(ref, payload)
    if ref != "parent":
        return payload
    value = copy.deepcopy(dict(payload))
    config = value.get("config")
    if isinstance(config, dict):
        # Project only the live list; retain the caller's exact raw null mirror.
        if value.get("clips", _MISSING) is None and isinstance(config.get("clips"), list):
            value.pop("clips")
        elif config.get("clips", _MISSING) is None and isinstance(value.get("clips"), list):
            config.pop("clips")
    if isinstance(config, dict):
        for key in ("clips", "registry"):
            if key in value and key in config:
                config.pop(key)
    return value


def _root_pairs(before: Mapping[str, Any], after: Mapping[str, Any]):
    from astrid.core.timeline.authoring_bundle import _remap_known_item_references

    from ._doc_patch import Closure

    yield "parent", before["parent"], after["parent"]
    changed = any(not _same(before["shots"][key][field], after["shots"][key][field])
                  for key in before["shots"] for field in ("payload", "internal_timeline"))
    # Detached bundles can still express parent-only patches. Child edits need
    # the source-bound physical resolver, never an entity/timeline-ID fallback.
    closure = Closure(before) if changed else None
    roots: dict[str, tuple[Any, Any]] = {}
    source = before.get("source_mapping", {}).get("shots", {})
    for local, shot in before["shots"].items():
        other = after["shots"][local]
        for field, kind in (("payload", "shot"), ("internal_timeline", "internal")):
            old, new = shot[field], other[field]
            if closure is None:
                # Validate even unchanged known collections.
                _payload_for_emit("shot:" if kind == "shot" else "internal:", old)
                continue
            origin = source.get(local, {})
            # A preceding semantic command can materialize a successor. Resolve
            # that sequential physical root through its verified owner projection.
            refs = [ref for ref, owners in closure.owners.items()
                    if ref.startswith(kind + "-revision:")
                    and any(owner_local == local for _, owner_local in owners)]
            if len(refs) != 1:
                raise DocumentPatchError(f"shot {local!r} has no unique verified {kind} revision")
            ref = refs[0]
            if kind == "shot":
                mapping = {v: k for k, v in origin.get("item_id_map", {}).items()}
                old = _remap_known_item_references(old, mapping)
                new = _remap_known_item_references(new, mapping)
            pair = (old, new)
            if ref in roots and any(not _same(left, right) for left, right in zip(roots[ref], pair)):
                raise DocumentPatchError(f"closure root {ref!r} has contradictory authoring aliases; use a semantic command")
            roots[ref] = pair
    for ref, (old, new) in roots.items():
        yield ref, old, new


def _created(changes: list[dict[str, Any]], before: Mapping[str, Any]) -> list[str]:
    """Use R1's allocation rules on introduced subtrees, in change order."""
    from ._doc_patch import Closure, allocations, identity_key

    closure = None
    created: list[str] = []
    for change in changes:
        if change["op"] not in ("set", "list.insert"):
            continue
        ref, path = change["ref"], change["path"]
        if any(identity_key(ref, path[:index]) for index in range(len(path))):
            continue  # Fields inside existing items cannot introduce schema entities.
        if ref != "parent" and closure is None:
            closure = Closure(before)
        old = change["before"].get("value", _MISSING)
        new = change["value"]
        if change["op"] == "list.insert":
            old, new = [], [new]
        for token in reversed(path):
            old = {} if old is _MISSING else {token: old}
            new = {token: new}
        if ref == "parent":
            old.setdefault("config", {})
            new.setdefault("config", {})
        created.extend(row["ref"] for row in allocations(closure, ref, old, new))
    return created


def emit_document_commands(before_bundle: Mapping[str, Any], after_bundle: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Emit guarded payload changes, bound to the verified input closure.

    R1 owns physical resolution, schema identities, managed edges and allocation
    rules. Unknown arrays remain whole values; unknown object keys stay literal.
    This adapter never infers what a free transform read.
    """
    from ._command_state import CommandError
    from ._doc_patch import managed_edges

    if not isinstance(before_bundle, Mapping) or not isinstance(after_bundle, Mapping):
        raise DocumentPatchError("document emitter requires mapping bundles")
    if before_bundle.get("project_id") != after_bundle.get("project_id") or before_bundle.get("timeline_id") != after_bundle.get("timeline_id"):
        raise DocumentPatchError("document and Runtime bundle have different project/timeline scope")
    for bundle in (before_bundle, after_bundle):
        if not isinstance(bundle.get("parent"), Mapping) or not isinstance(bundle.get("shots"), Mapping):
            raise DocumentPatchError("timeline bundle must contain a parent and shot map")
        for local, shot in bundle["shots"].items():
            if not isinstance(local, str) or not isinstance(shot, Mapping) or any(
                    not isinstance(shot.get(field), Mapping) for field in ("payload", "internal_timeline")):
                raise DocumentPatchError("timeline shot roots must have object payloads")
    if not _same(_outside(before_bundle), _outside(after_bundle)):
        raise DocumentPatchError("closure membership or provenance changed; use a semantic command")

    changes: list[dict[str, Any]] = []
    try:
        for ref, old, new in _root_pairs(before_bundle, after_bundle):
            if not _same(managed_edges(ref, old), managed_edges(ref, new)):
                raise DocumentPatchError(f"{ref} changes managed closure edges; use a semantic command")
            if ref == "parent":
                for left, right in ((old, new), (old.get("config", {}), new.get("config", {}))):
                    a, b = left.get("clips", _MISSING), right.get("clips", _MISSING)
                    if (a is None or b is None) and not _same(a, b):
                        raise DocumentPatchError("cannot change a null parent clip mirror")
            _diff(_payload_for_emit(ref, old), _payload_for_emit(ref, new), ref, (), changes)
        if not changes:
            return []
        commands: list[dict[str, Any]] = []
        pending: list[dict[str, Any]] = []
        parent = before_bundle["parent"]
        mirrored = "clips" in parent and "clips" in parent.get("config", {})
        for index, change in enumerate(changes):
            pending.append(change)
            # Native item setters refresh clip mirrors at command completion.
            # Keep the next operand's closure consistent at that boundary.
            clip_item = change["ref"] == "parent" and change["path"][:1] == ["clips"] and len(change["path"]) > 1
            if index != len(changes) - 1 and not (mirrored and clip_item):
                continue
            command: dict[str, Any] = {
                "schema_version": 1, "op": "doc.patch", "version": "1", "select": None,
                "args": {"changes": copy.deepcopy(pending)},
            }
            created = _created(pending, before_bundle)
            if created:
                command["created"] = created
            commands.append(command)
            pending = []
        return commands
    except (CommandError, ValueError, KeyError) as exc:
        raise DocumentPatchError(f"cannot emit a source-bound doc.patch: {exc}") from exc


__all__ = ["DocumentPatchError", "emit_document_commands"]
