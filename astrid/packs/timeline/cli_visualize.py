"""``timelines visualize``: working copy, addresses, presets, compare, plan and the human summary.

Kept beside ``cli.py`` (like ``cli_sweep`` and ``cli_verify``) so the verb stays one readable module. The
SDK call and the admitted views are Runtime-owned; this module resolves what the caller *meant* (an
address, a preset, the working copy) into the options that call takes, and prints the result as words.
"""
from __future__ import annotations

import argparse
import copy
import json
import re
import shlex
import sys
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from astrid.pack_api import print_result

PRESETS = ("overview", "scan", "motion", "beat", "frame", "cut", "compare")


def _cli():
    from astrid.packs.timeline import cli

    return cli


def _preset_max() -> dict[str, float]:
    """The window presets' widest window (motion/window.py), for the hint after a visualize."""
    try:
        from astrid.packs.rendering.actions.timeline_visualize.motion.window import PRESETS

        return {k: float(v["max_window_s"]) for k, v in PRESETS.items() if k in ("motion", "beat", "scan") and v.get("max_window_s")}
    except Exception:  # noqa: BLE001 - the hint is optional
        return {}


def _short_rev(revision: Any) -> str:
    from astrid.sdk.timeline.revisions import short_revision

    return short_revision(revision)


def _clips_changed(n: int) -> str:
    """The one count every verb prints for a working copy: clips added, removed, moved or edited."""
    return f"{n} clip{'s' if n != 1 else ''} changed"


def _visualize_working_copy(parsed: argparse.Namespace) -> dict[str, Any] | None:
    """Metadata for the explicitly selected draft, already pinned by source selection."""
    checkout = getattr(parsed, "_draft_checkout", None)
    if checkout is None:
        return None
    changes = list(checkout.edits().get("changes") or [])
    return {
        "name": parsed.draft,
        "draft": str(getattr(parsed, "_draft_path", "")),
        "base_revision": checkout.base_revision,
        "edits": len(changes),
        "changed_cuts": [int(n) for n in checkout.check().changed_cuts],
    }


def _select_visualize_source(parsed: argparse.Namespace) -> dict[str, Any] | None:
    """Select one immutable revision or one named working copy for this inspection."""
    from astrid.sdk.timeline_checkout import Checkout, find_draft

    draft_name = getattr(parsed, "draft", None)
    revision_id = getattr(parsed, "revision_id", None)
    if draft_name and (getattr(parsed, "published", False) or revision_id):
        raise ValueError("--draft cannot be combined with --published or --revision-id")
    if draft_name:
        ref = getattr(parsed, "timeline_slug", None) or getattr(parsed, "timeline_ref", None)
        try:
            path = find_draft(parsed.project, ref, draft_name, client=parsed.client)
        except Exception as exc:  # explicit selection must not fall back to published current
            raise ValueError(f"could not read working copy {draft_name!r}: {exc}") from exc
        if path is None:
            raise ValueError(f"working copy {draft_name!r} does not exist")
        checkout = Checkout.load(path)
        parsed._draft_checkout = checkout
        parsed._draft_path = str(path)
        parsed._source_revision_id = checkout.base_revision
        return _visualize_working_copy(parsed)

    if getattr(parsed, "published", False) and revision_id:
        raise ValueError("--published cannot be combined with --revision-id")
    if revision_id:
        revision_id = _full_revision_id(parsed, str(revision_id))
        parsed.revision_id = revision_id
        parsed._revision_explicit = True
        parsed._source_revision_id = revision_id
        return None

    ref = getattr(parsed, "timeline_slug", None) or getattr(parsed, "timeline_ref", None)
    opener = getattr(parsed.client.timelines, "open_bundle", None)
    if not callable(opener):
        raise ValueError("timeline visualization needs a client that can pin the published revision")
    opened = opener(parsed.project, ref)
    data = getattr(opened, "data", None)
    bundle = data.get("bundle") if isinstance(data, Mapping) else None
    parent = bundle.get("base_parent") if isinstance(bundle, Mapping) else None
    pinned = parent.get("revision_id") if isinstance(parent, Mapping) else None
    if not getattr(opened, "ok", False) or not isinstance(pinned, str) or not pinned:
        raise ValueError("could not pin the published timeline revision for visualization")
    parsed.revision_id = pinned
    parsed._source_revision_id = pinned
    parsed._bundle = bundle
    return None


def _full_revision_id(parsed: argparse.Namespace, revision_id: str) -> str:
    """``--revision-id 08dee21c`` (the short id pages and history print) → the full id; full ids pass through."""
    from astrid.sdk.timeline.revisions import _ID_PREFIXES

    if (revision_id.startswith(_ID_PREFIXES) or len(revision_id) >= 40 or parsed.client is None
            or not re.fullmatch(r"[0-9a-f]{6,39}", revision_id)):
        return revision_id  # a full or named id: the Runtime resolves it
    try:
        from astrid.packs.timeline.cli_history import _store
        from astrid.sdk.timeline.revisions import resolve_revision

        ref = getattr(parsed, "timeline_slug", None) or getattr(parsed, "timeline_ref", None)
        return resolve_revision(parsed.project, ref, revision_id, store=_store(parsed)) or revision_id
    except Exception as exc:  # noqa: BLE001 - say what the short id could not match
        raise ValueError(f"--revision-id {revision_id}: {exc}") from None


def _changed_cuts_line(cuts: list[int], *, limit: int = 12) -> str:
    """One line: which cuts the default selection shows, paged explicitly (never silently truncated)."""
    if not cuts:
        return "the working copy changes no cut · --every-cut for all cuts"
    if len(cuts) <= limit:
        noun = "cut" if len(cuts) == 1 else "cuts"
        return f"showing the {len(cuts)} {noun} you changed ({', '.join(map(str, cuts))}) · --every-cut for all cuts"
    return (
        f"showing 1–{limit} of {len(cuts)} cuts you changed ({', '.join(map(str, cuts[:limit]))}) · "
        f"next: --cut {cuts[limit]} · --every-cut for all cuts"
    )


# Flags that only mean something to the paired filmstrip (input lanes, pages).
_FILMSTRIP_ONLY = ("sample", "show", "hide", "track", "page_size", "include_media", "detail", "render_run",
                   "occurrence", "clip", "asset", "shot", "context", "neighbors", "include_cuts")


