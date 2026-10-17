"""Explicit App inline-text projection; word captions keep their own producer.

Only content/fontSize/color/align and canvas x/y/width have declared am-type
counterparts. Unsupported style/bounds refuse, leaving the caller's fallback
in charge rather than silently flattening its text.
"""
from __future__ import annotations

import copy
import math
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path

import jsonschema

from astrid.sdk.timeline._common import TimelineEditError


def _number(value, name, *, positive=False):
    if (not isinstance(value, (int, float)) or isinstance(value, bool)
            or not math.isfinite(value) or (positive and value <= 0)):
        raise TimelineEditError(f"manual text {name} must be finite" + (" and positive" if positive else ""))
    return value


def projection(text, geometry):
    if not isinstance(text, Mapping) or set(text) - {"content", "fontSize", "color", "align"}:
        raise TimelineEditError("manual text supports content/fontSize/color/align only; retain App fallback for other styles")
    if not isinstance(text.get("content"), str):
        raise TimelineEditError("manual text content must be a string")
    if not isinstance(geometry, Mapping) or set(geometry) - {"x", "y", "width"}:
        raise TimelineEditError("manual text maps canvas x/y/width only; retain App fallback for height/other bounds")
    params = dict(text=text["content"], size=text.get("fontSize", 64),
                  color=text.get("color", "#ffffff"), align=text.get("align", "center"),
                  reveal="none", tracking=0, lineHeight=1.1)
    _number(params["size"], "fontSize", positive=True)
    if params["size"] < 1:
        raise TimelineEditError("manual text fontSize below one pixel would be clamped by am-type")
    if (not isinstance(params["color"], str) or not isinstance(params["align"], str)
            or params["align"] not in {"left", "center", "right"}):
        raise TimelineEditError("manual text color/align cannot map to am-type")
    for key, value in geometry.items():
        params[key] = _number(value, key, positive=key == "width")
        if key == "width" and value < 1:
            raise TimelineEditError("manual text width below one pixel would be clamped by am-type")
    return params


def declaration(ref, params, clip_id):
    from astrid.core.element import catalog
    from astrid.sdk.invocation import (
        CapabilityValidationError,
        _admitted_timeline_element_resources,
    )

    if (not isinstance(ref, Mapping) or set(ref) != {"id", "kind", "packId", "revision"}
            or (ref["id"], ref["kind"], ref["packId"]) != ("am-type", "effect", "astrid_motion")
            or not isinstance(ref["revision"], str) or ref["revision"].startswith("draft-")):
        raise TimelineEditError("manual text requires the pinned astrid_motion/am-type effect declaration")
    try:
        registry = catalog._registry()
        element = registry.get("effects", "am-type", pack_id=ref["packId"])
        descriptor = catalog._element_descriptor(element)
        if any(descriptor[key] != ref[key] for key in ("id", "kind", "packId", "revision")):
            raise ValueError("stale text declaration")
        binding = _admitted_timeline_element_resources(
            {"clips": [{"id": clip_id, "elementRef": {**ref, "kind": "effects"}}]}, registry=registry
        )[clip_id]["declaration"]
        implementations = {(Path(binding["pack_root"]) / r["path"]).resolve()
                           for r in binding["resources"] if r["kind"] in {"implementation", "support"}}
        if element.component.resolve() not in implementations:
            raise ValueError("text implementation is not bound by the admitted declaration")
        jsonschema.Draft202012Validator.check_schema(descriptor["schema"])
        jsonschema.Draft202012Validator(descriptor["schema"]).validate({**descriptor["defaults"], **params})
    except (KeyError, ValueError, CapabilityValidationError, jsonschema.ValidationError, jsonschema.SchemaError) as exc:
        raise TimelineEditError(f"invalid manual text declaration/resource: {exc}") from None


def guard(tl, call, owner, states):
    """Capture existing owner/track/order dependencies in the ordinary envelope."""
    from astrid.sdk.timeline import commands
    create = call.op == "timeline.add"
    if create:
        if not call.created or len(call.created) != 1 or not getattr(tl, "_open_edit", None):
            raise TimelineEditError("manual text creation requires the exact App allocated ScopedId")
        sid = call.created[0]
        if call.args.get("clip_id") != sid.split("/clip:", 1)[1]:
            raise TimelineEditError("manual text clip_id must equal the App allocated identity")
        track = call.args.get("track")
    else:
        sid = commands.scoped_clip(tl, owner)
        track = owner.track
    prefix = sid.split("/clip:", 1)[0]
    expected = dict(call.expect or {})
    dependencies = [(sid, "clip_order"), (prefix + "/track:" + str(track), "")]
    if prefix != "parent":
        dependencies.append((prefix, ""))
    if not create:
        dependencies.extend((sid, path) for path in ("clipType", "text", "elementRef", "track", "x", "y", "width", "height"))
    for target, path in dependencies:
        expected.setdefault(target + "::" + path, commands.state_at(states, target, path))
    return replace(call, expect=expected)


