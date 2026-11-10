"""Managed custody joins the real Runtime/host, supervisor and sandboxed HTTP action."""
from __future__ import annotations

import contextlib
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time

import pytest

from astrid.core.execution.generic_host import GenericPackHost
from astrid.core.generation import entries
from astrid.core.generation.backends.vibecomfy import VibeComfyBackend
from astrid.core.generation.engine_contracts import CommandRequest
from astrid.core.generation.managed_preparation import ManagedEnginePreparation
from astrid.core.model_catalog.registry import ModelRegistry
from astrid.sdk.generation import _prepare_catalog_target_workflow
from tests.helpers.generation_defaults import default_entries
from tests.integrations import test_runpod_slot_path_cpu as matrix
from tests.integrations.test_remote_machine_job_seam_cpu import _owned_cpu_engine
from tests.integrations.test_runpod_host_endpoint_r5 import tuple_sources  # noqa: F401

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("fail_align", [False, True], ids=["repairs-before-worker", "align-fails-no-worker"])
def test_managed_host_prepares_before_sandboxed_worker(tmp_path, monkeypatch, real_runtime, tuple_sources, fail_align):
    from vibecomfy.workflow_bundle import emit_bundle
    events = []
    roots = []
    payload = b"CPU required artifact\n"
    artifact = "cpu-required.bin"

    @contextlib.contextmanager
    def installation(provider, folder, patch):
        with _owned_cpu_engine(provider, folder, patch):
            root = folder / 'managed/engines/comfyui'
            roots.append(root)
            assert not (root / 'runtime/out/sessions/comfyui/launch.json').exists()
            yield
    monkeypatch.setattr(matrix, '_managed_local_engine', installation)
    original_prepare = ManagedEnginePreparation.prepare
    def prepare(self, *args, **kwargs):
        events.append('prepare')
        report = original_prepare(self, *args, **kwargs)
        events.append('ready')
        assert (Path(self.root) / 'models' / artifact).read_bytes() == payload
        assert report['placement'] == 'managed' and report['health']['healthy'] is True
        return report
    monkeypatch.setattr(ManagedEnginePreparation, 'prepare', prepare)

    engine = entries.engine_table()['comfyui']
    original_align = type(engine).align
    def align(self, manifest, mode, **kwargs):
        events.append('align')
        assert mode == 'align'
        assert any(row['name'] == artifact for row in manifest['models'])
        if fail_align:
            raise ValueError('forced managed align failure')
        return original_align(self, manifest, mode, **kwargs)
    monkeypatch.setattr(type(engine), 'align', align)

    # CPU command stand-ins preserve the native supervisor command custody.
    # No real weights or dependency downloads occur; only the tiny artifact is
    # provisioned. The retained VibeComfy source still uses the recipe pin.
    class Commands:
        def doctor(self): pass
        def prepare_deps_sync(self): pass
        def nodes_restore(self, rows): pass
        def fetch(self, rows):
            from astrid.core.generation.engine_execution import create_local_backend
            root = roots[-1]
            context, _ = create_local_backend(engine='comfyui', platform='linux-x86_64-cu130', placement='local',
                root=str(root), deadline=time.monotonic() + 30)
            code = f"from pathlib import Path;Path({str(root / 'models' / artifact)!r}).write_bytes({payload!r})"
            result = context.run(CommandRequest((sys.executable, '-I', '-c', code), str(root), {}, time.monotonic() + 30))
            assert result.exit_code == 0
            events.append('fetched')
    monkeypatch.setattr(engine, 'commands', Commands())
    run_command = GenericPackHost._run_command_definition
    def launch(host, record, inputs, output_root, attempt, **kwargs):
        if record.id == 'vibecomfy.run':
            events.append('worker')
            assert events.index('ready') < events.index('worker')
            assert inputs['comfy_url'].startswith('http://127.0.0.1:')
        return run_command(host, record, inputs, output_root, attempt, **kwargs)
    monkeypatch.setattr(GenericPackHost, '_run_command_definition', launch)
    child_environment = GenericPackHost._child_environment
    def environment(host, record, attempt, **kwargs):
        result = child_environment(host, record, attempt, **kwargs)
        if record.id == 'vibecomfy.run' and kwargs.get('managed_preparation'):
            env, _ = result
            socket_path = roots[-1] / '.astrid-engine/session/control.sock'
            probe = "import socket,sys\ns=socket.socket(socket.AF_UNIX)\ntry:s.connect(sys.argv[1])\nexcept (PermissionError,RuntimeError):print('denied')\nelse:raise SystemExit('supervisor socket was accessible')"
            checked = subprocess.run([sys.executable, '-c', probe, str(socket_path)], env=env, cwd=attempt,
                                     capture_output=True, text=True, timeout=15)
            assert checked.returncode == 0 and checked.stdout.strip() == 'denied', checked.stderr
            events.append('supervisor-denied')
        return result
    monkeypatch.setattr(GenericPackHost, '_child_environment', environment)

    def invoke(facade, controls, folder):
        identifier = default_entries()[('image', 't2i')][0]
        registry = ModelRegistry.load_default()
        candidate = registry.get(identifier)
        workflow, plan, _, assets = _prepare_catalog_target_workflow(registry=registry, candidate=candidate, mode='t2i',
            modality='image', inputs={'prompt': 'CPU custody witness'}, target_client=None,
            backend=VibeComfyBackend(), defer_target=True)
        assert not assets
        workflow.metadata.setdefault('model_assets', []).append({'name': artifact, 'subdir': '',
            'url': 'https://example.invalid/cpu-required.bin', 'sha256': hashlib.sha256(payload).hexdigest(),
            'size_bytes': len(payload)})
        bundle = folder / 'canonical'
        bundle.mkdir()
        emit_bundle(workflow, bundle / 'workflow.py', {'operation': 'authored'})
        described = entries.find_entry(candidate.modes['t2i'].backends['local'].template)
        return controls['client'].invoke_result('vibecomfy.run', kind='action', project=controls['project'],
            inputs={'python': str(bundle / 'workflow.py'), 'companion': str(bundle / 'workflow.vibe.json'),
                    'source': str(entries.bundle_path(described)), 'workflow_inputs': json.dumps(plan['bindings'])},
            generation_intent={'version': 1, 'modality': 'image', 'partial_success_policy': 'reject',
                'groups': [{'group_key': 'main', 'selectors': [{'selector': 'main-0', 'ordinal': 0,
                    'variant_key': 'original', 'required': True}]}], 'metadata': {'model': identifier, 'mode': 't2i', 'align': 'align'}},
            wait=True, timeout_seconds=180, poll_seconds=0.05)
    with matrix._drive(('image', 't2i'), tmp_path, monkeypatch, real_runtime, tuple_sources,
                       target_kind='managed', call=invoke) as (_, _, provider, result):
        if fail_align:
            assert not result.ok
            assert 'worker' not in events and 'ready' not in events
            assert not (roots[-1] / 'peer-records/prompts').exists()
        else:
            assert result.ok, str(result.to_dict())[:6000]
            assert events.index('fetched') < events.index('ready') < events.index('worker')
            assert 'supervisor-denied' in events
            assert (roots[-1] / 'peer-records/prompts').is_file()
