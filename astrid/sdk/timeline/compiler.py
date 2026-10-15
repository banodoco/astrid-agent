"""One timing materializer for detached checkout inputs, refusing dependency cycles.

``commands`` selects existing checkout calls as ``(method, args, kwargs)``;
these are compatibility adapters, not a second edit vocabulary or a replay
journal. The caller supplies the actual old boundary locations and ripple
history. Compilation never infers an edit from previously compiled seconds.
"""
from __future__ import annotations

import copy
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from astrid.sdk.timeline._common import TimelineEditError


class TimingCycleError(TimelineEditError):
    """A directed timing loop, in stable dependency order."""

    code = "CYCLE"

    def __init__(self, path):
        loop = list(path[:-1])
        first = loop.index(min(loop))
        loop = loop[first:] + loop[:first]
        self.path = tuple(loop + [loop[0]])
        super().__init__(f"{self.code}: {' -> '.join(self.path)}")


def _validate_dependencies(tl):
    """Inspect declarations without reordering or evaluating the legacy timing pass.

    Start/end nodes keep references to a cut's end distinct from its start.
    The terminal cut end is the stored film-duration basis, as in MomentContext;
    it is deliberately not fed back from the picture it is used to size.
    """
    from astrid.core.timeline import moments as mo
    from astrid.sdk import timeline_intent as intent

    clips = list(tl.clips())
    groups = tl._cut_groups()
    graph = {}
    addresses = Counter(c.address for c in clips)
    cut_ids = {g['id'] for g in groups}
    nodes = {id(c.data): (c.address if addresses[c.address] == 1 and c.address not in cut_ids
                         else f'{c.address}[{c.shot_id}/{c.id}]') for c in clips}
    words = list(tl.words())
    by_word = {(c.shot_id, c.id): c for c in clips}
    bases = {g['id']: g['picture'] if g['picture'] is not None else
             min(g['clips'], key=lambda c: (c.start, c.id)) for g in groups}
    base_ids = {id(c.data) for c in bases.values()}
    aliases = {}

    def node(clip, edge):
        return f"{nodes[id(clip.data)]}.{edge}"

    def link(source, *targets):
        graph.setdefault(source, set()).update(targets)

    for i, group in enumerate(groups):
        link(f"{group['id']}.start", node(bases[group['id']], 'start'))
        if i + 1 < len(groups):
            link(f"{group['id']}.end", f"{groups[i + 1]['id']}.start")

    def references(text, clip):
        try:
            moment = mo.parse(text)
        except mo.MomentError:
            return set()  # existing evaluator owns malformed/missing diagnostics

        def visit(m):
            if m.kind in ('cut', 'cut_end', 'end', 'cut_start'):
                if m.cut and m.cut not in aliases:
                    aliases[m.cut] = tl._cut_id(m.cut)
                cid = aliases[m.cut] if m.cut else intent.cut_of(clip.data)
                if cid not in cut_ids:
                    return set()
                edge = 'end' if m.kind in ('cut_end', 'end') else 'start'
                return {f'{cid}.{edge}'}
            if m.kind == 'word':
                hits = mo.find_words(m, words)
                if m.n is not None:
                    hits = hits[m.n - 1:m.n] if m.n > 0 else []
                elif len(hits) > 1:
                    # Match the evaluator's nearest-occurrence compatibility rule.
                    at = clip.start - moment.offset_s - moment.offset_frames / tl.fps
                    hits = [min(hits, key=lambda r: abs((r[-1].end if m.edge == 'end' else r[0].start) - at))]
                return {node(by_word[w.shot_id, w.clip_id], 'start')
                        for run in hits for w in (run[-1] if m.edge == 'end' else run[0],)
                        if (w.shot_id, w.clip_id) in by_word}
            if m.kind in ('beat', 'downbeat'):
                deps = visit(m.base) if m.base else set()
                for bed in clips:
                    if bed.is_audio and (bed.track == 'music' or intent.beat_sources(bed.data)) \
                            and intent.beat_sources(bed.data, m.kind):
                        deps.update((node(bed, 'start'), node(bed, 'end')))
                return deps
            return set()

        return visit(moment)

    sequences = {}
    for clip in clips:
        tag = intent.sequence(clip.data)
        if tag and not intent.orphan(clip.data):
            sequences.setdefault(tag[0], []).append(clip)
    for steps in sequences.values():
        steps.sort(key=lambda c: intent.sequence(c.data)[1])

    for clip in clips:
        if intent.orphan(clip.data):
            continue
        start, end = node(clip, 'start'), node(clip, 'end')
        cid = intent.cut_of(clip.data)
        tag = intent.sequence(clip.data)
        steps = sequences.get(tag[0], []) if tag else []
        first = steps[0] if steps else clip
        on, until = intent.on(first.data), intent.until(first.data)
        if clip is first:
            if on:
                link(start, *references(on, clip))
            elif cid in cut_ids and id(clip.data) not in base_ids and not clip.is_audio:
                link(start, f'{cid}.start')
        else:
            link(start, node(first, 'start'))
        if until:
            link(end, *references(until, first))
        elif cid in cut_ids and not clip.is_audio and intent.for_s(first.data) is None:
            link(end, f'{cid}.end')
        else:
            link(end, start)
        if steps:
            # Steps share the first step's declared span and fit; no artificial
            # last-step -> first-step feedback through their cached durations.
            span = references(until, first) if until else ({f'{cid}.end'} if cid in cut_ids else set())
            if clip is not first:
                link(start, *span)
            fit = intent.sequence_fit(first.data) or {}
            land = fit.get('land')
            deps = set()
            if isinstance(land, str):
                deps = references(land, first)
            elif isinstance(land, Mapping):  # retained {word, text} fit form
                try:
                    word = tl._as_word(str(land.get('word') or '')) or tl.word(str(land.get('text')))
                except TimelineEditError:
                    pass  # leave the existing lookup failure to the evaluator
                else:
                    if (word.shot_id, word.clip_id) in by_word:
                        deps.add(node(by_word[word.shot_id, word.clip_id], 'start'))
            if deps:
                link(end, *deps)
                if clip is not first:
                    link(start, *deps)
        for path, expr in intent.formulas(clip.data).items():
            if path not in ('at', 'hold', 'from', 'to', 'speed') or not isinstance(expr, Mapping):
                continue  # element params do not write clip timing
            if expr.get('orphan'):
                continue  # a lost spoken identity keeps its cache, not a new edge
            target = start if path == 'at' else end
            if 'moment' in expr:
                link(target, *references(expr['moment'], clip))
                if expr.get('as', 'clip_seconds') != 'timeline_seconds':
                    link(target, start)
            elif 'word' in expr:
                word = next((w for w in words if w.id == expr['word']), None)
                if word and (word.shot_id, word.clip_id) in by_word:
                    link(target, node(by_word[word.shot_id, word.clip_id], 'start'))

    # Iterative DFS avoids imposing a recursion-depth limit on valid long chains.
    done, active, path = set(), {}, []
    for root in sorted(graph):
        if root in done:
            continue
        stack = [(root, iter(sorted(graph[root])))]
        active[root] = len(path)
        path.append(root)
        while stack:
            current, edges = stack[-1]
            target = next(edges, None)
            if target is None:
                stack.pop()
                done.add(current)
                active.pop(current)
                path.pop()
            elif target in active:
                raise TimingCycleError(path[active[target]:] + [target])
            elif target not in done:
                active[target] = len(path)
                path.append(target)
                stack.append((target, iter(sorted(graph.get(target, ())))))


