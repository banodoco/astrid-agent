"""EditCommand producer and runner, using the existing OpCall wire vocabulary.

Adapters call registered operations. Nested operations join the active transaction;
only its outer boundary resolves timing. Calls retain authoring units, never stored
sprite units. Runtime semantic commit remains outside this local runner.
"""
from __future__ import annotations

import copy
import inspect
import json
import re
from dataclasses import dataclass, replace
from functools import wraps
from pathlib import Path
from typing import Any, Mapping

from astrid.sdk.timeline._common import TimelineEditError
from astrid.version import __version__ as TOOL_VERSION


class CommandError(TimelineEditError):
    """A typed, expected refusal. Programming errors are deliberately not coerced."""

    code = "invalid_edit_command"

    def to_json(self):
        return {"code": self.code, "message": str(self)}


@dataclass(frozen=True)
class EditCommand:
    """The OpCall schema's one envelope (EditCommand is its executable SDK name)."""
    op: str
    select: str | None
    args: Mapping[str, Any]
    version: str | None = None
    resolved: tuple[str, ...] = ()

    expect: Mapping[str, Any] | None = None
    bindings: Mapping[str, str] | None = None
    created: tuple[str, ...] | None = None
    authorship: Mapping[str, str] | None = None

    def to_json(self):
        value = dict(op=self.op, select=self.select, args=copy.deepcopy(dict(self.args)),
                     version=self.version, resolved=list(self.resolved))
        for key in ("expect", "bindings", "created", "authorship"):
            item = getattr(self, key)
            if item is not None:
                value[key] = list(item) if key == "created" else copy.deepcopy(dict(item))
        return value

    @classmethod
    def coerce(cls, value):
        if isinstance(value, cls):
            value = value.to_json()
        if not isinstance(value, Mapping):
            raise CommandError("an EditCommand must be an OpCall object")
        from banodoco_workspace_client.generated import validate_edit_command
        value = dict(value)
        for key in ("resolved", "created"):
            if isinstance(value.get(key), tuple):
                value[key] = list(value[key])
        try:
            value = json.loads(json.dumps(value, allow_nan=False))
            validate_edit_command(value)
        except (ValueError, TypeError) as exc:
            raise CommandError(str(exc)) from None
        return cls(value["op"], value["select"], value["args"], value.get("version"),
                   tuple(value.get("resolved", ())), value.get("expect"), value.get("bindings"),
                   tuple(value["created"]) if "created" in value else None, value.get("authorship"))


@dataclass(frozen=True)
class Operation:
    op: str
    kind: str
    method: str
    handler: Any
    boundary: str = "edit"


REGISTRY: dict[str, Operation] = {}


def _plain(value):
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    # SDK views are serialized by stable identity, not repr/memory address.
    if hasattr(value, "id"):
        return str(value.id)
    from astrid.core.timeline import moments
    if isinstance(value, moments.Moment):
        return moments.format_moment(value)
    return value


def _checkout(owner, kind):
    return owner if kind == "timeline" else (getattr(owner, "_tl", None) or getattr(owner, "tl", None))


def register(cls, kind, names, *, boundary="edit"):
    """Install SDK adapters from a single explicit operation registry."""
    for name in names:
        function = getattr(cls, name)
        op = f"{kind}.{name.replace('_', '-').rstrip('-')}"
        entry = Operation(op, kind, name, function, boundary)
        REGISTRY[op] = entry
        setattr(cls, name, _adapter(entry))


def _signature(function):
    signature = inspect.signature(function)
    # music_keyword adds a real keyword while preserving the wrapped signature.
    implementation = inspect.signature(function, follow_wrapped=False)
    if "music" in implementation.parameters and "music" not in signature.parameters:
        parameters = list(signature.parameters.values())
        i = next((i for i, p in enumerate(parameters) if p.kind == p.VAR_KEYWORD), len(parameters))
        parameters.insert(i, inspect.Parameter("music", inspect.Parameter.KEYWORD_ONLY, default=None))
        signature = signature.replace(parameters=parameters)
    return signature


def _adapter(entry):
    @wraps(entry.handler)
    def wrapped(owner, *args, **kwargs):
        tl = _checkout(owner, entry.kind)
        if entry.boundary == "storage":
            return _storage_dispatch(entry, owner, args, kwargs)
        # Internal calls are implementation of the outer command, not extra calls.
        if getattr(tl, "_command_depth", 0):
            return entry.handler(owner, *args, **kwargs)
        bound = _signature(entry.handler).bind(owner, *args, **kwargs)
        arguments = {key: _plain(value) for key, value in bound.arguments.items() if key not in {"self", "client"}}
        if any(p.kind == p.VAR_KEYWORD for p in _signature(entry.handler).parameters.values()):
            for key, p in _signature(entry.handler).parameters.items():
                if p.kind == p.VAR_KEYWORD:
                    arguments.update(arguments.pop(key, {}))
        select = None if entry.kind == "timeline" else str(
            getattr(owner, "id", None) or getattr(owner, "segment", None) or getattr(owner, "seq_id", ""))
        command = EditCommand.coerce(dict(op=entry.op, select=select, args=arguments))
        label = f"{entry.method}({', '.join(f'{k}={v!r}' for k, v in arguments.items())})"
        with tl.step(label):
            return _invoke(tl, command, entry, owner=owner, sdk_args=(args, kwargs))
    wrapped.__edit_operation__ = entry.op
    return wrapped


