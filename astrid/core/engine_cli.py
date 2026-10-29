"""Canonical engine planning and execution CLI.

The backend seam deliberately has no production implementation until the A2
local context and P2-SELF orchestration have been reviewed and integrated.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import stat
import sys
import time
from dataclasses import fields, is_dataclass
from collections.abc import Mapping

from astrid.core.generation.engine_contracts import (
    EngineError, RecipeValidationError, UnsafePathError, UnsupportedPlacement, UnsupportedPlatform,
)
from astrid.core.generation.engine_recipe import load_recipe
from astrid.core.generation.engine_runner import engine_status, install_engine, plan_install


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        # Do not echo untrusted command-line values (which might contain secrets).
        self.exit(2, 'astrid engines: invalid or missing arguments; use --help\n')


def _parser():
    parser = _Parser(prog='astrid engines', description='Plan or inspect a canonical engine installation.')
    commands = parser.add_subparsers(dest='command', required=True, parser_class=_Parser)
    for name in ('install', 'status'):
        command = commands.add_parser(name)
        command.add_argument('engine', help='Engine ID, for example vibecomfy-h3')
        command.add_argument('--platform', default='linux-x86_64-cu130',
                             help='Target platform (default: linux-x86_64-cu130)')
        command.add_argument('--placement', choices=('local', 'runpod'), default='local',
                             help='Target placement (default: local)')
        command.add_argument('--root', help='Absolute target-native root (local default: ASTRID_ENGINE_ROOT, registered root, or ~/.astrid-data/engines/ENGINE; RunPod default: /workspace/astrid/engines/ENGINE; required for Windows)')
        command.add_argument('--deadline', type=float, default=3600.0, metavar='SECONDS',
                             help='Operation time limit in seconds (default: 3600)')
        if name == 'install':
            command.add_argument('--dry-run', action='store_true', help='Print the verified plan without executing it')
    return parser


def _default_backend(*, engine, platform, placement, root, deadline):
    if placement == 'runpod':
        raise UnsupportedPlacement('RunPod execution awaits W1 (runpod wrapper)')
    from .generation.engine_execution import create_local_backend
    return create_local_backend(engine=engine, platform=platform, placement=placement,
                                root=root, deadline=deadline)


def _root(args):
    if args.root is not None:
        return args.root
    if args.platform.startswith('windows-'):
        raise RecipeValidationError('A target-native --root is required for Windows')
    if args.placement == 'runpod':
        return '/workspace/astrid/engines/' + args.engine
    from .generation.engine_local_endpoint import default_engine_root
    return default_engine_root(args.engine)


class CustodyNotOwnerOnly(UnsafePathError):
    code = 'custody_not_owner_only'


def _custody_preflight(root):
    """Refuse an install whose engine custody directory is not an owner-only directory.

    /workspace is a FUSE mount on pods and does not enforce the owner-only mode that custody needs, so
    this runs before any backend is built and its public message names /workspace as FUSE-unsafe.
    """
    try:
        info = os.lstat(os.path.join(root, '.astrid-engine'))
    except FileNotFoundError:
        return
    if not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o077:
        raise CustodyNotOwnerOnly('engine custody directory must be an owner-only directory')


def _plain(value):
    if is_dataclass(value):
        return {field.name: _plain(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain(item) for item in value]
    return value


def main(argv=None, *, backend_factory=None):
    try:
        args = _parser().parse_args(argv)
        if not math.isfinite(args.deadline) or args.deadline <= 0 or args.deadline > 604800:
            raise RecipeValidationError('deadline must be greater than zero and at most 604800 seconds')
        recipe = load_recipe(args.engine, args.platform)
        plan = plan_install(recipe, root=_root(args), placement=args.placement)
        if args.command == 'install' and args.dry_run:
            print(json.dumps({'plan': _plain(plan), 'recipe': _plain(recipe)}, sort_keys=True, ensure_ascii=False))
            return 0
        if args.command == 'install':
            _custody_preflight(plan.root)
        if args.platform.startswith('windows-'):
            raise UnsupportedPlatform('Windows execution awaits A7')
        deadline = time.monotonic() + args.deadline
        factory = backend_factory if backend_factory is not None else _default_backend
        context, adapter = factory(engine=args.engine, platform=args.platform,
                                   placement=args.placement, root=plan.root, deadline=deadline)
        if args.command == 'install':
            result = install_engine(plan, context=context, adapter=adapter, deadline=deadline)
            if args.placement == 'local' and result.status == 'installed':
                # Run-path resolution (action run, the pack host) finds this install without an env var.
                from .generation.engine_local_endpoint import record_engine_root
                record_engine_root(args.engine, plan.root)
        else:
            result = engine_status(args.engine, platform=args.platform, placement=args.placement,
                                   root=plan.root, context=context, deadline=deadline)
        print(json.dumps(_plain(result) if result is not None else None, sort_keys=True, ensure_ascii=False))
        return 0
    except EngineError as exc:
        # Messages from execution transports can contain command output or secrets.
        # Emit only the stable public code and validated step identifier.
        import re
        step = exc.step_id if isinstance(exc.step_id, str) and re.fullmatch(r'[a-z0-9:_-]{1,128}', exc.step_id) else None
        handle = exc.recovery_handle
        if not isinstance(handle, str) or not re.fullmatch(r'[A-Za-z0-9._:/-]{1,512}', handle):
            handle = None
        messages = {
            'unsupported_platform': 'Target platform or local execution is unavailable; A2/A7 integration is pending.',
            'unsupported_placement': 'RunPod execution awaits W1 (runpod wrapper).',
            'custody_not_owner_only': 'Engine custody directory must be owner-only. /workspace is FUSE-unsafe; '
                                      'install under a local --root.',
        }
        print(json.dumps({'error': {'code': exc.code, 'step': step,
                                    'retryable': exc.retryable, 'recovery_handle': handle,
                                    'message': messages.get(exc.code, 'Engine operation failed.')}},
                         sort_keys=True), file=sys.stderr)
        return 1
    except Exception:  # Avoid leaking transport diagnostics or command arguments.
        print(json.dumps({'error': {'code': 'engine_error', 'step': None}}, sort_keys=True), file=sys.stderr)
        return 1
