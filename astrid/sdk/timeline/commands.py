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
from typing import TYPE_CHECKING, Any, Mapping

from astrid.sdk import timeline_intent as intent
from astrid.sdk.timeline._common import TIME_RE, TimelineEditError
from astrid.sdk.timeline.doc import _trim_end, _trim_front
from astrid.version import __version__ as TOOL_VERSION

if TYPE_CHECKING:
    from astrid.sdk.timeline.model import Clip

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
    # Compiled timing facades add another @wraps layer around that adapter.
    implementation = function
    while implementation is not None:
        implementation_signature = inspect.signature(implementation, follow_wrapped=False)
        if "music" in implementation_signature.parameters:
            break
        implementation = getattr(implementation, "__wrapped__", None)
    if implementation is not None and "music" not in signature.parameters:
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
        choices = kwargs.pop("relation_choices", None)
        bound = _signature(entry.handler).bind(owner, *args, **kwargs)
        arguments = {key: _plain(value) for key, value in bound.arguments.items() if key not in {"self", "client"}}
        if any(p.kind == p.VAR_KEYWORD for p in _signature(entry.handler).parameters.values()):
            for key, p in _signature(entry.handler).parameters.items():
                if p.kind == p.VAR_KEYWORD:
                    arguments.update(arguments.pop(key, {}))
        select = None if entry.kind == "timeline" else str(
            getattr(owner, "id", None) or getattr(owner, "segment", None) or getattr(owner, "seq_id", ""))
        if choices is not None:
            arguments["relation_choices"] = choices
        command = EditCommand.coerce(dict(op=entry.op, select=select, args=arguments))
        label = f"{entry.method}({', '.join(f'{k}={v!r}' for k, v in arguments.items())})"
        with tl.step(label):
            return _invoke(tl, command, entry, owner=owner, sdk_args=(args, kwargs))
    wrapped.__edit_operation__ = entry.op
    return wrapped


