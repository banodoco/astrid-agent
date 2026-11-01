"""The working-copy verbs other than edit: lint, lines, words, discard and duplicate.

``lint`` runs every registered check over the picture cuts (the working copy by default); ``lines`` and ``words``
read the narration; ``discard`` drops a working copy; ``duplicate`` copies a timeline for a trial.
Each handler is argument parsing plus one SDK call; the logic lives in astrid.sdk.timeline_*.
"""
from __future__ import annotations

import argparse
import copy
import json
import re
import shlex
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from astrid.core.cli.domain_output import print_result
from astrid.packs.timeline.cli_common import (
    VerbError, clips_changed, draft_name, guard, need_project, working_banner, working_copy, working_copy_view,
)


def _cmd_lint(parsed: argparse.Namespace) -> int:
    """Every registered check (built-in and pack) over every picture cut, from timeline data (no capture)."""
    from astrid.packs.timeline.cli_lint_header import print_broken_header

    print_broken_header("check")  # a check that could not load is named first, never silently absent
    from astrid.core.timeline.cuts import (
        bundle_fps,
        find_cut,
        occurrences_from_bundle,
        picture_cuts,
    )
    from astrid.packs.rendering.actions.timeline_visualize.layers.base import check_help
    from astrid.packs.rendering.actions.timeline_visualize.motion import model
    from astrid.packs.rendering.actions.timeline_visualize.motion.conditions import run_checks
    from astrid.packs.rendering.actions.timeline_visualize.motion.rules import resolve_rules

    try:
        rules = resolve_rules(getattr(parsed, "rules", None), search=not getattr(parsed, "no_rules", False))
    except (OSError, ValueError) as exc:
        print(f"error validation_error: {exc}", file=sys.stderr)
        return 2
    if getattr(parsed, "list_checks", False):
        print(check_help(rules))
        return 0
    opener = getattr(parsed.client.timelines, "open_bundle", None)
    if not callable(opener):
        print("error unavailable: this client cannot open a timeline bundle", file=sys.stderr)
        return 1
    opened = opener(parsed.project, parsed.ref, revision_id=getattr(parsed, "revision_id", None))
    if not opened.ok or not isinstance(opened.data, Mapping):
        return print_result(opened, as_json=parsed.json)
    bundle = opened.data["bundle"]
    working = None
    if parsed.ref and not getattr(parsed, "published", False) and not getattr(parsed, "revision_id", None):
        working = working_copy_view(parsed)
    if working is not None:
        bundle = working["bundle"]
    fps = bundle_fps(bundle)
    occurrences = occurrences_from_bundle(bundle)
    all_cuts = picture_cuts(occurrences, fps=fps)
    cuts = all_cuts
    try:
        if getattr(parsed, "cut", None):
            cuts = [find_cut(all_cuts, parsed.cut)]
        elif getattr(parsed, "range", None):
            from astrid.sdk.timeline_cuts import _parse_range

            low, high = _parse_range(parsed.range)
            cuts = [cut for cut in all_cuts if cut["end"] > low and cut["start"] < high]
        beats = json.loads(_read_beats(parsed.beats)) if getattr(parsed, "beats", None) else None
    except (OSError, ValueError) as exc:
        print(f"error validation_error: {exc}", file=sys.stderr)
        return 2
    elements = model.elements_from_occurrences(occurrences)
    track_order = next((o.get("track_order") for o in occurrences if o.get("track_order")), [])
    params = dict((rules or {}).get("params") or {})
    if getattr(parsed, "min_text_px", None) is not None:
        params["min_text_px"] = float(parsed.min_text_px)
    results, timeline_findings = run_checks(
        cuts, elements, fps, track_order=track_order, beats=beats, params=params,
        severity=(rules or {}).get("severity"), disable=(rules or {}).get("disable") or (), all_cuts=all_cuts,
    )
    if parsed.json:
        rows = [{**f.as_dict(), "cut_start": cut["start"], "cut_end": cut["end"]}
                for cut, findings in results for f in findings]
        rows += [f.as_dict() for f in timeline_findings]
        print(json.dumps({"ok": True, "data": {"timeline_id": opened.data.get("timeline_id"),
                                                "revision_id": opened.data.get("revision_id"),
                                                "working_copy": working is not None,
                                                "rules": (rules or {}).get("path"), "findings": rows},
                          "error": None}, indent=2))
        return 0
    counts: dict[str, int] = {}
    info: dict[str, int] = {}
    if rules:
        effective = ", ".join(f"{k}={v:g}" for k, v in sorted(rules["params"].items()))
        severities = ", ".join(f"{k}={v}" for k, v in sorted(rules["severity"].items()))
        rules_line = (f"rules: {rules['path']} ({effective or 'no thresholds'}"
                      + (f"; severity {severities}" if severities else "") + ")")
    else:
        rules_line = ("rules: built-in defaults (no astrid-lint.toml in this folder or its parents; "
                      "--rules FILE to name one; --list-checks for the keys)")
    lines = [
        f"Timeline {opened.data.get('timeline_id')} · {len(results)} cut(s) · revision {opened.data.get('revision_id')}",
        rules_line,
    ]
    if working is not None:
        lines.insert(0, working_banner(working))
    rank = {"error": 0, "warn": 1, "info": 2}
    from astrid.sdk.timeline_address import cut_ids_by_ordinal, name_things, ordinals_to_ids
    from astrid.sdk.timeline_checkout import Checkout

    named = Checkout(copy.deepcopy(dict(bundle)))
    ordinals = cut_ids_by_ordinal(named)
    # With a working copy, lint shows what is NEW since your checkout; --all shows everything.
    known: set[str] = set()
    if working is not None and not parsed.all:
        base_bundle = opened.data["bundle"]
        base_occurrences = occurrences_from_bundle(base_bundle)
        base_cuts = picture_cuts(base_occurrences, fps=fps)
        base_results, base_timeline = run_checks(
            base_cuts, model.elements_from_occurrences(base_occurrences), fps, track_order=track_order, beats=beats,
            params=params, severity=(rules or {}).get("severity"), disable=(rules or {}).get("disable") or (),
            all_cuts=base_cuts,
        )
        known = {f.line(c["start"]) for c, fs in base_results for f in fs} | {f.line(None) for f in base_timeline}
    already = 0
    only = {t.strip().upper() for t in str(getattr(parsed, "only", None) or "").split(",") if t.strip()}
    seen_now: list[str] = []

    def emit(finding, start=None, end=None) -> None:
        nonlocal already
        if only and finding.code.upper() not in only:
            return
        text = finding.line(start)
        seen_now.append(text)
        if text in known:
            already += 1
            return
        bucket = info if finding.severity == "info" else counts
        bucket[finding.code] = bucket.get(finding.code, 0) + 1
        if finding.severity != "info" or parsed.all:
            prefix = "ERROR " if finding.severity == "error" else ""
            shown = name_things(text, named, start, end) if start is not None and end is not None else text
            lines.append(prefix + ordinals_to_ids(shown, named, ordinals))

    for finding in sorted(timeline_findings, key=lambda f: rank.get(f.severity, 3)):
        emit(finding)
    from astrid.packs.rendering.actions.timeline_visualize.layers.base import checks as registered_checks

    ran = sorted(name for name in registered_checks() if name not in set((rules or {}).get("disable") or ())
                 and "frames" not in registered_checks()[name].needs)
    lines.append(f"checks run: {', '.join(ran)} (a check with no line passed or is off: see --list-checks)")
    for cut, findings in results:
        for finding in findings:
            emit(finding, cut["start"], cut["end"])
    summary = ", ".join(f"{code} {n}" for code, n in sorted(counts.items(), key=lambda item: -item[1])) or "none"
    if working is not None and not parsed.all:
        from collections import Counter

        from astrid.sdk.timeline_checkout import _lint_key

        now_keys = Counter(_lint_key(t) for t in seen_now)
        resolved = []
        for text in sorted(known):
            if only and text.split(" ", 1)[0].upper() not in only:
                continue
            if now_keys[_lint_key(text)] > 0:
                now_keys[_lint_key(text)] -= 1
            else:
                resolved.append(text)
        lines.append(f"(new since your checkout only; {already} finding(s) were already in the published version: --all shows everything)")
        if resolved:
            lines.append(f"resolved since checkout: {len(resolved)} — " + "; ".join(
                ordinals_to_ids(r, named, ordinals)[:70] for r in resolved[:3]) + (" …" if len(resolved) > 3 else ""))
    lines.append(f"findings: {summary}" + (
        f"; info hidden ({', '.join(f'{c} {n}' for c, n in sorted(info.items()))}; --all shows them)" if info and not parsed.all else ""))
    project = str(parsed.project or "<project>")
    lines.append("see one: " + shlex.join(["python3", "-m", "astrid", "timelines", "visualize", str(parsed.ref), "--project", project,
                                           "--view", "motion", "--cut", "N"])
                 + "   ·   machine-applicable fixes: --json")
    print("\n".join(lines))
    errors = sum(1 for _cut, fs in results for f in fs if f.severity == "error") + sum(
        1 for f in timeline_findings if f.severity == "error")
    return 1 if errors and getattr(parsed, "strict", False) else 0


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


