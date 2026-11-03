"""Final, provider-side validation for a timeline authoring candidate.

Commands are allowed to leave a working copy in a temporarily incomplete state.
This module is deliberately called once, after the command batch has finished,
so only a candidate that can be published is checked here.
"""
from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from fractions import Fraction
from typing import Any, NoReturn, cast

from jsonschema import Draft202012Validator
from referencing import Registry
from referencing.exceptions import Unresolvable

from astrid.core.timeline.authoring_bundle import AuthoringBundleError
from astrid.core.timeline.parent_registry import ParentRegistryError, effective_parent_registry
from astrid.sdk.timeline._command_state import equal
from astrid.sdk.timeline.compiler import TimingCompileError
from astrid.sdk.timeline.units import _time_fraction


class FinalCandidateError(AuthoringBundleError):
    """The complete candidate cannot be safely published."""


def _fail(path: str, message: str) -> NoReturn:
    raise FinalCandidateError(f"{path}: {message}")


def _validation_number(value: Any, path: str, *, positive: bool = False) -> Fraction:
    if isinstance(value, bool) or not isinstance(value, (int, float, Fraction)):
        _fail(path, "must be a finite number")
    try:
        result = _time_fraction(value, path)
    except (ArithmeticError, TypeError, ValueError) as exc:
        _fail(path, "must be a finite number")
        raise AssertionError from exc
    try:
        finite = math.isfinite(float(result))
    except OverflowError:
        finite = False
    if not finite:
        _fail(path, "must be finite")
    if positive and result <= 0:
        _fail(path, "must be positive")
    return result


def _fps(value: Any, path: str) -> Fraction:
    if isinstance(value, Mapping):
        if set(value) != {"numerator", "denominator"}:
            _fail(path, "rational fps requires numerator and denominator")
        numerator, denominator = value["numerator"], value["denominator"]
    else:
        return _validation_number(value, path, positive=True)
    if (isinstance(numerator, bool) or isinstance(denominator, bool)
            or not isinstance(numerator, int) or not isinstance(denominator, int)):
        _fail(path, "rational fps requires integer numerator/denominator")
    if numerator <= 0 or denominator <= 0:
        _fail(path, "fps rational must be positive")
    return Fraction(numerator, denominator)


def _fps_values(bundle: Mapping[str, Any]) -> list[tuple[str, Fraction]]:
    values: list[tuple[str, Fraction]] = []
    parent_value: Any = bundle.get("parent")
    if not isinstance(parent_value, Mapping):
        _fail("candidate.parent", "must be an object")
    parent: Mapping[str, Any] = parent_value
    config_value: Any = parent.get("config")
    if not isinstance(config_value, Mapping):
        _fail("candidate.parent.config", "must be an object")
    config: Mapping[str, Any] = config_value

    output = config.get("output")
    if isinstance(output, Mapping) and "fps" in output:
        values.append(("candidate.parent.config.output.fps", _fps(output["fps"], "candidate.parent.config.output.fps")))
    for shot_id, shot in (bundle.get("shots") or {}).items():
        if not isinstance(shot, Mapping):
            _fail(f"candidate.shots[{shot_id!r}]", "must be an object")
        internal = shot.get("internal_timeline")
        if not isinstance(internal, Mapping):
            _fail(f"candidate.shots[{shot_id!r}].internal_timeline", "must be an object")
        child_output = internal.get("output")
        if isinstance(child_output, Mapping) and "fps" in child_output:
            values.append((f"candidate.shots[{shot_id!r}].internal_timeline.output.fps",
                           _fps(child_output["fps"], f"candidate.shots[{shot_id!r}].internal_timeline.output.fps")))
    return values


def _typed_fps_paths(bundle: Mapping[str, Any]) -> set[str]:
    """Return the authored locations governed by the strict FPS policy."""
    paths: set[str] = set()
    parent = bundle.get("parent")
    config = parent.get("config") if isinstance(parent, Mapping) else None
    output = config.get("output") if isinstance(config, Mapping) else None
    if isinstance(output, Mapping) and "fps" in output:
        paths.add("parent.config.output.fps")
    for shot_id, shot in (bundle.get("shots") or {}).items():
        internal = shot.get("internal_timeline") if isinstance(shot, Mapping) else None
        child_output = internal.get("output") if isinstance(internal, Mapping) else None
        if isinstance(child_output, Mapping) and "fps" in child_output:
            paths.add(f"shots[{shot_id!r}].internal_timeline.output.fps")
    return paths


