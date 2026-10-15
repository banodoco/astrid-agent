"""Run every registered check (built-in and pack) over a timeline's cuts.

``timelines lint`` calls :func:`run_checks` with doc checks only; the motion
view adds frame checks for its one cut. Thresholds come from each check's
declared defaults, overridden by the project rules file (``rules.py``);
severities can be overridden per finding code (``"off"`` drops a code).
"""
from __future__ import annotations

from typing import Any, Mapping, Sequence

from ..layers.base import SEVERITIES, CheckContext, Finding, check_defaults, checks
from . import data, model


def _merged_params(params: Mapping[str, Any] | None) -> dict[str, Any]:
    merged = check_defaults()
    merged.update({key: value for key, value in (params or {}).items()})
    return merged


def _apply_severity(findings: list[Finding], severity: Mapping[str, str] | None) -> list[Finding]:
    if not severity:
        return findings
    out = []
    for finding in findings:
        level = severity.get(finding.code)
        if level == "off":
            continue
        if level in SEVERITIES:
            finding = Finding(finding.code, finding.cut, finding.t, finding.message, level,
                               finding.fix, finding.check, finding.show_scope)
        out.append(finding)
    return out


def _normalise(found: Any, check_name: str) -> list[Finding]:
    rows = []
    for item in found or ():
        if not isinstance(item, Finding):
            raise TypeError(f"check {check_name!r} returned {type(item).__name__}; return Finding objects (ctx.finding(...))")
        rows.append(item if item.check else Finding(item.code, item.cut, item.t, item.message,
                                                     item.severity, item.fix, check_name, item.show_scope))
    return rows


def run_checks(
    cuts: Sequence[Mapping[str, Any]], elements: Sequence[model.Element], fps: float, *,
    track_order: Sequence[str] = (), beats: Mapping[str, Any] | None = None,
    params: Mapping[str, Any] | None = None, severity: Mapping[str, str] | None = None,
    needs: set[str] | None = None, frames: Mapping[int, Any] | None = None, all_cuts: Sequence[Mapping[str, Any]] | None = None,
    only: Sequence[str] | None = None, shared: dict[str, Any] | None = None, disable: Sequence[str] = (),
    include_declared_checks: bool = False, extension_context: Any | None = None,
) -> tuple[list[tuple[Mapping[str, Any], list[Finding]]], list[Finding]]:
    """``(per_cut, timeline)`` findings.

    ``needs`` limits which checks run: ``{"doc"}`` (lint) skips checks that
    need frames; pass ``{"doc", "frames"}`` with ``frames`` inside the motion
    view. ``only`` restricts to named checks.
    """
    needs = needs or {"doc", "words", "beats", "audio"}
    merged = _merged_params(params)
    registry = [c for c in checks(include_v3=include_declared_checks).values()
                if (not only or c.name in only) and c.name not in set(disable)
                and set(c.needs) <= (needs | {"doc", "words", "beats", "audio"})
                and ("frames" not in c.needs or "frames" in needs)]
    elements = list(elements)
    words = model.words(elements)
    sfx = model.sfx(elements)
    all_tracks = data.tracks(elements)
    per_cut: list[tuple[Mapping[str, Any], list[Finding]]] = []
    for cut in cuts:
        start, end = float(cut["start"]), float(cut["end"])
        inside = model.in_window(elements, start, end)
        context = CheckContext(
            cut=cut, cuts=list(all_cuts or cuts), fps=fps, elements=inside, all_elements=elements, words=words,
            beats=model.timeline_beats(beats, elements, start - 0.5, end + 0.5), sfx=sfx,
            tracks=[t for t in all_tracks if t.clip_id in {e.id for e in inside}] + [t for t in all_tracks if t.name == "face"],
            params=merged, track_order=track_order, frames=frames or {}, shared=shared if shared is not None else {},
            extension_context=extension_context,
        )
        found: list[Finding] = []
        for check in registry:
            if check.scope == "cut":
                found.extend(_normalise(check.run(context), check.name))
        per_cut.append((cut, sorted(_apply_severity(found, severity), key=lambda f: (f.t if f.t is not None else 0, f.code))))
    timeline: list[Finding] = []
    whole = CheckContext(
        cut=None, cuts=list(all_cuts or cuts), fps=fps, elements=elements, all_elements=elements, words=words,
        beats=model.timeline_beats(beats, elements, 0.0, max([e.end for e in elements] or [0.0])), sfx=sfx,
        tracks=all_tracks, params=merged, track_order=track_order, frames=frames or {},
        shared=shared if shared is not None else {},
        extension_context=extension_context,
    )
    for check in registry:
        if check.scope == "timeline":
            timeline.extend(_normalise(check.run(whole), check.name))
    return per_cut, _apply_severity(timeline, severity)