def _invoke(tl, command, entry, *, owner=None, sdk_args=None):
    if entry.boundary == "storage":
        raise CommandError(f"{entry.op} is a storage boundary; use checkout.{entry.method}() or its existing CLI verb")
    if owner is None and command.resolved and entry.kind != "timeline":
        owner = recorded_owner(tl, command, entry.kind)
    if owner is None and entry.kind == "clip" and command.select and is_scoped_id(command.select):
        owner = recorded_owner(tl, replace(command, resolved=(command.select,)), "clip")
    if owner is None:
        if entry.kind == "timeline":
            if command.select is not None:
                raise CommandError(f"{entry.op} does not take a selector")
            owner = tl
        elif command.select is None:
            raise CommandError(f"{entry.op} requires a selector")
        elif entry.kind == "clip":
            owner = tl.clip(command.select)
        elif entry.kind == "cut":
            owner = tl.cut(command.select)
        elif entry.kind == "voice":
            owner = tl.voice(command.select)
        else:
            owner = tl.sequence(command.select)
    selected = selected_ids(tl, owner, entry.kind)
    if command.resolved and entry.kind != "timeline" and selected != command.resolved:
        raise SemanticConflict("delete-vs-edit", command.select or "", "resolved",
                               list(command.resolved), list(selected), None)
    before_entities = entity_states(tl)
    check_expectations(tl, command, selected)
    args = copy.deepcopy(dict(command.args))
    if entry.op == "timeline.add" and command.created:
        clips = [sid for sid in command.created if "/clip:" in sid]
        if len(clips) != 1:
            raise CommandError("timeline.add requires exactly one recorded created clip")
        args["clip_id"] = clips[0].split("/clip:", 1)[1]
    if command.created and any(sid in before_entities for sid in command.created):
        raise SemanticConflict("dependent", command.select or "", "created", None,
                               list(command.created), "created identity already exists")
    sig = _signature(entry.handler)
    positional = []
    for key, parameter in sig.parameters.items():
        if parameter.kind == parameter.VAR_POSITIONAL:
            positional = args.pop(key, [])
            if not isinstance(positional, (list, tuple)):
                raise CommandError(f"{entry.op}: {key} must be an array")
    try:
        bound = sig.bind(owner, *positional, **args)
    except TypeError as exc:
        raise CommandError(f"{entry.op}: {exc}") from None
    bound.apply_defaults()
    canonical = {}
    for key, value in bound.arguments.items():
        if key in {"self", "client"}:
            continue
        if sig.parameters[key].kind == inspect.Parameter.VAR_KEYWORD:
            canonical.update(_plain(value))
        else:
            canonical[key] = _plain(value)
    if "music" in canonical:
        canonical["music"] = str(canonical["music"] or tl.music).replace("-", "_")
    if entry.op == "timeline.add" and isinstance(canonical.get("at"), str):
        from astrid.sdk.timeline._common import TIME_RE
        if TIME_RE.fullmatch(canonical["at"].strip()):
            canonical["at"] = tl.time(canonical["at"])
    for key, value in bound.arguments.items():
        if key == "self":
            continue
        parameter = sig.parameters[key]
        annotation = str(parameter.annotation).strip("'")
        optional = "None" in annotation or parameter.default is None
        if value is None and optional:
            continue
        if parameter.kind in {parameter.VAR_KEYWORD, parameter.VAR_POSITIONAL}:
            continue
        options = [part.strip() for part in annotation.split(" | ")]
        def accepts(expected):
            if expected == "None":
                return value is None
            if expected in {"float", "int"}:
                valid = isinstance(value, (int, float)) and not isinstance(value, bool)
                return valid and (expected == "float" or int(value) == value)
            if expected == "str":
                return isinstance(value, str)
            if expected == "bool":
                return isinstance(value, bool)
            if expected.startswith(("Mapping[", "dict[")):
                return isinstance(value, Mapping)
            if expected.startswith(("Sequence[", "list[")):
                return isinstance(value, (list, tuple))
            return True  # richer domain values remain the handler's validation
        if not any(accepts(expected) for expected in options):
            raise CommandError(f"{entry.op}: {key} must be {annotation}")
    resolved = selected
    selector = None if entry.kind == "timeline" else str(
        getattr(owner, "id", None) or getattr(owner, "segment", None) or getattr(owner, "seq_id", None) or command.select)
    recorded = replace(command, select=selector, args=canonical, version=command.version or TOOL_VERSION,
                       resolved=resolved)
    bindings = dict(command.bindings or {})
    capture_argument_bindings(tl, command, owner, bindings)
    original_moment_time = tl._moment_time
    def bound_moment_time(text, clip=None, *, in_point=False):
        capture_binding(tl, str(text), clip, bindings)
        return original_moment_time(text, clip, in_point=in_point)
    tl._moment_time = bound_moment_time
    original_new_id = tl._new_id
    allocated = iter(sid.split("/clip:", 1)[1] for sid in (command.created or ()) if "/clip:" in sid)
    def fixed_new_id(element):
        if command.created is None:
            return original_new_id(element)
        try:
            return next(allocated)
        except StopIteration:
            raise CommandError("Replay attempted an unrecorded identity allocation") from None
    tl._new_id = fixed_new_id
    index = len(tl._open_edit.calls)
    if entry.op not in {"timeline.batch", "timeline.edit-flags"}:
        tl._open_edit.calls.append(recorded)
    split_source = None
    tl._command_depth = getattr(tl, "_command_depth", 0) + 1
    try:
        if entry.op == "timeline.add-cut":
            split_source = tl._cut_on_screen(tl._moment_time(tl._split_moment(canonical["on"], None)))
        if entry.op == "timeline.close-gap":
            from astrid.core.timeline import moments
            canonical["before"] = moments.format_moment(moments.parse(str(canonical["before"])))
        if entry.op == "clip.set-param":
            owner._check_param_keys([canonical["path"]])
        clear_args = {key for key, value in args.items() if value == {"op": "clear"}}
        if entry.op in {"clip.set", "clip.set-param"} and (clear_args or args.get("value") == {"op": "clear"}):
            if entry.op == "clip.set-param":
                owner.clear(args["path"])
            else:
                owner.clear(*sorted(clear_args))
                remaining = {key: value for key, value in args.items() if key not in clear_args}
                if remaining:
                    entry.handler(owner, **remaining)
            result = owner
        else:
            result = entry.handler(owner, *sdk_args[0], **sdk_args[1]) if sdk_args is not None else entry.handler(owner, *positional, **args)
        if entry.op in {"timeline.batch", "timeline.edit-flags"}:
            pass
        elif entry.op in {"timeline.add-cut", "cut.split"}:
            source = split_source if entry.op == "timeline.add-cut" else str(owner.id)
            split_args = {"on": tl._split_moment(canonical["on"], source),
                          "id": result.id, "picture": result.picture.asset if result.picture else None}
            tl._open_edit.calls[index] = replace(recorded, op="cut.split", select=source, args=split_args, resolved=selected_ids(tl, tl.cut(source), "cut"))
        elif entry.op in {"clip.extend", "clip.hold-for", "clip.for", "clip.set-duration"}:
            tl._open_edit.calls[index] = replace(recorded, op="clip.set-duration", args={"seconds": owner.duration})
        elif entry.op == "clip.remove-layer" and result == [owner.id]:
            tl._open_edit.calls[index] = replace(recorded, op="clip.remove", args={})
        elif entry.op in {"clip.on", "clip.enter-at", "clip.nudge"}:
            from astrid.sdk import timeline_intent as intent
            moment = intent.on(owner.data)
            if moment:
                tl._open_edit.calls[index] = replace(recorded, op="clip.on", args={"moment": moment})
        elif entry.op in {"clip.set", "clip.set-look"}:
            from astrid.core.timeline import clip_fields as cf
            from astrid.sdk.timeline_address import element_schema
            declared = element_schema(owner.element).get("properties") or {}
            if canonical and not clear_args and all(key in cf.FIELDS and key not in declared for key in canonical):
                # The codec supplies a canonical, replayable authoring spelling.
                fields = {}
                for key, value in canonical.items():
                    path, stored = cf.coerce(key, value, fps=tl.fps, audio=owner.is_audio)
                    normalized_key = next((name for name, spec in cf.FIELDS.items()
                                           if spec.stored_at == path and spec.kind != "fade"), key)
                    spec = cf.FIELDS[normalized_key]
                    fields[normalized_key] = spec.from_stored(stored, fps=tl.fps) if isinstance(stored, dict) else stored
                tl._open_edit.calls[index] = replace(recorded, op="clip.set-look", args=fields)
        elif entry.op == "timeline.add":
            from astrid.sdk import timeline_intent as intent
            canonical["clip_id"] = result.id
            canonical["at"] = intent.on(result.data) or result.start
            tl._open_edit.calls[index] = replace(recorded, args=canonical, resolved=(scoped_clip(tl, result),))
        if entry.op not in {"timeline.batch", "timeline.edit-flags"}:
            call = tl._open_edit.calls[index]
            after_entities = entity_states(tl)
            created = tuple(sorted(set(after_entities) - set(before_entities)))
            created = tuple(sid for sid in created if is_scoped_id(sid))
            if command.created is not None and not set(created) <= set(command.created):
                raise CommandError(f"Replay changed created identities: expected {command.created}, got {created}")
            paths = direct_paths(tl, entry, owner, canonical, before_entities, after_entities, selected)
            if entry.kind == "timeline" and entry.op != "timeline.add":
                hit = tuple(sorted({sid for sid, _ in paths if is_scoped_id(sid)}))
                if command.authorship is not None and hit != command.resolved:
                    raise CommandError(f"Replay changed resolved direct targets: expected {command.resolved}, got {hit}")
                call = replace(call, resolved=hit)
            expectations = dict(command.expect or {})
            authorship = dict(command.authorship or {})
            actual_direct = set()
            for sid, path in paths:
                key = write_key(sid, path, call.resolved)
                expectations.setdefault(key, state_at(before_entities, sid, path))
                authorship[key] = "direct"
                actual_direct.add(key)
            for sid, path in changed_paths(before_entities, after_entities):
                key = write_key(sid, path, call.resolved)
                authorship.setdefault(key, "consequence")
            # A supplied record is checked, never used to claim unobserved authorship.
            if command.authorship is not None:
                expected_direct = {key for key, kind in command.authorship.items() if kind == "direct"}
                if expected_direct != actual_direct:
                    raise CommandError(f"Replay changed direct paths: expected {sorted(expected_direct)}, got {sorted(actual_direct)}")
                if not actual_direct <= set(command.expect or {}):
                    raise CommandError("Recorded direct write is missing its original precondition")
            capture_changed_bindings(tl, before_entities, after_entities, bindings)
            for text, anchor in (command.bindings or {}).items():
                if any(anchor == sid or (sid.startswith("cut:") and anchor.startswith(sid + ":"))
                       for sid in (command.created or ())):
                    candidates = [clip for clip in tl.clips() if scoped_clip(tl, clip) in created]
                    if not candidates:
                        raise CommandError(f"Created anchor context is unavailable for {text!r}")
                    for clip in candidates:
                        capture_binding(tl, text, clip, bindings)
            call = replace(call, expect=expectations, bindings=bindings, created=created, authorship=authorship)
            tl._open_edit.calls[index] = EditCommand.coerce(call)
            snapshots = getattr(tl._open_edit, "semantic_snapshots", None)
            if snapshots is None:
                tl._open_edit.semantic_snapshots = snapshots = []
            snapshots.append((index, copy.deepcopy(tl.bundle), before_entities, after_entities, command.created))
        return result
    except TimelineEditError as exc:
        # Adapters may add source context to their displayed error; the durable
        # refusal records the command's original typed failure.
        tl._open_edit.failure = str(exc)
        raise
    finally:
        tl._moment_time = original_moment_time
        tl._new_id = original_new_id
        tl._command_depth -= 1



