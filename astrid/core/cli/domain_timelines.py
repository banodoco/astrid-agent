"""Core-owned Timeline argument declarations for CLI and MCP composition.

Optional Timeline execution helpers are resolved only when a command handler runs.
The flag builders and argparse actions below preserve the pack parser's surface;
constructing all product parsers never imports an optional pack.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Mapping

from astrid.core.cli.current_project import resolve_current_project
from astrid.core.cli.registration import CommandSpec, register_product_commands
from astrid.core.timeline import audio_curves

__all__ = ["COMMANDS", "TimelineHandlersUnavailable", "build_parser", "provide_handlers"]


# Execution handlers are provided by the Timeline core pack, which imports
# this module (packs may use core; core never imports a pack). Declarations
# here bind handler *names*; a name resolves only when a command runs. Product
# dispatch reaches the pack through the declared table in domain_product, so
# ``astrid timelines ...`` always provides the handlers before parsing.
_HANDLERS: dict[str, Any] = {}


class TimelineHandlersUnavailable(RuntimeError):
    """A declaration-only parser (e.g. MCP enumeration) was asked to execute."""


def provide_handlers(handlers: Mapping[str, Any]) -> None:
    """Bind the Timeline pack's execution handlers (called by the pack only)."""
    unknown = set(handlers) - _HANDLER_NAMES
    missing = _HANDLER_NAMES - set(handlers)
    if unknown or missing:
        raise ValueError(f"handler table mismatch for the timelines family: unknown={sorted(unknown)} missing={sorted(missing)}")
    _HANDLERS.update(handlers)


def _lookup(name: str):
    try:
        return _HANDLERS[name]
    except KeyError:
        raise TimelineHandlersUnavailable(
            f"timelines handler {name!r} is provided by the Timeline core pack; run it through `astrid timelines`"
        ) from None


def _provided(name: str):
    """Resolve an execution helper at invocation, after argument construction."""
    def run(*args, **kwargs):
        return _lookup(name)(*args, **kwargs)
    run.__name__ = name
    return run


def _provided_wrapper(name: str):
    def configure(*args):
        def run(parsed):
            return _lookup(name)(*args)(parsed)
        return run
    return configure

_as_timeline_verb = _provided_wrapper('_as_timeline_verb')
_cmd_apply = _provided('_cmd_apply')
_cmd_archive = _provided('_cmd_archive')
_cmd_assets = _provided('_cmd_assets')
_cmd_check = _provided('_cmd_check')
_cmd_checkout = _provided('_cmd_checkout')
_cmd_commit = _provided('_cmd_commit')
_cmd_create = _provided('_cmd_create')
_cmd_diff = _provided('_cmd_diff')
_cmd_edit = _provided('_cmd_edit')
_cmd_elements = _provided('_cmd_elements')
_cmd_export = _provided('_cmd_export')
_cmd_history = _provided('_cmd_history')
_cmd_import = _provided('_cmd_import')
_cmd_inspect = _provided('_cmd_inspect')
_cmd_list = _provided('_cmd_list')
_cmd_place_element = _provided('_cmd_place_element')
_cmd_recover = _provided('_cmd_recover')
_cmd_redo = _provided('_cmd_redo')
_cmd_render = _provided('_cmd_render')
_cmd_replace_parent_media = _provided('_cmd_replace_parent_media')
_cmd_script = _provided('_cmd_script')
_cmd_show = _provided('_cmd_show')
_cmd_status = _provided('_cmd_status')
_cmd_undo = _provided('_cmd_undo')
_cmd_update_element = _provided('_cmd_update_element')
_cmd_visualize = _provided('_cmd_visualize')
_find_run_find = _provided('_find_run_find')
_guarded = _provided_wrapper('_guarded')
_log_run_log = _provided('_log_run_log')
_pending_cmd_drop = _provided('_pending_cmd_drop')
_pending_cmd_list = _provided('_pending_cmd_list')
_pending_cmd_retry = _provided('_pending_cmd_retry')
_pending_cmd_show = _provided('_pending_cmd_show')
_pending_run = _provided('_pending_run')
_with_steps = _provided_wrapper('_with_steps')
_working_cmd_discard = _provided('_working_cmd_discard')
_working_cmd_duplicate = _provided('_working_cmd_duplicate')
_working_cmd_lines = _provided('_working_cmd_lines')
_working_cmd_lint = _provided('_working_cmd_lint')
_working_cmd_rename = _provided('_working_cmd_rename')
_working_cmd_restructure = _provided('_working_cmd_restructure')
_working_cmd_transcripts_sync = _provided('_working_cmd_transcripts_sync')
_working_cmd_words = _provided('_working_cmd_words')

_HANDLER_NAMES = frozenset({'_as_timeline_verb', '_cmd_apply', '_cmd_archive', '_cmd_assets', '_cmd_check', '_cmd_checkout', '_cmd_commit', '_cmd_create', '_cmd_diff', '_cmd_edit', '_cmd_elements', '_cmd_export', '_cmd_history', '_cmd_import', '_cmd_inspect', '_cmd_list', '_cmd_place_element', '_cmd_recover', '_cmd_redo', '_cmd_render', '_cmd_replace_parent_media', '_cmd_script', '_cmd_show', '_cmd_status', '_cmd_undo', '_cmd_update_element', '_cmd_visualize', '_find_run_find', '_guarded', '_log_run_log', '_pending_cmd_drop', '_pending_cmd_list', '_pending_cmd_retry', '_pending_cmd_show', '_pending_run', '_with_steps', '_working_cmd_discard', '_working_cmd_duplicate', '_working_cmd_lines', '_working_cmd_lint', '_working_cmd_rename', '_working_cmd_restructure', '_working_cmd_transcripts_sync', '_working_cmd_words'})


def _add_idempotency_key(subparser: argparse.ArgumentParser) -> None:
    subparser.add_argument(
        "--idempotency-key",
        dest="idempotency_key",
        default=None,
        help="Caller idempotency key (a fresh key is generated when absent).",
    )


class _JsonFlag(argparse.Action):
    """Record an explicit JSON request separately from a command default."""

    def __call__(self, parser, namespace, values, option_string=None) -> None:  # noqa: ANN001
        setattr(namespace, self.dest, True)
        setattr(namespace, "_json_explicit", True)


def _add_json_flag(subparser: argparse.ArgumentParser, *, default: bool = True) -> None:
    subparser.set_defaults(_json_explicit=False)
    subparser.add_argument(
        "--json",
        action=_JsonFlag,
        nargs=0,
        default=default,
        help=(
            "Print the exact SDK envelope (ok/data/error/receipt/idempotency_key)."
            if not default
            else "Print the exact SDK envelope (ok/data/error/receipt/idempotency_key); default output."
        ),
    )


def _native_configure_apply_options(subparser):
    subparser.add_argument("--dry-run", action="store_true", help="Preview the sheet commit without publishing.")
    _add_idempotency_key(subparser)
    _add_json_flag(subparser, default=False)
    subparser.set_defaults(native=False)


def _add_project_arg(
    subparser: argparse.ArgumentParser, *, required: bool = True
) -> None:
    subparser.add_argument(
        "--project",
        required=required,
        default=None,
        help=(
            "Owning project id or immutable slug. When omitted, use the "
            "workspace runtime's current project."
            if not required
            else "Owning project id or immutable slug."
        ),
    )


def _configure_apply(subparser: argparse.ArgumentParser) -> None:
    _add_project_arg(subparser, required=False)
    subparser.add_argument("ref", help="Timeline id or slug with a saved working copy.")
    subparser.add_argument("--draft", default=None, help="Working copy name (default: current working copy).")
    subparser.add_argument("sheet_file", type=Path, help="Path to the edited cut sheet.")

    _native_configure_apply_options(subparser)
    subparser.set_defaults(handler=_as_timeline_verb(_guarded(2, _cmd_apply)))


def _configure_archive(subparser: argparse.ArgumentParser) -> None:
    _add_project_arg(subparser)
    subparser.add_argument("ref", help="Timeline UUID, ULID, or slug.")
    _add_idempotency_key(subparser)
    _add_json_flag(subparser)
    subparser.set_defaults(handler=_cmd_archive)


def _configure_assets(subparser: argparse.ArgumentParser) -> None:
    _add_project_arg(subparser, required=False)
    subparser.add_argument("ref", help="Timeline id or slug.")
    subparser.add_argument("key", nargs="?", default=None, help="Show only this asset key.")
    subparser.add_argument("--draft", default=None, help="Working copy name (default: current working copy).")
    subparser.set_defaults(handler=_cmd_assets)


_verify_LABELS_HELP = (
    "With --at, draw each visible layer's address and box on the verification page."
)


