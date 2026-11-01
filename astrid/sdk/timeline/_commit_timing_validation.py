"""Validate a complete canonical candidate through the existing timing kernel."""
from __future__ import annotations

import copy
import json
from collections.abc import Mapping, Sequence
from typing import Any

from astrid.core.timeline import moments
from astrid.core.timeline.authoring_bundle import compile_authoring_candidate
from astrid.core.timeline.shot_composition_projection import _placement_speed
from astrid.sdk import timeline_intent as intent
from astrid.sdk.timeline._timing_evaluators import _fps, _maps, _ReadLookup
from astrid.sdk.timeline.compiler import TimingCompileError, TimingInputs, compile_timing
from astrid.sdk.timeline.native_word_anchor import binding, legacy_binding
from astrid.sdk.timeline.units import _time_fraction


def _legacy_rows(bundle: Mapping[str, Any]):
    parent = bundle.get("parent", {})
    parent_clips = parent.get("clips", []) if isinstance(parent, Mapping) else []
    for clip in parent_clips:
        if isinstance(clip, Mapping):
            yield ("parent", "", clip.get("id")), clip
    shots = bundle.get("shots", {})
    if not isinstance(shots, Mapping):
        return
    for shot_id, shot in shots.items():
        internal = shot.get("internal_timeline", {}) if isinstance(shot, Mapping) else {}
        clips = internal.get("clips", []) if isinstance(internal, Mapping) else []
        for clip in clips:
            if isinstance(clip, Mapping):
                yield ("shot", shot_id, clip.get("id")), clip


def _legacy_declarations(clip: Mapping[str, Any]) -> dict[str, str]:
    declarations = intent._app(clip)
    anchors = declarations.get("word_anchors")
    if not isinstance(anchors, Mapping):
        anchors = {}
    return {
        field: value
        for field in ("on", "until")
        if field not in anchors
        and isinstance(value := declarations.get(field), str)
        and value.strip()
    }


def _reference_shape(clip: Mapping[str, Any]) -> dict[str, Any]:
    """Keep timing/reference inputs while ignoring unrelated authored params."""
    value = copy.deepcopy(dict(clip))
    value.pop("params", None)
    app = value.get("app")
    if isinstance(app, dict):
        app.pop("opaque", None)
        nested = app.get("astrid")
        if isinstance(nested, dict):
            nested.pop("on", None)
            nested.pop("until", None)
            nested.pop("opaque", None)
            if not nested:
                app.pop("astrid", None)
        app.pop("on", None)
        app.pop("until", None)
        if not app:
            value.pop("app", None)
    return value


