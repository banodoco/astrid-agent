"""Parent word-anchor recipe and pure adapter to the captured-input timing kernel."""
from __future__ import annotations

import copy
import json
import re
from dataclasses import replace
from decimal import Decimal
from fractions import Fraction
from types import SimpleNamespace

from astrid.core.timeline import moments
from astrid.sdk.timeline._command_state import CommandError, equal, present
from astrid.sdk.timeline._common import AUDIO_KINDS
from astrid.sdk.timeline._recording import active
from astrid.sdk.timeline.compiler import (
    TimingCompileError,
    TimingInputs,
    WordBinding,
    compile_timing,
)
from astrid.sdk.timeline.units import _time_fraction, source_to_timeline_time

OP = "clip.anchor"
PATH = ["app", "astrid"]


def _namespace(row):
    app = row.get('app')
    value = app.get('astrid') if isinstance(app, dict) else None
    return value if isinstance(value, dict) else {}


def reference_schema():
    from .commands import _SCHEMA

    return _SCHEMA['$defs']['WordAnchor']


def guard_basis(ctx, command):
    if command['op'] in ('clip.move', 'placement.move', 'clip.trim', 'clip.split'):
        row = ctx.state.entity(command['select'])
        if _namespace(row).get('word_anchors'):
            raise CommandError('clear word anchors before changing the timing basis',
                               kind='invalid_anchor', ref=command['select'])


def authored(bundle):
    """Read namespaced values, without accepted records or a history fold."""
    return {(f"parent/clip:{clip['id']}", field): {
        "field": field, "declaration": _namespace(clip).get(field),
        "choice": copy.deepcopy(choice)}
        for clip in bundle["parent"]["clips"]
        for field, choice in _namespace(clip).get("word_anchors", {}).items()}


