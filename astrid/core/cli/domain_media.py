"""Product media family CLI.

This module is the product parser for the ``media`` family: every verb is
**argument parsing plus delegation** through the composed
:class:`~astrid.sdk.client.AstridClient` (stamped onto every subparser by
:func:`astrid.core.cli.registration.register_product_commands`), and every
handler renders through the shared product output layer
(:mod:`astrid.core.cli.domain_output`) so the exact five-key JSON envelope,
concise human output, and stable exit codes stay aligned with the frozen SDK
contract.

Core media verbs include:

- ``import <path>`` — accepts **only files and folders**: a regular file
  routes to ``client.media.import_file`` and a directory routes to
  ``client.media.import_directory`` (each file commits its own receipt and
  child key); video/audio containers are strictly probed with ``ffprobe``
  before any media/event/receipt write, and an undecodable container is a
  typed validation failure; any other path is a usage error before any SDK call;
- ``list`` — ``client.media.list`` (project-scoped, created_at then id);
- ``show <ref>`` — ``client.media.show`` by exact project-scoped media id
  (aliases resolve through the repository; cross-project ids are
  indistinguishable from missing);
- ``verify <ref>`` — ``client.media.verify`` with the required managed
  ``--realm managed_local`` (runtime-CAS verification; unsupported
  path-backed realms are rejected before any SDK call);
- ``relate`` — ``client.media.relate`` with ``--from``/``--to``/``--kind``
  restricted to the frozen five media relation kinds (``derived_from``,
  ``variant_of``, ``uses_as_input``, ``mask_for``, ``audio_for``) plus
  optional ``--ordinal``/``--metadata``.

The parser also mounts the reviewed runtime-owned nested ``references`` family
beneath ``media`` (``references: media references``): ``astrid media references <verb>``
embeds the references product parser (``astrid/core/cli/domain_references.py``)
so reference ``create/update/archive/associate/link/set-primary/list/show`` are
executable only beneath media. There is **no
top-level references family** (sense check SC30).

Handlers parse argv, delegate to the SDK or media service, and render the
returned envelope.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Sequence

from astrid.core.cli.domain_output import print_result
from astrid.core.cli.registration import CommandSpec, register_product_commands
from astrid.core.media_crop import DEFAULT_BAR_THRESHOLD

__all__ = ["COMMANDS", "build_parser"]

_FAMILY = "media"

REALMS: tuple[str, ...] = ("managed_local",)
"""The sole supported local media realm.

