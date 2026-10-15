"""``timelines show``'s editor views: the cut table, ``--as sheet``, one address, ``--summary``, ``--at``.

The working copy is what ``show`` reads unless ``--published`` or an exact revision is asked for. Each view is
a projection of the bundle (astrid.sdk.timeline_cuts / timeline_views / timeline_sheet / timeline_address).
"""
from __future__ import annotations

import argparse
import copy
import json
import re
import shlex
import sys
from collections.abc import Mapping
from typing import Any

from astrid.core.cli.domain_output import DomainResult, print_result
from astrid.packs.timeline.cli_common import clips_changed, short_rev, working_banner, working_copy_view


def show_checkout(parsed: argparse.Namespace, bundle_opener: Any) -> tuple[Any, str] | int:
    """The timeline ``show`` reads, as a Checkout: the working copy (unless --published/--revision-id)
    or the published head; with the banner line that says which. An int is an exit code."""
    from astrid.sdk.timeline_checkout import Checkout, find_draft

    if not getattr(parsed, "published", False) and not getattr(parsed, "revision_id", None):
        try:
            path = find_draft(parsed.project, parsed.ref, getattr(parsed, "draft", None), client=parsed.client)
        except Exception as exc:  # noqa: BLE001 - say so and show the head
            print(f"working copy: not checked ({exc}); showing the published head", file=sys.stderr)
            path = None
        if path is not None:
            tl = Checkout.load(path)
            n = len(tl.edits().get("changes") or [])
            return tl, (f"WORKING COPY · {clips_changed(n)} vs published "
                        f"{short_rev(tl.base_revision)} · --published for the live version")
    opened = bundle_opener(parsed.project, parsed.ref, revision_id=getattr(parsed, "revision_id", None))
    if not opened.ok or not isinstance(opened.data, Mapping):
        return print_result(opened, as_json=False)
    tl = Checkout(dict(opened.data["bundle"]))
    return tl, f"PUBLISHED {short_rev(opened.data.get('revision_id') or (tl.bundle.get('base_parent') or {}).get('revision_id'))}"


