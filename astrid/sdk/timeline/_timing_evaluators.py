"""Main Moment/cut evaluators over captured inputs; no mutable Checkout execution."""
from __future__ import annotations

from dataclasses import replace
from fractions import Fraction
from types import MappingProxyType, SimpleNamespace

from astrid.core.timeline import moments as mo
from astrid.core.timeline.authoring_bundle import _digest
from astrid.sdk import timeline_intent as intent
from astrid.sdk.media_transcript import SCHEMA, transcript_document_id
from astrid.sdk.timeline._common import TimelineEditError
from astrid.sdk.timeline.compiler import TimingCompileError, TimingCycleError
from astrid.sdk.timeline.units import (
    _playback_rate,
    _time_fraction,
    quantize_interval,
    quantize_time,
    source_to_timeline_time,
)
from astrid.sdk.timeline_checkout import Checkout


def _timing_number(value):
    return _time_fraction(value, "timing value")


def _fps(bundle):
    # The detached compiler keeps exact rational spellings for its direct
    # callers. The registered provider additionally carries the runtime
    # document marker; there, the open canvas path follows upstream's
    # positive-scalar-or-30 fallback, including True -> 1.0.
    source_mapping = bundle.get("source_mapping")
    production = (isinstance(source_mapping, dict)
                  and "_runtime_document" in source_mapping
                  and source_mapping.get("_timing_open_canvas", False))
    for key in ("parent", "base_parent_payload"):
        config = (bundle.get(key) or {}).get("config") or {}
        overrides = config.get("theme_overrides")
        visual = overrides.get("visual") if isinstance(overrides, dict) else None
        canvas = visual.get("canvas") if isinstance(visual, dict) else None
        if not isinstance(canvas, dict) or "fps" not in canvas:
            continue
        value = canvas["fps"]
        if production:
            if isinstance(value, (int, float)) and float(value) > 0:
                if isinstance(value, bool):
                    return Fraction(int(value), 1)
                return _playback_rate(value)
            return Fraction(30)
        if isinstance(value, dict) and set(value) == {"numerator", "denominator"}:
            parts = [value["numerator"], value["denominator"]]
        elif isinstance(value, list) and len(value) == 2:
            parts = value
        else:
            return _playback_rate(value)
        if any(isinstance(x, bool) or not isinstance(x, int) or x <= 0 for x in parts):
            raise TimingCompileError("fps rational requires positive integer numerator/denominator")
        return Fraction(*parts)
    return Fraction(30)


def _maps(value, label):
    for key in ("source_window", "time_map", "timeMap", "from_ms", "to_ms", "children", "clips"):
        if value.get(key) is not None:
            raise TimingCompileError(f"{label}: unsupported explicit map {key}")


def _value(value, key, default=0):
    return default if value.get(key) is None else value[key]


def _cut_identity(bundle, shot_id):
    # The final-candidate adapter carries the exact physical pin into this view.
    # Equal bytes on different revisions never establish shared ownership.
    source = bundle.get("source_mapping", {}).get("shots", {}).get(shot_id, {})
    revision = source.get("internal_timeline_revision_id")
    digest = source.get("internal_timeline_content_digest")
    if revision and digest == _digest(bundle["shots"][shot_id]["internal_timeline"]):
        return ("internal", source.get("internal_timeline_id"), revision, digest)
    return ("shot", shot_id)


class _ReadLookup(Checkout):
    def __init__(self, bundle, fps):
        self.bundle, self.fps = bundle, fps

    def _internal(self, shot_id):
        return self.bundle["shots"][shot_id]["internal_timeline"]