def _reference_basis(
    bundle: Mapping[str, Any], key: tuple[Any, ...], clip: Mapping[str, Any], declaration: str,
) -> dict[str, Any]:
    """Identify the authored target of one legacy timing declaration."""
    kind, owner, _ = key
    basis: dict[str, Any] = {"clip": _reference_shape(clip)}
    source = bundle.get("source_mapping", {})
    basis["documents"] = copy.deepcopy(
        source.get("pinned_documents", []) if isinstance(source, Mapping) else []
    )
    try:
        word = moments.parse(declaration).kind == "word"
    except (TypeError, ValueError, moments.MomentError):
        word = False
    if word:
        # Legacy text lookup searches every occurrence. An unresolved word has
        # no selected binding, so retain the complete search basis and physical
        # owners rather than guessing which source it meant to reference.
        basis["parent_owner"] = copy.deepcopy(bundle.get("base_parent"))
        basis["placements"] = copy.deepcopy(bundle.get("placements", []))
        basis["source_shots"] = copy.deepcopy(source.get("shots", {}))
        basis["word_sources"] = {
            shot_id: {
                "shot_id": shot.get("shot_id"),
                "clips": [{**_reference_shape(row), "declarations": copy.deepcopy(intent._app(row))}
                          for row in shot["internal_timeline"]["clips"]],
                "inputs": {field: copy.deepcopy(shot["internal_timeline"][field])
                           for field in ("tracks", "registry", "output", "source_window", "time_map", "timeMap")
                           if field in shot["internal_timeline"]},
            }
            for shot_id, shot in bundle.get("shots", {}).items()
        }
    if kind == "parent":
        parent = bundle.get("parent", {})
        clips = parent.get("clips", []) if isinstance(parent, Mapping) else []
        basis["siblings"] = [_reference_shape(row) for row in clips if isinstance(row, Mapping)]
        return basis

    shots = bundle.get("shots", {})
    shot = shots.get(owner, {}) if isinstance(shots, Mapping) else {}
    internal = shot.get("internal_timeline", {}) if isinstance(shot, Mapping) else {}
    clips = internal.get("clips", []) if isinstance(internal, Mapping) else []
    basis["siblings"] = [_reference_shape(row) for row in clips if isinstance(row, Mapping)]
    placements = bundle.get("placements", [])
    basis["placements"] = [
        copy.deepcopy(row)
        for row in placements
        if isinstance(row, Mapping) and row.get("shot_id") == owner
    ]
    source_shots = source.get("shots", {}) if isinstance(source, Mapping) else {}
    source_shot = source_shots.get(owner) if isinstance(source_shots, Mapping) else None
    basis["source"] = copy.deepcopy(source_shot)
    return basis


def _clear_legacy_declarations(bundle: dict[str, Any], keep: tuple[tuple[Any, ...], str]) -> None:
    """Leave one declaration in a detached probe of the original bundle."""
    rows = dict(_legacy_rows(bundle))
    for key, clip in rows.items():
        for field in ("on", "until"):
            if (key, field) != keep:
                intent._set(clip, field, None)


def _physical_shot_context(bundle: Mapping[str, Any], shot_id: str) -> dict[str, Any] | None:
    """Return one exact authoring-shot context for the existing evaluator."""
    placements = [
        copy.deepcopy(row)
        for row in bundle.get("placements", [])
        if isinstance(row, Mapping) and row.get("shot_id") == shot_id
    ]
    shots = bundle.get("shots", {})
    source_mapping = bundle.get("source_mapping", {})
    source_shots = source_mapping.get("shots", {}) if isinstance(source_mapping, Mapping) else {}
    if len(placements) != 1 or not isinstance(shots, Mapping) or shot_id not in shots:
        return None
    if not isinstance(source_shots, Mapping) or shot_id not in source_shots:
        return None

    context = copy.deepcopy(dict(bundle))
    context["placements"] = placements
    context["base_placements"] = {
        row["occurrence_id"]: copy.deepcopy(row)
        for row in bundle.get("base_placements", {}).values()
        if isinstance(row, Mapping) and row.get("shot_id") == shot_id
    }
    # The exact authoring shot ID is occurrence-scoped; do not resolve by the
    # source shot/timeline alias when narrowing this validation context.
    context["shots"] = {shot_id: copy.deepcopy(shots[shot_id])}
    narrowed = context["source_mapping"]
    narrowed["placements"] = copy.deepcopy(context["base_placements"])
    narrowed["shots"] = {shot_id: copy.deepcopy(source_shots[shot_id])}
    return context


def _legacy_beat_only(bundle: Mapping[str, Any]) -> bool:
    rows = list(_legacy_rows(bundle))
    if any(key[0] == "parent" and _legacy_declarations(clip) for key, clip in rows):
        return False
    declarations = [value for _, clip in rows for value in _legacy_declarations(clip).values()]
    if not declarations:
        return False
    try:
        return all(moments.parse(value).kind in {"beat", "downbeat"} for value in declarations)
    except (TypeError, ValueError, moments.MomentError):
        return False


def _known_unresolvable_timing_error(error: BaseException) -> bool:
    message = str(error)
    return any(
        marker in message
        for marker in (
            "missing cut anchor",
            "no beat",
            "no downbeat",
            "clip starts before its occurrence",
        )
    )