def _verify_add_labels_flag(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--labels", action="store_true", default=False, help=_verify_LABELS_HELP)


_verify_TIMINGS_HELP = "With --at / --verify: where the time went (startup, comparison, end to end) for this verify."


def _verify_add_timings_flag(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--timings", action="store_true", help=_verify_TIMINGS_HELP)


def _visualize_add_zoom_flags(parser: argparse.ArgumentParser) -> None:
    """``check --at MOMENT --zoom ADDRESS | --region X,Y,W,H``: a full-resolution crop of both sides."""
    parser.add_argument("--zoom", default=None, metavar="ADDRESS",
                        help="With --at: also crop both frames around this layer at full resolution (c08.app), "
                             "for details the thumbnails cannot show (connector ends, 1-px outlines).")
    parser.add_argument("--region", default=None, metavar="X,Y,W,H",
                        help="With --at: crop this canvas-px rectangle at full resolution instead of a layer.")


_sweep_CHECK_SWEEP_HELP = (
    "Compare the working copy with the published film at bounded samples across every cut; "
    "unexpected picture changes fail the check."
)


_sweep_NO_SWEEP_HELP = "Skip the automatic whole-film sweep after an edit."


def _sweep_add_check_sweep_flag(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--sweep", action="store_true", help=_sweep_CHECK_SWEEP_HELP)
    parser.add_argument("--no-sweep", dest="no_sweep", action="store_true", help=_sweep_NO_SWEEP_HELP)


def _configure_check(subparser: argparse.ArgumentParser) -> None:
    _add_project_arg(subparser, required=False)
    subparser.add_argument("ref", help="Timeline UUID, ULID, or slug with a saved working copy.")
    subparser.add_argument("--draft", default=None, help="Working copy name (default: current working copy).")
    subparser.add_argument("--all", action="store_true", help="Print the full validation report.")
    subparser.add_argument("--at", action="append", default=None, metavar="MOMENT",
                           help="Compare the published timeline and working copy at this moment; repeat for multiple moments.")

    _verify_add_labels_flag(subparser)
    _verify_add_timings_flag(subparser)

    _visualize_add_zoom_flags(subparser)

    _sweep_add_check_sweep_flag(subparser)
    subparser.set_defaults(handler=_as_timeline_verb(_cmd_check))


def _configure_checkout(subparser: argparse.ArgumentParser) -> None:
    _add_project_arg(subparser, required=False)
    subparser.add_argument("ref", help="Timeline id or slug to check out.")
    subparser.add_argument("--from", dest="from_ref", help="Adopt content from another timeline into this working copy.")
    subparser.add_argument("--draft", default=None, help="Working copy name (default: current working copy).")
    subparser.add_argument("--fresh", action="store_true", help="Check out the current head instead of reusing the selected working copy.")
    _add_json_flag(subparser)
    subparser.set_defaults(handler=_as_timeline_verb(_guarded(2, _cmd_checkout)))


def _configure_commit(subparser: argparse.ArgumentParser) -> None:
    _add_project_arg(subparser, required=False)
    subparser.add_argument("ref", help="Timeline UUID, ULID, or slug named by the detached document.")
    subparser.add_argument("--file", type=Path, required=True,
                           help="Detached timeline JSON from the existing timeline document checkout.")
    subparser.add_argument("-m", "--message", required=True,
                           help="Nonempty commit label recorded in Runtime history.")
    subparser.add_argument("--idempotency-key", required=True,
                           help="Stable key to retry this exact document commit.")
    subparser.add_argument("--dry-run", action="store_true",
                           help="Preview the commit without accepting a Runtime publication.")
    _add_json_flag(subparser, default=False)
    subparser.set_defaults(handler=_as_timeline_verb(_cmd_commit))


def _parse_canvas(value: str) -> tuple[int, int]:
    match = re.fullmatch(r"\s*([1-9]\d*)[xX]([1-9]\d*)\s*", value)
    if match is None:
        raise argparse.ArgumentTypeError("canvas must be WIDTHxHEIGHT, e.g. 1920x1080")
    return int(match.group(1)), int(match.group(2))


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a positive integer") from exc
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def _configure_create(subparser: argparse.ArgumentParser) -> None:
    subparser.add_argument(
        "timeline_id",
        nargs="?",
        default=None,
        help=(
            "New timeline id (ULID). Omit to generate one; the id is printed "
            "in the result and is what show/checkout/render accept."
        ),
    )
    _add_project_arg(subparser, required=False)
    subparser.add_argument("--name", default=None, help="Display name for the timeline.")
    subparser.add_argument(
        "--slug",
        default=None,
        help="Slug for the new timeline (passed as timeline_id); an alternative to the positional id.",
    )
    subparser.add_argument(
        "--canvas",
        type=_parse_canvas,
        default=(1920, 1080),
        help="Canvas size WIDTHxHEIGHT (default 1920x1080).",
    )
    subparser.add_argument(
        "--fps",
        type=_positive_int,
        default=30,
        help="Canvas frame rate (default 30).",
    )
    _add_idempotency_key(subparser)
    _add_json_flag(subparser)
    subparser.set_defaults(handler=_cmd_create)


def _configure_diff(subparser: argparse.ArgumentParser) -> None:
    _add_project_arg(subparser)
    subparser.add_argument("ref", help="Timeline UUID, ULID, or slug.")
    subparser.add_argument("--from", "--from-revision", dest="from_revision",
                           help="Exact immutable parent revision before the batch.")
    subparser.add_argument("--to", "--to-revision", dest="to_revision",
                           help="Exact immutable parent revision after the batch.")
    subparser.add_argument("--from-version", type=int, help="Explicit legacy integer version (not a canonical parent revision).")
    subparser.add_argument("--to-version", type=int, help="Explicit legacy integer version (not a canonical parent revision).")
    output = subparser.add_mutually_exclusive_group()
    output.add_argument("--format", choices=("readable", "details", "json"),
                        help="Group authored changes, show every raw detail, or print the exact SDK envelope (default: json).")
    output.add_argument("--json", dest="format", action="store_const", const="json",
                        help="Print the exact SDK envelope; equivalent to --format json (default).")
    subparser.add_argument(
        "--as",
        dest="presentation",
        choices=("cuts", "script", "code"),
        default=None,
        help="Present exact --from-revision/--to-revision bundles as cuts, script, or code.",
    )
    subparser.add_argument("--all", action="store_true", help="Every changed clip (by default the first 40, then a count).")
    subparser.add_argument("--published", action="store_true",
                           help="Diff saved revisions only, ignoring the working copy (no --from/--to: the head vs the working copy).")
    subparser.add_argument("--draft", default=None, help="Which working copy (default: the current one, last checked out).")
    subparser.set_defaults(handler=_cmd_diff, format=None, json=False)


def _native_configure_edit_options(subparser):
    subparser.description = ("Media layer adds, literal clip moves and trims commit through the durable timeline session. "
                             "Other flags edit a legacy detached working copy.")
    subparser.add_argument("--dry-run", action="store_true", help="Preview a media add, move or trim without accepting a publication.")
    _add_idempotency_key(subparser)
    _add_json_flag(subparser, default=False)
    subparser.set_defaults(native_trim_defaults={action.dest: action.default for action in subparser._actions})


_look_EXAMPLES = {"--entrance": "fade 12f ease=out", "--exit": "fade 12f ease=in", "--fade-in": "8f ease=out",
             "--fade-out": "8f ease=in"}


class _look_Tokens(argparse.Action):
    """Up to `max_tokens` words (--entrance NAME [DURATION] [ease=E]), joined into the human form."""

    def __init__(self, option_strings, dest, *, max_tokens: int, usage: str, **kwargs):
        self.max_tokens = max_tokens
        self.usage = usage
        super().__init__(option_strings, dest, **kwargs)

    def __call__(self, parser, namespace, values, option_string=None):  # noqa: ARG002
        if not 1 <= len(values) <= self.max_tokens:
            parser.error(f"{option_string} takes {self.usage}, e.g. {option_string} {_look_EXAMPLES[option_string]}")
        setattr(namespace, self.dest, " ".join(values))


def _look_add_look_flags(parser: argparse.ArgumentParser) -> None:
    group = parser.add_argument_group("look (clip-level; see clip_fields)")
    usage_entrance = "NAME [DURATION] [ease=E]"
    group.add_argument("--entrance", nargs="+", action=_look_Tokens, default=None, metavar=usage_entrance,
                       max_tokens=3, usage=usage_entrance,
                       help="An entrance: 'fade 8f', 'fade 12f ease=out', 'fade-up 0.4s', 'scale-in 18f', "
                            "'slide-left 12f', or none. ease= is linear (default), in, out, in-out or step.")
    group.add_argument("--exit", nargs="+", action=_look_Tokens, default=None, metavar=usage_entrance,
                       max_tokens=3, usage=usage_entrance,
                       help="An exit: 'fade 12f', 'fade 12f ease=in' or 'slide-up 12f'.")
    usage_fade = "DURATION [ease=E|curve=NAME]"
    curves = ", ".join(audio_curves.names())
    group.add_argument("--fade-in", dest="fade_in", nargs="+", action=_look_Tokens, default=None, metavar=usage_fade,
                       max_tokens=2, usage=usage_fade,
                       help=f"A fade in, e.g. '8f' or '0.4s'; audio also accepts curve=NAME ({curves}).")
    group.add_argument("--fade-out", dest="fade_out", nargs="+", action=_look_Tokens, default=None, metavar=usage_fade,
                       max_tokens=2, usage=usage_fade,
                       help=f"A fade out, e.g. '8f' or '0.4s'; audio also accepts curve=NAME ({curves}).")
    group.add_argument("--opacity", default=None, metavar="VALUE", help="Opacity 0..1, e.g. '0.6'.")
    volume = group.add_mutually_exclusive_group()
    volume.add_argument("--volume", default=None, metavar="GAIN",
                        help="Gain or dB, e.g. '0.5', 'mute' or '+2dB' (audio and footage). "
                             "A negative dB needs `--volume=-3dB` or `--gain-db -3`.")
    volume.add_argument("--gain-db", dest="gain_db", type=float, default=None, metavar="DB",
                        help="Volume in dB, e.g. '-3' (same as `--volume=-3dB`; audio and footage).")


def _film_add_film_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--film-end", dest="film_end", default=None, metavar="MOMENT",
                        help="Where the film ends, as a moment: --film-end 'after \"Astrid\" in n27 +2.5s'. The last "
                             "picture and everything that ends with the film hold to it; the music keeps its ending. "
                             "Not with a clip selector.")


_music_MUSIC_CHOICES = ("keep-end", "plays-on")


def _music_add_music_flag(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--music", choices=_music_MUSIC_CHOICES, default=None, metavar="{keep-end,plays-on}",
                        help="how the music bed follows a re-time: keep-end (default) cuts it at a bar with a crossfade "
                             "so its ending stays on the film's end; plays-on keeps it whole")


_sweep_EDIT_SWEEP_HELP = (
    "After saving, compare every cut when an edit re-flows the film, touches more than three cuts, "
    "or --sweep is given."
)


def _sweep_add_edit_sweep_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--sweep", action="store_true", help=_sweep_EDIT_SWEEP_HELP)
    parser.add_argument("--no-sweep", dest="no_sweep", action="store_true", help=_sweep_NO_SWEEP_HELP)


def _parse_insert(value: str) -> tuple[str, float]:
    at, sep, seconds = value.rpartition(":")
    if not sep or not at:
        raise argparse.ArgumentTypeError("--insert takes TIME:SECONDS, e.g. 1:02:0.5")
    try:
        return at, float(seconds)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"--insert seconds must be a number: {seconds!r}") from exc


def _configure_edit(subparser: argparse.ArgumentParser) -> None:
    _add_project_arg(subparser, required=False)
    subparser.add_argument("ref", nargs="?", default=None,
                           help="Timeline id or slug with a saved working copy (or use --file).")
    subparser.add_argument("--file", default=None, help="Edit a local checkout file instead of the working copy.")
    subparser.add_argument("--draft", default=None, help="Working copy name (default: current working copy).")
    subparser.add_argument("--clip", help="Clip by address (c30.cover), layer name, asset key, id or prefix, element, or on-screen text.")
    subparser.add_argument("--near", default=None, help="With --clip: the match nearest this word or time.")
    subparser.add_argument("--add-media", dest="add_media", default=None, metavar="FILE|HANDLE|KEY",
                           help="Add an audio media clip; audio tracks stay independent of picture cuts "
                                "(needs --track and --on; --for/--until/--trim-in/--trim-out/--gain-db/--id optional).")
    subparser.add_argument("--add-layer", dest="add_layer", default=None, metavar="ELEMENT",
                           help="Add media directly through the commit door, including the first clip "
                                "(--layer-asset FILE|HANDLE|KEY --on SECONDS --for SECONDS --track V1). "
                                "Other layers (type, sprite, snap-plate ...) edit the cut on screen at --on MOMENT "
                                "(--layer NAME, --layer-asset ASSET, --for/--until, --set k=v, --track optional).")
    subparser.add_argument("--layer", default=None, metavar="NAME", help="Layer name for --add-layer (default: its asset or element name).")
    subparser.add_argument("--layer-asset", dest="layer_asset", default=None, metavar="ASSET",
                           help="Asset (key, file or media handle) for --add-layer.")
    subparser.add_argument("--track", default=None, metavar="TRACK", help="Track for --add-layer, or the audio track for --add-media (music, vo, sfx ...).")
    subparser.add_argument("--id", dest="clip_id", default=None, metavar="ID", help="Clip id for --add-media or --add-layer.")
    subparser.add_argument("--trim-in", dest="trim_in", type=float, default=None, metavar="SECONDS",
                           help="Set a media clip's source in point; its film start stays and its length follows.")
    subparser.add_argument("--trim-out", dest="trim_out", type=float, default=None, metavar="SECONDS",
                           help="Set a media clip's source out point; its film start stays and its length follows.")
    subparser.add_argument("--slip", type=float, default=None, metavar="SECONDS",
                           help="Move a media clip's source window while its film position and length stay fixed.")
    subparser.add_argument("--set", dest="set_values", action="append", default=[])
    subparser.add_argument("--swap-asset", metavar="KEY|FILE|HANDLE|KEY=HANDLE",
                           help="Point the clip at an asset: a registry key, a local file, or project media "
                                "(run:RUN/PORT#n, task:TASK/PORT#n, ref:NAME, sha256:DIGEST). Name new media yourself "
                                "with KEY=HANDLE (BROOM=run:RUN/sprites#0).")
    subparser.add_argument("--replace-asset", dest="replace_asset", default=None, metavar="KEY=HANDLE",
                           help="Repoint every clip using KEY to new project media, keeping the key "
                                "(run:..., task:..., ref:..., sha256:...).")

    _look_add_look_flags(subparser)
    subparser.add_argument("--on", help='Start on a moment: "viral", after "Astrid", beat 2 after "Astrid", c22, +0.8s.')
    subparser.add_argument("--until", help='End on a moment (its start stays): "Astrid", after "viral", c26.')
    subparser.add_argument("--for", "--duration", dest="for_seconds", type=float, metavar="SECONDS",
                           help="A literal length in seconds (prefer --until when the end means something).")
    subparser.add_argument("--extend", type=float)
    subparser.add_argument("--nudge", type=float)
    subparser.add_argument("--nudge-frames", type=int)
    subparser.add_argument("--close-gap-before")
    subparser.add_argument("--at", dest="at", help="Start the clip at this time (seconds or m:ss).")
    subparser.add_argument("--at-word", dest="at_word", default=None, help="Start the clip at this word.")
    subparser.add_argument("--n", type=int, default=None, help="Which occurrence of --at-word (1-based).")
    subparser.add_argument("--offset", type=float, help="Seconds after the word or time.")
    subparser.add_argument("--split", metavar="CUT", help="Split this cut at --on MOMENT.")
    subparser.add_argument("--add-cut", action="store_true", help="Add a cut at --on MOMENT (see --after).")
    subparser.add_argument("--cut", help="Cut number or id; alone, the cut's picture clip.")
    subparser.add_argument("--cut-id", dest="cut_id", default=None, metavar="ID",
                           help="The new cut's id for --split/--add-cut (default: the next free).")
    subparser.add_argument("--picture", default=None, metavar="KEY|FILE|HANDLE",
                           help="The new cut's picture for --split/--add-cut.")
    subparser.add_argument("--why", default=None, metavar="TEXT",
                           help="With --cut: why the cut is there (the sheet's why: line).")
    subparser.add_argument("--hold", default=None, choices=("deliberate", "off"),
                           help="With --cut: mark the cut's hold deliberate (or off).")
    subparser.add_argument("--fit-sequence", dest="fit_sequence", action="store_true",
                           help="Fit the clip's whole step sequence into --on/--until as one unit.")
    subparser.add_argument("--shape-from", dest="shape_from", default=None, metavar="TL[@REV]",
                           help="With --fit-sequence: restore the shape of the same sequence in another timeline.")

    _film_add_film_flags(subparser)
    subparser.add_argument("--retime", action="store_true", help="Move every anchored clip back onto its word.")
    subparser.add_argument("--insert", type=_parse_insert, default=None, metavar="TIME:SECONDS",
                           help="Insert silence/time at TIME (e.g. 1:02:0.5), moving everything after it later.")
    subparser.add_argument("--keep", action="store_true", help="Accept an orphaned clip where it is (a fixed time, no moment); it stops blocking commit.")
    subparser.add_argument("--clear-asset", dest="clear_asset", action="store_true",
                           help="Empty the clip's asset (it stays on the timeline).")
    subparser.add_argument("--allow-new-params", dest="allow_new_params", action="store_true",
                           help="Let --set write a param the element does not declare.")
    subparser.add_argument("--beats", default=None, metavar="FILE|HANDLE|keep",
                           help="Attach a beat grid to the clip from a file or handle (or keep its own).")
    subparser.add_argument("--verify", action="store_true",
                           help="After saving, compare the published film and the working copy at the changed frames.")

    _verify_add_labels_flag(subparser)
    _verify_add_timings_flag(subparser)
    subparser.add_argument("--gap-after", action="append", default=[], metavar="LINE=SECONDS",
                           help="Set the declared silence after a line; repeat to set several lines.")
    subparser.add_argument("--remove-line", metavar="LINE", help="Remove a narration line.")
    narration = subparser.add_mutually_exclusive_group()
    narration.add_argument("--replace-line", "--line", metavar="LINE", help="Replace one line's selected take and word timings.")
    narration.add_argument("--insert-line", metavar="LINE", help="Insert a line after --after.")
    narration.add_argument("--from-script", metavar="PATH", help="Apply a JSON narration script and its local takes.")
    subparser.add_argument("--take", help="Selected WAV or registered take for --replace-line/--insert-line.")
    subparser.add_argument("--words", help="Word-timing JSON for --replace-line/--insert-line.")
    subparser.add_argument("--after", help="Existing line after which --insert-line is placed.")
    subparser.add_argument("--insert-gap-after", "--gap", dest="insert_gap_after", type=float, help="Declared gap after a newly inserted line, in seconds.")
    subparser.add_argument("--text", "--line-text", dest="line_text",
                           help="Narration text to bind with a replacement or insertion.")
    subparser.add_argument("--takes", help="Take directory used by --from-script.")
    subparser.add_argument("--gaps", "--script-gaps", dest="script_gaps", action="store_true",
                           help="Use script-declared gaps when applying --from-script.")

    _music_add_music_flag(subparser)
    from astrid.core.timeline.audio_curves import names as audio_curve_names

    subparser.add_argument("--crossfade", nargs="+", default=None, metavar="TOKEN",
                           help=f"Crossfade clips A and B on one audio track; optional third token curve=NAME "
                                f"(default: equal-power; curves: {', '.join(audio_curve_names())}). "
                                "With --over, moves B's start to create that overlap.")
    subparser.add_argument("--over", dest="overlap", type=float, default=None, metavar="SECONDS",
                           help="With --crossfade: requested overlap; keeps A fixed and moves B's start.")
    subparser.add_argument("--remove", action="store_true")

    _sweep_add_edit_sweep_flags(subparser)
    _native_configure_edit_options(subparser)
    subparser.set_defaults(handler=_as_timeline_verb(_cmd_edit))


def _configure_elements(subparser: argparse.ArgumentParser) -> None:
    _add_project_arg(subparser)
    subparser.add_argument('ref',help='Exact target timeline ID or slug.')
    subparser.add_argument('--realm',help='Canonical realm; defaults to configured launcher discovery.')
    _add_json_flag(subparser)
    subparser.set_defaults(handler=_cmd_elements)


def _configure_export(subparser: argparse.ArgumentParser) -> None:
    _add_project_arg(subparser, required=False)
    subparser.add_argument("ref", help="Saved timeline UUID, ULID, or slug.")
    subparser.add_argument("--out", required=True, type=Path, help="Destination .astrid archive.")
    _add_json_flag(subparser)
    subparser.set_defaults(handler=_cmd_export)


def _find_add_find_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--element", default=None, metavar="ELEMENT",
        help="Only clips of this element (exact or part, any case: callout is am-callout). With no query: every such clip.",
    )
    parser.add_argument(
        "--role", default=None, metavar="ROLE",
        help="Only clips with this role (label, sprite, voice, music, sfx ...). With no query: every such clip.",
    )