def _fps_basis(bundle: Mapping[str, Any]) -> str:
    """Fingerprint authored rates and their physical clock owners, not timing edits."""
    config = bundle["parent"]["config"]
    overrides = config.get("theme_overrides")
    visual = overrides.get("visual") if isinstance(overrides, Mapping) else None
    canvas_value = visual.get("canvas") if isinstance(visual, Mapping) else None
    canvas = canvas_value if isinstance(canvas_value, Mapping) else {}
    source = bundle.get("source_mapping") or {}
    source_shots = source.get("shots") or {}
    shots = {}
    for shot_id, shot in (bundle.get("shots") or {}).items():
        owner = source_shots.get(shot_id) or {}
        output = shot["internal_timeline"].get("output") or {}
        shots[shot_id] = {
            "shot_id": shot.get("shot_id"),
            "owner": {key: owner.get(key) for key in (
                "shot_id", "revision_id", "internal_timeline_id", "internal_timeline_revision_id")},
            "fps": {key: output[key] for key in ("fps",) if key in output},
        }
    return json.dumps({
        "project_id": bundle.get("project_id"), "timeline_id": bundle.get("timeline_id"),
        "parent": bundle.get("base_parent"),
        "output": {key: config.get("output", {})[key] for key in ("fps",)
                   if key in (config.get("output") or {})},
        "canvas": {key: canvas[key] for key in ("fps",) if key in canvas},
        "shots": shots,
        "owners": {row["occurrence_id"]: row.get("shot_id") for row in bundle.get("placements", [])},
    }, sort_keys=True, allow_nan=False)


def _authenticated_initialization_fps_removals(
    candidate: Mapping[str, Any], baseline: Mapping[str, Any] | None,
    restoration_records: Sequence[Mapping[str, Any]], inverse_records: Sequence[Mapping[str, Any]],
) -> set[str]:
    """Return FPS removed by the complete guarded inverse of ``timeline.init``."""
    allowed: set[str] = set()
    if baseline is None or not inverse_records or not restoration_records:
        return allowed
    parent = candidate.get("parent")
    config = parent.get("config") if isinstance(parent, Mapping) else None
    if isinstance(config, Mapping) and "output" in config:
        return allowed
    output_removed = any(
        record.get("command", {}).get("op") == "timeline.inverse"
        and any(write.get("ref") == "parent"
                and write.get("path") in (["config", "output"], ["config", "output", "fps"])
                and write.get("before", {}).get("state") == "present"
                and write.get("after") == {"state": "absent"}
                for write in record.get("writes", []))
        for record in restoration_records)
    if not output_removed:
        return allowed

    from ._step_undo import entry_records

    for entry in getattr(inverse_records, "entries", ()):
        selected = entry_records(inverse_records, entry)
        if not equal(selected, entry.get("records", {}).get("commands")):
            continue
        init_records = [record for record in selected
                        if record.get("command", {}).get("op") == "timeline.init"]
        introduced = any(
            write.get("ref") == "parent" and write.get("path") == ["config", "output"]
            and write.get("before") == {"state": "absent"}
            and write.get("after", {}).get("state") == "present"
            and isinstance(write.get("after", {}).get("value"), Mapping)
            and "fps" in write["after"]["value"]
            for record in init_records for write in record.get("writes", []))
        if not introduced:
            continue
        candidate_output = candidate.get("parent", {}).get("config", {}).get("output")
        if ("parent.config.output.fps" not in _typed_fps_paths(baseline)
                or candidate_output is not None):
            continue
        allowed.add("parent.config.output.fps")
        break
    return allowed


def _authenticated_deleted_shot_fps_removals(
    candidate: Mapping[str, Any], baseline: Mapping[str, Any] | None,
    restoration_records: Sequence[Mapping[str, Any]], inverse_records: Sequence[Mapping[str, Any]],
) -> set[str]:
    """Allow FPS to disappear with the exact shot root created by that history."""
    allowed: set[str] = set()
    if baseline is None or not inverse_records or not restoration_records:
        return allowed
    before_shots = baseline.get("shots")
    after_shots = candidate.get("shots")
    if not isinstance(before_shots, Mapping) or not isinstance(after_shots, Mapping):
        return allowed
    births = set()
    for source_record in inverse_records:
        for fact in source_record.get("created", []):
            ref = fact.get("ref") if isinstance(fact, Mapping) else None
            if not isinstance(ref, str) or not ref.startswith("shot:"):
                continue
            for write in source_record.get("writes", []):
                value = write.get("after", {}).get("value", {})
                internal = value.get("internal_timeline") if isinstance(value, Mapping) else None
                output = internal.get("output") if isinstance(internal, Mapping) else None
                if (write.get("ref") == ref and write.get("path") == []
                        and write.get("before") == {"state": "absent"}
                        and write.get("after", {}).get("state") == "present"
                        and isinstance(output, Mapping) and "fps" in output):
                    births.add(ref)
    deletions = {
        write["ref"] for record in restoration_records
        if record.get("command", {}).get("op") == "timeline.inverse"
        for write in record.get("writes", [])
        if write.get("ref", "").startswith("shot:") and write.get("path") == []
        and write.get("before", {}).get("state") == "present"
        and write.get("after") == {"state": "absent"}
    }
    for ref in births & deletions:
        shot_id = ref.removeprefix("shot:")
        fps_path = f"shots[{shot_id!r}].internal_timeline.output.fps"
        if fps_path in _typed_fps_paths(baseline) and shot_id not in after_shots:
            allowed.add(fps_path)
    return allowed