class SemanticConflict(CommandError):
    """A command cannot keep its recorded meaning on this document."""
    code = "semantic_conflict"

    def __init__(self, kind, identity, path, base, theirs, ours):
        self.conflict = dict(kind=kind, id=identity, path=path, base=copy.deepcopy(base),
                             theirs=copy.deepcopy(theirs), ours=copy.deepcopy(ours))
        super().__init__(f"{kind}: {identity}.{path}: expected {base!r}, found {theirs!r}")

    def to_json(self):
        return {**super().to_json(), "details": copy.deepcopy(self.conflict)}


def is_scoped_id(value):
    from banodoco_workspace_client.generated import OPCALL_SCHEMA
    return re.fullmatch(OPCALL_SCHEMA["$defs"]["scopedId"]["pattern"], value) is not None


def scoped_clip(tl, clip):
    rows = [row for row in tl.bundle.get("placements", []) if str(row.get("shot_id")) == clip.shot_id]
    if len(rows) != 1 or not rows[0].get("occurrence_id"):
        raise CommandError(f"Clip {clip.id!r} has no unambiguous occurrence identity")
    value = f"occurrence:{rows[0]['occurrence_id']}/clip:{clip.id}"
    if not is_scoped_id(value):
        raise CommandError(f"Unsupported scoped identity {value!r}")
    return value