def _find_configure_find(parser: argparse.ArgumentParser, add_project_arg: Any) -> None:
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
    _find_add_find_flags(parser)
    parser.add_argument("--file", default=None, help="Read a local checkout file instead.")
    parser.add_argument("--published", action="store_true", help="Read the published head, not the working copy.")


def _configure_find(subparser: argparse.ArgumentParser) -> None:
    _find_configure_find(subparser, _add_project_arg)
    subparser.set_defaults(handler=_find_run_find)


def _configure_history(subparser: argparse.ArgumentParser) -> None:
    _add_project_arg(subparser, required=False)
    subparser.add_argument("ref", help="Timeline UUID, ULID, or slug (TL@prev etc. names an earlier revision elsewhere).")
    subparser.add_argument("--limit", type=_positive_int, default=50, help="Newest revisions to list (default 50).")
    _add_json_flag(subparser)
    subparser.set_defaults(handler=_cmd_history)


def _native_configure_history_options(subparser):
    subparser.add_argument("--commit", action="append", metavar="RECEIPT_ID",
                           help="Reverse an accepted Runtime commit; for redo select the accepted undo. Repeat oldest first.")
    subparser.add_argument("--dry-run", action="store_true", help="Preview the inverse without accepting a commit.")
    _add_idempotency_key(subparser)
    _add_json_flag(subparser, default=False)


