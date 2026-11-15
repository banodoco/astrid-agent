"""W-B dispatch-only bundle integrity (CPU only)."""
import hashlib
import io
import json
import subprocess
import sys
import tarfile

import pytest

from astrid.packs.runpod import machine as rm
from tests.packs.runpod.test_generation_preparation import retained as retained
from tests.packs.runpod.test_machine import machine_case as machine_case
from tests.packs.runpod.test_remote_placement import sources as sources
from tests.packs.runpod.test_target_selection import case as case
from tests.packs.runpod.test_warm_session import lease_selection_case as lease_selection_case
from tests.packs.runpod.test_warm_session import small_lease as small_lease


@pytest.mark.parametrize('fault', ['digest', 'manifest', 'content', 'size', 'path', 'absolute',
                                    'symlink', 'hardlink', 'duplicate', 'undeclared', 'destination',
                                    'mode', 'missing', 'fifo'])
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
        if fault == 'mode':
            member.mode = 0o755
        if fault == 'fifo':
            member.type, member.size = tarfile.FIFOTYPE, 0
        if fault != 'missing':
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
    offending = str(bundle) if fault == 'digest' else job_bundle.MANIFEST if fault == 'manifest' else str(destination) if fault == 'destination' else 'surprise' if fault == 'undeclared' else name
    assert offending in result.stdout
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
    assert json.loads(done.stdout)['status'] == job_bundle.MARK
    assert (destination / 'in/image.png').read_bytes() == b'image'
    assert (destination / 'job.json').read_text() == '{}'
    assert (destination / 'selection/packs/proof/pack.yaml').read_text() == 'pack_id: proof'
    assert (destination / 'selection/packs/proof/workflow.py').read_text() == '# workflow'
    assert (destination / 'selection/packs/proof/workflow.py').stat().st_mode & 0o777 == 0o755
    if hardlink:
        assert (destination / 'selection/packs/proof/alias.py').read_text() == '# workflow'
    assert (destination / 'selection/selection.json').is_file()


@pytest.mark.parametrize('name', ['', '.', '../escape', '/absolute', 'in//image', 'in/./image',
                                  'in/../image', 'in\\image', 'C:/image', 'in/imag\x00e'])
def test_builder_refuses_unsafe_member_with_its_path(tmp_path, name):
    from astrid.packs.runpod import job_bundle
    local = tmp_path / 'image'
    local.write_bytes(b'image')
    with pytest.raises(ValueError) as caught:
        job_bundle.build([(local, name)], tmp_path / 'unused', tmp_path / 'dispatch.tar', lambda: None)
    assert repr(name) in str(caught.value)
    assert not (tmp_path / 'dispatch.tar').exists()


def test_builder_names_every_missing_local_file(tmp_path):
    from astrid.packs.runpod import job_bundle
    missing = [tmp_path / 'missing-one', tmp_path / 'missing-two']
    with pytest.raises(ValueError) as caught:
        job_bundle.build([(path, 'in/' + path.name) for path in missing], tmp_path / 'unused',
                         tmp_path / 'dispatch.tar', lambda: None)
    assert all(str(path) in str(caught.value) for path in missing)
    assert not (tmp_path / 'dispatch.tar').exists()


@pytest.mark.parametrize('kind', ['symlink', 'directory', 'fifo'])
def test_builder_refuses_nonregular_local_file(tmp_path, kind):
    import os

    from astrid.packs.runpod import job_bundle
    local = tmp_path / 'source'
    if kind == 'symlink':
        local.symlink_to(tmp_path / 'absent')
    elif kind == 'directory':
        local.mkdir()
    else:
        os.mkfifo(local)
    with pytest.raises(ValueError, match=str(local)):
        job_bundle.build([(local, 'in/image')], tmp_path / 'unused', tmp_path / 'dispatch.tar', lambda: None)