def binding(bundle, target, field, choice, sources):
    try:
        json.dumps(choice, allow_nan=False)
    except (ValueError, TypeError) as exc:
        raise TimingCompileError("invalid authored word reference: expected finite JSON") from exc
    from .commands import _VALIDATOR

    errors = list(_VALIDATOR.evolve(schema=reference_schema()).iter_errors(choice))
    if errors:
        raise TimingCompileError("invalid authored word reference: " + errors[0].message)
    owner, clip = choice["source"].split("/clip:")
    occurrence = owner.removeprefix("occurrence:")
    placements = [row for row in bundle["placements"] if row["occurrence_id"] == occurrence]
    if len(placements) != 1:
        raise TimingCompileError("bound source occurrence/clip is missing or ambiguous")
    placement = placements[0]
    internal = bundle['shots'][placement['shot_id']]['internal_timeline']
    clips = [row for row in internal.get('clips') or [] if row['id'] == clip]
    if len(clips) != 1:
        raise TimingCompileError('bound source occurrence/clip is missing or ambiguous')
    source = clips[0]
    tracks = [row for row in internal.get('tracks') or [] if row['id'] == source.get('track')]
    if (not active(placement) or not active(source) or len(tracks) != 1 or not active(tracks[0])):
        raise TimingCompileError('bound source is outside the active source selection')
    if sum(row['shot_id'] == placement['shot_id'] for row in bundle['placements']) != 1:
        raise TimingCompileError('shared word source requires canonical occurrence ownership')
    asset = internal.get('registry', {}).get('assets', {}).get(source.get('asset'), {})
    if not isinstance(asset, dict):
        raise TimingCompileError('transcript identity does not match the bound media source')
    if (any(value.get(key) is not None for value in (internal, asset, source)
            for key in ('source_window', 'time_map', 'timeMap'))
            or any(source.get(key) is not None for key in ('asset_id', 'media_id', 'object_id'))
            or any(isinstance(placement.get(key), bool) for key in ('source_offset', 'speed'))):
        raise TimingCompileError('word source requires an explicit supported source map')
    from astrid.sdk import timeline_intent

    declarations = timeline_intent._app(source)
    if any(declarations.get(key) for key in ('on', 'until', 'until_film_end', 'cut', 'sequence',
                                           'sequence_fit', 'formulas', 'orphan', 'gap_after_s')):
        raise TimingCompileError('word source requires literal timing without external dependencies')
    pin = choice["document"]
    captures = [row for row in sources if row["pin"] == pin]
    if len(captures) != 1:
        raise TimingCompileError("exact transcript pin is missing or revoked or ambiguous")
    words = {word["id"]: word for segment in captures[0]["document"]["content"]["segments"]
             for word in segment["words"]}
    if any(key not in words for key in choice["word_ids"]):
        raise TimingCompileError("bound stable word is missing; text rebinding is forbidden")
    try:
        geometry = placement.get('placement') or {}
        start = _time_fraction(geometry.get('start_ms', placement.get('at_ms', 0)), 'occurrence start') / 1000
        end = start + _time_fraction(placement['duration_ms'], 'occurrence duration') / 1000
        origin = start + _time_fraction(source.get('at', 0), 'clip start')
        for key in choice['word_ids']:
            onset, finish = (source_to_timeline_time(words[key][edge],
                source_start=source.get('from') or 0, timeline_start=origin,
                speed=1 if source.get('speed') is None else source['speed'])
                for edge in ('start', 'end'))
            if onset < start or onset >= end or finish > end:
                raise TimingCompileError('bound word is outside the half-open occurrence interval')
    except TimingCompileError:
        raise
    except (KeyError, ValueError, TypeError, ArithmeticError) as exc:
        raise TimingCompileError('word source requires a resolvable occurrence interval') from exc
    # A private kernel spelling from exact IDs, never from the echo. The kernel
    # validates only these IDs; it cannot select a different transcript word.
    text = " ".join(words[key]["text"] for key in choice["word_ids"])
    moment = moments.Moment(kind="word", text=text, edge=choice.get("edge", "start"),
        offset_s=choice.get("offset_s", 0), offset_frames=choice.get("offset_frames", 0))
    declaration = moments.format_moment(replace(moment, offset_s=0))
    if moment.offset_s:
        declaration += f" {Decimal(str(moment.offset_s)):+f}s"
        if Fraction(str(moments.parse(declaration).offset_s)) != Fraction(str(moment.offset_s)):
            raise TimingCompileError("word reference second offset exceeds main Moment precision")
    return WordBinding(("parent", "", target.removeprefix("parent/clip:")), field,
        declaration, (occurrence, placements[0]["shot_id"], clip),
        pin["document_id"], pin["version"], pin["content_digest"], choice["word_ids"])


def legacy_binding(bundle, target, field, declaration, sources):
    """Only an unambiguous legacy text lookup is safe without a stored choice."""
    moment = moments.parse(declaration)
    hits = []
    for capture in sources:
        if capture['document']['kind'] != 'media.transcript':
            continue
        content = capture["document"]["content"]
        for row in bundle["placements"]:
            internal = bundle["shots"][row["shot_id"]]["internal_timeline"]
            for clip in internal["clips"]:
                asset = internal.get("registry", {}).get("assets", {}).get(clip.get("asset"), {})
                if asset.get("media_id") != content["media_id"]:
                    continue
                origin = (
                    _time_fraction(row["placement"]["start_ms"], "occurrence start") / 1000
                    + Fraction(str(clip["at"]))
                )
                words = []
                for segment in content["segments"]:
                    for index, word in enumerate(segment["words"]):
                        if not clip["from"] <= word["start"] < word["end"] <= clip["to"]:
                            continue
                        times = {key: source_to_timeline_time(Fraction(str(word[key])),
                            source_start=Fraction(str(clip["from"])), timeline_start=origin,
                            speed=clip["speed"]) for key in ("start", "end")}
                        words.append(SimpleNamespace(**dict(word, **times), segment=segment["id"], index=index))
                for phrase in moments.find_words(replace(moment, n=None), words):
                    pin = capture["pin"]
                    hits.append(WordBinding(("parent", "", target.removeprefix("parent/clip:")), field,
                        declaration, (row["occurrence_id"], row["shot_id"], clip["id"]),
                        pin["document_id"], pin["version"], pin["content_digest"],
                        tuple(word.id for word in phrase)))
    if len(hits) > 1:
        raise TimingCompileError("ambiguous anchor; choose occurrence")
    if not hits:
        raise TimingCompileError("legacy word anchor is missing")
    return replace(hits[0], declaration=moments.format_moment(replace(moment, n=None)))


