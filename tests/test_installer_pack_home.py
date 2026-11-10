"""Service manager HOME must not change the collection served after an update."""
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

from astrid.core.execution.generic_host import GenericPackHost
from astrid.core.pack.transaction import install_group, refresh_pending
from astrid.installer import boundary_registration, update_job_platform
from astrid.installer.core import Version, write_json
from astrid.installer.service import install_selection
from tests.test_generic_host import FakeRuntime, _CHIPTUNE_ECHO, _write_v3_pack


def _install_chiptune(tmp_path, personal):
    source = tmp_path / 'source'
    _write_v3_pack(source / 'chiptune', 'chiptune', 'Chiptune',
                   {'sfx': _CHIPTUNE_ECHO['echo']}, capabilities=['sfx'])
    install_group(source=source, revision=None, packs={'chiptune': 'chiptune'},
                  packs_root=personal, python_packages={})


def _registered(host, runtime):
    host.register()
    return {row['capability_id'] for row in runtime.registrations[-1][1]['capabilities']
            if row['status'] == 'ready'}


@pytest.mark.linux_native
def test_linux_update_job_restart_keeps_the_installed_pack_collection(tmp_path, monkeypatch):
    install_home, manager_home = tmp_path / 'install-home', tmp_path / 'account-home'
    monkeypatch.setenv('HOME', str(install_home))
    monkeypatch.delenv('ASTRID_PACKS_PATH', raising=False)
    personal = install_home / 'Astrid/packs'
    runtime = FakeRuntime()
    host = GenericPackHost(pack_roots=[], client=runtime, personal_collection=True)
    host.register()
    _install_chiptune(tmp_path, personal)
    assert host.acknowledge_pack_refresh()
    assert 'chiptune.sfx' in _registered(host, runtime)
    assert not refresh_pending(packs_root=personal)

    # The fresh row completes a real action (including capture cleanup) before updating.
    task = {'task': {'id': 'fresh-task', 'attempt_id': 'fresh-attempt', 'fence': 3,
                     'capability': 'chiptune.sfx', 'project_id': 'demo', 'run_id': 'fresh-run',
                     'spec': {'spec': {'inputs': {}}}}}
    runtime.tasks['fresh-task'] = task
    runtime.INLINE_SETTLEMENT_OUTPUTS = True
    host.attempt_root = tmp_path / 'fresh-attempt'
    host.run_task(task, lease_token='fixture-lease')
    assert runtime.settlements and not runtime.failures

    root = tmp_path / 'program'
    record = {'job_name': 'astrid-update-' + 'a' * 32, 'operation_id': 'a' * 32,
              'program_root': str(root), 'log_path': str(root / 'logs/update.log'),
              'home': str(install_home)}
    command = ['installer', '--update']
    descriptor = update_job_platform.describe(record, command, system='linux')
    launches = []
    monkeypatch.setattr(update_job_platform, '_run', lambda args: launches.append(args)
                        or subprocess.CompletedProcess(args, 0, '', ''))
    update_job_platform.launch(descriptor, command, record['log_path'], system='linux')
    # A transient systemd job inherits the manager environment, plus explicit assignments.
    monkeypatch.setenv('HOME', str(manager_home))
    for arg in launches[0]:
        if arg.startswith('--setenv='):
            key, value = arg[len('--setenv='):].split('=', 1)
            monkeypatch.setenv(key, value)
    restarted_runtime = FakeRuntime()
    restarted = GenericPackHost(pack_roots=[], client=restarted_runtime, personal_collection=True)
    assert 'chiptune.sfx' in _registered(restarted, restarted_runtime)
    assert not refresh_pending(packs_root=personal)


@pytest.mark.posix_native
def test_handoff_launcher_update_hot_refresh_uses_the_install_home(tmp_path, monkeypatch):
    install_home, manager_home = tmp_path / 'install-home', tmp_path / 'account-home'
    monkeypatch.setenv('HOME', str(install_home))
    monkeypatch.delenv('ASTRID_PACKS_PATH', raising=False)
    root, source = tmp_path / 'program', tmp_path / 'data'
    data = Path(str(source) + '-r1')
    version = Version(root / 'versions/r12', Path(sys.executable), root / 'profile.json', {}, 12)
    version.root.mkdir(parents=True)
    (root / 'current').write_text('r12\n')
    selection = {'owner': 'haus.astrid.runtime', 'program_root': str(root),
                 'data_root': str(data), 'retained_r0_data_root': str(source),
                 'realm_id': 'fixture', 'realm_root': str(data / 'runtime/realms/fixture'),
                 'identity': version.document()['identity'],
                 'service_label': boundary_registration.R1_LINUX if sys.platform == 'linux'
                                  else boundary_registration.R1_MAC}
    write_json(install_selection(install_home, sys.platform), selection)
    write_json(root / 'install.json', selection)
    script = boundary_registration.launcher_script(version)
    monkeypatch.setenv('HOME', str(manager_home))
    monkeypatch.setattr(sys, 'argv', ['runtime', 'run', '--profile', 'astrid', '--data-root', str(data)])
    cli = ModuleType('astrid_runtime.cli')

    def main(_args):
        runtime = FakeRuntime()
        host = GenericPackHost(pack_roots=[], client=runtime, personal_collection=True)
        host.register()
        personal = install_home / 'Astrid/packs'
        # packs add runs in the install-line shell; the service keeps its own HOME.
        service_home = os.environ['HOME']
        with monkeypatch.context() as shell:
            shell.setenv('HOME', str(install_home))
            _install_chiptune(tmp_path, personal)
        assert os.environ['HOME'] == service_home
        assert host.acknowledge_pack_refresh(), 'service never consumed the installed collection outbox'
        assert 'chiptune.sfx' in _registered(host, runtime)
        assert not refresh_pending(packs_root=personal)
        return 0

    cli.main = main
    monkeypatch.setitem(sys.modules, 'astrid_runtime.cli', cli)
    with pytest.raises(SystemExit) as result:
        exec(script, {})
    assert result.value.code == 0
