"""Thin public CLI for the existing generation SDK facade."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


def _open_client() -> Any:
    """Acquire the Runtime connection at the explicit public CLI boundary."""
    from astrid.sdk.client import AstridClient

    return AstridClient.open_from_launcher()


class _Once(argparse.Action):
    """Reject repeated convenience flags instead of silently keeping the last."""

    def __call__(self, parser, namespace, value, option_string=None):
        if getattr(namespace, self.dest, None) is not None:
            raise argparse.ArgumentError(self, "specified more than once")
        setattr(namespace, self.dest, value)


def _entry_help(description: dict[str, Any]) -> str:
    lines = [f"Entry: {description['id']} ({description['source']}, {description['kind']})",
             "Modes: " + ", ".join(description["modes"])]
    selected = description.get("mode")
    schemas = {selected: description} if selected else description.get("mode_descriptions", {})
    for mode, schema in schemas.items():
        lines.append(f"Inputs for mode {mode}:")
        for field in schema.get("inputs", []):
            status = "required" if field.get("required") else "optional"
            if "default" in field:
                status += ", default=" + json.dumps(field["default"], sort_keys=True)
            lines.append(f"  {field['name']} ({field['type']}; {status})")
            if field.get("description"):
                lines.append("    " + field["description"])
            if field.get("aliases"):
                lines.append("    Aliases: " + ", ".join(field["aliases"]))
            for key in ("choices", "min", "max", "range"):
                if key in field:
                    lines.append(f"    {key}: " + json.dumps(field[key], sort_keys=True))
    return "\n".join(lines)


def _context_description(argv: list[str]) -> dict[str, Any] | None:
    if not argv or argv[0] not in {"image", "video"} or not any(
            item in {"-h", "--help"} for item in argv):
        return None
    selector = argparse.ArgumentParser(add_help=False)
    selector.add_argument("--entry")
    selector.add_argument("--model")
    selector.add_argument("--mode")
    parsed, _ = selector.parse_known_args(argv[1:])
    entry_id = parsed.entry or parsed.model
    if entry_id is None:
        return None
    if parsed.entry and parsed.model and parsed.entry != parsed.model:
        selector.error("--model and --entry select different generation entries")
    from astrid.core.generation.entries import describe_entry
    try:
        description = describe_entry(entry_id, mode=parsed.mode)
    except (ValueError, KeyError) as exc:
        selector.error(str(exc))
    if description["kind"] != argv[0]:
        selector.error(f"entry {entry_id!r} does not support {argv[0]}")
    return description


def _json_inputs(path: str) -> dict[str, Any]:
    def object_pairs(pairs):
        values = {}
        for name, value in pairs:
            if name in values:
                raise ValueError(f"duplicate JSON input {name!r}")
            values[name] = value
        return values

    def invalid_constant(value):
        raise ValueError(f"invalid JSON constant {value}")

    values = json.loads(Path(path).expanduser().read_text(), object_pairs_hook=object_pairs,
                        parse_constant=invalid_constant)
    if not isinstance(values, dict):
        raise ValueError("--inputs-json must contain a JSON object")
    return values


def _parser(contextual: dict[str, Any] | None = None) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="astrid generate",
        description="Generate media through the selected Comfy target and the Astrid runtime.",
    )
    sub = parser.add_subparsers(dest="modality", required=True)
    for modality in ("image", "video"):
        command = sub.add_parser(modality, help=f"Generate {'an image' if modality == 'image' else 'a video'}.",
            epilog=_entry_help(contextual) if contextual and contextual["kind"] == modality else None,
            formatter_class=argparse.RawDescriptionHelpFormatter)
        command.add_argument("prompt", nargs="?", help="Describe the media to generate.")
        command.add_argument("--model", action=_Once, help="Generation catalog entry ID (legacy selector).")
        command.add_argument("--entry", action=_Once, help="Generation entry ID; use --help to inspect its declared inputs.")
        command.add_argument("--inputs-json", action=_Once, metavar="FILE", help="Read declared entry inputs from a JSON object.")
        command.add_argument("--mode", action=_Once, help="Generation mode; inferred from the supplied inputs by default.")
        command.add_argument("--execution", help="Catalog execution backend, when explicitly selected.")
        command.add_argument(
            "--target", default="auto", metavar="TARGET",
            help="auto (default), managed, a loopback ComfyUI URL, or runpod.",
        )
        command.add_argument("--project", help="Project ID or slug; otherwise use the selected project.")
        command.add_argument("--out", help="Output destination passed to the runtime.")
        command.add_argument("--dry-run", action="store_true", help="Show the generation plan without running it.")
        command.add_argument("--edit", "--image-ref", dest="image_ref", action=_Once, help="Reference image for an edit or image-to-video request.")
        command.add_argument("--width", action=_Once, type=int)
        command.add_argument("--height", action=_Once, type=int)
        command.add_argument("--steps", action=_Once, type=int)
        command.add_argument("--seed", action=_Once, type=int)
        command.add_argument("--cfg", "--guidance-scale", dest="guidance_scale", action=_Once, type=float)
        command.add_argument("--negative-prompt", action=_Once)
        command.add_argument("--frames", action=_Once, type=int)
        command.add_argument("--fps", action=_Once, type=float)
        command.add_argument("--json", action="store_true", help="Print the SDK result as JSON.")
    command = sub.add_parser("describe", help="Describe one entry without checking execution readiness.")
    command.add_argument("--entry", required=True)
    command.add_argument("--mode")
    command.add_argument("--json", action="store_true")
    command = sub.add_parser("workflow", help="Run an unregistered VibeComfy bundle or UI workflow.")
    command.add_argument("workflow", help="Canonical bundle directory, workflow.py, or UI JSON.")
    command.add_argument("--as-entry", help="Save the workflow as an entry before running it.")
    command.add_argument("--target", default="auto")
    command.add_argument("--mode", help="Workflow mode, when it cannot be inferred.")
    command.add_argument("--project")
    command.add_argument("--out")
    command.add_argument("--prompt")
    command.add_argument("--negative-prompt")
    command.add_argument("--edit", "--image-ref", dest="image_ref")
    command.add_argument("--width", type=int)
    command.add_argument("--height", type=int)
    command.add_argument("--seed", type=int)
    command.add_argument("--frames", type=int)
    command.add_argument("--fps", type=float)
    command.add_argument("--steps", type=int)
    command.add_argument("--cfg", dest="guidance_scale", type=float)
    command.add_argument("--dry-run", action="store_true")
    command.add_argument("--json", action="store_true")
    return parser


def _workflow_command(parsed: argparse.Namespace) -> int:
    """Route authored workflows through the same target admission as entries."""
    from pathlib import Path
    from tempfile import TemporaryDirectory
    import astrid
    from astrid.core.generation import entries
    from astrid.core.generation.preflight import select_generation_target
    from astrid.packs.vibecomfy.production_engine import load_workflow_path
    from vibecomfy.entries import draft_entry, import_as_entry

    path = Path(parsed.workflow).expanduser()
    if path.is_dir():
        path = path / "workflow.py"
    if parsed.as_entry:
        entry = import_as_entry(path, parsed.as_entry)
    else:
        target = select_generation_target(parsed.target)
        with TemporaryDirectory(prefix="astrid-workflow-inspect-") as scratch:
            loaded = load_workflow_path(path, scratch, comfy_url=target.target.url if target else "")
            entry = draft_entry(loaded.resolved.workflow, "workflow", bundle=str(path.resolve()))
    mode = parsed.mode or (entry["modes"][0] if len(entry["modes"]) == 1 else "workflow")
    # Arbitrary workflows need only their output modality for publication.
    # The temporary mode is not persisted into an ambiguous entry manifest.
    if mode not in entry["modes"]:
        entry = dict(entry, modes=[mode], needs=[need for need in entry["needs"]
            if need.get("ambiguity") != "audio mode is unknown; choose tts, music, or sfx"])
    values = {name: getattr(parsed, name, None) for name in
              ("prompt", "negative_prompt", "image_ref", "width", "height", "seed", "frames", "fps", "steps", "guidance_scale")}
    values = {key: value for key, value in values.items() if value is not None}
    def generate(client=None):
        from astrid.sdk.generation import _load_model_registry
        return astrid.generate._generate_on_target(registry=_load_model_registry(), model=None,
            mode=mode, modality=entry["kind"], target=parsed.target, execution=None,
            inputs=values, dry_run=parsed.dry_run, workflow_entry=entry,
            client=client, project=parsed.project, out=parsed.out)
    if parsed.dry_run:
        result = generate()
    else:
        with _open_client() as client:
            result = generate(client)
    from astrid.core.contracts.errors import error_from_result, AstridError
    if getattr(result, "ok", True) is False:
        raise error_from_result(result) or AstridError("Workflow generation failed.")
    payload = result.to_dict() if hasattr(result, "to_dict") else result
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


def main(argv: list[str] | None = None) -> int:
    """Parse one request and call its existing public SDK method exactly once."""
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = _parser(_context_description(argv))
    parsed = parser.parse_args(argv)
    if parsed.modality == "describe":
        from astrid.core.generation.entries import describe_entry
        try:
            description = describe_entry(parsed.entry, mode=parsed.mode)
        except (ValueError, KeyError) as exc:
            parser.error(str(exc))
        print(json.dumps(description, indent=2, sort_keys=True) if parsed.json else _entry_help(description))
        return 0
    if parsed.modality == "workflow":
        return _workflow_command(parsed)
    if parsed.model and parsed.entry and parsed.model != parsed.entry:
        parser.error("--model and --entry select different generation entries")
    if parsed.prompt is not None and not parsed.prompt.strip():
        parser.error("prompt must not be empty")
    if (parsed.width is None) != (parsed.height is None):
        parser.error("--width and --height must be supplied together")
    if parsed.width is not None and (parsed.width <= 0 or parsed.height <= 0):
        parser.error("--width and --height must be positive")
    values = {name: getattr(parsed, name) for name in (
        "prompt", "image_ref", "width", "height", "steps", "seed", "guidance_scale",
        "negative_prompt", "frames", "fps") if getattr(parsed, name) is not None}
    description = None
    if parsed.inputs_json:
        if not (parsed.entry or parsed.model):
            parser.error("--inputs-json requires --entry or --model")
        try:
            structured = _json_inputs(parsed.inputs_json)
        except (OSError, ValueError) as exc:
            parser.error(str(exc))
        duplicate = sorted(set(values) & set(structured))
        if duplicate:
            parser.error("duplicate input values: " + ", ".join(duplicate))
        values.update(structured)
    if parsed.entry or parsed.inputs_json:
        from astrid.core.generation.entries import resolve_entry_inputs
        try:
            description, values = resolve_entry_inputs(parsed.entry or parsed.model, values,
                mode=parsed.mode, kind=parsed.modality)
        except (ValueError, KeyError, TypeError) as exc:
            parser.error(str(exc))
    elif parsed.prompt is None:
        parser.error("prompt is required unless declared entry inputs are supplied")
    import astrid
    from astrid.core.contracts.errors import (
        AstridError,
        coerce_astrid_error,
        error_from_result,
    )
    from astrid.sdk.exceptions import AstridSDKError

    kwargs: dict[str, Any] = {
        "target": parsed.target,
        "dry_run": parsed.dry_run,
        **values,
    }
    if parsed.entry:
        kwargs["entry"] = parsed.entry
    else:
        kwargs["model"] = parsed.model
    for name in ("mode", "execution", "project", "out"):
        value = getattr(parsed, name)
        if value is not None:
            kwargs[name] = value
    if description is not None:
        kwargs["mode"] = description["mode"]
    elif parsed.width is not None:
        kwargs.pop("width")
        kwargs.pop("height")
        kwargs["size" if parsed.modality == "image" else "resolution"] = f"{parsed.width}x{parsed.height}"
    try:
        generate = getattr(astrid.generate, parsed.modality)
        if parsed.dry_run or (description is not None and description["source"] == "action"):
            result = generate(**kwargs)
        else:
            with _open_client() as client:
                result = generate(client=client, **kwargs)
    except AstridSDKError as exc:
        # Preserve target/model problem codes and recovery guidance at the
        # gateway boundary instead of treating a typed SDK failure as a bug.
        raise coerce_astrid_error(exc) from exc
    if getattr(result, "ok", True) is False:
        raise error_from_result(result) or AstridError(
            "Generation did not succeed.",
            code="generation_failed",
            recovery_command="astrid tasks list",
        )
    payload = result.to_dict() if hasattr(result, "to_dict") else result
    if parsed.json or parsed.dry_run or not isinstance(payload, dict):
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"{parsed.modality} generation complete")
        for path in payload.get("image_paths", []):
            print(path)
        if payload.get("run_dir"):
            print(f"run: {payload['run_dir']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
