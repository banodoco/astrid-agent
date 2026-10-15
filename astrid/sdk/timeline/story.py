"""Canonical story ranges and a detached, revision-bound read producer.

TimeRefs use the existing moments grammar. A migrated cut label keeps its CUT
TimeRef, rather than copying the picture's `on` text as a word TimeRef: the
canonical cut resolver retains the first-word picture/take-origin exception.
No range ids, timing caches or migration markers are persisted.
"""
from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any

from astrid.core.timeline import moments as mo
from astrid.sdk import timeline_intent as intent
from astrid.sdk.timeline._common import TimelineEditError


class StoryRangeError(TimelineEditError):
    """Invalid authored story data or a mismatched pinned host revision."""


def _finding(code: str, path: str, message: str) -> dict[str, str]:
    return dict(code=code, path=path, message=message)


def _rows(bundle):
    return intent._film(bundle).get("ranges", [])


def _shape(row):
    if not isinstance(row, Mapping) or set(row) != {"name", "kind", "from", "to"}:
        return "expected exactly {name, kind, from, to}"
    if not isinstance(row["name"], str) or not row["name"].strip():
        return "name must be a nonempty string"
    if row["kind"] not in ("chapter", "beat"):
        return "kind must be chapter or beat"
    if not isinstance(row["from"], str) or not row["from"].strip():
        return "from must be a moment TimeRef"
    if row["to"] is not None and (not isinstance(row["to"], str) or not row["to"].strip()):
        return "to must be a moment TimeRef or null (film end)"
    return None


def _frame(tl, ref):
    # No numeric/address fallback: unsupported TimeRefs remain explicit findings.
    moment = mo.parse(ref)
    def supported(anchor):
        if anchor.kind == "cut_start":
            raise mo.MomentError("a relative TimeRef needs a cut; story ranges have no own cut")
        if anchor.cut:
            cid = tl._cut_id(anchor.cut) or anchor.cut
            group = next((g for g in tl.cut_index.groups(tl, include_orphans=True) if g["id"] == cid), None)
            if group and (group["orphan"] or group["picture"] is None or intent.orphan(group["picture"].data)
                          or cid in getattr(tl, "_orphan_cuts", ())):
                raise mo.MomentError(f"cut {cid!r} has no resolved live picture anchor")
            if group and group["picture"] is not None:
                pic = group["picture"]
                on = intent.on(pic.data)
                if on:
                    # Validate the declaration, but do not equate its word
                    # target with the compiled cut boundary. The compiler can
                    # pin an opening picture while narration moves. The shared
                    # MomentContext below resolves the live CutIndex start.
                    tl._moment_time(on, pic, in_point=True)
        if anchor.base is not None:
            supported(anchor.base)
    supported(moment)
    from astrid.sdk.timeline.model import _MomentContext
    seconds = mo.resolve(moment, _MomentContext(tl, None))
    return int(round(mo.floor_frame(seconds, tl.fps) * tl.fps))


def resolve_ranges(tl) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """Resolve on an already compiled checkout, using its shared moment context.

    This is also the chapter-selector/sheet/check consumer path. Each output row
    has only `range`, `start_frame`, `end_frame`; unresolved edges are null.
    Findings address row positions, never invented persistent identities.
    """
    raw = _rows(tl.bundle)
    findings, resolved = [], []
    if not isinstance(raw, list):
        return [], [_finding("invalid-range", "ranges", "ranges must be a list")]
    for i, row in enumerate(raw):
        path = f"ranges[{i}]"
        out = dict(range=copy.deepcopy(row), start_frame=None, end_frame=None)
        resolved.append(out)
        error = _shape(row)
        if error:
            findings.append(_finding("invalid-range", path, error))
            continue
        for key, output in (("from", "start_frame"), ("to", "end_frame")):
            try:
                out[output] = _frame(tl, row[key] if row[key] is not None else "end")
            except (mo.MomentError, TimelineEditError, ValueError) as exc:
                findings.append(_finding("unresolved-anchor", f"{path}.{key}", str(exc)))
        lo, hi = out["start_frame"], out["end_frame"]
        if lo is not None and hi is not None and (lo < 0 or hi <= lo):
            findings.append(_finding("invalid-span", path, "range must be positive and half-open, starting at or after frame zero"))
    chapters = sorted((r["start_frame"], r["end_frame"], i) for i, r in enumerate(resolved)
                      if isinstance(r["range"], Mapping) and r["range"].get("kind") == "chapter"
                      and r["start_frame"] is not None and r["end_frame"] is not None
                      and r["end_frame"] > r["start_frame"])
    furthest = None
    for lo, hi, i in chapters:
        if furthest is not None and lo < furthest[0]:
            findings.append(_finding("chapter-overlap", f"ranges[{i}]", f"chapter overlaps ranges[{furthest[1]}]"))
        if furthest is None or hi > furthest[0]:
            furthest = (hi, i)
    return resolved, findings