def _legacy_beat(declaration):
    if not isinstance(declaration, str) or not declaration.strip():
        return False
    try:
        return moments.parse(declaration).kind in ("beat", "downbeat")
    except moments.MomentError:
        return False


def _has_legacy_beat(bundle):
    return any(_legacy_beat(_namespace(clip).get(field))
               for clip in bundle["parent"].get("clips") or []
               for field in ("on", "until"))


def _legacy_beat_source(internal, clip):
    track = str(clip.get("track") or "")
    return track in AUDIO_KINDS or any(
        row.get("id") == track and row.get("kind") in AUDIO_KINDS
        for row in internal.get("tracks") or []
        if isinstance(row, dict)
    )


def _guard_legacy_beat_inputs(ctx):
    """Record Main's existing carrier arrays and owner order as replay guards."""
    bundle = ctx.state.bundle
    for clip in bundle["parent"].get("clips", []):
        app = _namespace(clip)
        if any(_legacy_beat(app.get(field)) for field in ("on", "until")):
            ref = "parent/clip:" + clip["id"]
            for field in ("on", "until"):
                if _legacy_beat(app.get(field)):
                    ctx.read(ref, ["app", "astrid", field])
    for row in bundle.get("placements", []):
        owner = "occurrence:" + row["occurrence_id"]
        ctx.select("collection.clips", owner)
        ctx.read(owner, ["occurrence_id"])
        for path in (
            ["placement", "start_ms"], ["at_ms"], ["duration_ms"],
            ["source_offset"], ["speed"], ["time_map"], ["timeMap"],
        ):
            ctx.read(owner, path)
        internal = bundle["shots"][row["shot_id"]]["internal_timeline"]
        for clip in internal.get("clips", []):
            if "beats" not in _namespace(clip) or not _legacy_beat_source(internal, clip):
                continue
            ref = owner + "/clip:" + clip["id"]
            ctx.read(ref, ["id"])
            ctx.read(ref, ["app", "astrid", "beats"])
            for field in ("track", "at", "from", "to", "hold", "duration", "speed", "from_ms", "to_ms"):
                ctx.read(ref, [field])
            track = str(clip.get("track") or "")
            if track and any(item.get("id") == track for item in internal.get("tracks") or []):
                ctx.read(owner + "/track:" + track, ["kind"])