def _timeline_args(subparser: argparse.ArgumentParser, *, required_timeline: bool = True, project_required: bool = True) -> None:
    from astrid.packs.timeline.cli import _add_project_arg

    if required_timeline:
        subparser.add_argument("ref", help="Timeline UUID, ULID, or slug.")
    else:
        subparser.add_argument("ref", nargs="?", default=None, help="Timeline UUID, ULID, or slug (or use --file).")
    _add_project_arg(subparser, required=project_required)
    subparser.add_argument("--draft", default=None, help="Working copy name (default: current working copy).")


def _configure_transcripts_sync(subparser: argparse.ArgumentParser) -> None:
    from astrid.packs.timeline.cli import _add_project_arg

    subparser.add_argument("ref", help="Timeline UUID, ULID, or slug.")
    _add_project_arg(subparser, required=True)
    subparser.add_argument("--dry-run", action="store_true", help="Print create/update/unchanged and skips without writing documents.")
    subparser.set_defaults(handler=_cmd_transcripts_sync)


@guard(2)
def _cmd_transcripts_sync(parsed: argparse.Namespace) -> int:
    from astrid.sdk.timeline_checkout import Checkout
    from astrid.sdk.timeline_transcripts import sync_narration_transcripts

    tl = Checkout.open(need_project(parsed), parsed.ref, client=parsed.client)
    report = sync_narration_transcripts(tl, parsed.client, str(tl.bundle["project_id"]), dry_run=parsed.dry_run)
    print(f"{'dry run: ' if parsed.dry_run else ''}{len(report)} narration transcript item(s)")
    for item in report:
        print(f"  {item['status']}: {item['label']}" + (f" ({item['reason']})" if item.get("reason") else ""))
    return 0


