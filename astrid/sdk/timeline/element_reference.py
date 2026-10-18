"""Pinned App element assignments and overlays in the existing OpCall runner.

Source rows remain opaque, in the existing single-row-per-ID container. Tagged
fields are explicit author intent; compiler/cache changes are never its writes.
"""
from __future__ import annotations

import copy
import hashlib
import math
from collections.abc import Mapping

from astrid.sdk.timeline._common import TimelineEditError

FIELDS = {"elementRef", "params", "entrance"}


def tagged(value):
    return isinstance(value, dict) and ((value.get("state") == "absent" and set(value) == {"state"})
        or (value.get("state") == "present" and set(value) == {"state", "value"}))


def reference(ref):
    if (not isinstance(ref, Mapping) or set(ref) - {"id", "kind", "revision", "packId"}
            or not {"id", "kind", "revision"} <= set(ref)
            or ref["kind"] not in {"effect", "animation"}
            or any(not isinstance(ref[k], str) or not ref[k] for k in ref)):
        raise TimelineEditError("element requires one complete effect/animation reference")
    return dict(ref)


def source_path(config, ref):
    app = config.get("app")
    if not isinstance(app, dict):
        return "config.app"
    elements = app.get("elements")
    if not isinstance(elements, dict) or ref is None:
        return "config.app.elements"
    from astrid.sdk.timeline.commands import _member_path
    return _member_path("config.app.elements", ref["id"])


def _row(config, ref):
    app = config.get("app")
    elements = app.get("elements") if isinstance(app, dict) else None
    return elements.get(ref["id"]) if isinstance(elements, dict) else None


