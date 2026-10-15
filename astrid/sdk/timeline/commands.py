"""EditCommand producer and runner, using the existing OpCall wire vocabulary.

Adapters call registered operations. Nested operations join the active transaction;
only its outer boundary resolves timing. Calls retain authoring units, never stored
sprite units. Runtime semantic commit remains outside this local runner.
"""
from __future__ import annotations

import copy
import inspect
import json
from dataclasses import dataclass
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

    def to_json(self):
        return dict(op=self.op, select=self.select, args=copy.deepcopy(dict(self.args)),
                    version=self.version, resolved=list(self.resolved))

    @classmethod
    def coerce(cls, value):
        if isinstance(value, cls):
            value = value.to_json()
        if not isinstance(value, Mapping):
            raise CommandError("an EditCommand must be an OpCall object")
        if set(value) - {"op", "select", "args", "version", "resolved"}:
            raise CommandError("unknown EditCommand fields")
        if not isinstance(value.get("op"), str) or "select" not in value or not isinstance(value.get("args"), Mapping):
            raise CommandError("an EditCommand requires op, select and args")
        if value["select"] is not None and not isinstance(value["select"], str):
            raise CommandError("select must be a string or null")
        if value.get("version") is not None and not isinstance(value["version"], str):
            raise CommandError("version must be a string or null")
        if not isinstance(value.get("resolved", []), (list, tuple)) or any(not isinstance(x, str) for x in value.get("resolved", [])):
            raise CommandError("resolved must contain stable string ids")
        try:
            args = json.loads(json.dumps(value["args"], allow_nan=False))
        except (ValueError, TypeError) as exc:
            raise CommandError(f"args must be finite JSON values: {exc}") from None
        return cls(value["op"], value["select"], args)


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
    args = dict(command.args)
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
    resolved = () if entry.kind == "timeline" else (str(getattr(owner, "id", None) or command.select),)
    recorded = EditCommand(command.op, resolved[0] if resolved else None, canonical, TOOL_VERSION, resolved)
    index = len(tl._open_edit.calls)
    tl._open_edit.calls.append(recorded)
    from astrid.sdk.timeline.store import _snapshot
    before = _snapshot(tl)
    split_source = None
    if entry.op == "timeline.add-cut":
        split_source = tl._cut_on_screen(tl._moment_time(str(canonical["on"])))
    if entry.op == "timeline.close-gap":
        from astrid.core.timeline import moments
        canonical["before"] = moments.format_moment(moments.parse(str(canonical["before"])))
    tl._command_depth = getattr(tl, "_command_depth", 0) + 1
    try:
        if entry.op == "clip.set-param":
            owner._check_param_keys([canonical["path"]])
        result = entry.handler(owner, *sdk_args[0], **sdk_args[1]) if sdk_args is not None else entry.handler(owner, *positional, **args)
        if _snapshot(tl) == before:
            del tl._open_edit.calls[index:]
        elif entry.op in {"timeline.batch", "timeline.edit-flags"}:
            del tl._open_edit.calls[index]
        elif entry.op in {"timeline.add-cut", "cut.split"}:
            from astrid.core.timeline import moments
            split_args = {"on": moments.format_moment(moments.parse(str(canonical["on"]))),
                          "id": result.id, "picture": result.picture.asset if result.picture else None}
            source = split_source if entry.op == "timeline.add-cut" else str(owner.id)
            tl._open_edit.calls[index] = EditCommand("cut.split", source, split_args, TOOL_VERSION, (source,))
        elif entry.op in {"clip.extend", "clip.hold-for", "clip.for", "clip.set-duration"}:
            tl._open_edit.calls[index] = EditCommand("clip.set-duration", str(owner.id), {"seconds": owner.duration}, TOOL_VERSION, (str(owner.id),))
        elif entry.op == "clip.remove-layer" and result == [owner.id]:
            tl._open_edit.calls[index] = EditCommand("clip.remove", str(owner.id), {}, TOOL_VERSION, (str(owner.id),))
        elif entry.op in {"clip.on", "clip.enter-at", "clip.nudge"}:
            from astrid.sdk import timeline_intent as intent
            moment = intent.on(owner.data)
            if moment:
                tl._open_edit.calls[index] = EditCommand("clip.on", str(owner.id), {"moment": moment}, TOOL_VERSION, (str(owner.id),))
        elif entry.op in {"clip.set", "clip.set-look"}:
            from astrid.core.timeline import clip_fields as cf
            from astrid.sdk.timeline_address import element_schema
            declared = element_schema(owner.element).get("properties") or {}
            if canonical and all(key in cf.FIELDS and key not in declared for key in canonical):
                # The codec supplies a canonical, replayable authoring spelling.
                fields = {}
                for key, value in canonical.items():
                    path, stored = cf.coerce(key, value, fps=tl.fps, audio=owner.is_audio)
                    normalized_key = next((name for name, spec in cf.FIELDS.items()
                                           if spec.stored_at == path and spec.kind != "fade"), key)
                    spec = cf.FIELDS[normalized_key]
                    fields[normalized_key] = spec.from_stored(stored, fps=tl.fps) if isinstance(stored, dict) else stored
                tl._open_edit.calls[index] = EditCommand("clip.set-look", str(owner.id), fields, TOOL_VERSION, (str(owner.id),))
        elif entry.op == "timeline.add":
            canonical["clip_id"] = result.id
            tl._open_edit.calls[index] = EditCommand(command.op, None, canonical, TOOL_VERSION, (result.id,))
        return result
    except TimelineEditError as exc:
        # Adapters may add source context to their displayed error; the durable
        # refusal records the command's original typed failure.
        tl._open_edit.failure = str(exc)
        raise
    finally:
        tl._command_depth -= 1