def _configure_lint(subparser: argparse.ArgumentParser) -> None:
    from astrid.packs.timeline.cli import _add_json_flag, _add_project_arg

    subparser.description = (
        "Run every registered check (built-in and pack-provided) over every picture cut, from timeline data "
        "alone (about a second, no capture): faces covered, text off-frame/outside title-safe/too small, layers "
        "covering each other, accents off their word, sfx off their visual, short/long cuts, holds, presenter "
        "share, VO level jumps. Thresholds come from astrid-lint.toml (or --rules). Each line names the fix; "
        "--json carries machine-applicable fixes."
    )
    _add_project_arg(subparser, required=False)
    subparser.add_argument("ref", nargs="?", default=None, help="Timeline UUID, ULID, or slug (default: the project's).")
    subparser.add_argument("--cut", default=None, help="Only this cut (number in timelines show, clip id, or @SECONDS).")
    subparser.add_argument("--range", dest="range", default=None, help="Only cuts overlapping START..END seconds.")
    subparser.add_argument("--revision-id", default=None, help="Lint a saved revision instead of the head.")
    subparser.add_argument("--beats", default=None, metavar="BEATS_JSON",
                           help="Override the beats the music clips carry (app.beats, written by the EDL builder) "
                                "with a cue's beats.json. Beats add accents 0.10–0.20 s off a music hit or downbeat.")
    subparser.add_argument("--min-text-px", dest="min_text_px", type=float, default=None,
                           help="Smallest acceptable text size in px at 1080p (default 32, or the rules file's).")
    subparser.add_argument("--rules", default=None, metavar="FILE",
                           help="Project rules (thresholds/severities) TOML; default: the nearest astrid-lint.toml "
                                "from the working directory upwards.")
    subparser.add_argument("--no-rules", dest="no_rules", action="store_true",
                           help="Ignore any astrid-lint.toml and use the checks' defaults.")
    subparser.add_argument("--list-checks", dest="list_checks", action="store_true",
                           help="List every registered check (built-in and pack), its threshold keys and defaults.")
    subparser.add_argument("--strict", action="store_true", help="Exit 1 when any finding has severity error.")
    subparser.add_argument("--all", action="store_true", help="Also print info lines (EDGE crops, BEAT near-misses).")
    subparser.add_argument("--only", default=None, metavar="TAGS", help="Only these findings, e.g. --only SAFE,FACE.")
    _add_json_flag(subparser, default=False)
    subparser.add_argument("--published", action="store_true",
                           help="Lint the published head, not the working copy (when one exists).")
    subparser.add_argument("--draft", default=None, help="Which working copy (default: the current one, last checked out).")
    subparser.set_defaults(handler=_cmd_lint)