def _invoke(tl, command, entry, *, owner=None, sdk_args=None):
    if getattr(tl._open_edit, "cancelled", False):
        return owner
    if (entry.op in {"clip.set-fade", "clip.set-transition"} and owner is None
            and (not command.select or not is_scoped_id(command.select) or "/clip:" not in command.select)):
        raise CommandError(f"{entry.op} requires an exact clip ScopedId")
    if entry.op == "timeline.add" and ("media" in command.args or isinstance(command.args.get("track"), Mapping)):
        entry = replace(entry, kind="track", handler=add_project_media if "media" in command.args else add_track)
    duplicate_source = None
    if entry.op == "timeline.add" and "from" in command.args:
        entry = replace(entry, handler=duplicate_clip_source)
        duplicate_source = command.args["from"]
        if "from_" in command.args:
            raise CommandError("Use the documented from argument only")
    if (entry.op in {"timeline.split", "timeline.remove"} and command.select and is_scoped_id(command.select)
            and command.resolved and command.resolved != (command.select,)):
        raise CommandError("Selector and recorded owner disagree")
    if entry.boundary == "storage":
        raise CommandError(f"{entry.op} is a storage boundary; use checkout.{entry.method}() or its existing CLI verb")
    if owner is None and entry.kind in {"shot", "occurrence"}:
        from astrid.sdk.timeline.graph_commands import Owner
        owner = Owner(tl, command.select, new=entry.op == "occurrence.add")
    if owner is None and command.resolved and entry.kind != "timeline" and not (entry.kind == "track" and entry.op == "timeline.add"):
        owner = recorded_owner(tl, command, entry.kind)
    if owner is None and entry.kind == "clip" and command.select and is_scoped_id(command.select):
        owner = recorded_owner(tl, replace(command, resolved=(command.select,)), "clip")
    if owner is None and entry.kind == "track":
        owner = track_owner(tl, command.select, new=entry.op == "timeline.add" and "track" in command.args)
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
    if entry.op == "timeline.batch":
        def cancelled(value):
            child = EditCommand.coerce(value)
            from astrid.sdk.timeline import relations
            rows = child.args.get("relation_choices")
            if rows is not None and any(r["choice"] == "cancel" for r in relations.validate(rows)):
                return True
            return child.op == "timeline.batch" and any(cancelled(c) for c in child.args.get("commands", []))
        if any(cancelled(c) for c in command.args.get("commands", [])):
            tl._open_edit.cancelled = True
            return []
    selected = selected_ids(tl, owner, entry.kind)
    if command.resolved and entry.kind != "timeline" and not (entry.op == "timeline.add" and "media" in command.args) and selected != command.resolved:
        raise SemanticConflict("delete-vs-edit", command.select or "", "resolved",
                               list(command.resolved), list(selected), None)
    from astrid.sdk.timeline.identity import cut_reservations, retain_cut_reservation_mark
    previous_reservations = cut_reservations(tl.bundle)
    before_entities = entity_states(tl)
    if duplicate_source is not None:
        if not isinstance(duplicate_source, str) or not is_scoped_id(duplicate_source) or "/clip:" not in duplicate_source:
            raise CommandError("timeline.add from requires an exact clip ScopedId")
        # Source ownership and created ownership are separate identities. Guard
        # the authored source because opaque fields participate in this copy.
        recorded_owner(tl, replace(command, resolved=(duplicate_source,)), "clip")
        expectations = dict(command.expect or {})
        expectations.setdefault(duplicate_source + "::", state_at(before_entities, duplicate_source, ""))
        command = replace(command, expect=expectations)
    if entry.op == "timeline.split":
        expectations = dict(command.expect or {})
        expectations.setdefault(selected[0] + "::", state_at(before_entities, selected[0], ""))
        command = replace(command, expect=expectations)
    if entry.op == "clip.remove" and selected and "music_bed" in before_entities.get(selected[0], {}):
        expectations = dict(command.expect or {})
        expectations.setdefault(selected[0] + "::music_bed", state_at(before_entities, selected[0], "music_bed"))
        command = replace(command, expect=expectations)
    if entry.kind in {"shot", "occurrence"}:
        if any(key in command.args and command.args[key] is None for key in ("values", "clear", "placement", "replacement")):
            raise CommandError("Omit unspecified graph arguments; null is a value only inside supported fields")
        from astrid.sdk.timeline.graph_commands import _source, guard_source
        expectations = dict(command.expect or {})
        if entry.op != "occurrence.add":
            guard_source(owner, command.args.get("source"))
        else:
            _source(tl, command.args.get("source"))
        command = replace(command, expect=expectations)
    if entry.op in {"shot-group.set", "shot-group.remove"}:
        from astrid.sdk.timeline.shot_groups import guard
        command = guard(tl, command)
    if entry.kind == "track":
        command = guard_track_call(tl, command, owner)
    from astrid.sdk.timeline import relations
    if "relation_affected" in command.args and "relation_choices" not in command.args:
        raise CommandError("relation_affected requires relation_choices")
    if "relation_choices" in command.args:
        command = relations.prepare(tl, command, selected)
        if any(row["choice"] == "cancel" for row in command.args["relation_choices"]):
            tl._open_edit.cancelled = True
            return owner
    if entry.op == "clip.set-transition":
        from astrid.sdk.timeline.composite_effects import guard_call
        command = guard_call(tl, command, owner, before_entities, selected)
    if (entry.op == "timeline.apply-script" and isinstance(command.args.get("spec"), Mapping)
            and command.args["spec"].get("version") == 1):
        from astrid.sdk.timeline.narration import scoped_line
        spec = command.args["spec"]
        expectations = dict(command.expect or {})
        for sid in [*(scoped_line(item) for item in spec["lines"]), *spec["remove"]]:
            paths = ("asset", "app.astrid.segment", "app.astrid.text", "app.astrid.words", "app.astrid.take")
            if sid not in before_entities or sid in spec["remove"]:
                paths = ("",)
            for path in paths:
                expectations.setdefault(sid + "::" + path, state_at(before_entities, sid, path))
        command = replace(command, expect=expectations)
    check_expectations(tl, command, command.resolved if entry.op == "timeline.add" and "media" in command.args else selected)
    args = copy.deepcopy(dict(command.args))
    relation_choices = args.pop("relation_choices", None)
    relation_affected = args.pop("relation_affected", None)
    if entry.op == "timeline.add" and command.created and not isinstance(command.args.get("track"), Mapping):
        clips = [sid for sid in command.created if "/clip:" in sid]
        if len(clips) != 1:
            raise CommandError("timeline.add requires exactly one recorded created clip")
        args["clip_id"] = clips[0].split("/clip:", 1)[1]
    if command.created and any(sid in before_entities for sid in command.created):
        raise SemanticConflict("dependent", command.select or "", "created", None,
                               list(command.created), "created identity already exists")
    if duplicate_source is not None:
        args["from_"] = args.pop("from")
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
    if entry.kind in {"shot", "occurrence"} or entry.op.startswith("shot-group."):
        canonical = {key: value for key, value in canonical.items() if value is not None}
    if relation_choices is not None:
        canonical["relation_choices"] = relation_choices
        canonical["relation_affected"] = relation_affected
    if duplicate_source is not None:
        canonical["from"] = canonical.pop("from_")
    if entry.kind == "track" and entry.op == "timeline.set":
        canonical["values"] = dict(args.get("values", {}))
        canonical["clear"] = list(args.get("clear", []))
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
    if entry.kind == "track":
        selector = owner.scoped_id
    if entry.op in {"clip.set-fade", "clip.set-transition"}:
        selector = selected[0]
    recorded = replace(command, select=selector, args=canonical, version=command.version or TOOL_VERSION,
                       resolved=resolved)
    bindings = dict(command.bindings or {})
    capture_argument_bindings(tl, command, owner, bindings)
    original_moment_time = tl._moment_time
    def bound_moment_time(text, clip=None, *, in_point=False, film_end=None):
        capture_binding(tl, str(text), clip, bindings, film_end=film_end)
        return original_moment_time(text, clip, in_point=in_point, film_end=film_end)
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
        relation_state = []
        if relation_choices is not None and duplicate_source is None:
            relation_state = relations.apply_choices(tl, relation_choices)
        tl._active_relation_choices = relation_choices or []
        if duplicate_source is not None and relation_choices is not None:
            args["relation_choices"] = relation_choices
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
        if relation_state:
            relations.finish(tl, relation_state)
        tl._active_relation_choices = []
        if entry.op == "timeline.inverse":
            undo_media_registry_consequences(tl, before_entities)
        # Retain legacy reservations before a structural handler's deletions
        # disappear. This also covers ripple's direct list edits, without
        # changing the timing implementation or turning no-op setters into edits.
        if cut_reservations(tl.bundle) != previous_reservations:
            retain_cut_reservation_mark(tl.bundle, previous_reservations)
        if entry.op in {"timeline.batch", "timeline.edit-flags"}:
            pass
        elif entry.op in {"timeline.add-cut", "cut.split"}:
            source = split_source if entry.op == "timeline.add-cut" else str(owner.id)
            split_args = {"on": tl._split_moment(canonical["on"], source),
                          "id": result.id, "picture": result.picture.asset if result.picture else None}
            tl._open_edit.calls[index] = replace(recorded, op="cut.split", select=source, args=split_args, resolved=selected_ids(tl, tl.cut(source), "cut"))
        elif entry.op == "cut.duplicate":
            tl._open_edit.calls[index] = replace(recorded, args={**canonical, "id": result.id})
        elif relation_choices is None and entry.op in {"clip.extend", "clip.hold-for", "clip.for", "clip.set-duration"}:
            tl._open_edit.calls[index] = replace(recorded, op="clip.set-duration", args={"seconds": owner.duration})
        elif entry.op == "clip.remove-layer" and result == [owner.id]:
            tl._open_edit.calls[index] = replace(recorded, op="clip.remove", args={})
        elif relation_choices is None and entry.op in {"clip.on", "clip.enter-at", "clip.nudge"}:
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
                    if isinstance(stored, dict):
                        fields[normalized_key] = (copy.deepcopy(stored) if spec.kind == "transition" and "revision" in stored
                                                  else spec.from_stored(stored, fps=tl.fps))
                    else:
                        fields[normalized_key] = stored
                tl._open_edit.calls[index] = replace(recorded, op="clip.set-look", args=fields)
        elif entry.op == "timeline.add" and not isinstance(canonical.get("track"), Mapping):
            canonical["clip_id"] = result.id
            canonical["at"] = intent.on(result.data) or result.start
            tl._open_edit.calls[index] = replace(recorded, args=canonical, resolved=(scoped_clip(tl, result),))
        if entry.op not in {"timeline.batch", "timeline.edit-flags"}:
            call = tl._open_edit.calls[index]
            after_entities = entity_states(tl)
            created = tuple(sorted(set(after_entities) - set(before_entities)))
            created = tuple(sid for sid in created if is_scoped_id(sid))
            if entry.op != "shot-group.set" or canonical.get("intent") != "images":
                from astrid.sdk.timeline.shot_groups import reservations
                if set(created) & set(reservations(tl)):
                    raise CommandError("created identity collides with a retained image snapshot")
            if command.created is not None and not set(created) <= set(command.created):
                raise CommandError(f"Replay changed created identities: expected {command.created}, got {created}")
            paths = direct_paths(tl, entry, owner, canonical, before_entities, after_entities, selected)
            if relation_choices is not None and duplicate_source is None:
                paths = set(paths)
                paths.update((row["target"], relations.path(row["edge"])) for row in relation_choices)
                for row in relation_choices:
                    sid = row["target"]
                    if row["edge"] == "start" or (row["edge"] == "sequence" and row["choice"] == "detach"):
                        paths.add((sid, "intent.fixed_start"))
                    if row["edge"] in {"end", "sequence"}:
                        paths.add((sid, "intent.for"))
                    if row["edge"] == "sequence" and row["choice"] == "detach":
                        paths.add((sid, "intent.on"))
                # Clocks derived from explicit relations/length are compiler output.
                paths = {(sid, path) for sid, path in paths if not (path in {"at", "hold"} and
                         ((intent.on(after_entities.get(sid, {})) and path == "at") or
                          ((intent.until(after_entities.get(sid, {})) or intent.for_s(after_entities.get(sid, {})) is not None)
                           and path == "hold")))}
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
                if path in {"music_bed", "music_bed.ripple_basis"}:
                    # Enrollment consumes an absent basis; subsequent ripples
                    # consume the previous one. Neither can apply twice.
                    basis = "music_bed.ripple_basis"
                    expectations.setdefault(write_key(sid, basis, call.resolved),
                                            state_at(before_entities, sid, basis))
            from astrid.sdk.timeline.music_beds import adopted
            bed = adopted(tl)
            if bed is not None and (entry.kind == "timeline" or
                    any(sid in {a["owner"] for a in bed["arrangements"]} for sid in selected)):
                basis = "music_bed.ripple_basis"
                expectations.setdefault(write_key(bed["owner"], basis, call.resolved),
                                        state_at(before_entities, bed["owner"], basis))
            if entry.op == "timeline.adopt" and canonical.get("sources"):
                for sid in canonical["sources"]:
                    expectations.setdefault(write_key(sid, "music_bed", call.resolved),
                                            state_at(before_entities, sid, "music_bed"))
                    expectations.setdefault(write_key(sid, "", call.resolved),
                                            state_at(before_entities, sid, ""))
            if entry.op == "timeline.inverse":
                for write in canonical["writes"]:
                    if write["path"] == "music_bed":
                        sid = write["id"]
                        expectations.setdefault(write_key(sid, "music_bed", call.resolved),
                                                state_at(before_entities, sid, "music_bed"))
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
            from astrid.sdk.timeline.compiler import _detached_checkout
            snapshots.append((index, _detached_checkout(tl), before_entities, after_entities, command.created))
        return result
    except TimelineEditError as exc:
        # Adapters may add source context to their displayed error; the durable
        # refusal records the command's original typed failure.
        tl._open_edit.failure = str(exc)
        raise
    finally:
        tl._active_relation_choices = []
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
    if clip.shot_id is None:
        return f"parent/clip:{clip.id}"
    rows = [row for row in tl.bundle.get("placements", []) if str(row.get("shot_id")) == clip.shot_id]
    if getattr(clip, "_occurrence_id", None) is not None:
        rows = [row for row in rows if row.get("occurrence_id") == clip._occurrence_id]
    if len(rows) != 1 or not rows[0].get("occurrence_id"):
        raise CommandError(f"Clip {clip.id!r} has no unambiguous occurrence identity")
    value = f"occurrence:{rows[0]['occurrence_id']}/clip:{clip.id}"
    if not is_scoped_id(value):
        raise CommandError(f"Unsupported scoped identity {value!r}")
    return value