def _duration(clip):
    # clip_duration's hold > source window > duration precedence, in exact source seconds.
    speed = _playback_rate(_value(clip, "speed", 1))
    if _timing_number(_value(clip, "from")) < 0:
        raise TimingCompileError("invalid negative source in-point")
    if clip.get("to") is not None:
        if _timing_number(clip["to"]) <= _timing_number(_value(clip, "from")):
            raise TimingCompileError("invalid source interval")
    if clip.get("hold") is not None:
        length = _timing_number(clip["hold"])
    elif clip.get("to") is not None:
        length = _timing_number(clip["to"]) - _timing_number(_value(clip, "from"))
    elif clip.get("duration") is not None:
        length = _timing_number(clip["duration"])
    else:
        return None
    if length <= 0:
        raise TimingCompileError("invalid literal interval")
    return length / speed


def _walk(graph, functions):
    """R4 compiler's iterative directed walk, evaluating only after all dependencies."""
    done, active, path, values = set(), {}, [], {}
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
                values[current] = functions[current](values)
                stack.pop()
                done.add(current)
                active.pop(current)
                path.pop()
            elif target in active:
                raise TimingCycleError(path[active[target]:] + [target])
            elif target not in done:
                active[target] = len(path)
                path.append(target)
                stack.append((target, iter(sorted(graph[target]))))
    return values


class _Context:
    def __init__(self, fps, own=None, cuts=None, words=(), beats=None, in_point=None):
        self.fps, self.own, self.cuts = fps, own, cuts or {}
        self.bound_words, self.bound_beats, self.in_point = words, beats or {}, in_point

    def own_cut(self):
        return self.own

    def cut_start(self, cut):
        return self.cuts[cut][0]

    def cut_end(self, cut):
        return self.cuts[cut][1]

    def words(self):
        return self.bound_words

    def beats(self, kind):
        return self.bound_beats.get(kind, ())

    def line_in_point(self, line):
        return self.in_point


def _beat_values(clip, kind):
    """Return Main's numeric beat carrier, preserving its authored order."""
    value = intent._app(clip).get("beats")
    if value is None:
        return None
    if isinstance(value, list):
        if kind != "beat":
            return []
        key = "beats"
        rows = value
    elif isinstance(value, dict):
        key = "downbeats" if kind == "downbeat" else "beats"
        if key not in value:
            return []
        rows = value[key]
    else:
        raise TimingCompileError("malformed app.astrid.beats carrier")
    if not isinstance(rows, list):
        raise TimingCompileError(f"malformed app.astrid.beats.{key} carrier")
    result = []
    for index, row in enumerate(rows):
        number = row[0] if isinstance(row, (list, tuple)) and row else row
        try:
            result.append(_timing_number(number))
        except (TypeError, ValueError, ArithmeticError, TimelineEditError) as exc:
            raise TimingCompileError(f"malformed app.astrid.beats.{key}[{index}]") from exc
    if isinstance(value, list):
        source_start = _timing_number(_value(clip, "from"))
        speed = _playback_rate(_value(clip, "speed", 1))
        return [source_start + point * speed for point in result]
    return result


def _resolved_beats(kind, clips, values, node, is_source):
    points = []
    for ordinal, (owner, clip) in enumerate(clips.items()):
        if not is_source(owner, clip):
            continue
        table = _beat_values(clip, kind)
        if table is None or not table:
            continue
        start = values[node(owner, "start")]
        end = values[node(owner, "end")]
        source_start = _timing_number(_value(clip, "from"))
        speed = _playback_rate(_value(clip, "speed", 1))
        for index, point in enumerate(table):
            timeline = source_to_timeline_time(point, source_start=source_start,
                                                timeline_start=start, speed=speed)
            if start <= timeline < end:
                points.append((timeline, ordinal, index))
    # Main's Context.beats sorts timeline positions.  Stable owner/table
    # order keeps equal times deterministic without inventing marker IDs.
    return {kind: tuple(point[0] for point in sorted(points))}