def print_sheet(parsed: argparse.Namespace, bundle_opener: Any) -> int:
    """``show --as sheet``: the cut sheet of the working copy (or the published head)."""
    from astrid.sdk.timeline_address import AddressError, resolve
    from astrid.sdk.timeline_sheet import SheetError, moment_range, range_cuts, render_sheet

    got = show_checkout(parsed, bundle_opener)
    if isinstance(got, int):
        return got
    tl, banner = got
    start = end = None
    cuts = None
    try:
        if parsed.range and "," in parsed.range:  # a list (or repeated --range): exactly those cuts
            cuts = range_cuts(tl, parsed.range)
        elif parsed.range:
            start, end = moment_range(tl, parsed.range)
        elif getattr(parsed, "at", None):
            # --at scopes the sheet to the cut on screen then
            target = resolve(tl, parsed.at, prefer="time")
            spans = tl._cut_spans()
            t = target.start
            cut_id = next((cid for cid, (lo, hi) in spans.items() if lo - 1e-6 <= t < (hi if hi is not None else tl.duration + 1)), None)
            if cut_id is None:
                raise AddressError(f"no cut is on screen at {t:.2f} s")
            start, end = spans[cut_id][0], spans[cut_id][1] or tl.duration
            print(f"--at {parsed.at!r}: {target} · in {cut_id}", file=sys.stderr)
    except (SheetError, AddressError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(render_sheet(tl, start=start, end=end, banner=banner, film=str(parsed.project), cuts=cuts,
                       detail=bool(getattr(parsed, "detail", False))), end="")
    where = f"{parsed.ref} --project {parsed.project}"
    # a hint only on a terminal (to stderr): a redirected sheet stays exactly the sheet, even with 2>&1
    if sys.stdout.isatty():
        print(f"next: save it (> FILE), change a line, then  timelines apply {where} FILE", file=sys.stderr)
    return 0


def print_address(parsed: argparse.Namespace, bundle_opener: Any) -> int:
    """``show TL ADDRESS``: the complete record of one thing (a layer, a word, a cut, a time)."""
    from astrid.sdk.timeline_address import AddressError, describe_target, resolve

    got = show_checkout(parsed, bundle_opener)
    if isinstance(got, int):
        return got
    tl, banner = got
    addresses = [parsed.address] + list(getattr(parsed, "more_addresses", None) or [])
    targets = []
    for address in addresses:
        try:
            targets.append(resolve(tl, address, prefer="thing"))
        except AddressError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
    print(banner)
    for k, target in enumerate(targets):
        timeline_ref = str(parsed.ref or tl.bundle.get("timeline_id") or "TIMELINE")
        project_ref = str(parsed.project or tl.bundle.get("project_id") or "PROJECT")
        text = describe_target(tl, target, timeline=timeline_ref, project=project_ref,
                               full=bool(getattr(parsed, "full", False)), keys=getattr(parsed, "keys", None))
        print(text if k == len(targets) - 1 else text.split("\nnext:")[0])
    return 0


def at_detail(bundle: Mapping[str, Any], seconds: float) -> str:
    """``show --at T --detail``: each clip on screen at T once, with its moments and every param untruncated."""
    from astrid.sdk import timeline_intent as intent
    from astrid.sdk.timeline_checkout import Checkout

    tl = Checkout(copy.deepcopy(dict(bundle)))
    lines = [f"on screen at {seconds:.3f} s (every param, untruncated):"]
    for clip in tl.clips():
        if clip.is_audio or not (clip.start - 1e-6 <= seconds < clip.end - 1e-6):
            continue
        when = " ".join(x for x in (f"on {intent.on(clip.data)}" if intent.on(clip.data) else "",
                                     f"until {intent.until(clip.data)}" if intent.until(clip.data) else "") if x)
        params = json.dumps(clip.data.get("params") or {}, ensure_ascii=False, separators=(", ", ": "))
        lines.append(f"  {clip.address:<18} {clip.element:<14} {clip.asset or '':<14} {clip.start:7.3f}–{clip.end:.3f} s  {when}")
        lines.append(f"      {params}")
    return "\n".join(lines)


def print_cut_table(parsed: argparse.Namespace, bundle_opener: Any) -> int:
    """Print the cut table for ``timelines show`` (human default, or the complete --json)."""
    from astrid.sdk.timeline_cuts import (
        build_cut_table,
        cut_table_payload,
        filter_rows,
        page_rows,
        paging_hint,
        parse_seconds,
        render_cut_table,
        render_summary,
        resolve_shot,
    )
    from astrid.sdk.timeline_views import render_at, render_clips

    opened = bundle_opener(parsed.project, parsed.ref, revision_id=getattr(parsed, "revision_id", None))
    if not opened.ok or not isinstance(opened.data, Mapping):
        return print_result(opened, as_json=bool(parsed.json))
    data = opened.data
    # The working copy (unpublished draft) is what show reads unless --published or an exact revision is asked for.
    working = None
    if not getattr(parsed, "published", False) and not getattr(parsed, "revision_id", None):
        working = working_copy_view(parsed)
    bundle = working["bundle"] if working else data["bundle"]
    table = build_cut_table(bundle)
    changes = working["changes"] if working else []
    changed = {str(change.get("clip_id")) for change in changes if change.get("clip_id")}
    working_info = None if working is None else {
        "draft": working["path"], "base_revision": working["base_revision"], "edits": len(changes),
    }
    banner: list[str] = []
    if working_info is not None:
        banner = [
            f"WORKING COPY · {clips_changed(len(changes))} vs published {working['base_revision']} · "
            "--published for the live version"
        ]
    if parsed.json:
        payload = cut_table_payload(bundle, table)
        payload.update({
            "project_id": data.get("project_id"),
            "timeline_id": data.get("timeline_id"),
            "revision_id": data.get("revision_id"),
            "is_current_head": data.get("is_current_head"),
            "head_revision_id": data.get("head_revision_id"),
            "working_copy": working_info,
        })
        return print_result(
            DomainResult.success(payload, receipt=opened.receipt, idempotency_key=opened.idempotency_key),
            as_json=True,
        )
    revision = str(data.get("revision_id") or "")
    state = "current head" if data.get("is_current_head") else f"saved revision (current head is {data.get('head_revision_id')})"
    title = f"Timeline {data.get('timeline_id')} · project {parsed.project or data.get('project_id')} · {state}\n  revision {revision}"
    project = str(parsed.project or data.get("project_id"))
    timeline = str(parsed.ref or data.get("timeline_id"))
    show_base = ["python3", "-m", "astrid", "timelines", "show", timeline, "--project", project]

    if getattr(parsed, "at", None):
        from astrid.sdk.timeline_address import AddressError, resolve
        from astrid.sdk.timeline_checkout import Checkout

        try:
            seconds = parse_seconds(parsed.at)
            at_note = None
        except ValueError:
            try:
                target = resolve(Checkout(copy.deepcopy(dict(bundle))), parsed.at, prefer="time")
            except AddressError as exc:
                print(f"error: {exc}")
                return 2
            seconds, at_note = target.start, f"--at {parsed.at!r}: {target}"
        text = render_at(bundle, table, seconds, changed=changed, show_command=shlex.join(show_base))
        if text is not None and at_note:
            text = at_note + "\n" + text
        if text is not None and getattr(parsed, "detail", False):
            text += "\n" + at_detail(bundle, seconds)
        if text is None:
            print(f"error validation_error: no cut at {seconds:.2f} s (the timeline runs {table['duration']:.2f} s)")
            return 2
        print("\n".join(banner + [title, text]))
        return 0
    if getattr(parsed, "clips", False):
        if not getattr(parsed, "shot", None):
            print("error validation_error: --clips needs --shot N (one chapter)")
            return 2
        try:
            shot_ids = resolve_shot(table, parsed.shot)
        except ValueError as exc:
            print(f"error validation_error: {exc}")
            return 2
        print("\n".join(banner + [title, render_clips(bundle, shot_ids, changed=changed)]))
        return 0
    if getattr(parsed, "summary", False):
        lines = banner + [title, "", render_summary(table)]
        if changes:
            lines += ["", f"unpublished: {clips_changed(len(changes))}"]
            for change in changes:
                lines.append(f"  ✎ {change.get('kind')} {change.get('clip_id')} "
                             f"({', '.join(change.get('fields') or []) or '-'}) at {change.get('after') or change.get('before')}")
        lines += ["", f"full table: {shlex.join(show_base)}   (no flag)  ·  one chapter: --shot N   ·  clips in a chapter: --shot N --clips"]
        print("\n".join(lines))
        return 0
    range_value = getattr(parsed, "range", None)
    provenance_target = None
    if range_value and not re.fullmatch(r"\s*[\d.:]+\s*\.\.\s*[\d.:]+\s*", str(range_value)):
        # a cut (c30), cut range (c30..c31, inclusive), words ("a".."b") or a layer: the same addresses as everywhere
        from astrid.sdk.timeline_address import AddressError, resolve
        from astrid.sdk.timeline_checkout import Checkout

        try:
            target = resolve(Checkout(copy.deepcopy(dict(bundle))), range_value, prefer="time")
        except AddressError as exc:
            print(f"error: {exc}")
            return 2
        range_value = f"{target.start:.3f}..{max(target.end, target.start + 1e-3):.3f}"
        provenance_target = target
    try:
        rows = filter_rows(table, range_value=range_value, shot=getattr(parsed, "shot", None))
    except ValueError as exc:
        print(f"error validation_error: {exc}")
        return 2
    view = getattr(parsed, "as_view", None) or "cuts"
    if view != "cuts":
        print("\n".join(banner + [render_view(view, bundle, parsed, rows, title=title, changed=changed)]))
        return 0
    try:
        shown, info = page_rows(rows, page=getattr(parsed, "page", None), page_size=getattr(parsed, "page_size", None))
    except ValueError as exc:
        print(f"error validation_error: {exc}")
        return 2
    lines = banner + [render_cut_table(table, shown, title=title, changed_clip_ids=changed), ""]
    if info is not None:
        lines.insert(len(lines) - 1, paging_hint(table, rows, shown, info))
    visual = ["python3", "-m", "astrid", "timelines", "visualize", timeline, "--project", project]
    if not data.get("is_current_head"):
        visual += ["--revision-id", revision]
    if shown and (getattr(parsed, "range", None) or getattr(parsed, "shot", None) or info is not None):
        low, high = shown[0]["start"], shown[-1]["end"]
        lines.append("see these cuts: " + shlex.join(visual + ["--view", "contact", "--range", f"{low:.2f}..{high:.2f}"]))
    else:
        lines.append("see the whole video: " + shlex.join(visual + ["--view", "contact"]))
    lines.append("one moment: " + shlex.join(show_base) + " --at 1:02   ·   one chapter: --shot N --clips   ·   overview: --summary")
    lines.append("one moment on screen: " + shlex.join(visual) + " --at SECONDS   ·   every N frames: --range A..B --every-frames N")
    lines.append("how one cut moves: " + shlex.join(visual + ["--view", "motion", "--cut", "N"])
                 + "   ·   lint: " + shlex.join(["python3", "-m", "astrid", "timelines", "lint", timeline, "--project", project]))
    from astrid.packs.timeline.cli import _editing_navigation

    edit = _editing_navigation(project=project, timeline=timeline)
    lines.append(f"edit: {edit['commands']['checkout']}   (guide: {edit['guide']})")
    lines.append("per-track layer rows: --layers · SDK envelope (complete): --json")
    if provenance_target is not None and getattr(provenance_target, "clip", None) is not None:
        from astrid.sdk.timeline_views import render_asset_provenance

        provenance = render_asset_provenance(provenance_target.clip)
        if provenance:
            lines.append(provenance)
    print("\n".join(lines))
    return 0


def render_view(
    view: str, bundle: Mapping[str, Any], parsed: argparse.Namespace, rows: list, *, title: str,
    changed: set[str] | None = None,
) -> str:
    """The script or code representation, scoped like the cut table (--range, --shot)."""
    from astrid.sdk.timeline_cuts import _parse_range
    from astrid.sdk.timeline_views import render_code, render_script

    window = _parse_range(parsed.range) if getattr(parsed, "range", None) else None
    shot_ids = {row["shot_id"] for row in rows} if getattr(parsed, "shot", None) else None
    if view == "script":
        return title + "\n" + render_script(bundle, window=window, shot_ids=shot_ids, changed=changed)
    return render_code(bundle, window=window, shot_ids=shot_ids, header="# " + title.replace("\n  ", "\n# "),
                       full=bool(getattr(parsed, "detail", False)))


class JoinRanges(argparse.Action):
    """``--range A --range B`` = ``A,B`` (never silently the last one)."""

    def __call__(self, parser: Any, namespace: Any, values: Any, option_string: Any = None) -> None:
        current = getattr(namespace, self.dest, None)
        setattr(namespace, self.dest, f"{current},{values}" if current else values)