def _validate_fps(
    candidate: Mapping[str, Any], baseline: Mapping[str, Any] | None,
    restoration_records: Sequence[Mapping[str, Any]] = (),
    inverse_records: Sequence[Mapping[str, Any]] = (),
) -> None:
    values = _fps_values(candidate)
    # New declarations (including pristine initialization) still pass value and
    # parity validation; every previously authored typed location must survive.
    removed = (_authenticated_initialization_fps_removals(
        candidate, baseline, restoration_records, inverse_records)
        | _authenticated_deleted_shot_fps_removals(
            candidate, baseline, restoration_records, inverse_records))
    if (baseline is not None
            and not _typed_fps_paths(baseline) <= (_typed_fps_paths(candidate) | removed)):
        _fail("candidate", "all authored fps declarations must retain their typed locations")
    if not values or all(rate == values[0][1] for _, rate in values[1:]):
        return
    # D12 preserves an untouched legacy mismatch without equating its exact
    # fractions. New/changed declarations or clock owners must satisfy parity.
    if baseline is not None:
        try:
            if _fps_basis(candidate) == _fps_basis(baseline):
                return
        except (KeyError, TypeError, ValueError, AttributeError):
            pass
    _fail("candidate", "all authored fps values must be the same rational rate")


def _identity_rows(rows: Any, path: str, key: str) -> dict[str, Mapping[str, Any]]:
    if not isinstance(rows, list):
        _fail(path, "must be a list")
    result: dict[str, Mapping[str, Any]] = {}
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            _fail(f"{path}[{index}]", "must be an object")
        identity = row.get(key)
        if not isinstance(identity, str) or not identity:
            _fail(f"{path}[{index}].{key}", "must be a non-empty string")
        if identity in result:
            _fail(path, f"duplicate {key} {identity!r}")
        result[identity] = row
    return result


def _clip_duration(clip: Mapping[str, Any], path: str) -> tuple[Fraction, Fraction]:
    at = clip.get("at")
    at_ms = clip.get("at_ms")
    if at is not None and at_ms is not None and _validation_number(at, f"{path}.at") != _validation_number(at_ms, f"{path}.at_ms") / 1000:
        _fail(path, "at and at_ms timing codecs disagree")
    if at is None and at_ms is not None:
        at = _validation_number(at_ms, f"{path}.at_ms") / 1000
    if at is None:
        _fail(path, "requires an authored start")
    start = _validation_number(at, f"{path}.at")
    if start < 0:
        _fail(f"{path}.at", "must not be negative")
    speed = _validation_number(1 if clip.get("speed") is None else clip["speed"], f"{path}.speed", positive=True)
    source_start = _validation_number(0 if clip.get("from") is None else clip["from"], f"{path}.from")
    if source_start < 0:
        _fail(f"{path}.from", "must not be negative")
    source_to = clip.get("to")
    if source_to is not None:
        source_end = _validation_number(source_to, f"{path}.to")
        if source_end <= source_start:
            _fail(path, "source interval must be half-open and non-empty")
    hold = clip.get("hold")
    if hold is not None:
        source_length = _validation_number(hold, f"{path}.hold", positive=True)
    else:
        if source_to is None:
            duration = clip.get("duration")
            if duration is None:
                _fail(path, "requires hold or a complete source interval")
            source_length = _validation_number(duration, f"{path}.duration", positive=True)
        else:
            source_length = source_end - source_start
    if source_length <= 0:
        _fail(path, "duration must be positive")
    return start, source_length / speed


def _asset_duration(asset: Mapping[str, Any]) -> Fraction | None:
    for key, scale in (("duration", 1), ("duration_seconds", 1), ("duration_ms", 1000)):
        value = asset.get(key)
        if value is not None:
            return _validation_number(value, f"asset.{key}") / scale
    return None


def _same_fields(current: Mapping[str, Any], previous: Mapping[str, Any] | None,
                 fields: Sequence[str]) -> bool:
    if previous is None:
        return False
    return json.dumps({key: current[key] for key in fields if key in current}, sort_keys=True) == json.dumps(
        {key: previous[key] for key in fields if key in previous}, sort_keys=True)