def compile_authored(bundle, sources):
    """Extract first, then compile. No history, IO, registry or authoring writes."""
    if any(_namespace(clip).get("word_anchors")
           for shot in bundle["shots"].values() for clip in shot["internal_timeline"]["clips"]):
        raise TimingCompileError("word anchors require parent ownership")
    projection, bindings = copy.deepcopy(bundle), []
    has_beats = _has_legacy_beat(bundle)
    targets = set()
    for clip in projection["parent"]["clips"]:
        app = _namespace(clip)
        refs = app.get("word_anchors", {})
        if not isinstance(refs, dict) or set(refs) - {"on", "until"}:
            raise TimingCompileError("invalid authored word anchor fields")
        for field in ("on", "until"):
            target = "parent/clip:" + clip["id"]
            if field in refs:
                selected = binding(bundle, target, field, refs[field], sources)
            elif app.get(field) and moments.parse(app[field]).kind == "word":
                selected = legacy_binding(bundle, target, field, app[field], sources)
            else:
                continue
            bindings.append(selected)
            app[field] = selected.declaration
            targets.add(clip["id"])
        if has_beats and any(_legacy_beat(app.get(field)) for field in ("on", "until")):
            targets.add(clip["id"])
    # Only the new path and its exact source clips enter this bounded kernel.
    # Existing films may contain unrelated timing features not yet ported here.
    targets.update(pin.target[2] for pin in bindings)
    owners = {pin.source for pin in bindings}
    if has_beats:
        # Main's legacy beat table is embedded in the captured occurrence
        # source. Keep the complete captured occurrence/clip order while the
        # pure kernel evaluates it; no marker source or synthetic ordinal is
        # introduced.
        projection['parent']['clips'] = [row for row in projection['parent']['clips']
                                         if row['id'] in targets]
        projection['placements'] = copy.deepcopy(bundle['placements'])
    else:
        projection['parent']['clips'] = [row for row in projection['parent']['clips']
                                         if row['id'] in targets]
        projection['placements'] = [row for row in projection['placements']
                                    if any(owner[0] == row['occurrence_id'] for owner in owners)]
    shot_ids = {row['shot_id'] for row in projection['placements']}
    projection['shots'] = {key: row for key, row in projection['shots'].items() if key in shot_ids}
    for sid, shot in projection['shots'].items():
        if not has_beats:
            shot['internal_timeline']['clips'] = [row for row in shot['internal_timeline']['clips']
                if any(owner[1:] == (sid, row['id']) for owner in owners)]
    transcript_sources = [row for row in sources if row['document']['kind'] == 'media.transcript']
    config = projection['parent'].get('config')
    if isinstance(config, dict) and isinstance(config.get('clips'), list):
        config['clips'] = projection['parent']['clips']
    result = compile_timing(TimingInputs(projection, sources=transcript_sources, bindings=bindings))
    return replace(result, bundle=copy.deepcopy(bundle))


def declare(ctx, command):
    target, args = command["select"], command["args"]
    if not isinstance(target, str) or re.fullmatch(r'parent/clip:(?!.*::)[^/]+', target) is None:
        raise CommandError('word anchors require parent/raw clip ownership', kind='unsupported_ownership')
    if ctx.select('move.target', 'parent', [target]) != [target]:
        raise CommandError('anchor target disappeared', kind='identity_conflict', ref=target)
    ctx.read(target, ["id"])
    asset = ctx.read(target, ['asset'])
    if asset is not None:
        ctx.read('parent/registry', ['assets', asset])
    if args["declaration"] is None:
        if args["choice"] is not None:
            raise CommandError("clear has no word choice")
        # Main still reads the legacy flat echo when the namespaced echo is
        # absent. A clear must not reveal that older declaration again.
        ctx.read(target, ['app', args['field']])
        if ctx.state.get(target, ['app', args['field']])['state'] == 'present':
            ctx.clear(target, ['app', args['field']])
        ctx.clear(target, PATH + [args["field"]])
        ctx.clear(target, PATH + ["word_anchors", args["field"]])
        return
    if args["choice"] is None:
        raise CommandError("word declaration requires an exact choice")
    choice = args["choice"]
    source = choice["source"]
    owner = source.split("/clip:")[0]
    if ctx.select('placement.owners', owner) != [owner]:
        raise CommandError('shared word source is unsupported', kind='unsupported_ownership', ref=owner)
    ctx.read(owner, ["occurrence_id"])
    ctx.read(owner + "/shot", ["shot_id"])
    if ctx.select('move.target', owner, [source]) != [source]:
        raise CommandError('bound source occurrence/clip is missing', kind='invalid_anchor', ref=source)
    ctx.read(source, ["id"])
    track = ctx.read(source, ['track'])
    for ref in (owner, source, owner + '/track:' + str(track)):
        for key in ('enabled', 'active', 'disabled', 'hidden', 'deleted'):
            ctx.read(ref, [key])
    asset = ctx.read(source, ["asset"])
    ctx.read(owner + "/registry", ["assets", asset])
    try:
        binding(ctx.state.bundle, target, args['field'], choice,
                ctx.state.bundle.get('source_mapping', {}).get('pinned_documents', []))
    except TimingCompileError as exc:
        raise CommandError(str(exc), kind='invalid_anchor', ref=target) from exc
    ctx.object(target, ["app"])
    ctx.object(target, PATH)
    ctx.object(target, PATH + ["word_anchors"])
    ctx.write(target, PATH + [args["field"]], args["declaration"])
    ctx.write(target, PATH + ["word_anchors", args["field"]], choice)
    ctx.dependency("anchor.transcript", "transcript", choice["document"])
    ctx.bind("anchor.source", source, dependencies=["anchor.transcript"])
    ctx.record["bindings"][-1]["policy"] = "follow"