def _resolve_visualize_addresses(parsed: argparse.Namespace) -> str | None:
    """``--at``, ``--range`` and ``--highlight`` take the same addresses as every other verb
    (``'"Building" in v27'``, ``c30``, ``c41.mink``, ``93.5``), read from the selected source.
    Ambiguity lists the choices; the line printed says which one was used."""
    from astrid.sdk.timeline_address import AddressError, resolve
    from astrid.sdk.timeline_checkout import Checkout

    def needs(name: str, plain: str) -> bool:
        value = getattr(parsed, name, None)
        return isinstance(value, str) and bool(value.strip()) and not re.fullmatch(plain, value)

    if not (needs("at", r"\s*@?[\d.:]+s?\s*") or needs("range", r"\s*[\d.:]+\s*\.\.\s*[\d.:]+\s*")
            or needs("highlight", r"") or needs("clip", r"[^.]*-\d\d-am-[a-z-]+")
            or (needs("cut", r"\s*\d+\s*") and re.fullmatch(r"\s*c\d+[a-z]?\s*", str(parsed.cut).lower()))):
        return None  # plain seconds and cut numbers need no lookup
    ref = getattr(parsed, "timeline_slug", None) or getattr(parsed, "timeline_ref", None)
    tl = None
    selected_draft = getattr(parsed, "_draft_checkout", None)
    if selected_draft is not None:
        tl = selected_draft
    if tl is None:
        opener = getattr(parsed.client.timelines, "open_bundle", None)
        if not callable(opener):
            raise ValueError("an address needs a client that can open the timeline")
        opened = opener(parsed.project, ref, revision_id=getattr(parsed, "revision_id", None))
        if not opened.ok or not isinstance(opened.data, Mapping):
            raise ValueError(f"cannot open the timeline to resolve the address: {opened.error}")
        tl = Checkout(dict(opened.data["bundle"]))
    parsed._named = tl  # findings name layers by address (c30b.icon, not "ICON")
    notes = []
    try:
        if getattr(parsed, "clip", None) and not any(c.id == parsed.clip for c in tl.clips()):
            target = resolve(tl, parsed.clip, prefer="thing")
            if target.kind != "clip":
                raise ValueError(f"--clip names a layer (c15.robot); {parsed.clip!r} is a {target.kind}")
            notes.append(f"--clip {target.address}")
            parsed.clip = target.clip.id
        if getattr(parsed, "highlight", None):
            target = resolve(tl, parsed.highlight, prefer="thing")
            if target.kind != "clip":
                raise ValueError(f"--highlight names a layer (c41.tool-16); {parsed.highlight!r} is a {target.kind}")
            parsed.highlight = target.clip.id
            notes.append(f"--highlight {target.address}")
            if not any(getattr(parsed, n, None) not in (None, "") for n in ("at", "range", "cut", "frame")):
                parsed.at = f"{min(target.end - 1 / tl.fps, target.start + 0.25):.3f}"
                notes.append(f"at {float(parsed.at):.2f} s (inside it)")
        cut = getattr(parsed, "cut", None)
        if isinstance(cut, str) and re.fullmatch(r"c\d+[a-z]?", cut.strip().lower()):
            from astrid.sdk.timeline_address import cut_ids_by_ordinal

            by_id = {cid: n for n, cid in cut_ids_by_ordinal(tl).items()}
            if cut.strip().lower() not in by_id:
                raise ValueError(f"no cut {cut}; cuts are {_id_span(list(tl._cut_spans()))}")
            parsed.cut = str(by_id[cut.strip().lower()])
            notes.append(f"--cut {cut.strip().lower()}")
        at = getattr(parsed, "at", None)
        if isinstance(at, str) and at.strip() and not re.fullmatch(r"\s*@?[\d.:]+s?\s*", at):
            target = resolve(tl, at, prefer="time")
            limit = _preset_max().get(str(getattr(parsed, "preset", None) or "scan"), 30.0)
            if target.kind == "clip" and not getattr(parsed, "range", None) and target.end - target.start <= limit + 1e-6:
                # a layer: show its whole span (where it enters, holds and leaves), not one frame at its entrance
                parsed.at, parsed.range = None, f"{target.start:.3f}..{target.end:.3f}"
                notes.append(f"--at {at!r}: the layer {target.address}, {target.start:.2f}–{target.end:.2f} s")
            else:
                parsed.at = f"{target.start:.3f}"
                notes.append(f"--at {at!r}: {target.address} = {target.start:.2f} s")
                entering = [c.address for c in tl.clips() if not c.is_audio and abs(c.start - target.start) < 0.5 / tl.fps
                            and not tl._is_picture(c)]
                if entering:  # its first frame can be blank (a stamp flickers in, a slide starts off its mask)
                    notes.append(f"{', '.join(entering[:3])} enter{'s' if len(entering) == 1 else ''} on this frame "
                                 f"(an entrance can start invisible; --at '{at} +2f' shows it in)")
        rng = getattr(parsed, "range", None)
        if isinstance(rng, str) and rng.strip() and not re.fullmatch(r"\s*[\d.:]+\s*\.\.\s*[\d.:]+\s*", rng):
            target = resolve(tl, rng, prefer="time")
            end = target.end if target.end > target.start else target.start + 1.0 / tl.fps
            parsed.range = f"{target.start:.3f}..{end:.3f}"
            notes.append(f"--range {rng!r}: {target.address} = {target.start:.2f}–{end:.2f} s")
    except AddressError as exc:
        raise ValueError(str(exc)) from None
    return " · ".join(notes) or None


def _id_span(ids: list[str]) -> str:
    """``c01…c42 (+ c30b)``: the valid cut ids in one short phrase."""
    plain = [c for c in ids if re.fullmatch(r"c\d+", c)]
    extra = [c for c in ids if c not in plain]
    head = f"{plain[0]}…{plain[-1]}" if len(plain) > 1 else ", ".join(plain)
    return head + (f" (+ {', '.join(extra)})" if extra else "")


def _resolve_view(parsed: argparse.Namespace) -> None:
    """Fill ``parsed.view``/``parsed.preset`` from what was asked: presets first, then the window given.

    No window → overview; ``--range`` → scan; ``--at``/``--frame`` → frame; ``--cut`` → cut;
    filmstrip-only flags (``--show``, ``--sample`` …) keep the paired filmstrip.
    """
    preset = getattr(parsed, "preset", None)
    if preset == "overview":
        parsed.view = "contact"
        return
    if preset and parsed.view in (None, "motion"):
        parsed.view = "motion"
        return
    if preset:
        return  # an explicitly chosen view keeps its place; the preset is forwarded as an option
    if parsed.view == "motion" and not getattr(parsed, "cut", None):
        parsed.preset = "frame" if (parsed.at is not None or parsed.frame is not None) and not parsed.range else "scan"
        return
    if parsed.view == "motion":
        parsed.preset = "cut"
        return
    if parsed.view is not None:
        return
    if any(getattr(parsed, name, None) not in (None, "", [], False) for name in _FILMSTRIP_ONLY):
        parsed.view = "filmstrip"
    elif getattr(parsed, "cut", None):
        parsed.view, parsed.preset = "motion", "cut"
    elif parsed.range:
        parsed.view, parsed.preset = "motion", "scan"
    elif parsed.at is not None or parsed.frame is not None:
        parsed.view, parsed.preset = "motion", ("motion" if getattr(parsed, "window_s", None) else "frame")
    elif parsed.every is not None or parsed.every_frames is not None:
        parsed.view = "filmstrip"
    else:
        parsed.view = "contact"


def _cmd_visualize_diff(parsed: argparse.Namespace) -> int:
    """Before/after tiles for the cuts an edit changed, plus the SCOPE condition (client-side)."""
    from astrid.packs.rendering.actions.timeline_visualize.motion.diffview import (
        compare,
        compose_page,
        frames_by_cut,
    )

    old = getattr(parsed, "from_revision", None)
    if not old:
        print("error validation_error: --view diff needs --from <revision id> (timelines history / an edit's "
              ".publication.json old_head)", file=sys.stderr)
        return 2
    ref = parsed.timeline_slug or parsed.timeline_ref
    opener = getattr(parsed.client.timelines, "open_bundle", None)
    if not callable(opener):
        print("error unavailable: this client cannot open a timeline bundle", file=sys.stderr)
        return 1
    before = opener(parsed.project, ref, revision_id=old)
    after = opener(parsed.project, ref, revision_id=getattr(parsed, "to_revision", None))
    for opened in (before, after):
        if not opened.ok or not isinstance(opened.data, Mapping):
            return print_result(opened, as_json=parsed.json)
    new = str(after.data.get("revision_id"))
    try:
        edited = [int(n) for n in str(getattr(parsed, "edited", None) or "").split(",") if n.strip()]
    except ValueError:
        print("error validation_error: --edited takes cut numbers, e.g. 4,5", file=sys.stderr)
        return 2
    report = compare(before.data["bundle"], after.data["bundle"], edited=edited)
    page = None
    notes = []
    window_given = any(getattr(parsed, name, None) not in (None, "") for name in ("range", "at", "cut"))
    if getattr(parsed, "preset", None) == "compare" and window_given and not getattr(parsed, "no_capture", False):
        from astrid.packs.rendering.actions.timeline_visualize.motion.diffview import (
            compose_compare,
            window_frames_of,
        )

        frames, roots = {}, {}
        options = {"view": "motion", "preset": "scan", "formats": ["png"]}
        for name in ("range", "at", "cut", "every", "every_frames"):
            if getattr(parsed, name, None) not in (None, ""):
                options[name] = getattr(parsed, name)
        for side, revision in (("before", old), ("after", new)):
            try:
                result = parsed.client.timelines.visualize(parsed.project, ref, mode="auto", revision_id=revision,
                                                           options={**options, "revision_id": revision})
                outputs = result.outputs if hasattr(result, "outputs") else (result.data or {})
                if not getattr(result, "ok", False) or not outputs.get("pack_root"):
                    raise RuntimeError(str(getattr(result, "error", None) or "no pack_root in the result"))
                roots[side] = Path(outputs["pack_root"])
                frames[side] = window_frames_of(roots[side])
            except Exception as exc:  # noqa: BLE001
                notes.append(f"{side} frames not captured: {exc}")
        if len(frames) == 2:
            out = roots["after"].parent / f"compare-{old[-8:]}-{new[-8:]}.png"
            page = compose_compare(frames["before"], frames["after"], out,
                                   title=f"{ref} · {old[-12:]} (top) vs {new[-12:]} (bottom)")
    if page is None and (report["changed_after"] or report["changed_before"]) and not getattr(parsed, "no_capture", False):
        captures, roots = {}, {}
        for side, revision, numbers, opened in (("before", old, report["changed_before"], before),
                                                ("after", new, report["changed_after"], after)):
            if not numbers:
                continue
            try:
                result = parsed.client.timelines.visualize(
                    parsed.project, ref, mode="auto", revision_id=revision,
                    options={"view": "contact", "cuts": ",".join(map(str, numbers[:12])), "resolution": "480x270",
                             "formats": ["png"], "revision_id": revision},
                )
                outputs = result.outputs if hasattr(result, "outputs") else (result.data or {})
                if not getattr(result, "ok", False) or not isinstance(outputs, Mapping) or not outputs.get("pack_root"):
                    raise RuntimeError(str(getattr(result, "error", None) or "no pack_root in the result"))
                roots[side] = Path(outputs["pack_root"])
                captures[side] = frames_by_cut(roots[side], opened.data["bundle"])
            except Exception as exc:  # noqa: BLE001 - the data diff stands without pictures
                notes.append(f"{side} tiles not captured: {exc}")
        if captures:
            out = (roots.get("after") or roots["before"]).parent / f"diff-{old[-8:]}-{new[-8:]}.png"
            page = compose_page(report, captures.get("before", {}), captures.get("after", {}), out,
                                title=f"{ref} · {old[-12:]} → {new[-12:]} · changed cuts")
    if parsed.json:
        print(json.dumps({"ok": True, "data": {"from": old, "to": new, "page": str(page) if page else None,
                                                "findings": [f.as_dict() for f in report["findings"]],
                                                "notes": notes}, "error": None}, indent=2))
        return 0
    lines = [f"diff page: {page}" if page else "diff page: (none: no changed cuts, --no-capture, or capture failed)",
             f"  {report['before_cuts']} → {report['after_cuts']} cuts · changed {len(report['changed_after'])}, "
             f"removed {sum(1 for r in report['rows'] if r['status'] == 'removed')}"]
    lines += [f"  {note}" for note in notes]
    lines += [f.line() for f in report["findings"]]
    scope = [f for f in report["findings"] if f.code == "SCOPE"]
    lines.append("SCOPE ok: nothing outside --edited changed" if edited and not scope else
                 ("" if edited else "tip: --edited 4,5 turns any change outside those cuts into a SCOPE warning"))
    print("\n".join(line for line in lines if line))
    return 0


