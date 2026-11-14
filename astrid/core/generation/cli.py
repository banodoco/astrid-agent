"""Thin public CLI for the existing generation SDK facade."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from contextlib import nullcontext
from pathlib import Path
from typing import Any

from astrid.core.generation import cli_shared

_logger = logging.getLogger(__name__)

# One table of what each generation kind needs at the CLI boundary: its help
# noun, the SDK keyword that carries its dimensions, whether a video prompt
# alone is refused, and (audio only) its modes.
_KIND_SPEC: dict[str, dict[str, Any]] = {
    'image': {'noun': 'an image', 'dimension_key': 'size'},
    'video': {'noun': 'a video', 'dimension_key': 'resolution', 'prompt_only_guard': True},
    'audio': {'noun': 'audio', 'dimension_key': 'resolution', 'audio_modes': ('music', 'tts', 'sfx')},
}


def _generate_facade() -> Any:
    """Load the SDK facade directly, avoiding astrid package lazy exports."""
    from astrid.sdk.generation import generate

    return generate


def _open_client() -> Any:
    """Acquire the Runtime connection at the explicit public CLI boundary."""
    from astrid.sdk.client import AstridClient

    return AstridClient.open_from_launcher()


def _no_generation_target(target: str, *, repo=None, client=None) -> bool:
    """Whether a local request has no engine and no ComfyUI to run on.

    Read-only: it never starts a recorded engine (it stops at the install
    record) and only probes an unbound discovery address, as the facade does.
    Anything it cannot settle is left to the facade's normal path.
    """
    try:
        from astrid.core.generation import placement as placements
        from astrid.core.generation.engine_local_endpoint import recorded_engine_root
        from astrid.core.generation.preflight import _select_generation_target

        if recorded_engine_root('comfyui') is not None:
            return False
        # Same inputs as the facade's own decision: the request, the repo and the client it would get.
        placement = placements.resolve_placement(placements.request_from_selector(target), repo=repo, client=client)
        return _select_generation_target(placements.comfy_selector(placement), repo=repo) is None
    except Exception:  # noqa: BLE001 - an unavailable target probe must leave admission to its owner.
        _logger.debug("Could not settle whether a local generation target is available", exc_info=True)
        return False


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
        from astrid.sdk.generation import _CLAIM_CONTROLS
        flags = {'--' + name.replace('_', '-') for name in _CLAIM_CONTROLS}
        flags.update({'--capacity-timeout', '--no-wait-for-capacity'})
        if any(flag in message for flag in flags) or ('--consent' in message and "'paid'" in message):
            message += '; use `astrid machines claim` for machine rental choices'
        if self.json_errors:
            from astrid.core.generation.preflight import generation_error
            raise generation_error(ValueError(message), code='input_rejected', details={'constraint': message})
        super().error(message)


def _common_options(command):
    command.add_argument('--input', action='append', default=[], metavar='NAME=VALUE')
    command.add_argument('--engine', action=_Once)
    command.add_argument('--no-wait', action='store_true', help='Return the admitted task; follow it separately.')
    command.add_argument('--consent', action='append', choices=('user_env',), default=[])
    command.add_argument('--recovery-path', action=_Once, help='Save the exact Runtime admission receipt.')


def _validate_datacenter(placement, datacenter, parser):
    """Validate the datacenter for a claim quote."""
    from astrid.core.generation.placement import is_runpod_placement

    if datacenter is None:
        return
    if not is_runpod_placement(placement):
        parser.error("--datacenter requires --placement runpod")
    if not datacenter.strip():
        parser.error("--datacenter must be a nonempty datacenter ID")


def _entry_help(description: dict[str, Any]) -> str:
    lines = [f"Entry: {description['id']} ({description['source']}, {description['kind']})",
             "Modes: " + ", ".join(description["modes"])]
    if description.get("license_notice"):
        lines.append(description["license_notice"])
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
    if not argv or argv[0] not in {"image", "video", "audio"} or not any(
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


def _parser(contextual: dict[str, Any] | None = None) -> _GenerationParser:
    parser = _GenerationParser(
        prog="astrid generate",
        description="Generate media through the selected Comfy target and the Astrid runtime.",
    )
    sub = parser.add_subparsers(dest="modality", required=True)
    command = sub.add_parser('list', help='List generation entries and readiness.')
    command.add_argument('--kind', choices=('image', 'video', 'audio', 'generation', 'fixture'))
    command.add_argument('--json', action='store_true')
    for name in ("image", "video", "audio"):
        modality = name
        spec = _KIND_SPEC[modality]
        command = sub.add_parser(name, help=f"Generate {spec['noun']}.",
            epilog=_entry_help(contextual) if contextual and contextual["kind"] == modality else None,
            formatter_class=argparse.RawDescriptionHelpFormatter)
        command.set_defaults(modality=modality)
        command.add_argument("prompt", nargs="?", help="Describe the media to generate.")
        command.add_argument('--prompt', dest='named_prompt', action=_Once)
        command.add_argument("--model", action=_Once, help="Generation catalog entry ID (legacy selector).")
        command.add_argument("--entry", action=_Once, help="Generation entry ID; use --help to inspect its declared inputs.")
        command.add_argument("--inputs-json", action=_Once, metavar="FILE", help="Read declared entry inputs from a JSON object.")
        command.add_argument("--mode", action=_Once, choices=spec.get('audio_modes'),
                             required='audio_modes' in spec, help=("Audio mode: music, tts, or sfx." if 'audio_modes' in spec
                             else "Generation mode; inferred from the supplied inputs by default."))
        command.add_argument("--execution", action=_Once, help="Catalog execution backend, when explicitly selected.")
        command.add_argument(
            "--placement", action=_Once, default='auto', metavar="PLACEMENT",
            help="auto (default), managed, a loopback ComfyUI URL, or runpod.",
        )
        command.add_argument("--project", help="Project ID or slug; otherwise use the selected project.")
        command.add_argument("--out", help="Caller-local output file or directory (default: ./outputs/<task-id>/).")
        command.add_argument("--dry-run", action="store_true", help="Show the generation plan without running it.")
        command.add_argument("--edit", "--image-ref", dest="image_ref", action=_Once, help="Reference image for an edit or image-to-video request.")
        if modality in {'video', 'audio'}:
            command.add_argument('--video-ref', action=_Once, metavar='FILE', help='Reference video for video generation or Foley sound effects.')
        command.add_argument("--width", action=_Once, type=int)
        command.add_argument("--height", action=_Once, type=int)
        command.add_argument("--steps", action=_Once, type=int)
        command.add_argument("--seed", action=_Once, type=int)
        command.add_argument("--cfg", "--guidance-scale", dest="guidance_scale", action=_Once, type=float)
        command.add_argument("--negative-prompt", action=_Once)
        command.add_argument("--frames", action=_Once, type=int)
        command.add_argument("--fps", action=_Once, type=float)
        command.add_argument("--align", action=_Once, choices=("align", "check", "trust"),
                             help="align: fetch what the target lacks; "
                                  "check: judge models against the target's inventory, fetch nothing (local default); "
                                  "trust: accept the workflow's declared models as present on the target, unverified.")
        command.add_argument("--json", action="store_true", help="Print the SDK result as JSON.")
        _common_options(command)
        if 'audio_modes' in spec:
            command.add_argument('--resume', action='store_true', help='Resume from the admission receipt.')
    command = sub.add_parser("describe", help="Describe an entry or quote a machine; rent with astrid machines claim.")
    command.add_argument('workflow', nargs='?')
    command.add_argument("--entry")
    command.add_argument("--mode")
    command.add_argument('--placement', action=_Once, help='Describe the retained RunPod target.')
    command.add_argument('--engine', action=_Once, default='comfyui')
    command.add_argument("--gpu", action="append", metavar="GPU_TYPE",
                         help="RunPod GPU type to quote for astrid machines claim; repeat in preference order.")
    command.add_argument("--datacenter", action=_Once, metavar="ID",
                         help="RunPod datacenter to quote for astrid machines claim.")
    command.add_argument('--max-hourly-usd', action=_Once, type=float,
                         help='Disclose a waiting-policy hourly ceiling even when the GPU is out of stock.')
    command.add_argument("--json", action="store_true")
    command = sub.add_parser("workflow", help="Run an unregistered VibeComfy bundle or UI workflow.")
    command.add_argument("workflow", help="Canonical bundle directory, workflow.py, or UI JSON.")
    command.add_argument("--as-entry", action=_Once, help="Save the workflow as an entry before running it.")
    command.add_argument('--replace', action='store_true')
    command.add_argument('--no-run', action='store_true',
                         help='With --as-entry: save the entry and queue nothing on any target.')
    command.add_argument('--inputs-json', action=_Once, metavar='FILE')
    command.add_argument("--placement", action=_Once, default="auto")
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
    command = sub.add_parser('align', help='Inspect locked managed assets; apply with explicit user_env consent.')
    selection = command.add_mutually_exclusive_group()
    selection.add_argument('--entry', action=_Once, metavar='ID')
    selection.add_argument('--all', action='store_true')
    command.add_argument('--apply', action='store_true')
    command.add_argument('--consent', action='append', choices=('user_env',), default=[])
    command.add_argument('--json', action='store_true')
    command = sub.add_parser('targets', help='Inspect or explicitly save the selected Comfy target.')
    command.add_argument('--placement', action=_Once, default='auto')
    command.add_argument('--engine', action=_Once, default='comfyui')
    command.add_argument('--save', action='store_true')
    command.add_argument('--input', action='append', default=[])
    command.add_argument('--json', action='store_true')
    command = sub.add_parser('launch', help='Launch or re-probe only the selected human ComfyUI.')
    command.add_argument('--placement', action=_Once, default='auto')
    command.add_argument('--engine', action=_Once, default='comfyui')
    command.add_argument('--consent', action='append', choices=('user_env',), default=[])
    command.add_argument('--timeout', action=_Once, type=float, default=10.0,
                         help='Launch and health deadline in seconds (positive, at most 30).')
    command.add_argument('--json', action='store_true')
    command = sub.add_parser('defaults', help='List, pin (set) or unpin (unset) the default entry of each kind/mode slot.')
    command.add_argument('--json', action='store_true')
    actions = command.add_subparsers(dest='defaults_action')
    pin = actions.add_parser('set', help='Pin an entry as the default for a slot.')
    pin.add_argument('kind', choices=('image', 'video', 'audio'))
    pin.add_argument('mode')
    pin.add_argument('entry')
    pin.add_argument('--pack', help='Pack id, when several packs offer the entry.')
    pin.add_argument('--json', action='store_true')
    unpin = actions.add_parser('unset', help='Remove the pin for a slot.')
    unpin.add_argument('kind', choices=('image', 'video', 'audio'))
    unpin.add_argument('mode')
    unpin.add_argument('--json', action='store_true')
    command = sub.add_parser('settings', help='Edit the generation settings document as a file.')
    actions = command.add_subparsers(dest='settings_action', required=True)
    out = actions.add_parser('checkout', help='Write the settings document to a new file.')
    out.add_argument('--file', required=True)
    back = actions.add_parser('checkin', help='Save an edited checkout; a stale one is refused.')
    back.add_argument('file')
    command = sub.add_parser('release', help='Admit release through the current host owner.')
    command.add_argument('--engine', required=True)
    command.add_argument('--placement', required=True)
    command.add_argument('--no-wait', action='store_true')
    command.add_argument('--json', action='store_true')
    command = sub.add_parser('failure', help='Show the local failure receipt of a failed generation (newest by default).')
    command.add_argument('task_id', nargs='?', help='Task id of the failed generation; omit for the newest receipt.')
    command.add_argument('--json', action='store_true')
    command.add_argument('--logs', action='store_true', help='Print the collected failure logs.')
    command.add_argument('--name', help='Print only the named failure log (requires --logs).')
    return parser


def _values(parsed):
    from astrid.core.generation.entries import merge_input_values
    common = {name: getattr(parsed, name, None) for name in (
        'prompt', 'negative_prompt', 'image_ref', 'video_ref', 'width', 'height', 'seed',
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
    if getattr(parsed, 'resume', False):
        controls['resume'] = True
    for name in ('engine', 'consent', 'recovery_path', 'align'):
        value = getattr(parsed, name, None)
        if value is not None and value != []:
            controls[name] = value
    return controls


def _targets_command(parsed):
    from astrid.core.generation.entries import merge_input_values
    payload = _generate_facade().targets(placement=parsed.placement, engine=parsed.engine,
        save=parsed.save, inputs=merge_input_values(assignments=parsed.input))
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


def _defaults_text(payload):
    settings = payload['settings']
    lines = [f"Defaults ({settings['label']}); edit with: {payload['edit']}"]
    for row in payload['slots']:
        entry = f"{row['pack']}/{row['entry']}" if row['entry'] else '(none)'
        lines.append(f"{row['slot']}: {entry}  [{row['source']}: {row['origin']}]")
        for other in row['others']:
            lines.append(f"    also: {other['pack']}/{other['entry']} (priority {other['priority']})")
    return '\n'.join(lines)


def _defaults_command(parsed):
    generate = _generate_facade()
    with _open_client() as client:
        if parsed.defaults_action == 'set':
            payload = generate.set_default(parsed.kind, parsed.mode, parsed.entry, pack=parsed.pack, client=client)
        elif parsed.defaults_action == 'unset':
            payload = generate.unset_default(parsed.kind, parsed.mode, client=client)
        else:
            payload = generate.defaults(client=client)
    print(json.dumps(payload, indent=2, sort_keys=True) if getattr(parsed, 'json', False) else _defaults_text(payload))
    return 0


def _settings_command(parsed):
    from astrid.core.generation import settings
    with _open_client() as client:
        if parsed.settings_action == 'checkout':
            result = settings.checkout(parsed.file, client)
        else:
            result = settings.checkin(parsed.file, client)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def _workflow_command(parsed: argparse.Namespace) -> int:
    """Route authored workflows through the same target admission as entries."""
    from astrid.core.generation import entries

    try:
        values = _values(parsed)
    except (ValueError, OSError) as exc:
        from astrid.core.generation.preflight import generation_error
        raise generation_error(exc, code='input_rejected', details={'field': 'inputs'}) from exc
    from astrid.sdk.generation import _generation_controls
    _generation_controls(engine=parsed.engine, consent=parsed.consent)
    path = Path(parsed.workflow).expanduser()
    if path.is_dir():
        path = path / "workflow.py"
    if parsed.replace and not parsed.as_entry:
        from astrid.core.generation.preflight import generation_error
        raise generation_error(ValueError('--replace requires --as-entry'),
                               code='input_rejected', details={'field': 'replace'})
    if parsed.no_run and not parsed.as_entry:
        raise ValueError('--no-run requires --as-entry')
    if parsed.as_entry and parsed.dry_run:
        # Preview only: validate the inputs the entry would declare, write nothing, run nothing.
        description, _ = entries.resolve_workflow_inputs(path, values, mode=parsed.mode)
        preview = {
            "dry_run": True,
            "written": False,
            "would_save_entry": parsed.as_entry,
            "replaces_existing": bool(parsed.replace),
            "kind": description.get("kind"),
            "modes": description.get("modes"),
            "inputs": description.get("inputs"),
        }
        print(json.dumps(preview, indent=2, sort_keys=True, default=str))
        return 0
    if parsed.as_entry:
        from vibecomfy.entries import import_as_entry
        if parsed.replace:
            from inspect import signature
            if 'replace' not in signature(import_as_entry).parameters:
                from astrid.core.generation.preflight import generation_error
                raise generation_error(ValueError('The installed VibeComfy entry API does not support atomic replacement'),
                                       code='missing_dependency', details={'field': 'replace'})
        entries.resolve_workflow_inputs(path, values, mode=parsed.mode)
        entry = import_as_entry(path, parsed.as_entry, **({'replace': True} if parsed.replace else {}))
        if parsed.no_run:
            print(json.dumps({'saved': True, 'ran': False, 'entry': parsed.as_entry,
                              'replaced': bool(parsed.replace)}, indent=2, sort_keys=True))
            return 0
    def generate(client=None):
        facade = _generate_facade()
        if not parsed.as_entry:
            return facade.workflow(workflow=path, placement=parsed.placement, mode=parsed.mode,
                inputs=values, dry_run=parsed.dry_run, client=client,
                project=parsed.project, out=parsed.out, **_controls(parsed))
        from astrid.sdk.generation import _load_model_registry
        mode = parsed.mode or (entry['modes'][0] if len(entry['modes']) == 1 else 'workflow')
        selected = entry
        if mode not in entry['modes']:
            selected = dict(entry, modes=[mode], needs=[need for need in entry['needs']
                if need.get('ambiguity') != 'audio mode is unknown; choose tts, music, or sfx'])
        return facade._generate_on_target(registry=_load_model_registry(), model=None,
            mode=mode, modality=selected['kind'], placement=parsed.placement, execution=None,
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


def _job_command(argv: list[str]) -> int:
    """``generate <entry> --placement local --job FILE --out DIR --json``: the pod-side run."""
    parser = _GenerationParser(prog='astrid generate', description='Run one machine job file locally.')
    parser.json_errors = '--json' in argv
    parser.add_argument('entry')
    parser.add_argument('--placement', required=True, choices=('local',))
    parser.add_argument('--job', required=True, metavar='FILE')
    parser.add_argument('--out', required=True)
    parser.add_argument('--mode')
    parser.add_argument('--align', choices=('align', 'check', 'trust'),
                        help="Override the job's align mode.")
    parser.add_argument('--json', action='store_true')
    parsed = parser.parse_args(argv)
    from astrid.core.contracts.errors import AstridError
    from astrid.core.generation.local_job import JobInputError, run_job
    from astrid.core.generation.progress import write_progress
    try:
        result = run_job(parsed.entry, parsed.job, parsed.out, mode=parsed.mode, align=parsed.align)
    except AstridError:
        # Preserve the typed failure for main's structured JSON renderer.
        raise
    except JobInputError as exc:
        parser.error(str(exc))
    write_progress('done')
    print(json.dumps(result, indent=2, sort_keys=True, default=str))
    return 0


def _main(argv: list[str]) -> int:
    """Parse one request and call its existing public SDK method exactly once."""
    if '--job' in argv:
        return _job_command(argv)
    parser = _parser(_context_description(argv))
    def configure(parser):
        parser.json_errors = '--json' in argv
        for action in parser._actions:
            if isinstance(action, argparse._SubParsersAction):
                for child in action.choices.values():
                    configure(child)
    configure(parser)
    parsed = parser.parse_args(argv)
    from astrid.core.generation.placement import is_runpod_placement
    if parsed.modality == 'list':
        from astrid.core.generation.listing import main as list_main
        return list_main(argv)
    if parsed.modality == 'align':
        from astrid.core.generation.asset_alignment import main as align_main
        return align_main(parsed)
    if parsed.modality == 'targets':
        return _targets_command(parsed)
    if parsed.modality == 'defaults':
        return _defaults_command(parsed)
    if parsed.modality == 'settings':
        return _settings_command(parsed)
    if parsed.modality == 'launch':
        payload = _generate_facade().launch(placement=parsed.placement, engine=parsed.engine,
            consent=parsed.consent, timeout=parsed.timeout)
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 2 if payload['status'] == 'blocked' else 0
    if parsed.modality == 'failure':
        return _failure_command(parsed)
    if parsed.modality == 'release':
        with _open_client() as client:
            result = _generate_facade().release(engine=parsed.engine, placement=parsed.placement,
                                               client=client, wait=not parsed.no_wait)
        if getattr(result, 'ok', True) is False:
            from astrid.core.generation.preflight import generation_error
            raise generation_error(getattr(result, 'error', None) or ValueError('Release admission failed'),
                                   task_id=getattr(result, 'kernel_task_id', None))
        payload = result.to_dict() if hasattr(result, 'to_dict') else result
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    if parsed.modality == "describe":
        if parsed.gpu and parsed.placement is None:
            parser.error("--gpu requires target describe with --placement runpod")
        _validate_datacenter(parsed.placement, parsed.datacenter, parser)
        if parsed.datacenter and parsed.placement is None:
            parser.error("--datacenter requires target describe with --placement runpod")
        if parsed.placement is not None:
            if parsed.entry is not None or parsed.workflow is not None:
                parser.error('target describe cannot also select an entry or workflow')
            placement = cli_shared._gpu_placement(parsed.placement, parsed.gpu, parser)
            with _open_client() as client:
                description = _generate_facade().describe(placement=placement, engine=parsed.engine, client=client,
                    **({"datacenter": parsed.datacenter} if parsed.datacenter is not None else {}),
                    **({'max_hourly_usd': parsed.max_hourly_usd} if parsed.max_hourly_usd is not None else {}))
            print(json.dumps(description, indent=2, sort_keys=True))
            return 0
        from astrid.core.generation.entries import describe_entry
        try:
            if bool(parsed.entry) == bool(parsed.workflow):
                raise ValueError('describe requires a workflow path or catalog entry ID')
            if parsed.workflow:
                path = Path(parsed.workflow).expanduser()
                if path.exists():
                    from astrid.core.generation.entries import describe_workflow
                    description = describe_workflow(path, mode=parsed.mode)
                else:
                    from astrid.core.generation.listing import catalog_rows
                    if not any(row['id'] == parsed.workflow for row in catalog_rows()):
                        raise ValueError(
                            f"{parsed.workflow!r} is neither an existing workflow path nor a known catalog entry ID"
                        )
                    description = describe_entry(parsed.workflow, mode=parsed.mode)
            else:
                description = describe_entry(parsed.entry, mode=parsed.mode)
        except (ValueError, KeyError) as exc:
            parser.error(str(exc))
        print(json.dumps(description, indent=2, sort_keys=True) if parsed.json else _entry_help(description))
        return 0
    if (is_runpod_placement(parsed.placement) and not parsed.dry_run
            and not getattr(parsed, 'no_run', False)):
        from astrid.sdk.generation import _require_claimed_machine
        _require_claimed_machine(parsed.placement)
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
    elif 'prompt' not in values and not ('audio_modes' in _KIND_SPEC[parsed.modality] and values):
        parser.error("prompt is required unless declared entry inputs are supplied")
    from astrid.sdk.exceptions import AstridSDKError

    kwargs: dict[str, Any] = {
        "placement": parsed.placement,
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
        kwargs[_KIND_SPEC[parsed.modality]['dimension_key']] = f"{parsed.width}x{parsed.height}"
    try:
        generate = getattr(_generate_facade(), parsed.modality)
        from astrid.sdk.generation import _prompt_only_video
        if parsed.dry_run or (_KIND_SPEC[parsed.modality].get('prompt_only_guard') and _prompt_only_video(
                model=parsed.model, entry=parsed.entry, inputs=values)):
            # Let the facade refuse before opening a Runtime connection.
            result = generate(**kwargs)
        elif description is not None and description["source"] == "action" and not is_runpod_placement(parsed.placement):
            # Action-backed entries are validated (and refused, fail-closed) by the SDK
            # before any Runtime is needed; never open one ahead of that.
            result = generate(**kwargs)
        else:
            try:
                client_scope = _open_client()
            except AstridSDKError:
                if not is_runpod_placement(parsed.placement) and _no_generation_target(parsed.placement, repo=kwargs.get('project_root'), client=None):
                    # No workspace and no engine or ComfyUI: the target guidance
                    # (the refusal a dry run gives) is the actionable answer.
                    generate(**{**kwargs, "dry_run": True})
                raise
            with client_scope as client:
                result = generate(client=client, **kwargs)
                if not parsed.no_wait:
                    from .delivery import deliver_generation
                    result = deliver_generation(result, client=client, out=parsed.out, project=parsed.project)
    except AstridSDKError as sdk_exc:
        # Preserve target/model problem codes and recovery guidance at the
        # gateway boundary instead of treating a typed SDK failure as a bug.
        from astrid.core.generation.preflight import generation_error
        error: Exception = sdk_exc
        selector = ['--entry', parsed.entry or parsed.model] if parsed.entry or parsed.model else []
        raise generation_error(error, fixes=[{
            'argv': ['astrid', 'generate', 'describe', *selector] if selector else
                    ['astrid', 'generate', 'targets', '--placement', parsed.placement],
            'scope': 'read_only', 'details': {'guidance': getattr(error, 'fix', None)}}]) from error
    if getattr(result, "ok", True) is False:
        from astrid.core.generation.preflight import generation_error
        error = getattr(result, 'error', None) or {
            'code': 'generation_failed', 'message': 'Generation did not succeed.'}
        raise generation_error(error, task_id=getattr(result, 'kernel_task_id', None))
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


def _failure_command(parsed) -> int:
    """Print one local failure receipt; it reads only the receipt file, never a machine."""
    from astrid.packs.runpod import failure_receipt
    path = failure_receipt.find_receipt(parsed.task_id)
    if path is None:
        what = f"task {parsed.task_id}" if parsed.task_id else "any failed generation"
        print(f"no failure receipt for {what}", file=sys.stderr)
        return 1
    receipt = json.loads(path.read_text(encoding="utf-8"))
    selected_name = getattr(parsed, 'name', None)
    if not selected_name:
        print(json.dumps(receipt, indent=2, sort_keys=True) if parsed.json else failure_receipt.render(receipt))
    if parsed.logs or getattr(parsed, 'name', None):
        logs = sorted(receipt.get('logs', []), key=failure_receipt.log_order)
        name = selected_name
        if name:
            logs = [log for log in logs if log.get('name') == name]
            if not logs or logs[0].get('missing') or not logs[0].get('path'):
                print(f"failure log '{name}' is absent from this receipt", file=sys.stderr)
                return 1
        for log in logs:
            if log.get('missing') or not log.get('path'):
                if name:
                    print(f"failure log '{name}' is unavailable", file=sys.stderr)
                    return 1
                continue
            print(f"==> {log['name']} <==")
            try:
                print(Path(log['path']).read_text(encoding='utf-8', errors='replace'), end='')
            except OSError as exc:
                print(f"failure log '{log['name']}' unavailable ({type(exc).__name__})", file=sys.stderr)
                if name:
                    return 1
    return 0


def _kept_failure(before: set[tuple[str | None, int | None]], *, kind: str = 'runpod') -> None:
    """Read only the bounded owner result after a failed RunPod command."""
    try:
        from astrid.core.generation.placement import _runpod_leases
        for row in _runpod_leases(kind):
            last = row.get('last_job') or {}
            identity = (row.get('lease'), last.get('generation'))
            if (identity in before or not row.get('live') or row.get('state') != 'idle'
                    or last.get('outcome') != 'failed'):
                continue
            facts = last.get('failure') or {}
            summary = ', '.join(f'{name}={facts[name]}' for name in
                                ('exception', 'stage', 'code', 'category', 'node_id', 'node_class', 'field', 'constraint')
                                if name in facts)
            details = [f"Reason: {facts['issue_message']}"] if isinstance(facts.get('issue_message'), str) else []
            if isinstance(facts.get('logs_dir'), str):
                details.append(f"logs: {facts['logs_dir']}")
            print(f"Failure: {summary or 'unknown'}. machine {row['id']} kept until "
                  f"{cli_shared._when(row['release_at'])} UTC (cost so far ${row['cost_usd']:.2f}); "
                  f"reuse it or run `astrid machines release {row['id']}`"
                  + ("; " + "; ".join(details) if details else ""), file=sys.stderr)
    except Exception:  # noqa: BLE001 - best-effort fallback; diagnostic at debug, never a user-facing traceback
        _logger.debug("Could not read the retained failure context", exc_info=True)


def subcommand_names() -> tuple[str, ...]:
    """Every subcommand this CLI defines, read from its own parser."""
    for action in _parser()._actions:
        if isinstance(action, argparse._SubParsersAction):
            return tuple(action.choices)
    return ()


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    from astrid.core.generation.preflight import generation_error
    from astrid.sdk.generation import _NO_MACHINE
    before: set[tuple[str | None, int | None]] = set()
    from astrid.core.generation.placement import is_runpod_placement
    selected = argv[argv.index('--placement') + 1:][:1] if '--placement' in argv else []
    kind = selected[0] if selected else ''
    runpod = is_runpod_placement(kind)
    if runpod:
        try:
            from astrid.core.generation.placement import _runpod_leases
            before = {(row.get('lease'), (row.get('last_job') or {}).get('generation'))
                      for row in _runpod_leases(kind)}
        except Exception:  # noqa: BLE001 - diagnostic snapshots cannot block generation admission.
            _logger.debug("Could not snapshot leases before the command", exc_info=True)
    try:
        scope = nullcontext()
        if '--dry-run' in argv:
            from astrid.core.pack.discovery import pack_discovery_scope
            scope = pack_discovery_scope()
        with scope:
            return _main(argv)
    except (RuntimeError, ValueError, OSError, ImportError) as exc:
        if str(exc) == _NO_MACHINE:
            print(str(exc), file=sys.stderr)
            return 2
        if runpod:
            _kept_failure(before, kind=kind)
        error = generation_error(exc, code='missing_dependency' if isinstance(exc, ImportError) else None)
        if '--json' in argv:
            from astrid.core.contracts.errors import AstridError, redact_credential_data
            from astrid.core.generation.local_job import JobFileError
            from astrid.sdk.exceptions import AstridSDKError
            if not isinstance(exc, (AstridError, AstridSDKError, JobFileError, ImportError)):
                import traceback
                diagnostic = ''.join(traceback.format_exception(exc, chain=True))
                print(redact_credential_data(diagnostic), file=sys.stderr, end='')
            print(json.dumps(error.to_envelope(), indent=2, sort_keys=True))
            return 2
        raise error from exc
    except BaseException:
        if runpod:
            _kept_failure(before, kind=kind)
        raise


if __name__ == "__main__":
    raise SystemExit(main())