@guard(2)
def _cmd_words(parsed: argparse.Namespace) -> int:
    from astrid.sdk.timeline_checkout import Checkout

    if parsed.file:
        if parsed.ref and not getattr(parsed, "word", None):  # `words --file F WORD`: no timeline to name
            parsed.word, parsed.ref = parsed.ref, None
        tl = Checkout.load(parsed.file)
    elif parsed.published:
        tl = Checkout.open(need_project(parsed), parsed.ref, client=parsed.client)
    else:
        tl, _existed = working_copy(parsed, create=True)
    if getattr(parsed, "word", None):
        return _print_word(tl, parsed)
    between = None
    if parsed.range:
        lo, hi = _parse_range(parsed.range)
        between = (tl.time(lo), tl.time(hi))
    words = tl.words(between=between)
    if parsed.find:
        needle = parsed.find.strip().lower()
        words = [w for w in words if needle in w.text.lower()]
    spans = sorted(((lo, hi if hi is not None else tl.duration, cid) for cid, (lo, hi) in tl._cut_spans().items()),
                   key=lambda row: row[0])
    cuts = tl.cuts if not spans else []  # a timeline without cut ids: its cuts by position
    for w in words:  # the cut ids the sheet and show print (c21), never positions
        cut = next((cid for lo, hi, cid in reversed(spans) if lo - 1e-6 <= w.start < hi - 1e-6), None) \
            or next((f"cut {c.n}" for c in cuts if c.start - 1e-6 <= w.start < c.end - 1e-6), "-")
        print(f"  {w.start:.3f}–{w.end:.3f}  {w.text}  [{w.id}]  {cut}")
    print(f'next: timelines edit {parsed.ref or "<timeline>"} --project {parsed.project or "<project>"} '
          f"--clip QUERY --at-word WORD")
    return 0


def _parse_range(value: str) -> tuple[str, str]:
    if "," in value:
        raise VerbError(f"--range {value!r}: one window here (a list of ranges works with show --as sheet)", 2)
    lo, sep, hi = value.partition("..")
    if not sep or not lo or not hi:
        raise VerbError("--range takes START..END (seconds or m:ss)", 2)
    return lo, hi


