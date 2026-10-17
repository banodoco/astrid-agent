"""App-owned continuous slot, through the existing guarded command runner.

Wire callers supply ``expect.continuous`` (or its exact scoped spelling) as a
tagged full before-state. SDK calls capture that state at their edit boundary.
``continuous`` replaces the complete slot; ``params`` shallow-patches its
parameter map; ``clear=True`` removes presence. Exactly one mode is accepted.
"""
from __future__ import annotations

import copy
import math
from collections.abc import Mapping
from dataclasses import replace

from astrid.sdk.timeline._common import TimelineEditError


def _validate(value):
    if not isinstance(value, dict) or not isinstance(value.get("type"), str) or not value["type"].strip():
        raise TimelineEditError("continuous requires an object with a nonempty effect type")
    if "intensity" in value:
        intensity = value["intensity"]
        if isinstance(intensity, bool) or not isinstance(intensity, (int, float)) or not math.isfinite(intensity):
            raise TimelineEditError("continuous intensity must be a finite number")
    if "params" in value and not isinstance(value["params"], dict):
        raise TimelineEditError("continuous params must be an object")


def guard_call(call, states, selected, *, sdk=False):
    from astrid.sdk.timeline import commands

    sid = selected[0]
    if not sdk and call.select != sid:
        raise commands.CommandError("continuous selector and recorded owner disagree")
    expectations = dict(call.expect or {})
    keys = [key for key in expectations if commands.write_target(key, selected) == (sid, "continuous")]
    if not keys:
        if not sdk:
            raise commands.CommandError("clip.set-continuous requires a tagged full continuous expectation")
        expectations["continuous"] = commands.state_at(states, sid, "continuous")
    else:
        # The recorder's single-owner direct path uses the unscoped spelling.
        # Preserve every supplied guard while retaining that canonical key.
        expectations.setdefault("continuous", copy.deepcopy(expectations[keys[0]]))
    # The runner checks every supplied expectation, including contradictory
    # scoped/unscoped spellings, before entering the source-edit adapter.
    return replace(call, expect=expectations)


def set_continuous(self, **values):
    """Replace, remove, or patch params of this clip's top-level effect slot."""
    if set(values) == {"clear"} and values["clear"] is True:
        self.data.pop("continuous", None)
    else:
        if set(values) == {"continuous"}:
            value = copy.deepcopy(values["continuous"])
        elif set(values) == {"params"}:
            if not isinstance(values["params"], Mapping) or not values["params"]:
                raise TimelineEditError("continuous parameter patch must be a nonempty object")
            value = copy.deepcopy(self.data.get("continuous"))
            _validate(value)
            value["params"] = {**value.get("params", {}), **copy.deepcopy(values["params"])}
        else:
            raise TimelineEditError("choose exactly one of continuous, params, or clear=True")
        _validate(value)
        self.data["continuous"] = value
    self._tl._mcache = None
    return self
