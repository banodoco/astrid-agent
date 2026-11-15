"""W-B: one verified dispatch transfer and one complete result transfer (CPU only)."""
import hashlib
import io
import json
import shlex
import subprocess
import sys
import tarfile
from collections import Counter
from dataclasses import replace
from pathlib import Path

import pytest

from astrid.core.execution.machine_run import run_on_machine
from astrid.packs.runpod import machine as rm
from tests.packs.runpod.test_generation_preparation import retained as retained
from tests.packs.runpod.test_machine import _claim_machine
from tests.packs.runpod.test_machine import machine_case as machine_case
from tests.packs.runpod.test_remote_placement import sources as sources
from tests.packs.runpod.test_target_selection import case as case
from tests.packs.runpod.test_warm_session import lease_selection_case as lease_selection_case
from tests.packs.runpod.test_warm_session import small_lease as small_lease


@pytest.mark.usefixtures('machine_case')
def test_warm_dispatch_and_collection_have_no_single_file_transfers(request, monkeypatch):
    c = request.getfixturevalue('machine_case')
    _claim_machine(c)
    first = c.job()
    run_on_machine(c.machine, first, destination=first.scratch / 'outputs', cancelled=lambda: False, sleep=lambda _: None)
    counts, calls = Counter(), []
    for name in ('exec', 'put', 'get', 'job_start', 'job_poll', 'job_cancel'):
        original = getattr(c.pod, name)

        def counted(*args, _name=name, _original=original, **kwargs):
            counts[_name] += 1
            calls.append((_name, args[1] if _name == 'put' else args[0]))
            if _name == 'job_start' and '--placement local' in args[0]:
                job_path = next(path for path in c.pod.files if path.endswith('/job.json') and '/runs/' in path)
                body = json.loads(c.pod.files[job_path])
                assert c.pod.files[body['inputs']['image']] == b'input image'
                for name in ('python', 'companion', 'source'):
                    assert c.pod.files[body['execution'][name]] == Path(request.inputs[name]).read_bytes()
                assert job_path.removesuffix('job.json') + 'selection/selection.json' in c.pod.files
            return _original(*args, **kwargs)

        monkeypatch.setattr(c.pod, name, counted)
    request = c.job(2)
    # Public input archives must join the same dispatch as workflow execution files.
    from astrid.core.generation.job_inputs import write_archive
    image = request.scratch / 'input.png'
    image.write_bytes(b'input image')
    envelope = write_archive(request.scratch / 'inputs.zip', {'image': str(image)},
                             [{'name': 'image', 'type': 'file'}])
    request = replace(request, inputs={**request.inputs, 'entry_inputs': str(envelope)})
    result = run_on_machine(c.machine, request, destination=request.scratch / 'outputs', cancelled=lambda: False, sleep=lambda _: None)
    assert [path.rsplit('/', 1)[-1] for name, path in calls if name == 'put'] == ['dispatch.tar']
    gets = [path for name, path in calls if name == 'get']
    assert [path.rsplit('/', 1)[-1] for path in gets if '/runs/' in path] == ['outputs.tar']
    # W-A owns the claim's canonical installation observation. Keep that
    # integrity read in the whole-job budget while forbidding file transfers
    # throughout W-B dispatch, progress-at-completion and result collection.
    assert [path for path in gets if '/runs/' not in path] == [
        c.installation.root + '/.astrid-engine/install.json']
    assert not any(name == 'exec' and 'sha256sum ' in command for name, command in calls)
    assert sum(name == 'exec' and 'DISPATCH_VERIFIED' in command for name, command in calls) == 1
    assert sum(counts.values()) <= 20, dict(counts)
    assert counts['job_start'] == counts['job_poll'] == 2
    assert Path(result['files']['artifacts/cpu.png']).read_bytes() == b'png-bytes'
    assert json.loads(request.progress_path.read_text()) == {'phase': 'fetch'}
    assert c.creates == 1 and c.deletes == []