def _migrate_legacy(tl) -> list[dict[str, str]]:
    """Migrate a complete legacy label table atomically; preserve opaque conflicts.

    An unanchored/missing/orphaned cut leaves the original legacy table intact.
    Partial conversion would lose the following boundary on a later retry. The
    canonical rows keep cut TimeRefs so picture and ordinary-word target kinds
    remain distinct without adding guessed fields to M8's stored schema.
    """
    film = intent._film(tl.bundle)
    if "chapters" not in film:
        return []
    labels = film["chapters"]
    if not isinstance(labels, list):
        return [_finding("legacy-range", "chapters", "legacy chapters must be a list; preserved")]
    if not labels:
        return []
    config = (tl.bundle.get("parent") or {}).get("config") or {}
    app = config.get("app")
    if "chapters" in config or (isinstance(app, Mapping) and "chapters" in app):
        # migrate_film leaves a source here only when the namespace or its
        # collision archive is opaque. Removing the canonical label would let
        # the preserved source become effective on the next load.
        return [_finding("legacy-conflict", "chapters", "opaque namespace collision retains a legacy source; preserved")]
    existing = film.get("ranges", [])
    if not isinstance(existing, list) or any(_shape(r) or r["kind"] != "beat" for r in existing):
        return [_finding("legacy-conflict", "chapters", "canonical ranges already exist; legacy labels preserved")]
    from astrid.core.timeline.compatibility import require_writable_parent
    try:
        require_writable_parent(tl.bundle.get("parent"))
        from astrid.sdk.timeline.compiler import _validate_dependencies
        _validate_dependencies(tl)
    except ValueError as exc:
        return [_finding("legacy-range", "chapters", str(exc))]
    except TimelineEditError as exc:
        return [_finding("legacy-range", "chapters", str(exc))]
    groups = tl.cut_index.groups(tl, include_orphans=True)
    by_id = {g["id"]: g for g in groups}
    positions = {g["id"]: i for i, g in enumerate(groups)}
    findings, candidates = [], []
    for i, row in enumerate(labels):
        path = f"chapters[{i}]"
        if not isinstance(row, Mapping) or set(row) != {"name", "from"} or not isinstance(row.get("name"), str) or not row["name"].strip():
            findings.append(_finding("legacy-range", path, "unsupported legacy label shape; preserved"))
            continue
        ref = row["from"]
        group = by_id.get(ref) if isinstance(ref, str) else None
        pic = group["picture"] if group else None
        on = intent.on(pic.data) if pic else None
        if group is None or group["orphan"] or pic is None or not on:
            findings.append(_finding("legacy-range", path, f"cut {ref!r} has no live anchored picture; legacy range preserved"))
            continue
        try:
            # Validate its declaration with the actual picture target kind. Do
            # not resolve/rewrite the document while doing a storage migration.
            target = tl._moment_time(on, pic, in_point=True)
            before = int(round(mo.floor_frame(group["start"], tl.fps) * tl.fps))
            after = int(round(target * tl.fps))
            if before != after:
                raise StoryRangeError(f"cut cache frame {before} differs from resolved picture frame {after}; resolve before migration")
        except (mo.MomentError, TimelineEditError, ValueError) as exc:
            findings.append(_finding("legacy-range", path, f"cut {ref!r}: {exc}; legacy range preserved"))
            continue
        candidates.append((positions[ref], dict(name=row["name"], kind="chapter", **{"from": ref, "to": None})))
    if findings:
        return findings
    candidates.sort(key=lambda item: item[0])
    if len({pos for pos, _ in candidates}) != len(candidates):
        return [_finding("legacy-range", "chapters", "duplicate chapter cut labels; preserved")]
    rows = []
    for _, row in candidates:
        if not rows or rows[-1]["name"] != row["name"]:
            rows.append(row)
    for current, following in zip(rows, rows[1:]):
        current["to"] = following["from"]
    for row in rows:
        try:
            lo = _frame(tl, row["from"])
            hi = _frame(tl, row["to"] if row["to"] is not None else "end")
            if lo < 0 or hi <= lo:
                raise StoryRangeError("legacy chapter boundaries collapse or precede frame zero")
        except (ValueError, TimelineEditError) as exc:
            return [_finding("legacy-range", "chapters", str(exc))]
    # Use the shared namespace collision rule. No app field may be overwritten.
    try:
        ns = intent._film_w(tl.bundle)
    except ValueError as exc:
        return [_finding("legacy-range", "chapters", str(exc))]
    # Namespace migration may have preserved a conflicting legacy source. The
    # effective namespaced value is the only table we just validated.
    ns["ranges"] = copy.deepcopy(existing) + rows
    ns["v"] = 2
    ns.pop("chapters", None)
    return []


