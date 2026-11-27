"""Canonical engine planning and execution CLI.

The backend seam deliberately has no production implementation until the A2
local context and P2-SELF orchestration have been reviewed and integrated.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import stat
import sys
import time
from dataclasses import fields, is_dataclass
from collections.abc import Mapping

from astrid.core.generation.engine_contracts import (
    EngineError, RecipeValidationError, UnsafePathError, UnsupportedPlacement, UnsupportedPlatform,
    custody_diagnostics, custody_message, engine_reason,
)
from astrid.core.generation.engine_recipe import load_recipe
from astrid.core.generation.engine_runner import (
    _disk_anchor, engine_status, install_engine, plan_install,
)


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        # Do not echo untrusted command-line values (which might contain secrets).
        self.exit(2, 'astrid engines: invalid or missing arguments; use --help\n')


def _parser():
    parser = _Parser(prog='astrid engines', description='Plan or inspect a canonical engine installation.')
    commands = parser.add_subparsers(dest='command', required=True, parser_class=_Parser)
    for name in ('install', 'status'):
        command = commands.add_parser(name)
        command.add_argument('engine', help='Engine ID, for example comfyui')
        command.add_argument('--platform', default='linux-x86_64-cu130',
                             help='Target platform (default: linux-x86_64-cu130)')
        command.add_argument('--placement', choices=('local', 'runpod'), default='local',
                             help='Target placement (default: local)')
        command.add_argument('--root', help='Absolute target-native root (local default: registered root, ASTRID_ENGINE_ROOT, or ~/.astrid-data/engines/ENGINE; RunPod default: /root/astrid/engines/ENGINE; required for Windows)')
        command.add_argument('--deadline', type=float, default=3600.0, metavar='SECONDS',
                             help='Operation time limit in seconds (default: 3600)')
        if name == 'install':
            mode = command.add_mutually_exclusive_group()
            mode.add_argument('--dry-run', action='store_true', help='Print the verified plan and managed offer without executing it')
            mode.add_argument('--apply', metavar='OFFER_SHA256', help='Apply exactly the available measured local managed offer')
            command.add_argument('--models-root', help='Absolute target-native model directory outside the engine root (a saved volume cache); default: <root>/models')
    return parser


def _default_backend(*, engine, platform, placement, root, deadline):
    if placement == 'runpod':
        raise UnsupportedPlacement('This machine manages only its own engine; a remote machine prepares its engine when its job starts.')
    from .generation.engine_execution import create_local_backend
    return create_local_backend(engine=engine, platform=platform, placement=placement,
                                root=root, deadline=deadline)


def _root(args):
    if args.root is not None:
        return args.root
    if args.platform.startswith('windows-'):
        raise RecipeValidationError('A target-native --root is required for Windows')
    if args.placement == 'runpod':
        return '/root/astrid/engines/' + args.engine
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
        return {field.name: _plain(getattr(value, field.name)) for field in fields(value)
                if field.name != 'managed_offer_sha256' or getattr(value, field.name) is not None}
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain(item) for item in value]
    return value


# Policy minimum free space before an install starts (a proxy, not a measured byte count).
ENGINE_INSTALL_MIN_FREE_BYTES = 10 * 1024 ** 3


# A source-owned measurement is deliberately absent until 94-D's native Linux
# receipt is accepted. Never substitute the legacy policy floor, a test profile,
# a catalogue download size or a caller-provided file for that evidence.
_LINUX_MANAGED_MEASUREMENT = None
_BUDGET_FIELDS = ('download_bytes', 'unpack_bytes', 'environment_bytes',
                  'model_bytes', 'cleanup_reserve_bytes', 'concurrent_bytes')


class ManagedOfferRejected(EngineError):
    code = 'managed_offer_rejected'

    def __init__(self, reason):
        super().__init__('Managed install offer is unavailable or changed.', retryable=True)
        self.offer_reason = reason


def _linux_supported(plan):
    return (plan.engine == 'comfyui' and plan.platform == 'linux-x86_64-cu130'
            and plan.placement == 'local' and sys.platform == 'linux'
            and os.uname().machine == 'x86_64')


def _managed_locations(plan):
    # The existing bootstrap pins UV_CACHE_DIR under root, but managed Python,
    # pip and subprocess temporary files need separate volume accounting.
    # This first measured route does not claim arbitrary cache/temp overrides.
    overrides = ('TMPDIR', 'TMP', 'TEMP', 'UV_PYTHON_INSTALL_DIR', 'XDG_CACHE_HOME',
                 'XDG_DATA_HOME', 'PIP_CACHE_DIR', 'ASTRID_ENGINE_SANITY_CMD')
    if any(os.environ.get(k) for k in overrides):
        return None
    home = os.path.expanduser('~')
    return {'root': plan.root, 'temp': '/tmp', 'var_temp': '/var/tmp',
            'home_other': home, 'home_cache': os.path.join(home, '.cache'),
            'pip_cache': os.path.join(home, '.cache', 'pip'),
            'managed_python': os.path.join(home, '.local', 'share', 'uv', 'python'),
            'registration': os.path.join(home, '.astrid-data', 'engines')}


def _managed_root_state(plan, deadline, *, locked=False):
    """Read the existing transaction receipt, never adopt an unowned directory."""
    from .generation._engine_target_files import _TargetFiles
    from .generation.engine_runner import _parse_receipt
    files = _TargetFiles(plan.root)
    custody = plan.root + '/.astrid-engine'
    try:
        with files._directory(files._relative(plan.root), deadline) as fd:
            info = os.fstat(fd)
            if info.st_uid != os.geteuid() or info.st_mode & 0o022:
                return 'foreign', None
            names = set(os.listdir(fd))
        if not names:
            return 'empty', None
        if '.astrid-engine' not in names:
            return 'foreign', None
        with files._directory(files._relative(custody), deadline) as fd:
            info = os.fstat(fd)
            if info.st_uid != os.geteuid() or info.st_mode & 0o077:
                return 'foreign', None
            custody_names = set(os.listdir(fd))
        if locked and names == {'.astrid-engine'} and custody_names == {'install.lock'}:
            # Only the current existing transaction's acquired lock may have
            # created this directory. A receipt-less pre-existing root refuses.
            return 'empty', None
        path = custody + '/install.json'
        fact = files.file_fact(path, hash_bytes=False, deadline=deadline)
        if not fact.regular or fact.symlink or fact.bytes is None or fact.bytes > 1024 * 1024:
            return 'foreign', None
        receipt = _parse_receipt(files.read_json(path, deadline=deadline), plan)
        if (receipt.recipe_sha256 != plan.recipe_sha256
                or receipt.models_lock_sha256 != plan.models_lock_sha256
                or receipt.models_root != plan.models_root):
            return 'foreign', None
        if receipt.status == 'installed':
            return 'installed', receipt.managed_offer_sha256
        if receipt.error_code == 'custody_unresolved' or receipt.managed_offer_sha256 is None:
            return 'foreign', None
        return 'interrupted', receipt.managed_offer_sha256
    except FileNotFoundError:
        return 'foreign', None
    except (OSError, EngineError):
        return 'unknown', None


def _managed_preview(plan, recipe, seconds, *, deadline=None, locked=False):
    """Return an inert offer; live free bytes are observations, not consent identity."""
    offer = {'schema': 'astrid.engine-managed-offer.v1', 'available': False,
             'offer_sha256': None, 'location': plan.root, 'cost_usd': 0,
             'cost_scope': 'no provider charge; local electricity/network not included',
             'estimated_seconds': None, 'peak_additional_bytes': None,
             'volumes': [], 'model_download_bytes': 0, 'models_included': False,
             'previous_root': None, 'previous_copy_retained': True,
             'apply_argv': None, 'reason': 'measurement_unavailable'}
    if not _linux_supported(plan):
        offer['reason'] = 'unsupported_local_platform'
        return offer
    if plan.models_root is not None:
        offer['reason'] = 'external_models_root_unqualified'
        return offer
    measurement = _LINUX_MANAGED_MEASUREMENT
    if measurement is None:
        return offer
    locations = _managed_locations(plan)
    if locations is None:
        offer['reason'] = 'unqualified_environment'
        return offer
    # These are native receipt-derived conservative bounds, not a size guess.
    # Require complete component observations for every actual destination role.
    bootstrap = 'installer_uv' if os.environ.get('ASTRID_INSTALL_UV') else 'system_pip'
    try:
        receipt_sha = measurement['receipt_sha256']
        elapsed = measurement['measured_seconds']
        estimate = measurement['estimated_seconds']
        previous_bytes = measurement['retained_working_copy_sample_bytes']
        budgets = measurement['locations']
        if (measurement['recipe_sha256'] != plan.recipe_sha256
                or measurement['bootstrap_kind'] != bootstrap
                or not isinstance(receipt_sha, str) or not re.fullmatch(r'[0-9a-f]{64}', receipt_sha)
                or type(elapsed) is not int or elapsed <= 0
                or type(estimate) is not int or estimate < elapsed
                or type(previous_bytes) is not int or previous_bytes <= 0
                or set(budgets) != set(locations)):
            return offer
        for budget in budgets.values():
            if (set(budget) != set(_BUDGET_FIELDS)
                    or any(type(v) is not int or v < 0 for v in budget.values())
                    or budget['model_bytes'] != 0):
                return offer
        if not all(sum(b[field] for b in budgets.values()) > 0
                   for field in ('download_bytes', 'unpack_bytes', 'environment_bytes',
                                 'cleanup_reserve_bytes', 'concurrent_bytes')):
            return offer
    except (KeyError, TypeError, ValueError, AttributeError):
        return offer
    deadline = deadline if deadline is not None else time.monotonic() + seconds
    from .generation.engine_local_endpoint import default_engine_root
    try:
        previous = default_engine_root(plan.engine)
        offer['previous_root'] = previous
        if previous != plan.root and os.path.commonpath((previous, plan.root)) in (previous, plan.root):
            offer['reason'] = 'overlapping_previous_root'
            return offer
        anchor, device, _, _ = _disk_anchor(plan.root)
        # A non-existent root is distinct from an inaccessible or foreign one.
        state, stored_digest = (('empty', None) if anchor != plan.root
                                else _managed_root_state(plan, deadline, locked=locked))
        if state == 'installed':
            offer.update(reason='already_installed', completed_offer_sha256=stored_digest)
            return offer
        if state not in ('empty', 'interrupted'):
            offer['reason'] = 'root_not_owned_or_empty'
            return offer
        # Refuse nested mounts under the engine tree until a corresponding
        # measured role exists. Never allocate on an unaccounted device.
        nested = ['runtime', 'runtime/uv-cache', 'runtime/bootstrap', '.astrid-engine',
                  *[x.relative_path for x in recipe.sources],
                  *[x.relative_path for x in recipe.environments]]
        if any(_disk_anchor(os.path.join(plan.root, p))[1] != device for p in nested):
            offer['reason'] = 'unqualified_nested_volume'
            return offer
        rows, volumes = [], {}
        for role, path in sorted(locations.items()):
            if not os.path.isabs(path) or os.path.normpath(path) != path:
                raise ValueError('noncanonical allocation path')
            actual_anchor, dev, _, free = _disk_anchor(path)
            if role in ('temp', 'var_temp') and actual_anchor != path:
                raise ValueError('temporary directory missing')
            row = {'role': role, 'path': path, 'device': dev, **budgets[role]}
            rows.append(row)
            volume = volumes.setdefault(dev, {'device': dev, 'paths': [], 'required_bytes': 0,
                                              'free_bytes': free})
            volume['paths'].append(path)
            volume['required_bytes'] += sum(budgets[role].values())
            volume['free_bytes'] = min(volume['free_bytes'], free)
    except (OSError, ValueError, EngineError):
        offer['reason'] = 'capacity_or_root_unknown'
        return offer
    # The previous working copy remains allocated, so current free space
    # already excludes it. Never subtract it again or credit deletion.
    identity = {'schema': offer['schema'], 'plan': _plain(plan), 'previous_root': previous,
                'receipt_sha256': receipt_sha, 'measured_seconds': elapsed,
                'estimated_seconds': estimate, 'deadline_seconds': seconds,
                'retained_working_copy_sample_bytes': previous_bytes,
                'bootstrap_kind': bootstrap, 'bootstrap_executable': os.environ.get('ASTRID_INSTALL_UV'),
                'cost_usd': 0, 'models_included': False, 'allocations': rows}
    digest = hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(',', ':'),
                                      ensure_ascii=False, allow_nan=False).encode('utf-8')).hexdigest()
    volume_rows = [volumes[d] for d in sorted(volumes)]
    for volume in volume_rows:
        volume['status'] = 'ok' if volume['free_bytes'] >= volume['required_bytes'] else 'blocked'
    offer.update(offer_sha256=digest, estimated_seconds=estimate, measurement_sha256=receipt_sha,
                 retained_working_copy_sample_bytes=previous_bytes, allocations=rows,
                 volumes=volume_rows, peak_additional_bytes=sum(v['required_bytes'] for v in volume_rows))
    if state == 'interrupted' and stored_digest != digest:
        offer['reason'] = 'interrupted_offer_changed'
    elif seconds < estimate:
        offer['reason'] = 'deadline_too_short'
    elif any(v['status'] != 'ok' for v in volume_rows):
        offer['reason'] = 'insufficient_disk'
    else:
        offer.update(available=True, reason=None,
                     apply_argv=['astrid', 'engines', 'install', plan.engine, '--platform', plan.platform,
                                 '--placement', 'local', '--root', plan.root, '--deadline', str(seconds),
                                 '--apply', digest])
    return offer


def _require_managed_offer(plan, recipe, seconds, digest, *, deadline=None, locked=False):
    offer = _managed_preview(plan, recipe, seconds, deadline=deadline, locked=locked)
    if not offer['available']:
        raise ManagedOfferRejected(offer['reason'])
    if not isinstance(digest, str) or not re.fullmatch(r'[0-9a-f]{64}', digest) or digest != offer['offer_sha256']:
        raise ManagedOfferRejected('offer_changed')
    return offer


def main(argv=None, *, backend_factory=None):
    try:
        args = _parser().parse_args(argv)
        if not math.isfinite(args.deadline) or args.deadline <= 0 or args.deadline > 604800:
            raise RecipeValidationError('deadline must be greater than zero and at most 604800 seconds')
        recipe = load_recipe(args.engine, args.platform)
        plan = plan_install(recipe, root=_root(args), placement=args.placement,
                            models_root=getattr(args, 'models_root', None))
        if args.command == 'install':
            # Plan 32 T5h: an install is model-free, so no model bytes are required. Sources and
            # environments have no recorded sizes yet, so the install is gated on a policy floor:
            # a proxy minimum, not a measured byte count.
            required = ENGINE_INSTALL_MIN_FREE_BYTES
            disk = {'required_bytes': required, 'free_bytes': None, 'status': 'unknown',
                    'basis': 'minimum_free_space_floor'}
            if args.apply is None and plan.placement == 'local' and plan.platform.startswith('linux-') and os.name == 'posix':
                try:
                    _, _, _, free = _disk_anchor(plan.root)
                except OSError:
                    if not args.dry_run:
                        raise
                else:
                    disk.update(free_bytes=free, status='ok' if free >= required else 'blocked')
            if args.dry_run:
                offer = _managed_preview(plan, recipe, args.deadline)
                print(json.dumps({'plan': _plain(plan), 'recipe': _plain(recipe), 'disk_check': disk,
                                  'managed_offer': offer}, sort_keys=True, ensure_ascii=False))
                return 0
            if args.apply is not None:
                if args.root is None:
                    raise ManagedOfferRejected('explicit_root_required')
                offer = _managed_preview(plan, recipe, args.deadline)
                if (offer['reason'] == 'already_installed'
                        and re.fullmatch(r'[0-9a-f]{64}', args.apply)
                        and offer.get('completed_offer_sha256') == args.apply):
                    print(json.dumps({'status': 'already_installed', 'location': plan.root,
                                      'next_argv': ['astrid', 'engines', 'status', plan.engine,
                                                    '--platform', plan.platform, '--placement', 'local',
                                                    '--root', plan.root]}, sort_keys=True))
                    return 0
                _require_managed_offer(plan, recipe, args.deadline, args.apply)
            if disk['status'] == 'blocked':
                if os.path.lexists(os.path.join(plan.root, '.astrid-engine')):
                    # Check custody before reporting the disk constraint.
                    _custody_preflight(plan.root)
                print(json.dumps({'error': {'code': 'ENGINE_INSTALL_INSUFFICIENT_DISK', 'step': None,
                    'reason': engine_reason('insufficient disk space for engine installation'),
                    'retryable': True, 'recovery_handle': None,
                    'message': 'Free disk space or choose a different --root, then retry.',
                    'required_bytes': required, 'free_bytes': disk['free_bytes']}}, sort_keys=True),
                    file=sys.stderr)
                return 1
        if args.command == 'install':
            _custody_preflight(plan.root)
        if args.platform.startswith('windows-'):
            raise UnsupportedPlatform('Windows execution awaits A7')
        deadline = time.monotonic() + args.deadline
        factory = backend_factory if backend_factory is not None else _default_backend
        context, adapter = factory(engine=args.engine, platform=args.platform,
                                   placement=args.placement, root=plan.root, deadline=deadline)
        if args.command == 'install':
            managed = {}
            if args.apply is not None:
                managed = {'managed_offer_sha256': args.apply,
                           'before_managed_install': lambda: _require_managed_offer(
                               plan, recipe, args.deadline, args.apply, deadline=deadline, locked=True)}
            result = install_engine(plan, context=context, adapter=adapter, deadline=deadline, **managed)
            if args.placement == 'local' and args.root is not None and result.status == 'installed':
                # Run-path resolution (action run, the pack host) finds this install without an env var.
                from .generation.engine_local_endpoint import record_engine_root
                record_engine_root(args.engine, plan.root)
        else:
            result = engine_status(args.engine, platform=args.platform, placement=args.placement,
                                   root=plan.root, context=context, deadline=deadline)
        print(json.dumps(_plain(result) if result is not None else None, sort_keys=True, ensure_ascii=False))
        return 0
    except EngineError as exc:
        if isinstance(exc, ManagedOfferRejected):
            print(json.dumps({'error': {'code': exc.code, 'reason': exc.offer_reason,
                                        'message': 'Inspect a fresh managed dry-run; no install was authorized.'}},
                             sort_keys=True), file=sys.stderr)
            return 1
        if isinstance(exc, CustodyNotOwnerOnly):
            exc.custody_details = dict(reason='custody_not_private', owner_state='unknown',
                                      lock_state='unknown', evidence='none', root=plan.root, engine=plan.engine,
                                      marker_path=plan.root + '/.astrid-engine/session/session.json',
                                      helper_sha256='unknown')
        # Messages from execution transports can contain command output or secrets.
        # Emit only declared fixed reasons and validated public fields.
        import re
        step = exc.step_id if isinstance(exc.step_id, str) and re.fullmatch(r'[a-z0-9:_-]{1,128}', exc.step_id) else None
        handle = exc.recovery_handle
        if not isinstance(handle, str) or not re.fullmatch(r'[A-Za-z0-9._:/-]{1,512}', handle):
            handle = None
        messages = {
            'unsupported_platform': 'Target platform or local execution is unavailable; A2/A7 integration is pending.',
            'unsupported_placement': 'This machine manages only its own engine; a remote machine prepares its engine when its job starts.',
            'engine_busy': 'Managed engine is busy; wait for the current operation to finish, then retry.',
            'custody_not_owner_only': 'Engine custody directory must be owner-only. /workspace is FUSE-unsafe; '
                                      'install under a local --root.',
            'endpoint_ownership': 'Engine endpoint ownership could not be verified; inspect the engine owner and retry.',
            'custody_unresolved': 'Engine custody could not be verified; inspect the engine state before retrying.',
            'unsafe_path': 'Engine custody path or state is unsafe; inspect the engine root before retrying.',
        }
        public = {'code': exc.code, 'step': step, 'retryable': exc.retryable,
                  'recovery_handle': handle, 'message': messages.get(exc.code, 'Engine operation failed.')}
        reason = engine_reason(exc.reason)
        if reason is not None:
            public['reason'] = reason
        details = custody_diagnostics(getattr(exc, 'custody_details', None))
        if details is not None:
            public['custody'] = details
            public['message'] = custody_message(details)
        if exc.code == 'endpoint_ownership' and reason is not None:
            if reason == 'engine port is occupied by an unowned service':
                port = getattr(exc, 'port', None)
                check = f'Port {port}' if type(port) is int and 1 <= port <= 65535 else 'Engine port'
                public['message'] = (check + ' is held by a service this installation does not own: '
                                     'stop it, or let the previous job release the engine.')
                if type(port) is int and 1 <= port <= 65535:
                    public['port'] = port
            else:
                public['message'] = reason + '; inspect the engine owner and launch, then retry.'
        if exc.code == 'engine_busy':
            running, pending = getattr(exc, 'running_count', None), getattr(exc, 'pending_count', None)
            if type(running) is int and running >= 0 and type(pending) is int and pending >= 0:
                public['message'] += f' running={running}, pending={pending}.'
        print(json.dumps({'error': public},
                         sort_keys=True), file=sys.stderr)
        return 1
    except Exception:  # Avoid leaking transport diagnostics or command arguments.
        print(json.dumps({'error': {'code': 'engine_error', 'step': None,
              'reason': engine_reason('unexpected engine operation failure')}}, sort_keys=True), file=sys.stderr)
        return 1