def _legacy_declaration_is_resolvable(
    bundle: Mapping[str, Any], key: tuple[Any, ...], field: str, declaration: str,
) -> bool:
    """Prove a retained declaration resolves before allowing an exemption."""
    try:
        moment = moments.parse(declaration)
    except (TypeError, ValueError, moments.MomentError):
        return False

    if moment.kind == "word":
        # Child legacy word anchors have never had an authorized source binding;
        # the production evaluator therefore treats them as unresolved.
        if key[0] != "parent":
            return False
        sources = bundle.get("source_mapping", {}).get("pinned_documents", [])
        try:
            legacy_binding(bundle, "parent/clip:" + str(key[2]), field, declaration, sources)
        except (TimingCompileError, KeyError, TypeError, ValueError):
            return False
        return True

    probe = copy.deepcopy(dict(bundle))
    _clear_legacy_declarations(probe, (key, field))
    target = dict(_legacy_rows(probe)).get(key)
    if target is None:
        return False
    intent._set(target, field, declaration)
    try:
        validate_candidate_timing(probe)
    except (TimingCompileError, KeyError, TypeError, ValueError) as exc:
        # A detached shared-child probe can fail on a sibling physical image
        # before reaching this declaration. Re-run the same evaluator against
        # this declaration's exact occurrence-scoped image; unresolved or
        # unknown results must never be converted into an exemption.
        if "ambiguous cut identity" not in str(exc) and "clip starts before its occurrence" not in str(exc):
            # An unclassified failure is not proof that this declaration was
            # unresolvable in the base, so keep it in the real validation path.
            return not _known_unresolvable_timing_error(exc)
        context = _physical_shot_context(probe, key[1]) if key[0] == "shot" else None
        if context is None:
            return True
        try:
            validate_candidate_timing(context)
        except (TimingCompileError, KeyError, TypeError, ValueError) as context_error:
            # A second ambiguity is unknown, not proof of an unresolvable
            # declaration. Known invalid/missing inputs remain exemptible.
            if "ambiguous cut identity" in str(context_error):
                return True
            return not _known_unresolvable_timing_error(context_error)
        return True
    return True


def _legacy_exemptions(
    candidate: Mapping[str, Any], baseline: Mapping[str, Any] | None,
) -> set[tuple[tuple[Any, ...], str]]:
    """Return unchanged declarations proven unresolvable in the base."""
    if baseline is None:
        return set()
    before = dict(_legacy_rows(baseline))
    exemptions: set[tuple[tuple[Any, ...], str]] = set()
    for key, clip in _legacy_rows(candidate):
        prior = before.get(key)
        if prior is None:
            continue
        current_values = _legacy_declarations(clip)
        prior_values = _legacy_declarations(prior)
        for field, value in current_values.items():
            if (prior_values.get(field) == value
                    and _reference_basis(candidate, key, clip, value)
                    == _reference_basis(baseline, key, prior, value)
                    and not _legacy_declaration_is_resolvable(baseline, key, field, value)):
                exemptions.add((key, field))
    return exemptions


def _legacy_inputs(
    candidate: Mapping[str, Any], exemptions: set[tuple[tuple[Any, ...], str]],
) -> tuple[tuple[tuple[str, str, str], str, str], ...]:
    rows = dict(_legacy_rows(candidate))
    return tuple((key, field, _legacy_declarations(rows[key])[field])
                 for key, field in sorted(exemptions, key=lambda item: (item[0], item[1])))