def _validate_clip_list(
    clips: Any,
    *,
    path: str,
    tracks: Mapping[str, Mapping[str, Any]],
    registry: Mapping[str, Any],
    bound: Fraction | None = None,
    baseline: Mapping[str, Any] | None = None,
    baseline_bound: Fraction | None = None,
) -> dict[str, Mapping[str, Any]]:
    if not isinstance(registry, Mapping):
        _fail(f"{path.rsplit('.', 1)[0]}.registry", "must be an object")
    by_id = _identity_rows(clips, path, "id")
    assets = registry.get("assets", {})
    if not isinstance(assets, Mapping):
        _fail(f"{path.rsplit('.', 1)[0]}.registry.assets", "must be an object")
    before = {row["id"]: row for row in baseline.get("clips", [])} if baseline else {}
    before_tracks = {row["id"] for row in baseline.get("tracks", [])} if baseline else set()
    before_assets = baseline.get("registry", {}).get("assets", {}) if baseline else {}
    for clip_id, clip in by_id.items():
        clip_path = f"{path}[{clip_id!r}]"
        prior = before.get(clip_id)
        track = clip.get("track")
        if not isinstance(track, str) or track not in tracks:
            if not (isinstance(track, str) and _same_fields(clip, prior, ("track",))
                    and track not in before_tracks):
                _fail(f"{clip_path}.track", "must reference an existing track")
        timing_changed = not _same_fields(
            clip, prior, ("at", "at_ms", "from", "to", "hold", "duration", "speed"))
        # D12a checks the interval when its declaration or occurrence bound is
        # written. An unrelated edit preserves an existing overrun verbatim.
        if timing_changed or bound != baseline_bound:
            start, duration = _clip_duration(clip, clip_path)
            if bound is not None and start + duration > bound:
                _fail(clip_path, "timeline interval must fit the occurrence's half-open bounds")
        asset_key = clip.get("asset", clip.get("asset_id"))
        if asset_key is not None:
            if asset_key not in assets or not isinstance(assets[asset_key], Mapping):
                if (_same_fields(clip, prior, ("asset", "asset_id"))
                        and asset_key not in before_assets):
                    continue
                _fail(f"{clip_path}.asset", f"unknown asset {asset_key!r}")
            if (_same_fields(clip, prior, ("asset", "asset_id", "from", "to", "hold", "duration", "speed"))
                    and _same_fields(assets[asset_key], before_assets.get(asset_key),
                                     ("media_id", "object_id", "type", "duration", "duration_seconds", "duration_ms"))):
                continue
            _, duration = _clip_duration(clip, clip_path)
            source_start = _validation_number(0 if clip.get("from") is None else clip["from"], f"{clip_path}.from")
            source_duration = _asset_duration(assets[asset_key])
            # The authored source window is independent of an image's hold.
            source_end = (_validation_number(clip["to"], f"{clip_path}.to") if clip.get("to") is not None
                          else source_start + duration * _validation_number(
                              1 if clip.get("speed") is None else clip["speed"], f"{clip_path}.speed", positive=True))
            if source_duration is not None and source_end > source_duration:
                _fail(clip_path, "source interval exceeds the retained asset bounds")
    return by_id


def _parent_clips(parent: Mapping[str, Any]) -> list[Any]:
    config = parent.get("config", {})
    rows = [(scope["clips"], path) for scope, path in (
        (parent, "candidate.parent.clips"), (config, "candidate.parent.config.clips"))
        if "clips" in scope]
    lists = [value for value, _ in rows if isinstance(value, list)]
    for value, path in rows:
        if not isinstance(value, list) and not (value is None and lists):
            _fail(path, "must be a list")
    return lists[0] if lists else []


def _baseline_clips(bundle: Mapping[str, Any]) -> dict[tuple[str, str], Mapping[str, Any]]:
    result: dict[tuple[str, str], Mapping[str, Any]] = {}
    parent = bundle.get("parent") or {}
    for clip in _parent_clips(parent) if isinstance(parent, Mapping) else []:
        if isinstance(clip, Mapping) and isinstance(clip.get("id"), str):
            result[("parent", clip["id"])] = clip
    for shot_id, shot in (bundle.get("shots") or {}).items():
        if isinstance(shot, Mapping):
            for clip in (shot.get("internal_timeline") or {}).get("clips", []):
                if isinstance(clip, Mapping) and isinstance(clip.get("id"), str):
                    result[(str(shot_id), clip["id"])] = clip
    return result