def add(self, *, element, text, elementRef, action, time_domain, track, clip_id, expected_order, clip_order,
        geometry=None, at=None):
    """Append exactly the caller's text identity/action/order, without allocating."""
    from astrid.sdk.timeline import commands
    from astrid.sdk.timeline.model import Clip
    from astrid.sdk.timeline.music_beds import locate

    destination = getattr(self, "_add_destination", None)
    if element != "am-type" or not destination or not isinstance(action, Mapping):
        raise TimelineEditError("manual text needs am-type, an explicit owner and the App action")
    if set(action) != {"id", "start", "end", "effectId"} or action["id"] != clip_id or action["effectId"] != "effect-" + clip_id:
        raise TimelineEditError("manual text action identity/fields do not match the allocated clip")
    start = _number(action["start"], "action.start")
    end = _number(action["end"], "action.end")
    if (not isinstance(time_domain, str) or time_domain not in {"timeline-seconds", "owner-seconds"}
            or start < 0 or end <= start):
        raise TimelineEditError("manual text action has invalid or contradictory timing")
    params = projection(text, {} if geometry is None else geometry)
    declaration(elementRef, params, clip_id)
    shot, members = locate(self, destination, {"id": clip_id})
    origin = self._shot_start(shot) if time_domain == "owner-seconds" else 0
    start, end = start + origin, end + origin
    if at is not None and at != start:
        raise TimelineEditError("manual text at contradicts the action clock")
    tracks = [r for r in self._resolved_tracks(shot) if r.get("id") == track]
    if len(tracks) != 1 or tracks[0].get("kind") != "visual":
        raise TimelineEditError("manual text requires the selected canonical visual track")
    actual = [r["id"] for r in members if r.get("track") == track]
    if (not isinstance(expected_order, list) or expected_order != actual
            or len(actual) != len(set(actual)) or clip_order != [*actual, clip_id]):
        raise TimelineEditError("manual text requires exact App source order and appended clip order")
    if any(r.get("id") == clip_id for r in members):
        raise TimelineEditError("manual text allocated identity already exists")
    local_start = start - self._shot_start(shot)
    if local_start < 0 or (shot is not None and end > self._shot_start(shot) + self._shot_length(shot)):
        raise TimelineEditError("manual text action is outside the selected occurrence")
    data = dict(id=clip_id, clipType=element, track=track, at=local_start, hold=end-start,
                text=copy.deepcopy(dict(text)), params=params, elementRef=copy.deepcopy(dict(elementRef)))
    data.update(copy.deepcopy(dict(geometry or {})))
    members.append(data)
    self._mcache = None
    result = Clip(self, shot, data)
    if shot is not None:
        result._occurrence_id = destination.split("/clip:", 1)[0].split(":", 1)[1]
    assert commands.scoped_clip(self, result) == destination
    return result


def set_text(self, *, text, elementRef):
    """Project an App text replacement, preserving every unrelated clip field."""
    if self.data.get("clipType") not in {"text", "am-type"} or "text" not in self.data:
        raise TimelineEditError("manual text edit requires an inline App text clip; word captions use their own producer")
    tracks = [r for r in self._tl._resolved_tracks(self.shot_id) if r.get("id") == self.track]
    if len(tracks) != 1 or tracks[0].get("kind") != "visual":
        raise TimelineEditError("manual text requires the selected canonical visual track")
    if "params" in self.data and not isinstance(self.data["params"], dict):
        raise TimelineEditError("manual text parameters must be an object")
    geometry = {key: self.data[key] for key in ("x", "y", "width", "height") if key in self.data}
    params = projection(text, geometry)
    declaration(elementRef, params, self.id)
    if self.data.get("elementRef") not in (None, elementRef):
        raise TimelineEditError("manual text edit cannot replace a different renderer declaration")
    self.data.update(clipType="am-type", text=copy.deepcopy(dict(text)), elementRef=copy.deepcopy(dict(elementRef)))
    self.data.setdefault("params", {}).update(params)
    return self


def direct_paths(sid, before):
    paths = {"clipType", "text", "elementRef"}
    mapped = {"text", "size", "color", "align", "reveal", "tracking", "lineHeight", "x", "y", "width"}
    if "params" not in before:
        paths.add("params")
    else:
        paths.update("params." + key for key in mapped)
    return {(sid, path) for path in paths}