def _read_beats(path: str) -> str:
    """A cue's beats.json reduced to what the sync layer and lint read (cue seconds)."""
    data = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    if isinstance(data, list):
        data = {"beats": data}
    if not isinstance(data, Mapping):
        raise ValueError("expected a beats.json object with beats/downbeats/hits")
    compact = {
        "beats": [round(float(t), 4) for t in data.get("beats") or [] if isinstance(t, (int, float))],
        "downbeats": [round(float(t), 4) for t in data.get("downbeats") or [] if isinstance(t, (int, float))],
        "hits": [
            {"t": round(float(hit["t"]), 4), "kind": str(hit.get("kind") or "hit")}
            for hit in data.get("hits") or [] if isinstance(hit, Mapping) and isinstance(hit.get("t"), (int, float))
        ],
    }
    return json.dumps(compact, separators=(",", ":"))


def _render_review_beside(page: Any) -> dict[str, Any] | None:
    """render-review.json written next to a contact page of a render (loudness, silences), if any."""
    if not page:
        return None
    try:
        from pathlib import Path as _Path

        path = _Path(str(page)).parent / "render-review.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None
    except (OSError, ValueError):
        return None


def _seconds(value: Any) -> str:
    return f"{float(value):.0f} s" if isinstance(value, (int, float)) and not isinstance(value, bool) else "?"


def _visualization_revision_lines(outputs: Mapping[str, Any]) -> list[str]:
    """Describe the revision the returned view actually consumed.

    The request is only a selector.  Native views prove identity with their
    Runtime inspection; rendered views prove it with the admission-owned
    snapshot label.  Keep a missing label visible instead of guessing from
    the requested revision or current head.
    """
    inspection = outputs.get("inspection")
    if isinstance(inspection, Mapping):
        actual = inspection.get("revision_id")
        if isinstance(actual, str) and actual:
            lines = [f"  committed revision {actual}"]
            head = inspection.get("head_revision_id") or outputs.get("head_revision_id")
            if isinstance(head, str) and head:
                lines.append(f"  known head {head}")
            return lines

    snapshot_label = outputs.get("snapshot_label")
    if isinstance(snapshot_label, Mapping):
        label = snapshot_label.get("label")
        if isinstance(label, str) and label:
            return [f"  {label}"]
        if snapshot_label.get("status") == "candidate":
            return ["  unsaved candidate (committed base unavailable)"]
        return ["  revision unavailable"]
    source_identity = outputs.get("source_identity")
    if isinstance(source_identity, Mapping) and source_identity.get("kind") == "revision":
        revision = source_identity.get("revision_id")
        if isinstance(revision, str) and revision:
            return [f"  committed revision {revision}"]
    return ["  revision unavailable"]


def _visualization_summary(outputs: Mapping[str, Any], *, parsed: argparse.Namespace, inputs: Mapping[str, Any]) -> str:
    """The human result: where the picture is, what it holds, how long it took, what to run next."""
    view = str(inputs.get("view") or "filmstrip")
    pages = [str(page) for page in outputs.get("pages") or [] if page]
    primary = outputs.get("primary_page") or (pages[0] if pages else None) or outputs.get("png")
    timing = outputs.get("timing") if isinstance(outputs.get("timing"), Mapping) else {}
    label = {"contact": "contact sheet", "motion": "motion sheet" if inputs.get("preset") == "cut" else
             f"{inputs.get('preset')} view", "filmstrip": "filmstrip"}.get(view, view)
    if primary:
        lines = [f"{label}: {primary}"]
    elif outputs.get("evidence_kind") == "declared_inputs":
        lines = [f"{label}: no picture produced (inputs mode is render-free; it lists declared inputs)"]
    else:
        queued = timing.get("queued_s") if isinstance(timing, Mapping) else None
        why = (f"; it waited {float(queued):.0f} s behind other Runtime work" if isinstance(queued, (int, float))
               and queued >= 15 else "")
        lines = [f"{label}: no picture produced (no PNG page was returned{why})"]
    for page in pages:
        if page != primary:
            lines.append(f"  also: {page}")
    artifacts = outputs.get("artifacts")
    if isinstance(artifacts, Mapping):
        for kind in ("png", "md"):
            artifact = artifacts.get(kind)
            if isinstance(artifact, Mapping) and artifact.get("filename"):
                artifact_label = "Markdown" if kind == "md" else kind.upper()
                lines.append(f"  {artifact_label} artifact: {artifact['filename']}")
    exported = outputs.get("exported")
    if isinstance(exported, Mapping):
        for page in exported.get("pages") or []:
            lines.append(f"  exported PNG: {page}")
        if exported.get("markdown"):
            lines.append(f"  exported Markdown: {exported['markdown']}")
        frames = [str(frame) for frame in exported.get("frames") or [] if frame]
        if len(frames) == 1:
            clean_composed = (
                getattr(parsed, "mode", None) == "composed"
                and not inputs.get("layers")
                and not inputs.get("highlight")
            )
            label = "clean composed frame (no visualization overlays)" if clean_composed else "raw frame image"
            lines.append(f"  {label}: {frames[0]}")
    if outputs.get("preview"):
        lines.append(f"  preview for humans (animated, sampled frames at real timing): {outputs['preview']}")
    frames = timing.get("frames")
    resolution = timing.get("resolution") or inputs.get("resolution")
    if isinstance(resolution, (list, tuple)) and len(resolution) == 2:
        resolution = f"{resolution[0]}x{resolution[1]}"
    tiles = timing.get("tiles") or frames
    window_info = outputs.get("window") if isinstance(outputs.get("window"), Mapping) else {}
    preset = inputs.get("preset")
    what = {
        "contact": f"{tiles} tiles, one per picture cut (cut N = the cut number timelines show prints)",
        "motion": (f"cut {inputs.get('cut')}: {frames} frames, dense after each entrance" if preset == "cut" else
                   f"{preset} {float(window_info.get('start', 0)):.2f}–{float(window_info.get('end', 0)):.2f} s: "
                   f"{frames} frames on one page"
                   + (f" (window {float(window_info.get('end', 0)) - float(window_info.get('start', 0)):.2f} s: "
                      f"--window S widens it, up to {_preset_max()[str(preset)]:g} s)" if str(preset) in _preset_max() else "")),
    }.get(
        view,
        "declared inputs inspected; no frames captured"
        if outputs.get("evidence_kind") == "declared_inputs" else f"{frames} frames",
    )
    lines.extend(_visualization_revision_lines(outputs))
    lines.append(f"  {what}" + (f" · frames {resolution}" if resolution else ""))
    if timing.get("capture_s") is not None:
        lines.append(
            f"  wall {_seconds(timing.get('wall_s'))} = queued {_seconds(timing.get('queued_s'))}"
            f" + capture {_seconds(timing.get('capture_s'))} + compose {_seconds(timing.get('compose_s'))}"
        )
    else:
        lines.append(f"  wall {_seconds(timing.get('wall_s'))} (executor timing unavailable: the host predates timing.json)")
    review = _render_review_beside(primary)
    if review:
        lufs, lra, peak = review.get("integrated_lufs"), review.get("lra_lu"), review.get("true_peak_dbtp")
        if lufs is not None:
            over = " (over -1 dBTP)" if isinstance(peak, (int, float)) and peak > -1.0 else ""
            lines.append(f"  loudness {lufs:.1f} LUFS integrated · LRA {lra:.1f} LU · true peak {peak:+.1f} dBTP{over}"
                         f" · {len(review.get('silences') or [])} silent span(s) in the file · sampled every {review.get('every_s')} s")
    findings = [str(line) for line in outputs.get("findings") or []]
    named = getattr(parsed, "_named", None)
    if findings and named is not None:
        from astrid.sdk.timeline_address import name_things

        lo = float(window_info.get("start", 0) or 0)
        hi = float(window_info.get("end", 0) or named.duration)
        from astrid.sdk.timeline_address import ordinals_to_ids

        findings = [ordinals_to_ids(name_things(line, named, lo, hi), named) for line in findings]
    if findings:
        lines.append("findings:")
        lines.extend(f"  {line}" for line in findings[:14])
        if len(findings) > 14:
            lines.append(f"  … {len(findings) - 14} more in findings.txt next to the page")
    project = str(parsed.project or "<project>")
    timeline = str(inputs.get("timeline_slug") or "<timeline>")
    command = _cli()._astrid_cli_command()
    base = [*command, "timelines", "visualize", timeline, "--project", project]
    if inputs.get("revision_id") and _revision_pinned_by_caller(parsed, inputs):
        # only when the caller pinned one: the default already draws the published revision
        base += ["--revision-id", _short_rev(inputs["revision_id"])]
    lint = [*command, "timelines", "lint", timeline, "--project", project]
    show = [*command, "timelines", "show", timeline, "--project", project]
    beats = ["--beats", str(parsed.beats)] if getattr(parsed, "beats", None) else []
    window = outputs.get("window") if isinstance(outputs.get("window"), Mapping) else None
    if outputs.get("evidence_kind") == "declared_inputs" and not primary:
        input_command = [
            *command, "timelines", "visualize", timeline, "--project", project,
            "--mode", "inputs", "--format", "md", "--out", "artifacts",
        ]
        composed_command = [
            *command, "timelines", "visualize", timeline, "--project", project,
            "--mode", "composed", "--format", "png",
        ]
        for name, flag in (
            ("range", "--range"), ("at", "--at"), ("frame", "--frame"),
            ("shot", "--shot"), ("clip", "--clip"), ("occurrence", "--occurrence"),
            ("asset", "--asset"), ("render_run", "--render-run"),
            ("revision_id", "--revision-id"),
        ):
            value = inputs.get(name)
            if value not in (None, ""):
                composed_command += [flag, str(value)]
        nexts = [
            ("get a picture", composed_command),
            ("export declared inputs", input_command),
        ]
    elif view == "motion" and window and inputs.get("preset") != "cut":
        nexts = _window_navigation(window, base)
    elif view == "motion":
        cut = str(inputs.get("cut") or "N")
        following = str(int(cut) + 1) if cut.isdigit() else "N"
        nexts = [
            ("next cut", base + ["--cut", following] + beats),
            ("lint the whole edit", lint + beats),
            ("whole film again", base + ["--preset", "overview"]),
        ]
        if window and window.get("busiest") is not None:
            t = float(window["busiest"])
            nexts.insert(1, (f"biggest frames at the busiest moment ({t:.2f} s)",
                             base + ["--preset", "motion", "--range", f"{max(0.0, t - 1.5):.2f}..{t + 1.5:.2f}"]))
    else:
        nexts = [
            ("motion of one cut", base + ["--view", "motion", "--cut", "N"] + beats),
            ("lint composition + timing", lint + beats),
            ("cut table (numbers, words)", show),
        ]
    lines.append("next:")
    width = max(len(name) for name, _argv in nexts)
    lines.extend(f"  {name:<{width}}  {shlex.join(argv)}" for name, argv in nexts)
    lines.append("(--json prints the full SDK envelope; --list-layers lists the views you can add with --layer)")
    return "\n".join(lines)