def test_finish_including_manifest_launches_exactly_one_process(tmp_path):
    root, job = tmp_path / 'run', tmp_path / 'job'
    root.mkdir()
    job.mkdir()
    (job / 'stdout').write_text('output')
    (job / 'stderr').write_text('error')
    counter = tmp_path / 'launches'
    interpreter = tmp_path / 'python'
    interpreter.write_text('#!/bin/sh\necho launch >> ' + shlex.quote(str(counter)) + '\nexec '
                           + shlex.quote(sys.executable) + ' "$@"\n')
    interpreter.chmod(0o755)
    done = subprocess.run(['bash', '-c', rm.finish_command(str(root), python=str(interpreter)),
                           str(job / 'command.sh')], capture_output=True, text=True, check=True)
    assert counter.read_text().splitlines() == ['launch']
    assert rm.LOGS_MANIFEST_MARK in done.stdout
    assert rm.MANIFEST_MARK in done.stdout
    assert rm.OUTPUT_ARCHIVE_MARK in done.stdout
    assert (root / 'out/logs/wrapper-manifest.json').is_file()
    manifest = json.loads((root / 'out' / rm.MANIFEST_NAME).read_text())
    size = rm._last_marked(done.stdout, rm.OUTPUT_BYTES_MARK).removeprefix(rm.OUTPUT_BYTES_MARK)
    assert int(size) == sum(entry['size'] for entry in manifest['files'].values())


@pytest.mark.parametrize('fault', ['digest', 'manifest', 'content', 'size', 'path', 'absolute',
                                    'symlink', 'hardlink', 'duplicate', 'undeclared', 'destination'])
def test_dispatch_refuses_tampered_or_unsafe_members_before_writing(tmp_path, fault):
    from astrid.packs.runpod import job_bundle

    name = '../escape' if fault == 'path' else '/absolute' if fault == 'absolute' else 'in/image.png'
    body = json.dumps({'files': {name: {'size': 5, 'mode': 0o644,
                                      'sha256': hashlib.sha256(b'image').hexdigest()}}}).encode()
    bundle = tmp_path / 'dispatch.tar'
    with tarfile.open(bundle, 'w') as archive:
        manifest = tarfile.TarInfo(job_bundle.MANIFEST)
        manifest.size = len(body)
        archive.addfile(manifest, io.BytesIO(body))
        member = tarfile.TarInfo(name)
        data = b'other' if fault == 'content' else b'imageX' if fault == 'size' else b'image'
        member.size = len(data)
        if fault in {'symlink', 'hardlink'}:
            member.type = tarfile.SYMTYPE if fault == 'symlink' else tarfile.LNKTYPE
            member.linkname, member.size = 'elsewhere', 0
        archive.addfile(member, io.BytesIO(data))
        if fault == 'duplicate':
            archive.addfile(member, io.BytesIO(data))
        if fault == 'undeclared':
            archive.addfile(tarfile.TarInfo('surprise'), io.BytesIO())
    destination = tmp_path / 'destination'
    if fault == 'destination':
        destination.symlink_to(tmp_path, target_is_directory=True)
    result = subprocess.run([sys.executable, '-c', job_bundle.UNPACK, str(bundle), str(destination),
                             '0' * 64 if fault == 'digest' else rm.sha256_file(bundle),
                             '0' * 64 if fault == 'manifest' else hashlib.sha256(body).hexdigest()],
                            capture_output=True, text=True)
    assert result.returncode != 0
    assert job_bundle.MARK not in result.stdout
    assert not (tmp_path / 'escape').exists()
    assert not (destination / 'in/image.png').exists()