# Only timing state crosses the boundary. Transaction/journal objects, clients,
# paths, save state and open edits belong to the caller, never to a compiler copy.
_STATE = (
    "bundle", "fps", "notes", "report", "_mcache", "_music", "_keep_inside",
    "_kept", "_outside", "_scope", "_music_pending", "_orphan_reason", "_orphan_cuts", "_moved_ends",
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
    from astrid.sdk.timeline._timing_evaluators import _TimingEvaluators
    from astrid.sdk.timeline.reflow import COMPILED_METHODS
    from astrid.sdk.timeline_checkout import Checkout

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
    workspace._command_depth = 1  # compiler evaluator calls stay below the outer EditCommand boundary
    from astrid.sdk.timeline import music_beds
    music_beds.prepare(workspace)
    music_beds.validate(workspace)
    music_beds.dematerialize(workspace)
    _validate_dependencies(workspace)  # outside legacy partial-state adoption
    values = []

    def result():
        state = {key: getattr(workspace, key) for key in _STATE if hasattr(workspace, key)}
        return TimingResult(workspace.bundle, tuple(values), state, origins, tuple(calls))

    try:
        for method, args, kwargs in calls:
            if method == "_apply_bed_cuts":
                args = (_records(args[0], identities), *args[1:])
            values.append(getattr(workspace, method)(*args, **kwargs))
            _validate_dependencies(workspace)
    except (TimingCycleError, music_beds.MusicBedError):
        raise  # a cyclic workspace must never be adopted, even for Checkout
    except BaseException as exc:
        _validate_dependencies(workspace)
        if inputs._owner is not None:
            raise _CheckoutCompileError(result(), exc) from exc
        raise
    music_beds.project(workspace)
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
    for key, value in result._state.items():
        if key == "_scope":
            # These are scalar identity records, not copied SDK containers.
            # Fresh remapped containers must not be matched against _origins:
            # their IDs can reuse a discarded deepcopy container's ID.
            value = _records(value, {key: id(value) for key, value in result._origins.items()})
        else:
            value = adopt(value)
        setattr(checkout, key, value)
    # _relay_steps also accepts standalone step dictionaries, outside the bundle.
    # Its compiled argument copies must be adopted even when they are not state.
    adopt(result._arguments)
    if failure is not None:
        raise failure.with_traceback(failure.__traceback__) from None
    return adopt(result.values[0])
