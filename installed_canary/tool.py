"""One installed-canary entrypoint. It never installs or acquires a pod/GPU."""
import argparse
import json
from pathlib import Path
import subprocess
import sys

from .common import GateError, inventory, read, require, save, sha
from .contract import validate
from .evidence import collect


def fingerprint(ctx):
    code = Path(__file__).parent
    return {'tuple': sha(ctx.tuple_path), 'files': {
        str(path.relative_to(code)): sha(path) for path in sorted(code.rglob('*'))
        if path.is_file() and (path.suffix in {'.py', '.json', '.yaml'})}}


def child_env(ctx):
    # No ambient Python, uv, Astrid, proxies, credentials or developer selectors.
    return {'PATH': '/usr/local/bin:/usr/bin:/bin', 'LANG': 'C.UTF-8',
            'HOME': str(ctx.install / 'home'), 'TMPDIR': str(ctx.install / 'tmp'),
            'XDG_DATA_HOME': str(ctx.install / 'xdg-data'),
            'XDG_STATE_HOME': str(ctx.install / 'xdg-state'),
            'XDG_CONFIG_HOME': str(ctx.install / 'xdg-config'),
            'XDG_CACHE_HOME': str(ctx.install / 'xdg-cache'),
            'PYTHONDONTWRITEBYTECODE': '1'}


REDACTED = '[REDACTED-CREDENTIAL]'


def credential_secrets(folder):
    """Every secret-looking value in the test-owned credential files: the raw text, and string values >= 8 chars."""
    secrets = set()
    for path in sorted(Path(folder).glob('*')) if Path(folder).is_dir() else ():
        if not path.is_file():
            continue
        raw = path.read_text(errors='replace').strip()
        if raw:
            secrets.add(raw)
        try:
            value = json.loads(raw)
        except ValueError:
            continue
        stack = [value]
        while stack:
            item = stack.pop()
            if isinstance(item, dict):
                stack.extend(item.values())
            elif isinstance(item, list):
                stack.extend(item)
            elif isinstance(item, str) and len(item) >= 8:
                secrets.add(item)
    return secrets


def redact_credentials(folder, *logs):
    """Retained evidence must never carry credential content (e.g. in a child traceback)."""
    secrets = sorted(credential_secrets(folder), key=len, reverse=True)
    for log in logs:
        log = Path(log)
        if not secrets or not log.is_file():
            continue
        text = log.read_text(errors='replace')
        clean = text
        for secret in secrets:
            for form in {secret, json.dumps(secret)[1:-1], repr(secret)[1:-1]}:
                clean = clean.replace(form, REDACTED)
        if clean != text:
            log.write_text(clean)


def run_child(ctx, stage, frozen):
    require(fingerprint(ctx) == frozen, stage, ctx.tuple_path, 'driver/tuple changed after validation')
    if stage == 'cli':
        argv = [str(ctx.python), '-I', '-B', '-m', 'astrid', '--help']
    else:
        argv = [str(ctx.python), '-I', '-B', str(Path(__file__).parent / 'child.py'),
                '--stage', stage, '--tuple', str(ctx.tuple_path), '--task-dir', str(ctx.task),
                '--work-dir', str(ctx.work)]
    with (ctx.evidence / f'{stage}.log').open('w') as output:
        process = subprocess.run(argv, cwd=ctx.version, env=child_env(ctx),
                                 stdout=output, stderr=subprocess.STDOUT)
    redact_credentials(ctx.install / 'data/credentials', ctx.evidence / f'{stage}.log')
    record = {'stage': stage, 'argv': argv, 'cwd': str(ctx.version),
              'environment': child_env(ctx), 'exit': process.returncode}
    with (ctx.evidence / 'commands.jsonl').open('a') as output:
        output.write(json.dumps(record, sort_keys=True) + '\n')
    require(process.returncode == 0, stage, ctx.evidence / f'{stage}.log',
            f'command={argv!r} exit={process.returncode}; inspect retained log')


def execute(ctx, action):
    # The public composition root is shared by the CLI and the CPU process-edge tests.
    ctx.evidence.mkdir(parents=True, exist_ok=False)
    frozen = fingerprint(ctx)
    save(ctx.evidence / 'launch.json', {'fingerprint': frozen, 'tuple': ctx.payload,
                                      'action': action, 'authorization': 'caller-owned; no canary authorized by tool'})
    stage = 'collection'
    try:
        result = collect(ctx)
        if action == 'inventory':
            return result
        stage = 'installer-receipt'
        require(result['installer'].get('exit') == 0, stage, ctx.task / 'logs/installer-exit.json',
                'installer did not succeed; qualification withheld after evidence capture')
        require((ctx.install / 'data/runtime/worker-astrid-ready.json').is_file(),
                'ordinary-readiness', ctx.install, 'verified-ready marker absent; no installed APIs')
        for name in ('home', 'tmp', 'xdg-data', 'xdg-state', 'xdg-config', 'xdg-cache'):
            (ctx.install / name).mkdir(parents=True, exist_ok=True)
        for stage in ('api', 'identity', 'cli', 'contracts', 'journey'):
            run_child(ctx, stage, frozen)
            if stage == 'api':
                receipt = read(ctx.evidence / 'api.json')
                require(receipt['result'] == 'PASS' and receipt['api_invocations'] == 0
                        and receipt['checked'] == len(read(Path(__file__).parent / 'api_shapes.json'))
                        and receipt['fingerprint'] == frozen,
                        stage, ctx.evidence / 'api.json', 'incomplete or stale installed API audit')
            elif stage == 'identity':
                receipt = read(ctx.evidence / 'identity.json')
                require(receipt['result'] == 'PASS' and receipt['fingerprint'] == frozen,
                        stage, ctx.evidence, 'identity receipt incomplete or stale')
            elif stage == 'contracts':
                from .contracts import CONTRACT_NAMES
                receipt = read(ctx.evidence / 'contracts.json')
                require(receipt.get('fingerprint') == frozen
                        and all(receipt.get(n, {}).get('result') == 'PASS' for n in CONTRACT_NAMES),
                        stage, ctx.evidence, 'four composition contract receipts required')
        require(read(ctx.evidence / 'parent-ready-receipt.json')['result'] == 'PASS'
                and read(ctx.evidence / 'h3-terminal-pass.json')['result'] == 'PASS',
                'journey', ctx.evidence, 'journey receipts absent')
        save(ctx.evidence / 'qualification.json', {'result': 'PASS', 'tuple': ctx.payload,
                                                'fingerprint': frozen, 'cpu_only': True})
    except BaseException as exc:
        save(ctx.evidence / 'failure.json', {'stage': stage, 'path': str(ctx.install),
             'error_type': type(exc).__name__, 'error': str(exc), 'status': 'STOP / UNQUALIFIED'})
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('inventory', 'qualify'))
    parser.add_argument('--tuple', required=True, type=Path)
    parser.add_argument('--task-dir', required=True, type=Path)
    parser.add_argument('--work-dir', required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        ctx = validate(args.tuple, args.task_dir, args.work_dir)
        execute(ctx, args.action)
    except (GateError, OSError, ValueError, KeyError, TypeError) as exc:
        print(f'installed-canary refused: {exc}', file=sys.stderr)
        return 1
    return 0