def _timing_parent_view(bundle: Mapping[str, Any]) -> Mapping[str, Any]:
    from astrid.sdk.timeline._doc_patch import null_clip_mirror

    parent = bundle["parent"]
    config = parent.get("config")
    timing_config = config
    open_canvas = False
    if isinstance(config, Mapping):
        overrides = config.get("theme_overrides")
        visual = overrides.get("visual") if isinstance(overrides, Mapping) else None
        if isinstance(overrides, Mapping) and isinstance(visual, Mapping) and "canvas" in visual:
            open_canvas = True
            canvas = visual["canvas"]
            if not isinstance(canvas, Mapping) or "fps" not in canvas:
                timing_canvas = dict(canvas) if isinstance(canvas, Mapping) else {}
                timing_canvas["fps"] = None
                timing_visual = {**visual, "canvas": timing_canvas}
                timing_overrides = dict(overrides)
                timing_overrides["visual"] = timing_visual
                timing_config = {**config, "theme_overrides": timing_overrides}
    if not null_clip_mirror(parent) and not open_canvas:
        return bundle
    clips = _parent_clips(parent)
    # The timing kernel consumes one list. Adapt its detached input, never the
    # canonical payload, source images or authenticated restoration records.
    source = bundle.get("source_mapping")
    timing_source = ({**source, "_timing_open_canvas": open_canvas}
                     if isinstance(source, Mapping) else source)
    return {**bundle, "source_mapping": timing_source, "parent": {**parent, "clips": clips,
            "config": {**timing_config, "clips": clips}}}


def _schema_for_selected(
    clip: Mapping[str, Any], *, schema_inputs: Sequence[Any], realm_id: str | None, project: str,
    required: bool = True, identity_only: bool = False,
) -> Mapping[str, Any] | None:
    reference = clip.get("elementRef") or clip.get("element_ref")
    if not isinstance(reference, Mapping) or not reference.get("packId"):
        return None
    # The retained closure may contain more than one admission for the same
    # pack owner.  Select the immutable element identity first; pack_id alone
    # is only a namespace and must never choose the declaration or pin.
    matches = []
    for item in schema_inputs:
        if not isinstance(item, (tuple, list)) or len(item) != 2:
            continue
        verified, objects = item
        if not isinstance(verified, Mapping) or not isinstance(objects, Mapping):
            continue
        document = verified.get("document")
        content = document.get("content") if isinstance(document, Mapping) else None
        if not isinstance(content, Mapping) or content.get("pack_id") != reference.get("packId"):
            continue
        admissions = content.get("admissions")
        if not isinstance(admissions, list):
            continue
        exact = [admission for admission in admissions
                 if isinstance(admission, Mapping) and all(
                     admission.get(key) == reference.get(key)
                     for key in ("id", "kind", "revision"))]
        if len(exact) == 1:
            matches.append((verified, objects, exact[0]))
    if not matches:
        if not required:
            return None
        _fail("elementRef", "selected project element has no unique retained schema")
    from astrid.core.element.project_packs import admitted_element_snapshot_descriptor

    # A commit may retain several exact DocumentPins for equal immutable
    # element identities.  Only the pin whose portable object was consumed by
    # this candidate can qualify; other retained pins may intentionally have
    # no object in this provider invocation.
    declarations = []
    for verified, objects, admission in matches:
        admission_map = cast(Mapping[str, Any], admission)
        descriptor_ref = admission_map.get("snapshot_object")
        object_id = descriptor_ref.get("object_id") if isinstance(descriptor_ref, Mapping) else None
        snapshot = objects.get(object_id) if isinstance(object_id, str) else None
        if snapshot is None:
            continue
        if not isinstance(realm_id, str) or not realm_id:
            _fail("elementRef", "selected project elements require retained schema authority")
        try:
            descriptor = admitted_element_snapshot_descriptor(
                realm_id, project, dict(reference), verified,
                snapshot,
            )
        except (KeyError, StopIteration, TypeError, ValueError) as exc:
            _fail("elementRef", f"retained element declaration is unavailable: {exc}")
        if isinstance(descriptor, Mapping):
            declarations.append(descriptor)
    if not declarations:
        # An element-only inverse can restore an authenticated retained
        # admission without consuming that revision's portable params schema.
        if not required or identity_only:
            return None
        _fail("elementRef", "retained element declaration is unavailable")
    schemas = [declaration.get("schema") for declaration in declarations]
    if any(schema != schemas[0] for schema in schemas[1:]):
        _fail("elementRef", "selected project element has ambiguous retained declarations")
    if not isinstance(schemas[0], Mapping):
        _fail("elementRef", "retained element schema is unavailable")
    return schemas[0]


