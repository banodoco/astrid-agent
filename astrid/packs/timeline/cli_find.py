"""The find verb: what you hear or see -> addresses and times. The search is astrid/sdk/timeline_address.find."""
from __future__ import annotations

import argparse
from typing import Any


def configure_find(parser: argparse.ArgumentParser, add_project_arg: Any) -> None:
    parser.description = (
        "Find what you see or hear: a spoken phrase (the conclusion; '\"tool\" in v20a'), a layer name (claw), "
        "on-screen text or an asset key. Each hit prints its address (paste it into show/edit/visualize), its time, "
        "its narration line and words, its cut, and the clips on screen then. A miss suggests the nearest spoken phrases."
    )
    add_project_arg(parser, required=False)
    parser.add_argument("ref", nargs="?", default=None, help="Timeline id or slug (or use --file).")
    parser.add_argument("query", nargs="?", default=None, help="What you heard or saw (every kind that matches).")
    parser.add_argument("--draft", default=None, help="Working copy name (default: current working copy).")
    parser.add_argument("--text", default=None, help="Only on-screen text containing this.")
    parser.add_argument("--asset", default=None, help="Only assets whose key is (or contains) this, any case.")
    parser.add_argument("--line", default=None, metavar="ID", help="Only in (or on screen during) narration line ID.")
    parser.add_argument("--cut", default=None, metavar="CUT", help="Only in cut CUT (c32).")
    parser.add_argument("--last", action="store_true", help="Only the last hit (in time).")
    add_find_flags(parser)
    parser.add_argument("--file", default=None, help="Read a local checkout file instead.")
    parser.add_argument("--published", action="store_true", help="Read the published head, not the working copy.")


def add_find_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--element", default=None, metavar="ELEMENT",
        help="Only clips of this element (exact or part, any case: callout is am-callout). With no query: every such clip.",
    )
    parser.add_argument(
        "--role", default=None, metavar="ROLE",
        help="Only clips with this role (label, sprite, voice, music, sfx ...). With no query: every such clip.",
    )


def run_find(parsed: argparse.Namespace) -> int:
    import sys

    from astrid.sdk.timeline_address import _nothing, describe_found, find
    from astrid.sdk.timeline_checkout import Checkout, find_draft

    def fail(message: str, code: int) -> int:
        print(message, file=sys.stderr)
        return code

    if parsed.file:
        if parsed.ref and not parsed.query:  # `find --file F QUERY`: the one word is the query
            parsed.query, parsed.ref = parsed.ref, None
        tl = Checkout.load(parsed.file)
    else:
        if not parsed.ref:
            return fail("name a timeline and query, or pass --file FILE", 2)
        path = None if parsed.published else find_draft(parsed.project, parsed.ref, parsed.draft, client=parsed.client)
        tl = Checkout.load(path) if path else Checkout.open(parsed.project, parsed.ref, client=parsed.client)
    from astrid.sdk.timeline_address import AddressError

    if not (parsed.query or parsed.text or parsed.asset or parsed.element or parsed.role):
        return fail('say what to find: timelines find <timeline> "phrase" · --text "visible text" · --asset KEY '
                    '· --element callout · --role label', 2)
    timeline_ref = str(parsed.ref or tl.bundle.get("timeline_id") or "TIMELINE")
    project_ref = str(parsed.project or tl.bundle.get("project_id") or "PROJECT")
    try:
        hits = find(tl, parsed.query, text=parsed.text, asset=parsed.asset, line=parsed.line, cut=parsed.cut,
                    last=parsed.last, element=parsed.element, role=parsed.role)
    except AddressError as exc:
        return fail(str(exc), 2)
    if not hits:
        if not (parsed.query or parsed.text or parsed.asset):
            return fail(f"no clip matches element={parsed.element!r} role={parsed.role!r}", 1)
        if parsed.query and not parsed.text and not parsed.asset:
            message = suggest_find_miss(
                tl, parsed.query, timeline=timeline_ref, project=project_ref,
                line=parsed.line, cut=parsed.cut,
            )
            if message:
                return fail(message, 1)
        return fail(_nothing(tl, parsed.query or parsed.text or parsed.asset), 1)
    for line in describe_found(tl, hits, timeline=timeline_ref, project=project_ref):
        print(line)
    return 0


def suggest_find_miss(
    tl: Any, query: str, *, timeline: str = "TIMELINE", project: str = "PROJECT",
    line: str | None = None, cut: str | None = None,
) -> str | None:
    """Format nearby spoken phrases for a failed exact query, if any are useful."""
    from astrid.sdk.timeline_address import shlex_quote, suggest_spoken

    suggestions = suggest_spoken(tl, query)
    if line:
        suggestions = [item for item in suggestions if item.line == line]
    if cut:
        cut_id = tl._cut_id(cut) or cut
        suggestions = [item for item in suggestions if tl._cut_at(item.start) == cut_id]
    if not suggestions:
        return None
    commands = [
        f'  {item.line}  {item.start:.2f}–{item.end:.2f} s  "{item.phrase}"\n'
        f'    timelines find --project {project} {timeline} {shlex_quote(item.phrase)}'
        for item in suggestions
    ]
    return "no exact match. Nearest spoken phrases:\n" + "\n".join(commands)
