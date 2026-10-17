"""Captured App shader metadata, separate from executable shader admission."""
from __future__ import annotations

import copy
from collections.abc import Mapping
from dataclasses import replace

from astrid.sdk.timeline import commands


def _validate(shader, scope):
    if not isinstance(shader, Mapping) or shader.get("scope") != scope:
        raise commands.CommandError("shader metadata requires the correct slot scope")
    for key in ("shaderId", "extensionId", "contributionId"):
        if not isinstance(shader.get(key), str) or (not shader[key] and key != "extensionId"):
            raise commands.CommandError(f"shader metadata requires a valid {key}")
    for key in ("label", "sourceHash"):
        if key in shader and not isinstance(shader[key], str):
            raise commands.CommandError(f"shader {key} must be a string")
    if "enabled" in shader and not isinstance(shader["enabled"], bool):
        raise commands.CommandError("shader enabled must be boolean")
    for key in ("uniforms", "keyframes", "textures", "metadata"):
        if key in shader and not isinstance(shader[key], Mapping):
            raise commands.CommandError(f"shader {key} must be an object")
    for keys in shader.get("keyframes", {}).values():
        if not isinstance(keys, list) or any(
            not isinstance(k, Mapping) or type(k.get("time")) not in {int, float}
            or "value" not in k or not isinstance(k.get("interpolation"), str)
            or k["interpolation"] not in {"linear", "hold"}
            for k in keys
        ):
            raise commands.CommandError("shader keyframes require time/value/interpolation rows")
    for texture in shader.get("textures", {}).values():
        if (not isinstance(texture, Mapping)
                or not isinstance(texture.get("kind"), str)
                or texture["kind"] not in {"clip-frame", "static-image-asset", "live-generated-frame"}
                or ("ref" in texture and not isinstance(texture["ref"], str))):
            raise commands.CommandError("invalid shader texture reference")


def _layout(op, selected, states):
    if op == "clip.set-shader":
        sid, prefix, slot = selected[0], "", "shader"
        data = states[sid]
    else:
        sid, prefix, slot = "parent", "config.", "shaderPostprocess"
        data = states[sid].get("config")
    if not isinstance(data, Mapping) or (data.get("app") is not None and not isinstance(data["app"], Mapping)):
        raise commands.CommandError("shader owner config/app must be an object or absent/null app")
    return sid, prefix + "app." + slot, prefix + "app", data


def direct_paths(op, selected, states):
    sid, slot_path, app_path, data = _layout(op, selected, states)
    return {(sid, slot_path if isinstance(data.get("app"), Mapping) else app_path)}


def capture_expectations(tl, owner, entry):
    """The synchronous SDK adapter captures before entering its mutation."""
    selected = commands.selected_ids(tl, owner, entry.kind)
    states = commands.entity_states(tl)
    sid, path, _, _ = _layout(entry.op, selected, states)
    return {commands.write_key(sid, path, selected): commands.state_at(states, sid, path)}


def guard_call(tl, call, owner, states, selected):
    if call.op == "timeline.set-shader-postprocess" and call.resolved:
        raise commands.CommandError("postprocess shader owner must be the timeline parent")
    sid, slot_path, _, _ = _layout(call.op, selected, states)
    expectations = dict(call.expect or {})
    if not any(commands.write_target(key, selected) == (sid, slot_path) for key in expectations):
        raise commands.CommandError("shader operation requires the captured tagged old slot expectation")
    if call.args.get("shader") is not None:
        _validate(call.args["shader"], "clip" if call.op == "clip.set-shader" else "postprocess")
    for target, path in direct_paths(call.op, selected, states):
        expectations.setdefault(commands.write_key(target, path, selected), commands.state_at(states, target, path))
    return replace(call, expect=expectations)


def _set(tl, owner, op, shader):
    data = owner.data if op == "clip.set-shader" else tl.bundle["parent"]["config"]
    slot = "shader" if op == "clip.set-shader" else "shaderPostprocess"
    if shader is None:
        if isinstance(data.get("app"), dict):
            data["app"].pop(slot, None)
    else:
        _validate(shader, "clip" if op == "clip.set-shader" else "postprocess")
        if data.get("app") is None:
            data["app"] = {}
        data["app"][slot] = copy.deepcopy(dict(shader))
    tl._mcache = None
    return owner


def set_shader(self, shader):
    """Set full clip app.shader metadata, or explicitly remove with None."""
    return _set(self._tl, self, "clip.set-shader", shader)


def set_shader_postprocess(self, shader):
    """Set full parent config.app.shaderPostprocess metadata, or remove with None."""
    return _set(self, self, "timeline.set-shader-postprocess", shader)