def validate_binding(config, binding, states):
    from astrid.sdk.timeline import commands
    if binding is None:
        return
    if not isinstance(binding, dict) or set(binding) != {"owner", "key", "reference", "row", "row_json", "content_digest"} or not tagged(binding["row"]):
        raise TimelineEditError("element requires a tagged complete source row")
    ref = reference(binding["reference"])
    encoded = binding["row_json"]
    try:
        from astrid.sdk.timeline.saved_intent import _finite_json
        captured = _finite_json(encoded)
    except (ValueError, TypeError):
        raise TimelineEditError("element source digest requires the exact finite JSON row") from None
    if (binding["owner"] != "parent" or binding["key"] != ref["id"]
            or not commands._same_value(captured, binding["row"])
            or binding["content_digest"] != "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()):
        raise TimelineEditError("element source owner/key/content digest does not match its complete row")
    app = config.get("app")
    rows = app.get("elements") if isinstance(app, dict) else None
    actual = dict(state="present", value=rows[ref["id"]]) if isinstance(rows, dict) and ref["id"] in rows else dict(state="absent")
    if not commands._same_value(binding["row"], actual):
        raise TimelineEditError("element source binding does not match the complete source row")
    if actual["state"] == "present":
        row = actual["value"]
        if (not isinstance(row, dict) or any(row.get(k) != ref.get(k) for k in ("kind", "revision", "packId"))
                or not isinstance(row.get("source"), str) or not row["source"].strip()
                or ("id" in row and row["id"] != ref["id"])):
            raise TimelineEditError("element source has a conflicting identity or missing source")
    else:
        # Resolve the actual declared catalog, never trust a caller's label.
        from astrid.core.element import catalog
        try:
            registry = catalog._registry()
            element = registry.get(ref["kind"] + "s", ref["id"], pack_id=ref.get("packId"))
            descriptor = catalog._element_descriptor(element)
            if any(descriptor.get(k) != ref.get(k) for k in ("id", "kind", "revision", "packId")):
                raise ValueError("incomplete or stale catalog reference")
        except (KeyError, ValueError) as exc:
            raise TimelineEditError(f"element declaration is unavailable or ambiguous: {exc}") from None
    if actual["state"] == "present":
        for sid, data in states.items():
            other = data.get("elementRef") if "/clip:" in sid and isinstance(data, dict) else None
            if isinstance(other, dict) and other.get("id") == ref["id"] and not commands._same_value(other, ref):
                raise TimelineEditError("single-row element source has conflicting remaining reference users")


def guard(tl, call, owner, states):
    from astrid.sdk.timeline import commands
    from astrid.sdk.timeline.inverse import _write
    create = call.op == "timeline.add"
    args = call.args
    fields = args.get("fields")
    if not isinstance(fields, dict) or set(fields) != FIELDS or not all(tagged(v) for v in fields.values()):
        raise TimelineEditError("element fields require exact tagged reference/params/entrance states")
    if args.get("intent") not in {"assign", "update", "clear", "overlay"} or create != (args["intent"] == "overlay"):
        raise TimelineEditError("element intent does not match the operation")
    if create:
        if not call.created or len(call.created) != 1 or not call.created[0].startswith("parent/clip:"):
            raise TimelineEditError("element overlay requires one reserved parent clip identity")
        sid = call.created[0]
        if args.get("clip_id") != sid.split("/clip:", 1)[1]:
            raise TimelineEditError("element overlay allocated identity disagrees")
    else:
        sid = commands.scoped_clip(tl, owner)
        if not sid.startswith("parent/clip:"):
            raise TimelineEditError("element reference source owner must be the parent timeline")
    before = states.get(sid, {})
    refstate = fields["elementRef"]
    ref = refstate.get("value")
    if ref is not None:
        reference(ref)
    if args["intent"] == "clear":
        if refstate != {"state": "absent"}:
            raise TimelineEditError("element clear must explicitly remove elementRef")
        prior = before.get("elementRef")
        entrance = before.get("entrance")
        matching = isinstance(prior, dict) and isinstance(entrance, dict) and entrance.get("type") == prior.get("id")
        expected_entrance = {"state": "absent"} if matching else commands.state_at(states, sid, "entrance")
        if fields["entrance"] != expected_entrance or fields["params"] != commands.state_at(states, sid, "params"):
            raise TimelineEditError("element detach may clear only its matching entrance and reference")
    elif not isinstance(ref, dict):
        raise TimelineEditError("element assignment/update requires a reference")
    params = fields["params"]
    if params["state"] == "present" and params["value"] is not None and not isinstance(params["value"], dict):
        raise TimelineEditError("element params must be absent, null or an object")
    mode = args.get("params_mode")
    prior_params = before.get("params")
    if mode not in {"keep", "merge", "replace", "clear"}:
        raise TimelineEditError("element params_mode requires keep/merge/replace/clear")
    if mode == "keep" and not create and params != commands.state_at(states, sid, "params"):
        raise TimelineEditError("element keep cannot alter params")
    if mode == "clear" and params != {"state": "absent"}:
        raise TimelineEditError("element clear params requires absence")
    if mode == "merge" and (params["state"] != "present" or not isinstance(params["value"], dict)
            or (prior_params is not None and not isinstance(prior_params, dict))):
        raise TimelineEditError("element merge params requires object parameters")
    # The caller captures the complete merged result, including untouched keys.
    if mode == "merge" and isinstance(prior_params, dict) and not set(prior_params) <= set(params["value"]):
        raise TimelineEditError("element merge cannot remove existing parameter keys")
    entrance = fields["entrance"]
    if entrance["state"] == "present" and entrance["value"] is not None:
        value = entrance["value"]
        if (not isinstance(value, dict) or not isinstance(value.get("type"), str)
                or not isinstance(value.get("duration"), (int, float)) or isinstance(value["duration"], bool)
                or not math.isfinite(value["duration"]) or value["duration"] <= 0):
            raise TimelineEditError("element entrance must retain a valid animation span")
    if entrance != commands.state_at(states, sid, "entrance") and args["intent"] != "clear":
        prior = before.get("elementRef")
        old_entrance = before.get("entrance")
        if entrance["state"] == "absent":
            if not isinstance(prior, dict) or not isinstance(old_entrance, dict) or old_entrance.get("type") != prior.get("id"):
                raise TimelineEditError("element command cannot clear an unrelated entrance")
        elif not isinstance(ref, dict) or ref["kind"] != "animation" or entrance["value"].get("type") != ref["id"]:
            raise TimelineEditError("element entrance must match its animation reference")
    binding = args.get("binding")
    if ref is not None and (not isinstance(binding, dict) or binding.get("reference") != ref):
        raise TimelineEditError("element reference must equal its source binding")
    path, source = args.get("source_path"), args.get("source")
    binding_ref = binding.get("reference") if isinstance(binding, dict) else None
    config = tl.bundle["parent"].get("config", {})
    if path != source_path(config, binding_ref) or not tagged(source):
        raise TimelineEditError("element requires the exact source-container presence path")
    expected = call.expect or {}
    dependencies = [("parent", path)] + ([] if create else [(sid, p) for p in sorted(FIELDS)])
    if create:
        dependencies.extend([(sid, ""), (sid, "clip_order"), ("parent/track:" + str(args.get("track")), "kind")])
    for target, prop in dependencies:
        key = target + "::" + prop
        if key not in expected and not (target == sid and prop in expected):
            raise TimelineEditError(f"element requires captured tagged expectation for {key}")
    trial = copy.deepcopy(tl.bundle["parent"])
    _write(trial, path, source)
    if binding_ref:
        previous_row = _row(config, binding_ref)
        old_app = config.get("app")
        old_rows = old_app.get("elements") if isinstance(old_app, dict) else None
        if isinstance(old_rows, dict) and binding_ref["id"] in old_rows and not isinstance(previous_row, dict):
            raise TimelineEditError("element source has a conflicting null/malformed row")
        next_row = _row(trial.get("config", {}), binding_ref)
        if previous_row is not None and not commands._same_value(previous_row, next_row):
            raise TimelineEditError("single-row element source refuses overwrite/collision")
        # Only insertion of this exact complete row may alter the source container.
        allowed = copy.deepcopy(tl.bundle["parent"])
        if isinstance(binding, dict) and binding["row"]["state"] == "present" and previous_row is None:
            app = allowed.setdefault("config", {}).setdefault("app", {})
            if not isinstance(app, dict) or not isinstance(app.setdefault("elements", {}), dict):
                raise TimelineEditError("element source container is null or malformed")
            app["elements"][binding_ref["id"]] = copy.deepcopy(binding["row"]["value"])
        if not commands._same_value(trial, allowed):
            raise TimelineEditError("element command may insert only its own source row")
    elif not commands._same_value(trial, tl.bundle["parent"]):
        raise TimelineEditError("element detach cannot change the source")
    validate_binding(trial.get("config", {}), binding, states)
    return call


def _source(tl, source_path, source):
    from astrid.sdk.timeline.inverse import _write
    _write(tl.bundle["parent"], source_path, source)


def set_element(self, *, intent, fields, binding, source_path, source, params_mode):
    from astrid.sdk.timeline.inverse import _write
    for path, state in fields.items():
        _write(self.data, path, state)
    _source(self._tl, source_path, source)
    return self


def add(self, *, intent, fields, binding, source_path, source, params_mode, clip_id, track,
        at, duration, time_domain, clip_type, expected_order, insertion_index):
    from astrid.sdk.timeline.model import Clip
    from astrid.sdk.timeline.inverse import _write
    if time_domain != "timeline-seconds" or any(not isinstance(v, (int, float)) or isinstance(v, bool)
            or not math.isfinite(v) for v in (at, duration)) or at < 0 or duration <= 0:
        raise TimelineEditError("element overlay requires finite timeline-seconds placement")
    tracks = [r for r in self._resolved_tracks(None) if r.get("id") == track]
    if len(tracks) != 1 or tracks[0].get("kind") != "visual":
        raise TimelineEditError("element overlay requires an exact canonical visual track")
    members = self.bundle["parent"]["clips"]
    actual = [r["id"] for r in members]
    if expected_order != actual or len(actual) != len(set(actual)) or type(insertion_index) is not int or not 0 <= insertion_index <= len(actual):
        raise TimelineEditError("element overlay requires exact captured clip order and insertion position")
    ref = fields["elementRef"]["value"]
    if clip_type != (ref["id"] if ref["kind"] == "effect" else "effect-layer"):
        raise TimelineEditError("element overlay clip type contradicts reference")
    data = dict(id=clip_id, at=at, hold=duration, track=track, clipType=clip_type)
    for path, state in fields.items():
        _write(data, path, state)
    members.insert(insertion_index, data)
    _source(self, source_path, source)
    self._mcache = None
    return Clip(self, None, data)


def direct_paths(call, selected):
    paths = {("parent", call["source_path"])}
    if call["intent"] == "overlay":
        return paths | {("parent/clip:" + call["clip_id"], "")}
    return paths | {(selected[0], p) for p in FIELDS}


def guard_inverse(tl, writes):
    """Removing a newly inserted source cannot strand a later reference user."""
    from astrid.sdk.timeline import commands
    from astrid.sdk.timeline.inverse import _inverse_states
    if not any(w["id"] == "parent" and w["path"].startswith("config.app") for w in writes):
        return
    states = commands._recording_states(tl)
    trial = _inverse_states(states, writes)
    old_config = states["parent"].get("config", {})
    config = trial["parent"].get("config", {})
    for sid, row in trial.items():
        ref = row.get("elementRef") if "/clip:" in sid else None
        if isinstance(ref, dict) and _row(old_config, ref) is not None and _row(config, ref) != _row(old_config, ref):
            raise TimelineEditError("element source inverse has a remaining dependent reference user")
