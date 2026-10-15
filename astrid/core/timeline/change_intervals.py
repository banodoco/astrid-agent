"""Conservative revision impact, in half-open frame and audio-sample intervals.

This is dependency analysis, not a visibility evaluator. Mounted visual support
comes from scene_answers (the existing warm evaluation endpoint). We never use
opacity, cached rectangles or sampled pixels to rule out a changed frame.
Unknown/unqueried support is retained, with the reason and skipped work exposed.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from fractions import Fraction

from astrid.core.execution.remotion_frame_owner import FrameOwnerError

from . import scene_answers
from .duration import clip_end_frame, clip_start_frame, timeline_render_duration_frames


def merge_intervals(intervals):
    result = []
    for start, end in sorted(intervals):
        if end <= start:
            continue
        if result and start <= result[-1][1]:
            result[-1][1] = max(end, result[-1][1])
        else:
            result.append([start, end])
    return result


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for key, child in value.items():
            yield str(key)
            yield from _strings(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            yield from _strings(child)


def _config(snapshot):
    return snapshot.get("config", snapshot)


def _fps(snapshot):
    canvas = _config(snapshot).get("theme_overrides", {}).get("visual", {}).get("canvas", {})
    raw = snapshot.get("fps_rational", canvas.get("fps", 30))
    rate = Fraction(*raw) if isinstance(raw, (list, tuple)) else Fraction(str(raw))
    if rate <= 0:
        raise ValueError("fps must be positive")
    return rate


def _total(snapshot, fps):
    compiled = timeline_render_duration_frames(_config(snapshot), float(fps))
    declared = snapshot.get("render_duration_frames", snapshot.get("duration_frames", compiled))
    if type(declared) is not int or declared < 1:
        raise ValueError("revision duration_frames must be a positive integer")
    return max(compiled, declared)


def _nodes(snapshot):
    config = _config(snapshot)
    rows = {}
    # Compare complete values: new fields cannot silently fall off an allowlist.
    for kind, values in (("clip", config.get("clips", [])), ("track", config.get("tracks", []))):
        for row in values:
            key = (kind, row["id"])
            if key in rows:
                raise ValueError(f"duplicate {kind} identity: {row['id']}")
            rows[key] = row
    for key, value in snapshot.get("registry", config.get("assets", {})).get("assets", {}).items():
        rows["asset", key] = value
    return rows


def _affected(before, after):
    old, new = _nodes(before), _nodes(after)
    changed = {key for key in old.keys() | new.keys() if old.get(key) != new.get(key)}
    timing = ("at", "hold", "from", "to", "speed", "track", "transition")
    changed_tracks = set()
    for key in changed:
        if key[0] == "clip":
            a, b = old.get(key, {}), new.get(key, {})
            if not a or not b or any(a.get(field) != b.get(field) for field in timing):
                changed_tracks.update(c.get("track") for c in (a, b) if c)
    # planTrack couples adjacent draw windows. Retain the affected track's
    # support instead of pretending a transition only depends on its own clip.
    transition_tracks = {
        v.get("track")
        for source in (old, new)
        for k, v in source.items()
        if k[0] == "clip" and v.get("transition") is not None
    }
    for nodes in (old, new):
        changed.update(
            key
            for key, value in nodes.items()
            if key[0] == "clip"
            and value.get("track") in changed_tracks
            and value.get("track") in transition_tracks
        )
    if changed:
        for nodes in (old, new):
            names = {key[1] for key in nodes}
            for key, value in nodes.items():
                if key[0] != "clip":
                    continue
                dependencies = value.get("dependencies", value.get("dependsOn"))
                unknown = dependencies is not None and set(_strings(dependencies)) - names
                # The admitted component producer has no dependency manifest.
                # Its internals may consume changed inputs outside its params.
                component = value.get("clipType", "media") not in {"media", "effect-layer"}
                if unknown or component:
                    changed.add(key)
    # Array order changes paint/mix order even when stable-id values are equal.
    for field, kind in (("clips", "clip"), ("tracks", "track")):
        a = [r["id"] for r in _config(before).get(field, [])]
        b = [r["id"] for r in _config(after).get(field, [])]
        if a != b:
            # Insert/remove does not reorder the surviving rows.
            if [i for i in a if i in b] != [i for i in b if i in a]:
                changed.update((kind, i) for i in a + b)
    # Reverse edges in BOTH revisions; removed references still matter. Exact
    # string references include nested asset/clip/track dependencies and cycles.
    dependents = {}
    for nodes in (old, new):
        by_name = {}
        for key in nodes:
            by_name.setdefault(key[1], set()).add(key)
        for key, value in nodes.items():
            for name in _strings(value):
                for dependency in by_name.get(name, ()):
                    if dependency != key:
                        dependents.setdefault(dependency, set()).add(key)
    pending = list(changed)
    while pending:
        for key in dependents.get(pending.pop(), ()):
            if key not in changed:
                changed.add(key)
                pending.append(key)
    return {key[1] for key in changed if key[0] == "clip"}


def _global(config):
    # Unknown new top-level fields widen rather than being silently ignored.
    return {k: v for k, v in config.items() if k not in {"clips", "tracks", "assets", "notes"}}


def layer_visibility(snapshot, clip_id, frame):
    """Answer a coverage question from the same scene query, including unknown."""
    config, fps = _config(snapshot), _fps(snapshot)
    canvas = config.get("theme_overrides", {}).get("visual", {}).get("canvas", {})
    canvas = {"width": canvas.get("width", 1920), "height": canvas.get("height", 1080)}
    try:
        answer = scene_answers.frames_at(
            config, [frame], float(fps), canvas, assets=snapshot.get("registry")
        )[0]
    except FrameOwnerError as error:
        return {
            "visible": "unknown",
            "findings": [{"code": "evaluation-unavailable", "reason": str(error)}],
        }
    matches = [row for row in answer["layers"] if row["clipId"] == clip_id]
    if not matches:
        return {"visible": False, "findings": [], "reason": "not-mounted"}
    row = matches[0]
    return {
        "visible": row["visible"],
        "bounds": row["bounds"],
        "opacity": row["opacity"],
        "findings": row["findings"],
    }


def revision_intervals(before, after, *, sample_rate=48000, evaluated_frames=None):
    """List every potentially changed frame/sample of resolved immutable inputs.

    ``evaluated_frames`` optionally restricts scene-query work, never the impact
    result. Omitted queries remain explicit skipped intervals. Audio uses the
    renderer's Sequence support, rounded OUTWARD to samples, without guessing
    envelope equality. All gain/curve/source/trim/rate changes retain that support.
    """
    if type(sample_rate) is not int or sample_rate <= 0:
        raise ValueError("sample_rate must be a positive integer")
    configs = [_config(before), _config(after)]
    rates = [_fps(before), _fps(after)]
    totals = [_total(s, f) for s, f in zip((before, after), rates)]
    horizon = max(totals)
    affected = _affected(before, after)
    global_change = _global(configs[0]) != _global(configs[1]) or rates[0] != rates[1]
    registries = [s.get("registry", {}) for s in (before, after)]
    global_change |= {k: v for k, v in registries[0].items() if k != "assets"} != {
        k: v for k, v in registries[1].items() if k != "assets"
    }
    global_change |= any(
        before.get(k) != after.get(k) for k in ("compiled_audio", "renderer_closure", "sample_rate")
    )
    if global_change:
        affected.update(c["id"] for config in configs for c in config.get("clips", []))
    findings, skipped, visual, audio = [], [], [], []
    requested = None if evaluated_frames is None else set(evaluated_frames)
    if requested is not None and any(type(f) is not int or f < 0 for f in requested):
        raise ValueError("evaluated_frames must contain nonnegative integer frames")
    for side, (snapshot, config, fps, total) in enumerate(
        zip((before, after), configs, rates, totals)
    ):
        tracks = {t["id"]: t for t in config.get("tracks", [])}
        changed = [c for c in config.get("clips", []) if c["id"] in affected]
        visual_ids = {
            c["id"] for c in changed if tracks.get(c.get("track"), {}).get("kind") != "audio"
        }
        audio_ids = set()
        fallback = []
        known = {key[1] for key in _nodes(snapshot)}
        for clip in changed:
            start, end = clip_start_frame(clip, float(fps)), clip_end_frame(clip, float(fps))
            span = (max(0, start), min(total, end))
            if clip["id"] in visual_ids:
                fallback.append(span)
            if clip.get("clipType", "media") not in {"media", "effect-layer"}:
                findings.append(
                    {
                        "code": "unknown-component-dependencies",
                        "side": side,
                        "clipId": clip["id"],
                        "reason": "No admitted component dependency manifest; retaining mounted support",
                    }
                )
            if clip.get("dependencies") is not None or clip.get("dependsOn") is not None:
                names = set(_strings(clip.get("dependencies", clip.get("dependsOn"))))
                missing = sorted(names - known)
                if missing:
                    findings.append(
                        {
                            "code": "unknown-dependency",
                            "side": side,
                            "clipId": clip["id"],
                            "reason": ", ".join(missing),
                        }
                    )
            asset = (
                snapshot.get("registry", config.get("assets", {}))
                .get("assets", {})
                .get(clip.get("asset"), {})
            )
            audio_clip = tracks.get(clip.get("track"), {}).get("kind") == "audio"
            # Native video carries sound on a visual track too. Unknown native
            # media types cannot prove silence (source-only layers included).
            media = clip.get("clipType", "media") == "media"
            component = clip.get("clipType", "media") not in {"media", "effect-layer"}
            if (
                audio_clip
                or component
                or (media and not str(asset.get("type", "")).startswith("image/"))
            ):
                audio_ids.add(clip["id"])
                audio.append(
                    (
                        max(0, math.floor(Fraction(start) * sample_rate / fps)),
                        min(
                            math.ceil(Fraction(total) * sample_rate / fps),
                            math.ceil(Fraction(end) * sample_rate / fps),
                        ),
                    )
                )
        if global_change:
            visual.append((0, total))
            audio.append((0, math.ceil(Fraction(total) * sample_rate / fps)))
        if not visual_ids:
            continue
        # Transitions can extend a mount beyond its authored interval. Query the
        # scene over the revision horizon; do not clone planTrack arithmetic.
        canvas = config.get("theme_overrides", {}).get("visual", {}).get("canvas", {})
        canvas = {"width": canvas.get("width", 1920), "height": canvas.get("height", 1080)}
        transitions = any(c.get("transition") is not None for c in config.get("clips", []))
        failed = False
        for start in range(0, total, 120):
            frames = list(range(start, min(start + 120, total)))
            query = frames if requested is None else [f for f in frames if f in requested]
            omitted = {
                f
                for f in set(frames) - set(query)
                if transitions or any(a <= f < b for a, b in fallback)
            }
            if omitted:
                if not any(
                    f["code"] == "evaluation-skipped" and f["side"] == side for f in findings
                ):
                    findings.append(
                        {
                            "code": "evaluation-skipped",
                            "side": side,
                            "reason": "Requested frame subset leaves candidate support unqueried",
                        }
                    )
                skipped.extend((f, f + 1) for f in omitted)
                visual.extend((f, f + 1) for f in omitted)
                if visual_ids & audio_ids:
                    audio.extend(
                        (
                            math.floor(Fraction(f) * sample_rate / fps),
                            math.ceil(Fraction(f + 1) * sample_rate / fps),
                        )
                        for f in omitted
                    )
            if not query:
                continue
            try:
                answers = scene_answers.frames_at(
                    config, query, float(fps), canvas, assets=snapshot.get("registry")
                )
            except FrameOwnerError as error:
                findings.append(
                    {"code": "evaluation-unavailable", "side": side, "reason": str(error)}
                )
                failed = True
                break
            for answer in answers:
                layers = [layer for layer in answer["layers"] if layer["clipId"] in visual_ids]
                if layers:
                    visual.append((answer["frame"], answer["frame"] + 1))
                if any(layer["clipId"] in audio_ids for layer in layers):
                    audio.append(
                        (
                            math.floor(Fraction(answer["frame"]) * sample_rate / fps),
                            math.ceil(Fraction(answer["frame"] + 1) * sample_rate / fps),
                        )
                    )
                for layer in layers:
                    findings.extend({**f, "side": side} for f in layer.get("findings", []))
        if failed:
            # Without the planner, transitions/unknown support are not bounded
            # by stale authored boxes or times. Plain Sequence support remains
            # useful when no track has a transition.
            support = [(0, total)] if transitions else fallback
            visual.extend(support)
            skipped.extend(support)
            if transitions and visual_ids & audio_ids:
                audio.append((0, math.ceil(Fraction(total) * sample_rate / fps)))
    # Duration changes alter presence beyond the shorter revision, even if the
    # extension/removal is blank/silent. Compare in each revision's own clock.
    if totals[0] != totals[1]:
        visual.append((min(totals), horizon))
    sample_totals = [math.ceil(Fraction(t) * sample_rate / f) for t, f in zip(totals, rates)]
    if sample_totals[0] != sample_totals[1]:
        audio.append((min(sample_totals), max(sample_totals)))
    unique = {str(sorted(f.items())): f for f in findings}
    return {
        "frame_intervals": merge_intervals(visual),
        "audio_sample_intervals": merge_intervals(audio),
        "fps_rational": [rates[0].numerator, rates[0].denominator],
        "after_fps_rational": [rates[1].numerator, rates[1].denominator],
        "sample_rate": sample_rate,
        "affected_clips": sorted(affected),
        "status": "unknown" if findings or skipped else "conservative",
        "findings": list(unique.values()),
        "skipped_frame_intervals": merge_intervals(skipped),
        "skipped_audio_sample_intervals": merge_intervals(audio),
        "audio_status": "conservative-support" if audio else "unchanged",
    }


def intervals_from_bundles(before, after):
    """Use the existing immutable renderer projection; never follow live heads."""
    from .shot_composition_projection import project_runtime_parent_composition

    snapshots = []
    for bundle in (before, after):
        closure = bundle.get("immutable_closure")
        if not closure:
            # Normalized authoring bundles are not a second render authority.
            # Their occurrence windows only bound the unresolved work.
            from .cuts import bundle_fps, occurrences_from_bundle

            fps = bundle_fps(bundle)
            end = max((o["end"] for o in occurrences_from_bundle(bundle)), default=0)
            parent = bundle.get("parent", {})
            parent_config = dict(parent.get("config", {}))
            if parent.get("clips"):
                parent_config["clips"] = parent["clips"]
            end = max(end, timeline_render_duration_frames(parent_config, fps) / fps)
            snapshots.append(
                {
                    "config": {
                        "clips": [],
                        "tracks": [],
                        "theme_overrides": {"visual": {"canvas": {"fps": fps}}},
                    },
                    "end": end,
                }
            )
            continue
        projected = project_runtime_parent_composition(
            closure["parent"],
            shot_revisions=list(closure["shots"].values()),
            internal_timeline_revisions=list(closure["internal_timelines"].values()),
        )
        snapshots.append({"config": projected.config, "registry": projected.registry})
    if any("end" in s for s in snapshots):
        # Legacy compare has no admitted renderer input. Preserve an explicit
        # unknown rather than pretending its semantic cut rows prove pixels.
        fps = _fps(snapshots[0])
        end = max(
            (
                s.get(
                    "end",
                    timeline_render_duration_frames(s["config"], float(_fps(s))) / float(_fps(s)),
                )
                for s in snapshots
            )
        )
        changed = before != after
        frames = (
            [[0, max(math.ceil(end * float(_fps(s))) for s in snapshots)]]
            if changed and end > 0
            else []
        )
        samples = [[0, math.ceil(end * 48000)]] if frames else []
        return {
            "frame_intervals": frames,
            "audio_sample_intervals": samples,
            "sample_rate": 48000,
            "fps_rational": [fps.numerator, fps.denominator],
            "status": "unknown",
            "findings": [
                {
                    "code": "resolved-input-unavailable",
                    "reason": "Normalized bundle lacks immutable renderer closure",
                }
            ],
            "skipped_frame_intervals": frames,
            "skipped_audio_sample_intervals": samples,
        }
    return revision_intervals(*snapshots)


def interval_lines(report):
    lines = [
        f"may change: frames {report['frame_intervals']} · audio samples {report['audio_sample_intervals']} ({report['sample_rate']} Hz)"
    ]
    if report.get("skipped_frame_intervals") or report.get("skipped_audio_sample_intervals"):
        lines.append(
            f"not exhaustively checked: frames {report['skipped_frame_intervals']} · audio samples {report['skipped_audio_sample_intervals']}"
        )
    if report["status"] == "unknown":
        lines.append(
            "change coverage: unknown · "
            + "; ".join(dict.fromkeys(f["code"] for f in report["findings"]))
        )
    return lines