def selected_ids(tl, owner, kind):
    if kind == "timeline":
        return ()
    if kind in {"track", "shot", "occurrence"}:
        return (owner.scoped_id,)
    if kind == "clip":
        return (scoped_clip(tl, owner),)
    if kind == "cut":
        if not owner.id:
            raise CommandError("A command needs a named stable cut identity; this cut has no id")
        return (f"cut:{owner.id}",)
    clips = owner.clips if kind == "voice" else owner.steps
    return tuple(scoped_clip(tl, clip) for clip in clips)


def recorded_owner(tl, call, kind):
    if kind == "track":
        if len(call.resolved) != 1:
            raise CommandError("Track removal requires exactly one recorded track")
        sid = call.resolved[0]
        if sid not in entity_states(tl):
            raise SemanticConflict("delete-vs-edit", sid, "", None, None, call.args)
        return track_owner(tl, sid)
    if kind == "cut":
        sid = call.resolved[0]
        if not sid.startswith("cut:") or not any(g["id"] == sid[4:] for g in tl.cut_index.groups(tl)):
            raise SemanticConflict("delete-vs-edit", sid, "", None, None, call.args)
        return tl.cut(sid[4:])
    states = entity_states(tl)
    for sid in call.resolved:
        if sid not in states:
            raise SemanticConflict("delete-vs-edit", sid, "", None, None, call.args)
    # Resolve the authored carrier through placements, including outputs: [].
    # F's lookup owns materialization when composed; never select a generated
    # output merely because it has the same local ID as the authored source.
    first = authored_owner(tl, call.resolved[0], selected=True)
    if kind == "clip":
        if len(call.resolved) != 1:
            raise CommandError("clip operation requires exactly one resolved clip")
        return first
    from astrid.sdk import timeline_intent as intent
    return tl.voice(intent.line(first.data)) if kind == "voice" else tl.sequence(first)


def music_carriers(tl):
    from astrid.sdk.timeline.music_beds import sources
    return sources(tl)


def authored_owner(tl, sid, *, selected=False):
    beds = music_carriers(tl)
    # State lookup alone does not grant an ordinary shared Clip a placement.
    # Recorded commands explicitly select their validated resolved occurrence;
    # F retains that ephemeral selection until materialization in the transaction.
    try:
        from astrid.sdk.timeline.music_beds import authored_clip
    except ModuleNotFoundError as exc:
        if exc.name != "astrid.sdk.timeline.music_beds":
            raise
    else:
        if selected or sid in beds:
            found = authored_clip(tl, sid)
            if found is not None:
                return found
    if sid in beds:
        from astrid.sdk.timeline.model import Clip
        source = beds[sid]["source"]
        rows = [r for r in tl.bundle.get("placements", [])
                if sid == f"occurrence:{r.get('occurrence_id')}/clip:{source['id']}"]
        if sid == f"parent/clip:{source['id']}":
            return Clip(tl, None, source)
        if len(rows) != 1:
            raise CommandError(f"{sid}: ambiguous authored occurrence")
        shot = str(rows[0]["shot_id"])
        if sum(str(r.get("shot_id")) == shot for r in tl.bundle["placements"]) != 1:
            raise CommandError(f"{sid}: materialize occurrence-local authoring shots before mutation")
        return Clip(tl, shot, source)
    from astrid.sdk.timeline.model import Clip
    if sid.startswith("parent/clip:"):
        rows = tl.bundle.get("parent", {}).get("clips", [])
        shot = None
    else:
        occurrences = [r for r in tl.bundle.get("placements", [])
                       if sid.startswith(f"occurrence:{r.get('occurrence_id')}/clip:")]
        if len(occurrences) != 1:
            raise CommandError(f"{sid}: ambiguous or missing occurrence")
        shot = str(occurrences[0]["shot_id"])
        rows = tl._internal(shot)["clips"]
    found = [row for row in rows if row.get("id") == sid.split("/clip:", 1)[1]]
    if len(found) != 1:
        raise CommandError(f"{sid}: missing or duplicate scoped clip")
    return Clip(tl, shot, found[0])