Local-v1 is ingest-only: runnable bytes are owned by the neutral runtime's
CAS.  The former path-backed realm was a persistent path authority and is
intentionally not part of the product parser.
"""

MEDIA_RELATION_KINDS: tuple[str, ...] = (
    "derived_from",
    "variant_of",
    "uses_as_input",
    "mask_for",
    "audio_for",
)
"""The frozen five media relation kinds (m1 decision artifact section 7)."""


def _parse_json_object(value: str) -> dict[str, Any]:
    """Parse a ``--metadata`` JSON object argument (usage error otherwise)."""
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise argparse.ArgumentTypeError(
            f"invalid JSON object: {exc.msg}"
        ) from exc
    if not isinstance(parsed, dict):
        raise argparse.ArgumentTypeError("must be a JSON object")
    return parsed


def _existing_file_or_directory(value: str) -> str:
    """Accept only an existing regular file or directory (usage error else)."""
    path = Path(value)
    if path.is_file() or path.is_dir():
        return value
    raise argparse.ArgumentTypeError(
        f"import accepts only an existing file or directory, got {value!r}"
    )


def _add_json_flag(subparser: argparse.ArgumentParser) -> None:
    subparser.add_argument(
        "--json",
        action="store_true",
        default=False,
        help="Print the exact SDK envelope (ok/data/error/receipt/idempotency_key).",
    )


def _add_idempotency_key(subparser: argparse.ArgumentParser) -> None:
    subparser.add_argument(
        "--idempotency-key",
        dest="idempotency_key",
        default=None,
        help="Caller idempotency key (a fresh key is generated when absent).",
    )


def _add_project_arg(subparser: argparse.ArgumentParser) -> None:
    subparser.add_argument(
        "--project",
        required=True,
        default=None,
        help="Owning project id or immutable slug.",
    )


def _add_realm_arg(
    subparser: argparse.ArgumentParser, *, required: bool, default: str | None
) -> None:
    subparser.add_argument(
        "--realm",
        choices=REALMS,
        required=required,
        default=default,
        help="Media realm to operate on "
        f"(choices: {', '.join(REALMS)}).",
    )


# -- handlers (one SDK call each, no domain rules) -------------------------


def _cmd_import(parsed: argparse.Namespace) -> int:
    path = Path(parsed.path)
    if path.is_dir():
        result = parsed.client.media.import_directory(
            project=parsed.project,
            directory=path,
            realm=parsed.realm,
            idempotency_key=parsed.idempotency_key,
        )
    else:
        result = parsed.client.media.import_file(
            project=parsed.project,
            path=path,
            realm=parsed.realm,
            idempotency_key=parsed.idempotency_key,
        )
    return print_result(result, as_json=parsed.json)


def _cmd_list(parsed: argparse.Namespace) -> int:
    result = parsed.client.media.list(
        parsed.project,
        **{
            name: value
            for name, value in (
                ("kind", parsed.kind),
                ("model", parsed.model),
                ("target", parsed.target),
                ("tag", parsed.tag),
            )
            if value is not None
        },
    )
    return print_result(result, as_json=parsed.json)


def _cmd_show(parsed: argparse.Namespace) -> int:
    result = parsed.client.media.show(parsed.project, parsed.ref)
    return print_result(result, as_json=parsed.json)


def _open_ref(value: str) -> object:
    """Decode a structured returned opening action, retaining plain ids."""
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return value
    if not isinstance(parsed, (dict, str)):
        raise argparse.ArgumentTypeError("open reference must be a JSON object or string id")
    return parsed


def _cmd_open(parsed: argparse.Namespace) -> int:
    result = parsed.client.media.open(
        parsed.project,
        parsed.ref,
        materialize=parsed.materialize,
        cache_root=parsed.cache_root,
        preview_bytes=parsed.preview_bytes,
        max_bytes=parsed.max_bytes,
    )
    return print_result(result, as_json=parsed.json)


def _cmd_verify(parsed: argparse.Namespace) -> int:
    kwargs = {
        "realm": parsed.realm,
        "idempotency_key": parsed.idempotency_key,
    }
    if parsed.location_id is not None:
        kwargs["location_id"] = parsed.location_id
    if parsed.locator is not None:
        kwargs["locator"] = parsed.locator
    result = parsed.client.media.verify(parsed.project, parsed.ref, **kwargs)
    return print_result(result, as_json=parsed.json)


def _cmd_relate(parsed: argparse.Namespace) -> int:
    relation: dict[str, Any] = {
        "from_media_id": parsed.from_media,
        "to_media_id": parsed.to_media,
        "kind": parsed.kind,
    }
    if parsed.ordinal is not None:
        relation["ordinal"] = parsed.ordinal
    if parsed.metadata is not None:
        relation["metadata"] = parsed.metadata
    result = parsed.client.media.relate(
        parsed.project,
        relations=[relation],
        idempotency_key=parsed.idempotency_key,
    )
    return print_result(result, as_json=parsed.json)


def _cmd_crop(parsed: argparse.Namespace) -> int:
    from astrid.core.media_crop import DEFAULT_BAR_THRESHOLD, crop_media

    if parsed.box is not None and parsed.bar_threshold is not None:
        print(
            "warning: --bar-threshold is ignored with --box (it only applies to --remove-bars)",
            file=sys.stderr,
        )
    result = crop_media(
        parsed.client,
        project=parsed.project,
        ref=parsed.ref,
        box=parsed.box,
        remove_bars=parsed.remove_bars,
        bar_threshold=DEFAULT_BAR_THRESHOLD if parsed.bar_threshold is None else parsed.bar_threshold,
        even=parsed.even,
        dry_run=parsed.dry_run,
        idempotency_key=parsed.idempotency_key,
        allow_no_bars=parsed.allow_no_bars,
    )
    code = print_result(result, as_json=parsed.json)
    audio = result.data.get("audio") if result.ok and isinstance(result.data, dict) else None
    if not parsed.json and isinstance(audio, dict) and audio.get("action") == "reencode":
        print(
            f"note: source audio codec {audio.get('codec')} cannot be copied into mp4; "
            "audio was re-encoded to AAC",
            file=sys.stderr,
        )
    return code


def _render_mask(result: Any) -> str:
    data = result.data
    lines = [data["object_id"], json.dumps(data["recipe"], ensure_ascii=False, sort_keys=True)]
    for sample in data.get("samples", []):
        changed = sample["changed_fraction"]
        change = "" if changed is None else f" ({changed:.1%} of mask pixels changed)"
        lines.append(f'{sample["seconds"]:.3f}s{change}')
    if "preview_path" in data:
        lines.append(data["preview_path"])
    return "\n".join(lines)


def _cmd_mask(parsed: argparse.Namespace) -> int:
    from astrid.core.media_mask import mask_media

    result = mask_media(
        parsed.client, project=parsed.project, name=parsed.name, ellipses=parsed.ellipse,
        boxes=parsed.box, grow=parsed.grow, size=parsed.size, invert=parsed.invert,
        mask_ref=parsed.mask, preview_on=parsed.preview_on, span=parsed.range,
        samples=parsed.samples,
    )
    return print_result(result, as_json=parsed.json, human_renderer=_render_mask)


def _cmd_thumbnail_backfill(parsed: argparse.Namespace) -> int:
    result = parsed.client.media.backfill_thumbnails(
        parsed.project,
        generation_id=parsed.generation_id,
        limit=parsed.limit,
        dry_run=parsed.dry_run,
    )
    return print_result(result, as_json=parsed.json)


def _cmd_variant_thumbnail_backfill(parsed: argparse.Namespace) -> int:
    result = parsed.client.media.backfill_variant_thumbnails(
        parsed.project,
        generation_id=parsed.generation_id,
        limit=parsed.limit,
        dry_run=parsed.dry_run,
    )
    return print_result(result, as_json=parsed.json)


def _cmd_timeline_thumbnail_backfill(parsed: argparse.Namespace) -> int:
    result = parsed.client.media.backfill_timeline_thumbnails(
        parsed.project,
        timeline=parsed.timeline,
        limit=parsed.limit,
        dry_run=parsed.dry_run,
    )
    return print_result(result, as_json=parsed.json)


# -- parser ----------------------------------------------------------------


def _configure_import(subparser: argparse.ArgumentParser) -> None:
    _add_project_arg(subparser)
    subparser.add_argument(
        "path",
        type=_existing_file_or_directory,
        help=(
            "Existing file or directory to import (files/folders only); "
            "video/audio containers must be ffprobe-decodable."
        ),
    )
    _add_realm_arg(subparser, required=False, default="managed_local")
    _add_idempotency_key(subparser)
    _add_json_flag(subparser)
    subparser.set_defaults(handler=_cmd_import)


def _configure_list(subparser: argparse.ArgumentParser) -> None:
    _add_project_arg(subparser)
    subparser.add_argument("--kind", default=None, help="Exact generation kind filter.")
    subparser.add_argument("--model", default=None, help="Exact generation model filter.")
    subparser.add_argument("--placement", dest="target", default=None, help="Exact generation placement filter.")
    subparser.add_argument("--tag", default=None, help="Exact generation tag filter.")
    _add_json_flag(subparser)
    subparser.set_defaults(handler=_cmd_list)


def _configure_show(subparser: argparse.ArgumentParser) -> None:
    _add_project_arg(subparser)
    subparser.add_argument("ref", help="Exact project-scoped media id.")
    _add_json_flag(subparser)
    subparser.set_defaults(handler=_cmd_show)


def _configure_open(subparser: argparse.ArgumentParser) -> None:
    _add_project_arg(subparser)
    subparser.add_argument(
        "ref",
        type=_open_ref,
        help="Returned structured opening reference (JSON object) or exact project media id.",
    )
    subparser.add_argument(
        "--materialize",
        action="store_true",
        help="Write a verified bounded copy to a disposable local path.",
    )
    subparser.add_argument(
        "--cache-root",
        default=None,
        help="Optional directory for explicit materialization.",
    )
    subparser.add_argument(
        "--preview-bytes",
        type=int,
        default=16 * 1024,
        help="Maximum inline preview bytes (default: 16384).",
    )
    subparser.add_argument(
        "--max-bytes",
        type=int,
        default=10 * 1024 * 1024,
        help="Maximum bytes read/materialized (default: 10485760).",
    )
    _add_json_flag(subparser)
    subparser.set_defaults(handler=_cmd_open)


def _configure_verify(subparser: argparse.ArgumentParser) -> None:
    subparser.description = (
        "Verify every local location in the selected realm. Results are "
        "deterministic and per-location; use one selector for a precise retry."
    )
    _add_project_arg(subparser)
    subparser.add_argument("ref", help="Exact project-scoped media id.")
    _add_realm_arg(subparser, required=True, default=None)
    selector = subparser.add_mutually_exclusive_group()
    selector.add_argument(
        "--location-id",
        default=None,
        help="Verify only this exact media-location id.",
    )
    selector.add_argument(
        "--locator",
        default=None,
        help="Verify only this exact realm locator.",
    )
    _add_idempotency_key(subparser)
    _add_json_flag(subparser)
    subparser.set_defaults(handler=_cmd_verify)


def _configure_relate(subparser: argparse.ArgumentParser) -> None:
    _add_project_arg(subparser)
    subparser.add_argument(
        "--from",
        dest="from_media",
        required=True,
        help="Source media id for the relation edge.",
    )
    subparser.add_argument(
        "--to",
        dest="to_media",
        required=True,
        help="Target media id for the relation edge.",
    )
    subparser.add_argument(
        "--kind",
        choices=MEDIA_RELATION_KINDS,
        required=True,
        help="Frozen relation kind "
        f"(choices: {', '.join(MEDIA_RELATION_KINDS)}).",
    )
    subparser.add_argument(
        "--ordinal",
        type=int,
        default=None,
        help="Optional edge ordinal (default: 0).",
    )
    subparser.add_argument(
        "--metadata",
        type=_parse_json_object,
        default=None,
        help="Optional edge metadata as a JSON object.",
    )
    _add_idempotency_key(subparser)
    _add_json_flag(subparser)
    subparser.set_defaults(handler=_cmd_relate)


def _cmd_hold(parsed: argparse.Namespace) -> int:
    from astrid.core.media_hold import hold_media

    result = hold_media(parsed.client, project=parsed.project, ref=parsed.ref,
                        duration=parsed.duration, fps=parsed.fps)
    return print_result(result, as_json=parsed.json)


def _configure_hold(subparser: argparse.ArgumentParser) -> None:
    _add_project_arg(subparser)
    subparser.add_argument("ref", help="Project media id of the still image.")
    subparser.add_argument("--duration", type=float, required=True, metavar="SECONDS")
    subparser.add_argument("--fps", type=int, default=24, help="Frames per second (default 24).")
    _add_json_flag(subparser)
    subparser.set_defaults(handler=_cmd_hold)


def _cmd_trim(parsed: argparse.Namespace) -> int:
    from astrid.core.media_trim import parse_range, trim_media

    try:
        start, end = parse_range(parsed.range)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    result = trim_media(
        parsed.client,
        project=parsed.project,
        ref=parsed.ref,
        start=start,
        end=end,
        dry_run=parsed.dry_run,
        idempotency_key=parsed.idempotency_key,
    )
    return print_result(result, as_json=parsed.json)


def _configure_trim(subparser: argparse.ArgumentParser) -> None:
    subparser.description = (
        "Trim one managed video to a time range into a NEW media item. The cut is "
        "frame-exact (video by frame, audio by sample). The source is untouched; the "
        "result is imported like 'media import' and linked to the source by a "
        "derived_from relation carrying the range."
    )
    _add_project_arg(subparser)
    subparser.add_argument("ref", help="Exact project-scoped source media id.")
    subparser.add_argument(
        "--range",
        dest="range",
        required=True,
        metavar="START,END",
        help="Seconds into the source: keep [START, END). END must be after START; "
        "both must fall on a frame at the source's frame rate.",
    )
    subparser.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        default=False,
        help="Print the frame and sample bounds; import nothing.",
    )
    _add_idempotency_key(subparser)
    _add_json_flag(subparser)
    subparser.set_defaults(handler=_cmd_trim)


def _configure_crop(subparser: argparse.ArgumentParser) -> None:
    subparser.description = (
        "Crop one managed still image or video into a NEW media item. The source "
        "is untouched; the result is imported like 'media import' and linked to "
        "the source by a derived_from relation carrying the exact box."
    )
    _add_project_arg(subparser)
    subparser.add_argument("ref", help="Exact project-scoped source media id.")
    mode = subparser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--box",
        default=None,
        help="Crop rectangle X,Y,W,H in source pixels.",
    )
    mode.add_argument(
        "--remove-bars",
        dest="remove_bars",
        action="store_true",
        default=False,
        help="Detect near-black uniform bands on any edge and crop them away.",
    )
    subparser.add_argument(
        "--bar-threshold",
        dest="bar_threshold",
        type=int,
        default=None,
        metavar="0..255",
        help="Max channel value still counted as black for --remove-bars "
        f"(default: {DEFAULT_BAR_THRESHOLD}; ignored with --box).",
    )
    subparser.add_argument(
        "--allow-no-bars",
        dest="allow_no_bars",
        action="store_true",
        default=False,
        help="With --remove-bars: import an uncropped copy when no bars are found "
        "(otherwise nothing is imported and the command says so).",
    )
    subparser.add_argument(
        "--even",
        action="store_true",
        default=False,
        help="Round width and height down to even values (video encoders).",
    )
    subparser.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        default=False,
        help="Print the box and output size; import nothing.",
    )
    _add_idempotency_key(subparser)
    _add_json_flag(subparser)
    subparser.set_defaults(handler=_cmd_crop)


def _configure_mask(subparser: argparse.ArgumentParser) -> None:
    _add_project_arg(subparser)
    subparser.add_argument("--name", help="Name to store in the mask recipe.")
    subparser.add_argument("--ellipse", action="append", default=[], metavar="CX,CY,RX,RY[,ROT_DEG]",
                           help="Fill an ellipse in working-frame pixels. Repeat for more shapes.")
    subparser.add_argument("--box", action="append", default=[], metavar="X,Y,W,H",
                           help="Fill a box in working-frame pixels. Repeat for more shapes.")
    subparser.add_argument("--grow", type=int, metavar="PX", help="Expand each shape by this margin.")
    subparser.add_argument("--size", metavar="WIDTHxHEIGHT", help="Working frame (default: 1024x576).")
    subparser.add_argument("--invert", action="store_true", help="Swap the white and black regions.")
    subparser.add_argument("--mask", metavar="MEDIA", help="Show the recipe for a stored mask, with an optional preview.")
    subparser.add_argument("--preview-on", metavar="MEDIA", help="Preview the mask over this image or video.")
    subparser.add_argument("--range", metavar="A,B", help="Video preview range in seconds.")
    subparser.add_argument("--samples", type=int, metavar="N", help="Number of video preview frames (default: 6).")
    _add_json_flag(subparser)
    subparser.set_defaults(handler=_cmd_mask)


def _configure_thumbnails(subparser: argparse.ArgumentParser) -> None:
    nested = subparser.add_subparsers(dest="thumbnail_command", required=True)
    backfill = nested.add_parser(
        "backfill",
        help="Extract missing Runtime Generation thumbnails through managed custody.",
    )
    _add_project_arg(backfill)
    backfill.add_argument(
        "--generation-id",
        default=None,
        help="Restrict the bounded scan to one Generation.",
    )
    backfill.add_argument(
        "--limit",
        type=int,
        default=50,
        choices=range(1, 101),
        metavar="1..100",
        help="Maximum Generations to scan (default: 50).",
    )
    backfill.add_argument(
        "--dry-run",
        action="store_true",
        help="Scan and extract eligibility without Runtime mutations.",
    )
    _add_json_flag(backfill)
    backfill.set_defaults(handler=_cmd_thumbnail_backfill)
    variants = nested.add_parser(
        "backfill-variants",
        help="Extract and attach source-correct posters for every visual variant.",
    )
    _add_project_arg(variants)
    variants.add_argument("--generation-id", default=None, help="Restrict the scan to one Generation.")
    variants.add_argument("--limit", type=int, default=100, choices=range(1, 501), metavar="1..500")
    variants.add_argument("--dry-run", action="store_true")
    _add_json_flag(variants)
    variants.set_defaults(handler=_cmd_variant_thumbnail_backfill)
    timeline = nested.add_parser(
        "backfill-timeline",
        help="Ensure missing source-frame thumbnails for a pinned canonical timeline.",
    )
    _add_project_arg(timeline)
    timeline.add_argument("--timeline", required=True, help="Canonical timeline id or slug.")
    timeline.add_argument("--limit", type=int, default=100, choices=range(1, 101), metavar="1..100")
    timeline.add_argument("--dry-run", action="store_true")
    _add_json_flag(timeline)
    timeline.set_defaults(handler=_cmd_timeline_thumbnail_backfill)


COMMANDS: tuple[CommandSpec, ...] = (
    CommandSpec(
        "import",
        help="Import one existing file or directory "
        "(files/folders only; one exact-media result per file).",
        configure=_configure_import,
    ),
    CommandSpec(
        "list",
        help="List every media row in a project (created_at then id).",
        configure=_configure_list,
    ),
    CommandSpec(
        "show",
        help="Show one media read model by exact project-scoped media id.",
        configure=_configure_show,
    ),
    CommandSpec(
        "open",
        help="Open one returned source, text, view, artifact, or definition reference.",
        configure=_configure_open,
    ),
    CommandSpec(
        "verify",
        help="Fingerprint-verify one realm location "
        "(missing/mutated bytes change zero rows).",
        configure=_configure_verify,
    ),
    CommandSpec(
        "relate",
        help="Materialize one media relation edge "
        "(frozen five kinds; constraints delegated to the service).",
        configure=_configure_relate,
    ),
    CommandSpec(
        "crop",
        help="Crop one still image or video into a new media item "
        "(--box X,Y,W,H or --remove-bars; source untouched).",
        configure=_configure_crop,
    ),
    CommandSpec("hold", help="Hold a still image as a silent video.", configure=_configure_hold),
    CommandSpec(
        "trim",
        help="Trim one video to a frame-exact time range into a new media item "
        "(--range START,END; source untouched).",
        configure=_configure_trim,
    ),
    CommandSpec(
        "mask",
        help="Create a binary edit mask and preview its fit over project media.",
        configure=_configure_mask,
    ),
    CommandSpec(
        "thumbnails",
        help="Runtime-owned Generation thumbnail operations.",
        configure=_configure_thumbnails,
    ),
)


def build_parser(
    client: Any,
    *,
    reference_commands: Sequence[CommandSpec] = (),
) -> argparse.ArgumentParser:
    """Build the ``media`` product-family parser stamped with *client*.

    Register the media verbs and the manifest-declared
    nested ``references`` mount (``astrid media references <verb>``)
    embedded from the references product parser. There is no top-level
    references family.
    """
    def _configure_references(subparser: argparse.ArgumentParser) -> None:
        nested = subparser.add_subparsers(dest="reference_command", required=True)
        register_product_commands(
            nested, reference_commands, family="references", client=client
        )

    parser = argparse.ArgumentParser(
        prog="astrid media",
        description=(
            "Media import/list/show/verify/relate (product family); "
            "nested references beneath 'media references'."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    commands: tuple[CommandSpec, ...] = COMMANDS
    if reference_commands:
        commands = (
            *commands,
            CommandSpec(
                "references",
                help="Nested reference create/update/archive/associate/link/"
                "set-primary/list/show (manifest-owned mount).",
                configure=_configure_references,
            ),
        )
    register_product_commands(
        subparsers, commands, family=_FAMILY, client=client
    )
    return parser
