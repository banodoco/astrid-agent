"""Observe the installed canonical CLI; preserve every return value and check."""
import contextlib
from dataclasses import replace
import hashlib
import io
import os
import json
from pathlib import Path
import re
import sys
import time

PREFIX = 'H91_HELPER_SITE '
# Executed before the unchanged, source-bound finite helper program. The helper
# imports, digest checks, operation, request and result are all unchanged.
PRELUDE = r'''
import sys, json
def h91_trace(frame,event,arg):
    if event=='exception' and frame.f_globals.get('__name__') in {
        'astrid.core.generation.engine_runner_target',
        'astrid.core.generation._engine_target_files',
        'astrid.core.generation.engine_recipe'}:
        kind=type(arg[1]).__name__
        if kind in {'IntegrityError','UnsafePathError','RecipeValidationError',
                    'FileNotFoundError','PermissionError','ValueError','KeyError','TypeError','OSError'}:
            print('H91_HELPER_SITE '+json.dumps(dict(module=frame.f_globals['__name__'],
                function=frame.f_code.co_name,line=frame.f_lineno,type=kind)),file=sys.stderr,flush=True)
    return h91_trace
sys.settrace(h91_trace)
'''


def instrument(program):
    return PRELUDE + '\nexec(compile(' + repr(program) + ", '<h91-original-helper>', 'exec'))\n"


def command_diagnostic(request, result, active):
    """Bounded task evidence, retaining the original process result unchanged."""
    return dict(exit_code=result.exit_code, timed_out=result.timed_out,
                step=active['step'], argv=list(request.argv), cwd=request.cwd,
                bootstrap_index=active['bootstrap'].get(tuple(request.argv)),
                stderr=result.stderr[-65536:], stdout=result.stdout[-65536:],
                stderr_truncated=result.stderr_truncated or len(result.stderr)>65536,
                stdout_truncated=result.stdout_truncated or len(result.stdout)>65536,
                stderr_sha256=hashlib.sha256((result.stderr or '').encode()).hexdigest(),
                http_codes=re.findall(r'(?:HTTP Error |status code )([45][0-9]{2})', result.stderr or '')[-8:])