def _native_history_steps(value):
    import argparse


    if not value.lstrip().startswith("{"):
        return _positive_int(value)
    try:
        steps = json.loads(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("steps must be a JSON map of receipt IDs to step-ID lists") from exc
    if (not steps or not all(isinstance(key, str) and key.strip()
                             and isinstance(ids, list) and ids
                             and all(isinstance(step, str) and step.strip() for step in ids)
                             and len(set(ids)) == len(ids) for key, ids in steps.items())):
        raise argparse.ArgumentTypeError("steps must map each receipt ID to nonempty unique step IDs")
    return steps


def _configure_history_edit(subparser: argparse.ArgumentParser, handler) -> None:
    _add_project_arg(subparser, required=False)
    subparser.add_argument("ref")
    subparser.add_argument("--draft", default=None, help="Working copy name (default: current working copy).")
    subparser.add_argument("steps", nargs="?", type=_positive_int, default=None, help="How many edits (default 1).")


    subparser.add_argument("--steps", dest="steps_option", type=_native_history_steps, default=None,
                           help="Local edit count, or with --commit a JSON map of receipt IDs to step-ID lists.")
    _native_configure_history_options(subparser)
    subparser.set_defaults(handler=_as_timeline_verb(_guarded(2, _with_steps(handler))))


def _configure_import(subparser: argparse.ArgumentParser) -> None:
    _add_project_arg(subparser, required=False)
    subparser.add_argument("file", type=Path, help=".astrid timeline archive to import.")
    subparser.add_argument("--name", help="Display name for the imported timeline.")
    _add_json_flag(subparser)
    subparser.set_defaults(handler=_cmd_import)


_inspection_contract_INSPECTION_SECTIONS = ("summary", "pages", "cards", "placements", "audio", "boundaries")


def _configure_inspect(subparser: argparse.ArgumentParser) -> None:
    subparser.description = "Inspect a verified materialized filmstrip offline; no runtime or render admission. Responses are capped at 8 KiB."
    subparser.add_argument("--manifest", required=True, help="Materialized filmstrip manifest.json; bundle members are verified before reading.")
    subparser.add_argument("--section", default="summary", help="Named section: " + ", ".join(_inspection_contract_INSPECTION_SECTIONS))
    subparser.add_argument("--limit", type=int, default=10, help="Records per response: 1–50, default 10; byte cap may reduce the page.")
    subparser.add_argument("--cursor", default=None, help="Opaque cursor from this bundle and identical query.")
    subparser.add_argument("--frame", type=int, default=None, help="Exact decoded frame number.")
    for selector in ("card", "shot", "occurrence", "clip", "track", "asset"):
        subparser.add_argument("--" + selector, default=None, help="Exact " + selector + " identity (shot also accepts an exact name).")
    subparser.add_argument("--range", dest="range_value", default=None, help="Half-open START..END seconds window.")
    subparser.add_argument("--json", action="store_true", help="Compact JSON envelope is always used.")
    subparser.set_defaults(handler=_cmd_inspect)


def _configure_list(subparser: argparse.ArgumentParser) -> None:
    _add_project_arg(subparser, required=False)
    subparser.add_argument(
        "--include-archived",
        dest="include_archived",
        action="store_true",
        help="Include archived timelines and their archived_at state.",
    )
    subparser.add_argument("--recent", action="store_true", help="Show up to three timelines by latest revision time.")
    subparser.add_argument("--all-projects", action="store_true", help="Search every project (with --recent).")
    _add_json_flag(subparser, default=False)
    subparser.set_defaults(handler=_cmd_list)


def _log_configure_log(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("ref", help="Timeline UUID, ULID, or slug.")
    parser.add_argument("--project", required=True, help="Owning project id or immutable slug.")
    parser.add_argument("--since", default=None, help="A sequence number or ISO timestamp.")
    parser.add_argument("--actor", default=None, help="Actor id or kind:id.")
    parser.add_argument("--op", default=None, help="Operation label or operation id.")
    parser.add_argument("--cut", default=None, help="Only records that touched this cut.")
    parser.add_argument("--clip", default=None, help="Only records that touched this clip.")
    parser.add_argument("--outcome", default=None, help="Only records with this outcome.")
    parser.add_argument("--json", action="store_true", help="Print the exact five-key product envelope.")
    parser.add_argument("--verify", action="store_true", help="Verify the journal hash chain.")
    parser.set_defaults(handler=_log_run_log)


def _configure_log(subparser: argparse.ArgumentParser) -> None:
    _log_configure_log(subparser)


def _pending_configure_drop(parser):
    parser.add_argument("ref", help="Timeline id or slug.")
    parser.add_argument("ref_id", help="Pending record id or key.")
    parser.add_argument("--project", required=True)
    parser.add_argument("--reason", help="Why this saved edit was dropped.")
    parser.add_argument("--json", action="store_true")
    parser.set_defaults(handler=lambda p: _pending_run(p, _pending_cmd_drop))


def _pending_configure_list(parser):
    parser.add_argument("ref", help="Timeline id or slug.")
    parser.add_argument("--project", required=True)
    parser.add_argument("--all", action="store_true", help="Include landed and dropped records.")
    parser.add_argument("--all-actors", action="store_true", help="Include other actors on this machine.")
    parser.add_argument("--json", action="store_true")
    parser.set_defaults(handler=lambda p: _pending_run(p, _pending_cmd_list))


def _pending_configure_show(parser):
    parser.add_argument("ref", help="Timeline id or slug.")
    parser.add_argument("ref_id", help="Pending record id or key.")
    parser.add_argument("--project", required=True)
    parser.add_argument("--json", action="store_true")
    parser.set_defaults(handler=lambda p: _pending_run(p, _pending_cmd_show))


def _pending_configure_pending(parser):
    nested = parser.add_subparsers(dest="pending_command", required=True)
    nested.add_parser("list", help="List saved timeline edits.").set_defaults(_configure=_pending_configure_list)
    nested.add_parser("show", help="Show a saved edit and its conflict packet.").set_defaults(_configure=_pending_configure_show)
    nested.add_parser("drop", help="Mark a saved edit dropped without deleting it.").set_defaults(_configure=_pending_configure_drop)
    for action in nested.choices.values():
        configure = action.get_default("_configure")
        configure(action)



def _pending_configure_retry(parser):
    parser.add_argument("ref", help="Timeline id or slug.")
    parser.add_argument("ref_id", help="Pending record id or key.")
    parser.add_argument("--project", required=True)
    parser.add_argument("--commit", dest="commit_token", metavar="TOKEN",
                        help="Commit only if the current retry preview still has this token.")
    parser.add_argument("--json", action="store_true")
    parser.set_defaults(handler=lambda p: _pending_run(p, _pending_cmd_retry))



def _configure_pending_verb(name: str):
    return {'pending': _pending_configure_pending, 'retry': _pending_configure_retry}[name]


def _configure_place_element(subparser: argparse.ArgumentParser) -> None:
    _add_project_arg(subparser)
    subparser.add_argument('ref', help='Exact target timeline ID or slug.')
    subparser.add_argument('--realm', required=True,
                           help='Canonical realm used by the configured project-pack admission.')
    subparser.add_argument('--target', required=True,
                           help=('Exact occurrence shot clip: occurrence:<occurrence-id>/clip:<clip-id>. '
                                 'First placement supports effect elements on am-discord clips only.'))
    subparser.add_argument('--pack', required=True, help='Admitted project-pack owner (packId).')
    subparser.add_argument('--element', required=True, help='Admitted element id.')
    subparser.add_argument('--kind', choices=['effect'], default='effect')
    subparser.add_argument('--revision', required=True, help='Exact admitted element revision.')
    subparser.add_argument('--expected-head', required=True,
                           help='Exact parent-composition revision; stale heads fail closed.')
    _add_idempotency_key(subparser)
    _add_json_flag(subparser)
    subparser.set_defaults(handler=_cmd_place_element)


def _configure_recover(subparser: argparse.ArgumentParser) -> None:
    _add_project_arg(subparser)
    subparser.add_argument(
        "ref",
        help="Timeline UUID, ULID, or slug from list --include-archived.",
    )
    _add_idempotency_key(subparser)
    _add_json_flag(subparser)
    subparser.set_defaults(handler=_cmd_recover)


def _parse_json_object(value: str) -> dict[str, Any]:
    """Parse a ``--config``/``--registry`` JSON object argument."""
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise argparse.ArgumentTypeError(f"invalid JSON object: {exc.msg}") from exc
    if not isinstance(parsed, dict):
        raise argparse.ArgumentTypeError("must be a JSON object")
    return parsed


def _configure_render(subparser: argparse.ArgumentParser) -> None:
    _add_project_arg(subparser, required=False)
    _add_idempotency_key(subparser)
    subparser.add_argument("--review", action="store_true", default=None, help="Burn in shot names/time plus pinned authored speech captions at the bottom (Remotion/Three.js).")
    subparser.add_argument("--audio-target", dest="audio_target", type=float, default=None, metavar="LUFS",
                           help="Master the final mix to this integrated loudness (e.g. -14); with --true-peak (default -1 dBTP).")
    subparser.add_argument("--true-peak", dest="true_peak", type=float, default=None, metavar="DBTP",
                           help="The true-peak ceiling of the master (e.g. -1); alone it implies --audio-target -14.")
    subparser.add_argument("--delivery-audio", dest="delivery_audio", default=None, metavar="JSON",
                           help='The master as JSON, e.g. {"lufs": -14, "true_peak": -1} (same as the two flags).')
    subparser.add_argument(
        "--range", nargs=2, metavar=("START", "END"), default=None,
        help="Render the half-open timeline window START END in seconds without editing the timeline.",
    )
    subparser.add_argument(
        "ref",
        nargs="?",
        default=None,
        help=(
            "Canonical timeline UUID, ULID, or slug. "
            "When omitted, the project's default timeline is rendered."
        ),
    )
    subparser.add_argument(
        "--expected-version",
        dest="expected_version",
        type=int,
        default=None,
        help="Optional exact kernel config version; stale pins fail before admission.",
    )
    subparser.add_argument(
        "--draft", nargs="?", const="main", default=None, metavar="NAME",
        help='Render the working copy (unpublished draft) instead of the published head; NAME defaults to "main".',
    )
    subparser.add_argument(
        "--backend",
        default=None,
        help="Qualified renderer id or supported compatibility selector.",
    )
    subparser.add_argument(
        "--profile",
        type=_parse_json_object,
        default=None,
        metavar="JSON",
        help=(
            "Flat RenderProfile v1 JSON object (no video/audio nesting). "
            "Complete Remotion MP4 example: "
            "{\"width\": 1920, \"height\": 1080, \"fps_rational\": [30, 1], "
            "\"time_base\": [1, 90000], \"container\": \"mp4\", "
            "\"video_codec\": \"h264\", \"video_profile\": null, "
            "\"video_level\": null, \"pixel_format\": \"yuv420p\", "
            "\"audio_codec\": \"aac\", \"audio_sample_rate\": 48000, "
            "\"audio_channel_layout\": \"stereo\", \"duration_tolerance\": 1}. "
            "The audio trio must be supplied together or all omitted. When omitted, the "
            "resolved theme canvas is used (default 1920x1080 at 30 fps), "
            "not legacy config.output resolution/fps hints. Explicit profiles must "
            "match the authoritative theme canvas; set theme_overrides.visual.canvas "
            "for a different size."
        ),
    )
    subparser.add_argument(
        "--output-name",
        default=None,
        help=(
            "Plain output filename (default hype.mp4). A canonical timeline stamped "
            "metadata.astrid_layer.alpha=true may request .mov for ProRes 4444/PCM output."
        ),
    )
    completion = subparser.add_mutually_exclusive_group()
    completion.add_argument(
        "--wait",
        dest="wait",
        action="store_true",
        default=True,
        help="Follow the render to completion and propagate terminal failure (default).",
    )
    completion.add_argument(
        "--detach",
        dest="wait",
        action="store_false",
        help="Return after admission with state=admitted; inspect the returned task/run later.",
    )
    subparser.add_argument(
        "--timeout-seconds",
        type=float,
        default=3600.0,
        help="Maximum wait for --wait before returning a non-success result (default: 3600).",
    )
    _add_json_flag(subparser)
    subparser.set_defaults(handler=_cmd_render)


def _configure_replace_parent_media(subparser: argparse.ArgumentParser) -> None:
    _add_project_arg(subparser)
    subparser.add_argument("ref", help="Timeline UUID, ULID, or slug.")
    subparser.add_argument("--occurrence-id", required=True, help="Exact parent-composition occurrence to edit.")
    subparser.add_argument("--clip-id", required=True, help="Exact internal-timeline clip id to replace.")
    subparser.add_argument("--source-object-id", required=True, help="Admitted project-owned media digest/object id.")
    subparser.add_argument(
        "--expected-head",
        required=True,
        help="Exact parent-composition revision id read from the target; stale heads fail closed.",
    )
    _add_idempotency_key(subparser)
    _add_json_flag(subparser)
    subparser.set_defaults(handler=_cmd_replace_parent_media)


def _configure_script(subparser: argparse.ArgumentParser) -> None:
    _add_project_arg(subparser, required=False)
    subparser.add_argument("ref", nargs="?", default=None)
    subparser.add_argument("--revision", default=None, help="Exact immutable parent revision; defaults to the current head.")
    subparser.add_argument("--occurrence", default=None, help="Read one exact placed occurrence.")
    subparser.add_argument("--kind", default="voiceover_script")
    _add_json_flag(subparser)
    subparser.set_defaults(handler=_cmd_script)


class _show_JoinRanges(argparse.Action):
    """``--range A --range B`` = ``A,B`` (never silently the last one)."""

    def __call__(self, parser: Any, namespace: Any, values: Any, option_string: Any = None) -> None:
        current = getattr(namespace, self.dest, None)
        setattr(namespace, self.dest, f"{current},{values}" if current else values)


def _configure_show(subparser: argparse.ArgumentParser) -> None:
    subparser.add_argument("--native", action="store_true",
                           help="With --as sheet: retain a pinned editable sheet in the scoped outbox. "
                                "Supports starts, parent labels, source trim/hold, explicit asset/transition clears "
                                "and built-in literal params in canvas units; replaces the previous export.")
    subparser.epilog = (
        "Next: use `timelines visualize` for composed pixels. For edits, use the "
        "timeline_editing guide. Literal numeric moves/trims use `timelines edit`. "
        "Use `timelines visualize --help` for the visual controls."
    )
    _add_project_arg(subparser, required=False)
    subparser.add_argument(
        "ref", nargs="?", default=None,
        help="Optional timeline UUID, ULID, or slug; omit to use the project's default timeline.",
    )
    subparser.add_argument(
        "--summary",
        action="store_true",
        help="One line per chapter (name, span, cut count, first words); with a working copy, its edits too.",
    )
    subparser.add_argument(
        "address", nargs="?", default=None,
        help='One thing\'s complete record: a layer (c41.mink), a cut (c30), a word ("Building" in v27), a time, '
        "an asset key or a layer name. Every param (long lists counted: --full prints them), moments in seconds.",
    )
    subparser.add_argument("--full", action="store_true", help="With an address: long params (a 41-item list) in full.")
    subparser.add_argument("--keys", default=None, metavar="K1,K2", help="With an address: only these params, in full.")
    subparser.add_argument("--page", type=int, default=None, help="Cut table page N (1-based); with no --page-size, 20 cuts a page.")
    subparser.add_argument("--page-size", type=int, default=None, help="Cuts per page (default: all cuts).")
    subparser.add_argument("--at", default=None, help="The one cut on screen at SECONDS or m:ss (or a word): its clips, the words around it, next cuts.")
    subparser.add_argument("--clips", action="store_true", default=False, help="With --shot N: every clip in that chapter, one line each.")
    subparser.add_argument("--published", action="store_true", default=False,
                           help="Read published current explicitly (the default).")
    subparser.add_argument("--draft", default=None, metavar="NAME",
                           help="Read this named working copy explicitly; output identifies it and its base revision.")
    subparser.add_argument(
        "--as",
        dest="presentation",
        choices=("cuts", "script", "code", "sheet"),
        default=None,
        help="cuts (default): one row per cut. sheet: the cut sheet, an A/V script you can edit and "
        "`timelines apply` (with --range \"word\"..\"word\" or A..B seconds or c30..c31). script: the words in "
        "time order with cuts, layer entrances and element keyframes between them. code: shots and cuts as a "
        "readable program of element calls with resolved keyframes and each element's source path.",
    )
    subparser.add_argument(
        "--occurrence",
        default=None,
        help="Restrict the bounded inspection projection to one exact authored occurrence id.",
    )
    subparser.add_argument("--clip", default=None, help="Restrict the inspection projection to one authored clip id.")
    subparser.add_argument("--shot", default=None, help="Restrict the inspection projection to one authored shot id.")
    subparser.add_argument("--track", action="append", default=None, help="Restrict the inspection projection to one or more tracks.")
    subparser.add_argument("--asset", default=None, help="Restrict the inspection projection to one canonical asset key.")


    subparser.add_argument("--range", dest="range", default=None, action=_show_JoinRanges,
                           help="START..END (seconds, cut ids c10..c12 by time, or words); with --as sheet also a list "
                                "c10,c29,c33a (repeating --range adds to it).")
    subparser.add_argument("--limit", type=int, default=50, help="Maximum bounded inspection rows (1–100).")
    subparser.add_argument("--cursor", default=None, help="Continue a bounded inspection page from its cursor.")
    subparser.add_argument("--revision-id", default=None, help="Inspect an exact immutable timeline revision.")
    subparser.add_argument("--detail", action="store_true", default=False, help="Include full bounded text for selected clips.")
    subparser.add_argument("--layers", action="store_true", default=False,
                           help="Use the bounded per-track Runtime inspection projection.")
    # ``show`` is the human inspection route by default. Machine callers use
    # the explicit stable envelope switch and keep the SDK shape unchanged.
    _add_json_flag(subparser, default=False)
    subparser.set_defaults(handler=_cmd_show)


def _configure_status(subparser: argparse.ArgumentParser) -> None:
    _add_project_arg(subparser, required=False)
    subparser.add_argument("ref", help="Timeline id or slug to inspect.")
    subparser.add_argument("--draft", default=None, help="Working copy name (default: current working copy).")
    subparser.add_argument("--text", "--human", dest="text", action="store_true",
                           help="Print the human summary (changes in plain words, check, next steps) instead of the envelope.")
    subparser.add_argument("--all", action="store_true", help="With --text: every change and the full check report.")
    subparser.add_argument("--render", action="store_true",
                           help="With --text: also look up the latest render of this timeline (scans the run history, ~10 s).")
    _add_json_flag(subparser)
    subparser.set_defaults(handler=_as_timeline_verb(_guarded(2, _cmd_status)))


def _configure_update_element(subparser: argparse.ArgumentParser) -> None:
    _add_project_arg(subparser)
    subparser.add_argument('ref',help='Exact target timeline ID or slug.')
    subparser.add_argument('--realm',required=True,help='Canonical realm used by the configured project pack admission.')
    subparser.add_argument('--clip-id',required=True)
    subparser.add_argument('--pack',required=True)
    subparser.add_argument('--element',required=True)
    subparser.add_argument('--kind',choices=['effect'],default='effect')
    subparser.add_argument('--revision',required=True)
    subparser.add_argument('--expected-head',required=True)
    subparser.add_argument('--duration',type=float)
    subparser.add_argument('--params-json',type=_parse_json_object)
    _add_idempotency_key(subparser)
    _add_json_flag(subparser)
    subparser.set_defaults(handler=_cmd_update_element)


_verify_VISUALIZE_LABELS_HELP = (
    "With --view motion or --view contact, include the labels overlay without changing the selected view."
)


def _verify_add_visualize_labels_flag(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--labels", action="store_true", default=False, help=_verify_VISUALIZE_LABELS_HELP)


def _visualize_add_visualize_flags(subparser: argparse.ArgumentParser) -> None:
    """The flags ``visualize`` had before the combined port (working copy, compare, plan, layers, review)."""
    subparser.add_argument("--published", action="store_true", default=False,
                           help="Read published current explicitly (the default).")
    subparser.add_argument("--draft", default=None, metavar="NAME",
                           help="Read this named working copy explicitly; output identifies it and its base revision.")
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


PRESET_CHOICES = ("overview", "scan", "motion", "beat", "frame", "cut", "compare")


def _visualize_lenient_json_or_path(value: str) -> Any:
    """``--beats`` / ``--rules`` take the JSON object itself or a path to the file that holds it."""
    text = str(value)
    if text.lstrip().startswith("{") or text.lstrip().startswith("["):
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            raise argparse.ArgumentTypeError(f"invalid JSON: {exc.msg}") from exc
        return parsed
    return text


def _json_object_or_path(value: str) -> Any:
    """A JSON object given inline, or a path to the file that holds it (resolved by the verb)."""


    return _visualize_lenient_json_or_path(value)


def _visualize_format_argument(value: str) -> str:
    """Validate one bounded visualization format argument."""
    values = [part.strip().lower() for part in value.split(",") if part.strip()]
    if not values:
        raise argparse.ArgumentTypeError("format must name png or md")
    invalid = sorted(set(values) - {"png", "md"})
    if invalid:
        raise argparse.ArgumentTypeError(
            f"invalid visualization format(s): {', '.join(invalid)}; choose png or md"
        )
    return ",".join(values)


def _configure_visualize(subparser: argparse.ArgumentParser) -> None:
    subparser.epilog = (
        "Next: use `timelines show` for authored rows and timing. For edits, use the "
        "timeline_editing guide. Literal numeric moves/trims use `timelines edit`. "
        "Use this help page for component, sampling, frame, and resolution controls."
    )
    _add_project_arg(subparser, required=False)
    subparser.add_argument(
        "timeline_ref", nargs="?", default=None,
        help="Optional positional timeline slug, UUID, or ULID (prefer --timeline-slug).",
    )
    subparser.add_argument(
        "--timeline-slug",
        default=None,
        help="Timeline slug, UUID, or ULID; omit to use the project default.",
    )
    subparser.add_argument(
        "--shot", default=None,
        help=(
            "Focus an authored shot id or exact name; use 'first' or a positive "
            "one-based authored-order ordinal (for example 1) for friendly shot selection."
        ),
    )
    subparser.add_argument("--range", dest="range", default=None, help="Zoom to a closed-open START..END seconds window.")
    subparser.add_argument("--at", default=None,
                           help="Any address: seconds (7.2), a word ('\"Building\" in v27', viral), a cut (c30), a layer "
                                "(c41.mink). Ambiguous words list their scoped forms. Alone: one frame; with --preset "
                                "motion: 1 s around it, every frame.")
    subparser.add_argument("--frame", type=int, default=None, help="Capture one exact rendered frame number.")
    subparser.add_argument("--revision-id", default=None, help="Capture/inspect an exact immutable timeline revision.")
    subparser.add_argument("--clip", default=None, help="Focus an authored clip id.")
    subparser.add_argument("--occurrence", default=None, help="Focus an exact authored shot occurrence id.")
    subparser.add_argument("--asset", default=None, help="Focus a canonical asset key.")
    subparser.add_argument(
        "--show", action="append", default=None, metavar="COMPONENT[,COMPONENT...]",
        help=(
            "Add synchronized components: inputs, output, text, or audio "
            "(default composed view: output with synchronized inputs, text, and audio; "
            "use --hide inputs for output-only)."
        ),
    )
    subparser.add_argument(
        "--hide", action="append", default=None, metavar="COMPONENT[,COMPONENT...]",
        help="Hide components from the resolved surface.",
    )
    subparser.add_argument(
        "--track", action="append", default=None, metavar="TRACK_ID",
        help="Restrict input lanes; repeat for multiple tracks.",
    )
    subparser.add_argument(
        "--detail", action="store_true", default=None,
        help="Use enlarged frame, waveform, and text panels; combine with --range/--shot for a focused inspection.",
    )
    subparser.add_argument("--context", type=float, default=None, help="Context seconds around a focus.")
    subparser.add_argument("--neighbors", type=int, default=None, help="Neighbor clips retained around a focus.")
    subparser.add_argument(
        "--format", dest="formats", action="append", default=None,
        metavar="FORMAT[,FORMAT...]",
        type=_visualize_format_argument,
        help="Repeatable/comma-separated png or md (default: png,md).",
    )
    subparser.add_argument(
        "--mode",
        choices=("auto", "inputs", "composed"),
        default="auto",
        help=(
            "auto always composes frames from the pinned saved revision; "
            "inputs is render-free; composed always requests composed frames."
        ),
    )
    subparser.add_argument(
        "--view",
        choices=("filmstrip", "contact", "overview", "motion", "diff"),
        default=None,
        help=(
            "Usually leave this out and use --preset (or just give a window). filmstrip: Rendered paired filmstrip "
            "with input lanes (the only view for drill-down pages; chosen when --show/--sample/--track is used). "
            "contact (overview): ONE overview page of the whole video, one tile per picture cut (capped at 120), "
            "480x270. motion: one cut (--cut N) as a motion sheet you can judge as stills: frame strip, onion skins, "
            "motion curves, word/beat/sfx sync, stillness, lip-sync and findings (<= 60 frames at 480x270). "
            "diff: immutable revision diff, before/after tiles of only the cuts an edit changed, and the SCOPE "
            "condition; needs --before-revision-id/--after-revision-id (or --from/--to)."
        ),
    )
    subparser.add_argument(
        "--preset", choices=PRESET_CHOICES, default=None,
        help=(
            "What you want to see, one page each: overview (whole film, one tile per cut); scan (a --range at "
            "2 fps, 8 per row, up to 30 s); motion (<= 3 s at every 2nd frame, big 640x360 frames, 3 per row, onion "
            "skins + curves); beat (a frame at each word onset, music hit and sfx, labelled); frame (one exact "
            "frame, 1280x720, with --at or --frame); cut (a cut's motion sheet, with --cut); compare (--from REV: "
            "before/after of what changed). Defaults: no window -> overview, --range -> scan, --at -> frame, "
            "--cut -> cut."
        ),
    )
    subparser.add_argument(
        "--before-revision-id", default=None,
        help="Required with --view diff: exact immutable before revision; no current-head fallback.",
    )
    subparser.add_argument(
        "--after-revision-id", default=None,
        help="Required with --view diff: exact immutable after revision; no current-head fallback.",
    )
    subparser.add_argument("--cut", default=None, help="Motion cut number, picture clip id, or @seconds.")
    subparser.add_argument(
        "--layer", dest="layers", action="append", default=None,
        help="Motion/contact layer name; repeat or comma-separate registered layers.",
    )

    _verify_add_visualize_labels_flag(subparser)
    subparser.add_argument(
        "--beats", type=_json_object_or_path, default=None, metavar="BEATS_JSON",
        help="A cue's beats.json file (or the JSON object itself) with beats/downbeats/hits in cue seconds; "
             "overrides the beats the music clips carry (app.beats).",
    )
    subparser.add_argument("--frame-budget", dest="frame_budget", type=int, default=None,
                           help="Motion frame budget (1–120; default 60).")
    subparser.add_argument("--window", dest="window_s", type=float, default=None,
                           help="Motion window width in seconds around --at.")
    subparser.add_argument(
        "--rules", type=_json_object_or_path, default=None, metavar="FILE",
        help="motion view: project rules TOML (default: the nearest astrid-lint.toml), or the rules JSON object "
             "(params, severity, disable, path).",
    )
    subparser.add_argument("--cuts", default=None, help="Contact-only positive cut numbers, comma-separated (max 120).")
    subparser.add_argument("--preview", action="store_true", default=None,
                           help="Motion-only GIF from the already captured frames.")
    subparser.add_argument("--sample", choices=("interval", "clips", "cuts", "shots"), default=None,
                           help="Filmstrip sampling: interval (default), picture clips, cut boundaries, or authored story beats.")
    sampling = subparser.add_mutually_exclusive_group()
    sampling.add_argument("--every", type=float, default=None,
                          help="Filmstrip interval in seconds (default: 0.5); rerun with --range START..END to zoom.")
    sampling.add_argument("--every-frames", type=int, default=None,
                          help="Filmstrip interval in exact rendered frames; replaces --every (rerun for finer samples).")
    subparser.add_argument(
        "--include-cuts", action="store_true", default=None,
        help="With interval sampling, also capture visual cut-neighbor frames.",
    )
    subparser.add_argument(
        "--render-run", default=None,
        help=(
            "Explicit successful render run, or latest (filmstrip default). "
            "An explicit run may be historical/candidate; auto reuse checks the current timeline head."
        ),
    )
    subparser.add_argument("--columns", type=int, default=None,
                           help="Filmstrip contact sheet columns (default: 5; paired pages use one row by default).")
    subparser.add_argument("--page-size", type=int, default=None,
                           help="Filmstrip cards per static page (default: 50 standalone; paired pages use one row, or explicitly opt into up to two rows / 10 cards).")
    subparser.add_argument("--resolution", default=None, metavar="WIDTHxHEIGHT",
                           help="Filmstrip frame resolution, e.g. 960x540; recorded and applied exactly by the executor.")
    subparser.add_argument(
        "--include-media", action="store_true", default=None,
        help="Include a relative, digest-verified rendered video for offline filmstrip playback.",
    )
    subparser.add_argument(
        "--out", default=None, metavar="DIR",
        help=(
            "Also copy the view's page(s) and its raw frames (frames/, frame-index.json) to DIR once it "
            "settles; the managed copy stays where the summary says."
        ),
    )


    _visualize_add_visualize_flags(subparser)
    # Human summary by default (page path, counts, timing, next commands), like show/diff; --json opts into the SDK envelope.
    _add_json_flag(subparser, default=False)
    subparser.set_defaults(handler=_cmd_visualize)


def _working_timeline_args(subparser: argparse.ArgumentParser, *, required_timeline: bool = True, project_required: bool = True) -> None:
    if required_timeline:
        subparser.add_argument("ref", help="Timeline UUID, ULID, or slug.")
    else:
        subparser.add_argument("ref", nargs="?", default=None, help="Timeline UUID, ULID, or slug (or use --file).")
    _add_project_arg(subparser, required=project_required)
    subparser.add_argument("--draft", default=None, help="Working copy name (default: current working copy).")


def _working_configure_discard(subparser: argparse.ArgumentParser) -> None:
    subparser.description = (
        "Drop the working copy and recoverable unsent session intent. A request with an unknown commit outcome "
        "is retained; discard does not undo a possibly accepted commit."
    )
    _working_timeline_args(subparser, project_required=False)
    subparser.set_defaults(handler=_working_cmd_discard)


def _working_configure_duplicate(subparser: argparse.ArgumentParser) -> None:
    subparser.description = (
        "Create a new timeline holding a copy of this timeline's published head, with new shot ids "
        "(shot ids are global per project). Narration is bound again from the lines' script text when they declare it."
    )


    subparser.add_argument("ref", help="Source timeline (UUID, ULID or slug); read only.")
    _add_project_arg(subparser)
    subparser.add_argument("--slug", default=None, help="Slug for the new timeline (default: its generated id).")
    subparser.add_argument("--name", default=None, help="Display name for the duplicate.")
    subparser.add_argument("--revision", default=None, help="Copy this parent revision instead of the current head.")
    subparser.set_defaults(handler=_working_cmd_duplicate)


def _working_configure_rename(subparser: argparse.ArgumentParser) -> None:
    subparser.add_argument("ref", help="Timeline id or slug.")
    subparser.add_argument("name", help="New display name.")
    _add_project_arg(subparser)
    _add_json_flag(subparser)
    subparser.set_defaults(handler=_working_cmd_rename)


def _working_configure_restructure(subparser: argparse.ArgumentParser) -> None:
    subparser.description = (
        "Store each chapter (from the chapter labels) as a real shot, and the music and any clip crossing a chapter "
        "wall as film-level clips. Editing is unchanged (one flat timeline in film seconds). The change is published only "
        "if the renderer input before and after is identical; --dry-run prints the walls and what goes where."
    )


    subparser.add_argument("ref", help="Timeline (UUID, ULID or slug).")
    _add_project_arg(subparser)
    subparser.add_argument("--dry-run", action="store_true", help="Print the plan and the equivalence check; publish nothing.")
    subparser.add_argument("--name", default=None, help="Display name for the restructured timeline.")
    subparser.add_argument("--keep-original", metavar="NAME", default=None,
                           help="Duplicate the current head under this name before restructuring.")
    subparser.add_argument("-m", "--message", default=None, help="Revision message.")
    subparser.set_defaults(handler=_working_cmd_restructure)


def _working_configure_lines(subparser: argparse.ArgumentParser) -> None:
    subparser.description = "The narration line by line: id, when it is spoken, the silence after it, its text and take."
    _working_timeline_args(subparser, project_required=False)
    subparser.add_argument("--published", action="store_true", help="Read the published head, not the working copy.")
    subparser.add_argument("--line", default=None, metavar="ID", help="One line in full: its text, take, span, gap, "
                                                                        "every word with its time, and the cuts it is under.")
    subparser.set_defaults(handler=_working_cmd_lines)


def _working_configure_words(subparser: argparse.ArgumentParser) -> None:
    _working_timeline_args(subparser, required_timeline=False, project_required=False)
    subparser.add_argument("word", nargs="?", default=None,
                           help='One word: its scoped form, time, cut, what is on screen and what is anchored to it '
                                '(Live, \'"Building" in v27\'); a word said more than once lists its scoped forms.')
    subparser.add_argument("--beats", action="store_true", help="With a word: the music beats near it, with their moments.")
    subparser.add_argument("--file", default=None, help="Read a local checkout file instead.")
    subparser.add_argument("--published", action="store_true", help="Read the published head, not the working copy.")
    subparser.add_argument("--find", default=None, help="Only words containing this text.")
    subparser.add_argument("--range", default=None, metavar="A..B", help="Only words between two times (seconds or m:ss).")
    subparser.set_defaults(handler=_working_cmd_words)


def _working_configure_lint(subparser: argparse.ArgumentParser) -> None:
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
    subparser.set_defaults(handler=_working_cmd_lint)


def _configure_working_verb(name: str):
    return {'discard': _working_configure_discard, 'duplicate': _working_configure_duplicate, 'rename': _working_configure_rename, 'restructure': _working_configure_restructure, 'lines': _working_configure_lines, 'words': _working_configure_words, 'lint': _working_configure_lint}[name]


_parser_CURRENT_PROJECT_VERBS = frozenset(
    {"checkout", "apply", "assets", "check", "commit", "create", "discard", "find", "history", "lines", "redo", "status", "undo"}
)



class _parser_TimelinesParser(argparse.ArgumentParser):
    """``show TL --project P c41.mink``: a positional after the flags fills the verb's open positional
    (address, word, query, sheet file, asset key) instead of failing with "unrecognized arguments"."""

    def parse_args(self, args: Any = None, namespace: Any = None) -> argparse.Namespace:
        args = list(sys.argv[1:] if args is None else args)
        parsed, extras = self.parse_known_args(args, namespace)
        loose = [e for e in extras if not e.startswith("-")]
        if extras and len(loose) == len(extras):
            for dest in ("address", "word", "query", "sheet", "sheet_file", "key", "steps"):
                if loose and getattr(parsed, dest, "absent") is None:
                    value = loose.pop(0)
                    if dest == "steps":
                        if not value.isdigit() or int(value) < 1:
                            self.error(f"steps must be a positive integer, got {value!r}")
                        value = int(value)
                    setattr(parsed, dest, value)
            if loose and getattr(parsed, "address", None) and getattr(parsed, "command", None) == "show":
                parsed.more_addresses, loose = list(loose), []  # show TL a b c: one record each
        if loose or len(extras) != len([e for e in extras if not e.startswith("-")]):
            self.error(f"unrecognized arguments: {' '.join(extras)}")
        if (getattr(parsed, "command", None) in _parser_CURRENT_PROJECT_VERBS
                and not getattr(parsed, "project", None)
                and (getattr(parsed, "command", None) == "commit" or not getattr(parsed, "file", None))):
            project = resolve_current_project(parsed)  # an explicit --project always wins
            if project is None:
                self.error(
                    f"{parsed.command}: no --project given and no current project is selected "
                    "(pass --project P, or run: astrid projects select <project>)"
                )
            parsed.project = project
        return parsed


def _working_configure_transcripts_sync(subparser: argparse.ArgumentParser) -> None:
    subparser.add_argument("ref", help="Timeline UUID, ULID, or slug.")
    _add_project_arg(subparser, required=True)
    subparser.add_argument("--dry-run", action="store_true", help="Print create/update/unchanged and skips without writing documents.")
    subparser.set_defaults(handler=_working_cmd_transcripts_sync)


_FAMILY = "timelines"


COMMANDS: tuple[CommandSpec, ...] = (
    CommandSpec("import", help="Import a validated .astrid timeline archive into a project.", configure=_configure_import),
    CommandSpec("export", help="Export the saved head and its media into a .astrid archive.", configure=_configure_export),
    CommandSpec("checkout", help="Create or open a detached editable working copy.", configure=_configure_checkout),
    CommandSpec("commit", help="Commit a detached timeline document through Runtime.", configure=_configure_commit),
    CommandSpec("edit", help="Commit a clip move or trim, or edit a legacy detached working copy.", configure=_configure_edit),
    CommandSpec("assets", help="List timeline asset keys, media details, and every clip using each key.", configure=_configure_assets),
    CommandSpec("find", help="Find what you hear or see: addresses and times.", configure=_configure_find),
    CommandSpec("apply", help="Apply supported cut-sheet rows through Runtime, or edit a detached working copy.", configure=_configure_apply),
    CommandSpec("undo", help="Undo local edits, or an accepted Runtime commit with --commit.", configure=lambda p: _configure_history_edit(p, _cmd_undo)),
    CommandSpec("redo", help="Redo local edits, or reverse an accepted undo with --commit.", configure=lambda p: _configure_history_edit(p, _cmd_redo)),
    CommandSpec("check", help="Validate a working copy and optionally sweep every cut.", configure=_configure_check),
    CommandSpec("status", help="Show the canonical head, working copy, and connected commit state.", configure=_configure_status),
    CommandSpec("discard", help="Drop recoverable working edits; retain commits with unknown outcomes.", configure=_configure_working_verb("discard")),
    CommandSpec("pending", help="List, inspect, or drop saved edits that did not land.",
                configure=_configure_pending_verb("pending")),
    CommandSpec("retry", help="Preview or commit a saved edit after a timeline clash.",
                configure=_configure_pending_verb("retry")),
    CommandSpec("duplicate", help="Copy a timeline's published head into a new timeline, for a trial.", configure=_configure_working_verb("duplicate")),
    CommandSpec("rename", help="Set a timeline's display name.", configure=_configure_working_verb("rename")),
    CommandSpec("restructure", help="Store chapters as shots and music / wall-crossing clips on the film (proven equal first).", configure=_configure_working_verb("restructure")),
    CommandSpec("lines", help="The narration line by line: id, when it is spoken, the gap after it, its text and take.", configure=_configure_working_verb("lines")),
    CommandSpec("words", help="List the spoken words with their times, or one word in full.", configure=_configure_working_verb("words")),
    CommandSpec(
        "create",
        help="Create an empty timeline with its first canonical head.",
        configure=_configure_create,
    ),
    CommandSpec(
        "list",
        help="List active timelines in a project (slug ascending).",
        configure=_configure_list,
    ),
    CommandSpec(
        "show",
        help="Show one timeline by UUID, ULID, or slug (use --summary for editor context).",
        configure=_configure_show,
    ),
    CommandSpec(
        "script",
        help="Read placed narration in timeline order from pinned shot text revisions.",
        configure=_configure_script,
    ),
    CommandSpec("elements",help="Inspect exact saved effect refs, editable pack files and admission/publication commands.",configure=_configure_elements),
    CommandSpec("update-element",help="Update the same parent effect from an admitted file pack using exact-head CAS.",configure=_configure_update_element),
    CommandSpec("place-element",help="Place an admitted effect on an am-discord occurrence shot clip using exact-head CAS.",configure=_configure_place_element),
    CommandSpec(
        "replace-parent-media",
        help="Atomically replace one clip in an exact canonical parent-composition closure.",
        configure=_configure_replace_parent_media,
    ),
    CommandSpec(
        "archive",
        help="Archive a timeline (reversible with recover).",
        configure=_configure_archive,
    ),
    CommandSpec(
        "recover",
        help="Restore archived work; safe to repeat (changed=false when active).",
        configure=_configure_recover,
    ),
    CommandSpec(
        "history",
        help="Ordered lifecycle event history for one timeline.",
        configure=_configure_history,
    ),
    CommandSpec(
        "log",
        help="Read and verify the hash-chained edit journal for one timeline.",
        configure=_configure_log,
    ),
    CommandSpec(
        "diff",
        help="Deterministic adjacent-version diffs for one timeline.",
        configure=_configure_diff,
    ),
    CommandSpec(
        "visualize",
        help="Build a timeline evidence pack synchronously through the public SDK.",
        configure=_configure_visualize,
        requires_pack_host=True,
    ),
    CommandSpec(
        "lint",
        help="Run every registered check over every picture cut, from timeline data (no capture).",
        configure=_configure_working_verb("lint"),
    ),
    CommandSpec(
        "inspect",
        help="Read bounded named sections of a verified filmstrip bundle offline.",
        configure=_configure_inspect,
    ),
    CommandSpec(
        "render",
        help="Render a canonical kernel timeline with optional version pinning.",
        configure=_configure_render,
        requires_pack_host=True,
    ),
)


def build_parser(client: Any) -> argparse.ArgumentParser:
    """Build the ``timelines`` product-family parser stamped with *client*.

    Exactly the supported verbs above are registered — no aliases, no legacy
    migration/push/pull/sync/audit/erase/repair verbs, and no ``copy``
    (reserved for m6) — plus the manifest-declared nested ``shots`` mount
    (``astrid timelines shots <verb>``) embedded from the shots product
    parser.
    """
    from astrid.core.cli import domain_shots as shots_cli

    def _configure_shots(subparser: argparse.ArgumentParser) -> None:
        nested = subparser.add_subparsers(dest="shot_command", required=True)
        register_product_commands(nested, shots_cli.COMMANDS, family="shots", client=client)

    def _configure_transcripts(subparser: argparse.ArgumentParser) -> None:


        nested = subparser.add_subparsers(dest="transcript_command", required=True)
        register_product_commands(nested, (CommandSpec("sync", help="Backfill narration transcript documents from published VO clips.",
                                                       configure=_working_configure_transcripts_sync),),
                                  family=_FAMILY, client=client)


    parser = _parser_TimelinesParser(
        prog="astrid timelines",
        description=(
            "Timeline check/create/list/show/script/replace-parent-media/archive/recover/history/log/diff/visualize/lint/render/lines/words/discard/pending/retry/duplicate "
            "(product family); nested shots beneath 'timelines shots'."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    register_product_commands(
        subparsers,
        (
            *COMMANDS,
            CommandSpec(
                "shots",
                help="Nested project-level shot list/create/show/add/remove/reorder "
                "(manifest-owned mount).",
                configure=_configure_shots,
            ),
            CommandSpec("transcripts", help="Sync shared media transcripts from narration clips.",
                        configure=_configure_transcripts),
        ),
        family=_FAMILY,
        client=client,
    )
    return parser