@pytest.mark.parametrize('hardlink', [False, True])
def test_dispatch_builder_preserves_every_execution_input_and_selection_member(tmp_path, hardlink):
    from astrid.packs.runpod import job_bundle
    staging = tmp_path / 'staging'
    staging.mkdir()
    local = staging / 'image.png'
    local.write_bytes(b'image')
    job = staging / 'job.json'
    job.write_text('{}')
    pack = staging / 'pack'
    pack.mkdir()
    (pack / 'pack.yaml').write_text('pack_id: proof')
    (pack / 'workflow.py').write_text('# workflow')
    (pack / 'workflow.py').chmod(0o755)
    if hardlink:
        (pack / 'alias.py').hardlink_to(pack / 'workflow.py')
    source = type('Source', (), {'pack_id': 'proof', 'revision': 'a' * 40, 'repository': 'test',
                                 'tree_sha256': 'b' * 64, 'manifest_sha256': 'c' * 64, 'pack_root': pack})()
    selection, _ = rm.build_selection(None, staging, sources=[source])
    bundle = staging / job_bundle.NAME
    digest = job_bundle.build([(local, 'in/image.png'), (job, 'job.json')], selection, bundle, lambda: None)
    destination = tmp_path / 'landed'
    done = subprocess.run([sys.executable, '-c', job_bundle.UNPACK, str(bundle), str(destination),
                           rm.sha256_file(bundle), digest], capture_output=True, text=True, check=True)
    assert done.stdout.strip() == job_bundle.MARK
    assert (destination / 'in/image.png').read_bytes() == b'image'
    assert (destination / 'job.json').read_text() == '{}'
    assert (destination / 'selection/packs/proof/pack.yaml').read_text() == 'pack_id: proof'
    assert (destination / 'selection/packs/proof/workflow.py').read_text() == '# workflow'
    assert (destination / 'selection/packs/proof/workflow.py').stat().st_mode & 0o777 == 0o755
    if hardlink:
        assert (destination / 'selection/packs/proof/alias.py').read_text() == '# workflow'
    assert (destination / 'selection/selection.json').is_file()


@pytest.mark.parametrize('far', [False, True])
def test_failed_job_collects_all_declared_logs_with_one_verified_download(tmp_path, far):
    from types import SimpleNamespace

    from astrid.core._shared import run_logs

    root, job = tmp_path / 'run', tmp_path / 'job'
    root.mkdir()
    job.mkdir()
    (job / 'stdout').write_text('output')
    (job / 'stderr').write_text('error')
    (root / 'result.json').write_text('{}')
    (root / 'record.json').write_text('{"run":1}')
    run_logs.declare(root / 'out', run_logs.LogDecl('run', 'run', str(root / 'record.json')))
    done = subprocess.run(['bash', '-c', rm.finish_command(str(root), python=sys.executable),
                           str(job / 'command.sh')], capture_output=True, text=True, check=True)
    calls = []

    def get(remote, local):
        calls.append(remote)
        Path(local).write_bytes(Path(remote).read_bytes())

    scratch = tmp_path / 'scratch'
    scratch.mkdir()
    owner = object.__new__(rm.RunPodMachine)
    run = SimpleNamespace(remote_root=str(root), scratch=scratch, directory=str(job),
                          lease=SimpleNamespace(timing=None))
    (job / 'stdout').write_text(done.stdout + '\n' + 'x' * 10000)
    reads = []

    def execute(command, timeout):
        reads.append(command)
        result = subprocess.run(['bash', '-c', command], capture_output=True, text=True, timeout=timeout)
        return result.returncode, result.stdout, result.stderr

    logs = owner._fetch_failure_logs(SimpleNamespace(get=get, exec=execute), run,
                                     '' if far else done.stdout, tmp_path / 'local')
    assert {row['name'] for row in logs['files']} == {'run', 'job.stdout', 'job.stderr', 'generation.stdout'}
    assert calls == [str(root / 'logs.tar')]
    assert len(reads) == int(far)
    assert all(Path(row['path']).is_file() for row in logs['files'])


