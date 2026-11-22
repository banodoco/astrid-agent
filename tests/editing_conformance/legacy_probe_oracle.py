"""The per-declaration legacy probe of ``release/14/main`` before #394, frozen verbatim as a test oracle.

Production now proves legacy exemptions with a declaration-only pass on the base and the candidate, and
probes one declaration only when a resolve-time failure points at it. The functions below are the code
that was removed (``_legacy_exemptions`` is renamed ``old_exemptions``). Do not edit them: they are what
the differential tests compare against. ``_reference_shape`` and ``_reference_basis`` are the deep-copying versions from before #400. The helpers
still imported from production (``_legacy_rows``, ``_legacy_declarations``, ``_physical_shot_context``)
were not changed by #400; the probe runs the production ``validate_candidate_timing``.
"""
from __future__ import annotations

import copy
from collections.abc import Mapping
from typing import Any

from astrid.core.timeline import moments
from astrid.sdk import timeline_intent as intent
from astrid.sdk.timeline._commit_timing_validation import (
    _legacy_declarations, _legacy_rows, _physical_shot_context, validate_candidate_timing)
from astrid.sdk.timeline.compiler import TimingCompileError
from astrid.sdk.timeline.native_word_anchor import legacy_binding


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


def old_exemptions(
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