def entity_states(tl):
    """Detached authored state keyed by actual occurrence identities, never aliases."""
    from astrid.sdk.timeline.bed_adoption import validate_adopted
    from astrid.sdk.timeline.music_beds import adopted, validate_occurrence_ownership
    validate_occurrence_ownership(tl)
    validate_adopted(tl)
    bed = adopted(tl)
    arrangements = {a["owner"]: a for a in bed["arrangements"]} if bed else {}
    adopted_projection = {}
    result = {"parent": copy.deepcopy(tl.bundle.get("parent", {}))}
    for kind, rows in (("clip", result["parent"].get("clips", [])),
                       ("track", tl._tracks(None))):
        for item in rows:
            sid = f"parent/{kind}:{item['id']}"
            if sid in result:
                raise CommandError(f"Duplicate scoped identity {sid}")
            result[sid] = copy.deepcopy(item)
    for row in tl.bundle.get("placements", []):
        sid = str(row.get("shot_id"))
        oid = row.get("occurrence_id")
        if not oid:
            # Legacy checkouts can still be read; a selected identity fails closed.
            continue
        if f"occurrence:{oid}" in result:
            raise CommandError(f"Duplicate occurrence identity {oid!r}")
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
    claimed = set()
    carriers = music_carriers(tl)
    for owner, entry in carriers.items():
        if (not isinstance(entry, Mapping) or set(entry) != {"owner", "source", "ripple_basis", "outputs"}
                or entry["owner"] != owner or not isinstance(entry["source"], dict)
                or not isinstance(entry["source"].get("id"), str) or not entry["source"]["id"]
                or not isinstance(entry["outputs"], list) or not isinstance(entry["ripple_basis"], list)):
            raise CommandError(f"{owner}: invalid authored music carrier")
        if not is_scoped_id(owner) or "/clip:" not in owner:
            raise CommandError(f"{owner}: invalid authored owner")
        source = authored_owner(tl, owner)
        if owner in result and entry["source"]["id"] not in entry["outputs"]:
            raise CommandError(f"{owner}: authored source collides with an unrelated clip")
        prefix = owner.rsplit("/clip:", 1)[0] + "/clip:"
        projection = {}
        for local in entry["outputs"]:
            if not isinstance(local, str):
                raise CommandError(f"{owner}: invalid generated output ID")
            key = prefix + local
            if key in claimed or (key != owner and key in carriers):
                raise CommandError(f"{owner}: generated output ownership collision")
            claimed.add(key)
            if owner.startswith("parent/"):
                found = [c for c in tl.bundle.get("parent", {}).get("clips", []) if c.get("id") == local]
                if len(found) > 1:
                    raise CommandError(f"{owner}: duplicate generated output")
                if found:
                    projection[local] = copy.deepcopy(found[0])
                    result.pop(key, None)
            elif key in result:
                projection[local] = result.pop(key)
        result[owner] = copy.deepcopy(source.data)
        if owner in arrangements:
            adopted_projection[arrangements[owner]["id"]] = projection
        else:
            result[owner]["music_bed"] = dict(ripple_basis=copy.deepcopy(entry["ripple_basis"]),
                                               outputs=copy.deepcopy(entry["outputs"]), projection=projection)
    if bed is not None:
        # The reference source survives reordering and empty output lists. This
        # is a detached semantic view, never a second persisted bed authority.
        result[bed["owner"]]["music_bed"] = dict(
            owner=bed["owner"],
            arrangements=[dict(id=a["id"], owner=a["owner"]) for a in bed["arrangements"]],
            switches=copy.deepcopy(bed["switches"]),
            ripple_basis=copy.deepcopy(bed["ripple_basis"]),
            outputs=copy.deepcopy(bed["outputs"]),
            projections={a["id"]: {k: copy.deepcopy(a[k]) for k in
                         ("asset", "source_offset_s", "rate", "gain")} for a in bed["arrangements"]},
            projection=adopted_projection)
    # Carrier storage is represented once under its authored owner. The parent
    # snapshot must not claim a second authored entity for the same source.
    app = result["parent"].get("config", {}).get("app", {}).get("astrid", {})
    # Sources live only in the owner-scoped view, but the authored collection's
    # presence is independent state. Keep its shell so causal recording can
    # distinguish enrollment into an absent collection from an authored {}.
    if "music_beds" in app:
        app["music_beds"] = {}
    app.pop("bed", None)
    if "clips" in result["parent"]:
        result["parent"]["clips"] = [c for c in result["parent"]["clips"]
                                     if f"parent/clip:{c.get('id')}" not in claimed]
    for group in tl.cut_index.groups(tl):
        result[f"cut:{group['id']}"] = copy.deepcopy(tl.cut_note(group['id']))
    from astrid.sdk.timeline.shot_groups import semantic_states
    semantic_states(tl, result)
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
                if not path and key == "modifiers" and (key not in left or key not in right):
                    from astrid.sdk.timeline.modifiers import paths
                    for prop, old, new in paths(left.get(key, []), right.get(key, [])):
                        yield from walk(old, new, prop)
                elif key == "music_bed" and ("arrangements" in (left.get(key) or {}) or
                                              "arrangements" in (right.get(key) or {})):
                    yield from walk(left.get(key, {}), right.get(key, {}), child)
                elif key not in left or key not in right:
                    yield child
                elif not path and key == "modifiers" and isinstance(left[key], list) and isinstance(right[key], list):
                    from astrid.sdk.timeline.modifiers import paths
                    for prop, old, new in paths(left[key], right[key]):
                        yield from walk(old, new, prop)
                else:
                    yield from walk(left[key], right[key], child)
        elif (path.startswith("modifiers[") and isinstance(left, list) and isinstance(right, list)
              and len(left) == len(right)):
            for index, (old, new) in enumerate(zip(left, right)):
                yield from walk(old, new, f"{path}[{index}]")
        else:
            yield path
    for sid in sorted(set(before) | set(after)):
        if sid not in before or sid not in after:
            yield sid, ""
            if "music_bed" in before.get(sid, {}) or "music_bed" in after.get(sid, {}):
                yield sid, "music_bed"
        else:
            for path in walk(before[sid], after[sid], ""):
                yield sid, path.replace("app.astrid.", "intent.", 1) if path.startswith("app.astrid.") else path


def state_at(entities, sid, path):
    if path.startswith("clip_order[") and "/clip:" in sid:
        from astrid.sdk.timeline.shot_groups import order_state
        return order_state(entities, sid, path)
    if path == "clip_order" and "/clip:" in sid:
        prefix = sid.split("/clip:", 1)[0] + "/clip:"
        return dict(state="present", value=[key for key in entities if key.startswith(prefix)])
    if path == "track_order" and "/track:" in sid:
        prefix = sid.split("/track:", 1)[0] + "/track:"
        return dict(state="present", value=[key for key in entities if key.startswith(prefix)])
    if sid not in entities:
        return {"state": "absent"}
    path = "app.astrid." + path[7:] if path.startswith("intent.") else path
    exists, value = _path_state(entities[sid], path)
    if not path and isinstance(value, dict) and "music_bed" in entities[sid]:
        # Whole-source guards contain authored clip state. Carrier/projection
        # changes have separate owner-scoped consequence paths and basis guards.
        value.pop("music_bed", None)
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
    if entry.op == "timeline.duplicate":
        return {("parent", key) for key in ("config", "registry", "clips")} | {
            ("occurrence:" + row["occurrence_id"], "") for row in args["identities"]}
    if entry.op in {"shot-group.set", "shot-group.remove"}:
        from astrid.sdk.timeline.shot_groups import direct_paths as group_paths
        return group_paths(entry, args, before, after)
    if entry.op == "shot.set":
        from astrid.sdk.timeline.graph_commands import SHOT_PATHS
        return {(selected[0], SHOT_PATHS[k]) for k in set(args.get("values") or {}) | set(args.get("clear") or [])}
    if entry.op == "occurrence.add" or (entry.op == "occurrence.set" and args.get("replacement") is not None):
        return {(selected[0], "")}
    if entry.op == "occurrence.set":
        from astrid.sdk.timeline.graph_commands import PLACEMENT_PATHS
        return {(selected[0], PLACEMENT_PATHS[k]) for k in args.get("placement") or {}}
    if entry.op == "clip.set-fade":
        from astrid.sdk.timeline.composite_effects import direct_paths as fade_paths
        return fade_paths(selected[0], args, before[selected[0]])
    if entry.op == "clip.set-transition":
        return {(selected[0], path) for path in ("transition", "elementRef")}
    if entry.op == "clip.remove" and selected and "music_bed" in before.get(selected[0], {}):
        return {(selected[0], ""), (selected[0], "music_bed")}
    if entry.op == "timeline.inverse":
        direct = set()
        for write in args["writes"]:
            sid, path = write["id"], write["path"]
            if path == "music_bed" and any("arrangements" in states.get(sid, {}).get("music_bed", {})
                                            for states in (before, after)):
                direct.update((sid, "music_bed." + key) for key in
                              ("owner", "arrangements", "switches", "ripple_basis")
                              if state_at(before, sid, "music_bed." + key) != state_at(after, sid, "music_bed." + key))
            else:
                direct.add((sid, path))
        return direct
    if entry.kind == "track":
        if entry.op == "timeline.set":
            return {(selected[0], path) for path in set(args.get("values") or {}) | set(args.get("clear") or [])}
        if entry.op == "timeline.add" and "media" in args:
            return {(sid, path) for sid, path in changes if sid not in before and "/clip:" in sid}
        if entry.op == "timeline.reorder":
            return {(selected[0], "track_order")}
        return {(selected[0], "")}
    if entry.op == "timeline.add" and "from" in args:
        # The cloned entity is authored directly; its container array is a
        # consequence. Retain that array in state for older recorded guards.
        return {(sid, path) for sid, path in changes if sid not in before and "/clip:" in sid}
    if entry.op in {"timeline.resolve", "timeline.reflow", "timeline.retime"}:
        return set()  # compiler execution is not a claim of direct authorship
    if entry.op == "clip.declare-quotes":
        return [(selected[0], "app.astrid.copy_quotes")]
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
            from astrid.sdk.timeline.modifiers import handles
            if entry.op == "clip.clear" and key in {"on", "until", "sequence.land"}:
                paths.add((sid, {"on": "intent.on", "until": "intent.until", "sequence.land": "intent.sequence_fit.land"}[key]))
            elif handles(key):
                paths.add((sid, key))
                # Adding/removing an instance changes the list as a consequence.
                # Only an explicit order edit guards order; ID-addressed edits
                # remain replayable after an independent reorder.
            elif key in cf.FIELDS and key not in declared:
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
    if entry.op == "timeline.split":
        targets.update(set(after) - set(before))
    if entry.kind == "cut":
        targets.update(scoped_clip(tl, clip) for clip in owner.clips)
    for sid, path in changes:
        if path == "music_bed" or path.startswith("music_bed."):
            if entry.op == "timeline.adopt" and args.get("sources") and path in {
                    "music_bed", "music_bed.owner", "music_bed.arrangements", "music_bed.switches"}:
                direct.add((sid, path))
            continue
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