def _text_reference_basis(bundle: Mapping[str, Any], shot_id: str, reference: Mapping[str, Any]) -> Any:
    shots = bundle.get("shots", {})
    owners = sorted(shots) if shot_id == "parent" else [shot_id]
    bindings: list[tuple[str, Mapping[str, Any]]] = []
    for owner in owners:
        rows = shots.get(owner, {}).get("payload", {}).get("text_bindings", [])
        if not isinstance(rows, list):
            _fail("text_bindings", "must be a list")
        bindings.extend((owner, row) for row in rows if isinstance(row, Mapping)
                        and row.get("binding_id") == reference["binding_id"])
    source = bundle.get("source_mapping", {})
    documents = [row for row in source.get("pinned_documents", [])
                 if row.get("pin", {}).get("document_id") == reference["anchor"]["document_id"]]
    return (bundle.get("project_id"), bundle.get("timeline_id"),
            {owner: source.get("shots", {}).get(owner) for owner in owners}, bindings, documents)


def _validate_element_reference(
    clip: Mapping[str, Any], *, path: str, baseline: Mapping[str, Any] | None,
    candidate: Mapping[str, Any], baseline_bundle: Mapping[str, Any] | None, shot_id: str,
) -> bool:
    """Retain historical text bindings without treating them as element selections."""
    from astrid.sdk.timeline.commands import _SCHEMA, _VALIDATOR

    references = {key: clip[key] for key in ("elementRef", "element_ref") if key in clip}
    if not references:
        return False
    text = False
    for key, reference in references.items():
        if isinstance(reference, Mapping) and reference.get("kind") == "text" and "binding_id" in reference:
            anchor = reference.get("anchor")
            if (set(reference) != {"kind", "binding_id", "anchor"}
                    or not isinstance(reference["binding_id"], str) or not reference["binding_id"].strip()
                    or not isinstance(anchor, Mapping) or set(anchor) != {"document_id", "word_id"}
                    or any(not isinstance(anchor[field], str) or not anchor[field].strip()
                           for field in ("document_id", "word_id"))):
                _fail(f"{path}.{key}", "invalid historical text binding reference")
            # This historical form has no immutable transcript pin with which
            # the native word-reference validator could authorize a new choice.
            # D12 permits only its exact retained declaration and source basis.
            from astrid.sdk.timeline.commit import _json

            previous = {name: baseline[name] for name in ("elementRef", "element_ref")
                        if baseline is not None and name in baseline}
            if (baseline is None or baseline_bundle is None
                    or _json(references) != _json(previous)
                    or clip.get("clipType") != baseline.get("clipType")
                    or _json(_text_reference_basis(candidate, shot_id, reference))
                    != _json(_text_reference_basis(baseline_bundle, shot_id, reference))):
                _fail(f"{path}.{key}", "historical text binding requires an unchanged declaration and source")
            text = True
        else:
            errors = list(_VALIDATOR.evolve(schema=_SCHEMA["$defs"]["ElementRef"]).iter_errors(reference))
            if errors:
                _fail(f"{path}.{key}", errors[0].message)
    if text and len(references) != 1:
        _fail(path, "historical text binding has ambiguous reference aliases")
    return text


def _validate_params(
    clip: Mapping[str, Any], *, path: str, baseline: Mapping[str, Any] | None,
    schema_inputs: Sequence[Any], realm_id: str | None, project: str,
    text_reference: bool = False,
) -> None:
    params = clip.get("params", {})
    # Guarded inverse may restore this literal container after a PARAM write.
    if params is None:
        params = {}
    if not isinstance(params, Mapping):
        _fail(f"{path}.params", "must be an object")
    previous = baseline.get("params", {}) if isinstance(baseline, Mapping) else {}
    previous = previous if isinstance(previous, Mapping) else {}
    changed = {key: value for key, value in params.items()
               if key not in previous or not equal(previous[key], value)}
    element_ref = clip.get("elementRef") or clip.get("element_ref")
    element = (element_ref.get("id") if isinstance(element_ref, Mapping) and not text_reference
               else clip.get("clipType", "media"))
    if not isinstance(element, str):
        _fail(f"{path}.clipType", "must identify an element")
    selected = isinstance(element_ref, Mapping) and element_ref.get("packId")
    # Native creation retains opaque literals until a separate selection
    # supplies authority; preserve that existing command lifecycle.
    if selected and baseline is None:
        return
    if selected:
        previous_ref = (baseline.get("elementRef") or baseline.get("element_ref")) if baseline else None
        schema = _schema_for_selected(
            clip, schema_inputs=schema_inputs, realm_id=realm_id, project=project,
            required=bool(changed) or not equal(element_ref, previous_ref),
            identity_only=not changed)
        if schema is None:
            return
    else:
        from astrid.sdk.timeline_address import element_schema

        schema = element_schema(element)
    properties = schema.get("properties", {}) if isinstance(schema, Mapping) else {}
    if not isinstance(properties, Mapping):
        properties = {}
    validator = Draft202012Validator(dict(schema), registry=Registry())
    # Sealed built-in manifests are normalized by element_schema under the
    # short ``additional`` key; retained project JSON Schemas use the standard
    # ``additionalProperties`` spelling.  Keep their policies separate.
    if selected:
        # Project declarations validate known fields only; unknown authored
        # values remain opaque by the retained-parameter compatibility rule.
        additional = True
    elif isinstance(schema, Mapping):
        additional = schema.get("additional", schema.get("additionalProperties", True))
    else:
        additional = True
    for key, value in params.items():
        if key in properties:
            try:
                errors = list(validator.evolve(schema=properties[key]).iter_errors(value))
            except Unresolvable as exc:
                _fail(f"{path}.params.{key}", f"parameter schema reference is unavailable: {exc}")
            if errors:
                _fail(f"{path}.params.{key}", errors[0].message)
        elif key in changed and key not in previous and additional is False:
            _fail(f"{path}.params.{key}", "is not declared by the retained element schema")