def selected_ids(tl, owner, kind):
    if kind == "timeline":
        return ()
    if kind == "clip":
        return (scoped_clip(tl, owner),)
    if kind == "cut":
        if not owner.id:
            raise CommandError("A command needs a named stable cut identity")
        return (f"cut:{owner.id}",)
    clips = owner.clips if kind == "voice" else owner.steps
    return tuple(scoped_clip(tl, clip) for clip in clips)


def recorded_owner(tl, call, kind):
    if kind == "cut":
        sid = call.resolved[0]
        if not sid.startswith("cut:") or not any(g["id"] == sid[4:] for g in tl._cut_groups()):
            raise SemanticConflict("delete-vs-edit", sid, "", None, None, call.args)
        return tl.cut(sid[4:])
    clips = {scoped_clip(tl, clip): clip for clip in tl.clips()}
    for sid in call.resolved:
        if sid not in clips:
            raise SemanticConflict("delete-vs-edit", sid, "", None, None, call.args)
    first = clips[call.resolved[0]]
    if kind == "clip":
        if len(call.resolved) != 1:
            raise CommandError("clip operation requires exactly one resolved clip")
        return first
    from astrid.sdk import timeline_intent as intent
    return tl.voice(intent.line(first.data)) if kind == "voice" else tl.sequence(first)


