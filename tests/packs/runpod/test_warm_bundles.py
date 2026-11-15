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
