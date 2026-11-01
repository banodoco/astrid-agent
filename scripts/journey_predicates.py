"""Journey predicates called by M-CI's authenticated evaluator.

These functions consume one already-authenticated, selected index entry.  They
recompute the R-30 direct/Pinokio semantic relations, but never authenticate
artifacts, select executions, close inventory coverage, or become a second
evaluator.  An absent M-CI finalized context is deliberately non-admissible.
"""
from __future__ import annotations

if __package__:
    from . import journey_observations as observations
else:
    try:
        import journey_observations as observations
    except ModuleNotFoundError:
        from scripts import journey_observations as observations


def _check(family: str, parser, evidence: dict) -> tuple[bool, list[str]]:
    parsed, reasons = parser(evidence)
    if parsed is not None:
        reasons.extend(observations.semantic_observation_errors(parsed, family))
    return not reasons, reasons


def check_release1(evidence: dict) -> tuple[bool, list[str]]:
    """Check one selected Release-1 row with a finalized M-CI context."""
    return _check("release1", observations.parse_release1_evidence, evidence)


def check_m4_5(evidence: dict) -> tuple[bool, list[str]]:
    """Check one selected M4-5 scenario with producer semantics."""
    return _check("m4_5", observations.parse_m4_5_evidence, evidence)


def check_m4_6(evidence: dict) -> tuple[bool, list[str]]:
    """Check one selected M4-6 slot with producer semantics."""
    return _check("m4_6", observations.parse_m4_6_evidence, evidence)