def validate_final_candidate(
    candidate: Mapping[str, Any], *,
    baseline: Mapping[str, Any] | None = None,
    schema_inputs: Sequence[Any] = (),
    realm_id: str | None = None,
    source_bundles: Sequence[Mapping[str, Any]] = (),
    restoration_records: Sequence[Mapping[str, Any]] = (),
    inverse_records: Sequence[Mapping[str, Any]] = (),
) -> None:
    """Validate one complete candidate without changing it.

    ``baseline`` distinguishes newly authored declarations from unchanged
    legacy declarations that must survive a round trip.  ``schema_inputs``
    are the exact retained Runtime captures collected for this commit; this
    helper never resolves a mutable project-pack source.
    ``source_bundles`` are authenticated history boundaries and restore closures,
    also used for immutable child reuse when a source is absent from the head.
    ``restoration_records`` are provider-executed guarded history writes; only
    their exact restored declarations may use those authenticated boundaries.
    """
    if not isinstance(candidate, Mapping):
        _fail("candidate", "must be an object")
    try:
        json.dumps(candidate, allow_nan=False)
    except (TypeError, ValueError) as exc:
        _fail("candidate", f"must be finite JSON: {exc}")
    _validate_fps(candidate, baseline, restoration_records, inverse_records)
    parent_value: Any = candidate.get("parent")
    if not isinstance(parent_value, Mapping):
        _fail("candidate.parent", "must be an object")
    parent: Mapping[str, Any] = parent_value
    config_value: Any = parent.get("config")
    if not isinstance(config_value, Mapping):
        _fail("candidate.parent.config", "must be an object")
    config: Mapping[str, Any] = config_value
    tracks = _identity_rows(config.get("tracks", []), "candidate.parent.config.tracks", "id")
    try:
        registry = effective_parent_registry(parent)
    except ParentRegistryError as exc:
        _fail("candidate.parent.registry", str(exc))
    if "registry" in parent and "registry" in config and not equal(parent["registry"], config["registry"]):
        _fail("candidate.parent.config.registry", "does not match parent.registry")
    baseline_parent = (baseline or {}).get("parent", {})
    if not _same_fields(candidate, baseline, ("project_id", "timeline_id", "base_parent")):
        baseline_parent = {}
    # An explicit null mirror is legacy storage, never a newly authored list.
    for scope, previous, path in (
        (parent, baseline_parent, "candidate.parent.clips"),
        (config, baseline_parent.get("config", {}), "candidate.parent.config.clips"),
    ):
        if "clips" in scope and scope["clips"] is None and (
                "clips" not in previous or previous["clips"] is not None):
            _fail(path, "must be a list")
    clips = _validate_clip_list(
        _parent_clips(parent), path="candidate.parent.clips",
        tracks=tracks, registry=registry,
        baseline={"clips": _parent_clips(baseline_parent),
                  "tracks": baseline_parent.get("config", {}).get("tracks", []),
                  "registry": effective_parent_registry(baseline_parent)} if baseline_parent else None,
    )
    mirror = config.get("clips")
    if isinstance(mirror, list) and isinstance(parent.get("clips"), list) and not equal(mirror, parent["clips"]):
        _fail("candidate.parent.config.clips", "does not match parent.clips")

    placements = _identity_rows(candidate.get("placements", []), "candidate.placements", "occurrence_id")
    shots_value: Any = candidate.get("shots")
    if not isinstance(shots_value, Mapping):
        _fail("candidate.shots", "must be an object")
    shots: Mapping[str, Any] = shots_value
    for occurrence_id, placement in placements.items():
        shot_id_value = placement.get("shot_id")
        if not isinstance(shot_id_value, str):
            _fail(f"candidate.placements[{occurrence_id!r}].shot_id", "must be a string")
        shot_id = cast(str, shot_id_value)
        if shot_id not in shots:
            _fail(f"candidate.placements[{occurrence_id!r}].shot_id", "references a missing shot")
        duration_ms = _validation_number(placement.get("duration_ms"), f"candidate.placements[{occurrence_id!r}].duration_ms", positive=True)
        start_ms = (placement.get("placement") or {}).get("start_ms", placement.get("at_ms", 0))
        start = _validation_number(start_ms, f"candidate.placements[{occurrence_id!r}].placement.start_ms") / 1000
        if start < 0:
            _fail(f"candidate.placements[{occurrence_id!r}].placement.start_ms", "must not be negative")
        bound = duration_ms / 1000
        shot_value: Any = shots[shot_id]
        internal_value = shot_value.get("internal_timeline") if isinstance(shot_value, Mapping) else None
        if not isinstance(internal_value, Mapping):
            _fail(f"candidate.shots[{shot_id!r}].internal_timeline", "must be an object")
        child_config = cast(Mapping[str, Any], internal_value)
        child_tracks = _identity_rows(child_config.get("tracks", []), f"candidate.shots[{shot_id!r}].internal_timeline.tracks", "id")
        baseline_shot = ((baseline or {}).get("shots") or {}).get(shot_id, {})
        baseline_internal = baseline_shot.get("internal_timeline", {}) if isinstance(baseline_shot, Mapping) else {}
        source = candidate.get("source_mapping", {}).get("shots", {}).get(shot_id, {})
        previous_source = (baseline or {}).get("source_mapping", {}).get("shots", {}).get(shot_id)
        previous_placement = next((row for row in (baseline or {}).get("placements", [])
                                   if row.get("occurrence_id") == occurrence_id), None)
        if (not _same_fields(source, previous_source, (
                "shot_id", "revision_id", "internal_timeline_id", "internal_timeline_revision_id"))
                or not _same_fields(placement, previous_placement, ("shot_id",))
                or not _same_fields(candidate, baseline, ("project_id", "timeline_id"))):
            baseline_internal = {}
        previous_bound = (bound if baseline_internal and _same_fields(
            placement, previous_placement, ("duration_ms",)) else None)
        child_clips = _validate_clip_list(
            child_config.get("clips", []), path=f"candidate.shots[{shot_id!r}].internal_timeline.clips",
            tracks=child_tracks, registry=child_config.get("registry", {}), bound=bound,
            baseline=baseline_internal, baseline_bound=previous_bound,
        )
        before_child = _identity_rows(baseline_internal.get("clips", []), "baseline.clips", "id") if isinstance(baseline_internal, Mapping) and isinstance(baseline_internal.get("clips"), list) else {}
        for clip_id, clip in child_clips.items():
            clip_path = f"candidate.shots[{shot_id!r}].internal_timeline.clips[{clip_id!r}]"
            text_reference = _validate_element_reference(
                clip, path=clip_path, baseline=before_child.get(clip_id),
                candidate=candidate, baseline_bundle=baseline, shot_id=shot_id,
            )
            _validate_params(
                clip, path=clip_path, text_reference=text_reference,
                baseline=before_child.get(clip_id), schema_inputs=schema_inputs,
                realm_id=realm_id, project=str(candidate.get("project_id", "")),
            )
    for clip_id, clip in clips.items():
        prior = _baseline_clips(baseline or {}).get(("parent", clip_id))
        clip_path = f"candidate.parent.clips[{clip_id!r}]"
        text_reference = _validate_element_reference(
            clip, path=clip_path, baseline=prior, candidate=candidate,
            baseline_bundle=baseline, shot_id="parent",
        )
        _validate_params(
            clip, path=clip_path, baseline=prior, text_reference=text_reference,
            schema_inputs=schema_inputs, realm_id=realm_id, project=str(candidate.get("project_id", "")),
        )

    from astrid.sdk.timeline._commit_timing_validation import validate_candidate_timing

    try:
        if source_bundles:
            validate_candidate_timing(_timing_parent_view(candidate),
                                      baseline=_timing_parent_view(baseline) if baseline else None,
                                      source_bundles=[_timing_parent_view(source) for source in source_bundles],
                                      restoration_records=restoration_records)
        else:
            validate_candidate_timing(_timing_parent_view(candidate),
                                      baseline=_timing_parent_view(baseline) if baseline else None)
    except (TimingCompileError, KeyError, TypeError, ValueError) as exc:
        _fail("candidate.timing", str(exc))


__all__ = ["FinalCandidateError", "validate_final_candidate"]