def _print_word(tl: Any, parsed: argparse.Namespace) -> int:
    """``timelines words TL Live``: the word's record, the clips anchored to it, and (--beats) the beats near it."""
    from astrid.core.timeline import moments as mo
    from astrid.sdk import timeline_intent as intent
    from astrid.sdk.timeline_address import AddressError, describe_target, resolve

    try:
        target = resolve(tl, parsed.word, prefer="time")
    except AddressError as exc:
        raise VerbError(str(exc), 2) from None
    print(describe_target(tl, target, timeline=str(parsed.ref), project=str(parsed.project)).split("\nnext:")[0])
    if target.kind == "word":
        word = target.word
        anchored = []
        for clip in tl.clips():
            for field, text in (("on", intent.on(clip.data)), ("until", intent.until(clip.data))):
                if not text:
                    continue
                try:
                    moment = mo.parse(text)
                except mo.MomentError:
                    continue
                base = moment.base if moment.kind in ("beat", "downbeat") else moment
                if base is None or base.kind != "word":
                    continue
                hits = mo.find_words(base, tl.words())
                if base.n is not None:
                    hits = hits[base.n - 1:base.n]
                if any(run[0].segment == word.segment and run[0].index <= word.index <= run[-1].index for run in hits):
                    anchored.append(f"{clip.address} {field} {text}")
        print("  anchored to it: " + (", ".join(anchored) if anchored else "nothing"))
    if getattr(parsed, "beats", False):
        from astrid.sdk.timeline_checkout import _MomentContext

        ctx = _MomentContext(tl, None)
        beats, downs = ctx.beats("beat"), set(round(b, 3) for b in ctx.beats("downbeat"))
        t = base = target.start
        if target.kind == "word":  # count exactly as the resolver does: from the word itself, not its frame
            try:
                base = mo.resolve(mo.parse(target.address), ctx)
            except mo.MomentError:
                pass
        near = 0.5 / tl.fps
        after = [b for b in beats if b > base + near]
        grids = sorted({intent.beats_label(c.data) for c in tl.clips(audio=True) if intent.beat_sources(c.data)} - {None})
        print(f"  beats near {t:.2f} s" + (f" (grid: {', '.join(grids)})" if grids else " (no music clip has a beat grid)") + ":")
        for b in [b for b in beats if t - 1.0 <= b <= t + 2.5]:
            n = after.index(b) + 1 if b in after else None
            label = f"beat {n} after {target.address}" if n else ("on it (its own frame)" if abs(b - base) <= near else "before it")
            print(f"    {b:7.3f} s  {'downbeat' if round(b, 3) in downs else 'beat':<8}  {label}")
    return 0


def _configure_words(subparser: argparse.ArgumentParser) -> None:
    _timeline_args(subparser, required_timeline=False, project_required=False)
    subparser.add_argument("word", nargs="?", default=None,
                           help='One word: its scoped form, time, cut, what is on screen and what is anchored to it '
                                '(Live, \'"Building" in v27\'); a word said more than once lists its scoped forms.')
    subparser.add_argument("--beats", action="store_true", help="With a word: the music beats near it, with their moments.")
    subparser.add_argument("--file", default=None, help="Read a local checkout file instead.")
    subparser.add_argument("--published", action="store_true", help="Read the published head, not the working copy.")
    subparser.add_argument("--find", default=None, help="Only words containing this text.")
    subparser.add_argument("--range", default=None, metavar="A..B", help="Only words between two times (seconds or m:ss).")
    subparser.set_defaults(handler=_cmd_words)


def _configure_lines(subparser: argparse.ArgumentParser) -> None:
    subparser.description = "The narration line by line: id, when it is spoken, the silence after it, its text and take."
    _timeline_args(subparser, project_required=False)
    subparser.add_argument("--published", action="store_true", help="Read the published head, not the working copy.")
    subparser.add_argument("--line", default=None, metavar="ID", help="One line in full: its text, take, span, gap, "
                                                                        "every word with its time, and the cuts it is under.")
    subparser.set_defaults(handler=_cmd_lines)


