"""Join the controller's shipped machine job to the real pod-side local runner."""
from __future__ import annotations

import contextlib
import json
import shlex
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from astrid.core.execution.machine_run import MachineJob
from astrid.core.generation import entries
from astrid.core.generation.backends.vibecomfy import VibeComfyBackend
from astrid.core.generation.job_inputs import write_archive
from astrid.core.generation.local_job import run_job
from astrid.core.model_catalog.registry import ModelRegistry
from astrid.packs.runpod import machine
from astrid.sdk.generation import _prepare_catalog_target_workflow
from tests.helpers.generation_defaults import default_entries
from tests.integrations.test_runpod_slot_path_cpu import SlotPod, _slot_classes
from tests.core.generation.test_engine_custody import session  # noqa: F401
from tests.integrations._pinned_source import pinned_comfy_source, pinned_managed_install  # noqa: F401

pytestmark = pytest.mark.integration


class FileTransport:
    """A pod filesystem under tmp_path; uploads retain their exact controller bytes."""
    def __init__(self):
        self.uploads = {}

    def _exec_ok(self, state, command, label):
        argv = shlex.split(command)
        assert argv[:2] == ['mkdir', '-p']
        for name in argv[2:]:
            Path(name).mkdir(parents=True, exist_ok=True)

    def _put_checked(self, state, source, destination):
        data = Path(source).read_bytes()
        target = Path(destination)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        self.uploads[destination] = data


class LeaseOwner:
    def __init__(self):
        self.lock = threading.RLock()
        self.record = {'lease': {}}

    def preparation_claim(self, lease):
        return self.record

    def _save(self, record, lease):
        self.record['lease'] = lease


@contextlib.contextmanager
def _owned_cpu_engine(provider, installed, monkeypatch):
    """Use the same pinned native install and owned HTTP peer as the matrix."""
    from tests.integrations._pinned_source import owned_cpu_peer, read_peer_events

    with owned_cpu_peer(provider, installed, monkeypatch) as records:
        try:
            yield
        finally:
            read_peer_events(provider, records)


def test_controller_shipped_default_job_runs_on_the_real_local_path(tmp_path, monkeypatch, pinned_managed_install):  # noqa: F811
    """Real shared preparation -> machine.start's uploads -> unmodified run_job.

    Claim/source-bootstrap/Runtime journeys have their own provider tests. This
    seam doubles transport and lease persistence, and the engine's HTTP peer.
    """
    from vibecomfy.workflow_bundle import emit_bundle

    slot, mode = ('image', 't2i'), 't2i'
    identifier = default_entries()[slot][0]
    registry = ModelRegistry.load_default()
    candidate = registry.get(identifier)
    values = {'prompt': 'A red cube on a white tabletop.'}
    workflow, plan, _, assets = _prepare_catalog_target_workflow(
        registry=registry, candidate=candidate, mode=mode, modality='image', inputs=values,
        target_client=None, backend=VibeComfyBackend(), defer_target=True)
    assert not assets
    folder = tmp_path / 'controller'
    folder.mkdir()
    emit_bundle(workflow, folder / 'workflow.py', {'operation': 'authored'})
    described = entries.find_entry(candidate.modes[mode].backends['local'].template)
    public, public_values = entries.resolve_entry_inputs(identifier, values, mode=mode)
    assert 'seed' in {port['name'] for port in public['inputs']}
    public_values['seed'] = plan['bindings']['seed']
    archive = write_archive(folder / 'entry-inputs.zip', public_values, public['inputs'])
    transport, owner = FileTransport(), LeaseOwner()
    started = []
    def job_start(command, *, job_id):
        started.append(command)
        return {'id': job_id, 'dir': str(tmp_path / 'pod-agent' / job_id)}
    adapter = SimpleNamespace(placement=transport, lease_owner=owner)
    state = SimpleNamespace(note=lambda *args, **kwargs: None)
    # cause 9e5a1963a; ruling: machines 11:35 Sat
    lease = machine.MachineLease(SimpleNamespace(claim=owner.record),
        SimpleNamespace(ops=SimpleNamespace(job_start=job_start)), state, {}, False)
    monkeypatch.setattr(machine, 'RUNS_ROOT', str(tmp_path / 'pod' / 'runs'))
    job = MachineJob(entry=identifier, mode=mode, policy={},
        inputs={'python': str(folder / 'workflow.py'), 'companion': str(folder / 'workflow.vibe.json'),
                'source': str(entries.bundle_path(described)), 'workflow_inputs': json.dumps(plan['bindings']),
                'entry_inputs': str(archive)},
        invocation_id='task:attempt', task_id='task', attempt_id='attempt',
        scratch=folder, progress_path=folder / 'progress.json', align='trust')
    remote = machine.RunPodMachine(adapter).start(lease, job)
    job_path = Path(remote.remote_root) / 'job.json'
    # Read the uploaded bytes without reconstructing or rewriting their file paths.
    assert job_path.read_bytes() == transport.uploads[str(job_path)]
    shipped = json.loads(job_path.read_bytes())
    assert shipped['inputs']['seed'] == shipped['bindings']['seed'] == plan['bindings']['seed']
    assert shipped['align'] == 'trust' and shipped['mode'] == mode
    assert set(shipped['execution']) >= {'python', 'companion', 'source'}
    assert 'workflow_inputs' not in shipped['execution']
    assert '--job ' + str(job_path) in started[0]
    assert owner.record['lease']['job']['state'] == 'running'

    provider = SlotPod(tmp_path / 'claim.json', tmp_path / 'attempts', _slot_classes(slot))
    with _owned_cpu_engine(provider, pinned_managed_install, monkeypatch):
        manifest = run_job(identifier, job_path, Path(remote.remote_root) / 'out', mode=mode)
    assert (Path(remote.remote_root) / 'out' / manifest['managed_generation_result_path']).is_file()
    assert provider.comfy.prompts and not provider.comfy.rejections
    assert provider.comfy.views, 'the real runner never retrieved an engine output'
    seeds = [node['inputs']['noise_seed'] for node in provider.comfy.prompts[-1]['prompt'].values()
             if node['class_type'] == 'RandomNoise']
    assert seeds == [shipped['bindings']['seed']]