def _faulting_finish(tmp_path, fault, *, persistent=False):
    root, job = tmp_path / 'run', tmp_path / 'job'
    out = root / 'out'
    out.mkdir(parents=True)
    job.mkdir()
    (out / 'image.png').write_bytes(b'image')
    (job / 'stdout').write_text('output')
    (job / 'stderr').write_text('error')
    counter = tmp_path / 'launches'
    interpreter = tmp_path / 'python'
    interpreter.write_text(f'''#!{sys.executable}
import builtins, errno, os, pathlib, sys, tarfile
with open({str(counter)!r}, 'a') as stream:
    stream.write('launch\\n')
original = tarfile.open
original_open = builtins.open
original_os_open = os.open
failed = False
def checked_open(file, mode='r', *args, **kwargs):
    if {persistent!r} and failed and any(flag in mode for flag in 'wax'):
        raise OSError(errno.ENOSPC, 'persistent disk full')
    return original_open(file, mode, *args, **kwargs)
builtins.open = checked_open
def checked_os_open(path, *args, **kwargs):
    if {fault!r} in ('vanished-log', 'unreadable-log') and str(path).endswith('job.stdout.log'):
        if {fault!r} == 'vanished-log':
            pathlib.Path(path).unlink(missing_ok=True)
        else:
            raise PermissionError('log became unreadable')
    return original_os_open(path, *args, **kwargs)
os.open = checked_os_open
def checked(name, *args, **kwargs):
    global failed
    if pathlib.Path(name).name == {fault!r}:
        failed = True
        # Log sealing must happen after a complete, authenticated output bundle.
        if {fault!r} == 'logs.tar':
            with original({str(root / 'outputs.tar')!r}, 'r:') as archive:
                assert archive.extractfile('image.png').read() == b'image'
        raise OSError(errno.ENOSPC, 'sealing failed ' + 'x' * 100000)
    return original(name, *args, **kwargs)
tarfile.open = checked
program = sys.argv[2]
sys.argv = sys.argv[2:]
exec(compile(program, '<finish>', 'exec'))
''')
    interpreter.chmod(0o755)
    done = subprocess.run(['bash', '-c', rm.finish_command(str(root), python=str(interpreter)),
                           str(job / 'command.sh')], capture_output=True, text=True, timeout=30)
    assert counter.read_text().splitlines() == ['launch']
    return done, root


def test_log_seal_failure_delivers_verified_outputs_in_one_process_and_download(tmp_path):
    from types import SimpleNamespace

    done, root = _faulting_finish(tmp_path, 'logs.tar')
    assert done.returncode == 0
    assert 0 < len(done.stderr) <= 65536
    assert rm.LOGS_MANIFEST_MARK not in done.stdout
    manifest = json.loads((root / 'out' / rm.MANIFEST_NAME).read_text())
    assert rm.LOGS_UNAVAILABLE_MARK + 'OSError' in done.stdout
    with tarfile.open(root / 'outputs.tar') as archive:
        assert json.load(archive.extractfile(rm.MANIFEST_NAME)) == manifest
    digest = rm._last_marked(done.stdout, rm.MANIFEST_MARK).removeprefix(rm.MANIFEST_MARK)
    calls = []

    def get(remote, local):
        calls.append(remote)
        Path(local).write_bytes(Path(remote).read_bytes())

    scratch = tmp_path / 'scratch'
    scratch.mkdir()
    files = rm._fetch_output_archive(SimpleNamespace(get=get), str(root / 'outputs.tar'),
                                     tmp_path / 'landed', scratch, digest)
    assert Path(files['image.png']).read_bytes() == b'image'
    assert calls == [str(root / 'outputs.tar')]


def test_output_seal_failure_propagates_nonzero_and_bounded_stderr(tmp_path):
    done, _ = _faulting_finish(tmp_path, 'outputs.tar')
    assert done.returncode != 0
    assert 0 < len(done.stderr) <= 65536
    assert rm.OUTPUT_ARCHIVE_MARK not in done.stdout


