"""Guarded historical-realm upgrade with the existing operator process boundary."""
from __future__ import annotations

import copy
import io
import json
import subprocess
import sys
import sysconfig
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.helpers.runtime import ASTRID, runtime_checkout

SOURCE_AGENT = 'ba21beffd177e7d80a5b839bde5a9e25b97235c2'
SOURCE_RUNTIME = 'b2a1d9fd82121a5509769531a2e91aece97d78b9'
SOURCE_FINGERPRINT = '30234262d5ccc9f13cfb086234e9f33b6e97670825ddfbda826863802ccc84c0'


def upgrade_manifest_copy(root, tmp_path):
    from astrid_runtime import operator_upgrade
    from astrid_runtime.bootstrap import BootstrapConfig, SourceProfile
    from astrid_runtime.paths import RuntimePaths
    from runtime_protocol.auth import CredentialStore
    from runtime_protocol.service import RuntimeService
    from runtime_protocol.timeline_commit_provider import load_timeline_commit_provider
    from runtime_protocol.upgrade import inspect_realm_schema
    current_runtime = runtime_checkout()
    with pytest.MonkeyPatch.context() as imports:
        imports.syspath_prepend(str(current_runtime / 'tests'))
        from test_operator_upgrade import _Boundary

    prior_runtime = runtime_checkout(commit=SOURCE_RUNTIME)
    prior_agent = tmp_path / 'prior-agent'
    prior_agent.mkdir()
    archive = subprocess.run(['git', '-C', str(ASTRID), 'archive', SOURCE_AGENT],
                             check=True, capture_output=True).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as source:
        source.extractall(prior_agent, filter='data')
    identity = inspect_realm_schema(root)
    assert identity['kind'] == 'H31'
    assert identity['fingerprint'] == SOURCE_FINGERPRINT
    realm_id = identity['realm_id']
    home = tmp_path / 'operator-home'
    home.mkdir()
    paths = RuntimePaths.current(home=home, data_root=tmp_path / 'operator-support')
    paths.ensure_support_dirs()
    prior = SourceProfile(profile='astrid', runtime_checkout=str(prior_runtime),
                          source_checkout=str(prior_agent))
    current = SourceProfile(profile='astrid', runtime_checkout=str(current_runtime),
                            source_checkout=str(ASTRID))
    catalog = {'version': 1, 'selected_realm_id': realm_id,
               'realms': [{'realm_id': realm_id, 'display_name': 'S01 historical copy',
                           'data_root': str(root)}],
               'source_profiles': {'astrid': prior.as_dict()}}
    paths.catalog_path.write_text(json.dumps(catalog))
    (paths.source_profiles_dir / 'astrid.json').write_text(json.dumps(prior.as_dict()))
    credentials = CredentialStore(paths.runtime_support / 'credentials')
    token, _ = credentials.provision('owner', ['projects:read', 'projects:write'])
    proof = {'schema_before': identity, 'process_boundary': 'test_operator_upgrade._Boundary',
             'source_agent': SOURCE_AGENT, 'source_runtime': SOURCE_RUNTIME, 'starts': []}

    class Boundary(_Boundary):
        def __init__(self):
            super().__init__(paths, live=False)
            self.report = None

        def health(self, **kwargs):
            discovery = json.loads(paths.discovery_path.read_text())
            assert kwargs == {'endpoint': discovery['endpoint'], 'pid': discovery['pid'],
                              'instance_id': discovery['runtime_instance_id']}
            return bool(self.report and self.report.get('ok'))

        def connect(self, **kwargs):
            credentials.require(kwargs['credential'], 'projects:read')
            assert kwargs['credential'] == token
            self.connection_options = {key: value for key, value in kwargs.items() if key != 'credential'}
            # Keep actual integrity evidence; the existing boundary's canned
            # doctor response must not stand in for the real Runtime report.
            return SimpleNamespace(doctor=lambda: copy.deepcopy(self.report))

    boundary = Boundary()

    def start(target_paths, selected_boundary, config):
        profile = config.resolve_source_profile(target_paths)
        proof['starts'].append(profile.as_dict())
        if Path(profile.runtime_checkout).resolve() == current_runtime.resolve():
            service = RuntimeService(root, support_root=paths.runtime_support)
            try:
                service.register_timeline_commit_provider(load_timeline_commit_provider('astrid'))
                report = service.doctor()
                assert report['ok'], report
                proof['candidate_doctor'] = report
                proof['candidate_integrity'] = service.store.integrity_report(catalog_path=paths.catalog_path)
                assert proof['candidate_integrity']['ok']
            finally:
                service.close()
        else:
            # Recovery must use the retained matching source in its own
            # interpreter. Do not mix historical modules into current Runtime.
            child = '''
import json, sys
from pathlib import Path
runtime, agent, dependencies, root, support, output = map(Path, sys.argv[1:])
sys.path[:0] = [str(agent), str(runtime), str(runtime / 'tests'), str(dependencies)]
from runtime_protocol.service import RuntimeService
from runtime_protocol.timeline_commit_provider import load_timeline_commit_provider
service = RuntimeService(root, support_root=support)
try:
    service.register_timeline_commit_provider(load_timeline_commit_provider('astrid'))
    report = service.doctor()
    output.write_text(json.dumps(report))
    assert report['ok'], report
finally:
    service.close()
'''
            recovered = tmp_path / 'recovery-doctor.json'
            native = subprocess.run([sys.executable, '-I', '-S', '-c', child,
                profile.runtime_checkout, profile.source_checkout, sysconfig.get_paths()['purelib'],
                str(root), str(paths.runtime_support), str(recovered)], capture_output=True, text=True)
            proof['recovery_native'] = {'returncode': native.returncode,
                                        'stdout': native.stdout, 'stderr': native.stderr}
            assert native.returncode == 0, native.stdout + native.stderr
            report = json.loads(recovered.read_text())
        boundary.report = report
        discovery = {'pid': 1235, 'active_realm': realm_id, 'endpoint': 'http://127.0.0.1:1235',
                     'runtime_instance_id': 's01-process-boundary', 'process_birth_id': 's01-boundary-birth',
                     'realm_root': str(root.resolve())}
        target_paths.discovery_path.write_text(json.dumps(discovery))
        target_paths.instance_lock_path.write_text(json.dumps({**discovery, 'realm_id': realm_id}))
        return SimpleNamespace(endpoint=discovery['endpoint'], pid=1235,
                               runtime_instance_id=discovery['runtime_instance_id'])

    try:
        with pytest.MonkeyPatch.context() as patch:
            patch.setenv('XDG_DATA_HOME', str(tmp_path / 'operator-xdg'))
            patch.setattr(operator_upgrade, '_bootstrap_locked', start)
            result = operator_upgrade.upgrade_workspace(paths, boundary, BootstrapConfig(source_profile=current))
        proof['result'] = result
        assert result['ok']
        assert result['schema_before'] == 'H31'
        assert result['verification']['integrity']['ok']
        proof['schema_after'] = inspect_realm_schema(root, expected_realm_id=realm_id)
        assert proof['schema_after']['kind'] == 'U33'
        return result
    finally:
        journal = paths.runtime_support / 'upgrade-journal.json'
        if journal.exists():
            proof['journal'] = json.loads(journal.read_text())
        (tmp_path / 'operator-proof.json').write_text(json.dumps(proof, indent=2, sort_keys=True) + '\n')