def capture_binding(tl, text, clip, bindings, *, film_end=None):
    from astrid.core.timeline import moments as mo
    from astrid.sdk import timeline_intent as intent
    from astrid.sdk.timeline.model import _MomentContext
    ctx = _MomentContext(tl, clip, film_end=film_end)
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
        if moment.kind in {"clip_start", "clip_end"}:
            anchor = moment.clip  # exact edge remains in the lossless reserved TimeRef key
        elif moment.kind == "word":
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
        elif moment.kind == "end" and (clip is None or film_end is not None):
            # The timeline ends with its last picture, even without a named cut.
            # Use that real ScopedId in the existing anchor vocabulary.
            shot_ids = tl._shot_ids()
            last_shot = shot_ids[-1] if shot_ids else None
            pictures = [c for c in tl.clips() if c.shot_id == last_shot and not c.is_audio
                        and (c.track == "plate" or c.element in tl.FULL_FRAME)]
            if not pictures:
                raise CommandError(f"No stable picture anchor for timeline TimeRef {text!r}")
            anchor = scoped_clip(tl, max(pictures, key=lambda c: (c.end, c.id)))
        else:
            cut = moment.cut or (intent.cut_of(clip.data) if clip else None)
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
    from astrid.sdk.timeline._common import TIME_RE
    from astrid.sdk.timeline.model import Clip
    clip = owner if isinstance(owner, Clip) else owner.steps[0] if call.op == "sequence.fit" else None
    for key in ("moment", "on", "until", "at", "when", "before", "land"):
        value = call.args.get(key)
        if key == "when" and call.op in {"clip.enter-at", "clip.end-at"}:
            # These SDK conveniences take a word query/Word handle, not an
            # authored TimeRef. Bind the resulting moment (including offset).
            continue
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
    from astrid.sdk.timeline.music_beds import adopted
    bed = adopted(tl)
    if bed is not None and before.get(bed["owner"], {}).get("music_bed", {}).get("switches") != bed["switches"]:
        from astrid.sdk.timeline._common import TIME_RE
        for switch in bed["switches"]:
            if not TIME_RE.fullmatch(switch["on"].strip()):
                capture_binding(tl, switch["on"], authored_owner(tl, bed["owner"]), bindings)
    for clip in tl.clips():
        sid = scoped_clip(tl, clip)
        if before.get(sid) == after.get(sid):
            continue
        old = before.get(sid, {})
        for reader in (intent.on, intent.until):
            text = reader(clip.data)
            if text and text != reader(old):
                basis = intent.until_film_end(clip.data) if reader is intent.until else None
                capture_binding(tl, text, clip, bindings, film_end=basis)
        for path, expr in intent.formulas(clip.data).items():
            if (isinstance(expr, Mapping) and not expr.get("orphan")
                    and expr.get("moment") and expr != intent.formulas(old).get(path)):
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
    from astrid.sdk.timeline.transaction import _needs_resolve
    for index, probe, before, after, expected_created in snapshots:
        probe._command_depth = 1
        probe._edit_local_only = True
        from astrid.sdk.timeline.store import _snapshot
        if _needs_resolve(tx.calls[:index + 1]) and _snapshot(probe) != _snapshot(tl.__class__(copy.deepcopy(entry))):
            probe.resolve()
        if tx.report is not None:
            probe._check(_admission_context=tx._check_context())
        compiled = entity_states(probe)
        created = tuple(sorted(sid for sid in set(compiled) - set(previous) if is_scoped_id(sid)))
        if expected_created is not None and set(created) != set(expected_created):
            raise CommandError(f"Compiler changed recorded created identities: expected {expected_created}, got {created}")
        tx.calls[index] = replace(tx.calls[index], created=created)
        for sid, path in changed_paths(previous, compiled):
            causes[(sid, path)] = index
            if path in {"music_bed", "music_bed.ripple_basis"}:
                call = tx.calls[index]
                basis = "music_bed.ripple_basis"
                key = write_key(sid, basis, call.resolved)
                expectations = dict(call.expect or {})
                expectations.setdefault(key, state_at(previous, sid, basis))
                tx.calls[index] = replace(call, expect=expectations)
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
        recorded_path = path
        if (path.startswith("modifiers[") or call.op in {"clip.set-fade", "clip.set-transition"}) and kind != "direct":
            # A whole-instance/params replacement owns its changed descendants.
            # Keep the original explicit guard path in the call so authenticated
            # replay checks exactly the same direct-write set.
            parents = []
            for prop, authored in authorship.items():
                if authored == "direct":
                    target, parent = write_target(prop, call.resolved)
                    if target == sid and path.startswith((parent + ".", parent + "[")):
                        parents.append(parent)
            if parents:
                kind, recorded_path = "direct", max(parents, key=len)
        write = dict(id=sid, path=recorded_path, before=state_at(original, sid, recorded_path),
                     after=state_at(final, sid, recorded_path))
        if kind == "direct":
            if write not in direct:
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
    if entry is None and command.op == "timeline.operation":
        # Trusted local contributions expand before publication. Only the existing
        # guarded primitive calls enter the journal and Runtime commit envelope.
        from astrid.core.pack.operations import invoke_operation, resolve_operation
        if (command.select is not None or command.resolved or command.expect is not None
                or command.bindings is not None or command.created is not None or command.authorship is not None
                or set(command.args) != {"identity", "implementation_digest", "inputs"}):
            raise CommandError("operation OpCall requires only identity, implementation_digest and inputs, without a selector or replay guards")
        operation = resolve_operation(command.args["identity"])
        if command.args["implementation_digest"] != operation.implementation_digest:
            raise CommandError("operation implementation changed; rediscover before invoking")
        return invoke_operation(tl, operation, command.args["inputs"], dry_run=dry_run)
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
    if entry is None and command.op == "timeline.operation":
        return run(tl, command)
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
    "clip.set": "set_values allow_new_params",
    "clip.set-param": "add_component instance_id component_params component_pack_roots", "clip.clear": "clear_keys",
    "clip.set-look": "entrance exit fade_in fade_out opacity volume gain_db",
    "clip.on": "on", "clip.enter-at": "at at_word", "clip.until": "until",
    "clip.set-duration": "for_seconds", "clip.extend": "extend", "clip.nudge": "nudge nudge_frames",
    "cut.lift": "lift layers",
    "timeline.close-gap": "close_gap_before", "cut.split": "split cut_id picture", "timeline.add-cut": "add_cut after",
    "timeline.set-cut-note": "why hold", "sequence.fit": "fit_sequence shape_from",
    "timeline.film-end": "film_end", "timeline.retime": "retime", "timeline.insert-time": "insert",
    "clip.keep": "keep", "clip.clear-asset": "clear_asset", "clip.set-beats": "beats",
    "voice.set-gap-after": "gap_after", "timeline.remove-line": "remove_line visuals",
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
    "Checkout": frozenset("copy_inventory copy_lint assets at changed_cut_ids changes check clip cut cut_count cut_note cut_notes_all cut_picture dips document edits empty_cuts expected_cuts find frame gaps lint log narration narration_changes orphans picture_gaps quantize sequence slot_clips time to_shot_seconds verify voice word".split()),
}
STORAGE_BOUNDARIES = frozenset({"discard", "draft", "edit", "load", "open", "pin_narration", "publish", "refresh", "step"})


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
            if (isinstance(node, list) and isinstance(key, str) and
                    path.startswith("modifiers") and match.start() == len("modifiers")):
                if match.group(1) == "order":
                    from astrid.sdk.timeline.modifiers import instances
                    node = list(instances({"modifiers": node}))
                else:
                    from astrid.sdk.timeline.modifiers import instances
                    node = instances({"modifiers": node})[key]
            else:
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
    arguments = {key: _plain(value) for key, value in bound.arguments.items() if key not in {"self", "client"}}
    tl._storage_call = EditCommand(entry.op, None, arguments, TOOL_VERSION, ())
    try:
        return entry.handler(tl, *args, **kwargs)
    finally:
        tl._storage_call = previous


