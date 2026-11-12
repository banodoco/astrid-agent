"""Offline acceptance of declared logs through failed remote and local jobs."""
import json
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from astrid.core.cli.domain_tasks import build_parser
from astrid.core.execution import generic_host
from astrid.core.execution.generic_host import GenericPackHost
from astrid.core.generation import cli
from astrid.core.generation.engine_custody import engine_log_path
from astrid.packs.runpod import failure_receipt, machine
from astrid.packs.runpod.remote_placement import MANIFEST_MARK, RESULT_MARK, manifest_command
from astrid.sdk.contracts import DomainResult
from tests.test_generic_host import FakeRuntime, _write_manifest


@pytest.mark.parametrize('exit_code', [0, 2], ids=['success', 'failure'])
def test_remote_logs_are_collectable_by_host_after_machine_is_gone(tmp_path, exit_code):
    """Both fetch paths leave the same host-readable collected log tree."""
    pod = tmp_path / 'pod'
    run_root = pod / 'runs/r1'
    out = run_root / 'out'
    job_dir = pod / 'jobs/j1'
    out.mkdir(parents=True)
    job_dir.mkdir(parents=True)
    (job_dir / 'stdout').write_text('remote stdout\n')
    (job_dir / 'stderr').write_text('remote stderr\n')
    (run_root / 'result.json').write_text('{}\n')
    (out / 'engine.log').write_text('engine output\n')
    from astrid.core._shared import run_logs
    run_logs.declare(out, run_logs.LogDecl('engine', 'engine', 'engine.log'))

    command = job_dir / 'command.sh'
    command.write_text('#!/bin/bash\n' + machine.finish_command(str(run_root), python=sys.executable) + '\n')
    with (job_dir / 'stdout').open('a') as stdout, (job_dir / 'stderr').open('a') as stderr:
        subprocess.run(['bash', str(command)], stdout=stdout, stderr=stderr,
                       check=True, timeout=60)
    manifest = subprocess.run(
        ['bash', '-c', manifest_command(str(out), sys.executable)],
        capture_output=True, text=True, check=True, timeout=60,
    )
    manifest_digest = next(line.split('=', 1)[1] for line in manifest.stdout.splitlines()
                           if line.startswith(MANIFEST_MARK))
    with (job_dir / 'stdout').open('a') as stdout:
        stdout.write(f'{RESULT_MARK}{{}}\n{MANIFEST_MARK}{manifest_digest}\n')

    class Ops:
        def exec(self, command, timeout):
            result = subprocess.run(['bash', '-c', command], capture_output=True,
                                    text=True, timeout=timeout, check=False)
            return result.returncode, result.stdout, result.stderr

        @staticmethod
        def get(remote, local):
            shutil.copyfile(remote, local)

        @staticmethod
        def job_poll(job_id):
            assert job_id == 'j1'
            return {'state': 'exited', 'exit_code': exit_code}

    scratch = tmp_path / 'scratch'
    scratch.mkdir()
    lease = machine.MachineLease(None, SimpleNamespace(ops=Ops()),
                                 SimpleNamespace(note=lambda *_args, **_kwargs: None), {}, False)
    run = machine.MachineRun(lease, 'j1', str(job_dir), str(run_root), scratch / 'progress', scratch)
    owner = object.__new__(machine.RunPodMachine)
    owner._require = lambda: SimpleNamespace(placement=SimpleNamespace(_check_deadline=lambda _stage: None))
    owner._checkpoint = lambda *args: None
    owner._checkpoint_installed = lambda *_args: None
    destination = tmp_path / 'download'
    if exit_code:
        with pytest.raises(machine.MachineError, match='remote generation failed'):
            owner.fetch(run, destination)
    else:
        owner.fetch(run, destination)

    # Fetch and host collection happen on different machines in production.
    # Prove every descriptor is backed by downloaded bytes, not the source pod.
    moved_pod = tmp_path / 'pod-collected'
    pod.rename(moved_pod)
    host = object.__new__(GenericPackHost)
    descriptors = host._log_outputs(destination)
    logs = {row['name']: Path(row['path']).read_text() for row in descriptors}
    assert logs == {
        'engine': 'engine output\n',
        'job.stdout': 'remote stdout\n',
        'job.stderr': 'remote stderr\n',
        'generation.stdout': '{}\n',
    }


