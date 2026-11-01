"""Fail-closed journey predicates called by M-CI's authenticated evaluator.

These functions consume one already-authenticated, selected index entry.  They
never authenticate artifacts, select executions, close inventory coverage, or
turn producer fields into R-30 independent observations.
"""
from __future__ import annotations

if __package__:
    from . import journey_observations as observations
else:
    try:
        import journey_observations as observations
    except ModuleNotFoundError:
        from scripts import journey_observations as observations


def _closed(label: str, parser, evidence: dict) -> tuple[bool, list[str]]:
    _parsed, reasons = parser(evidence)
    reasons.append(
        f"{label}: independent R-30 section 5 observations and frozen bindings unavailable"
    )
    return False, reasons


def check_release1(evidence: dict) -> tuple[bool, list[str]]:
    """Check one selected Release-1 row; it remains non-admissible."""
    return _closed("Release-1", observations.parse_release1_evidence, evidence)


def check_m4_5(evidence: dict) -> tuple[bool, list[str]]:
    """Check one selected M4-5 scenario; it remains non-admissible."""
    return _closed("M4-5", observations.parse_m4_5_evidence, evidence)


def check_m4_6(evidence: dict) -> tuple[bool, list[str]]:
    """Check one selected M4-6 slot; it remains non-admissible."""
    return _closed("M4-6", observations.parse_m4_6_evidence, evidence)
