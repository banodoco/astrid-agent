"""Join the controller's shipped machine job to the real pod-side local runner."""
from __future__ import annotations

import contextlib
import inspect
import json
import subprocess
import shlex
import shutil
from dataclasses import replace
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
from tests.integrations.test_dry_run_equals_live_run import (  # noqa: F401
    _pinned_comfy_source, _pinned_owned_cpu_peer, session,
)
from tests.integrations.test_managed_host_preparation import managed_install
from tests.integrations.test_runpod_slot_path_cpu import SlotPod, _attached_comfy, _slot_classes

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


@pytest.fixture
def _job_seam_install(session, monkeypatch, tmp_path, _pinned_comfy_source):
    """Reuse the verified native CPU installation with both real source pins.

    The shared fixture supplies an isolated environment, its hash-bound lock,
    real uv pip check and owned service. Its default tiny VibeComfy Git witness
    is replaced here by the product-pinned checkout used by the shipped job.
    GPU dependencies and inference remain outside this CPU seam's claim.
    """
    import vibecomfy
    from astrid.core.generation.vibecomfy_dependency import VIBECOMFY_ENGINE_REVISION
    from astrid.core.pack.discovery import pack_discovery_scope

    _, _, recipe, root, _, _ = session
    checkout = root / 'runtime' / 'vibecomfy'
    shutil.rmtree(checkout)
    subprocess.run(['git', 'clone', '--quiet', '--shared',
                    str(Path(vibecomfy.__file__).resolve().parents[1]), str(checkout)], check=True)
    revision = VIBECOMFY_ENGINE_REVISION
    subprocess.run(['git', 'checkout', '--quiet', '--detach', revision], cwd=checkout, check=True)
    assert subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=checkout, text=True).strip() == revision
    # The shared fixture sets each source's canonical origin itself.
    subprocess.run(['git', 'remote', 'remove', 'origin'], cwd=checkout, check=True)
    recipe = replace(recipe, sources=tuple(
        replace(source, pin=replace(source.pin, revision=revision))
        if source.name == 'vibecomfy' else source for source in recipe.sources))
    pinned_session = (*session[:2], recipe, *session[3:])
    profile = SimpleNamespace(comfy_source=_pinned_comfy_source,
        extra_launch_flags=('--use-ck-attention', '--disable-comfy-compiler'),
        standin_distributions={'torch': '2.10.0+cu130', 'comfy-kitchen': '0.2.35', 'comfy-aimdo': '0.5.5'})
    installation = managed_install.__wrapped__(pinned_session, monkeypatch, profile, tmp_path)
    installed = next(installation)
    try:
        # Reuse admitted pack metadata while constructing and replaying one job.
        # The product scope fingerprints the complete trees on every lookup;
        # changed inputs still require admission and all engine checks still run.
        with pack_discovery_scope():
            yield installed
    finally:
        installation.close()


@contextlib.contextmanager
def _owned_cpu_engine(provider, installed, monkeypatch):
    """Run the validating HTTP peer through the verified installation's owner."""
    records = None
    try:
        with _pinned_owned_cpu_peer(provider, installed, monkeypatch) as records:
            # Importing the matrix module in the cold child runs pack discovery
            # before it can listen. Embed its identical HTTP handler so this CPU
            # peer meets the fixture's unchanged four-second readiness budget.
            program = installed[3] / 'tiny_http.py'
            source = program.read_text()
            handler_import = 'from tests.integrations.test_runpod_slot_path_cpu import _attached_comfy'
            assert source.count(handler_import) == 1
            program.write_text(source.replace(handler_import,
                'import contextlib,re\n' + inspect.getsource(_attached_comfy)))
            yield
    finally:
        if records is not None:
            for name in ('prompts', 'views', 'rejections'):
                path = records / name
                setattr(provider.comfy, name,
                        [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else [])


def test_controller_shipped_default_job_runs_on_the_real_local_path(tmp_path, monkeypatch, _job_seam_install):
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
    lease = machine.MachineLease(object(), SimpleNamespace(ops=SimpleNamespace(job_start=job_start)), state, {}, False)
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
    with _owned_cpu_engine(provider, _job_seam_install, monkeypatch):
        manifest = run_job(identifier, job_path, Path(remote.remote_root) / 'out', mode=mode)
    assert (Path(remote.remote_root) / 'out' / manifest['managed_generation_result_path']).is_file()
    assert provider.comfy.prompts and not provider.comfy.rejections
    assert provider.comfy.views, 'the real runner never retrieved an engine output'
    seeds = [node['inputs']['noise_seed'] for node in provider.comfy.prompts[-1]['prompt'].values()
             if node['class_type'] == 'RandomNoise']
    assert seeds == [shipped['bindings']['seed']]