def entity_states(tl):
    """Detached authored state keyed by actual occurrence identities, never aliases."""
    result = {"parent": copy.deepcopy(tl.bundle.get("parent", {}))}
    for row in tl.bundle.get("placements", []):
        sid = str(row.get("shot_id"))
        oid = row.get("occurrence_id")
        if not oid:
            # Legacy checkouts can still be read; a selected identity fails closed.
            continue
        result[f"occurrence:{oid}"] = copy.deepcopy(row)
        shot = tl.bundle.get("shots", {}).get(sid, {})
        result[f"occurrence:{oid}/shot"] = copy.deepcopy(shot.get("payload", {}))
        for kind, rows in (("clip", shot.get("internal_timeline", {}).get("clips", [])),
                           ("track", shot.get("internal_timeline", {}).get("tracks", []))):
            for item in rows:
                key = f"occurrence:{oid}/{kind}:{item['id']}"
                if key in result:
                    raise CommandError(f"Duplicate scoped identity {key}")
                result[key] = copy.deepcopy(item)
    for group in tl._cut_groups():
        result[f"cut:{group['id']}"] = copy.deepcopy(tl.cut_note(group['id']))
    return result


def _member_path(prefix, key):
    if re.fullmatch(r"[A-Za-z_][\w-]*", key):
        return f"{prefix}.{key}" if prefix else key
    return prefix + "[" + json.dumps(key) + "]"


def _same_value(left, right):
    """JSON equality keeps booleans distinct from numeric zero/one."""
    if isinstance(left, bool) or isinstance(right, bool):
        return type(left) is type(right) and left == right
    if isinstance(left, Mapping) and isinstance(right, Mapping):
        return set(left) == set(right) and all(_same_value(left[key], right[key]) for key in left)
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(_same_value(a, b) for a, b in zip(left, right))
    return left == right


def changed_paths(before, after):
    def walk(left, right, path):
        if _same_value(left, right):
            return
        if isinstance(left, dict) and isinstance(right, dict):
            for key in sorted(set(left) | set(right)):
                child = _member_path(path, key)
                if key not in left or key not in right:
                    yield child
                else:
                    yield from walk(left[key], right[key], child)
        else:
            yield path
    for sid in sorted(set(before) | set(after)):
        if sid not in before or sid not in after:
            yield sid, ""
        else:
            for path in walk(before[sid], after[sid], ""):
                yield sid, path.replace("app.astrid.", "intent.", 1) if path.startswith("app.astrid.") else path


def state_at(entities, sid, path):
    if sid not in entities:
        return {"state": "absent"}
    path = "app.astrid." + path[7:] if path.startswith("intent.") else path
    exists, value = _path_state(entities[sid], path)
    return dict(state="present", value=value) if exists else dict(state="absent")


def write_key(sid, path, resolved):
    return path if len(resolved) == 1 and sid == resolved[0] else f"{sid}::{path}"


def write_target(key, resolved):
    if "::" in key:
        return tuple(key.split("::", 1))
    if len(resolved) != 1:
        raise CommandError(f"Expectation {key!r} needs one scoped target or an id::path")
    return resolved[0], key


def check_expectations(tl, call, selected):
    states = entity_states(tl)
    for key, expected in (call.expect or {}).items():
        sid, path = write_target(key, selected or call.resolved)
        current = state_at(states, sid, path)
        if not _same_value(current, expected):
            kind = "delete-vs-edit" if ((sid not in states and expected["state"] == "present") or
                    call.op in {"clip.remove", "clip.remove-layer"}) else "same-property"
            error = SemanticConflict(kind, sid, path, expected, current, call.args)
            error.code = "EXPECT_MISMATCH"
            raise error


def direct_paths(tl, entry, owner, args, before, after, selected):
    from astrid.core.timeline import clip_fields as cf
    from astrid.sdk.timeline_address import element_schema
    changes = set(changed_paths(before, after))
    if entry.op in {"timeline.resolve", "timeline.reflow", "timeline.retime"}:
        return set()  # compiler execution is not a claim of direct authorship
    if entry.kind == "clip" and entry.op in {"clip.set", "clip.set-param", "clip.set-look", "clip.clear"}:
        sid = selected[0]
        if entry.op == "clip.set-param":
            keys = [args["path"]]
        elif entry.op == "clip.clear":
            keys = args["keys"]
        else:
            keys = [key for key in args if key != "_allow_new"]
        declared = element_schema(owner.element).get("properties") or {}
        paths = set()
        for key in keys:
            if key in cf.FIELDS and key not in declared:
                path = cf.FIELDS[key].stored_at
                paths.add((sid, path))
            else:
                paths.add((sid, f"params.{key}"))
        # Replacing/clearing a formula also writes its authoring relation.
        paths.update((entity, path) for entity, path in changes if entity == sid and
                     (path.startswith("intent.formulas") or path.startswith("app.astrid")))
        return paths
    direct = set()
    targets = set(selected)
    if entry.kind == "cut":
        targets.update(scoped_clip(tl, clip) for clip in owner.clips)
    for sid, path in changes:
        if entry.kind == "timeline" or sid in targets:
            # Authored timing inputs are direct; derived clocks remain consequences.
            data = after.get(sid, before.get(sid, {}))
            if path in {"at", "hold"} and (data.get("app", {}).get("astrid") or {}).get("on"):
                continue
            direct.add((sid, path))
    if entry.kind == "clip":
        sid = selected[0]
        if entry.op in {"clip.on", "clip.enter-at", "clip.nudge"}:
            from astrid.sdk import timeline_intent as intent
            direct.add((sid, "intent.on" if intent.on(owner.data) else "at"))
        elif entry.op in {"clip.until", "clip.end-at", "clip.set-duration", "clip.hold-for", "clip.for", "clip.extend"}:
            direct.update((sid, path) for path in ("intent.until", "intent.for"))
        elif entry.op == "clip.slip":
            direct.update((sid, path) for path in ("from", "to"))
    return direct