def _restored_legacy_exemptions(
    candidate: Mapping[str, Any], sources: Sequence[Mapping[str, Any]],
    records: Sequence[Mapping[str, Any]],
) -> set[tuple[tuple[Any, ...], str]]:
    """Recognize exact parent-word restorations from provider-owned boundaries.

    Sources come from authenticated history, and records from guarded execution,
    never from request JSON. Ordinary writes cannot borrow inverse authority.
    The legacy lookup searches child audio, so independent parent clip values
    are immaterial; their membership/order and all physical child inputs remain
    part of the comparison. The parent revision itself necessarily turns over.
    """
    from ._command_state import State, overlaps

    exemptions: set[tuple[tuple[Any, ...], str]] = set()
    if not records:
        return exemptions
    current = State(candidate)
    for source in sources:
        if any(candidate.get(key) != source.get(key) for key in ("project_id", "timeline_id")):
            continue
        before = dict(_legacy_rows(source))
        previous = State(source)
        for key, clip in _legacy_rows(candidate):
            if key[0] != "parent" or clip != before.get(key):
                continue
            for field, value in _legacy_declarations(clip).items():
                if moments.parse(value).kind != "word":
                    continue
                target = ("parent/clip:" + str(key[2]), ("app", "astrid", field))
                alias = (target[0], ("app", field))
                restored = False
                authored = False
                for record in records:
                    for write in record["writes"]:
                        site = (write["ref"], write["path"])
                        if record["command"]["op"] not in {"timeline.inverse", "timeline.restore"}:
                            authored |= overlaps(site, (target[0], ()))
                            continue
                        if not (overlaps(site, target) or overlaps(site, alias)):
                            continue
                        if write["after"] == current.get(*site) == previous.get(*site):
                            restored = True
                if authored or not restored:
                    continue
                bases = []
                for bundle in (candidate, source):
                    basis = _reference_basis(bundle, key, clip, value)
                    basis.pop("parent_owner", None)
                    basis["siblings"] = [row.get("id") for row in bundle["parent"]["clips"]]
                    basis["parent_inputs"] = {
                        name: content for name, content in bundle["parent"].items()
                        if name not in {"clips", "config"}
                    }
                    basis["parent_config"] = {
                        name: content for name, content in bundle["parent"]["config"].items()
                        if name != "clips"
                    }
                    bases.append(basis)
                if bases[0] == bases[1] and not _legacy_declaration_is_resolvable(source, key, field, value):
                    exemptions.add((key, field))
    return exemptions


def _timing_basis(bundle: Mapping[str, Any], owners: set[tuple[Any, ...]]) -> str:
    """Retain the physical owners and inputs of one timing dependency component."""
    parent = bundle["parent"]
    timelines = {"parent": {**parent["config"], **parent}}
    timelines.update({"shot:" + key: shot["internal_timeline"] for key, shot in bundle["shots"].items()})
    inputs = {}
    for owner, timeline in timelines.items():
        key = ("parent", "") if owner == "parent" else ("shot", owner.removeprefix("shot:"))
        clips = [clip for clip in timeline.get("clips", []) if (*key, clip["id"]) in owners]
        tracks = {clip.get("track") for clip in clips}
        assets = {clip.get("asset", clip.get("asset_id")) for clip in clips}
        inputs[owner] = {
            "clips": [{"clip": _reference_shape(clip), "declarations": intent._app(clip)} for clip in clips],
            "tracks": [row for row in timeline.get("tracks", []) if row.get("id") in tracks],
            "assets": {key: value for key, value in timeline.get("registry", {}).get("assets", {}).items()
                       if key in assets},
            "config": {key: value for key, value in timeline.items()
                       if key not in {"clips", "tracks", "registry", "config", "name", "unknown", "meta"}},
        }
    source = bundle.get("source_mapping", {})
    return json.dumps({
        "project_id": bundle.get("project_id"), "timeline_id": bundle.get("timeline_id"),
        "parent_owner": bundle.get("base_parent"), "placements": bundle.get("placements"),
        "source_shots": source.get("shots"), "documents": source.get("pinned_documents"),
        "shots": {key: {field: value for field, value in shot.items() if field != "internal_timeline"}
                  for key, shot in bundle["shots"].items()},
        "timelines": inputs,
    }, sort_keys=True, allow_nan=False)