def _legacy_words(documents, clips, internals, values, node, is_source):
    """Build Main's word pool from the exact captured transcript/source rows."""
    words = []
    for capture in documents.values():
        document = capture["document"]
        content = document.get("content") or {}
        media_id = content.get("media_id")
        for owner, clip in clips.items():
            if not is_source(owner, clip):
                continue
            internal = internals[owner]
            asset = (internal.get("registry") or {}).get("assets", {}).get(clip.get("asset"))
            if not isinstance(asset, dict) or asset.get("media_id") != media_id:
                continue
            source_start = _timing_number(_value(clip, "from"))
            speed = _playback_rate(_value(clip, "speed", 1))
            source_end = source_start + _duration(clip) * speed
            timeline_start = values[node(owner, "start")]
            timeline_end = values[node(owner, "end")]
            for segment in content.get("segments") or []:
                for index, word in enumerate(segment.get("words") or []):
                    start, end = (_timing_number(word[key]) for key in ("start", "end"))
                    if not source_start <= start < end <= source_end:
                        continue
                    start, end = (source_to_timeline_time(
                        value, source_start=source_start, timeline_start=timeline_start,
                        speed=_value(clip, "speed", 1)) for value in (start, end))
                    if start < timeline_start or start >= timeline_end or end > timeline_end:
                        continue
                    words.append(SimpleNamespace(text=word["text"], segment=segment["id"],
                                                 index=index, start=start, end=end,
                                                 owner=owner, id=word.get("id", "")))
    return sorted(words, key=lambda word: (word.start, word.owner, word.id))


def _legacy_word_owners(documents, clips, internals, is_source):
    media_ids = {capture["document"].get("content", {}).get("media_id")
                 for capture in documents.values()}
    owners = []
    for owner, clip in clips.items():
        if not is_source(owner, clip):
            continue
        assets = (internals[owner].get("registry") or {}).get("assets", {})
        asset = assets.get(clip.get("asset")) if isinstance(assets, dict) else None
        if isinstance(asset, dict) and asset.get("media_id") in media_ids:
            owners.append(owner)
    return owners


