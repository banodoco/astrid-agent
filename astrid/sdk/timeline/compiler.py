"""One behavior-preserving timing materializer for detached checkout inputs.

``commands`` selects existing checkout calls as ``(method, args, kwargs)``;
these are compatibility adapters, not a second edit vocabulary or a replay
journal. The caller supplies the actual old boundary locations and ripple
history. Compilation never infers an edit from previously compiled seconds.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence


# Only timing state crosses the boundary. Transaction/journal objects, clients,
# paths, save state and open edits belong to the caller, never to a compiler copy.
_STATE = (
    "bundle", "fps", "notes", "report", "_mcache", "_music", "_keep_inside",
    "_kept", "_outside", "_scope", "_orphan_reason", "_orphan_cuts", "_moved_ends",
)


@dataclass(frozen=True)
class TimingInputs:
    """Read-only declared state; compile defensively copies all nested values.

    ``bundle`` alone is sufficient for resolve. Checkout adapters also carry the
    existing music policy, old ripple locations/exclusions and diagnostic state.
    Neither these containers nor command arguments are mutated by compilation.
    """

    bundle: Mapping[str, Any]
    state: Mapping[str, Any] = field(default_factory=dict, repr=False)
    _owner: Any = field(default=None, repr=False, compare=False)


@dataclass(frozen=True)
class TimingResult:
    bundle: dict[str, Any]
    values: tuple[Any, ...]
    _state: dict[str, Any] = field(repr=False)
    _origins: Mapping[int, Any] = field(repr=False, compare=False)
    _arguments: tuple = field(repr=False, compare=False)


class _CheckoutCompileError(Exception):
    """Carry partial evaluator state back to the legacy mutable adapter."""

    def __init__(self, result, cause):
        self.result, self.cause = result, cause


def _containers(value, found):
    """Keep the original JSON objects needed to preserve existing SDK handles."""
    if id(value) in found:
        return
    if isinstance(value, (dict, list, tuple, set, frozenset)):
        found[id(value)] = value
        for item in (value.values() if isinstance(value, dict) else value):
            _containers(item, found)


def _records(records, identities):
    if records is None:
        return None
    return [(p, delta, frozenset(identities.get(i, i) for i in skip))
            for p, delta, skip in records]


def compile_timing(inputs: TimingInputs | Mapping[str, Any], commands: Sequence = ()) -> TimingResult:
    """Materialize current timing rules on owned copies, leaving inputs untouched.

    With no commands, resolve moments, sequence fits and formulas, in today's
    order. Explicit calls retain their existing distinctions: retime does not
    evaluate formulas, and reflow uses supplied gaps and old boundary locations.
    """
    from astrid.sdk.timeline_checkout import Checkout
    from astrid.sdk.timeline._timing_evaluators import _TimingEvaluators
    from astrid.sdk.timeline.reflow import COMPILED_METHODS

    if not isinstance(inputs, TimingInputs):
        inputs = TimingInputs(inputs)
    commands = tuple(commands) or (("resolve", (), {}),)
    for method, _args, _kwargs in commands:
        if method not in COMPILED_METHODS:
            raise ValueError(f"not a timing entry point: {method}")

    # Override the compatibility wrappers only on this private instance's class.
    # Nested evaluator calls stay inside this one compilation and copy boundary.
    owner_type = type(inputs._owner) if inputs._owner is not None else Checkout
    workspace_type = type("_TimingWorkspace", (owner_type,), {
        name: getattr(_TimingEvaluators, name) for name in COMPILED_METHODS
        if getattr(getattr(owner_type, name), "_timing_entrypoint", False)
    })
    workspace = object.__new__(workspace_type)
    source = dict(inputs.state, bundle=inputs.bundle)
    originals: dict[int, Any] = {}
    _containers((source, commands), originals)
    memo = {id(inputs._owner): workspace} if inputs._owner is not None else {}
    state, calls = copy.deepcopy((source, commands), memo)
    origins = {id(memo[key]): value for key, value in originals.items() if key in memo}
    identities = {key: id(value) for key, value in memo.items()}

    # Checkout migration also operates exclusively on the owned copy.
    Checkout.__init__(workspace, state.pop("bundle"))
    workspace.__dict__.update(state)
    workspace._scope = _records(workspace._scope, identities)
    workspace._edit_depth = 1  # journal ownership stays with the calling checkout
    values = []

    def result():
        state = {key: getattr(workspace, key) for key in _STATE if hasattr(workspace, key)}
        return TimingResult(workspace.bundle, tuple(values), state, origins, tuple(calls))

    try:
        for method, args, kwargs in calls:
            if method == "_apply_bed_cuts":
                args = (_records(args[0], identities), *args[1:])
            values.append(getattr(workspace, method)(*args, **kwargs))
    except BaseException as exc:
        if inputs._owner is not None:
            raise _CheckoutCompileError(result(), exc) from exc
        raise
    return result()


def _compile_checkout(checkout, method, args, kwargs):
    """Apply compiled state while retaining Clip.data and open scope identities."""
    state = {key: getattr(checkout, key) for key in _STATE if hasattr(checkout, key)}
    # A cached lookup contains handles into the old checkout. Rebuild it on the
    # compiler's copy; it is derived state, never a timing declaration.
    state["_mcache"] = None
    failure = None
    try:
        result = compile_timing(TimingInputs(checkout.bundle, state, checkout), ((method, args, kwargs),))
    except _CheckoutCompileError as exc:
        result, failure = exc.result, exc.cause
    restored = {}

    def adopt(value):
        if id(value) in restored:
            return restored[id(value)]
        target = result._origins.get(id(value), value)
        restored[id(value)] = target
        if isinstance(value, dict):
            items = {key: adopt(item) for key, item in value.items()}
            target.clear()
            target.update(items)
        elif isinstance(value, list):
            target[:] = [adopt(item) for item in value]
        elif isinstance(value, tuple):
            target = tuple(adopt(item) for item in value)
            restored[id(value)] = target
        elif isinstance(value, set):
            items = set(value)
            target.clear()
            target.update(items)
        return target

    # Exclusions are identities, not clip IDs; preserve them across the copy just
    # as the current scope does across two edits with different ripple locations.
    result._state["_scope"] = _records(
        result._state["_scope"], {key: id(value) for key, value in result._origins.items()})
    for key, value in result._state.items():
        setattr(checkout, key, adopt(value))
    # _relay_steps also accepts standalone step dictionaries, outside the bundle.
    # Its compiled argument copies must be adopted even when they are not state.
    adopt(result._arguments)
    if failure is not None:
        raise failure.with_traceback(failure.__traceback__) from None
    return adopt(result.values[0])
