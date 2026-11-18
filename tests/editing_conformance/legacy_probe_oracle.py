"""The retired per-declaration legacy probe, kept as a test oracle.

Production now decides which legacy ``on``/``until`` declarations the base already failed to resolve with
one declaration-only pass on the base and one on the candidate (``_declaration_failures``). This module
is the code it replaced: it proves each declaration with a full ``validate_candidate_timing`` on a copy of
the base that keeps only that declaration. The differential tests compare the two exemption sets.
"""
from __future__ import annotations

import copy
from collections.abc import Mapping
from typing import Any

from astrid.core.timeline import moments
from astrid.sdk import timeline_intent as intent
from astrid.sdk.timeline._commit_timing_validation import (
    _known_unresolvable_message, _legacy_declarations, _legacy_rows, _physical_shot_context,
    _reference_basis, validate_candidate_timing)
from astrid.sdk.timeline.compiler import TimingCompileError
from astrid.sdk.timeline.native_word_anchor import legacy_binding


def _known_unresolvable(error: BaseException) -> bool:
    return _known_unresolvable_message(str(error))


def _clear_legacy_declarations(bundle: dict[str, Any], keep: tuple[tuple[Any, ...], str]) -> None:
    """Leave one declaration in a detached probe of the original bundle."""
    rows = dict(_legacy_rows(bundle))
    for key, clip in rows.items():
        for field in ("on", "until"):
            if (key, field) != keep:
                intent._set(clip, field, None)


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
            return not _known_unresolvable(exc)
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
            return not _known_unresolvable(context_error)
        return True
    return True


def old_exemptions(candidate, baseline):
    """The exemption set the per-declaration probes produced."""
    if baseline is None:
        return set()
    before = dict(_legacy_rows(baseline))
    exemptions = set()
    for key, clip in _legacy_rows(candidate):
        prior = before.get(key)
        if prior is None:
            continue
        prior_values = _legacy_declarations(prior)
        for field, value in _legacy_declarations(clip).items():
            if (prior_values.get(field) == value
                    and _reference_basis(candidate, key, clip, value)
                    == _reference_basis(baseline, key, prior, value)
                    and not _legacy_declaration_is_resolvable(baseline, key, field, value)):
                exemptions.add((key, field))
    return exemptions