def _revision_pinned_by_caller(parsed: Any, inputs: Mapping[str, Any]) -> bool:
    """True when ``--revision-id`` was given (or no source pinning ran): the next commands must keep it."""
    explicit = getattr(parsed, "_revision_explicit", None)
    if explicit is not None:
        return bool(explicit)
    return not hasattr(parsed, "_source_revision_id")


def _visualize_plan(parsed: argparse.Namespace, inputs: Mapping[str, Any]) -> int:
    """``--plan``: resolve the view, window and frames without capturing (reads the timeline only)."""
    import math

    from astrid.packs.rendering.actions.timeline_visualize.filmstrip_cards import plan_filmstrip
    from astrid.packs.rendering.actions.timeline_visualize.filmstrip_options import (
        filmstrip_options,
    )
    from astrid.packs.rendering.actions.timeline_visualize.motion.window import (
        fit_columns,
        preset_settings,
    )
    from astrid.sdk.timeline_filmstrip import prepare_filmstrip

    values = dict(inputs)
    values["composed_capture"] = not bool(values.get("render_run"))
    try:
        authority = prepare_filmstrip({**values, "view": "filmstrip"}, project=parsed.project, client=parsed.client)
        options = filmstrip_options(values)
        snapshot = authority["capture_snapshot"] if values["composed_capture"] else authority["filmstrip_snapshot"]
        index = plan_filmstrip(snapshot, options)
    except Exception as exc:  # noqa: BLE001 - a plan reports what would fail
        print(f"plan: would fail: {exc}")
        return 2
    frames = len(index["cards"])
    view = options["view"]
    lines = [f"plan: view {view}" + (f", preset {options.get('preset')}" if view == "motion" else "")]
    if view == "motion":
        motion = index.get("motion") or {}
        window = motion.get("window") or [0, 0]
        settings = preset_settings(options.get("preset"))
        size = options.get("resolution") or list(settings["size"])
        columns, shown = fit_columns(frames, int(options.get("columns") or settings["columns"]),
                                     int(size[0]) if options.get("size_explicit") else int(settings["tile_w"]),
                                     int(size[1]))
        lines.append(f"  window {float(window[0]):.2f}–{float(window[1]):.2f} s · cuts {motion.get('cuts_in')}")
        lines.append(f"  {frames} frames captured at {size[0]}x{size[1]}, shown {shown} px wide, {columns} per row, "
                     f"{math.ceil(frames / columns)} rows on ONE page")
    elif view == "contact":
        lines.append(f"  {frames} tiles at {options['resolution'][0]}x{options['resolution'][1]} on one page")
    else:
        lines.append(f"  {frames} frames on {math.ceil(frames / max(1, int(options.get('page_size') or 50)))} page(s)")
    lines.append(f"  expect ~{max(8, round(5 + frames * 0.3))} s of capture when the host is free (frames are cached "
                 "after the first run)")
    lines.append("  run it: the same command without --plan (the run prints next: earlier/later/zoom/other preset)")
    print("\n".join(lines))
    return 0


def _window_navigation(window: Mapping[str, Any], base: list[str]) -> list[tuple[str, list[str]]]:
    """Neighbour windows as copyable commands: earlier, later, zoom in/out, another preset, the cut."""
    preset = str(window.get("preset") or "scan")

    def view(name: str, low: float, high: float) -> list[str]:
        if name == "motion" and high - low > 3.0:
            name = "scan"
        return base + ["--preset", name, "--range", f"{low:.2f}..{high:.2f}"]

    rows: list[tuple[str, list[str]]] = []
    start, end = float(window["start"]), float(window["end"])
    if preset == "frame":
        # one frame: step a frame at a time (a time window of one frame), never "zoom" (that reads as a picture zoom)
        if window.get("earlier"):
            a, _b = window["earlier"]
            rows.append((f"previous frame ({a:.2f} s)", base + ["--at", f"{a:.2f}"]))
        if window.get("later"):
            a, _b = window["later"]
            rows.append((f"next frame ({a:.2f} s)", base + ["--at", f"{a:.2f}"]))
    else:
        if window.get("earlier"):
            a, b = window["earlier"]
            rows.append((f"earlier {a:.2f}–{b:.2f} s", view(preset, a, b)))
        if window.get("later"):
            a, b = window["later"]
            rows.append((f"later {a:.2f}–{b:.2f} s", view(preset, a, b)))
        if window.get("zoom_in"):
            a, b = window["zoom_in"]
            where = (f"around the busiest moment {window['busiest']:.2f} s" if window.get("busiest") is not None
                     else "around the middle")
            rows.append((f"shorter time window {where}", view("motion" if b - a <= 3.0 else preset, a, b)))
        if window.get("zoom_out"):
            a, b = window["zoom_out"]
            rows.append((f"longer time window {a:.2f}–{b:.2f} s", view("scan", a, b)))
    other = {"scan": "beat", "beat": "scan", "motion": "beat", "frame": "motion"}.get(preset, "scan")
    if other == "motion" and preset == "frame":
        rows.append(("this moment moving (motion, 3 s)", base + ["--preset", "motion", "--at", f"{start:.2f}"]))
    else:
        rows.append((f"same window as {other}", view(other, start, end)))
    if window.get("cut") is not None:
        rows.append((f"the cut it is in (cut {window['cut']})", base + ["--cut", str(window["cut"])]))
    if preset == "frame":
        rows.append(("a detail at source resolution", base + ["--at", f"{start:.2f}", "--region", "X,Y,W,H"]))
    return rows


