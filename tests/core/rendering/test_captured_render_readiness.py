from pathlib import Path
import os
import pytest
from astrid.core.execution import generic_host as g
from astrid.core.foundation.paths import REPO_ROOT
from astrid.core.rendering import remotion_runtime as runtime
from tests.test_generic_host import _write_manifest


def test_command_dispatch_uses_command_readiness(tmp_path, monkeypatch):
    _write_manifest(tmp_path / 'render', capability_id='rendering.render')
    host = g.GenericPackHost(pack_roots=[tmp_path])
    record = host.discover()[0]
    class Observed(Exception): pass
    def inspect_environment(*args, **kwargs):
        assert kwargs.get('readiness_worker_kind') == 'command'
        raise Observed
    monkeypatch.setattr(host, '_child_environment', inspect_environment)
    output = tmp_path / 'attempt/outputs'; output.mkdir(parents=True)
    with pytest.raises(Observed):
        host._run_command_definition(record, {}, output, output.parent)


def test_captured_builtin_command_matches_launch_import_context(tmp_path, monkeypatch):
    monkeypatch.setenv('ASTRID_HOST_EXECUTION_PROFILE', 'source')
    environment = {'HOME': str(tmp_path), 'PATH': os.defpath,
                   'ASTRID_HOST_EXECUTION_PROFILE': 'source'}
    host = g.GenericPackHost(pack_roots=[REPO_ROOT / 'astrid/packs'], credential_source=environment)
    record = next(r for r in host.discover() if r.id == 'rendering.render')
    source = Path(record.definition.metadata['pack_root'])
    snapshot = tmp_path / 'capture/rendering'; snapshot.mkdir(parents=True)
    (snapshot / 'pack.yaml').write_bytes((source / 'pack.yaml').read_bytes())
    captured = g._relocate_pack_record(record, source, snapshot)
    # A captured builtin is outside the builtin tree; native projection would
    # introduce this private root, but command execution uses selected namespaces.
    assert g._admitted_python_roots(captured.source_root, captured.definition) == (snapshot,)
    launch = [runtime._registry_context(env, generator=False) for env in
              g._launch_worker_probe_environments(host.pack_roots, environment, remotion_only=True)]
    monkeypatch.setenv('ASTRID_REMOTION_VALIDATION_ROLE', 'host')
    monkeypatch.setenv('ASTRID_REMOTION_PROJECT_DIR', str(REPO_ROOT / 'remotion'))
    def validate(project, *, environment):
        assert runtime._registry_context(environment, generator=False) in launch
    monkeypatch.setattr(runtime, '_validated_runtime', validate)
    path = os.pathsep.join((str(REPO_ROOT), *g._dependency_pythonpath()))
    host._child_environment(captured, tmp_path / 'attempt', explicit_env={'PYTHONPATH': path},
                            readiness_worker_kind='command', allow_runtime_connection=False)