# Scoped structural producers share the one registry, recorder and rebase path.
def _literal_source(clip, *, relation_choices=None):
    from astrid.sdk.timeline import commands

    sid = commands.scoped_clip(clip._tl, clip)
    if (
        sid in commands.music_carriers(clip._tl)
        or (not relation_choices and (intent.on(clip.data) or intent.until(clip.data) or intent.cut_of(clip.data)))
        or intent.sequence(clip.data)
        or intent.line(clip.data)
        or intent.formulas(clip.data)
        or intent.loop(clip.data)
        or intent.orphan(clip.data)
    ):
        raise TimelineEditError(
            "Structural gesture touches tied/grouped content; choose keep, detach, adjust offset or re-anchor explicitly"
        )
    if any(key in clip.data for key in ("keyframes", "modifiers", "keyframeOrigin")):
        raise TimelineEditError(
            "Animated structural copy/split needs an explicit animation partition contract"
        )
    if clip.data.get("hold") is not None and any(key in clip.data for key in ("from", "to")):
        raise TimelineEditError(
            "Combined hold and source range needs an explicit media split/copy contract"
        )
    clip._tl._require_independent_clip(clip)
    return sid


def duplicate_clip_source(
    self, *, from_: str, at: Any, track: str | None = None, clip_id: str | None = None,
    relation_choices: list | None = None
) -> Clip:
    """Copy an exact source within its recorded owner; placement is explicit."""
    tl = self
    from astrid.sdk.timeline import commands

    if not commands.is_scoped_id(from_) or "/clip:" not in from_:
        raise TimelineEditError("timeline.add from requires an exact clip ScopedId")
    source = commands.recorded_owner(
        tl, commands.EditCommand("timeline.add", None, {}, resolved=(from_,)), "clip"
    )
    from astrid.sdk.timeline import relations
    if relation_choices:
        covered = {(r["target"], r["edge"]) for r in relation_choices}
        needed = {(from_, edge) for edge in ("start", "end") if relations.read(source, edge)}
        if not needed <= covered or any(r["target"] != from_ for r in relation_choices):
            raise CommandError("Duplicate needs explicit choices for every copied authored relation")
    _literal_source(source, relation_choices=relation_choices)
    start = tl.quantize(
        tl._moment_time(at, source)
        if isinstance(at, str) and not TIME_RE.fullmatch(at.strip())
        else tl.time(at)
    )
    if start < 0 or (source.shot_id is not None and tl._shot_at(start) != source.shot_id):
        raise TimelineEditError(
            "Duplicate within the source owner; cross-owner insertion requires explicit asset/track mapping"
        )
    data = copy.deepcopy(source.data)
    data["id"] = clip_id or tl._new_id(source.id)
    data["at"] = start - tl._shot_start(source.shot_id)
    if track is not None:
        if not any(t.get("id") == track for t in tl._tracks(source.shot_id)):
            raise TimelineEditError(f"No existing track {track!r} in the source owner")
        data["track"] = track
    # This form is intentionally literal: anchored placement needs its own
    # explicit intent choice rather than detaching an authored source silently.
    if isinstance(at, str) and not relation_choices:
        if not TIME_RE.fullmatch(at.strip()):
            raise TimelineEditError(
                "Anchored duplicate placement requires an explicit intent choice"
            )
    tl._internal(source.shot_id).setdefault("clips", []).append(data)
    from astrid.sdk.timeline.model import Clip

    result = Clip(tl, source.shot_id, data)
    if relation_choices:
        state = relations.apply_choices(tl, relation_choices, copy_target=result)
        # keep is relative to the captured source, not the just-created placement.
        state = [(c, edge, action, old, text, source.start if edge == "start" else source.end)
                 for c, edge, action, old, text, pos in state]
        relations.finish(tl, state)
    return result


def split_clip(self, at: Any) -> Clip:
    """Split one literal clip, keeping its identity on the left and minting right."""
    clip = self
    _literal_source(clip)
    tl = clip._tl
    point = tl.quantize(
        tl._moment_time(at, clip)
        if isinstance(at, str) and not TIME_RE.fullmatch(at.strip())
        else tl.time(at)
    )
    if not clip.start < point < clip.end:
        raise TimelineEditError("Split point must be strictly inside the selected clip")
    end = clip.end
    offset = tl.quantize(point - clip.start)
    data = copy.deepcopy(clip.data)
    data["id"] = tl._new_id(clip.id)
    _trim_front(data, offset)
    data["at"] = point - tl._shot_start(clip.shot_id)
    _trim_end(clip.data, offset)
    if intent.for_s(clip.data) is not None:
        intent.set_for(clip.data, offset)
        intent.set_for(data, tl.quantize(end - point))
    tl._internal(clip.shot_id)["clips"].append(data)
    from astrid.sdk.timeline.model import Clip

    return Clip(tl, clip.shot_id, data)


class _Track:
    """Ephemeral view of an existing track; identity comes from the document."""

    def __init__(self, tl, shot_id, scoped_id, data):
        self._tl, self.shot_id, self.scoped_id, self.data = tl, shot_id, scoped_id, data
        self.id = data["id"]