def _visualization_timing(outputs: Mapping[str, Any], requested_at: float) -> dict[str, Any] | None:
    """Add queue time to the executor's timing: queued = executor start - request time.

    The executor cannot see when its request was admitted, so the client, which
    timed the request itself, computes the queue.  ``wall_s`` is the full
    client-observed duration.
    """
    raw = outputs.get("timing")
    timing = dict(raw) if isinstance(raw, Mapping) else {}
    started = timing.get("started_at")
    timing["queued_s"] = (
        round(max(0.0, float(started) - requested_at), 3)
        if isinstance(started, (int, float)) and not isinstance(started, bool) else None
    )
    timing["wall_s"] = round(time.time() - requested_at, 3)
    return timing


def _print_visualization_timing(timing: Any, requested_at: float) -> None:
    """One human line: how many frames were captured, how long, and how long it queued."""
    if not isinstance(timing, Mapping) or timing.get("capture_s") is None:
        print(f"visualized in {time.time() - requested_at:.0f} s (executor timing unavailable)")
        return
    queued = timing.get("queued_s")
    queue_text = f" (queued {float(queued):.0f} s)" if queued is not None else ""
    print(f"captured {int(timing.get('frames') or 0)} frames in {float(timing['capture_s']):.0f} s{queue_text}")


def _lenient_json_or_path(value: str) -> Any:
    """``--beats`` / ``--rules`` take the JSON object itself or a path to the file that holds it."""
    text = str(value)
    if text.lstrip().startswith("{") or text.lstrip().startswith("["):
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            raise argparse.ArgumentTypeError(f"invalid JSON: {exc.msg}") from exc
        return parsed
    return text


def _layer_names(parsed: argparse.Namespace) -> list[str]:
    names = [
        part.strip()
        for value in (getattr(parsed, "layers", None) or []) + (getattr(parsed, "overlay", None) or [])
        for part in str(value).split(",") if part.strip()
    ]
    return list(dict.fromkeys(names))


