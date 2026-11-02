"""Filesystem and transport snapshots, always retained before qualification assertions."""
import dataclasses
from pathlib import Path
import shutil

from .common import exact_path, pages, read, require, save, sha


def retain_tree(source, destination):
    copied = []
    if not source.exists():
        return copied
    exact_path(str(source), kind='directory')
    for path in sorted(source.rglob('*')):
        require(not path.is_symlink(), 'capture', path, 'log alias refused')
        if path.is_file():
            target = destination / path.relative_to(source)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
            copied.append({'relative': str(path.relative_to(source)), 'bytes': target.stat().st_size,
                           'sha256': sha(target)})
    return copied


def collect(ctx):
    out = ctx.evidence
    result = {'installer': read(ctx.task / 'logs/installer-exit.json'),
              'version': str(ctx.version), 'python': str(ctx.python), 'purelib': str(ctx.purelib),
              'boundary': ctx.boundary, 'errors': []}
    result['service_logs'] = retain_tree(ctx.install / 'xdg-state/astrid-runtime/logs', out / 'ordinary-service-logs')
    custody = ctx.install / 'data/runtime/local-execution-custody'
    # Credentials and arbitrary private files are never retained.
    result['custody'] = []
    if custody.exists():
        for path in sorted(custody.rglob('*')):
            if path.is_file() and path.name in {'custody.ledger.json', 'custody.journal.jsonl',
                    'admissions.jsonl', 'designation-host.json', 'host-launch.json'}:
                exact_path(str(path), kind='file')
                target = out / 'custody' / path.relative_to(custody)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, target)
                result['custody'].append({'relative': str(path.relative_to(custody)), 'sha256': sha(target)})
    candidates = [ctx.version / n for n in ('manifest.json', 'version.json', 'profile.json')]
    candidates += [ctx.install / n for n in ('program/current', 'program/install.json',
        'program/service-env.json', 'data/runtime/worker-astrid-profile.json',
        'data/runtime/worker-astrid-ready.json', 'xdg-config/systemd/user/astrid.service',
        'xdg-data/astrid-install.json')]
    for path in candidates:
        if path.is_file():
            exact_path(str(path), kind='file')
            target = out / 'install-context' / path.relative_to(ctx.install)
            if path.suffix == '.json':
                value = read(path)
                if isinstance(value, dict):
                    value.pop('env', None)
                save(target, value)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, target)
    save(out / 'collection.json', result)
    return result


def restart_snapshot(ctx, worker_path, list_capabilities):
    """Attempt independent captures even if ready JSON or the transport is broken."""
    result = {'errors': [], 'service_logs': []}
    out = ctx.evidence
    for name, source in [('canary-ready.json', ctx.install / 'data/runtime/worker-astrid-ready.json'),
                         ('canary-worker-profile.json', worker_path),
                         ('selected-capabilities.json', Path(ctx.payload['matrix']['selected_path']))]:
        try:
            exact_path(str(source), kind='file')
            (out / name).write_bytes(source.read_bytes())
        except Exception as exc:
            result['errors'].append({'snapshot': name, 'path': str(source), 'error': repr(exc)})
    try:
        rows = pages(list_capabilities)
        save(out / 'canary-capabilities.json',
             [dataclasses.asdict(row) if dataclasses.is_dataclass(row) else row for row in rows])
    except Exception as exc:
        result['errors'].append({'snapshot': 'capabilities', 'error': repr(exc)})
    try:
        result['service_logs'] = retain_tree(ctx.install / 'xdg-state/astrid-runtime/logs',
                                             out / 'pre-assert-service-logs')
        if (ctx.install / 'canary.log').is_file():
            shutil.copy2(ctx.install / 'canary.log', out / 'pre-assert-canary.log')
    except Exception as exc:
        result['errors'].append({'snapshot': 'service-logs', 'error': repr(exc)})
    save(out / 'restart-snapshots.json', result)
    return result


def assert_ready(ctx, snapshot):
    require(not snapshot['errors'] and snapshot['service_logs'], 'restart-capture', ctx.evidence, str(snapshot))
    ready = read(ctx.evidence / 'canary-ready.json')
    capabilities = read(ctx.evidence / 'canary-capabilities.json')
    require(ready['source_checkout'] == str(ctx.purelib) and ready['python_executable'] == str(ctx.python),
            'readiness', ctx.evidence, 'installed source/interpreter differ')
    ids = [row['capability_id'] for row in capabilities]
    require(len(ids) == len(set(ids)), 'readiness', ctx.evidence, 'duplicate capability ids')
    for name in ('installed_canary.parent', 'h3_av.prepare'):
        require(name in ready['ready_capabilities'] and any(row['capability_id'] == name
                and row['status'] == 'ready' and row['definition_digest'] for row in capabilities),
                'readiness', ctx.evidence, f'{name} absent, unavailable or unpinned')
    save(ctx.evidence / 'parent-ready-receipt.json', {'result': 'PASS', 'ready': ready,
         'capabilities': capabilities, 'snapshot_manifest': 'restart-snapshots.json'})
