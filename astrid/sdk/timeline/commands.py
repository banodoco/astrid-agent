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
    inverse: str = "working-copy-snapshot"
    canonical_name: str | None = None
    aliases: tuple[str, ...] = ()
    canonical_flags: tuple[str, ...] = ()
    sdk_method: str | None = None
    sheet_token: str | None = None

    def __post_init__(self):
        # Local undo remains snapshot based; semantic inverses for rebase are a
        # separate contract. This names the inverse currently used here.
        if self.canonical_name is None:
            object.__setattr__(self, "canonical_name", _CANONICAL_OPERATION.get(self.op, self.op))
        canonical = self.canonical_name
        if not self.aliases and canonical == self.op:
            object.__setattr__(self, "aliases", _OPERATION_ALIASES.get(canonical, ()))
        if not self.canonical_flags:
            object.__setattr__(self, "canonical_flags", _CANONICAL_FLAGS.get(canonical, ()))
        if self.sdk_method is None:
            object.__setattr__(self, "sdk_method", "commands.run")
        if self.sheet_token is None:
            selector = "null" if self.kind == "timeline" else '"CLIP"'
            object.__setattr__(self, "sheet_token",
                               f'edit {{"op":"{canonical}","select":{selector},"args":...}}')


REGISTRY: dict[str, Operation] = {}

_CANONICAL_OPERATION = {
    "clip.for": "clip.set-duration",
    "clip.hold-for": "clip.set-duration",
}
_OPERATION_ALIASES = {
    "clip.set-duration": ("clip.for", "clip.hold-for", "--duration"),
    "voice.replace": ("--line",),
    "timeline.insert-line": ("--gap", "--line-text"),
    "timeline.apply-script": ("--script-gaps",),
    "look.opacity": ("--opacity",),
}
_CANONICAL_FLAGS = {
    "timeline.edit-flags": ("--clip", "--cut", "--file", "--project", "--draft", "--dry-run"),
    "clip.on": ("--on",),
    "clip.until": ("--until",),
    "clip.set-duration": ("--for",),
    "clip.nudge": ("--nudge",),
    "clip.set": ("--set path=value",),
    "look.opacity": ("--set look.opacity=VALUE",),
    "timeline.add-media": ("--add-media",),
    "timeline.crossfade": ("--crossfade", "--over"),
    "clip.trim": ("--trim-in", "--trim-out"),
    "clip.slip": ("--slip",),
    "clip.swap-asset": ("--swap-asset",),
    "timeline.replace-asset": ("--replace-asset",),
    "cut.split": ("--split",),
    "timeline.close-gap": ("--close-gap-before",),
    "timeline.retime": ("--retime",),
    "timeline.insert-time": ("--insert",),
    "timeline.film-end": ("--film-end",),
    "sequence.fit": ("--fit-sequence",),
    "voice.replace": ("--replace-line",),
    "timeline.insert-line": ("--insert-line",),
    "timeline.remove-line": ("--remove-line",),
    "voice.set-gap-after": ("--gap-after",),
    "timeline.apply-script": ("--from-script",),
}


def restore_spec(value):
    """A selected immutable image, never caller-supplied replacement bytes."""
    call = EditCommand.coerce(value)
    args = call.args
    if (call.op != "timeline.restore" or call.select is not None or call.resolved
            or any(v is not None for v in (call.expect, call.bindings, call.created, call.authorship))
            or set(args) - {"revision_id", "content_digest", "publication_id"}
            or not {"revision_id", "content_digest"} <= set(args)
            or not isinstance(args["revision_id"], str) or not args["revision_id"]
            or not isinstance(args["content_digest"], str)
            or re.fullmatch(r"sha256:[0-9a-f]{64}", args["content_digest"]) is None
            or ("publication_id" in args and args["publication_id"] is not None
                and (not isinstance(args["publication_id"], str) or not args["publication_id"]))):
        raise CommandError("timeline.restore requires an exact revision/digest and optional publication identity")
    return call


def _restore_plan(tl, value, *, restore_image=None):
    """Record a complete verified image write without property inverse semantics.

    The commit service supplies a detached request-bound closure, never an OpCall
    argument. Local drafts cannot manufacture canonical restore authority.
    """
    from astrid.sdk.timeline.transaction import Plan
    call = restore_spec(value)
    if restore_image is None:
        raise CommandError("timeline.restore requires the authenticated commit service's verified image")
    if restore_image.get("base_parent") != {"revision_id": call.args["revision_id"],
                                           "content_digest": call.args["content_digest"]}:
        raise CommandError("verified restore image differs from the selected revision/digest")
    before, after = tl.document(), copy.deepcopy(restore_image)
    call = replace(call, version=TOOL_VERSION)
    return Plan("timeline.restore", calls=(call,), changed=before != after,
                changes=("Restore complete immutable timeline image",),
                direct_writes=({"id": "parent", "path": "image",
                                "before": {"state": "present", "value": before},
                                "after": {"state": "present", "value": after}},))


REGISTRY["timeline.restore"] = Operation("timeline.restore", "timeline", "restore", _restore_plan, "image")


def _plain(value):
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    from astrid.core.timeline import moments
    from astrid.sdk.timeline.model import Word
    if isinstance(value, Word) and value.source is not None:
        return moments.format_moment(moments.word_moment(value, (value,)))
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
        canonical = _CANONICAL_OPERATION.get(op, op)
        inverse = "storage-boundary" if boundary == "storage" else "working-copy-snapshot"
        entry = Operation(
            op=op,
            kind=kind,
            method=name,
            handler=function,
            boundary=boundary,
            inverse=inverse,
            canonical_name=canonical,
            aliases=_OPERATION_ALIASES.get(canonical, ()) if op == canonical else (),
            canonical_flags=_CANONICAL_FLAGS.get(canonical, ()),
            sdk_method=f"{cls.__name__}.{name}",
        )
        register_operation(entry)
        setattr(cls, name, _adapter(entry))


def register_operation(entry: Operation) -> Operation:
    """Install one explicit command adapter without generating front-end APIs."""
    if not isinstance(entry, Operation) or not callable(entry.handler):
        raise TypeError("a registry operation needs an Operation and a callable handler")
    if entry.op in REGISTRY:
        raise RuntimeError(f"duplicate operation registry id: {entry.op}")
    REGISTRY[entry.op] = entry
    return entry


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
        arguments = {key: (copy.deepcopy(value) if entry.op == "clip.set-continuous"
                           or (entry.op == "clip.set-beats" and key == "source" and isinstance(value, Mapping))
                           or (entry.op in {"clip.set-shader", "timeline.set-shader-postprocess"} and key == "shader")
                           else _plain(value))
                     for key, value in bound.arguments.items() if key not in {"self", "client"}}
        if any(p.kind == p.VAR_KEYWORD for p in _signature(entry.handler).parameters.values()):
            for key, p in _signature(entry.handler).parameters.items():
                if p.kind == p.VAR_KEYWORD:
                    arguments.update(arguments.pop(key, {}))
        app_keys = entry.op == "clip.set" and bool({"keyframes", "keyframeOrigin"} & kwargs.keys())
        if app_keys:
            arguments = copy.deepcopy(kwargs)  # opaque App keys must bypass numeric/unit normalization
        select = None if entry.kind == "timeline" else str(
            getattr(owner, "id", None) or getattr(owner, "segment", None) or getattr(owner, "seq_id", ""))
        if choices is not None:
            arguments["relation_choices"] = choices
        command = EditCommand.coerce(dict(op=entry.op, select=select, args=arguments))
        if app_keys:
            select = scoped_clip(tl, owner)
            states = entity_states(tl)
            command = replace(command, select=select, expect={path: state_at(states, select, path)
                                                              for path in ("keyframes", "keyframeOrigin")})
        if entry.op in {"clip.set-shader", "timeline.set-shader-postprocess"}:
            from astrid.sdk.timeline.shader_slots import capture_expectations
            command = replace(command, expect=capture_expectations(tl, owner, entry))
        if entry.op == "timeline.restructure":
            run(tl, command)  # same no-write refusal/no-op policy as CLI and COMMIT
            return owner
        label = f"{entry.method}({', '.join(f'{k}={v!r}' for k, v in arguments.items())})"
        with tl.step(label):
            return _invoke(tl, command, entry, owner=owner, sdk_args=(args, kwargs))
    wrapped.__edit_operation__ = entry.op
    return wrapped


