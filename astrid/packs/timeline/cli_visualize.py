"""``timelines visualize``: working copy, addresses, presets, compare, plan and the human summary.

Kept beside ``cli.py`` (like ``cli_sweep`` and ``cli_verify``) so the verb stays one readable module. The
SDK call and the admitted views are Runtime-owned; this module resolves what the caller *meant* (an
address, a preset, the working copy) into the options that call takes, and prints the result as words.
"""
from __future__ import annotations

import argparse
import json
import re
import shlex
import sys
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from astrid.core.cli.domain_output import print_result

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
    text = str(revision or "")
    return text.removeprefix("authoring-parent-revision-")[:8] or text


def _clips_changed(n: int) -> str:
    """The one count every verb prints for a working copy: clips added, removed, moved or edited."""
    return f"{n} clip{'s' if n != 1 else ''} changed"


def _working_banner(working: Mapping[str, Any]) -> str:
    """The one-line banner show, lint and diff print when they read the working copy."""
    return (f"WORKING COPY · {_clips_changed(len(working['changes']))} vs published {working['base_revision']} · "
            "--published for the live version")


def _visualize_working_copy(parsed: argparse.Namespace) -> dict[str, Any] | None:
    """The working copy visualize reads (path, base, edits, changed cuts), or None when it reads the published head."""
    from astrid.sdk.timeline_checkout import Checkout, find_draft

    if getattr(parsed, "published", False) or getattr(parsed, "revision_id", None) or getattr(parsed, "from_revision", None):
        return None
    ref = getattr(parsed, "timeline_slug", None) or getattr(parsed, "timeline_ref", None)
    try:
        path = find_draft(parsed.project, ref, getattr(parsed, "draft", None))
    except Exception as exc:  # the runtime is not reachable: say so, visualize the published head
        print(f"working copy: not checked ({exc}); visualizing the published head", file=sys.stderr)
        return None
    if path is None:
        return None
    checkout = Checkout.load(path)
    changes = list(checkout.edits().get("changes") or [])
    return {
        "draft": str(path),
        "base_revision": checkout.base_revision,
        "edits": len(changes),
        "changed_cuts": [int(n) for n in checkout.check().changed_cuts],
    }


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
    (``'"Building" in v27'``, ``c30``, ``c41.mink``, ``93.5``), read from the working copy unless
    ``--published``. Ambiguity lists the choices; the line printed says which one was used."""
    from astrid.sdk.timeline_address import AddressError, resolve
    from astrid.sdk.timeline_checkout import Checkout, find_draft

    def needs(name: str, plain: str) -> bool:
        value = getattr(parsed, name, None)
        return isinstance(value, str) and bool(value.strip()) and not re.fullmatch(plain, value)

    if not (needs("at", r"\s*@?[\d.:]+s?\s*") or needs("range", r"\s*[\d.:]+\s*\.\.\s*[\d.:]+\s*")
            or needs("highlight", r"") or needs("clip", r"[^.]*-\d\d-am-[a-z-]+")
            or (needs("cut", r"\s*\d+\s*") and re.fullmatch(r"\s*c\d+[a-z]?\s*", str(parsed.cut).lower()))):
        return None  # plain seconds and cut numbers need no lookup
    ref = getattr(parsed, "timeline_slug", None) or getattr(parsed, "timeline_ref", None)
    tl = None
    if not getattr(parsed, "published", False) and not getattr(parsed, "revision_id", None):
        try:
            path = find_draft(parsed.project, ref, getattr(parsed, "draft", None), client=parsed.client)
        except Exception:  # noqa: BLE001 - fall back to the published head
            path = None
        tl = Checkout.load(path) if path is not None else None
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
            elif target.kind == "cut":
                start_frame = round(target.start * tl.fps)
                end_frame = round(target.end * tl.fps)
                frame = start_frame + max(0, (end_frame - start_frame - 1) // 2)
                parsed.at = f"{frame / tl.fps:.3f}"
                notes.append(f"--at {at!r}: middle of {target.address}, frame {frame} ({float(parsed.at):.2f} s)")
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
        from astrid.packs.rendering.actions.timeline_visualize.motion.diffview import compose_compare, window_frames_of

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
                                                "intervals": report['intervals'],
                                                "notes": notes}, "error": None}, indent=2))
        return 0
    from astrid.core.timeline.change_intervals import interval_lines
    notes.extend(interval_lines(report['intervals']))
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


def _visualization_summary(outputs: Mapping[str, Any], *, parsed: argparse.Namespace, inputs: Mapping[str, Any]) -> str:
    """The human result: where the picture is, what it holds, how long it took, what to run next."""
    view = str(inputs.get("view") or "filmstrip")
    pages = [str(page) for page in outputs.get("pages") or [] if page]
    primary = outputs.get("primary_page") or (pages[0] if pages else None) or outputs.get("png")
    timing = outputs.get("timing") if isinstance(outputs.get("timing"), Mapping) else {}
    label = {"contact": "contact sheet", "motion": "motion sheet" if inputs.get("preset") == "cut" else
             f"{inputs.get('preset')} view", "filmstrip": "filmstrip"}.get(view, view)
    lines = [f"{label}: {primary or '(no page produced; see --json)'}"]
    for page in pages:
        if page != primary:
            lines.append(f"  also: {page}")
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
        "contact": f"{tiles} tiles, one per picture cut (#N = cut number in timelines show)",
        "motion": (f"cut {inputs.get('cut')}: {frames} frames, dense after each entrance" if preset == "cut" else
                   f"{preset} {float(window_info.get('start', 0)):.2f}–{float(window_info.get('end', 0)):.2f} s: "
                   f"{frames} frames on one page"
                   + (f" (window {float(window_info.get('end', 0)) - float(window_info.get('start', 0)):.2f} s: "
                      f"--window S widens it, up to {_preset_max()[str(preset)]:g} s)" if str(preset) in _preset_max() else "")),
    }.get(view, f"{frames} frames")
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
    base = ["python3", "-m", "astrid", "timelines", "visualize", timeline, "--project", project]
    if inputs.get("revision_id"):
        base += ["--revision-id", str(inputs["revision_id"])]
    lint = ["python3", "-m", "astrid", "timelines", "lint", timeline, "--project", project]
    show = ["python3", "-m", "astrid", "timelines", "show", timeline, "--project", project]
    beats = ["--beats", str(parsed.beats)] if getattr(parsed, "beats", None) else []
    window = outputs.get("window") if isinstance(outputs.get("window"), Mapping) else None
    if view == "motion" and window and inputs.get("preset") != "cut":
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


def _visualize_plan(parsed: argparse.Namespace, inputs: Mapping[str, Any]) -> int:
    """``--plan``: resolve the view, window and frames without capturing (reads the timeline only)."""
    import math

    from astrid.packs.rendering.actions.timeline_visualize.filmstrip_cards import plan_filmstrip
    from astrid.packs.rendering.actions.timeline_visualize.filmstrip_options import filmstrip_options
    from astrid.packs.rendering.actions.timeline_visualize.motion.window import fit_columns, preset_settings
    from astrid.sdk.timeline_filmstrip import prepare_filmstrip

    values = dict(inputs)
    values["composed_capture"] = True
    try:
        authority = prepare_filmstrip({**values, "view": "filmstrip"}, project=parsed.project, client=parsed.client)
        options = filmstrip_options(values)
        index = plan_filmstrip(authority["capture_snapshot"], options)
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
    if window.get("earlier"):
        a, b = window["earlier"]
        rows.append((f"earlier {a:.2f}–{b:.2f} s", view(preset, a, b)))
    if window.get("later"):
        a, b = window["later"]
        rows.append((f"later {a:.2f}–{b:.2f} s", view(preset, a, b)))
    if window.get("zoom_in"):
        a, b = window["zoom_in"]
        where = f"the busiest moment {window['busiest']:.2f} s" if window.get("busiest") is not None else "the middle"
        rows.append((f"zoom in on {where}", view("motion" if b - a <= 3.0 else preset, a, b)))
    if window.get("zoom_out") and preset != "frame":
        a, b = window["zoom_out"]
        rows.append(("zoom out", view("scan", a, b)))
    other = {"scan": "beat", "beat": "scan", "motion": "beat", "frame": "motion"}.get(preset, "scan")
    if other == "motion" and preset == "frame":
        rows.append(("this moment moving (motion, 3 s)", base + ["--preset", "motion", "--at", f"{start:.2f}"]))
    else:
        rows.append((f"same window as {other}", view(other, start, end)))
    if window.get("cut") is not None:
        rows.append((f"the cut it is in (#{window['cut']})", base + ["--cut", str(window["cut"])]))
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


def add_visualize_flags(subparser: argparse.ArgumentParser) -> None:
    """The flags ``visualize`` had before the combined port (working copy, compare, plan, layers, review)."""
    subparser.add_argument("--published", action="store_true", default=False,
                           help="Visualize the published head, ignoring the unpublished working copy (when one exists).")
    subparser.add_argument("--draft", default=None,
                           help="Which working copy (default: the current one, last checked out).")
    subparser.add_argument("--every-cut", dest="all_cuts", action="store_true", default=False,
                           help="With a working copy and no cut/range selection: every cut, not only the cuts you changed.")
    subparser.add_argument("--compare", dest="compare_with", choices=("published",), default=None,
                           help="With a working copy: the published version and the working copy for the same cuts, labelled.")
    subparser.add_argument("--highlight", default=None, metavar="ADDRESS",
                           help="Outline one layer over the frames (c41.tool-16; implies --layer bounds); "
                                "with no window, the frames are taken inside it.")
    subparser.add_argument("--overlay", action="append", default=None, metavar="LAYER",
                           help="Alias of --layer for frame overlays (bounds: element bounds, face box, safe areas).")
    subparser.add_argument("--size", default=None, metavar="WIDTHxHEIGHT",
                           help="Frame size for the window presets, e.g. 960x540 (alias of --resolution).")
    subparser.add_argument("--plan", action="store_true",
                           help="Resolve the view, window, frame count and page layout without capturing (about a second).")
    subparser.add_argument("--list-layers", dest="list_layers", action="store_true",
                           help="List the registered visualize layers (built-in and pack-contributed) and exit.")
    subparser.add_argument("--no-rules", dest="no_rules", action="store_true",
                           help="motion view: ignore astrid-lint.toml.")
    subparser.add_argument("--render-every", dest="render_every", type=float, default=None,
                           help="With --view contact --render-run RUN: sample the rendered file every N seconds "
                                "(default 5). The review also prints the render's loudness (LUFS, LRA, true peak) and "
                                "its silences (render-review.json).")
    subparser.add_argument("--edited", default=None, metavar="N[,M]",
                           help="diff view: the cuts you meant to edit; a change anywhere else is a SCOPE warning.")
    subparser.add_argument("--no-capture", dest="no_capture", action="store_true",
                           help="diff view: compare the documents only (no before/after tiles).")
    subparser.add_argument("--from", dest="from_revision", default=None,
                           help="diff/compare: the revision before the edit (alias of --before-revision-id).")
    subparser.add_argument("--to", dest="to_revision", default=None,
                           help="diff/compare: the revision after (default: head; alias of --after-revision-id).")


def _layer_names(parsed: argparse.Namespace) -> list[str]:
    names = [
        part.strip()
        for value in (getattr(parsed, "layers", None) or []) + (getattr(parsed, "overlay", None) or [])
        for part in str(value).split(",") if part.strip()
    ]
    return list(dict.fromkeys(names))


def cmd_visualize(parsed: argparse.Namespace) -> int:
    """Resolve what was asked, run it through the one SDK call, and say what came out."""
    from astrid.sdk.contracts import DomainResult, ErrorObject
    from astrid.packs.rendering.actions.timeline_visualize.inspection_contract import inspection_options

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
    if getattr(parsed, "view", None) == "diff" and getattr(parsed, "before_revision_id", None) \
            and not getattr(parsed, "after_revision_id", None):
        head = _head_revision(parsed)
        if head:
            parsed.after_revision_id = head
    try:
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

    working = None
    if validation_error is None and inputs.get("view") != "diff":
        working = _visualize_working_copy(parsed)
    if working is not None:
        from astrid.core.timeline.authoring_bundle import AuthoringBundleError, preview_authoring_candidate
        from astrid.sdk.timeline_checkout import Checkout

        draft = Checkout.load(working["draft"])
        if getattr(parsed, "compare_with", None) == "published":
            # Before, then after: the published version and the working copy, same frames.
            parsed.compare_with = None
            parsed.published = True
            print("── PUBLISHED " + "─" * 60)
            code = cmd_visualize(parsed)
            parsed.published = False
            print("── WORKING COPY " + "─" * 57)
            if code not in (0, None):
                return code
        changed_ids = draft.changed_cut_ids()
        banner = (
            f"WORKING COPY · {_clips_changed(len(draft.edits().get('changes') or []))} vs published "
            f"{_short_rev(working['base_revision'])} · --published for the live version"
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
                    here = next((g["id"] for g in draft.cut_index.groups(draft)
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
            inputs["authoring_preview"] = preview_authoring_candidate(draft.document())
        except AuthoringBundleError as exc:
            print(f"error validation_error: the working copy does not compile ({exc}); run timelines check",
                  file=sys.stderr)
            return 2
        mode = "composed" if mode == "auto" else mode
    if getattr(parsed, "compare_with", None) and not parsed.json:
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
            revision_id=getattr(parsed, "revision_id", None),
        )
    if isinstance(result, DomainResult):
        # Native Runtime views are already typed DomainResults and deliberately have no task/run identity.
        if result.ok:
            outputs = dict(result.data) if isinstance(result.data, Mapping) else {"data": result.data}
            outputs["working_copy"] = working if working is not None else None
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
            human_outputs["exported"] = cli._export_visualization(human_outputs, export_dir)
    if parsed.json or human_outputs is None or not result.ok:
        return print_result(envelope, as_json=parsed.json)
    print(_visualization_summary(human_outputs, parsed=parsed, inputs=inputs))
    if human_outputs.get("exported"):
        exported = human_outputs["exported"]
        print(f"exported: {exported['dir']}  ({len(exported['pages'])} page(s), {len(exported['frames'])} frame(s))")
    return 0


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