@guard(2)
def _cmd_lines(parsed: argparse.Namespace) -> int:
    from astrid.sdk.timeline_checkout import Checkout

    if parsed.published:
        tl = Checkout.open(need_project(parsed), parsed.ref, client=parsed.client)
    else:
        tl, _existed = working_copy(parsed, create=True)
    lines = tl.lines()
    if getattr(parsed, "line", None):
        return _print_line(tl, parsed)
    width = max([len(v.segment) for v in lines] + [4])
    print(f"{len(lines)} lines · film {tl.duration:.2f} s" + (" · working copy" if not parsed.published else " · published"))
    for v in lines:
        words = v.words
        start, end = (words[0].start, words[-1].end) if words else (v.clips[0].start, v.clips[-1].end)
        gap = v.gap_after
        text = v.text if len(v.text) <= 70 else v.text[:69] + "…"
        print(f"  {v.segment:<{width}}  {start:7.2f}–{end:7.2f} s  gap {('%.2f' % gap) if gap is not None else '  - '}  "
              f"take {v.clips[0].asset or '?':<12} \"{text}\"")
    print(f"next: timelines words {parsed.ref} --project {parsed.project} WORD   ·   "
          f"timelines edit {parsed.ref} --project {parsed.project} --line ID --take WAV --words WORDS.json")
    return 0


def _print_line(tl: Any, parsed: argparse.Namespace) -> int:
    """``lines TL --line w23c``: one narration line in full."""
    from astrid.sdk.timeline_address import _cut_at

    voice = tl.voice(parsed.line)
    words = voice.words
    start, end = (words[0].start, words[-1].end) if words else (voice.clips[0].start, voice.clips[-1].end)
    gap = voice.gap_after
    cuts = list(dict.fromkeys(c for c in (_cut_at(tl, w.start) for w in words) if c))
    print(f"{voice.segment} · {start:.3f}–{end:.3f} s · gap after {('%.2f s' % gap) if gap is not None else '-'} · "
          f"take {voice.clips[0].asset or '?'} · under {', '.join(cuts) or 'no cut'}")
    print(f'  "{voice.text}"')
    for w in words:
        print(f"  {w.start:8.3f}–{w.end:.3f}  {w.text:<16} [{w.id}]  {_cut_at(tl, w.start) or '-'}")
    print(f"next: timelines edit {parsed.ref} --project {parsed.project} --line {voice.segment} --take WAV --words WORDS.json"
          f"   ·   --gap-after {voice.segment}=SECONDS")
    return 0


@guard(1)
def _cmd_discard(parsed: argparse.Namespace) -> int:
    tl, _ = working_copy(parsed, create=False)
    name = draft_name(parsed)
    if tl is None:
        print(f"no working copy \"{name}\" to discard · next: timelines checkout {parsed.ref} --project {parsed.project}")
        return 0
    dropped = len(tl.edits().get("changes") or [])
    tl.discard()
    print(f'discarded working copy "{name}" of {parsed.ref} ({clips_changed(dropped)}, dropped)')
    print(f"next: timelines checkout {parsed.ref} --project {parsed.project}")
    return 0


@guard(1)
def _cmd_duplicate(parsed: argparse.Namespace) -> int:
    from astrid.sdk.timeline_duplicate import duplicate_timeline

    result = duplicate_timeline(parsed.project, parsed.ref, slug=parsed.slug, name=parsed.name,
                                revision_id=parsed.revision, client=parsed.client)
    print(f"duplicated {parsed.ref} → {result['timeline_id']} ({result['shots']} shots) · head {result['new_head']}")
    for note in result["notes"]:
        print(f"note: {note}")
    print(f"next: timelines show {result['timeline_id']} --project {parsed.project}")
    return 0