def test_failed_generation_logs_survive_fetch_and_reset(tmp_path, monkeypatch, capsys):
    pod = tmp_path / 'pod'
    run_root = pod / 'runs/r1'
    out = run_root / 'out'
    job_dir = pod / 'jobs/j1'
    out.mkdir(parents=True)
    job_dir.mkdir(parents=True)
    engine = Path(engine_log_path(str(pod / 'engine'), 'comfyui'))
    engine.parent.mkdir(parents=True)
    engine.write_bytes(b'x' * (5000 - len(b'EARLIER JOB LINE\n')) + b'EARLIER JOB LINE\n')
    fake = tmp_path / 'generation.py'
    fake.write_text('''import json, sys
from pathlib import Path
from astrid.core._shared import run_logs
out, engine = map(Path, sys.argv[1:])
run_logs.declare(out, run_logs.LogDecl('engine', 'engine', str(engine), start=engine.stat().st_size))
child = out / '.vibecomfy-runtime/out/runs/run-test'
child.mkdir(parents=True)
(child / 'attempt.json').write_text('{"run": "test"}')
(child / 'logs.json').write_text(json.dumps({'schema': 'run-logs.v1', 'logs': [{'name': 'run', 'role': 'run', 'path': 'attempt.json'}], 'children': []}))
run_logs.declare_child(out, child / 'logs.json')
with engine.open('a') as stream:
    stream.write('!!! Exception during processing !!! index out of range in self\\nTraceback (most recent call last):\\n  File "nodes.py", line 12, in synth\\nIndexError: index out of range in self\\n')
(child / 'engine-error.json').write_text(json.dumps({'schema': 'engine-error.v1', 'error': {'exception_message': 'index out of range in self', 'node_type': 'TTSNode', 'traceback': ['  File "nodes.py", line 12, in synth', 'IndexError: index out of range in self']}}))
run_logs.declare(child, run_logs.LogDecl('engine-error', 'engine', 'engine-error.json'))
sys.stdout.write('{"ok": false}\\n')
sys.stderr.write('fake generation stderr\\n')
sys.exit(2)
''')
    command = job_dir / 'command.sh'
    command.write_text(f'{shlex.quote(sys.executable)} {shlex.quote(str(fake))} {shlex.quote(str(out))} '
                       f'{shlex.quote(str(engine))} > {shlex.quote(str(run_root / "result.json"))}\n'
                       f'rc=$?\n{machine.finish_command(str(run_root), python=sys.executable)}\nexit $rc\n')
    with (job_dir / 'stdout').open('w') as stdout, (job_dir / 'stderr').open('w') as stderr:
        completed = subprocess.run(['bash', str(command)], stdout=stdout, stderr=stderr, timeout=60, check=False)
    assert completed.returncode == 2

    class Ops:
        def exec(self, command, timeout):
            result = subprocess.run(['bash', '-c', command], capture_output=True, text=True, timeout=timeout, check=False)
            return result.returncode, result.stdout, result.stderr

        def get(self, remote, local):
            shutil.copyfile(remote, local)

        def job_poll(self, job_id):
            assert job_id == 'j1'
            return {'state': 'exited', 'exit_code': 2}

    scratch = tmp_path / 'scratch'
    scratch.mkdir()
    lease = machine.MachineLease(None, SimpleNamespace(ops=Ops()), SimpleNamespace(), {}, False)
    run = machine.MachineRun(lease, 'j1', str(job_dir), str(run_root), scratch / 'progress', scratch)
    owner = object.__new__(machine.RunPodMachine)
    owner._require = lambda: SimpleNamespace(placement=None)
    owner._checkpoint = lambda *args: None
    with pytest.raises(machine.MachineError, match='exit 2') as failed:
        owner.fetch(run, tmp_path / 'download')
    rows = {row['name']: row for row in lease.failure_logs['files']}
    assert Path(rows['generation.stdout']['path']).read_text() == '{"ok": false}\n'
    root = tmp_path / 'failures'
    receipt = failure_receipt.record_failure({'failure': failed.value, 'stage': 'fetch', 'lease': lease, 'run': run},
                                             job=SimpleNamespace(task_id='task-1'), machine={}, timing=None, root=root)
    assert receipt is not None
    monkeypatch.setattr(failure_receipt, 'failures_root', lambda: root)

    def check_cli():
        assert cli.main(['failure', '--logs']) == 0
        text = capsys.readouterr().out
        for expected in ('index out of range in self', 'TTSNode', '  File "nodes.py", line 12, in synth',
                         'IndexError: index out of range in self', '!!! Exception during processing !!!',
                         'fake generation stderr'):
            assert expected in text
        assert 'EARLIER JOB LINE' not in text
        assert cli.main(['failure', '--logs', '--name', 'engine-error']) == 0
        selected = capsys.readouterr().out
        error = json.loads(selected.split('\n', 1)[1])['error']
        assert error['node_type'] == 'TTSNode'
        assert error['exception_message'] == 'index out of range in self'
        assert error['traceback'] == ['  File "nodes.py", line 12, in synth',
                                      'IndexError: index out of range in self']
        assert '!!! Exception during processing !!!' not in selected and 'fake generation stderr' not in selected

    check_cli()
    engine_copy = out / 'logs/engine.log'
    data = engine_copy.read_bytes()
    engine_copy.write_bytes(bytes([data[0] ^ 1]) + data[1:])
    with pytest.raises(machine.MachineError, match='exit 2') as tampered:
        owner.fetch(run, tmp_path / 'tampered')
    bad_receipt = failure_receipt.record_failure({'failure': tampered.value, 'stage': 'fetch', 'lease': lease, 'run': run},
                                                 job=SimpleNamespace(task_id='task-2'), machine={}, timing=None,
                                                 root=tmp_path / 'bad-failures')
    assert json.loads(bad_receipt.read_text())['logs_unavailable'] == 'DigestMismatch'
    shutil.rmtree(pod)
    check_cli()

    manifest = _write_manifest(tmp_path / 'pack')
    definition = json.loads(manifest.read_text())
    script = '''import json, sys
from pathlib import Path
out = Path('{out}')
(out / 'attempt.json').write_text('{"run":"local"}')
from astrid.core._shared.run_logs import LogDecl, declare
declare(out, LogDecl('run', 'run', 'attempt.json'))
sys.stdout.write('local stdout\\n')
sys.stderr.write('local stderr\\n')
sys.exit(2)
'''
    definition['command']['argv'] = ['{python_exec}', '-c', script]
    manifest.write_text(json.dumps(definition))
    runtime = FakeRuntime()
    monkeypatch.setattr(generic_host, '_network_sandbox_argv', lambda argv, *_args: argv)
    host = GenericPackHost(pack_roots=[tmp_path], client=runtime, attempt_root=tmp_path / 'attempt')
    host.discover()
    task = {'task': {'id': 'task-1', 'capability': 'test.echo', 'project_id': 'demo',
                     'attempt_id': 'attempt-1', 'fence': 1, 'spec': {'spec': {'inputs': {}}}}}
    runtime.tasks['task-1'] = task
    try:
        with pytest.raises(Exception):
            host.run_task(task, lease_token='lease-1')
    finally:
        host.shutdown()
    failure_result = runtime.failures[0][3]

    class Tasks:
        """Answers as the real Runtime does for a failed task."""

        def list(self, project_id, **kwargs):
            return DomainResult.success([{'task_id': 'task-1'}])

        def show(self, task_id):
            assert task_id == 'task-1'
            return DomainResult.success({'task_id': 'task-1', 'state': 'failed', 'result': failure_result})

        def list_managed_outputs(self, task_id):
            return DomainResult.success([])  # a failed task commits no managed outputs

    parser = build_parser(SimpleNamespace(tasks=Tasks()))
    args = parser.parse_args(['logs', '--project', 'demo', 'task-1'])
    assert args.handler(args) == 0
    listing = capsys.readouterr().out
    for name in ('job.stdout', 'job.stderr', 'run'):
        assert name in listing