def cmd_visualize(parsed: argparse.Namespace) -> int:
    """Resolve what was asked, run it through the one SDK call, and say what came out."""
    from astrid.packs.rendering.actions.timeline_visualize.inspection_contract import (
        inspection_options,
    )
    from astrid.sdk.contracts import DomainResult, ErrorObject

    cli = _cli()
    if getattr(parsed, "list_layers", False):
        from astrid.packs.rendering.actions.timeline_visualize.layers import layer_help

        print(layer_help())
        return 0
    # The pre-port spellings of the diff selectors.
    if getattr(parsed, "from_revision", None) and not getattr(parsed, "before_revision_id", None):
        parsed.before_revision_id = parsed.from_revision
    if getattr(parsed, "to_revision", None) and not getattr(parsed, "after_revision_id", None):
        parsed.after_revision_id = parsed.to_revision
    if getattr(parsed, "preset", None) == "compare":
        return _cmd_visualize_diff(parsed)
    if getattr(parsed, "view", None) == "overview":
        parsed.view = "contact"
    view_was_explicit = getattr(parsed, "view", None) is not None
    try:
        is_diff_view = getattr(parsed, "view", None) == "diff"
        working = None if is_diff_view else _select_visualize_source(parsed)
        at_note = _resolve_visualize_addresses(parsed)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if at_note:
        print(at_note)
    _resolve_view(parsed)
    human_outputs: Mapping[str, Any] | None = None

    formats = [
        item.strip().lower()
        for value in (parsed.formats or ["png", "md"])
        for item in str(value).split(",")
        if item.strip()
    ]
    inputs: dict[str, Any] = {"formats": formats}
    timeline_slug = parsed.timeline_slug or parsed.timeline_ref
    for name in (
        "shot", "view", "sample", "every", "every_frames", "render_every", "include_cuts",
        "render_run", "columns", "page_size", "resolution", "include_media",
        "range", "at", "frame", "clip", "asset", "context", "neighbors", "show", "hide",
        "track", "detail", "occurrence", "revision_id", "before_revision_id", "after_revision_id", "edited",
    ):
        value = getattr(parsed, name, None)
        if value not in (None, "", []):
            inputs[name] = value
    if timeline_slug not in (None, ""):
        inputs["timeline_slug"] = timeline_slug
    if getattr(parsed, "size", None) and not inputs.get("resolution"):
        inputs["resolution"] = parsed.size
    if inputs.get("view") == "contact" and not inputs.get("resolution"):
        # Review scale for the overview (the executor defaults the same; sending it keeps an older host
        # from capturing at full canvas).
        inputs["resolution"] = "480x270"
    if getattr(parsed, "window_s", None) is not None:
        inputs["window_s"] = parsed.window_s
    if inputs.get("view") == "motion":
        inputs["preset"] = parsed.preset
        # a short window shows EVERY frame (the motion preset samples every 2nd)
        if inputs.get("every") is None and inputs.get("every_frames") is None and parsed.preset in (None, "motion"):
            span = float(getattr(parsed, "window_s", None) or 1.0)
            if inputs.get("range"):
                lo, _, hi = str(inputs["range"]).partition("..")
                try:
                    span = float(hi) - float(lo)
                except ValueError:
                    pass
            if span <= 1.0 + 1e-6:
                inputs["every_frames"] = 1
    elif getattr(parsed, "preset", None) and inputs.get("view") != "motion":
        inputs["preset"] = parsed.preset
    layers = _layer_names(parsed)
    from astrid.packs.timeline.cli_verify import with_labels_layer

    layers = with_labels_layer(layers, bool(getattr(parsed, "labels", False))) or []
    if layers:
        inputs["layers"] = layers
    if getattr(parsed, "cut", None) not in (None, ""):
        inputs["cut"] = str(parsed.cut)
    if getattr(parsed, "frame_budget", None) is not None:
        inputs["frame_budget"] = parsed.frame_budget
    if getattr(parsed, "highlight", None):
        inputs["highlight"] = parsed.highlight
    if getattr(parsed, "cuts", None):
        inputs["cuts"] = parsed.cuts
    if getattr(parsed, "preview", False):
        inputs["preview"] = True
    if inputs.get("view") == "motion":
        from astrid.packs.rendering.actions.timeline_visualize.motion.rules import resolve_rules

        try:
            given = getattr(parsed, "rules", None)
            rules = given if isinstance(given, Mapping) else resolve_rules(
                given, search=not getattr(parsed, "no_rules", False))
        except (OSError, ValueError) as exc:
            print(f"error validation_error: {exc}", file=sys.stderr)
            return 2
        if rules:
            inputs["rules"] = dict(rules)
    elif isinstance(getattr(parsed, "rules", None), Mapping):
        inputs["rules"] = dict(parsed.rules)
    if getattr(parsed, "beats", None):
        try:
            beats = parsed.beats
            inputs["beats"] = dict(beats) if isinstance(beats, Mapping) else json.loads(_read_beats(str(beats)))
        except (OSError, ValueError) as exc:
            print(f"error validation_error: --beats {parsed.beats}: {exc}", file=sys.stderr)
            return 2
    # The public text and visual routes use the same bounded selector normalizer.
    inputs["occurrence"] = inspection_options({"occurrence": getattr(parsed, "occurrence", None)})["occurrence"]
    if inputs["occurrence"] is None:
        inputs.pop("occurrence")
    shown_components = inputs.get("show") or []
    hidden_components = inputs.get("hide") or []
    if "output" in hidden_components or (shown_components and "output" not in shown_components):
        mode = "inputs"
    else:
        mode = getattr(parsed, "mode", "auto")
    if mode == "inputs" and not view_was_explicit:
        # Native input inspection only admits the filmstrip view.  Keep the
        # ordinary contact default for composed/auto requests, but do not let
        # it leak into an inputs request assembled from --show/--hide either.
        inputs["view"] = "filmstrip"
        if not getattr(parsed, "resolution", None) and not getattr(parsed, "size", None):
            inputs.pop("resolution", None)

    region_request = _region_request(parsed)
    if region_request is not None:
        if isinstance(region_request, str):
            print(f"error validation_error: {region_request}", file=sys.stderr)
            return 2
        if not getattr(parsed, "size", None) and not getattr(parsed, "resolution", None):
            # source resolution: capture the frame at the canvas size, so one page px = one canvas px
            inputs["resolution"] = "x".join(map(str, _bundle_canvas(parsed)))
    if mode != "inputs" and inputs.get("view") != "diff":
        title = _timeline_title(parsed, timeline_slug)
        if title:
            inputs["title"] = title
        if inputs.get("view") == "contact":
            plans = _contact_shot_plans(parsed, inputs, working, labels=bool(getattr(parsed, "shot_plans", False)))
            if plans is not None:
                inputs["shot_plans"] = plans

    validation_error = None
    revision_selectors = ("before_revision_id", "after_revision_id")
    if inputs.get("view") == "diff":
        missing = [name for name in revision_selectors if not str(inputs.get(name) or "").strip()]
        if not isinstance(parsed.project, str) or not parsed.project.strip():
            validation_error = ErrorObject("validation_error", "view=diff requires an explicit --project",
                                           {"field": "project", "view": "diff"})
        elif not isinstance(timeline_slug, str) or not timeline_slug.strip():
            validation_error = ErrorObject("validation_error",
                                           "view=diff requires an explicit timeline slug or positional timeline",
                                           {"field": "timeline", "view": "diff"})
        elif missing:
            validation_error = ErrorObject("validation_error",
                                           "view=diff requires both --before-revision-id and --after-revision-id "
                                           "(--from REV and --to REV also work)",
                                           {"fields": missing, "view": "diff"})
        elif getattr(parsed, "revision_id", None) not in (None, ""):
            validation_error = ErrorObject("validation_error",
                                           "view=diff uses only the explicit before/after revision selectors",
                                           {"field": "revision_id", "view": "diff"})
        elif mode == "inputs":
            validation_error = ErrorObject("validation_error",
                                           "render-free input mode supports only view=filmstrip; diff requires "
                                           "admitted immutable bundles", {"field": "mode", "view": "diff"})
    elif any(str(inputs.get(name) or "").strip() for name in revision_selectors):
        validation_error = ErrorObject("validation_error",
                                       "before/after revision selectors are valid only with view=diff",
                                       {"fields": list(revision_selectors), "view": inputs.get("view")})
    elif inputs.get("edited"):
        validation_error = ErrorObject("validation_error", "--edited applies to --view diff",
                                       {"field": "edited", "view": inputs.get("view")})

    if working is not None:
        from astrid.core.timeline.authoring_bundle import (
            AuthoringBundleError,
            preview_authoring_candidate,
        )
        from astrid.sdk.timeline_checkout import Checkout

        draft = Checkout.load(working["draft"])
        if getattr(parsed, "compare_with", None) == "published":
            # Before, then after: the published version and the working copy, same frames.
            published_parsed = copy.copy(parsed)
            published_parsed.compare_with = None
            published_parsed.published = True
            published_parsed.draft = None
            published_parsed.revision_id = None
            published_parsed._draft_checkout = None
            print("── PUBLISHED " + "─" * 60)
            code = cmd_visualize(published_parsed)
            print("── WORKING COPY " + "─" * 57)
            if code not in (0, None):
                return code
        changed_ids = draft.changed_cut_ids()
        banner = (
            f"WORKING COPY {working['name']} · {_clips_changed(len(draft.edits().get('changes') or []))} · "
            f"base revision {working['base_revision']}"
        )
        selected = any(
            inputs.get(name) not in (None, "", [])
            for name in ("cut", "range", "at", "frame", "clip", "asset", "occurrence", "shot")
        )
        cut_line = None
        if not selected and not getattr(parsed, "all_cuts", False):
            if working["changed_cuts"]:
                inputs["cuts"] = ",".join(map(str, working["changed_cuts"][:12]))
                inputs["view"] = "contact"
                inputs.setdefault("resolution", "480x270")
            cut_line = _changed_cuts_line(working["changed_cuts"])
            if changed_ids:
                cut_line += f"  [✎ {', '.join(changed_ids)}]"
        elif getattr(parsed, "all_cuts", False):
            cut_line = "showing every cut of the working copy"
        else:
            window = inputs.get("at") or inputs.get("frame") or (str(inputs.get("range") or "").split("..")[0] or None)
            here = None
            try:
                if window not in (None, ""):
                    t = float(window)
                    here = next((g["id"] for g in draft._cut_groups()
                                 if g["start"] - 1e-6 <= t < (draft._cut_spans()[g["id"]][1] or draft.duration)), None)
            except (TypeError, ValueError):
                here = None
            if here and here in changed_ids:
                cut_line = f"✎ {here} is changed in the working copy (before/after: --compare published)"
            elif here:
                elsewhere = ", ".join(changed_ids[:8]) + (" …" if len(changed_ids) > 8 else "")
                cut_line = (f"{here}: no clip of it differs from the published head (same place, same params)"
                            + (f"; your changes are in {elsewhere} (--cut {changed_ids[0]})" if changed_ids else ""))
        if not parsed.json:
            print(banner)
            if cut_line:
                print(cut_line)
        try:
            preview = preview_authoring_candidate(draft.document())
            if isinstance(preview, dict):
                preview["label"] = f"working copy {parsed.draft} · base revision {working['base_revision']}"
            inputs["authoring_preview"] = preview
        except AuthoringBundleError as exc:
            print(f"error validation_error: the working copy does not compile ({exc}); run timelines check",
                  file=sys.stderr)
            return 2
        mode = "composed" if mode == "auto" else mode
    source_identity = (
        {"kind": "working_copy", "name": working["name"], "base_revision_id": working["base_revision"]}
        if working is not None else
        {"kind": "revision", "revision_id": getattr(parsed, "_source_revision_id", None)}
        if not is_diff_view else None
    )
    if getattr(parsed, "compare_with", None) and working is None and not parsed.json:
        print("no working copy: nothing to compare (showing the published head)")
    if getattr(parsed, "plan", False):
        return _visualize_plan(parsed, inputs)
    requested_at = time.time()
    # --out is an export of the settled view (the managed cache stays the source), never the
    # capture's working directory.
    export_dir = getattr(parsed, "out", None)
    if validation_error is not None:
        result = DomainResult.failure(validation_error)
    else:
        result = parsed.client.timelines.visualize(
            parsed.project,
            timeline_slug,
            mode=mode,
            options=inputs,
            revision_id=getattr(parsed, "_source_revision_id", getattr(parsed, "revision_id", None)),
        )
    if isinstance(result, DomainResult):
        # Native Runtime views are already typed DomainResults and deliberately have no task/run identity.
        if result.ok:
            outputs = dict(result.data) if isinstance(result.data, Mapping) else {"data": result.data}
            outputs["working_copy"] = working if working is not None else None
            outputs["source_identity"] = source_identity
            summary = cli._visualization_artifact_summary(outputs)
            if summary is not None:
                outputs["artifact_summary"] = summary
            outputs["navigation"] = cli._visualization_navigation_help(
                project=parsed.project, inputs=inputs, outputs=outputs,
            )
            human_outputs = outputs
            envelope = DomainResult.success(
                {
                    "capability_id": "timelines.visualize",
                    "run_id": None,
                    "kernel_run_id": None,
                    "kernel_task_id": None,
                    "kernel_attempt_id": None,
                    "manifest_path": None,
                    "outputs": outputs,
                },
                receipt=result.receipt,
                idempotency_key=result.idempotency_key,
            )
        else:
            envelope = result
    elif result.ok:
        outputs = result.outputs
        if isinstance(outputs, Mapping):
            outputs = dict(outputs)
            outputs.setdefault("working_copy", working if working is not None else None)
            outputs.setdefault("source_identity", source_identity)
            summary = cli._visualization_artifact_summary(outputs)
            if summary is not None:
                outputs["artifact_summary"] = summary
            outputs["navigation"] = cli._visualization_navigation_help(
                project=parsed.project, inputs=inputs, outputs=outputs,
            )
            human_outputs = outputs
        envelope = DomainResult.success(
            {
                "capability_id": result.capability_id,
                "run_id": result.run_id,
                "kernel_run_id": result.kernel_run_id,
                "kernel_task_id": result.kernel_task_id,
                "kernel_attempt_id": result.kernel_attempt_id,
                "manifest_path": result.manifest_path,
                "outputs": outputs,
            }
        )
    else:
        detail = dict(result.error or {})
        category = str(detail.get("sdk_category") or "invocation")
        envelope = DomainResult.failure(
            ErrorObject(
                code="validation_error" if category == "validation" else "invocation_error",
                message=str(detail.get("message") or "timeline visualization failed"),
                details={
                    "sdk_error": detail.get("sdk_error"),
                    "sdk_category": category,
                    "validation": detail.get("validation"),
                    "run_id": result.run_id,
                    "kernel_run_id": result.kernel_run_id,
                    "kernel_task_id": result.kernel_task_id,
                    "kernel_attempt_id": result.kernel_attempt_id,
                },
            )
        )
    if human_outputs is not None:
        timing = _visualization_timing(human_outputs, requested_at)
        if timing is not None:
            human_outputs["timing"] = timing
        if export_dir:
            try:
                human_outputs["exported"] = cli._export_visualization(
                    human_outputs, export_dir, client=parsed.client, project=parsed.project,
                )
            except cli.VisualizationExportError as exc:
                return print_result(DomainResult.failure(ErrorObject(
                    exc.code,
                    exc.args[0],
                    {
                        "artifacts": exc.artifacts,
                        "files_written": exc.written,
                        "export_dir": export_dir,
                    },
                )), as_json=parsed.json)
    plan_rows = None
    if getattr(parsed, "shot_plans", False) and human_outputs is not None:
        plan_rows = _visualized_plan_rows(parsed, inputs, working)
        human_outputs["shot_plans"] = plan_rows
    region_page = None
    if human_outputs is not None and region_request is not None:
        try:
            region_page = _draw_region_page(human_outputs, region_request, parsed=parsed, inputs=inputs)
            human_outputs["region_page"] = region_page
            human_outputs["primary_page"] = region_page["page"]
            human_outputs.setdefault("findings", [])
            human_outputs["findings"] = [region_page["finding"], *list(human_outputs["findings"] or [])]
        except Exception as exc:  # noqa: BLE001 - the full frame stands; say why the crop is missing
            human_outputs["findings"] = [f"REGION not drawn: {exc}", *list(human_outputs.get("findings") or [])]
    if human_outputs is not None:
        stable = _stable_page(human_outputs, parsed=parsed, inputs=inputs)
        if stable:
            human_outputs["page"] = stable
    if parsed.json or human_outputs is None or not result.ok:
        return print_result(envelope, as_json=parsed.json)
    queued_note = _queue_note(human_outputs)
    if queued_note:
        print(queued_note)
    print(_visualization_summary(human_outputs, parsed=parsed, inputs=inputs))
    if plan_rows is not None:
        print("\n".join(shot_plan_lines(plan_rows)))
    if human_outputs.get("exported"):
        exported = human_outputs["exported"]
        print(f"export directory: {exported['dir']}")
        if len(exported["frames"]) > 1:
            frame_dir = Path(exported["frames"][0]).parent
            print(f"  raw frames: {len(exported['frames'])} file(s) in {frame_dir}")
    page = human_outputs.get("page") or human_outputs.get("primary_page")
    if page:
        print(f"page: {page}")  # always the last line: the picture to open
    return 0