def _invoke(tl, command, entry, *, owner=None, sdk_args=None):
    tl._assert_runner_state()
    if entry.boundary == "image":
        raise CommandError("timeline.restore must run alone through the authenticated commit service")
    if getattr(tl._open_edit, "cancelled", False):
        return owner
    app_keys = entry.op == "clip.set" and bool({"keyframes", "keyframeOrigin"} & command.args.keys())
    if ((entry.op in {"clip.set-element", "clip.set-fade", "clip.set-transition", "clip.set-continuous", "clip.set-shader", "clip.set-text"} or app_keys) and owner is None
            and (not command.select or not is_scoped_id(command.select) or "/clip:" not in command.select)):
        raise CommandError(f"{entry.op} requires an exact clip ScopedId")
    if (entry.op == "clip.set-shader" and owner is None and command.resolved
            and command.resolved != (command.select,)):
        raise CommandError("clip shader selector and recorded owner disagree")
    if entry.op == "timeline.add" and ("media" in command.args or isinstance(command.args.get("track"), Mapping)):
        entry = replace(entry, kind="track", handler=add_project_media if "media" in command.args else add_track)
    if entry.op == "clip.swap-asset" and "media" in command.args:
        entry = replace(entry, handler=swap_project_media)
    if entry.op == "timeline.add" and "text" in command.args:
        from astrid.sdk.timeline.manual_text import add as add_manual_text
        entry = replace(entry, handler=add_manual_text)
    if entry.op == "timeline.add" and command.args.get("intent") == "overlay":
        from astrid.sdk.timeline.element_reference import add as add_element_reference
        entry = replace(entry, handler=add_element_reference)
    duplicate_source = None
    if entry.op == "timeline.add" and "from" in command.args:
        entry = replace(entry, kind="timeline", handler=copy_cut_layer if "destination_cut" in command.args else duplicate_clip_source)
        duplicate_source = command.args["from"]
        if "from_" in command.args:
            raise CommandError("Use the documented from argument only")
    layer_copy = duplicate_source is not None and "destination_cut" in command.args
    if layer_copy:
        command = guard_layer_copy(tl, command)
    element_add = entry.op == "timeline.add" and entry.kind == "timeline" and duplicate_source is None
    media_add = entry.op == "timeline.add-media"
    destination = None
    if element_add or media_add:
        command, destination = guard_add_destination(tl, command)
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
        owner = track_owner(tl, command.select, new=entry.op == "timeline.add" and "track" in command.args,
                            resolved=entry.op == "timeline.add" and "media" in command.args)
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
    recording_entry, recording_owner = entry, owner
    if entry.op == "timeline.add-cut":
        # add_cut is the native split convenience. Establish its source-cut
        # context before guards and authorship, while retaining its handler's
        # validation (including after=) and return value. A timeline-wide entry
        # must not claim the generated cut/picture/reservation as direct edits.
        if "on" not in command.args:
            raise CommandError("timeline.add-cut: missing required argument: 'on'")
        moment = tl._split_moment(command.args["on"], None)
        source = tl._cut_on_screen(tl._moment_time(moment, None, in_point=True))
        if source is None:
            raise CommandError("no named cut is on screen to split")
        recording_entry, recording_owner = REGISTRY["cut.split"], tl.cut(source)
    selected = selected_ids(tl, recording_owner, recording_entry.kind)
    if command.resolved and recording_entry.kind != "timeline" and not (entry.op == "timeline.add" and "media" in command.args) and selected != command.resolved:
        raise SemanticConflict("delete-vs-edit", command.select or "", "resolved",
                               list(command.resolved), list(selected), None)
    from astrid.sdk.timeline.identity import cut_reservations, retain_cut_reservation_mark
    previous_reservations = cut_reservations(tl.bundle)
    before_entities = _recording_states(tl)
    if entry.op == "timeline.inverse":
        # A selected-media child owns registry cleanup, but later authored users
        # still own their references. Refuse its whole inverse before deletion.
        for write in command.args.get("writes", []):
            sid = write.get("id") if isinstance(write, Mapping) else None
            row = before_entities.get(sid, {})
            if (isinstance(sid, str) and "/clip:" in sid and write.get("path") == ""
                    and write.get("after") == {"state": "absent"}
                    and row.get("app", {}).get("astrid", {}).get("media_registry_insert")):
                from astrid.sdk.timeline import relations

                if any(row["target"] != sid for row in relations.affected(tl, [sid])):
                    raise CommandError("Selected-media child has later authored dependents")
    if app_keys:
        from astrid.core.timeline import clip_fields as cf
        if not all(any(write_target(key, selected) == (selected[0], path)
                       for key in command.expect or {}) for path in ("keyframes", "keyframeOrigin")):
            raise CommandError("App keyframes require tagged before expectations for the complete pair")
        try:
            cf.app_keyframes(owner.data, command.args)
        except cf.ClipFieldError as exc:
            raise CommandError(str(exc)) from None
        from astrid.sdk.timeline.modifiers import validate_keyframes
        dependencies = validate_keyframes(owner.data, command.args['keyframes'])
        if dependencies and owner.is_audio:
            raise CommandError('modifier keyframes require an owning visual clip subtree')
        expectations = dict(command.expect or {})
        for path, value in dependencies.items():
            expectations.setdefault(write_key(selected[0], path, selected), dict(state='present', value=value))
        command = replace(command, expect=expectations)
    if entry.op in {'clip.set-param', 'clip.clear'}:
        from astrid.sdk.timeline import modifiers
        paths = command.args.get('keys', []) if entry.op == 'clip.clear' else [command.args.get('path', '')]
        addressed = [modifiers.address(path)[0] for path in paths
                     if modifiers.handles(path) and path != 'modifiers.order']
        if addressed:
            expectations = dict(command.expect or {})
            dependencies = modifiers.validate_keyframes(owner.data, owner.data.get('keyframes'))
            elements = {row['element'] for mid, row in modifiers.instances(owner.data).items() if mid in addressed}
            consumed_any = False
            for path, value in dependencies.items():
                consumed = value['identity'] in elements if path.startswith('modifierDeclarations[') else modifiers.address(path)[0] in addressed
                if consumed:
                    consumed_any = True
                    expectations.setdefault(write_key(selected[0], path, selected), dict(state='present', value=value))
            # Removal consumes map presence, even when no curve exists yet.
            # Ordinary static edits keep their historical guards unchanged.
            if entry.op == 'clip.clear' or consumed_any:
                expectations.setdefault(write_key(selected[0], 'keyframes', selected),
                                        state_at(before_entities, selected[0], 'keyframes'))
            command = replace(command, expect=expectations)
    if entry.op == "cut.move":
        # Geometry and membership are inputs to a grouped translation. Rebase
        # must not silently translate a different set or changed layer offsets.
        expectations = dict(command.expect or {})
        for clip in tl._cut_clips(owner.id):
            sid = scoped_clip(tl, clip)
            if command.resolved and sid + "::" not in expectations:
                raise SemanticConflict("dependent", "cut:" + owner.id, "members", None, sid, command.args)
            expectations.setdefault(sid + "::", state_at(before_entities, sid, ""))
        command = replace(command, expect=expectations)
    if duplicate_source is not None:
        if "media" in command.args and (command.created is None or len(command.created) != 1):
            raise CommandError("Selected clone requires one reserved child identity")
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
    if entry.op == "timeline.restructure":
        from astrid.sdk.timeline.chapter_storage import guard as guard_storage
        command = guard_storage(tl, command)
    if entry.op in {"shot-group.set", "shot-group.remove"}:
        from astrid.sdk.timeline.shot_groups import guard
        command = guard(tl, command)
    if entry.op == "clip.overhang-to-hold":
        from astrid.sdk.timeline.overhang import guard
        command = guard(tl, command, owner, before_entities)
    if entry.op == "clip.set-text" or (entry.op == "timeline.add" and "text" in command.args):
        from astrid.sdk.timeline.manual_text import guard as guard_manual_text
        command = guard_manual_text(tl, command, owner, before_entities)
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
    if entry.op == "clip.set-element" or (entry.op == "timeline.add" and command.args.get("intent") == "overlay"):
        from astrid.sdk.timeline.element_reference import guard as guard_element_reference
        command = guard_element_reference(tl, command, owner, before_entities)
    if entry.op == "clip.set-transition":
        from astrid.sdk.timeline.composite_effects import guard_call
        command = guard_call(tl, command, owner, before_entities, selected)
    if entry.op == "clip.set-continuous":
        from astrid.sdk.timeline.continuous import guard_call
        command = guard_call(command, before_entities, selected, sdk=sdk_args is not None)
    if entry.op in {"clip.set-shader", "timeline.set-shader-postprocess"}:
        from astrid.sdk.timeline.shader_slots import guard_call
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
    check_expectations(tl, command, command.resolved if (
        entry.op == "timeline.add" and "media" in command.args or entry.op == "timeline.add-media"
    ) else selected)
    args = copy.deepcopy(dict(command.args))
    relation_choices = args.pop("relation_choices", None)
    relation_affected = args.pop("relation_affected", None)
    if entry.op in {"timeline.add", "timeline.add-media"} and command.created and not isinstance(command.args.get("track"), Mapping):
        clips = [sid for sid in command.created if "/clip:" in sid]
        if len(clips) != 1:
            raise CommandError(f"{entry.op} requires exactly one recorded created clip")
        local = clips[0].split("/clip:", 1)[1]
        if args.get("clip_id") is not None and args["clip_id"] != local:
            raise CommandError(f"{entry.op} clip_id contradicts created identity")
        args["clip_id"] = local
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
        if (layer_copy or (entry.op == "clip.set-element" or entry.op == "timeline.add" and command.args.get("intent") == "overlay")
                or (entry.op == "timeline.inverse" and key == "writes")
                or (entry.op == "clip.set-beats" and key == "source" and isinstance(value, Mapping))
                or (entry.op in {"clip.set-shader", "timeline.set-shader-postprocess"} and key == "shader")
                or entry.op == "clip.set-text" or (entry.op == "timeline.add" and "text" in command.args)):
            # These authored JSON payloads, including beats source mappings,
            # must reproduce the exact saved bundle on detached replay.
            # Preserve opaque values and numeric identity (6.0/6).
            # Keep _plain and the operation/request digest contracts unchanged.
            canonical[key] = copy.deepcopy(value)
        elif entry.op == "clip.set-continuous" and sig.parameters[key].kind == inspect.Parameter.VAR_KEYWORD:
            # The full App slot and parameter payloads are literal authored
            # JSON; recorded replay must retain opaque 1.0 versus 1 too.
            canonical.update(copy.deepcopy(value))
        elif sig.parameters[key].kind == inspect.Parameter.VAR_KEYWORD:
            canonical.update(_plain(value))
        else:
            canonical[key] = _plain(value)
    if app_keys:
        canonical = copy.deepcopy(dict(command.args))
    if entry.kind in {"shot", "occurrence"} or entry.op.startswith("shot-group."):
        canonical = {key: value for key, value in canonical.items() if value is not None}
    if relation_choices is not None:
        canonical["relation_choices"] = relation_choices
        canonical["relation_affected"] = relation_affected
    if duplicate_source is not None:
        canonical["from"] = canonical.pop("from_")
        # Optional acquisition fields must not change ordinary clone replay.
        for key in ("media", "media_record"):
            if canonical.get(key) is None:
                canonical.pop(key, None)
    if entry.kind == "track" and entry.op == "timeline.set":
        canonical["values"] = dict(args.get("values", {}))
        canonical["clear"] = list(args.get("clear", []))
    if "music" in canonical:
        canonical["music"] = str(canonical["music"] or tl.music).replace("-", "_")
    if entry.op in {"timeline.add", "timeline.add-media"} and isinstance(canonical.get("at"), str):
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
    selector = None if recording_entry.kind == "timeline" else str(
        getattr(recording_owner, "id", None) or getattr(recording_owner, "segment", None) or getattr(recording_owner, "seq_id", None) or command.select)
    if entry.kind == "track":
        selector = owner.scoped_id
    if entry.op in {"clip.set-element", "clip.set-fade", "clip.set-transition", "clip.set-continuous", "clip.set-shader", "clip.set-text", "clip.overhang-to-hold"} or app_keys:
        selector = selected[0]
    recorded = replace(command, op=entry.canonical_name, select=selector, args=canonical,
                       version=command.version or TOOL_VERSION,
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
    tl._command_depth = getattr(tl, "_command_depth", 0) + 1
    previous_destination = getattr(tl, "_add_destination", None)
    if element_add or media_add or layer_copy:
        tl._add_destination = destination if element_add or media_add else command.args["destination"]
    try:
        relation_state = []
        if relation_choices is not None and duplicate_source is None:
            relation_state = relations.apply_choices(tl, relation_choices)
        tl._active_relation_choices = relation_choices or []
        if duplicate_source is not None and relation_choices is not None:
            args["relation_choices"] = relation_choices
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
        if entry.op == "clip.overhang-to-hold":
            from astrid.sdk.timeline.overhang import validate_result
            validate_result(owner, result, canonical)
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
            source = str(recording_owner.id)
            split_args = {"on": tl._split_moment(canonical["on"], source),
                          "id": result.id, "picture": result.picture.asset if result.picture else None}
            tl._open_edit.calls[index] = replace(recorded, op="cut.split", select=source, args=split_args)
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
        elif entry.op in {"clip.set", "clip.set-look", "look.opacity"}:
            from astrid.core.timeline import clip_fields as cf
            from astrid.sdk.timeline_address import element_schema
            declared = element_schema(owner.element).get("properties") or {}
            look_fields = {key: value for key, value in canonical.items() if key != "_allow_new"}
            if look_fields and not clear_args and all(key in cf.FIELDS and key not in declared for key in look_fields):
                # The codec supplies a canonical, replayable authoring spelling.
                fields = {}
                for key, value in look_fields.items():
                    path, stored = cf.coerce(key, value, fps=tl.fps, audio=owner.is_audio)
                    normalized_key = next((name for name, spec in cf.FIELDS.items()
                                           if spec.stored_at == path and spec.kind != "fade"), key)
                    spec = cf.FIELDS[normalized_key]
                    if owner.is_audio and spec.kind == "fade":
                        # Audio storage splits duration and curve. Coercing only
                        # the scalar duration would silently replay a linear fade.
                        _, curve = cf._audio_fade_parts(value)
                        fields[normalized_key] = (f"{stored}s curve={curve}"
                                                  if stored is not None and curve != "linear" else stored)
                    elif isinstance(stored, dict):
                        fields[normalized_key] = (copy.deepcopy(stored) if spec.kind == "transition" and "revision" in stored
                                                  else spec.from_stored(stored, fps=tl.fps))
                    else:
                        fields[normalized_key] = stored
                operation = ("look.opacity" if entry.op == "look.opacity"
                             or set(fields) == {"opacity"}
                             else "clip.set-look")
                tl._open_edit.calls[index] = replace(recorded, op=operation, args=fields)
        elif entry.op in {"timeline.add", "timeline.add-media"} and not isinstance(canonical.get("track"), Mapping):
            canonical["clip_id"] = result.id
            canonical["at"] = intent.on(result.data) or result.start
            recorded = replace(recorded, args=canonical, resolved=(scoped_clip(tl, result),))
            if element_add or media_add:
                recorded, _ = guard_add_destination(tl, recorded)
            tl._open_edit.calls[index] = recorded
        if entry.op not in {"timeline.batch", "timeline.edit-flags"}:
            call = tl._open_edit.calls[index]
            after_entities = _recording_states(tl)
            created = tuple(sorted(set(after_entities) - set(before_entities)))
            created = tuple(sid for sid in created if is_scoped_id(sid))
            if entry.op != "shot-group.set" or canonical.get("intent") != "images":
                from astrid.sdk.timeline.shot_groups import reservations
                if set(created) & set(reservations(tl)):
                    raise CommandError("created identity collides with a retained image snapshot")
            if command.created is not None and not set(created) <= set(command.created):
                raise CommandError(f"Replay changed created identities: expected {command.created}, got {created}")
            paths = direct_paths(tl, recording_entry, recording_owner, canonical, before_entities, after_entities, selected)
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
            if recording_entry.kind == "timeline" and entry.op != "timeline.add":
                hit = tuple(sorted({sid for sid, _ in paths if is_scoped_id(sid)}))
                if command.authorship is not None and hit != command.resolved:
                    raise CommandError(f"Replay changed resolved direct targets: expected {command.resolved}, got {hit}")
                call = replace(call, resolved=hit)
            expectations = dict(command.expect or {})
            if entry.op == 'timeline.inverse':
                from astrid.sdk.timeline.modifiers import validate_keyframes
                affected = {write['id'] for write in canonical['writes']
                            if write['path'] == 'keyframes' or write['path'].startswith('modifiers')}
                for sid in affected & after_entities.keys():
                    dependencies = validate_keyframes(after_entities[sid], after_entities[sid].get('keyframes'))
                    for path, value in dependencies.items():
                        expected = (dict(state='present', value=value) if path.startswith('modifierDeclarations[')
                                    else state_at(before_entities, sid, path))
                        expectations.setdefault(write_key(sid, path, call.resolved), expected)
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
            if bed is not None and (recording_entry.kind == "timeline" or
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
        tl._accept_bundle()
        return result
    except TimelineEditError as exc:
        # Adapters may add source context to their displayed error; the durable
        # refusal records the command's original typed failure.
        tl._open_edit.failure = str(exc)
        raise
    finally:
        tl._add_destination = previous_destination
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


def guard_add_destination(tl, call):
    """Keep the existing envelope's exact creation owner through execution/replay."""
    operation = call.op
    created = call.created
    if created is not None and (len(created) != 1 or "/clip:" not in created[0]):
        raise CommandError(f"{operation} requires exactly one created clip ScopedId")
    if call.resolved and (len(call.resolved) != 1 or "/clip:" not in call.resolved[0]):
        raise CommandError(f"{operation} requires exactly one resolved clip ScopedId")
    destination = created[0] if created else call.resolved[0] if call.resolved else None
    if destination is None:
        return call, None  # legacy SDK resolves once; recorder retains that result
    if not is_scoped_id(destination) or (call.resolved and call.resolved != (destination,)):
        raise CommandError(f"{operation} created and resolved identities disagree")
    local = destination.split("/clip:", 1)[1]
    if call.args.get("clip_id") is not None and call.args["clip_id"] != local:
        raise CommandError(f"{operation} clip_id contradicts created identity")
    expectations = dict(call.expect or {})
    for key, expected in expectations.items():
        sid, path = write_target(key, (destination,))
        if not path and "/clip:" in sid and (sid != destination or expected != {"state": "absent"}):
            raise CommandError(f"{operation} creation expectation contradicts destination")
    expectations.setdefault(destination + "::", {"state": "absent"})
    # Use F's existing owner/materialization seam, including shared-shot refusal.
    from astrid.sdk.timeline.music_beds import locate
    shot, _ = locate(tl, destination, {"id": local})
    if shot is not None:
        oid = destination.split("/clip:", 1)[0]
        expectations.setdefault(oid + "::shot_id", {"state": "present", "value": shot})
    return replace(call, args={**call.args, "clip_id": local}, expect=expectations), destination


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
        groups = tl.cut_index.groups(tl, include_orphans=call.op == "cut.move")
        if not sid.startswith("cut:") or not any(g["id"] == sid[4:] for g in groups):
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
    if "storage_policy" in tl.bundle:
        if "storage_policy" in result["parent"]:
            raise CommandError("parent.storage_policy collides with command-owned storage state")
        result["parent"]["storage_policy"] = copy.deepcopy(tl.bundle["storage_policy"])
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
    stored_app = result["parent"].get("config", {}).get("app")
    app = ({} if stored_app is None else stored_app).get("astrid", {})
    app.pop("music_beds", None)
    app.pop("bed", None)
    if "clips" in result["parent"]:
        result["parent"]["clips"] = [c for c in result["parent"]["clips"]
                                     if f"parent/clip:{c.get('id')}" not in claimed]
    # Orphaning hides a cut from timing, not from command/history identity.
    for group in tl.cut_index.groups(tl, include_orphans=True):
        result[f"cut:{group['id']}"] = copy.deepcopy(tl.cut_note(group['id']))
    from astrid.sdk.timeline.shot_groups import semantic_states
    semantic_states(tl, result)
    return result


def _recording_states(tl):
    """Include source-free storage presence for causal receipts and inverse guards.

    Carrier members remain solely in entity_states' owner-scoped views. The
    existing parent property path records absence versus an authored empty
    collection so undo can restore storage exactly, without persisting a marker.
    """
    states = entity_states(tl)
    stored_app = tl.bundle.get("parent", {}).get("config", {}).get("app")
    ns = ({} if stored_app is None else stored_app).get("astrid", {})
    if "music_beds" in ns:
        states["parent"]["config"]["app"]["astrid"]["music_beds"] = {}
    return states


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
                if not path and key == 'keyframes':
                    if not _same_value(left.get(key), right.get(key)) or (key in left) != (key in right):
                        yield child
                elif not path and key == "modifiers" and (key not in left or key not in right):
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
    if sid in entities and path.startswith('modifierDeclarations['):
        from astrid.sdk.timeline.modifiers import declaration_state
        declaration = declaration_state(entities[sid], path)
        if declaration is not None:
            return declaration
    if path == "group_order" and sid.startswith("parent/shot-group:"):
        return dict(state="present", value=[key for key in entities if key.startswith("parent/shot-group:")])
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
    states = _recording_states(tl)
    for key, expected in (call.expect or {}).items():
        sid, path = write_target(key, selected or call.resolved)
        if sid.startswith("occurrence:") and sid.endswith("/shot") and path.startswith("internal_timeline.registry."):
            # A read-only path into this shot's existing internal document;
            # never add a registry mirror to placement or shot payload state.
            oid = sid[len("occurrence:"):-len("/shot")]
            rows = [row for row in tl.bundle.get("placements", []) if row.get("occurrence_id") == oid]
            if len(rows) != 1:
                raise CommandError("Registry expectation requires one exact occurrence")
            observed = {**states, sid: {**states.get(sid, {}), "internal_timeline": tl._internal(rows[0]["shot_id"])}}
            current = state_at(observed, sid, path)
        else:
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
    if entry.op in {"transcript.bind", "transcript.reference"}:
        from astrid.sdk.timeline.transcript_commands import direct_paths as transcript_paths
        return transcript_paths(entry, args, before)
    if entry.op == "timeline.restructure":
        return {("parent", "storage_policy")}
    if entry.op == "timeline.register-asset":
        # Own the entry as a value, including opaque fields and explicit null.
        # Missing containers are authored presence too, so inverse restores
        # their original absence rather than leaving synthesized empty maps.
        parent = before["parent"]
        path = "registry"
        if "registry" in parent:
            path += ".assets"
            if "assets" in parent["registry"]:
                path = _member_path(path, args["asset_id"])
        return {("parent", path)}
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
    if entry.op == "clip.set" and {"keyframes", "keyframeOrigin"} & args.keys():
        return {(selected[0], path) for path in ("keyframes", "keyframeOrigin")}
    if entry.op == "clip.set-fade":
        from astrid.sdk.timeline.composite_effects import direct_paths as fade_paths
        return fade_paths(selected[0], args, before[selected[0]])
    if entry.op == "clip.set-text":
        from astrid.sdk.timeline.manual_text import direct_paths as text_paths
        return text_paths(selected[0], before[selected[0]])
    if entry.op == "clip.set-element" or (entry.op == "timeline.add" and args.get("intent") == "overlay"):
        from astrid.sdk.timeline.element_reference import direct_paths as reference_paths
        return reference_paths(args, selected)
    if entry.op == "clip.set-transition":
        return {(selected[0], path) for path in ("transition", "elementRef")}
    if entry.op == "clip.set-continuous":
        return {(selected[0], "continuous")}
    if entry.op in {"clip.set-shader", "timeline.set-shader-postprocess"}:
        from astrid.sdk.timeline.shader_slots import direct_paths as shader_paths
        return shader_paths(entry.op, selected, before)
    if entry.op == "clip.detach-audio":
        # The handler authors the allocated child, including its complete copied
        # payload. The enclosing clips array remains a storage consequence.
        return {(selected[0], "volume")} | {
            (sid, "") for sid in set(after) - set(before) if "/clip:" in sid}
    if entry.op == "clip.overhang-to-hold":
        return {(selected[0], "to"), (args["child"], "")} | {
            (selected[0], path) for path in ("hold", "intent.for")
            if state_at(before, selected[0], path) != state_at(after, selected[0], path)} | {
            # Detach authors an absolute start even when 7 -> 7.0 compares
            # equal. Its inverse needs the original literal cached clock too.
            (row["target"], "at") for row in args.get("relation_choices", [])
            if row["choice"] == "detach" and row["edge"] in {"start", "sequence"}}
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
    if entry.op == "timeline.add":
        # The cloned entity is authored directly; its container array is a
        # consequence. Retain that array in state for older recorded guards.
        return {(sid, path) for sid, path in changes if sid not in before and "/clip:" in sid}
    if entry.op in {"timeline.resolve", "timeline.reflow", "timeline.retime"}:
        return set()  # compiler execution is not a claim of direct authorship
    if entry.op == "cut.move":
        # The operation explicitly translates the whole authored group, including
        # audio and orphaned layers omitted by the visual Cut.clips projection.
        # Keep its cache writes guarded/invertible alongside the new relations.
        members = {scoped_clip(tl, clip) for clip in tl._cut_clips(owner.id)}
        return {(sid, path) for sid, path in changes if sid in members}
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
                spec = cf.FIELDS[key]
                path = spec.stored_at_audio if owner.is_audio and spec.stored_at_audio else spec.stored_at
                paths.add((sid, path))
                if owner.is_audio and spec.kind == "fade":
                    paths.add((sid, "params.fadeInCurve" if key == "fade_in" else "params.fadeOutCurve"))
            else:
                paths.add((sid, f"params.{key}"))
        # Replacing/clearing a formula also writes its authoring relation.
        paths.update((entity, path) for entity, path in changes if entity == sid and
                     (path.startswith("intent.formulas") or path.startswith("app.astrid")))
        return paths
    direct = set()
    if entry.op == "timeline.crossfade":
        # The canonical macro authors both duration and curve, even when the
        # selected curve equals its previous value. Keep both replay guards.
        for reference, side in ((args["first"], "Out"), (args["second"], "In")):
            sid = scoped_clip(tl, tl.clip(reference))
            direct.update((sid, path) for path in (f"params.fade{side}", f"params.fade{side}Curve"))
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
        anchor = word.source[3] if word.source is not None else f"word:{word.id}"
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
            anchor = word.source[3] if word.source is not None else f"word:{word.id}"
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
    from astrid.core.timeline import moments as mo
    from astrid.sdk.timeline._common import TIME_RE
    from astrid.sdk.timeline.model import Clip
    clip = (owner if isinstance(owner, Clip) else owner.steps[0] if call.op == "sequence.fit" else
            owner.picture if call.op == "cut.move" else None)
    for key in ("moment", "on", "until", "at", "when", "before", "land"):
        value = call.args.get(key)
        if key == "when" and call.op in {"clip.enter-at", "clip.end-at"}:
            # These SDK conveniences take a word query/Word handle, not an
            # authored TimeRef. Bind the resulting moment (including offset).
            continue
        if isinstance(value, str) and not TIME_RE.fullmatch(value.strip()):
            if call.op == "clip.until" and key == "moment" and re.match(r"^\s*for\s+", value):
                # Clip.until delegates this literal duration to hold_for; it
                # has no word/cut anchor to capture before the handler runs.
                continue
            if call.op == "cut.move" and TIME_RE.fullmatch(value.strip().removeprefix("@")):
                continue  # absolute address, resolved by Checkout.time, has no anchor
            # These keys are TimeRefs only on the corresponding timing handlers.
            if call.op in {"clip.on", "clip.until", "clip.enter-at", "clip.end-at", "timeline.add", "timeline.add-media",
                           "cut.split", "cut.move", "timeline.add-cut", "timeline.close-gap", "sequence.fit"}:
                capture_binding(tl, value, clip, bindings)
    # Original exact text survives canonical spelling changes on replay.
    for text in list(bindings):
        anchor = bindings[text]
        if call.op == "cut.move" and anchor == scoped_clip(tl, owner.picture):
            moment = mo.parse(text)
            if moment.kind == "clip_start" and moment.clip == anchor:
                # Internal offsets consume the picture's newly authored onset.
                # Its old word can be missing while this cut is orphaned; the
                # member-root guards retain identity, and the runner binds the
                # changed relations after the handler has re-homed the picture.
                continue
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
        if intent.orphan(after.get(sid, {})):
            continue  # restoring an orphan must not resolve its missing word
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


def _position_reset_guards(call):
    """Keep all eight explicit reset guards, including absent -> absent.

    A reset owns absence of every field, so a receipt-only inverse/redo must
    retain the same guarded paths even when some fields were already absent.
    Other clears and inverse families keep their existing receipt behavior.
    """
    if call.op not in {"clip.clear", "timeline.inverse"}:
        return set()
    from astrid.core.timeline import clip_fields as cf
    paths = {cf.FIELDS[key].stored_at for key in cf.POSITION_RESET_FIELDS}
    direct = {write_target(key, call.resolved) for key, kind in (call.authorship or {}).items()
              if kind == "direct"}
    owners = {sid for sid, _ in direct if "/clip:" in sid}
    return {(sid, path) for sid in owners if all((sid, path) in direct for path in paths) for path in paths}


def finish_recording(tl, tx, entry):
    """Attribute compiler output using actual compiled prefixes of this execution.

    Prefixes are detached probes of already executed bytes, never relative command
    re-execution. The working checkout retains its existing one final resolve.
    """
    snapshots = getattr(tx, "semantic_snapshots", ())
    if not snapshots:
        return
    previous = _recording_states(tl.__class__(copy.deepcopy(entry)))
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
        compiled = _recording_states(probe)
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
    final = _recording_states(tl)
    if not _same_value(previous, final):
        raise CommandError("Compiler prefix evidence does not match the executed candidate")
    original = _recording_states(tl.__class__(copy.deepcopy(entry)))
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
        if (path.startswith("modifiers[") or call.op in {"clip.set-element", "clip.set-fade", "clip.set-transition", "clip.set-continuous", "clip.set-shader", "timeline.set-shader-postprocess", "clip.set-text", "timeline.register-asset", "timeline.inverse"}
                or (call.op == "timeline.add" and call.args.get("intent") == "overlay")
                or (call.op == "clip.set" and "keyframes" in call.args)) and kind != "direct":
            # A whole-instance/params replacement (including inverse restore)
            # owns its changed descendants.
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
    # Track order is a synthetic authored property: entity_states preserves
    # collection iteration order, but changed_paths only sees stored payloads.
    # Use the executed call's explicit authorship, never its aggregate config
    # consequence, to include the complete guarded permutation in the receipt.
    for call in tx.calls:
        for sid, path in sorted(_position_reset_guards(call)):
            write = dict(id=sid, path=path, before=state_at(original, sid, path),
                         after=state_at(final, sid, path))
            if write not in direct:
                direct.append(write)
        for key, kind in (call.authorship or {}).items():
            sid, path = write_target(key, call.resolved)
            if kind != "direct" or path != "track_order" or "/track:" not in sid:
                continue
            old, new = state_at(original, sid, path), state_at(final, sid, path)
            write = dict(id=sid, path=path, before=old, after=new)
            if not _same_value(old, new) and write not in direct:
                direct.append(write)
    # The App inspector authors the pair even when the retained origin is equal.
    # Include both tagged writes; history can then recover the complete clock.
    for call in tx.calls:
        if call.op != "clip.set" or "keyframes" not in call.args:
            continue
        for path in ("keyframes", "keyframeOrigin"):
            sid = call.resolved[0]
            write = dict(id=sid, path=path, before=state_at(original, sid, path),
                         after=state_at(final, sid, path))
            if write not in direct:
                direct.append(write)
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


def run(tl, value, *, dry_run=False, restore_image=None):
    """Run one OpCall atomically and return its change preview."""
    command = EditCommand.coerce(value)
    entry = REGISTRY.get(command.op)
    if entry is not None and entry.boundary == "image":
        return entry.handler(tl, command, restore_image=restore_image)
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
    from astrid.sdk.timeline.transaction import edit_transaction
    with edit_transaction(tl, command.op, dry_run=dry_run, quiet=True,
                          record_no_change=command.op != "timeline.restructure",
                          record_rollback=False) as tx:
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


def _set_look_opacity(self, opacity: Any):
    """The explicit OpCall for the clip-level opacity field."""
    return self.set_look(opacity=opacity)


def _add_media(self, asset: Any, track: str, at: Any, clip_id: str | None = None,
               until: Any = None, for_seconds: float | None = None,
               trim_in: float | None = None, trim_out: float | None = None):
    """One transaction for the legacy ``--add-media`` edit."""
    clip = self.add("media", at=at, asset=asset, track=track, hold=for_seconds,
                    clip_id=clip_id, cut=None)
    if not clip.is_audio:
        raise CommandError(f"--track {track!r} is not an audio track")
    if until is not None:
        clip.until(until)
    source_in = trim_in if trim_in is not None else 0.0
    speed = float(clip.data.get("speed") or 1.0)
    if trim_out is not None:
        source_out = trim_out
    elif for_seconds is None and until is None:
        source_out = clip._source_duration()
    else:
        source_out = source_in + clip.duration * speed
    clip.trim(in_s=source_in, out_s=source_out)
    return clip


def _crossfade(self, first: str, second: str, curve: str | None = None,
               overlap: float | None = None):
    """One timeline OpCall for the legacy ``--crossfade`` edit."""
    from astrid.core.timeline.audio_curves import validate

    a, b = self.clip(first), self.clip(second)
    if not a.is_audio or not b.is_audio:
        raise CommandError("--crossfade requires two audio clips")
    if a.track != b.track:
        raise CommandError(f"--crossfade requires the same audio track ({a.track!r} vs {b.track!r})")
    if b.start < a.start:
        raise CommandError("--crossfade requires A to start before B")
    try:
        selected_curve = validate(curve or "equal-power")
    except ValueError as exc:
        raise CommandError(str(exc)) from None
    if overlap is not None:
        overlap = float(overlap)
        if overlap <= 0:
            raise CommandError("--over must be a positive number of seconds")
        if overlap > min(a.duration, b.duration):
            raise CommandError("--over cannot exceed either clip's duration")
        b.enter_at(a.start + a.duration - overlap)
        print(f"crossfade overlap {overlap:g}s: moved {b.address} start; {a.address} stayed in place")
    else:
        if b.end < a.end:
            raise CommandError("--crossfade requires B to continue through A's end; use --over to position B")
        overlap = min(a.start + a.duration, b.start + b.duration) - max(a.start, b.start)
        if overlap <= 0:
            raise CommandError("clips do not overlap; supply --over SECONDS to move B's start")
    a.set_look(fade_out=f"{overlap}s curve={selected_curve}")
    b.set_look(fade_in=f"{overlap}s curve={selected_curve}")
    print(f"crossfade {a.address} → {b.address}: {overlap:g}s, curve={selected_curve}")
    return {"first": a.id, "second": b.id, "overlap": overlap, "curve": selected_curve}


# This is an explicit adapter map, not a generated parser. Context/report flags
# belong to the executable edit-flags entry; mutation flags name their operation.
_EDIT_FLAG_DECLARATIONS = (
    ("timeline.edit-flags", "project file draft clip cut near n offset labels timings verify sweep no_sweep music add_component like accept_suggestions"),
    ("timeline.add-media", "add_media track clip_id"),
    ("clip.trim", "trim_in trim_out"), ("clip.slip", "slip"),
    ("clip.set", "set_values allow_new_params"),
    ("clip.set-param", "instance_id component_params component_pack_roots"),
    ("clip.clear", "clear_keys"),
    ("clip.set-look", "entrance exit fade_in fade_out volume gain_db"),
    ("look.opacity", "opacity"),
    ("clip.on", "on"), ("clip.enter-at", "at at_word"), ("clip.until", "until"),
    ("clip.set-duration", "for_seconds"), ("clip.extend", "extend"), ("clip.nudge", "nudge nudge_frames"),
    ("cut.lift", "lift layers"),
    ("timeline.close-gap", "close_gap_before"), ("cut.split", "split cut_id picture"),
    ("timeline.add-cut", "add_cut after"), ("timeline.set-cut-note", "why hold"),
    ("sequence.fit", "fit_sequence shape_from"), ("timeline.film-end", "film_end"),
    ("timeline.retime", "retime"), ("timeline.insert-time", "insert"),
    ("clip.keep", "keep"), ("clip.clear-asset", "clear_asset"), ("clip.set-beats", "beats"),
    ("voice.set-gap-after", "gap_after"), ("timeline.remove-line", "remove_line visuals"),
    ("voice.replace", "replace_line take words line_text"),
    ("timeline.insert-line", "insert_line insert_gap_after"),
    ("timeline.apply-script", "from_script takes script_gaps"), ("clip.swap-asset", "swap_asset"),
    ("timeline.replace-asset", "replace_asset"), ("clip.remove-layer", "remove"),
    ("timeline.batch", "command dry_run"), ("timeline.crossfade", "crossfade overlap"),
)


def build_edit_flag_operations(declarations=_EDIT_FLAG_DECLARATIONS):
    """Build a closed destination map and reject duplicate adapter declarations."""
    operations = {}
    for operation, names in declarations:
        for name in names.split():
            if name in operations:
                raise CommandError(f"duplicate edit flag mapping: {name}")
            operations[name] = operation
    return operations


EDIT_FLAG_OPERATIONS = build_edit_flag_operations()


def flag_inventory_table(inventory):
    rows = []
    seen = set()
    for flag in inventory["commands"]["timelines.edit"]["flags"]:
        destination = flag["dest"]
        if destination in seen:
            raise CommandError(f"duplicate edit flag inventory destination: {destination}")
        seen.add(destination)
        operation = EDIT_FLAG_OPERATIONS.get(destination)
        entry = REGISTRY.get(operation)
        if entry is None or not callable(entry.handler):
            raise CommandError(f"unmapped edit flag: {destination}")
        rows.append({"flags": flag["options"], "destination": destination,
                     "operation": entry.canonical_name, "handler": entry.sdk_method})
    return rows


def registry_aliases() -> frozenset[str]:
    """CLI aliases stay parseable but are hidden from canonical generated help."""
    return frozenset(alias for entry in REGISTRY.values() for alias in entry.aliases
                     if alias.startswith("--"))


def render_cli_alias_reference() -> str:
    """Render retained parser aliases and their canonical operation names."""
    rows = {}
    for entry in REGISTRY.values():
        for alias in entry.aliases:
            if alias.startswith("--"):
                rows[alias] = entry.canonical_name
    return "\n".join(f"  {alias:<30} {operation}" for alias, operation in sorted(rows.items()))


def canonical_flag_rows():
    """Canonical spellings projected from the registry, one row per flag."""
    rows = []
    seen = set()
    for entry in REGISTRY.values():
        if entry.canonical_name != entry.op:
            continue
        for flag in entry.canonical_flags:
            key = (flag, entry.canonical_name)
            if key not in seen:
                seen.add(key)
                rows.append({"flag": flag, "operation": entry.canonical_name,
                             "sdk": entry.sdk_method, "sheet": entry.sheet_token})
    return sorted(rows, key=lambda row: (row["flag"], row["operation"]))


def render_canonical_flag_reference(*, markdown=False):
    """Render the same registry projection for ``edit --help`` and the skill."""
    rows = canonical_flag_rows()
    if markdown:
        return "\n".join(
            f"| `{row['flag']}` | `{row['operation']}` | `{row['sdk']}` | `{row['sheet']}` |"
            for row in rows
        )
    return "\n".join(f"  {row['flag']:<30} {row['operation']}" for row in rows)


def replace_skill_reference(text: str) -> str:
    """Replace the marked generated operation table in the timeline skill."""
    begin, end = "<!-- timeline-operation-registry:begin -->", "<!-- timeline-operation-registry:end -->"
    if text.count(begin) != 1 or text.count(end) != 1:
        raise ValueError("timeline skill must contain exactly one operation-registry marker pair")
    before, remainder = text.split(begin, 1)
    _generated, after = remainder.split(end, 1)
    table = ("| Canonical flag | OpCall | SDK adapter | Sheet token |\n|---|---|---|---|\n"
             + render_canonical_flag_reference(markdown=True))
    return before + begin + "\n" + table + "\n" + end + after


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
    states = _recording_states(tl)
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
    states = _recording_states(tl)
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


def layer_copy_call(tl, cut, like, *, at=None, set_values=None, track=None, asset=None):
    """Bind the UI conveniences once; the operation retains exact source/owner identities."""
    target = tl.cut(cut)
    picture = target.picture
    if picture is None or target.id is None:
        raise CommandError("--add needs a named destination picture cut")
    return {"op": "timeline.add", "select": None, "args": {
        "from": scoped_clip(tl, tl.clip(like)), "destination_cut": target.id,
        "destination": scoped_clip(tl, picture), "at": at if at is not None else target.id,
        "set_values": set_values or {}, "track": track, "asset": asset}}


def guard_layer_copy(tl, call):
    """Guard the exact destination cut/owner and copied asset/track dependencies."""
    source_id = call.args.get("from")
    if not isinstance(source_id, str) or not is_scoped_id(source_id) or "/clip:" not in source_id:
        raise CommandError("timeline.add from requires an exact clip ScopedId")
    if {"relation_choices", "relation_affected", "media", "media_record"} & call.args.keys():
        raise CommandError("destination-cut copy authors fresh placement; duplicate relation/media choices are a separate form")
    cid, destination = call.args.get("destination_cut"), call.args.get("destination")
    if not isinstance(cid, str) or not re.fullmatch(r"c\d+[a-z]?", cid):
        raise CommandError("destination_cut requires an exact named cut id")
    picture = tl.cut(cid).picture
    if picture is None or scoped_clip(tl, picture) != destination:
        raise CommandError("destination cut and recorded picture identity disagree")
    source = recorded_owner(tl, replace(call, resolved=(call.args["from"],)), "clip")
    prefix = destination.rsplit("/clip:", 1)[0]
    if call.created and (len(call.created) != 1 or not call.created[0].startswith(prefix + "/clip:")):
        raise CommandError("created identity contradicts destination cut owner")
    if call.resolved and call.resolved != call.created:
        raise CommandError("resolved identity contradicts destination cut creation")
    from astrid.sdk.timeline.music_beds import locate
    locate(tl, destination, {"id": picture.id})  # refuse shared-shot mutation
    cross_owner = scoped_clip(tl, source).rsplit("/clip:", 1)[0] != prefix
    if cross_owner and (call.args.get("track") is None or source.asset is not None and call.args.get("asset") is None):
        raise CommandError("cross-owner layer copy requires explicit destination --track and --swap-asset mapping")
    track = call.args.get("track") or source.track
    destination_track = next((row for row in tl._resolved_tracks(picture.shot_id) if row.get("id") == track), None)
    if destination_track is None:
        raise CommandError(f"destination owner has no track {track!r}")
    if destination_track.get("kind") != "visual":
        raise CommandError("destination layer track must be visual")
    # The existing asset resolver accepts an explicit destination registry key or
    # mapping. Never copy the source owner's asset entry behind the user's back.
    asset = call.args.get("asset") if call.args.get("asset") is not None else source.asset
    if asset is not None and (not isinstance(asset, str) or asset not in tl._registry(picture.shot_id)):
        raise CommandError("layer copy asset must be an existing destination registry key")
    target = tl.cut(cid)
    members = sorted(scoped_clip(tl, clip) for clip in tl._cut_clips(cid))
    span = [target.start, target.end]
    for key, current in (("destination_members", members), ("destination_span", span)):
        if key in call.args and not _same_value(call.args[key], current):
            raise CommandError(f"layer copy {key} changed since preview")
    states, expectations = _recording_states(tl), dict(call.expect or {})
    for sid in (*members, prefix + "/track:" + str(track)):
        expectations.setdefault(sid + "::", state_at(states, sid, ""))
    if prefix.startswith("occurrence:"):
        expectations.setdefault(prefix + "::", state_at(states, prefix, ""))
    assets = {"source": copy.deepcopy(tl._registry(source.shot_id).get(source.asset)),
              "destination": copy.deepcopy(tl._registry(picture.shot_id).get(asset))}
    if "asset_expectations" in call.args and not _same_value(call.args["asset_expectations"], assets):
        raise CommandError("layer copy asset mapping changed since preview")
    return replace(call, args={**call.args, "asset_expectations": assets,
                               "destination_members": members, "destination_span": span}, expect=expectations)



def copy_cut_layer(self, *, from_: str, destination_cut: str, destination: str, at: Any,
                   set_values: Mapping[str, Any] | None = None, track: str | None = None,
                   asset: str | None = None, clip_id: str | None = None, asset_expectations=None,
                   destination_members=None, destination_span=None):
    """A fresh destination-cut layer; ordinary source-owner duplication stays separate."""
    from astrid.core.timeline.astrid_ns import CLIP_INTENT_KEYS
    source = recorded_owner(self, EditCommand("timeline.add", None, {}, resolved=(from_,)), "clip")
    target = self.cut(destination_cut)
    if source.is_audio:
        raise CommandError("--add copies a visual layer; use --add-media for independent audio")
    if any(key in source.data for key in ("keyframes", "keyframeOrigin")):
        raise CommandError("animated layer copy requires an explicit animation placement contract")
    start = self._moment_time(at) if isinstance(at, str) and not TIME_RE.fullmatch(at.strip()) else self.time(at)
    if not target.start <= start < target.end:
        raise CommandError("layer copy placement must be inside the destination cut")
    taken = {c.layer_name for c in self._cut_clips(target.id)}
    layer, suffix = source.layer_name, 2
    base = layer
    while layer in taken:
        layer, suffix = f"{base}-{suffix}", suffix + 1
    # add() owns allocation, retired-image protection, tracks, placement and cut
    # intent. Copy stored element/look payload verbatim, keeping opaque app data.
    clip_id = clip_id or self._new_id(source.element)
    self._add_destination = destination.rsplit("/clip:", 1)[0] + "/clip:" + clip_id
    result = self.add(source.element, at=at, asset=asset if asset is not None else source.asset,
                      track=track or source.track, cut=target.id, layer=layer, clip_id=clip_id)
    fresh = copy.deepcopy(result.data)
    payload = copy.deepcopy(source.data)
    for key in ("id", "at", "hold", "from", "to", "speed", "app"):
        payload.pop(key, None)
    result.data.update(payload)
    app = copy.deepcopy(source.data.get("app", {}))
    for key in CLIP_INTENT_KEYS:
        app.pop(key, None)
        if isinstance(app.get("astrid"), dict):
            app["astrid"].pop(key, None)
    app.setdefault("astrid", {}).update(copy.deepcopy(fresh.get("app", {}).get("astrid", {})))
    result.data["app"] = app
    result.data["track"] = track or source.track
    if "asset" in fresh:
        result.data["asset"] = fresh["asset"]
    # Default duration belongs to the receiving cut, not to the old source.
    result.data["hold"] = self.quantize(target.end - start)
    if source.element == "media" and "to" in source.data:
        result.data.update({k: copy.deepcopy(source.data[k]) for k in ("from", "to", "speed") if k in source.data})
    if set_values:
        result.set(**dict(set_values))
    self._mcache = None
    return result


def duplicate_clip_source(
    self, *, from_: str, at: Any, track: str | None = None, clip_id: str | None = None,
    relation_choices: list | None = None, media=None, media_record=None
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
    if (media is None) != (media_record is None):
        raise CommandError("Selected clone requires captured project media")
    if media is not None:
        import math

        if not isinstance(clip_id, str) or not clip_id or "/" in clip_id or any(
                row.get("id") == clip_id for row in tl._internal(source.shot_id).get("clips", [])):
            raise CommandError("Selected clone reserved child identity is unavailable")
        if track is not None and track != data.get("track"):
            raise CommandError("Selected clone must preserve the source track")
        old_entry = tl._registry(source.shot_id).get(data.get("asset"), {})
        new_type = media_record["entry"].get("media_type", "")
        old_type = old_entry.get("type", "").split("/")[0]
        if old_type not in {"video", "image"} or new_type.split("/")[0] != old_type:
            raise CommandError("Selected clone must preserve the source media kind")
        if old_type == "video":
            duration = media_record["entry"].get("duration_seconds")
            left, right = data.get("from", 0), data.get("to")
            if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v)
                   for v in (left, right, duration)) or not 0 <= left < right <= duration:
                raise CommandError("Selected clone cannot preserve the full source range")
        key, entry, inserted = _register_selected_media(tl, source.shot_id, media, media_record)
        data["asset"] = key
        # Internal registry provenance belongs to the replacement, not its source.
        astrid = data.get("app", {}).get("astrid", {})
        if "media_registry_insert" in astrid:
            del astrid["media_registry_insert"]
        if inserted:
            from astrid.sdk.timeline.commit import _digest

            data.setdefault("app", {}).setdefault("astrid", {})["media_registry_insert"] = {
                "asset": key, "entry_digest": _digest(entry)}
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
    if media is not None:
        # Choice resolution is allowed to edit copied timing intent, but it
        # must not silently retime/trim the selected clone or move its anchor.
        # This runs inside the existing runner transaction, after resolution.
        expected = copy.deepcopy(source.data)
        expected.update(id=data["id"], asset=data["asset"], at=start - tl._shot_start(source.shot_id))

        def preserved_payload(value):
            value = copy.deepcopy(value)
            app = value.get("app")
            if isinstance(app, dict):
                astrid = app.get("astrid")
                if isinstance(astrid, dict):
                    for key in ("on", "until", "for", "fixed_start", "media_registry_insert"):
                        astrid.pop(key, None)
                    if not astrid:
                        app.pop("astrid", None)
                if not app:
                    value.pop("app", None)
            return value

        if preserved_payload(result.data) != preserved_payload(expected):
            raise CommandError("Selected clone choices must preserve full source range, style, track and insertion anchor")
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


def track_owner(tl, sid, *, new=False, resolved=False):
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
    tracks = [t for t in (tl._resolved_tracks(shot) if resolved else tl._tracks(shot)) if t.get("id") == local]
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


def register_asset(self, asset_id: str, entry: Mapping[str, Any]):
    """Explicitly assign one managed parent asset; never infer this from a save.

    This does no acquisition or local-file inspection. Runtime retains authority
    over the referenced object at atomic acceptance. File-only legacy entries
    still need their existing publication adapter.
    """
    from astrid.core.timeline.authoring_bundle import AuthoringBundleError, _media_identity

    if not isinstance(asset_id, str) or not asset_id:
        raise CommandError("asset_id must be a nonempty string")
    if not isinstance(entry, Mapping):
        raise CommandError("entry must be an asset object")
    identities = []
    try:
        for key in ("media_id", "object_id", "digest", "content_digest"):
            if key in entry:
                identities.append(_media_identity(entry[key], f"entry.{key}"))
        if "content_sha256" in entry:
            identities.append(_media_identity(entry["content_sha256"], "entry.content_sha256"))
    except AuthoringBundleError as exc:
        raise CommandError(str(exc)) from None
    if not identities or not any(key in entry for key in ("media_id", "object_id")):
        raise CommandError("Asset registration requires an exact managed source identity")
    if len(set(identities)) != 1:
        raise CommandError("Asset source identities disagree")
    if any(key in entry for key in ("path", "local_path")):
        raise CommandError("Local media paths require an admitted detached inspection producer")
    parent = self.bundle["parent"]
    if "registry" in parent and not isinstance(parent["registry"], dict):
        raise CommandError("Parent registry must be an object")
    registry = parent.get("registry", {})
    if "assets" in registry and not isinstance(registry["assets"], dict):
        raise CommandError("Parent registry.assets must be an object")
    parent.setdefault("registry", {}).setdefault("assets", {})[asset_id] = copy.deepcopy(dict(entry))
    return self


def _register_selected_media(tl, shot_id, media, media_record):
    """Reuse the detached capture supplied by COMMIT; never resolve media here."""
    if any(media_record.get(key) != media.get(key) for key in ("asset", "generation_id", "variant_id", "object_id", "catalog_digest")):
        raise CommandError("Captured clone media identity mismatch")
    media_type = media_record["entry"].get("media_type", "")
    intrinsic = media_record["entry"].get("duration_seconds")
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
    assets = tl._registry(shot_id)
    if key in assets and (
        not isinstance(assets[key], Mapping)
        or any(not _same_value(assets[key].get(field), value) for field, value in entry.items())
    ):
        from urllib.parse import quote

        # The App's compact, object-keyed still-image entry has no detached
        # format/catalog metadata. Reuse only that exact representation, with
        # all owner-selection fields and its canonical object route matching.
        # Extra/opaque fields must not be treated as missing SDK metadata.
        compact = dict(
            file="/api/runtime/v1/objects/" + quote(media_record["object_id"], safe=""),
            media_id=entry["media_id"], type=entry["type"],
            generationId=entry["generationId"], variantId=entry["variantId"],
        )
        if (
            key != media_record["object_id"]
            or not media_type.startswith("image/")
            or intrinsic is not None
            or not _same_value(assets[key], compact)
        ):
            raise CommandError("Owner asset-key identity collision")
    registry_inserted = key not in assets
    assets.setdefault(key, entry)
    return key, assets[key], registry_inserted


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
    key, entry, registry_inserted = _register_selected_media(tl, self.shot_id, media, media_record)
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


def swap_project_media(self, *, media, media_record):
    """Replace the selected clip's source using one request-bound catalog capture.

    Authored clocks, anchors, clip identity and opaque fields remain untouched.
    No ambient file/media adapter is consulted by this semantic lowering.
    """
    import math

    tl = self._tl
    tl._require_independent_clip(self)
    kind = media_record["entry"].get("media_type", "").split("/")[0]
    if kind not in {"image", "video", "audio"} or (self.is_audio != (kind == "audio")):
        raise CommandError("Selected replacement media is incompatible with the clip track")
    if kind in {"video", "audio"}:
        duration = media_record["entry"].get("duration_seconds")
        if (isinstance(duration, bool) or not isinstance(duration, (int, float))
                or not math.isfinite(duration) or duration <= 0):
            raise CommandError("Replacement media requires finite intrinsic duration")
        end = self.data.get("to", self.data.get("hold"))
        if end is not None and (not isinstance(end, (int, float)) or end > duration):
            raise CommandError("Replacement media is shorter than the authored source range")
    key, entry, inserted = _register_selected_media(tl, self.shot_id, media, media_record)
    self.data["asset"] = key
    if inserted:
        from astrid.sdk.timeline.commit import _digest
        self.data.setdefault("app", {}).setdefault("astrid", {})["media_registry_insert"] = {
            "asset": key, "entry_digest": _digest(entry)}
    return self


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
        if "/clip:" not in sid or (sid in after and after[sid].get("asset") == row.get("asset")):
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