def _map_components(bundle: Mapping[str, Any]) -> list[set[tuple[Any, ...]]]:
    """Bound unsupported nodes using the kernel's cut lookup and Moment grammar.

    Edges are undirected: retaining an unsupported node also retains its unchanged
    consumers. The compiler still evaluates every component that was written.
    """
    rows = dict(_legacy_rows(bundle))
    graph = {key: {key} for key in rows}
    lookup = _ReadLookup(copy.deepcopy(dict(bundle)), _fps(bundle))

    def connect(keys):
        keys = set(keys) & rows.keys()
        for key in keys:
            graph[key].update(keys)

    groups = lookup._cut_groups()
    cuts = {group["id"]: {
        ("shot", clip.shot_id, clip.id) if clip.shot_id else ("parent", "", clip.id)
        for clip in group["clips"]
    } for group in groups}
    for index, group in enumerate(groups):
        # A cut's end consumes the next cut's start (including implicit fits).
        connect(cuts[group["id"]] | (cuts[groups[index + 1]["id"]] if index + 1 < len(groups) else set()))
    audio = {key for key, clip in rows.items()
             if key[0] == "shot" and lookup._is_audio(key[1], clip)}
    sources = bundle.get("source_mapping", {}).get("pinned_documents", [])
    for key, clip in rows.items():
        declarations = intent._app(clip)
        if any(declarations.get(field) is not None for field in (
                "sequence", "sequence_fit", "formulas", "orphan", "gap_after_s", "until_film_end")):
            # The kernel cannot bound these inputs. Keep their dependency
            # selection conservative instead of interpreting another grammar.
            connect(rows)
        for field in ("on", "until"):
            text = declarations.get(field)
            refs = declarations.get("word_anchors") or {}
            if field in refs:
                selected = binding(bundle, "parent/clip:" + str(key[2]), field, refs[field], sources)
                connect({key, ("shot", selected.source[1], selected.source[2])})
            if not isinstance(text, str) or not text:
                continue
            try:
                moment: moments.Moment | None = moments.parse(text)
            except (TypeError, ValueError, moments.MomentError):
                # Malformed legacy text has no target in the existing grammar.
                # A written declaration still remains in compiler validation.
                continue
            while moment is not None:
                if moment.kind == "word":
                    # Legacy text lookup searches the captured audio pool.
                    connect({key} | audio)
                elif moment.kind in {"beat", "downbeat"}:
                    connect({key} | audio | set().union(*cuts.values()))
                else:
                    cut = lookup._cut_id(moment.cut or intent.cut_of(clip))
                    connect({key} | cuts.get(cut, set()))
                moment = moment.base
    components = []
    remaining = set(rows)
    while remaining:
        pending = [next(iter(remaining))]
        component = set()
        while pending:
            key = pending.pop()
            if key not in component:
                component.add(key)
                pending.extend(graph[key] - component)
        remaining -= component
        if any(_has_explicit_map(rows[key]) for key in component):
            components.append(component)
    return components


def _has_explicit_map(clip: Mapping[str, Any]) -> bool:
    try:
        _maps(clip, "candidate")
    except TimingCompileError:
        return True
    return False


def _retain_legacy_map_components(
    projection: dict[str, Any], candidate: Mapping[str, Any], baseline: Mapping[str, Any] | None,
) -> set[tuple[Any, ...]]:
    if baseline is None:
        return set()
    if not any(_has_explicit_map(clip) for _, clip in _legacy_rows(candidate)):
        return set()
    components = _map_components(candidate)
    for previous in _map_components(baseline):
        overlapping = [component for component in components if component & previous]
        if overlapping:
            combined = previous.union(*overlapping)
            components = [component for component in components if component not in overlapping]
            components.append(combined)
    retained = set().union(*(component for component in components
                            if _timing_basis(candidate, component) == _timing_basis(baseline, component)))
    if not retained:
        return set()
    # This is a detached compiler selection, never a rewritten map or authored
    # declaration. Every written/involved node remains in the real evaluator.
    parent = projection["parent"]
    parent["clips"] = [clip for clip in parent["clips"] if ("parent", "", clip["id"]) not in retained]
    if "clips" in parent["config"]:
        parent["config"]["clips"] = copy.deepcopy(parent["clips"])
    for shot_id, shot in projection["shots"].items():
        internal = shot["internal_timeline"]
        internal["clips"] = [clip for clip in internal["clips"]
                             if ("shot", shot_id, clip["id"]) not in retained]
    return retained