def _region_request(parsed: argparse.Namespace) -> Any:
    """``--region X,Y,W,H`` / ``--zoom LAYER`` with ``--at``: the box (or layer) to draw at source resolution.

    None when neither is given; an error string when they are given wrongly."""
    region, zoom = getattr(parsed, "region", None), getattr(parsed, "zoom", None)
    if not region and not zoom:
        return None
    if region and zoom:
        return "choose --region or --zoom, not both"
    if getattr(parsed, "at", None) in (None, "") and getattr(parsed, "frame", None) is None:
        return "--region/--zoom need --at T (one frame to crop)"
    if region:
        from astrid.packs.rendering.actions.timeline_visualize.fast_lane import FastLaneError, parse_region

        try:
            box = parse_region(region)
        except FastLaneError as exc:
            return str(exc)
        return {"region": box, "label": f"region {region}"}
    return {"zoom": str(zoom), "label": f"zoom {zoom}"}


def _bundle_canvas(parsed: argparse.Namespace) -> tuple[int, int]:
    """The timeline's canvas (the source resolution of a composed frame); 1920x1080 when unknown."""
    bundle = getattr(parsed, "_bundle", None)
    if not isinstance(bundle, Mapping):
        try:
            opened = parsed.client.timelines.open_bundle(
                parsed.project, getattr(parsed, "timeline_slug", None) or getattr(parsed, "timeline_ref", None),
                **({"revision_id": parsed.revision_id} if getattr(parsed, "revision_id", None) else {}))
            bundle = opened.data.get("bundle") if getattr(opened, "ok", False) else None
            parsed._bundle = bundle
        except Exception:  # noqa: BLE001
            bundle = None
    parent = bundle.get("parent") if isinstance(bundle, Mapping) else None
    config = parent.get("config") if isinstance(parent, Mapping) else None
    canvas = (((config or {}).get("theme_overrides") or {}).get("visual") or {}).get("canvas") or {}
    try:
        return int(canvas.get("width") or 1920), int(canvas.get("height") or 1080)
    except (TypeError, ValueError, AttributeError):
        return 1920, 1080


def _timeline_title(parsed: argparse.Namespace, ref: Any) -> str | None:
    """The timeline's display name (``Rough cut C2``) for page headers; None when the Runtime does not say."""
    resolver = getattr(getattr(parsed.client, "timelines", None), "resolve_scope", None)
    if not callable(resolver):
        return None
    try:
        scoped = resolver(parsed.project, ref)
    except Exception:  # noqa: BLE001 - the page then shows the id
        return None
    data = getattr(scoped, "data", None)
    if not getattr(scoped, "ok", False) or not isinstance(data, Mapping):
        return None
    name = data.get("name") or data.get("display_name") or data.get("slug")
    return str(name) if isinstance(name, str) and name.strip() else None


def _contact_shot_plans(parsed: argparse.Namespace, inputs: Mapping[str, Any], working: Mapping[str, Any] | None,
                        *, labels: bool) -> dict[str, Any] | None:
    """The overview's plan rows: where a plan has a line (silence there is flagged) and, with ``--shot-plans``,
    each shot's ``methods · status · takes`` label drawn on its tiles."""
    from astrid.sdk.shot_plan import plan_rows

    try:
        if working is not None:
            from astrid.sdk.timeline_checkout import Checkout

            bundle = Checkout.load(working["draft"]).bundle
        else:
            bundle = getattr(parsed, "_bundle", None)
            if not isinstance(bundle, Mapping):
                revision = inputs.get("revision_id") or getattr(parsed, "_source_revision_id", None)
                opened = parsed.client.timelines.open_bundle(
                    parsed.project, inputs.get("timeline_slug"), **({"revision_id": revision} if revision else {}))
                bundle = opened.data.get("bundle") if getattr(opened, "ok", False) else None
        if not isinstance(bundle, Mapping):
            return None
        rows = plan_rows(bundle)
    except Exception:  # noqa: BLE001 - the overview stands without plans
        return None
    out = []
    for row in rows:
        plan = row.get("plan") or {}
        line = bool(row.get("voiceover_script")) or bool(str((plan or {}).get("line") or "").strip())
        out.append({"occurrence_id": str(row.get("occurrence_id") or ""), "start": float(row["start"]),
                    "end": float(row["end"]), "label": tile_plan_label(plan) if labels else "", "line": line})
    if not out:
        return None
    return {"labels": labels, "rows": out}


def tile_plan_label(plan: Mapping[str, Any] | None) -> str:
    """What fits on a tile: ``LM+R · planned · 2 takes`` (the line itself stays in the text rows)."""
    from astrid.sdk.shot_plan import plan_problem

    if not plan:
        return "no plan"
    if plan_problem(plan) is not None:
        return "invalid plan"
    parts = ["+".join(plan.get("method") or []) or "no method", str(plan.get("status") or "no status")]
    takes = plan.get("takes") or []
    if takes:
        parts.append(f"{len(takes)} take{'s' if len(takes) != 1 else ''}")
    return " · ".join(parts)


def _frame_image(outputs: Mapping[str, Any]) -> tuple[Path, Path, dict[str, Any]]:
    """The captured frame of an ``--at`` view: (pack dir, frame image, frame-index card)."""
    pages = [str(p) for p in outputs.get("pages") or [] if p] or [str(outputs.get("primary_page") or "")]
    root = Path(pages[0]).parent
    index = json.loads((root / "frame-index.json").read_text(encoding="utf-8"))
    cards = [c for c in index.get("cards") or [] if isinstance(c, Mapping) and c.get("image")]
    if not cards:
        raise ValueError("the view captured no frame to crop")
    return root, root / str(cards[0]["image"]), dict(cards[0])


