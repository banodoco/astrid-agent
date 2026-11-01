"""Thin public CLI for the existing generation SDK facade."""

from __future__ import annotations

import argparse
import json
import sys
from contextlib import nullcontext
from pathlib import Path
from typing import Any


def _generate_facade() -> Any:
    """Load the SDK facade directly, avoiding astrid package lazy exports."""
    from astrid.sdk.generation import generate

    return generate


def _open_client() -> Any:
    """Acquire the Runtime connection at the explicit public CLI boundary."""
    from astrid.sdk.client import AstridClient

    return AstridClient.open_from_launcher()


class _Once(argparse.Action):
    """Reject repeated convenience flags instead of silently keeping the last."""

    def __call__(self, parser, namespace, value, option_string=None):
        seen = vars(namespace).setdefault('_supplied_once', set())
        if self.dest in seen:
            raise argparse.ArgumentError(self, "specified more than once")
        seen.add(self.dest)
        setattr(namespace, self.dest, value)


class _GenerationParser(argparse.ArgumentParser):
    json_errors = False

    def error(self, message):
        if self.json_errors:
            from astrid.core.generation.preflight import generation_error
            raise generation_error(ValueError(message), details={'constraint': message})
        super().error(message)


def _common_options(command):
    command.add_argument('--input', action='append', default=[], metavar='NAME=VALUE')
    command.add_argument('--engine', action=_Once)
    command.add_argument('--no-wait', action='store_true', help='Return the admitted task; follow it separately.')
    command.add_argument('--consent', action='append', choices=('user_env', 'paid'), default=[])
    command.add_argument('--max-usd', action=_Once, type=float)
    command.add_argument('--deadline', action=_Once, help='Absolute UTC execution deadline.')
    command.add_argument('--recovery-path', action=_Once, help='Save the exact Runtime admission receipt.')


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
    parser = _GenerationParser(
        prog="astrid generate",
        description="Generate media through the selected Comfy target and the Astrid runtime.",
    )
    sub = parser.add_subparsers(dest="modality", required=True)
    command = sub.add_parser('list', help='List generation entries and readiness.')
    command.add_argument('--kind', choices=('image', 'video', 'audio', 'generation', 'fixture'))
    command.add_argument('--json', action='store_true')
    for modality in ("image", "video"):
        command = sub.add_parser(modality, help=f"Generate {'an image' if modality == 'image' else 'a video'}.",
            epilog=_entry_help(contextual) if contextual and contextual["kind"] == modality else None,
            formatter_class=argparse.RawDescriptionHelpFormatter)
        command.add_argument("prompt", nargs="?", help="Describe the media to generate.")
        command.add_argument('--prompt', dest='named_prompt', action=_Once)
        command.add_argument("--model", action=_Once, help="Generation catalog entry ID (legacy selector).")
        command.add_argument("--entry", action=_Once, help="Generation entry ID; use --help to inspect its declared inputs.")
        command.add_argument("--inputs-json", action=_Once, metavar="FILE", help="Read declared entry inputs from a JSON object.")
        command.add_argument("--mode", action=_Once, help="Generation mode; inferred from the supplied inputs by default.")
        command.add_argument("--execution", action=_Once, help="Catalog execution backend, when explicitly selected.")
        command.add_argument(
            "--target", action=_Once, default="auto", metavar="TARGET",
            help="auto (default), managed, a loopback ComfyUI URL, or runpod.",
        )
        command.add_argument("--project", help="Project ID or slug; otherwise use the selected project.")
        command.add_argument("--out", help="Caller-local output file or directory (default: ./outputs/<task-id>/).")
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
        _common_options(command)
    command = sub.add_parser("describe", help="Describe one entry without checking execution readiness.")
    command.add_argument('workflow', nargs='?')
    command.add_argument("--entry")
    command.add_argument("--mode")
    command.add_argument('--target', action=_Once, help='Describe the retained RunPod target.')
    command.add_argument('--engine', action=_Once, default='comfyui')
    command.add_argument("--json", action="store_true")
    command = sub.add_parser("workflow", help="Run an unregistered VibeComfy bundle or UI workflow.")
    command.add_argument("workflow", help="Canonical bundle directory, workflow.py, or UI JSON.")
    command.add_argument("--as-entry", action=_Once, help="Save the workflow as an entry before running it.")
    command.add_argument('--replace', action='store_true')
    command.add_argument('--inputs-json', action=_Once, metavar='FILE')
    command.add_argument("--target", action=_Once, default="auto")
    command.add_argument("--mode", action=_Once, help="Workflow mode, when it cannot be inferred.")
    command.add_argument("--project")
    command.add_argument("--out")
    command.add_argument("--prompt", action=_Once)
    command.add_argument("--negative-prompt", action=_Once)
    command.add_argument("--edit", "--image-ref", dest="image_ref", action=_Once)
    command.add_argument("--width", action=_Once, type=int)
    command.add_argument("--height", action=_Once, type=int)
    command.add_argument("--seed", action=_Once, type=int)
    command.add_argument("--frames", action=_Once, type=int)
    command.add_argument("--fps", action=_Once, type=float)
    command.add_argument("--steps", action=_Once, type=int)
    command.add_argument("--cfg", '--guidance-scale', dest="guidance_scale", action=_Once, type=float)
    command.add_argument("--dry-run", action="store_true")
    command.add_argument("--json", action="store_true")
    _common_options(command)
    command = sub.add_parser('targets', help='Inspect or explicitly save the selected Comfy target.')
    command.add_argument('--target', action=_Once, default='auto')
    command.add_argument('--engine', action=_Once, default='comfyui')
    command.add_argument('--save', action='store_true')
    command.add_argument('--input', action='append', default=[])
    command.add_argument('--json', action='store_true')
    command = sub.add_parser('launch', help='Launch or re-probe only the selected human ComfyUI.')
    command.add_argument('--target', action=_Once, default='auto')
    command.add_argument('--engine', action=_Once, default='comfyui')
    command.add_argument('--consent', action='append', choices=('user_env', 'paid'), default=[])
    command.add_argument('--timeout', action=_Once, type=float, default=10.0,
                         help='Launch and health deadline in seconds (positive, at most 30).')
    command.add_argument('--json', action='store_true')
    command = sub.add_parser('release', help='Admit release through the current host owner.')
    command.add_argument('--engine', required=True)
    command.add_argument('--target', required=True)
    command.add_argument('--no-wait', action='store_true')
    command.add_argument('--json', action='store_true')
    return parser