def capture_binding(tl, text, clip, bindings):
    from astrid.core.timeline import moments as mo
    from astrid.sdk.timeline.model import _MomentContext
    from astrid.sdk import timeline_intent as intent
    ctx = _MomentContext(tl, clip)
    from astrid.sdk.timeline._common import WORD_ID_RE
    if WORD_ID_RE.fullmatch(text.strip()):
        word = tl.word(text.strip())
        if word.id != text.strip():
            raise CommandError(f"Unavailable exact word identity {text!r}")
        anchor = f"word:{word.segment}:{word.index}"
        if text in bindings and bindings[text] != anchor:
            raise SemanticConflict("dependent", "parent", "bindings", bindings[text], anchor, text)
        bindings[text] = anchor
        return
    try:
        moment = mo.parse(text)
        resolved_time = mo.resolve(moment, ctx)  # use the same resolver and ambiguity rules
        if moment.kind == "word":
            hits = mo.find_words(moment, ctx.words())
            run = hits[(moment.n or 1) - 1]
            word = run[-1] if moment.edge == "end" else run[0]
            anchor = f"word:{word.segment}:{word.index}"
        elif moment.kind in {"beat", "downbeat"}:
            beats = list(ctx.beats(moment.kind))
            at = resolved_time - moment.offset_s - moment.offset_frames / tl.fps
            anchor = f"beat:{beats.index(at)}"
            if moment.base:
                capture_binding(tl, mo.format_moment(moment.base), clip, bindings)
        else:
            cut = moment.cut or (intent.cut_of(clip.data) if clip else None)
            if cut is None and moment.kind == "end":
                groups = tl._cut_groups()
                cut = groups[-1]["id"] if groups else None
            cut = tl._cut_id(cut) or cut if cut else None
            if not cut:
                raise CommandError(f"No stable anchor identity for TimeRef {text!r}")
            anchor = f"cut:{cut}:{'end' if moment.kind in {'cut_end', 'end'} else 'start'}"
    except mo.MomentError as exc:
        raise CommandError(f"TimeRef {text!r}: {exc}") from None
    if text in bindings and bindings[text] != anchor:
        raise SemanticConflict("dependent", scoped_clip(tl, clip) if clip else "parent", "bindings",
                               bindings[text], anchor, text)
    bindings[text] = anchor


def capture_argument_bindings(tl, call, owner, bindings):
    from astrid.sdk.timeline.model import Clip
    from astrid.sdk.timeline._common import TIME_RE
    clip = owner if isinstance(owner, Clip) else None
    for key in ("moment", "on", "until", "at", "when", "before", "land"):
        value = call.args.get(key)
        if isinstance(value, str) and not TIME_RE.fullmatch(value.strip()):
            # These keys are TimeRefs only on the corresponding timing handlers.
            if call.op in {"clip.on", "clip.until", "clip.enter-at", "clip.end-at", "timeline.add",
                           "cut.split", "timeline.add-cut", "timeline.close-gap", "sequence.fit"}:
                capture_binding(tl, value, clip, bindings)
    # Original exact text survives canonical spelling changes on replay.
    for text in list(bindings):
        anchor = bindings[text]
        if any(anchor == sid or (sid.startswith("cut:") and anchor.startswith(sid + ":"))
               for sid in (call.created or ())):
            continue  # this dependency is created by this call, not available before it
        capture_binding(tl, text, clip, bindings)


def capture_changed_bindings(tl, before, after, bindings):
    from astrid.sdk import timeline_intent as intent
    for clip in tl.clips():
        sid = scoped_clip(tl, clip)
        if before.get(sid) == after.get(sid):
            continue
        old = before.get(sid, {})
        for reader in (intent.on, intent.until):
            text = reader(clip.data)
            if text and text != reader(old):
                capture_binding(tl, text, clip, bindings)
        for path, expr in intent.formulas(clip.data).items():
            if isinstance(expr, Mapping) and expr.get("moment") and expr != intent.formulas(old).get(path):
                capture_binding(tl, expr["moment"], clip, bindings)