@pytest.mark.parametrize('field', ['sha256', 'size', 'mode'])
def test_pod_verifier_names_every_mismatched_member_before_writing(tmp_path, field):
    from astrid.packs.runpod import job_bundle
    names = ['in/one', 'selection/two']
    files = {name: {'size': 5, 'mode': 0o644, 'sha256': hashlib.sha256(b'image').hexdigest()}
             for name in names}
    for entry in files.values():
        entry[field] = {'sha256': '0' * 64, 'size': 6, 'mode': 0o755}[field]
    body = json.dumps({'files': files}).encode()
    bundle = tmp_path / 'dispatch.tar'
    with tarfile.open(bundle, 'w') as archive:
        for name, data in [(job_bundle.MANIFEST, body), *((name, b'image') for name in names)]:
            member = tarfile.TarInfo(name)
            member.size, member.mode = len(data), 0o644
            archive.addfile(member, io.BytesIO(data))
    destination = tmp_path / 'destination'
    result = subprocess.run([sys.executable, '-c', job_bundle.UNPACK, str(bundle), str(destination),
                             rm.sha256_file(bundle), hashlib.sha256(body).hexdigest()],
                            capture_output=True, text=True)
    assert result.returncode != 0
    assert all(name in result.stdout for name in names)
    assert not destination.exists()


@pytest.mark.usefixtures("machine_case")
def test_dispatch_preserves_public_inputs_and_checkpoints_before_launch(request, monkeypatch):
    from dataclasses import replace
    from pathlib import Path

    from astrid.core.execution.machine_run import run_on_machine
    from astrid.core.generation.job_inputs import write_archive
    from tests.packs.runpod.test_machine import _claim_machine

    c = request.getfixturevalue("machine_case")
    _claim_machine(c)
    start, put = c.pod.job_start, c.pod.put
    roots, identities, puts = [], [], []
    request = None

    def put_(local, remote):
        if "/runs/" in remote:
            puts.append(remote)
        return put(local, remote)

    def start_(command, **kwargs):
        if ' generate ' in command and '--placement local' in command:
            identity = kwargs['job_id']
            assert c.read()['lease']['job'] == {'id': identity, 'state': 'starting'}
            job_path = next(path for path in c.pod.files if path.endswith('/job.json') and '/runs/' in path)
            body = json.loads(c.pod.files[job_path])
            assert body['task_identity'] == request.task_id
            assert body['attempt_identity'] == request.attempt_id
            assert body['idempotency_key'] == f'remote:{request.task_id}:{request.attempt_id}'
            assert body['generation_settings']['models']['trust_unchanged'] is not None
            assert c.pod.files[body['inputs']['image']] == b'input image'
            for name in ('python', 'companion', 'source'):
                assert c.pod.files[body['execution'][name]] == Path(request.inputs[name]).read_bytes()
            root = job_path.removesuffix('/job.json')
            assert root + '/selection/selection.json' in c.pod.files
            assert not any(path.endswith('/selection.tar.gz') for path in c.pod.files)
            roots.append(root)
            identities.append(identity)
        return start(command, **kwargs)

    monkeypatch.setattr(c.pod, 'put', put_)
    monkeypatch.setattr(c.pod, 'job_start', start_)
    for n in (1, 2):
        request = c.job(n)
        image = request.scratch / 'image.png'
        image.write_bytes(b'input image')
        envelope = write_archive(request.scratch / 'inputs.zip', {'image': str(image)},
                                 [{'name': 'image', 'type': 'file'}])
        request = replace(request, inputs={**request.inputs, 'entry_inputs': str(envelope)})
        run_on_machine(c.machine, request, destination=request.scratch / 'outputs',
                       cancelled=lambda: False, sleep=lambda _: None)
    assert len(set(roots)) == len(set(identities)) == 2
    assert all(len(root.rsplit('/', 1)[-1]) == 32 for root in roots)
    assert [Path(path).name for path in puts] == ['dispatch.tar', 'dispatch.tar']
    assert c.creates == 1 and c.deletes == []