def _values(parsed):
    from astrid.core.generation.entries import merge_input_values
    common = {name: getattr(parsed, name, None) for name in (
        'prompt', 'negative_prompt', 'image_ref', 'width', 'height', 'seed',
        'frames', 'fps', 'steps', 'guidance_scale')}
    common = {name: value for name, value in common.items() if value is not None}
    named = getattr(parsed, 'named_prompt', None)
    if named is not None:
        if not named.strip():
            raise ValueError('prompt must not be empty')
        if 'prompt' in common:
            raise ValueError('duplicate input values: positional prompt and --prompt')
        common['prompt'] = named
    structured = _json_inputs(parsed.inputs_json) if getattr(parsed, 'inputs_json', None) else {}
    return merge_input_values(common, structured, assignments=parsed.input)


def _controls(parsed):
    controls = {}
    if parsed.no_wait:
        controls['wait'] = False
    for name in ('engine', 'consent', 'max_usd', 'deadline', 'recovery_path'):
        value = getattr(parsed, name, None)
        if value is not None and value != []:
            controls[name] = value
    return controls


def _targets_command(parsed):
    from astrid.core.generation.entries import merge_input_values
    payload = _generate_facade().targets(target=parsed.target, engine=parsed.engine,
        save=parsed.save, inputs=merge_input_values(assignments=parsed.input))
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