def finish_recording(tl, tx, entry):
    """Attribute compiler output using actual compiled prefixes of this execution.

    Prefixes are detached probes of already executed bytes, never relative command
    re-execution. The working checkout retains its existing one final resolve.
    """
    snapshots = getattr(tx, "semantic_snapshots", ())
    if not snapshots:
        return
    previous = entity_states(tl.__class__(copy.deepcopy(entry)))
    causes = {}
    for index, bundle, before, after, expected_created in snapshots:
        probe = tl.__class__(copy.deepcopy(bundle))
        probe._command_depth = 1
        probe._edit_local_only = True
        from astrid.sdk.timeline.store import _snapshot
        if _snapshot(probe) != _snapshot(tl.__class__(copy.deepcopy(entry))):
            probe.resolve()
        if tx.report is not None:
            probe.check()
        compiled = entity_states(probe)
        created = tuple(sorted(sid for sid in set(compiled) - set(previous) if is_scoped_id(sid)))
        if expected_created is not None and set(created) != set(expected_created):
            raise CommandError(f"Compiler changed recorded created identities: expected {expected_created}, got {created}")
        tx.calls[index] = replace(tx.calls[index], created=created)
        for sid, path in changed_paths(previous, compiled):
            causes[(sid, path)] = index
        previous = compiled
    final = entity_states(tl)
    if not _same_value(previous, final):
        raise CommandError("Compiler prefix evidence does not match the executed candidate")
    original = entity_states(tl.__class__(copy.deepcopy(entry)))
    direct, consequences = [], []
    for sid, path in changed_paths(original, final):
        # The last prefix that actually changed this path is its causal command.
        cause = causes.get((sid, path))
        if cause is None:
            # A dictionary becoming present may subsume a leaf difference.
            parents = [(key, idx) for key, idx in causes.items() if key[0] == sid and
                       (not key[1] or path.startswith(key[1] + "."))]
            if not parents:
                raise CommandError(f"No executed cause for {sid}.{path}")
            cause = max(idx for _, idx in parents)
        call = tx.calls[cause]
        key = write_key(sid, path, call.resolved)
        authorship = dict(call.authorship or {})
        kind = authorship.get(key, "consequence")
        authorship[key] = kind
        tx.calls[cause] = replace(call, authorship=authorship)
        write = dict(id=sid, path=path, before=state_at(original, sid, path), after=state_at(final, sid, path))
        if kind == "direct":
            direct.append(write)
        else:
            consequences.append({**write, "cause": cause})
    tx.direct_writes, tx.consequences = direct, consequences


def compact_calls(calls, tl=None):
    """Keep execution history when a single OpCall cannot express every guard.

    Even adjacent setters may carry intermediate expectations, gesture boundaries,
    bindings or a consequence cause. The current envelope has no lossless fold for
    those records. In particular add/set and voice/reflow dependencies stay ordered.
    """
    return tuple(EditCommand.coerce(call) for call in calls)


def call_label(calls):
    return "; ".join(f"{c.op} {c.select or ''} {json.dumps(_plain(dict(c.args)), sort_keys=True, ensure_ascii=False, separators=(',', ':'))}".strip()
                     for c in calls)[:160]


def run(tl, value, *, dry_run=False):
    """Run one OpCall atomically and return its change preview."""
    command = EditCommand.coerce(value)
    entry = REGISTRY.get(command.op)
    if entry is None:
        raise CommandError(f"unknown edit operation {command.op!r}; choose from {', '.join(sorted(REGISTRY))}")
    with tl.edit(command.op, dry_run=dry_run, quiet=True) as tx:
        tx.canonical = True
        _invoke(tl, command, entry)
    return tx.plan


def execute(tl, value):
    """Small sheet/SDK adapter; join an outer edit without persisting early."""
    command = EditCommand.coerce(value)
    entry = REGISTRY.get(command.op)
    if entry is None:
        raise CommandError(f"unknown edit operation {command.op!r}")
    with tl.step(command.op):
        return _invoke(tl, command, entry)


# This is an explicit adapter map, not a generated parser. Context/report flags
# belong to the executable edit-flags entry; mutation flags name their operation.
EDIT_FLAG_OPERATIONS = {}
for _operation, _names in {
    "timeline.edit-flags": "project file draft clip cut near n offset labels timings verify sweep no_sweep music crossfade overlap",
    "timeline.add": "add_media track clip_id",
    "clip.trim": "trim_in trim_out", "clip.slip": "slip",
    "clip.set": "set_values allow_new_params", "clip.clear": "clear_keys",
    "clip.set-look": "entrance exit fade_in fade_out opacity volume gain_db",
    "clip.on": "on", "clip.enter-at": "at at_word", "clip.until": "until",
    "clip.set-duration": "for_seconds", "clip.extend": "extend", "clip.nudge": "nudge nudge_frames",
    "timeline.close-gap": "close_gap_before", "cut.split": "split cut_id picture", "timeline.add-cut": "add_cut after",
    "timeline.set-cut-note": "why hold", "sequence.fit": "fit_sequence shape_from",
    "timeline.film-end": "film_end", "timeline.retime": "retime", "timeline.insert-time": "insert",
    "clip.keep": "keep", "clip.clear-asset": "clear_asset", "clip.set-beats": "beats",
    "voice.set-gap-after": "gap_after", "timeline.remove-line": "remove_line",
    "voice.replace": "replace_line take words line_text", "timeline.insert-line": "insert_line insert_gap_after",
    "timeline.apply-script": "from_script takes script_gaps", "clip.swap-asset": "swap_asset",
    "timeline.replace-asset": "replace_asset", "clip.remove-layer": "remove",
    "timeline.batch": "command dry_run",
}.items():
    for _name in _names.split():
        if _name in EDIT_FLAG_OPERATIONS:
            raise RuntimeError(f"duplicate edit flag mapping: {_name}")
        EDIT_FLAG_OPERATIONS[_name] = _operation