def evaluate(bundle, sources, bindings, policy, legacy_declarations=()):
    legacy = {
        (row[0], row[1]): row[2]
        for row in legacy_declarations
        if isinstance(row, (tuple, list)) and len(row) == 3
    }
    fps = _fps(bundle)
    clips, offsets, internals = {}, {}, {}
    placement_ends, seen_shots, seen_occurrences = [], set(), set()
    occurrence_intervals = {}
    for row in bundle.get("placements", []):
        occurrence, sid = row["occurrence_id"], row["shot_id"]
        if sid in seen_shots:
            raise TimingCompileError("shared shot requires canonical per-occurrence authoring materialization")
        if occurrence in seen_occurrences:
            raise TimingCompileError("duplicate occurrence identity")
        seen_occurrences.add(occurrence)
        seen_shots.add(sid)
        placement = row.get("placement") or {}
        for value in (row, placement):
            _maps(value, occurrence)
            if value.get("speed") not in (None, 1) or value.get("source_offset") not in (None, 0):
                raise TimingCompileError("placement source map requires the canonical authoring adapter")
        start_ms = placement.get("start_ms", row.get("at_ms", 0))
        if row.get("at_ms") is not None and _timing_number(row["at_ms"]) != _timing_number(start_ms):
            raise TimingCompileError("contradictory placement starts")
        offset = _timing_number(start_ms) / 1000
        length = _timing_number(row["duration_ms"]) / 1000
        if offset < 0 or length <= 0:
            raise TimingCompileError("invalid occurrence interval")
        placement_ends.append(offset + length)
        occurrence_intervals[occurrence] = (offset, offset + length)
        internal = bundle["shots"][sid]["internal_timeline"]
        for clip in internal.get("clips", []):
            owner = (occurrence, sid, clip["id"])
            if owner in clips:
                raise TimingCompileError(f"duplicate clip owner {owner}")
            clips[owner], offsets[owner], internals[owner] = clip, offset, internal
    parent = bundle.get("parent") or {}
    for clip in parent.get("clips", []):
        owner = ("parent", "", clip["id"])
        if owner in clips:
            raise TimingCompileError(f"duplicate clip owner {owner}")
        clips[owner], offsets[owner], internals[owner] = clip, Fraction(0), parent

    # Reuse main's group membership, picture choice, ordering and cut aliases.
    lookup = _ReadLookup(bundle, fps)
    groups = lookup._cut_groups()
    by_clip = {(owner[1], owner[2]): owner for owner in clips if owner[1]}
    bases, local_groups, local_bases = {}, {}, {}
    for group in groups:
        if len({_cut_identity(bundle, c.shot_id) for c in group["clips"]}) != 1:
            raise TimingCompileError("ambiguous cut identity across occurrences")
        base = group["picture"] or min(group["clips"], key=lambda c: (c.start, c.id))
        bases[group["id"]] = by_clip[base.shot_id, base.id]
        if len({c.shot_id for c in group["clips"]}) > 1:
            for sid in {c.shot_id for c in group["clips"]}:
                members = [c for c in group["clips"] if c.shot_id == sid]
                pictures = [c for c in members if c.track == "plate" and not c.is_audio]
                local = min(pictures or members, key=lambda c: (c.start, c.id))
                local_bases[group["id"], sid] = by_clip[sid, local.id]
                local_groups.setdefault(sid, []).append(group["id"])
    film_end = max(placement_ends, default=Fraction(0))
    graph, functions = {}, {}

    def node(owner, edge):
        return ("clip", *owner, edge)

    def cut_node(cut, edge, sid=""):
        return ("cut", cut, edge, sid) if sid else ("cut", cut, edge)

    def is_beat_source(owner, clip):
        return owner[0] != "parent" and lookup._is_audio(owner[1], clip)

    def add(key, dependencies, function):
        graph[key], functions[key] = set(dependencies), function

    def forward(key, target):
        add(key, [target], lambda values: values[target])

    for index, group in enumerate(groups):
        cut = group["id"]
        forward(cut_node(cut, "start"), node(bases[cut], "start"))
        if index + 1 < len(groups):
            forward(cut_node(cut, "end"), cut_node(groups[index + 1]["id"], "start"))
        else:
            add(cut_node(cut, "end"), [], lambda values: film_end)

    # One physical child can appear several times. Keep each occurrence's cut
    # interval local while retaining the global alias and its ambiguity check.
    for sid, cuts in local_groups.items():
        for index, cut in enumerate(cuts):
            owner = local_bases[cut, sid]
            forward(cut_node(cut, "start", sid), node(owner, "start"))
            if index + 1 < len(cuts):
                forward(cut_node(cut, "end", sid), cut_node(cuts[index + 1], "start", sid))
            else:
                end = occurrence_intervals[owner[0]][1]
                add(cut_node(cut, "end", sid), [], lambda values, end=end: end)

    pins = {}
    for binding in bindings:
        key = (binding.target, binding.field)
        if key in pins:
            raise TimingCompileError(f"ambiguous captured binding for {key}")
        pins[key] = binding
    documents = {}
    for capture in sources:
        pin, row = capture["pin"], capture["document"]
        version = pin["version"]
        if isinstance(version, bool) or not isinstance(version, int) or version < 1:
            raise TimingCompileError("captured transcript version must be a positive integer")
        if (set(pin) != {"document_id", "version", "content_digest"}
                or row.get("document_id") != pin["document_id"]
                or type(row.get("version")) is not int or row["version"] != version
                or row.get("project_id") != bundle["project_id"]):
            raise TimingCompileError("captured document identity does not match its exact scoped pin")
        if _digest(row["content"]) != pin["content_digest"]:
            raise TimingCompileError("captured document content digest mismatch")
        key = (pin["document_id"], version)
        if key in documents:
            raise TimingCompileError(f"ambiguous captured transcript {key}")
        documents[key] = capture
    used = set()

    def moment_rule(owner, field, text, in_point=False):
        moment = mo.parse(text)
        dependencies = []
        bound = None
        if moment.kind == "word":
            if moment.n is not None and moment.n < 1:
                raise TimingCompileError("word occurrence must be positive")
            pin = pins.get((owner, field))
            if pin is None or pin.declaration != text:
                raise TimingCompileError(f"missing exact word binding for {owner}.{field}")
            used.add((owner, field))
            bound = _bound_words(bundle, documents, pin, clips, internals)
            dependencies = [node(pin.source, "start")]
        elif moment.kind in ("beat", "downbeat"):
            # Main's legacy beat grammar is resolved from the authored numeric
            # grid, not from a generic marker publication.  Depend on every
            # captured carrier so a changed carrier cannot silently reuse the
            # old derived time; the resolver below keeps Main's stable array
            # order and half-open clip window.
            dependencies.extend(
                dependency for owner_and_clip in clips.items()
                if is_beat_source(*owner_and_clip)
                and _beat_values(owner_and_clip[1], moment.kind) is not None
                for dependency in (node(owner_and_clip[0], "start"), node(owner_and_clip[0], "end"))
            )
            dependencies.extend(dependency for group in groups
                                for dependency in (cut_node(group["id"], "start"),
                                                   cut_node(group["id"], "end")))
            if moment.base is not None and moment.base.kind == "word":
                # The existing Main word pool is assembled from captured
                # source clips.  Make those exact clip intervals available
                # before resolving the beat's word base.
                dependencies.extend(
                    dependency for source_owner in _legacy_word_owners(
                        documents, clips, internals, is_beat_source)
                    for dependency in (node(source_owner, "start"), node(source_owner, "end"))
                )
            elif moment.base is not None and moment.base.kind in ("cut", "cut_end"):
                base_cut = lookup._cut_id(moment.base.cut)
                if base_cut not in bases:
                    raise TimingCompileError(f"missing cut anchor {text!r} for {owner}")
                dependencies.append(cut_node(base_cut, "end" if moment.base.kind == "cut_end" else "start"))
            if moment.base is not None and moment.base.kind in ("beat", "downbeat"):
                raise TimingCompileError("nested beat anchors are unsupported")
        else:
            cut = moment.cut or intent.cut_of(clips[owner])
            cut = lookup._cut_id(cut) if cut else None
            if cut not in bases:
                raise TimingCompileError(f"missing cut anchor {text!r} for {owner}")
            edge = "end" if moment.kind in ("cut_end", "end") else "start"
            dependencies = [cut_node(cut, edge, owner[1] if (cut, owner[1]) in local_bases else "")]
            moment = replace(moment, cut=cut) if moment.cut else moment
        # Main offsets and first-word picture in-point semantics, on the exact bound pool.
        moment = replace(moment, offset_s=_timing_number(moment.offset_s))

        def resolve(values):
            if bound is not None:
                pin, entries, source_in, speed = bound
                source_start = values[node(pin.source, "start")]
                source_end = source_start + _duration(clips[pin.source])
                words = []
                for segment, index, word in entries:
                    start, end = (source_to_timeline_time(_timing_number(word[k]), source_start=source_in,
                                  timeline_start=source_start, speed=speed) for k in ("start", "end"))
                    if start < source_start or start >= source_end or end > source_end:
                        raise TimingCompileError("bound word is outside the half-open source clip interval")
                    words.append(SimpleNamespace(text=word["text"], segment=segment, index=index,
                                                 start=start, end=end))
                if moment.line is not None and any(w.segment != moment.line for w in words):
                    raise TimingCompileError("bound words do not belong to the declared line")
                want = [mo._norm(t) for t in moment.text.split() if mo._norm(t)]
                if len(want) != len(words):
                    raise TimingCompileError("binding must cover the whole declared phrase")
                if want != [mo._norm(word.text) for word in words]:
                    raise TimingCompileError("bound words do not match the declared phrase")
                # Exact IDs already selected the phrase. Resolve its edge directly;
                # even a bounded text search must not select the anchor again.
                t = words[-1].end if moment.edge == "end" else words[0].start
                if (in_point and moment.edge == "start" and words[0].index == 0
                        and not moment.offset_s and not moment.offset_frames):
                    t = source_start - source_in / speed
                else:
                    t += moment.offset_s + moment.offset_frames / fps
            elif moment.kind in ("beat", "downbeat"):
                words = _legacy_words(documents, clips, internals, values, node, is_beat_source)
                beat_values = _resolved_beats(moment.kind, clips, values, node, is_beat_source)
                cuts = {group["id"]: (values[cut_node(group["id"], "start")],
                                      values[cut_node(group["id"], "end")])
                        for group in groups}
                context = _Context(fps, cuts=cuts, words=words, beats=beat_values)
                try:
                    t = mo.resolve(moment, context, in_point=in_point)
                except mo.MomentError as exc:
                    raise TimingCompileError(str(exc)) from exc
            else:
                t = values[dependencies[0]]
                context = _Context(fps, own=(t, t), cuts={cut: (t, t)})
                t = mo.resolve(moment, context, in_point=in_point)
            # main's floor_frame tolerance, using units' exact quantizer at rational fps.
            return Fraction(quantize_time(t + Fraction(str(mo.FRAME_EPSILON)) / fps,
                                          fps, policy="floor")) / fps
        return dependencies, resolve

    for owner, clip in clips.items():
        _maps(clip, str(owner))
        if clip.get("at_ms") is not None or clip.get("duration_ms") is not None:
            raise TimingCompileError("clip millisecond timing needs an explicit authoring adapter")
        declarations = intent._app(clip)
        for key in ("sequence", "sequence_fit", "formulas", "orphan", "gap_after_s"):
            if declarations.get(key):
                raise TimingCompileError(f"unsupported timing declaration {owner}.{key}")
        if declarations.get("until_film_end") is not None:
            raise TimingCompileError("unsupported until_film_end timing basis")
        if declarations.get("fixed_start") is not None and type(declarations["fixed_start"]) is not bool:
            raise TimingCompileError("fixed_start must be a boolean or clear")
        for key in ("on", "until"):
            if declarations.get(key) is not None and not isinstance(declarations[key], str):
                raise TimingCompileError(f"unsupported {key} declaration: expected main Moment text")
        length = _duration(clip)
        start = offsets[owner] + _timing_number(clip.get("at", 0))
        cut = intent.cut_of(clip)
        base = local_bases.get((cut, owner[1]), bases.get(cut))
        cut_sid = owner[1] if (cut, owner[1]) in local_bases else ""
        audio = owner[1] and lookup._is_audio(owner[1], clip)
        identity = (("parent", "", owner[2]) if owner[0] == "parent"
                    else ("shot", owner[1], owner[2]))
        on = intent.on(clip)
        until = intent.until(clip)
        if legacy.get((identity, "on")) == on:
            on = None
        if legacy.get((identity, "until")) == until:
            until = None
        if on:
            add(node(owner, "start"), *moment_rule(owner, "on", on, base == owner))
        elif (cut in bases and base != owner and not audio
              and not declarations.get("fixed_start")):
            forward(node(owner, "start"), cut_node(cut, "start", cut_sid))
        else:
            add(node(owner, "start"), [], lambda values, start=start: start)
        if until:
            add(node(owner, "end"), *moment_rule(owner, "until", until))
        elif declarations.get("for") is not None:
            length = _timing_number(declarations["for"])
            if length <= 0:
                raise TimingCompileError("declared duration must be positive")
            add(node(owner, "end"), [node(owner, "start")],
                lambda values, owner=owner, length=length: values[node(owner, "start")] + length)
        elif cut in bases and not audio and (group_has_successor(groups, cut) if not cut_sid
                                            else local_groups[cut_sid][-1] != cut):
            forward(node(owner, "end"), cut_node(cut, "end", cut_sid))
        elif cut in bases and base != owner and not audio:
            forward(node(owner, "end"), node(base, "end"))
        elif length is not None:
            add(node(owner, "end"), [node(owner, "start")],
                lambda values, owner=owner, length=length: values[node(owner, "start")] + length)
        else:
            raise TimingCompileError(f"missing interval end for {owner}")
    if used != set(pins):
        raise TimingCompileError("unused or stale captured word binding")
    values = _walk(graph, functions)
    result = {}
    for owner in sorted(clips):
        start, end = values[node(owner, "start")], values[node(owner, "end")]
        if start < offsets[owner]:
            raise TimingCompileError(f"clip starts before its occurrence: {owner}")
        interval = quantize_interval(start, end, fps, policy=policy)
        result[owner] = MappingProxyType(dict(interval, start=start, end=end, duration=end - start))
    for pin in bindings:
        _, entries, source_in, speed = _bound_words(bundle, documents, pin, clips, internals)
        interval = result[pin.source]
        for _, _, word in entries:
            start, end = (source_to_timeline_time(_timing_number(word[k]), source_start=source_in,
                          timeline_start=interval["start"], speed=speed) for k in ("start", "end"))
            if start < interval["start"] or start >= interval["end"] or end > interval["end"]:
                raise TimingCompileError("bound word is outside the derived source interval")
            if pin.source[0] != 'parent':
                occurrence_start, occurrence_end = occurrence_intervals[pin.source[0]]
                if start < occurrence_start or start >= occurrence_end or end > occurrence_end:
                    raise TimingCompileError("bound word is outside the half-open occurrence interval")
    return result