@guard(1)
def _cmd_restructure(parsed: argparse.Namespace) -> int:
    """Store each chapter as a shot and the music / wall-crossing clips on the film, after proving the render input is equal."""
    from astrid.sdk.timeline_checkout import Checkout
    from astrid.sdk.timeline_restructure import check_restructure

    tl = Checkout.open(need_project(parsed), parsed.ref, client=parsed.client)
    report, problems, _after = check_restructure(tl)
    for line in report:
        print(line)
    if problems:
        print("restructure REFUSED: the renderer would receive something different:")
        for line in problems:
            print(f"  {line}")
        return 1
    print("equivalence: the renderer input before and after is identical (clips, order, tracks, duration)")
    if parsed.dry_run:
        print(f"dry run: nothing published · next: timelines restructure {parsed.ref} --project {parsed.project}")
        return 0
    if parsed.keep_original:
        from astrid.sdk.timeline_duplicate import duplicate_timeline

        kept = duplicate_timeline(parsed.project, parsed.ref, name=parsed.keep_original, client=parsed.client)
        print(f"kept original head as {kept['timeline_id']} ({parsed.keep_original})")
    receipt = tl.publish(parsed.message or "restructure: chapters as shots, music and wall-crossing clips on the film",
                         client=parsed.client, restructure=True)
    print(f"restructured {parsed.ref} · head {receipt.get('new_head')}"
          + (f" · narration pinned for {len(receipt.get('narration_pinned') or [])} shot(s)" if receipt.get("narration_pinned") else ""))
    if receipt.get("narration_error"):
        print(f"note: {receipt['narration_error']}")
    if parsed.name:
        renamed = parsed.client.timelines.rename(parsed.project, parsed.ref, parsed.name)
        if not renamed.ok:
            return print_result(renamed, as_json=False)
        print(f"named {parsed.ref} → {renamed.data['name']}")
    return 0


def _configure_restructure(subparser: argparse.ArgumentParser) -> None:
    subparser.description = (
        "Store each chapter (from the chapter labels) as a real shot, and the music and any clip crossing a chapter "
        "wall as film-level clips. Editing is unchanged (one flat timeline in film seconds). The change is published only "
        "if the renderer input before and after is identical; --dry-run prints the walls and what goes where."
    )
    from astrid.packs.timeline.cli import _add_project_arg

    subparser.add_argument("ref", help="Timeline (UUID, ULID or slug).")
    _add_project_arg(subparser)
    subparser.add_argument("--dry-run", action="store_true", help="Print the plan and the equivalence check; publish nothing.")
    subparser.add_argument("--name", default=None, help="Display name for the restructured timeline.")
    subparser.add_argument("--keep-original", metavar="NAME", default=None,
                           help="Duplicate the current head under this name before restructuring.")
    subparser.add_argument("-m", "--message", default=None, help="Revision message.")
    subparser.set_defaults(handler=_cmd_restructure)


def _configure_discard(subparser: argparse.ArgumentParser) -> None:
    subparser.description = "Drop a working copy (its unpublished edits) without publishing."
    _timeline_args(subparser, project_required=False)
    subparser.set_defaults(handler=_cmd_discard)


def _configure_duplicate(subparser: argparse.ArgumentParser) -> None:
    subparser.description = (
        "Create a new timeline holding a copy of this timeline's published head, with new shot ids "
        "(shot ids are global per project). Narration is bound again from the lines' script text when they declare it."
    )
    from astrid.packs.timeline.cli import _add_project_arg

    subparser.add_argument("ref", help="Source timeline (UUID, ULID or slug); read only.")
    _add_project_arg(subparser)
    subparser.add_argument("--slug", default=None, help="Slug for the new timeline (default: its generated id).")
    subparser.add_argument("--name", default=None, help="Display name for the duplicate.")
    subparser.add_argument("--revision", default=None, help="Copy this parent revision instead of the current head.")
    subparser.set_defaults(handler=_cmd_duplicate)


def _cmd_rename(parsed: argparse.Namespace) -> int:
    result = parsed.client.timelines.rename(need_project(parsed), parsed.ref, parsed.name)
    return print_result(result, as_json=parsed.json)


def _configure_rename(subparser: argparse.ArgumentParser) -> None:
    from astrid.packs.timeline.cli import _add_json_flag, _add_project_arg

    subparser.add_argument("ref", help="Timeline id or slug.")
    subparser.add_argument("name", help="New display name.")
    _add_project_arg(subparser)
    _add_json_flag(subparser)
    subparser.set_defaults(handler=_cmd_rename)