def validate_candidate_timing(
    candidate: Mapping[str, Any], *, baseline: Mapping[str, Any] | None = None,
    _allow_physical_context_fallback: bool = True,
    source_bundles: Sequence[Mapping[str, Any]] = (),
    restoration_records: Sequence[Mapping[str, Any]] = (),
) -> None:
    """Compile a detached validation view, leaving authored JSON and pins intact.

    The canonical compiler guards changes to occurrence source maps. As in
    shot_composition_projection._project_clip, retained occurrence speed and
    source_offset are provenance, while child windows supply timing. Only that
    placement representation and physical child identities are adapted. Untouched
    unsupported components are retained outside the compiler selection; authored
    clips, maps and declarations remain byte-identical.
    """
    publication = compile_authoring_candidate(candidate).publication
    projection = copy.deepcopy(dict(candidate))
    exemptions = _legacy_exemptions(candidate, baseline)
    restored = _restored_legacy_exemptions(candidate, source_bundles, restoration_records)
    exemptions.update(restored)
    legacy_declarations = _legacy_inputs(candidate, exemptions)
    for restored_key, restored_field in restored:
        if restored_field == "on":
            # An exact historical unresolved start retains its cached interval,
            # including its historical film-end flag. Adapt only the detached
            # compiler view; the canonical declaration and aliases stay intact.
            clip = dict(_legacy_rows(projection))[restored_key]
            intent._set(clip, "until_film_end", None)
            clip.get("app", {}).pop("until_film_end", None)
            mirrors = projection["parent"]["config"].get("clips", [])
            for mirror in mirrors:
                if mirror.get("id") == restored_key[2]:
                    intent._set(mirror, "until_film_end", None)
                    mirror.get("app", {}).pop("until_film_end", None)
    config = projection["parent"]["config"]
    output = config.get("output") or {}
    if "fps" in output:
        canvas = config
        for key in ("theme_overrides", "visual", "canvas"):
            if canvas.get(key) is None:
                canvas[key] = {}
            canvas = canvas[key]
        canvas.setdefault("fps", copy.deepcopy(output["fps"]))
    projection["placements"] = copy.deepcopy(publication["parent_composition"]["occurrences"])
    if "_runtime_document" in candidate["source_mapping"]:
        from astrid.sdk.timeline.commit import document_from_bundle

        # Use the provider's physical closure, including shared patch successors
        # and unchanged reused images, rather than the compiler's local copies.
        document = document_from_bundle(candidate, source_bundles=source_bundles)
        occurrences = {row["occurrence_id"]: row for row in document["parent_revision"]["payload"]["occurrences"]}
        shots = {row["revision_id"]: row for row in document["shot_revisions"]}
        internals = {row["revision_id"]: row for row in document["internal_timeline_revisions"]}
        for row in projection["placements"]:
            physical = shots[occurrences[row["occurrence_id"]]["shot_revision_id"]]
            internal = internals[physical["internal_timeline_revision_id"]]
            projection["source_mapping"]["shots"][row["shot_id"]] = {
                "internal_timeline_id": internal["timeline_id"],
                "internal_timeline_revision_id": internal["revision_id"],
                "internal_timeline_content_digest": internal["content_digest"],
            }
    for row in projection["placements"]:
        path = f"candidate.placements[{row['occurrence_id']!r}]"
        _placement_speed(row.get("speed"), path=f"{path}.speed")
        offset = row.get("source_offset")
        if isinstance(offset, Mapping):
            for key in ("start", "end"):
                if key in offset:
                    _time_fraction(offset[key], f"{path}.source_offset.{key}")
        elif offset is not None:
            _time_fraction(offset, f"{path}.source_offset")
        row["speed"], row["source_offset"] = 1, 0

    sources = candidate.get("source_mapping", {}).get("pinned_documents", [])
    bindings = []
    for shot in projection["shots"].values():
        for clip in shot["internal_timeline"]["clips"]:
            refs = intent._app(clip).get("word_anchors", {})
            if not isinstance(refs, dict) or set(refs) - {"on", "until"}:
                raise TimingCompileError("invalid authored word anchor fields")
            if refs:
                raise TimingCompileError("word anchors require parent ownership")
    for clip in projection["parent"].get("clips", []):
        declarations = intent._app(clip)
        refs = declarations.get("word_anchors", {})
        if not isinstance(refs, dict) or set(refs) - {"on", "until"}:
            raise TimingCompileError("invalid authored word anchor fields")
        for field in ("on", "until"):
            target = "parent/clip:" + clip["id"]
            if field in refs:
                selected = binding(projection, target, field, refs[field], sources)
            elif declarations.get(field) and isinstance(declarations[field], str) and moments.parse(
                    declarations[field]).kind == "word":
                if (("parent", "", clip["id"]), field) in exemptions:
                    continue
                selected = legacy_binding(projection, target, field, declarations[field], sources)
            else:
                continue
            bindings.append(selected)
            # The existing binding adapter supplies the private kernel spelling
            # from exact word IDs. The authored echo remains in the candidate.
            app = clip.setdefault("app", {})
            if app.get("astrid") is None:
                app["astrid"] = {}
            app["astrid"][field] = selected.declaration
            # Parent clips may be mirrored under config.clips.  The detached
            # validation view rewrites the authoritative declaration from the
            # captured binding, so keep the mirror in lockstep before the
            # timing lookup checks the two canonical representations.
            mirrors = projection["parent"].get("config", {}).get("clips")
            if isinstance(mirrors, list):
                mirror = next((row for row in mirrors if row.get("id") == clip.get("id")), None)
                if isinstance(mirror, dict):
                    mirror_app = mirror.setdefault("app", {})
                    mirror_astrid = mirror_app.setdefault("astrid", {})
                    mirror_astrid[field] = selected.declaration
    retained = _retain_legacy_map_components(projection, candidate, baseline)
    inputs = TimingInputs(
        projection,
        sources=[row for row in sources if row["document"]["kind"] == "media.transcript"],
        bindings=[selected for selected in bindings if selected.target not in retained],
        legacy_declarations=legacy_declarations,
    )
    try:
        compile_timing(inputs)
    except TimingCompileError as exc:
        if (not _allow_physical_context_fallback
                or not any(marker in str(exc) for marker in (
                    "ambiguous cut identity", "clip starts before its occurrence"))
                or not _legacy_beat_only(candidate)):
            raise
        # A sibling physical image may make the global cut basis ambiguous even
        # though each unchanged beat/downbeat declaration resolves in its own
        # exact authoring-shot context. Keep all declarations and re-run the
        # existing evaluator per physical image; an exact-context failure still
        # rejects the candidate.
        shot_ids: list[str] = []
        for row in candidate.get("placements", []):
            shot_id = row.get("shot_id") if isinstance(row, Mapping) else None
            if isinstance(shot_id, str):
                shot_ids.append(shot_id)
        if not shot_ids:
            raise
        for shot_id in shot_ids:
            context = _physical_shot_context(candidate, shot_id)
            if context is None:
                raise
            context_baseline = (
                _physical_shot_context(baseline, shot_id) if baseline is not None else None
            )
            validate_candidate_timing(
                context,
                baseline=context_baseline,
                _allow_physical_context_fallback=False,
                source_bundles=source_bundles,
            )