def flag_inventory_table(inventory):
    rows = []
    for flag in inventory["commands"]["timelines.edit"]["flags"]:
        destination = flag["dest"]
        operation = EDIT_FLAG_OPERATIONS.get(destination)
        if operation not in REGISTRY:
            raise CommandError(f"unmapped edit flag: {destination}")
        rows.append({"flags": flag["options"], "destination": destination, "operation": operation})
    return rows


# Any new public callable must be explicitly classified; the census test fails
# closed rather than silently assuming that an unfamiliar method is a reader.
PUBLIC_READERS = {
    "Clip": frozenset({"get"}),
    "Cut": frozenset({"layer", "step"}),
    "Checkout": frozenset("assets at changed_cut_ids changes check clip cut cut_count cut_note cut_notes_all cut_picture dips document edits empty_cuts expected_cuts find frame gaps lint log narration narration_changes orphans picture_gaps quantize sequence slot_clips time to_shot_seconds verify voice word".split()),
}
STORAGE_BOUNDARIES = frozenset({"discard", "draft", "edit", "load", "open", "pin_narration", "publish", "step"})


def unregistered_mutators(classes):
    missing = []
    for cls in classes:
        for name, value in inspect.getmembers(cls, callable):
            if name.startswith("_") or name in PUBLIC_READERS.get(cls.__name__, ()):
                continue
            if cls.__name__ == "Checkout" and name in STORAGE_BOUNDARIES:
                continue
            operation = getattr(value, "__edit_operation__", None)
            if operation not in REGISTRY:
                missing.append(f"{cls.__name__}.{name}")
    return missing


def _path_state(data, path):
    if not path:
        return True, copy.deepcopy(data)
    node = data
    token = re.compile(r'(?:^|\.)([A-Za-z_][\w-]*)|\[(\d+|"(?:[^"\\]|\\.)*")\]')
    offset = 0
    for match in token.finditer(path):
        if match.start() != offset:
            raise CommandError(f"Invalid property path {path!r}")
        offset = match.end()
        key = match.group(1) if match.group(1) is not None else json.loads(match.group(2))
        try:
            node = node[key]
        except (KeyError, IndexError, TypeError):
            return False, None
    if offset != len(path):
        raise CommandError(f"Invalid property path {path!r}")
    return True, copy.deepcopy(node)


def direct_writes(tl, calls):
    """Guard actual direct properties, scoped to their resolved occurrences."""
    from astrid.sdk import timeline_intent as intent
    states = entity_states(tl)
    clips = {scoped_clip(tl, clip): clip for clip in tl.clips()}
    guards = {}
    for call in calls:
        for key, kind in (call.authorship or {}).items():
            if kind != "direct":
                continue
            sid, path = write_target(key, call.resolved)
            if not path or sid not in states:
                continue  # created/deleted entities are checked by identity recording
            formulas = intent.formulas(clips[sid].data) if sid in clips else {}
            if any(p == path or p.startswith((path + ".", path + "[")) for p in formulas):
                continue  # authored formula output is a compiler consequence
            guards[(sid, path)] = state_at(states, sid, path)
        if call.op == "timeline.add" and call.resolved:
            sid = call.resolved[0]
            for key in call.args.get("params") or {}:
                path = f"params.{key}"
                if sid in clips and path not in intent.formulas(clips[sid].data):
                    guards[(sid, path)] = state_at(states, sid, path)
    return guards


def guard_direct_writes(tl, guards):
    states = entity_states(tl)
    for (sid, path), state in guards.items():
        if not _same_value(state_at(states, sid, path), state):
            raise CommandError(f"{sid}.{path}: timing replaced an explicit write; edit rolled back")



def _storage_dispatch(entry, tl, args, kwargs):
    """Route local persistence/history through the runner without adding an edit.

    These handlers own their existing file/history protocol. In particular undo
    and redo must not create a new forward edit snapshot.
    """
    bound = inspect.signature(entry.handler).bind(tl, *args, **kwargs)
    bound.apply_defaults()
    previous = getattr(tl, "_storage_call", None)
    arguments = {key: _plain(value) for key, value in bound.arguments.items() if key != "self"}
    tl._storage_call = EditCommand(entry.op, None, arguments, TOOL_VERSION, ())
    try:
        return entry.handler(tl, *args, **kwargs)
    finally:
        tl._storage_call = previous