def _draw_region_page(outputs: Mapping[str, Any], request: Mapping[str, Any], *, parsed: argparse.Namespace,
                      inputs: Mapping[str, Any]) -> dict[str, Any]:
    """Crop the captured frame at source resolution (the ``timelines check --zoom/--region`` crop) onto a page."""
    from PIL import Image, ImageDraw

    from astrid.packs.rendering.actions.timeline_visualize.fast_lane import _zoom_tile, zoom_region, zoom_scale
    from astrid.packs.rendering.actions.timeline_visualize.layers.base import PALETTE, draw_text

    root, frame_path, card = _frame_image(outputs)
    snapshot = json.loads((root / "render-snapshot.json").read_text(encoding="utf-8"))
    config = snapshot.get("config") if isinstance(snapshot.get("config"), Mapping) else {}
    canvas_raw = (((config.get("theme_overrides") or {}).get("visual") or {}).get("canvas") or {})
    canvas = (int(canvas_raw.get("width") or 1920), int(canvas_raw.get("height") or 1080))
    t = float(card.get("time_seconds") or 0.0)
    if request.get("region"):
        box = tuple(request["region"])
    else:
        from fractions import Fraction

        fps = float(Fraction(*snapshot.get("fps_rational") or [30, 1]))
        box = zoom_region((snapshot,), str(request["zoom"]), t, fps, canvas)
        if box is None:
            raise ValueError(f"{request['zoom']} is not on screen at {t:.2f} s (try --region X,Y,W,H)")
    x0, y0, x1, y1 = (max(0, box[0]), max(0, box[1]), min(canvas[0], box[2]), min(canvas[1], box[3]))
    if x1 <= x0 or y1 <= y0:
        raise ValueError(f"the region {box} lies outside the {canvas[0]}x{canvas[1]} canvas")
    with Image.open(frame_path) as image:
        frame = image.convert("RGB")
    sx, sy = frame.size[0] / canvas[0], frame.size[1] / canvas[1]
    crop_box = (round(x0 * sx), round(y0 * sy), round(x1 * sx), round(y1 * sy))
    source_px = "source resolution" if abs(sx - 1) < 1e-6 else f"frame captured at {frame.size[0]}x{frame.size[1]}"
    crop_path = root / f"region-{t:.2f}-{x0}-{y0}-{x1 - x0}x{y1 - y0}.png"
    frame.crop(crop_box).save(crop_path)
    scale = zoom_scale((x1 - x0, y1 - y0), width=1880, height=1040)
    tile = _zoom_tile(crop_path, scale)
    title = str(inputs.get("title") or inputs.get("timeline_slug") or "timeline")
    what = request.get("label") or "region"
    head = f"{title} · {t:.2f} s · {what}"
    sub = (f"canvas px {x0},{y0} to {x1},{y1} ({x1 - x0}x{y1 - y0}) · {source_px} · "
           + ("shown 1:1" if scale == 1 else f"shown at {scale:g}x"))
    thumb_w = 384
    thumb = frame.resize((thumb_w, round(thumb_w * frame.size[1] / frame.size[0])), Image.LANCZOS)
    tdraw = ImageDraw.Draw(thumb)
    k = thumb_w / frame.size[0]
    tdraw.rectangle((crop_box[0] * k, crop_box[1] * k, crop_box[2] * k, crop_box[3] * k), outline="#facc15", width=2)
    width = max(tile.size[0] + 20, thumb_w + 20, 900)
    height = 16 + 28 + 22 + 10 + max(tile.size[1], 0) + 14 + thumb.size[1] + 30
    page = Image.new("RGB", (width, height), PALETTE["bg"])
    draw = ImageDraw.Draw(page)
    draw_text(draw, (10, 10), head, 22, "white")
    draw_text(draw, (10, 42), sub, 15, PALETTE["muted"])
    page.paste(tile, (10, 70))
    y = 70 + tile.size[1] + 14
    draw_text(draw, (10, y), "where it is in the frame:", 14, PALETTE["muted"])
    page.paste(thumb, (10, y + 20))
    out = root / f"view-region-{t:.2f}-{x0}-{y0}-{x1 - x0}x{y1 - y0}.png"
    page.save(out)
    finding = f"REGION {t:.2f}s {what}: canvas {x0},{y0}–{x1},{y1} ({x1 - x0}x{y1 - y0} px) at {source_px}"
    return {"page": str(out), "crop": str(crop_path), "box": [x0, y0, x1, y1], "finding": finding}


def _slug(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-")
    return slug[:60] or "timeline"


def _stable_name(inputs: Mapping[str, Any], outputs: Mapping[str, Any]) -> str:
    """``overview``, ``cut-08``, ``frame-14.00s``, ``frame-14.00s-region-700-300-600x300``, ``scan-8.00-20.00s``."""
    view = str(inputs.get("view") or "filmstrip")
    preset = str(inputs.get("preset") or "")
    window = outputs.get("window") if isinstance(outputs.get("window"), Mapping) else {}
    if view == "contact":
        name = "overview" + (f"-cuts-{str(inputs['cuts']).replace(',', '-')}" if inputs.get("cuts") else "")
    elif view == "motion" and preset == "cut":
        cut = str(inputs.get("cut") or "")
        name = f"cut-{int(cut):02d}" if cut.isdigit() else f"cut-{_slug(cut)}"
    elif view == "motion" and preset == "frame":
        at = window.get("start", inputs.get("at"))
        try:
            name = f"frame-{float(at):.2f}s"
        except (TypeError, ValueError):
            name = f"frame-{_slug(str(at))}"
    elif view == "motion":
        name = f"{preset or 'window'}-{float(window.get('start', 0)):.2f}-{float(window.get('end', 0)):.2f}s"
    else:
        name = view
    region = outputs.get("region_page")
    if isinstance(region, Mapping) and region.get("box"):
        x0, y0, x1, y1 = region["box"]
        name += f"-region-{x0}-{y0}-{x1 - x0}x{y1 - y0}"
    return name


def _stable_page(outputs: Mapping[str, Any], *, parsed: argparse.Namespace, inputs: Mapping[str, Any]) -> str | None:
    """Copy the page to ``<visualize cache>/<project>/pages/<timeline>/<view>.png``: a path a person can find
    again (named by timeline, view and time), next to the hash folder that stays the cache."""
    import shutil

    primary = outputs.get("primary_page") or next(iter(outputs.get("pages") or []), None)
    if not primary or not Path(str(primary)).is_file():
        return None
    try:
        root = Path(str(primary)).parent.parent  # <base>/<project>/<digest>/page.png
        timeline = _slug(str(inputs.get("title") or inputs.get("timeline_slug") or "timeline"))
        if getattr(parsed, "_revision_explicit", False) and inputs.get("revision_id"):
            timeline += f"@{_short_rev(inputs['revision_id'])}"
        if getattr(parsed, "draft", None):
            timeline += f"@draft-{_slug(parsed.draft)}"
        target = root / "pages" / timeline / (_stable_name(inputs, outputs) + Path(str(primary)).suffix)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(str(primary), target)
        return str(target)
    except OSError:
        return None


def _queue_note(outputs: Mapping[str, Any]) -> str | None:
    """Say plainly when the view waited behind other Runtime work (and that nothing is lost by waiting)."""
    timing = outputs.get("timing") if isinstance(outputs.get("timing"), Mapping) else {}
    queued = timing.get("queued_s")
    if isinstance(queued, (int, float)) and queued >= 15:
        return (f"waited {queued:.0f} s for the Runtime: another task (a render, a capture) held the frame "
                "capture first")
    return None


def _visualized_plan_rows(parsed: argparse.Namespace, inputs: Mapping[str, Any],
                          working: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """The shot plans of the revision (or working copy) that was drawn, one row per placement, in time order."""
    from astrid.sdk.shot_plan import plan_label, plan_rows

    if working is not None:
        from astrid.sdk.timeline_checkout import Checkout

        bundle = Checkout.load(working["draft"]).bundle
    else:
        revision = inputs.get("revision_id") or getattr(parsed, "_source_revision_id", None)
        opened = parsed.client.timelines.open_bundle(
            parsed.project, inputs.get("timeline_slug"), **({"revision_id": revision} if revision else {}))
        if not getattr(opened, "ok", False) or not isinstance(getattr(opened, "data", None), Mapping):
            return [{"error": f"shot plans unavailable: {getattr(opened, 'error', opened)}"}]
        bundle = opened.data["bundle"]
    return [{**row, "label": plan_label(row["plan"])} for row in plan_rows(bundle)]


def shot_plan_lines(rows: list[Mapping[str, Any]]) -> list[str]:
    """``shot plans:`` then one line per shot placement: time, name, methods · status · takes · line."""
    if rows and "error" in rows[0]:
        return [str(rows[0]["error"])]
    lines = [f"shot plans ({sum(1 for r in rows if r.get('plan'))} of {len(rows)} placed shots planned):"]
    for row in rows:
        lines.append(f"  {float(row['start']):6.2f}–{float(row['end']):.2f}s  {row['name']}  {row['label']}"
                     + ("  (shared shot)" if row.get("shared") else ""))
    if not rows:
        lines.append("  (no shots on this timeline)")
    return lines


def _head_revision(parsed: argparse.Namespace) -> str | None:
    """The head revision id of the timeline (the default ``--to`` of a diff)."""
    opener = getattr(parsed.client.timelines, "open_bundle", None)
    if not callable(opener):
        return None
    opened = opener(parsed.project, parsed.timeline_slug or parsed.timeline_ref)
    data = getattr(opened, "data", None)
    if getattr(opened, "ok", False) and isinstance(data, Mapping):
        return str(data.get("revision_id") or "") or None
    return None


def add_zoom_flags(parser: argparse.ArgumentParser) -> None:
    """``check --at MOMENT --zoom ADDRESS | --region X,Y,W,H``: a full-resolution crop of both sides."""
    parser.add_argument("--zoom", default=None, metavar="ADDRESS",
                        help="With --at: also crop both frames around this layer at full resolution (c08.app), "
                             "for details the thumbnails cannot show (connector ends, 1-px outlines).")
    parser.add_argument("--region", default=None, metavar="X,Y,W,H",
                        help="With --at: crop this canvas-px rectangle at full resolution instead of a layer.")


def check_zoom(parsed: argparse.Namespace, checkout: Any) -> tuple[list[Any] | None, dict[str, Any]]:
    """The moments ``check`` verifies and the extra ``verify`` keywords: ``--zoom`` alone looks at the layer's middle."""
    at = parsed.at
    extra: dict[str, Any] = {}
    zoom, region = getattr(parsed, "zoom", None), getattr(parsed, "region", None)
    if zoom:
        extra["zoom"] = zoom
    if region:
        extra["region"] = region
    if at is None and zoom:
        layer = checkout.clip(zoom)
        at = [f"{(layer.start + layer.end) / 2:.2f}"]
    return at, extra