def track_owner(tl, sid, *, new=False):
    from astrid.sdk.timeline import commands

    if not isinstance(sid, str) or not commands.is_scoped_id(sid) or "/track:" not in sid:
        raise TimelineEditError("Track removal requires an exact track ScopedId")
    prefix, local = sid.split("/track:", 1)
    if prefix == "parent":
        shot = None
    elif prefix.startswith("occurrence:"):
        rows = [
            r
            for r in tl.bundle.get("placements", [])
            if "occurrence:" + str(r.get("occurrence_id")) == prefix
        ]
        if len(rows) != 1:
            raise TimelineEditError(f"Unavailable or ambiguous track owner {prefix!r}")
        shot = str(rows[0]["shot_id"])
    else:
        raise TimelineEditError("Select an occurrence or parent track, not shared-shot content")
    tracks = [t for t in tl._tracks(shot) if t.get("id") == local]
    if new and not tracks:
        tracks = [{"id": local}]
    if len(tracks) != 1:
        raise TimelineEditError(f"Unavailable or ambiguous track {sid!r}")
    owner = _Track(tl, shot, sid, tracks[0])
    tl._require_independent_clip(owner)
    return owner


def track_users(tl, owner):
    """Authoritative authored references, including durable zero-output owners."""
    internal = tl._internal(owner.shot_id)
    users = [c["id"] for c in internal.get("clips", []) if c.get("track") == owner.id]
    prefix = owner.scoped_id.split("/track:", 1)[0] + "/clip:"
    users.extend(
        sid
        for sid, entry in music_carriers(tl).items()
        if sid.startswith(prefix) and entry["source"].get("track") == owner.id
    )
    if owner.shot_id is None:
        users.extend(
            "occurrence:" + str(row.get("occurrence_id"))
            for row in tl.bundle.get("placements", [])
            if row.get("track") == owner.id or row.get("placement", {}).get("track") == owner.id
        )
    for container in (internal, internal.get("config", {})):
        users.extend(
            "pinned-group:" + str(group.get("shotId"))
            for group in container.get("pinnedShotGroups", [])
            if group.get("trackId") == owner.id
        )
    return sorted(set(users))


def remove_track(self):
    """Remove an empty track after explicit authored-content/reference edits."""
    tracks = self._tl._tracks(self.shot_id)
    users = track_users(self._tl, self)
    if users:
        raise CommandError(
            f"Track {self.scoped_id} is occupied by {', '.join(users)}; move or remove those references explicitly first"
        )
    if sum(t.get("kind") == self.data.get("kind") for t in tracks) <= 1:
        raise CommandError("Cannot remove the last track of its kind")
    tracks[:] = [t for t in tracks if t is not self.data]


def unused_track_removals(tl, select):
    """Plan clear-unused as ordinary guarded removals using authored references.

    This helper consumes the authoritative detached document, never UI spans.
    Acceptance/rebase recheck each empty-track removal in the same transaction.
    """
    owner = track_owner(tl, select)
    prefix = owner.scoped_id.split("/track:", 1)[0] + "/track:"
    tracks = tl._tracks(owner.shot_id)
    referenced = {
        row["id"] for row in tracks if track_users(tl, track_owner(tl, prefix + row["id"]))
    }
    retained = set(referenced) | {row["id"] for row in tracks if row["id"] in {"V1", "A1"}}
    for kind in {row["kind"] for row in tracks}:
        if not any(row["kind"] == kind and row["id"] in retained for row in tracks):
            retained.add(next(row["id"] for row in tracks if row["kind"] == kind))
    return [
        dict(
            op="timeline.remove",
            select=prefix + row["id"],
            args={},
            expect={prefix + row["id"] + "::": dict(state="present", value=copy.deepcopy(row))},
        )
        for row in tracks
        if row["id"] not in retained
    ]


_TRACK_FIELDS = {"label", "role", "scale", "fit", "opacity", "volume", "muted", "blendMode"}


def _track_path(path):
    if not isinstance(path, str) or not (
        path in _TRACK_FIELDS
        or path.startswith("app.")
        and all(re.fullmatch(r"[A-Za-z0-9_-]+", p) for p in path.split("."))
    ):
        raise CommandError(f"Unsupported or structural track property {path!r}")


def _validate_track_value(path, value):
    import math

    _track_path(path)
    if path.startswith("app."):
        return  # JSON values, including null and clear-shaped objects, are opaque data.
    enums = {
        "role": {"output", "source"},
        "fit": {"contain", "cover", "manual"},
        "blendMode": {
            "darken",
            "hard-light",
            "lighten",
            "multiply",
            "normal",
            "overlay",
            "screen",
            "soft-light",
        },
    }
    if path in enums:
        valid = isinstance(value, str) and value in enums[path]
    elif path == "label":
        valid = isinstance(value, str)
    elif path == "muted":
        valid = isinstance(value, bool)
    else:
        valid = (
            isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
        )
    if not valid:
        raise CommandError(f"Invalid track {path} value {value!r}")


def guard_track_call(tl, command, owner):
    expectations = dict(command.expect or {})
    sid = owner.scoped_id
    if command.op == "timeline.add":
        if isinstance(command.args.get("track"), Mapping):
            if expectations.get(sid + "::") != {"state": "absent"}:
                raise CommandError("Track add requires whole-entity :: absent expectation")
            parent = tl.bundle["parent"]
            if owner.shot_id is None and "tracks" not in parent and "tracks" not in parent.get("config", {}):
                # Capture creation of the canonical collection so publication
                # inverse can restore tagged absence rather than an empty shell.
                expectations.setdefault("parent::config.tracks", {"state": "absent"})
        elif "media" in command.args:
            local = command.args.get("clip_id")
            if (
                not isinstance(local, str)
                or not local
                or not is_scoped_id(sid.split("/track:")[0] + "/clip:" + local)
            ):
                raise CommandError("Media add requires a reserved clip_id")
            key = sid.split("/track:")[0] + "/clip:" + local + "::"
            if expectations.get(key) != {"state": "absent"}:
                raise CommandError("Media add requires whole-entity :: absent expectation")
    elif command.op == "timeline.set":
        values, clear = command.args.get("values", {}), command.args.get("clear", [])
        if (
            not isinstance(values, Mapping)
            or not isinstance(clear, list)
            or any(not isinstance(p, str) for p in clear)
        ):
            raise CommandError("Track set requires values object and clear array")
        for path in set(values) | set(clear):
            if path not in expectations and sid + "::" + path not in expectations:
                raise CommandError(f"Track set requires captured tagged expectation for {path}")
    elif command.op == "timeline.reorder":
        expected = command.args.get("expected_order")
        key = sid + "::track_order"
        tagged = dict(state="present", value=expected)
        if key in expectations and not _same_value(expectations[key], tagged):
            raise CommandError("Reorder expectation disagrees with expected_order")
        expectations[key] = tagged
    elif command.op == "timeline.remove":
        expectations.setdefault(sid + "::", state_at(entity_states(tl), sid, ""))
    return replace(command, expect=expectations)


def add_track(self, *, track):
    if (
        not isinstance(track, Mapping)
        or track.get("id") != self.id
        or track.get("kind") not in {"audio", "visual"}
        or "label" not in track
    ):
        raise CommandError(
            "Track add requires a complete row matching the selected id, kind and label"
        )
    for key in _TRACK_FIELDS & set(track):
        _validate_track_value(key, track[key])
    if "app" in track and not isinstance(track["app"], Mapping):
        raise CommandError("Track app metadata must be an object")
    tracks = self._tl._tracks(self.shot_id, create=True)
    if any(row.get("id") == self.id for row in tracks):
        raise CommandError("Track identity already exists")
    self.data = copy.deepcopy(dict(track))
    tracks.append(self.data)
    return self