def migrate_story_ranges(bundle: dict[str, Any]) -> list[dict[str, str]]:
    """Migrate only this detached working copy; return findings, never a journal."""
    from astrid.sdk.timeline_checkout import Checkout
    tl = Checkout(bundle)
    return _migrate_legacy(tl)


def _compiled_checkout(result):
    from astrid.sdk.timeline_checkout import Checkout
    tl = Checkout(result.bundle)
    # Retain the compiler's transient cut-span/orphan context too: a bare new
    # Checkout cannot reconstruct an unresolved cut's status from cached times.
    for key in ("_mcache", "_orphan_cuts"):
        if key in result._state:
            setattr(tl, key, result._state[key])
    return tl


def set_ranges(bundle: dict[str, Any], rows: Sequence[Mapping[str, Any]]) -> None:
    """Validated storage accessor, not a chapter-authoring command.

    Validate on a defensive compiler copy before replacing canonical intent.
    Opaque namespace containers and unresolved/overlapping ranges are refused.
    """
    from astrid.sdk.timeline.compiler import TimingInputs, compile_timing
    if not isinstance(rows, (list, tuple)):
        raise StoryRangeError("ranges must be a list of story rows")
    value = copy.deepcopy(list(rows))
    for row in value:
        error = _shape(row)
        if error:
            raise StoryRangeError(error)
    candidate = copy.deepcopy(bundle)
    from astrid.core.timeline.compatibility import require_writable_parent
    require_writable_parent(candidate.get("parent"))
    intent._film_w(candidate)["ranges"] = value
    intent._film_w(candidate)["v"] = 2
    compiled = compile_timing(TimingInputs(candidate))
    _, findings = resolve_ranges(_compiled_checkout(compiled))
    if findings:
        raise StoryRangeError("; ".join(f"{f['path']}: {f['message']}" for f in findings))
    intent._film_w(bundle)["ranges"] = value
    intent._film_w(bundle)["v"] = 2


def story_overview(bundle: Mapping[str, Any], *, revision_id: str) -> dict[str, Any]:
    """Read a pinned bundle without mutation, saves, clients or journal effects.

    Hosts should call `open_story_overview` for an authenticated pinned read.
    Direct callers supply the host-owned immutable bundle, not an editable draft.
    The content digest binds the projection to this exact detached input too.
    """
    from astrid.core.timeline.cuts import bundle_fps
    from astrid.sdk.timeline.compiler import TimingInputs, compile_timing
    actual = (bundle.get("base_parent") or {}).get("revision_id")
    if not isinstance(revision_id, str) or not revision_id or actual != revision_id:
        raise StoryRangeError(f"story projection requires pinned revision {revision_id!r}; bundle is {actual!r}")
    owned = copy.deepcopy(dict(bundle))
    digest = hashlib.sha256(json.dumps(owned, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
    result = dict(project_id=owned.get("project_id"), timeline_id=owned.get("timeline_id"),
                  revision_id=revision_id, content_digest=digest, fps=bundle_fps(owned),
                  ranges=[], legacy_chapters=copy.deepcopy(intent._film(owned).get("chapters", [])), findings=[])
    try:
        compiled = compile_timing(TimingInputs(owned), commands=())
    except (TimelineEditError, mo.MomentError, ValueError) as exc:
        raw = _rows(owned)
        if isinstance(raw, list):
            result["ranges"] = [dict(range=copy.deepcopy(row), start_frame=None, end_frame=None) for row in raw]
        result["findings"] = [_finding("timing-unresolved", "ranges", str(exc))]
        return result
    tl = _compiled_checkout(compiled)
    result["ranges"], findings = resolve_ranges(tl)
    result["legacy_chapters"] = copy.deepcopy(intent._film(tl.bundle).get("chapters", []))
    result["findings"] = _migrate_legacy(tl) + findings
    # The compiler's preserved orphan diagnostics are unresolved declarations,
    # not evidence that its cached positions are freshly resolved.
    diagnostics = compiled._state.get("notes", [])
    result["findings"] += [_finding("timing-finding", "timing", str(message)) for message in diagnostics]
    return result


def open_story_overview(project: str, timeline: str, *, revision_id: str, client: Any = None) -> dict[str, Any]:
    """Host boundary: fetch one exact revision, verify identity, then project."""
    from astrid.sdk.timeline.store import fetch_bundle
    if not isinstance(revision_id, str) or not revision_id:
        raise StoryRangeError("an exact revision_id is required for a story view")
    bundle = fetch_bundle(project, timeline, revision_id=revision_id, client=client)
    if bundle.get("project_id") != project or bundle.get("timeline_id") != timeline:
        raise StoryRangeError("story bundle project/timeline identity does not match the host request")
    return story_overview(bundle, revision_id=revision_id)