def _workflow_command(parsed: argparse.Namespace) -> int:
    """Route authored workflows through the same target admission as entries."""
    from astrid.core.generation import entries

    values = _values(parsed)
    from astrid.sdk.generation import _generation_controls
    _generation_controls(engine=parsed.engine, consent=parsed.consent,
                         max_usd=parsed.max_usd, deadline=parsed.deadline)
    path = Path(parsed.workflow).expanduser()
    if path.is_dir():
        path = path / "workflow.py"
    if parsed.replace and not parsed.as_entry:
        raise ValueError('--replace requires --as-entry')
    if parsed.as_entry:
        from vibecomfy.entries import import_as_entry
        if parsed.dry_run:
            raise ValueError('--as-entry writes an entry; omit it for a read-only dry-run')
        if parsed.replace:
            from inspect import signature
            if 'replace' not in signature(import_as_entry).parameters:
                from astrid.core.generation.preflight import generation_error
                raise generation_error(ValueError('The installed VibeComfy entry API does not support atomic replacement'),
                                       code='missing_dependency', details={'field': 'replace'})
        entries.resolve_workflow_inputs(path, values, mode=parsed.mode)
        entry = import_as_entry(path, parsed.as_entry, **({'replace': True} if parsed.replace else {}))
    def generate(client=None):
        facade = _generate_facade()
        if not parsed.as_entry:
            return facade.workflow(workflow=path, target=parsed.target, mode=parsed.mode,
                inputs=values, dry_run=parsed.dry_run, client=client,
                project=parsed.project, out=parsed.out, **_controls(parsed))
        from astrid.sdk.generation import _load_model_registry
        mode = parsed.mode or (entry['modes'][0] if len(entry['modes']) == 1 else 'workflow')
        selected = entry
        if mode not in entry['modes']:
            selected = dict(entry, modes=[mode], needs=[need for need in entry['needs']
                if need.get('ambiguity') != 'audio mode is unknown; choose tts, music, or sfx'])
        return facade._generate_on_target(registry=_load_model_registry(), model=None,
            mode=mode, modality=selected['kind'], target=parsed.target, execution=None,
            inputs=values, dry_run=False, workflow_entry=selected,
            client=client, project=parsed.project, out=parsed.out, **_controls(parsed))
    if parsed.dry_run:
        result = generate()
    else:
        with _open_client() as client:
            result = generate(client)
            if not parsed.no_wait:
                from .delivery import deliver_generation
                result = deliver_generation(result, client=client, out=parsed.out, project=parsed.project)
    if getattr(result, "ok", True) is False:
        from astrid.core.generation.preflight import generation_error
        raise generation_error(getattr(result, 'error', None) or ValueError('Workflow generation failed.'),
                               task_id=getattr(result, 'kernel_task_id', None))
    payload = result.to_dict() if hasattr(result, "to_dict") else result
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