def main():
    from astrid.core import engine_cli
    from astrid.core.generation import engine_runner as runner, engine_runner_target as target, engine_custody
    from astrid.core.generation.engine_contracts import EngineError
    from astrid.core.generation.engine_recipe import load_recipe
    root, cache, device, mount_id, output = sys.argv[1:]
    out = Path(output)
    progress = out.parent / 'progress.log'
    def emit(phase, **values):
        value = dict(schema='h91.capture.v1', phase=phase, utc=time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()), **values)
        with (out / 'engine-events.jsonl').open('a') as stream:
            stream.write(json.dumps(value, sort_keys=True) + '\n')
        with progress.open('a') as stream:
            stream.write(value['utc'] + ' ' + phase + ' ' + values.get('status', '') + '\n')
    canary = os.environ.get('H91_CANARY_STOP_BEFORE_GPU') == '1'
    args = ['install', 'vibecomfy-h3', '--platform', 'linux-x86_64-cu130', '--placement', 'local',
            '--root', root, '--model-cache-root', cache, '--model-cache-device', device,
            '--model-cache-mount-id', mount_id, '--deadline', '3000']
    if canary:
        args = args[:args.index('--model-cache-device')] + ['--deadline','3000']
    recipe = load_recipe('vibecomfy-h3', 'linux-x86_64-cu130')
    plan = runner.plan_install(recipe, root=root, placement='local')
    allowed = runner._status_step_ids(plan) | {'sanity'}
    assert not Path(root).exists(), 'fresh root required'
    emit('engine-plan', status='passed', recipe_sha256=plan.recipe_sha256, models_lock_sha256=plan.models_lock_sha256,
         helper_sha256=runner._sha(runner.helper_manifest(recipe)), argv=args,
         module_sha256={m.__name__: hashlib.sha256(Path(m.__file__).read_bytes()).hexdigest()
                        for m in (runner, target, engine_cli, engine_custody)})
    active = {'step': None, 'bootstrap': {}}
    sites = []
    def trace(frame, event, arg):
        module = frame.f_globals.get('__name__', '')
        if event == 'exception' and isinstance(arg[1], EngineError) and module.startswith('astrid.core.'):
            exc = arg[1]
            site = dict(module=module, function=frame.f_code.co_name, line=frame.f_lineno,
                        type=type(exc).__name__, code=exc.code, step=exc.step_id if exc.step_id in allowed else None)
            if site not in sites:
                sites.append(site)
                emit('engine-exception-site', status='observed', **site)
        return trace
    execute = runner._Runner.execute
    def observed_execute(self, step, **kwargs):
        started = time.monotonic()
        active['step'] = step.id
        active['bootstrap'] = {}
        if step.kind == 'environment':
            environment = next(e for e in self.recipe.environments if 'environment:' + e.name == step.id)
            active['bootstrap'] = {tuple(argv): index for index, argv in enumerate(self.adapter.environment_commands(self.recipe, environment, self.plan.root))}
        emit('step', status='started', step=step.id)
        try:
            result = execute(self, step, **kwargs)
        except BaseException:
            emit('step', status='failed', step=step.id, seconds=round(time.monotonic()-started, 3))
            raise
        emit('step', status='passed', step=step.id, seconds=round(time.monotonic()-started, 3))
        return result
    run = engine_custody.LinuxEngineServiceClient.run
    def observed_run(self, request):
        original = request
        if canary:
            import shutil
            free = shutil.disk_usage(Path(root).parent).free
            if free < 2_000_000_000:
                emit('canary-disk-stop', status='blocked', free_bytes=free, argv=list(request.argv))
                raise CanaryStop('disk below 2 GB')
            emit('finite-command-before', status='observed', step=active['step'], argv=list(request.argv), cwd=request.cwd)
            if any(str(arg).endswith('/scripts/gpu_sanity.py') for arg in request.argv):
                emit('canary-gpu-boundary', status='stopped-before-execution', step='sanity',
                     argv=list(request.argv), cwd=request.cwd, environment=dict(request.env), free_bytes=free)
                raise CanaryStop('first GPU-only command')
        # LocalLinuxExecutionContext already source-binds helpers to this interpreter.
        helper = (len(request.argv) == 6 and request.argv[1:3] == ('-I', '-c')
                  and 'from astrid.core.generation.engine_runner_target import main;' in request.argv[3])
        if helper:
            request = replace(request, argv=(*request.argv[:3], instrument(request.argv[3]), *request.argv[4:]))
        result = run(self, request)
        if helper:
            remaining = []
            for line in result.stderr.splitlines(keepends=True):
                if line.startswith(PREFIX):
                    try:
                        item = json.loads(line[len(PREFIX):])
                        if (set(item) == {'module','function','line','type'} and isinstance(item['line'], int)
                                and item['module'].startswith('astrid.core.generation.')
                                and all(re.fullmatch('[A-Za-z_][A-Za-z_0-9]*', item[k]) for k in ('function','type'))):
                            emit('target-helper-site', status='observed', operation=original.argv[4], **item)
                    except (ValueError, TypeError, KeyError):
                        pass
                else:
                    remaining.append(line)
            result = replace(result, stderr=''.join(remaining))
        for kind, program in (('environment-probe', target._ENV_PROBE), ('wheel-record-receipt', target._RECEIPT_SCRIPT)):
            if program in original.argv:
                lines = [int(n) for n in re.findall(r'File "<string>", line ([0-9]+)', result.stderr or '')][-12:]
                emit(kind, status='passed' if result.exit_code == 0 else 'failed', exit_code=result.exit_code,
                     script_sha256=hashlib.sha256(program.encode()).hexdigest(), static_script_lines=lines)
        if result.exit_code:
            # Task-owned diagnostic export: only selected public recipe commands,
            # under the job's credential-free process environment.
            emit('finite-command', status='failed', **command_diagnostic(original, result, active))
        return result
    runner._Runner.execute = observed_execute
    engine_custody.LinuxEngineServiceClient.run = observed_run
    class CanaryStop(BaseException):
        pass
    stdout, stderr = io.StringIO(), io.StringIO()
    boundary = None
    started = time.monotonic()
    try:
        sys.settrace(trace)
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = engine_cli.main(args)
    except CanaryStop as exc:
        boundary = str(exc)
        code = 0 if boundary == 'first GPU-only command' else 78
    finally:
        sys.settrace(None)
        runner._Runner.execute = execute
        engine_custody.LinuxEngineServiceClient.run = run
    (out/'engine-cli.stdout').write_text(stdout.getvalue())
    (out/'engine-cli.stderr').write_text(stderr.getvalue())
    error = {}
    if code:
        try:
            error = json.loads(stderr.getvalue())['error']
        except (ValueError, KeyError, TypeError):
            pass
    receipt = Path(root) / runner._RECEIPT
    report = dict(status=('stopped-before-gpu' if boundary == 'first GPU-only command' else 'blocked') if boundary else ('failed' if code else 'installed'), boundary=boundary, exit_code=code, seconds=round(time.monotonic()-started,3),
                  code=error.get('code') if error.get('code') in runner._STATUS_ERROR_CODES else None,
                  step=error.get('step') if error.get('step') in allowed else None, failure_sites=sites,
                  receipt_exists=receipt.is_file())
    if receipt.is_file() and not receipt.is_symlink():
        raw = receipt.read_bytes()
        value = json.loads(raw)
        report.update(receipt_sha256=hashlib.sha256(raw).hexdigest(), receipt_status=value.get('status'),
                      receipt_error_code=value.get('error_code'),
                      steps=[{k:s.get(k) for k in ('id','kind','action','validation')} for s in value.get('steps',[])])
    failure = Path(root)/'.astrid-engine/command-failure.json'
    if failure.is_file() and not failure.is_symlink():
        raw = failure.read_bytes()
        assert len(raw) < 1048576, 'command failure log exceeds diagnostic export bound'
        (out/'command-failure.json').write_bytes(raw)
        value = json.loads(raw)
        report['command_failure_sha256'] = hashlib.sha256(raw).hexdigest()
        report['failed_argv'] = value['argv']
        report['bootstrap_index'] = active['bootstrap'].get(tuple(value['argv']))
        report['diagnostic_fix'] = 'inspect command-failure.json stderr and the selected bootstrap index; retain the source, uv, wheel and model checks'
    (out / 'engine-result.json').write_text(json.dumps(report, indent=2)+'\n')
    emit('canonical-engine', **report)
    return code


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except BaseException as exc:
        sites = []
        trace = exc.__traceback__
        while trace:
            sites.append(dict(file=Path(trace.tb_frame.f_code.co_filename).name,
                              function=trace.tb_frame.f_code.co_name, line=trace.tb_lineno))
            trace = trace.tb_next
        value = dict(schema='h91.capture-failure.v1', type=type(exc).__name__, sites=sites)
        (Path(sys.argv[5])/'capture-failure.json').write_text(json.dumps(value,indent=2)+'\n')
        raise SystemExit(1)