def group_has_successor(groups, cut):
    return any(group["id"] == cut for group in groups[:-1])


def _bound_words(bundle, documents, pin, clips, internals):
    if pin.source not in clips:
        raise TimingCompileError("bound source occurrence/clip is missing")
    capture = documents.get((pin.document_id, pin.version))
    if capture is None:
        raise TimingCompileError("exact transcript pin is missing or revoked")
    if capture["pin"]["content_digest"] != pin.content_digest:
        raise TimingCompileError("word binding content digest does not match the exact source pin")
    row = capture["document"]
    content = row["content"]
    source = clips[pin.source]
    asset = (internals[pin.source].get("registry") or {}).get("assets", {}).get(source.get("asset"))
    if (row.get("kind") != "media.transcript" or content.get("schema") != SCHEMA
            or not asset or asset.get("media_id") != content.get("media_id")
            or pin.document_id != transcript_document_id(bundle["project_id"], content["media_id"],
                                                          content["source_id"])):
        raise TimingCompileError("transcript identity does not match the bound media source")
    words = {}
    for segment in content["segments"]:
        for index, word in enumerate(segment["words"]):
            if word["id"] in words:
                raise TimingCompileError("ambiguous stable transcript word identity")
            words[word["id"]] = (segment["id"], index, word)
    if any(key not in words for key in pin.word_ids):
        raise TimingCompileError("bound stable word is missing; text rebinding is forbidden")
    entries = [words[key] for key in pin.word_ids]
    segment, first, _ = entries[0]
    if any(seg != segment or index != first + i for i, (seg, index, _) in enumerate(entries)):
        raise TimingCompileError("bound phrase must contain consecutive words of one segment")
    for _, _, word in entries:
        if _timing_number(word["start"]) < 0 or _timing_number(word["end"]) <= _timing_number(word["start"]):
            raise TimingCompileError("invalid bound word interval")
        if (_timing_number(word["start"]) < _timing_number(_value(source, "from"))
                or source.get("to") is not None and _timing_number(word["end"]) > _timing_number(source["to"])):
            raise TimingCompileError("bound word is outside the half-open source window")
    if _duration(source) is None:
        raise TimingCompileError("bound source needs an exact authored source interval")
    return pin, entries, _timing_number(_value(source, "from")), _playback_rate(_value(source, "speed", 1))