def _main(argv: list[str]) -> int:
    """Parse one request and call its existing public SDK method exactly once."""
    parser = _parser(_context_description(argv))
    def configure(parser):
        parser.json_errors = '--json' in argv
        for action in parser._actions:
            if isinstance(action, argparse._SubParsersAction):
                for child in action.choices.values():
                    configure(child)
    configure(parser)
    parsed = parser.parse_args(argv)
    if parsed.modality == 'list':
        from astrid.core.generation.listing import main as list_main
        return list_main(argv)
    if parsed.modality == 'targets':
        return _targets_command(parsed)
    if parsed.modality == 'launch':
        payload = _generate_facade().launch(target=parsed.target, engine=parsed.engine,
            consent=parsed.consent, timeout=parsed.timeout)
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 2 if payload['status'] == 'blocked' else 0
    if parsed.modality == 'release':
        with _open_client() as client:
            result = _generate_facade().release(engine=parsed.engine, target=parsed.target,
                                               client=client, wait=not parsed.no_wait)
        if getattr(result, 'ok', True) is False:
            from astrid.core.generation.preflight import generation_error
            raise generation_error(getattr(result, 'error', None) or ValueError('Release admission failed'),
                                   task_id=getattr(result, 'kernel_task_id', None))
        payload = result.to_dict() if hasattr(result, 'to_dict') else result
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    if parsed.modality == "describe":
        if parsed.target is not None:
            if parsed.entry is not None or parsed.workflow is not None:
                parser.error('target describe cannot also select an entry or workflow')
            with _open_client() as client:
                description = _generate_facade().describe(target=parsed.target, engine=parsed.engine, client=client)
            print(json.dumps(description, indent=2, sort_keys=True))
            return 0
        from astrid.core.generation.entries import describe_entry
        try:
            if bool(parsed.entry) == bool(parsed.workflow):
                raise ValueError('describe requires a workflow path or --entry')
            if parsed.workflow:
                from astrid.core.generation.entries import describe_workflow
                description = describe_workflow(Path(parsed.workflow), mode=parsed.mode)
            else:
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
    try:
        values = _values(parsed)
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    description = None
    if parsed.inputs_json:
        if not (parsed.entry or parsed.model):
            parser.error("--inputs-json requires --entry or --model")
    if parsed.entry or parsed.inputs_json:
        from astrid.core.generation.entries import resolve_entry_inputs
        try:
            description, values = resolve_entry_inputs(parsed.entry or parsed.model, values,
                mode=parsed.mode, kind=parsed.modality)
        except (ValueError, KeyError, TypeError) as exc:
            if parser.json_errors:
                from astrid.core.generation.entries import describe_entry
                from astrid.sdk.generation import GenerationInputError
                try:
                    context = describe_entry(parsed.entry or parsed.model, mode=parsed.mode)
                except (ValueError, KeyError):
                    context = None
                raise GenerationInputError(str(exc), entry=context) from exc
            parser.error(str(exc))
    elif 'prompt' not in values:
        parser.error("prompt is required unless declared entry inputs are supplied")
    from astrid.sdk.exceptions import AstridSDKError

    kwargs: dict[str, Any] = {
        "target": parsed.target,
        "dry_run": parsed.dry_run,
        **values,
        **_controls(parsed),
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
        generate = getattr(_generate_facade(), parsed.modality)
        if parsed.dry_run:
            result = generate(**kwargs)
        else:
            with _open_client() as client:
                result = generate(client=client, **kwargs)
                if not parsed.no_wait:
                    from .delivery import deliver_generation
                    result = deliver_generation(result, client=client, out=parsed.out, project=parsed.project)
    except AstridSDKError as exc:
        # Preserve target/model problem codes and recovery guidance at the
        # gateway boundary instead of treating a typed SDK failure as a bug.
        from astrid.core.generation.preflight import generation_error
        selector = ['--entry', parsed.entry or parsed.model] if parsed.entry or parsed.model else []
        raise generation_error(exc, fixes=[{
            'argv': ['astrid', 'generate', 'describe', *selector] if selector else
                    ['astrid', 'generate', 'targets', '--target', parsed.target],
            'scope': 'read_only', 'details': {'guidance': getattr(exc, 'fix', None)}}]) from exc
    if getattr(result, "ok", True) is False:
        from astrid.core.generation.preflight import generation_error
        raise generation_error(getattr(result, 'error', None) or {
            'code': 'generation_failed', 'message': 'Generation did not succeed.'},
            task_id=getattr(result, 'kernel_task_id', None))
    payload = result.to_dict() if hasattr(result, "to_dict") else result
    if parsed.json or parsed.dry_run or not isinstance(payload, dict):
        print(json.dumps(payload, indent=2, sort_keys=True))
    elif parsed.no_wait:
        task_id = payload.get('kernel_task_id') or payload.get('task_id')
        print(f'task: {task_id}' if task_id else json.dumps(payload, sort_keys=True))
    else:
        print(f"{parsed.modality} generation complete")
        for path in payload.get("image_paths", []):
            print(path)
        if payload.get("run_dir"):
            print(f"run: {payload['run_dir']}")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    from astrid.core.generation.preflight import generation_error
    try:
        scope = nullcontext()
        if '--dry-run' in argv:
            from astrid.core.pack.discovery import pack_discovery_scope
            scope = pack_discovery_scope()
        with scope:
            return _main(argv)
    except (RuntimeError, ValueError, OSError, ImportError) as exc:
        error = generation_error(exc, code='missing_dependency' if isinstance(exc, ImportError) else None)
        if '--json' in argv:
            print(json.dumps(error.to_envelope(), indent=2, sort_keys=True))
            return 2
        raise error from exc


if __name__ == "__main__":
    raise SystemExit(main())
