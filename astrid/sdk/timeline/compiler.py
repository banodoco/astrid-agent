"""One timing materializer for detached checkout inputs, refusing dependency cycles.

``commands`` selects existing checkout calls as ``(method, args, kwargs)``;
these are compatibility adapters, not a second edit vocabulary or a replay
journal. The caller supplies the actual old boundary locations and ripple
history. Compilation never infers an edit from previously compiled seconds.
"""
from __future__ import annotations

import copy
import json
from collections import Counter
from dataclasses import dataclass
from dataclasses import field as dataclass_field
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


class TimingAmbiguityError(TimelineEditError):
    """An existing reference needs an explicit occurrence choice."""

    code = "AMBIGUOUS_REFERENCE"


def _validate_dependencies(tl, *, music_end_owners=()):
    """Inspect declarations without reordering or evaluating the legacy timing pass.

    Start/end nodes keep references to a cut's end distinct from its start.
    Film end follows the final shot's pictures. A terminal picture sized by its
    own cut end retains its imported literal extent as a compatibility basis.
    """
    from astrid.core.timeline import moments as mo
    from astrid.sdk import timeline_intent as intent

    clips = list(tl.clips())
    groups = tl.cut_index.groups(tl)
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
    last_shot = tl._shot_ids()[-1] if tl._shot_ids() else None
    final_pictures = [c for c in clips if c.shot_id == last_shot and not c.is_audio
                      and (c.track == 'plate' or c.element in tl.FULL_FRAME)]
    terminal = groups[-1]['id'] if groups else None

    def node(clip, edge):
        return f"{nodes[id(clip.data)]}.{edge}"

    def link(source, *targets):
        graph.setdefault(source, set()).update(targets)

    for i, group in enumerate(groups):
        link(f"{group['id']}.start", node(bases[group['id']], 'start'))
        if i + 1 < len(groups):
            link(f"{group['id']}.end", f"{groups[i + 1]['id']}.start")
        elif final_pictures:
            link(f"{group['id']}.end", 'film.pictureEnd')
    for picture in final_pictures:
        link('film.pictureEnd', node(picture, 'end'))

    def checked_moment(text, clip, field):
        try:
            moment = mo.parse(text)
        except mo.MomentError:
            return None  # existing evaluator owns malformed/missing diagnostics
        base = moment
        while base.base is not None:
            base = base.base
        if base.kind == 'word' and base.n is None and len(mo.find_words(base, words)) > 1:
            from astrid.sdk.timeline.model import _MomentContext
            try:
                mo.resolve(base, _MomentContext(tl, clip))
            except mo.MomentError as exc:
                raise TimingAmbiguityError(f'{TimingAmbiguityError.code}: {clip.address}.{field} {text}: {exc}') from None
        return moment

    def terminal_basis(text, clip):
        if clip not in final_pictures or tl._cut_key(clip) != terminal:
            return False
        if not text:
            return True
        try:
            m = mo.parse(text)
        except mo.MomentError:
            return False
        return ((m.kind == 'end' and intent.until_film_end(clip.data) is None)
                or (m.kind == 'cut_end' and tl._cut_id(m.cut, clip) == terminal))

    def references(text, clip, *, film_end=None, compatibility=False):
        moment = checked_moment(text, clip, 'reference')
        if moment is None:
            return set()
        if compatibility:
            basis = node(clip, 'compatibilityEnd')
            link(basis, node(clip, 'start'))
            return {basis}

        def visit(m):
            if m.kind == 'end' and film_end is not None:
                return set()  # fixed timeline-end basis, independent of the clip's owning cut
            if m.kind in ('cut', 'cut_end', 'end', 'cut_start'):
                alias = (clip.shot_id, m.cut)
                if m.cut and alias not in aliases:
                    aliases[alias] = tl._cut_id(m.cut, clip)
                cid = aliases[alias] if m.cut else tl._cut_key(clip)
                if cid not in cut_ids:
                    return set()
                edge = 'end' if m.kind in ('cut_end', 'end') else 'start'
                return {f'{cid}.{edge}'}
            if m.kind in ('clip_start', 'clip_end'):
                from astrid.sdk.timeline.commands import authored_owner
                target = authored_owner(tl, m.clip)
                if id(target.data) not in nodes:
                    raise TimelineEditError(f"Clip anchor {m.clip} has no evaluated authored owner")
                return {node(target, m.kind.split('_')[1])}
            if m.kind == 'word':
                from astrid.sdk.timeline.model import _MomentContext
                hits = mo.find_words(m, _MomentContext(tl, clip).words_for(m))
                if m.n is not None:
                    hits = hits[m.n - 1:m.n] if m.n > 0 else []
                return {node(by_word[w.shot_id, w.clip_id], 'start')
                        for run in hits for w in (run[-1] if m.edge == 'end' else run[0],)
                        if (w.shot_id, w.clip_id) in by_word}
            if m.kind in ('beat', 'downbeat'):
                deps = visit(m.base) if m.base else set()
                from astrid.sdk.timeline import music_beds
                from astrid.sdk.timeline.model import _MomentContext
                ctx = _MomentContext(tl, clip, film_end=film_end)
                try:
                    base = mo.resolve(m.base, ctx) if m.base else 0.
                    beds = ctx._beat_beds(m.kind)
                    owner = ctx._beat_owner(m.kind, base, beds)
                    pieces = {id(c.data) for c in beds[owner][1]} if owner is not None else set()
                    selected = [bed for bed in clips if id(bed.data) in pieces
                                or (owner is not None and music_beds.owner_for(tl, bed) == owner)]
                except (mo.MomentError, TimelineEditError):
                    # A missing base retains the conservative graph until the
                    # existing resolver can supply its diagnostic/ORPHAN.
                    selected = clips
                for bed in selected:
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
            sequences.setdefault((clip.shot_id, tag[0]), []).append(clip)
    for steps in sequences.values():
        steps.sort(key=lambda c: intent.sequence(c.data)[1])

    for clip in clips:
        if intent.orphan(clip.data):
            continue
        # Validate every retained declaration, including later sequence members
        # and parameter formulas which do not themselves write timing edges.
        for field, text in (('on', intent.on(clip.data)), ('until', intent.until(clip.data))):
            if text:
                checked_moment(text, clip, field)
        fit = intent.sequence_fit(clip.data) or {}
        land = fit.get('land')
        if isinstance(land, str):
            checked_moment(land, clip, 'sequence_fit.land')
        elif isinstance(land, Mapping) and 'source' not in land:
            word = next((w for w in words if w.source is None and w.id == land.get('word')), None)
            if word is None and land.get('text'):
                checked_moment(json.dumps(str(land['text'])), clip, 'sequence_fit.land')
        for field, expr in intent.formulas(clip.data).items():
            if isinstance(expr, Mapping) and not expr.get('orphan') and expr.get('moment'):
                checked_moment(expr['moment'], clip, field)
            elif isinstance(expr, Mapping) and not expr.get('orphan') and 'word' in expr and 'source' not in expr:
                from astrid.sdk.timeline._common import _norm
                word = next((w for w in words if w.source is None and w.id == expr['word']), None)
                if (word is None or expr.get('text') and _norm(word.text) != _norm(expr['text'])) and expr.get('text'):
                    checked_moment(json.dumps(str(expr['text'])), clip, field)
        start, end = node(clip, 'start'), node(clip, 'end')
        cid = tl._cut_key(clip)
        tag = intent.sequence(clip.data)
        steps = sequences.get((clip.shot_id, tag[0]), []) if tag else []
        first = steps[0] if steps else clip
        on, until = intent.on(first.data), intent.until(first.data)
        if clip is first:
            if on:
                link(start, *references(on, clip))
            elif cid in cut_ids and not intent.fixed_start(clip.data) and id(clip.data) not in base_ids and not clip.is_audio:
                link(start, f'{cid}.start')
        else:
            link(start, node(first, 'start'))
        if until:
            link(end, *references(until, first, film_end=intent.until_film_end(first.data),
                                  compatibility=terminal_basis(until, first)))
        elif cid in cut_ids and not clip.is_audio and intent.for_s(first.data) is None:
            if terminal_basis(None, first):
                link(end, *references('end', first, compatibility=True))
            else:
                link(end, f'{cid}.end')
        else:
            link(end, start)
        if steps:
            # Steps share the first step's declared span and fit; no artificial
            # last-step -> first-step feedback through their cached durations.
            span = references(until or 'end', first, film_end=intent.until_film_end(first.data),
                              compatibility=terminal_basis(until, first)) if until or cid in cut_ids else set()
            if clip is not first:
                link(start, *span)
            fit = intent.sequence_fit(first.data) or {}
            land = fit.get('land')
            deps = set()
            if isinstance(land, str):
                deps = references(land, first, compatibility=terminal_basis(land, first))
            elif isinstance(land, Mapping):  # retained {word, text} fit form
                try:
                    if "source" in land:
                        from astrid.sdk.timeline.transcript_binding import resolve_source_word
                        word = resolve_source_word(land, words)
                    else:
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
        if music_end_owners and clip.is_audio:
            from astrid.sdk.timeline import music_beds
            if music_beds.owner_for(tl, clip) in music_end_owners:
                link(end, 'film.pictureEnd')
        loop = intent.loop(clip.data)
        if isinstance(loop, Mapping):
            phase = loop.get('phase')
            if isinstance(phase, Mapping) and isinstance(phase.get('ref'), str):
                link(f'{nodes[id(clip.data)]}.loop', *references(phase['ref'], clip))
            period = loop.get('period')
            if isinstance(period, Mapping) and 'bed' in period:
                for bed in clips:
                    if bed.id == period['bed']:
                        link(f'{nodes[id(clip.data)]}.loop', node(bed, 'start'), node(bed, 'end'))
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
                from astrid.sdk.timeline.transcript_binding import resolve_source_word
                if "source" in expr:
                    try:
                        word = resolve_source_word(expr, words)
                    except TimelineEditError:
                        word = None  # evaluator preserves its accepted orphan cache
                else:
                    word = next((w for w in words if w.source is None and w.id == expr['word']), None)
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
    "_narration_takes", "_transcript_dependencies",
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
    state: Mapping[str, Any] = dataclass_field(default_factory=dict, repr=False)
    _owner: Any = dataclass_field(default=None, repr=False, compare=False)


