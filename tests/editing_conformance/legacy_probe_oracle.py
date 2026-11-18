"""The per-declaration legacy probe, used as a test oracle for the one-pass pre-pass.

Production now decides which legacy ``on``/``until`` declarations the base already failed to resolve with
one declaration-only pass on the base and one on the candidate (``_declaration_failures``). This module
is the code it replaced: it proves each declaration with a full ``validate_candidate_timing`` on a copy of
the base that keeps only that declaration. The differential tests compare the two exemption sets.
"""
from __future__ import annotations

from astrid.sdk.timeline._commit_timing_validation import (
    _clear_legacy_declarations, _legacy_declaration_is_resolvable, _legacy_declarations,
    _legacy_rows, _reference_basis)


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