def test_generation_propagates_finish_exit_before_reporting_success(tmp_path):
    command = rm.generation_command('cpu-image', str(tmp_path / 'run'), mode='t2i')
    # Exercise the real post-generation shell, with no generation/provider call.
    settlement = command[command.index('rc=$?\n'):]
    done = subprocess.run(['bash', '-c', 'finish_logs() { return 23; }; true\n' + settlement],
                           capture_output=True, text=True, timeout=10)
    assert done.returncode == 23
    assert rm.RESULT_MARK not in done.stdout


def test_log_archive_failure_alone_keeps_verified_job_warm(machine_case, tmp_path, monkeypatch):
    done, root = _faulting_finish(tmp_path, 'logs.tar')
    assert done.returncode == 0
    c = machine_case
    _claim_machine(c)
    first = c.job()
    run_on_machine(c.machine, first, destination=first.scratch / 'outputs',
                   cancelled=lambda: False, sleep=lambda _: None)
    c.host.wait_for_reset()
    assert c.read()['state'] == 'claimed', c.read()
    assert c.read()['lease']['state'] == 'idle', c.read()['lease']
    assert c.host.started and not c.host.closed, (c.host.started, c.host.closed)
    generate, download = c.pod._generate, c.pod.get
    calls = Counter()

    def generated(command):
        generate(command)
        return rm.RESULT_MARK + '{"status":"succeeded"}\n' + done.stdout

    def get(remote, local):
        calls['get'] += 1
        if remote.endswith('/outputs.tar'):
            Path(local).write_bytes((root / 'outputs.tar').read_bytes())
        else:
            download(remote, local)

    monkeypatch.setattr(c.pod, '_generate', generated)
    monkeypatch.setattr(c.pod, 'get', get)
    second = c.job(2)
    result = run_on_machine(c.machine, second, destination=second.scratch / 'outputs',
                            cancelled=lambda: False, sleep=lambda _: None)
    c.host.wait_for_reset()
    assert Path(result['files']['image.png']).read_bytes() == b'image'
    assert calls['get'] == 2  # canonical installation receipt plus one output bundle
    assert c.read()['lease']['state'] == 'idle'
    assert c.read()['lease']['reset']['state'] == 'ready'
    assert c.creates == 1 and c.deletes == []


@pytest.mark.parametrize('fault', ['vanished-log', 'unreadable-log'])
def test_log_lost_between_hash_and_archive_does_not_fail_outputs(tmp_path, fault):
    done, root = _faulting_finish(tmp_path, fault)
    assert done.returncode == 0, done.stderr
    assert rm.OUTPUT_ARCHIVE_MARK in done.stdout
    manifest = json.loads((root / 'out' / rm.MANIFEST_NAME).read_text())
    assert manifest['logs_unavailable'] in {'FileNotFoundError', 'PermissionError'}
    with tarfile.open(root / 'outputs.tar') as archive:
        assert archive.extractfile('image.png').read() == b'image'
        assert 'logs/job.stdout.log' not in archive.getnames()
        assert json.load(archive.extractfile(rm.MANIFEST_NAME)) == manifest


def test_persistent_enospc_after_first_output_seal_preserves_bundle(tmp_path):
    done, root = _faulting_finish(tmp_path, 'logs.tar', persistent=True)
    assert done.returncode == 0, done.stderr
    assert rm.OUTPUT_ARCHIVE_MARK in done.stdout
    digest = rm._last_marked(done.stdout, rm.MANIFEST_MARK).removeprefix(rm.MANIFEST_MARK)
    with tarfile.open(root / 'outputs.tar') as archive:
        body = archive.extractfile(rm.MANIFEST_NAME).read()
        assert hashlib.sha256(body).hexdigest() == digest
        assert archive.extractfile('image.png').read() == b'image'
    assert (root / 'out' / rm.MANIFEST_NAME).read_bytes() == body