@dataclass(frozen=True)
class TimingResult:
    bundle: dict[str, Any]
    values: tuple[Any, ...]
    _state: dict[str, Any] = dataclass_field(repr=False)
    _origins: Mapping[int, Any] = dataclass_field(repr=False, compare=False)
    _arguments: tuple = dataclass_field(repr=False, compare=False)


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


def _detached_checkout(checkout):
    """Capture a semantic prefix with the compiler's actual timing inputs.

    Ripple locations, music policy and identity exclusions are not recoverable
    from compiled bundle seconds. They belong to this detached probe only.
    """
    probe = object.__new__(type(checkout))
    memo = {id(checkout): probe}
    state = copy.deepcopy({key: getattr(checkout, key) for key in _STATE
                           if key != "_mcache" and hasattr(checkout, key)}, memo)
    type(checkout).__init__(probe, state.pop("bundle"))
    probe.__dict__.update(state)
    probe._scope = _records(probe._scope, {key: id(value) for key, value in memo.items()})
    return probe


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
    workspace_type = type("_TimingWorkspace", (owner_type,), {"_timing_workspace": True, **{
        name: getattr(_TimingEvaluators, name) for name in COMPILED_METHODS
        if getattr(getattr(owner_type, name), "_timing_entrypoint", False)
    }})
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
    workspace._word_list()  # reject missing/altered frozen transcript inputs before materialization
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
            # A numeric ripple basis is an input on later compiles. Only a
            # film-end command which actually changes that basis creates the
            # bed -> picture-end dependency in this compile.
            old_bases = {owner: copy.deepcopy(entry['ripple_basis'])
                         for owner, entry in music_beds.sources(workspace).items()} if method == 'film_end' else {}
            if method == "_apply_bed_cuts":
                args = (_records(args[0], identities), *args[1:])
            values.append(getattr(workspace, method)(*args, **kwargs))
            changed = {owner for owner, entry in music_beds.sources(workspace).items()
                       if entry['ripple_basis'] != old_bases.get(owner, [])} if method == 'film_end' else set()
            _validate_dependencies(workspace, music_end_owners=changed)
    except (TimingCycleError, TimingAmbiguityError, music_beds.MusicBedError):
        raise  # a cyclic workspace must never be adopted, even for Checkout
    except BaseException as exc:
        changed = {owner for owner, entry in music_beds.sources(workspace).items()
                   if entry['ripple_basis'] != old_bases.get(owner, [])} if method == 'film_end' else set()
        _validate_dependencies(workspace, music_end_owners=changed)
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