def _derived(ctx, ref, path, after):
    before = ctx.state.get(ref, path)
    if not equal(before, after):
        ctx.record['derived_writes'].append({'ref': ref, 'path': path, 'before': before,
            'after': after, 'cause': ctx.record['ordinal']})
        ctx.state.set(ref, path, after)


def compile_recorded(ctx, *, inverse_records=()):
    bundle = ctx.state.bundle
    clips = ctx.state.collection('parent', 'clip') or []
    has_beats = _has_legacy_beat(bundle)
    if any('word_anchors' in _namespace(row) for shot in bundle['shots'].values()
           for row in shot['internal_timeline'].get('clips', [])):
        raise CommandError('word anchors require parent ownership', kind='unsupported_ownership')
    if ctx.record['command']['op'] == 'timeline.inverse':
        # When the last reference disappears there is no live declaration for
        # the kernel to evaluate. Restore its prior literal cache from ordinary
        # authenticated inverse provenance, never use history to select a word.
        for record in reversed(inverse_records):
            anchors = {write['ref'] for write in record['writes']
                       if write['path'][:3] == PATH + ['word_anchors']}
            for write in reversed(record['derived_writes']):
                if write['ref'] not in anchors or write['path'] not in (['at'], ['hold']):
                    continue
                row = ctx.state.entity(write['ref'], optional=True)
                if row is not None and not _namespace(row).get('word_anchors'):
                    _derived(ctx, write['ref'], write['path'], write['before'])
    if not any('word_anchors' in _namespace(row) for row in clips) and not has_beats:
        return
    if has_beats:
        _guard_legacy_beat_inputs(ctx)
    projection = copy.deepcopy(bundle)
    projection['parent']['clips'] = [copy.deepcopy(row) for row in clips
                                    if ('word_anchors' in _namespace(row)
                                        or (has_beats and any(_legacy_beat(_namespace(row).get(field))
                                                              for field in ('on', 'until'))))]
    config = projection['parent'].get('config')
    if isinstance(config, dict) and isinstance(config.get('clips'), list):
        config['clips'] = projection['parent']['clips']
    try:
        result = compile_authored(projection, bundle.get('source_mapping', {}).get('pinned_documents', []))
    except TimingCompileError as exc:
        raise CommandError(str(exc), kind='invalid_anchor') from exc
    for row in clips:
        if not (_namespace(row).get('word_anchors')
                or (has_beats and any(_legacy_beat(_namespace(row).get(field))
                                      for field in ('on', 'until')))):
            continue
        ref = 'parent/clip:' + row['id']
        timing = result.timings[('parent', '', row['id'])]
        speed = 1 if row.get('speed') is None else row['speed']
        for path, value in ((['at'], float(timing['start'])),
                            (['hold'], float(timing['duration']) * speed)):
            _derived(ctx, ref, path, present(value))


def capture_anchor(bundle, select, field, declaration, choice):
    """Capture a picker choice using independently verified document inputs."""
    from .commands import execute

    return execute(bundle, [{'schema_version': 1, 'op': OP, 'version': '1', 'select': select,
        'args': {'field': field, 'declaration': declaration, 'choice': copy.deepcopy(choice)}}]).commands[0]
