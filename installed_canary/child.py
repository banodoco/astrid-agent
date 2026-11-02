"""Installed-interpreter entrypoint. Only this process imports product packages."""
import argparse
import importlib
import importlib.metadata
import inspect
from pathlib import Path
import sys
import sysconfig

# -I deliberately excludes cwd/PYTHONPATH; select only this committed tool root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from installed_canary.common import read, require, save, sha
from installed_canary.contract import validate
from installed_canary.tool import fingerprint


def audit_shapes(shapes, purelib):
    """Import and bind call shapes, invoking none of the selected APIs."""
    results = []
    for shape in shapes:
        row = dict(shape)
        try:
            obj = importlib.import_module(shape['module'])
            for component in shape['attribute'].split('.'):
                obj = getattr(obj, component)
            origin = Path(inspect.getfile(obj)).resolve()
            require(origin.is_relative_to(purelib), 'api-origin', origin, 'outside installed purelib')
            signature = inspect.signature(obj)
            signature.bind(*([object()] * shape['positional']),
                           **{name: object() for name in shape['keywords']})
            row.update(result='PASS', origin=str(origin), signature=str(signature))
        except Exception as exc:
            row.update(result='FAIL', error_type=type(exc).__name__, error=str(exc))
        results.append(row)
    return results


def api_check(ctx):
    shapes = read(Path(__file__).parent / 'api_shapes.json')
    results = audit_shapes(shapes, ctx.purelib)
    realm = read(ctx.install / 'data/runtime/discovery.json').get('active_realm')
    passed = all(row['result'] == 'PASS' for row in results) and isinstance(realm, str) and bool(realm.strip())
    save(ctx.evidence / 'api.json', {'result': 'PASS' if passed else 'FAIL', 'checked': len(results),
         'checks': results, 'realm_id': realm, 'api_invocations': 0, 'python': sys.executable,
         'prefix': sys.prefix, 'fingerprint': fingerprint(ctx)})
    require(passed, 'api-signatures', ctx.evidence / 'api.json', 'signature/origin/realm audit failed')


def identity(ctx):
    from astrid.installer.core import current
    version = current(ctx.install / 'program')
    require(version and version.python == Path(sys.executable) and version.root == ctx.version,
            'identity', ctx.version, 'installer selected version/interpreter differ')
    origins = {}
    source_for = {'astrid': ('agent', 'astrid/__init__.py'),
                  'astrid_runtime': ('runtime', 'astrid_runtime/__init__.py'),
                  'runtime_protocol': ('runtime', 'runtime_protocol/__init__.py'),
                  'astrid_runtime_client': ('agent', 'astrid_runtime_client/__init__.py'),
                  'vibecomfy': ('vibecomfy', 'vibecomfy/__init__.py')}
    from installed_canary.contract import blob
    import hashlib
    for name, (source, relative) in source_for.items():
        origin = Path(importlib.import_module(name).__file__).resolve()
        require(origin.is_relative_to(ctx.purelib), 'identity', origin, 'ambient module refused')
        require(origin.read_bytes() == blob(ctx.payload['sources'][source], relative),
                'identity', origin, 'module differs from selected Git blob')
        origins[name] = {'path': str(origin), 'sha256': hashlib.sha256(origin.read_bytes()).hexdigest()}
    distributions = {}
    for name in ('astrid', 'astrid-runtime', 'astrid-runtime-client', 'vibecomfy', 'opencv-python-headless',
                 'runpod-lifecycle', 'runpod'):
        dist = importlib.metadata.distribution(name)
        require(Path(dist.locate_file('')).resolve() == ctx.purelib,
                'identity', name, 'distribution metadata outside installed purelib')
        record = dist.read_text('RECORD')
        require(bool(record), 'identity', name, 'distribution RECORD missing')
        distributions[name] = {'version': dist.version, 'RECORD_sha256': hashlib.sha256(record.encode()).hexdigest()}
    save(ctx.evidence / 'identity.json', {'origins': origins, 'distributions': distributions,
         'python': sys.executable, 'prefix': sys.prefix, 'base_prefix': sys.base_prefix})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--stage', required=True, choices=('api', 'identity', 'contracts', 'journey'))
    for option in ('tuple', 'task-dir', 'work-dir'):
        parser.add_argument('--' + option, required=True, type=Path)
    args = parser.parse_args()
    ctx = validate(args.tuple, args.task_dir, args.work_dir, fresh=False)
    require(not sys.flags.optimize and Path(sys.executable) == ctx.python
            and Path(sys.prefix) == ctx.version / 'py'
            and Path(sysconfig.get_path('purelib')).resolve() == ctx.purelib
            and Path(sys.base_prefix).resolve().is_relative_to(ctx.install / 'xdg-data/astrid/bootstrap/python'),
            'interpreter', sys.executable, 'exact managed installed interpreter without optimization required')
    launch = read(ctx.evidence / 'launch.json')
    require(fingerprint(ctx) == launch['fingerprint'], 'integrity', ctx.evidence, 'driver/tuple changed')
    if args.stage == 'api':
        api_check(ctx)
        return
    receipt = read(ctx.evidence / 'api.json')
    require(receipt['result'] == 'PASS' and receipt['fingerprint'] == fingerprint(ctx)
            and receipt['python'] == sys.executable and receipt['prefix'] == sys.prefix,
            'api-receipt', ctx.evidence, 'stale or mismatched API audit')
    if args.stage == 'identity':
        identity(ctx)
    elif args.stage == 'contracts':
        from installed_canary.contracts import run
        run(ctx)
    else:
        from installed_canary.journey import run
        run(ctx)


if __name__ == '__main__':
    main()