def compact_calls(calls, tl=None):
    """Canonicalize equivalent setter spellings and collapse overwritten writes.

    This operates on executed OpCalls, never on a document-only proxy. Structural
    and timing operations retain their ordering.
    """
    result = []
    for call in calls:
        args, op = copy.deepcopy(dict(call.args)), call.op
        if op in {"clip.for", "clip.hold-for"}:
            op = "clip.set-duration"
        if op == "clip.set-param":
            args, op = {args["path"]: args["value"]}, "clip.set"
        item = EditCommand(op, call.select, args, call.version, call.resolved)
        if result and op in {"clip.set", "clip.set-look"} and result[-1].op == op and result[-1].select == call.select:
            old = result.pop()
            item = EditCommand(op, call.select, {**old.args, **args}, call.version, call.resolved)
        if op == "clip.swap-asset":
            found = next((i for i in range(len(result)-1, -1, -1)
                          if result[i].op == "timeline.add" and call.select in result[i].resolved), None)
            if found is not None:
                old = result[found]
                result[found] = EditCommand(old.op, old.select, {**old.args, "asset": args["asset"]}, old.version, old.resolved)
                continue
        if op == "timeline.reflow" and result and result[-1].op == "voice.set-gap-after" and not result[-1].args.get("reflow") and args.get("gaps") is None and args.get("points") is None:
            old = result.pop()
            result.append(EditCommand(old.op, old.select, {**old.args, "reflow": True}, old.version, old.resolved))
            continue
        result.append(item)
    if tl is not None:
        # A layer added through a projection can acquire its on/for immediately
        # afterwards. Lower that construction to the same fully specified add.
        current_ids = {clip.id for clip in tl.clips()}
        added = {c.resolved[0] for c in result if c.op == "timeline.add" and c.resolved and c.resolved[0] in current_ids}
        folded = []
        from astrid.sdk import timeline_intent as intent
        for call in result:
            if call.op == "timeline.add" and call.resolved and call.resolved[0] in added:
                clip = tl.clip(call.resolved[0])
                args = {**call.args, "asset": clip.asset, "track": clip.track,
                        "cut": clip.cut_id, "layer": clip.layer_name,
                        "at": intent.on(clip.data) or clip.start,
                        "anchor": bool(intent.on(clip.data)), "hold": intent.for_s(clip.data) or clip.duration}
                call = EditCommand(call.op, None, args, call.version, call.resolved)
            elif call.select in added and call.op in {"clip.on", "clip.set-duration"}:
                continue
            folded.append(call)
        result = folded
    return tuple(result)


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
    from astrid.sdk.timeline.doc import _PATH_RE
    node = data
    for match in _PATH_RE.finditer(path):
        key = match.group(1) if match.group(1) is not None else int(match.group(2))
        try:
            node = node[key]
        except (KeyError, IndexError, TypeError):
            return False, None
    return True, copy.deepcopy(node)


def direct_writes(tl, calls):
    """Capture explicit fixed param/clear writes before the timing compiler runs."""
    from astrid.sdk import timeline_intent as intent
    clips = {clip.id: clip for clip in tl.clips()}
    guards = {}
    for call in calls:
        clip_id = call.select
        if call.op == "timeline.add" and call.resolved:
            clip_id = call.resolved[0]
            keys = list((call.args.get("params") or {}))
        elif call.op == "clip.set":
            keys = [key for key in call.args if key != "_allow_new"]
        elif call.op == "clip.set-param":
            keys = [call.args["path"]]
        elif call.op == "clip.clear":
            keys = list(call.args.get("keys", []))
        else:
            continue
        clip = clips.get(clip_id)
        if clip is None:
            continue  # explicitly removed later in this edit
        formulas = intent.formulas(clip.data)
        for key in keys:
            path = f"params.{key}"
            if any(p == path or p.startswith((path + ".", path + "[")) for p in formulas):
                continue  # a formula is an explicit request for compiler evaluation
            guards[(clip_id, path)] = _path_state(clip.data, path)
    return guards


def guard_direct_writes(tl, guards):
    clips = {clip.id: clip for clip in tl.clips()}
    for (clip_id, path), state in guards.items():
        if clip_id not in clips or _path_state(clips[clip_id].data, path) != state:
            raise CommandError(f"{clip_id}.{path}: timing replaced an explicit write; edit rolled back")



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