def set_track(self, *, values=None, clear=None):
    values, clear = dict(values or {}), list(clear or [])
    paths = list(values) + clear
    if len(clear) != len(set(clear)) or set(values) & set(clear):
        raise CommandError("Track values and clear conflict or contain duplicate clear paths")
    for path in paths:
        _track_path(path)
    if "label" in clear:
        raise CommandError("Required track label cannot be cleared")
    if any(
        a != b and (a.startswith(b + ".") or b.startswith(a + ".")) for a in paths for b in paths
    ):
        raise CommandError("Overlapping track property paths")
    for path, value in values.items():
        _validate_track_value(path, value)
    for path in paths:
        data = self.data
        parts = path.split(".")
        for part in parts[:-1]:
            if part not in data:
                if path in clear:
                    data = None
                    break
                data[part] = {}
            if not isinstance(data[part], dict):
                raise CommandError(f"Track metadata parent {part!r} is not an object")
            data = data[part]
        if data is not None:
            if path in clear:
                data.pop(parts[-1], None)
            else:
                data[parts[-1]] = copy.deepcopy(values[path])
    return self


def reorder_tracks(self, *, order, expected_order):
    tl = self._tl
    prefix = self.scoped_id.split("/track:")[0] + "/track:"
    tracks = tl._tracks(self.shot_id)
    current = [prefix + t["id"] for t in tracks]
    for items in (order, expected_order):
        if (
            not isinstance(items, list)
            or any(not isinstance(s, str) for s in items)
            or len(items) != len(set(items))
            or set(items) != set(current)
        ):
            raise CommandError("Track order must be a complete unique same-owner permutation")
    if current != expected_order:
        raise SemanticConflict(
            "same-property", self.scoped_id, "track_order", expected_order, current, order
        )
    rows = {prefix + t["id"]: t for t in tracks}
    tracks[:] = [rows[sid] for sid in order]
    return self


def add_project_media(self, *, media, at, clip_id, media_record, duration=None, source_range=None):
    import math

    tl = self._tl

    def positive(value):
        return (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(value)
            and value > 0
        )

    intrinsic = media_record["entry"].get("duration_seconds")
    if intrinsic is not None and not positive(intrinsic):
        raise CommandError("Media intrinsic duration must be finite and positive")
    media_type = media_record["entry"].get("media_type", "")
    if not isinstance(media_type, str) or media_type.split("/")[0] not in {
        "audio",
        "video",
        "image",
    }:
        raise CommandError("Unsupported selected media type")
    start = tl.quantize(tl.time(at))
    local_start = start - tl._shot_start(self.shot_id)
    if local_start < 0 or self.shot_id is not None and local_start >= tl._shot_length(self.shot_id):
        raise CommandError("Media insertion time is outside selected occurrence")
    if media_type.startswith("audio/") and not positive(intrinsic):
        raise CommandError("SFX requires finite positive intrinsic duration")
    if source_range is not None:
        if not isinstance(source_range, list) or len(source_range) != 2:
            raise CommandError("source_range must contain source in/out seconds")
        left, right = source_range
        if (
            not positive(right)
            or not isinstance(left, (int, float))
            or isinstance(left, bool)
            or not math.isfinite(left)
            or left < 0
            or left >= right
            or intrinsic is None
            or right > intrinsic
        ):
            raise CommandError("Invalid intrinsic source range")
        if duration is not None and duration != right - left:
            raise CommandError("Duration contradicts source range")
        duration = right - left
    else:
        left, right = 0, duration
        if duration is None and media_type.startswith("audio/"):
            duration = right = intrinsic
    if not positive(duration) or intrinsic is not None and right > intrinsic:
        raise CommandError("Media needs explicit positive duration within intrinsic range")
    if media_type.startswith("image/") and source_range is not None:
        raise CommandError("Still images require explicit duration")
    entry = dict(
        media_id=media_record["object_id"],
        content_sha256=media_record["object_id"].removeprefix("sha256:"),
        type=media_type,
        duration=intrinsic,
        format=copy.deepcopy(media_record["entry"]),
        generationId=media_record["generation_id"],
        variantId=media_record["variant_id"],
        authoring_metadata={key: copy.deepcopy(media_record[key]) for key in ("generation", "variant")},
    )
    key = media["asset"]
    if not isinstance(key, str) or not key:
        raise CommandError("Media asset key must be a nonempty string")
    assets = tl._registry(self.shot_id)
    if key in assets and (
        not isinstance(assets[key], Mapping)
        or any(not _same_value(assets[key].get(field), value) for field, value in entry.items())
    ):
        raise CommandError("Owner asset-key identity collision")
    registry_inserted = key not in assets
    assets.setdefault(key, entry)
    row = dict(
        id=clip_id,
        clipType="media",
        track=self.id,
        asset=key,
        at=local_start,
        hold=tl.quantize(duration),
        params={},
    )
    if registry_inserted:
        from astrid.sdk.timeline.commit import _digest

        row["app"] = {
            "astrid": {"media_registry_insert": {"asset": key, "entry_digest": _digest(entry)}}
        }
    if not media_type.startswith("image/"):
        row.update({"from": left, "to": right})
    if any(c.get("id") == clip_id for c in tl._internal(self.shot_id).get("clips", [])):
        raise CommandError("Reserved media clip identity already exists")
    tl._internal(self.shot_id).setdefault("clips", []).append(row)
    from astrid.sdk.timeline.model import Clip

    return Clip(tl, self.shot_id, row)


def undo_media_registry_consequences(tl, before):
    """Reverse only the removed insertion's still-unreferenced registry effect.

    The accepted authored clip carries consequence provenance; no project media
    is deleted. A changed entry or any remaining owner reference retains it.
    The canonical publication inverse invokes this inside its transaction.
    """
    from astrid.sdk.timeline.commit import _digest

    after = entity_states(tl)

    def refers(value, key):
        if isinstance(value, Mapping):
            return any(
                (name in {"asset", "assetId"} and child == key) or refers(child, key)
                for name, child in value.items()
            )
        return isinstance(value, list) and any(refers(child, key) for child in value)

    for sid, row in before.items():
        if "/clip:" not in sid or sid in after:
            continue
        mark = row.get("app", {}).get("astrid", {}).get("media_registry_insert")
        if (
            not isinstance(mark, Mapping)
            or set(mark) != {"asset", "entry_digest"}
            or row.get("asset") != mark["asset"]
        ):
            continue
        scope = sid.split("/clip:", 1)[0]
        if scope == "parent":
            shot = None
        else:
            matches = [
                r
                for r in tl.bundle.get("placements", [])
                if "occurrence:" + str(r.get("occurrence_id")) == scope
            ]
            if len(matches) != 1:
                continue
            shot = matches[0]["shot_id"]
        internal = tl._internal(shot)
        assets = internal.get("registry", {}).get("assets", {})
        key = mark["asset"]
        if key not in assets or _digest(assets[key]) != mark["entry_digest"]:
            continue
        sources = [
            internal,
            *[
                entry["source"]
                for owner, entry in music_carriers(tl).items()
                if owner.startswith(scope + "/clip:")
            ],
        ]
        if shot is not None:
            sources.append(tl.bundle["shots"][shot].get("payload", {}))
        if not any(refers(value, key) for value in sources):
            del assets[key]
