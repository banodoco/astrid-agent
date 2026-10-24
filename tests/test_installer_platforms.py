"""Service contracts and native command boundaries, exercised without OS services."""
import io
import json
import plistlib
import subprocess
import tarfile
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime, timedelta
from pathlib import Path, PureWindowsPath
from unittest.mock import Mock
from urllib.error import HTTPError

import pytest

from astrid.installer import service, tools
from astrid.installer.backend import Backend
from astrid.installer.core import Version, read_json, write_json

FIXTURES = Path(__file__).parent / 'fixtures/installer'


def sample(system='linux'):
    root = PureWindowsPath('C:/Astrid/versions/v1') if system == 'win32' else Path('/astrid/versions/v1')
    python = root / ('py/Scripts/python.exe' if system == 'win32' else 'py/bin/python')
    version = Version(root, python, root / 'profile.json', {'PATH': 'C:\\Astrid\\tools;C:\\Windows\\System32' if system == 'win32' else '/astrid/tools:/usr/bin:/bin', 'PYTHONUTF8': '1'})
    data = PureWindowsPath('C:/Astrid Data') if system == 'win32' else Path('/astrid data')
    log = PureWindowsPath('C:/Astrid/logs/install.log') if system == 'win32' else Path('/logs/install.log')
    return version, data, log


def test_bun_build_environment_scrubs_ambient_policy_but_keeps_explicit_cache_and_ca(tmp_path, monkeypatch):
    ambient = {
        "BUN_INSTALL_GLOBAL_DIR": str(tmp_path / "outside"),
        "BUN_INSTALL_BIN": str(tmp_path / "outside-bin"),
        "BUN_CONFIG_NO_VERIFY": "1", "BUN_CONFIG_SKIP_LOAD_LOCKFILE": "1",
        "BUN_CONFIG_SKIP_SAVE_LOCKFILE": "1", "BUN_INSTALL_GLOBAL_STORE": "1",
        "BUN_INSTALL_CACHE_DIR": str(tmp_path / "ambient-cache"),
        "BUN_OPTIONS": "--preload=/outside/bun.ts", "NODE_OPTIONS": "--require=/outside/node.js",
        "HTTPS_PROXY": "http://proxy.invalid:8080", "NODE_EXTRA_CA_CERTS": "/chosen/ca.pem",
    }
    for key, value in ambient.items():
        monkeypatch.setenv(key, value)
    captured = []

    def execute(argv, **kwargs):
        captured.append(kwargs["env"])
        return subprocess.CompletedProcess(argv, 0, stdout="done")

    monkeypatch.setattr("astrid.installer.backend.subprocess.run", execute)
    backend = Backend(tmp_path / "program", tmp_path / "install.log", tmp_path / "uv")
    controlled = {"BUN_INSTALL_GLOBAL_DIR": str(tmp_path / "owned/global"),
                  "BUN_INSTALL_BIN": str(tmp_path / "owned/bin"),
                  "BUN_INSTALL_CACHE_DIR": str(tmp_path / "chosen-cache")}
    assert backend.run(["bun", "link"], extra_env=controlled, clean_bun_env=True) == "done"
    assert {key: value for key, value in captured[0].items() if key.startswith("BUN_")} == controlled
    assert "NODE_OPTIONS" not in captured[0]
    assert captured[0]["HTTPS_PROXY"] == ambient["HTTPS_PROXY"]
    assert captured[0]["NODE_EXTRA_CA_CERTS"] == ambient["NODE_EXTRA_CA_CERTS"]
    assert backend.run(["another-command"]) == "done"
    assert all(captured[1][key] == value for key, value in ambient.items())


@pytest.mark.parametrize('system,fixture', [('darwin','launchd.plist'), ('linux','systemd.service'), ('win32','task.xml')])
def test_service_definitions_match_reviewed_snapshots(system, fixture):
    version, data, log = sample(system)
    rendered = service.render(system, version, data, log, user='S-1-5-21-1234')
    if system == 'win32':
        root = ET.fromstring(rendered)
        ns = {'t': 'http://schemas.microsoft.com/windows/2004/02/mit/task'}
        boundary = root.find('t:Triggers/t:TimeTrigger/t:StartBoundary', ns)
        rendered = rendered.replace(boundary.text, '2000-01-01T00:00:00+00:00')
    assert rendered == (FIXTURES / fixture).read_text(encoding='utf-8')
    assert 'npm' not in rendered and 'pack_host' not in rendered and 'chat' not in rendered
    assert 'runtime-service.log' in rendered and 'install.log' not in rendered


def test_launchd_is_foreground_keepalive_with_explicit_environment():
    version, data, log = sample('darwin')
    value = plistlib.loads(service.render('darwin', version, data, log).encode())
    assert value['KeepAlive'] is True and value['RunAtLoad'] is True
    assert value['ProgramArguments'] == [str(version.python), '-I', '-m', 'astrid_runtime', 'run', '--profile', 'astrid', '--data-root', str(data), '--source-manifest', str(version.profile)]
    assert value['EnvironmentVariables']['PATH'] == version.env['PATH']


def test_launchd_operations_use_runtime_posix_user_identity(tmp_path, monkeypatch):
    current_user = Mock(return_value=501)
    monkeypatch.setattr(service.platform, 'current_user', current_user)
    runner = Mock(return_value='')
    manager = service.Service(tmp_path / 'program', tmp_path / 'log', runner,
                              home=tmp_path, system='darwin')
    version, data, _ = sample('darwin')
    manager.register(version, data)
    assert manager.start()
    manager.disable()
    target = f'gui/501/{service.LABEL}'
    assert [call.args[0] for call in runner.call_args_list] == [
        ['launchctl', 'bootstrap', 'gui/501', manager.path],
        ['launchctl', 'kickstart', target],
        ['launchctl', 'print', target],
        ['launchctl', 'bootout', target],
    ]
    assert current_user.call_count == 3


def test_windows_task_settings_and_no_console_executable():
    before = datetime.now().astimezone() - timedelta(seconds=1)
    version, data, log = sample('win32')
    root = ET.fromstring(service.render('win32', version, data, log, user='S-1-5-21-1234'))
    after = datetime.now().astimezone() + timedelta(seconds=1)
    ns = {'t': 'http://schemas.microsoft.com/windows/2004/02/mit/task'}
    def field(path):
        return root.find(path, ns).text
    assert field('t:Settings/t:ExecutionTimeLimit') == 'PT0S'
    assert field('t:Settings/t:DisallowStartIfOnBatteries') == 'false'
    assert field('t:Settings/t:StopIfGoingOnBatteries') == 'false'
    assert field('t:Settings/t:MultipleInstancesPolicy') == 'IgnoreNew'
    assert field('t:Principals/t:Principal/t:LogonType') == 'InteractiveToken'
    assert field('t:Principals/t:Principal/t:RunLevel') == 'LeastPrivilege'
    assert field('t:Triggers/t:LogonTrigger/t:UserId') == 'S-1-5-21-1234'
    assert field('t:Triggers/t:LogonTrigger/t:Repetition/t:Interval') == 'PT1M'
    assert field('t:Triggers/t:LogonTrigger/t:Repetition/t:StopAtDurationEnd') == 'false'
    logon = root.find('t:Triggers/t:LogonTrigger', ns)
    assert [child.tag.rsplit('}', 1)[-1] for child in logon] == ['Enabled', 'Repetition', 'UserId']
    assert root.find('t:Triggers/t:LogonTrigger/t:Repetition/t:Duration', ns) is None
    time_trigger = root.find('t:Triggers/t:TimeTrigger', ns)
    assert time_trigger is not None
    assert [child.tag.rsplit('}', 1)[-1] for child in time_trigger] == ['Enabled', 'StartBoundary', 'Repetition']
    boundary = datetime.fromisoformat(time_trigger.find('t:StartBoundary', ns).text)
    assert boundary.tzinfo is not None and before <= boundary <= after
    assert time_trigger.find('t:Repetition/t:Interval', ns).text == 'PT1M'
    assert time_trigger.find('t:Enabled', ns).text == 'true'
    assert field('t:Actions/t:Exec/t:Command').endswith('pythonw.exe')
    arguments = field('t:Actions/t:Exec/t:Arguments')
    assert 'os.environ.update' in arguments and 'astrid_runtime.cli' in arguments
    assert 'open(' in arguments and 'encoding=' in arguments
    xml = service.render('win32', version, data, log)
    assert xml.startswith('<Task ')
    assert '<?xml' not in xml
    assert 'Hidden' not in xml


def test_systemd_quotes_spaces_percent_dollar_and_supply_tool_path():
    version, data, log = sample()
    version = Version(version.root, Path('/astrid %v $x/py/bin/python'), version.profile, version.env)
    unit = service.render('linux', version, data, log)
    assert 'ExecStart="/astrid %%v $$x/py/bin/python"' in unit
    assert '"/astrid data"' in unit
    assert 'Environment="PATH=/astrid/tools:/usr/bin:/bin"' in unit
    assert 'Restart=always' in unit
    assert 'WantedBy=default.target' in unit


def test_non_systemd_fallback_is_explicit_and_requests_runtime_start(tmp_path):
    runner = Mock(side_effect=FileNotFoundError('systemctl'))
    lines = []
    manager = service.Service(tmp_path / 'program', tmp_path / 'log', runner, home=tmp_path, system='linux', say=lines.append)
    version = Version(tmp_path / 'program/versions/v1', tmp_path / 'python', tmp_path / 'profile', {'PATH': '/tools:/bin'})
    manager.register(version, tmp_path / 'data')
    assert manager.start() is False
    assert lines == ['There is no systemd user service; run the install line to restart Astrid.']
    assert read_json(manager.selection)['data_root'] == str(tmp_path / 'data')
    assert 'version' not in read_json(manager.selection)
    assert not (service.default_program_root(tmp_path, 'linux') / 'service.json').exists()


def test_task_registration_is_no_profile_and_failure_is_not_hidden(tmp_path):
    runner = Mock(side_effect=subprocess.CalledProcessError(1, 'powershell'))
    manager = service.Service(tmp_path / 'program', tmp_path / 'log', runner, home=tmp_path, system='win32')
    with pytest.raises(subprocess.CalledProcessError):
        manager.powershell('Register-ScheduledTask')
    argv = runner.call_args.args[0]
    assert '-NoProfile' in argv and '-ExecutionPolicy' in argv
    assert "$ErrorActionPreference='Stop'" in argv[-1]


def test_service_record_preserves_an_unavailable_workspace_instead_of_falling_back(tmp_path):
    manager = service.Service(tmp_path / 'program', tmp_path / 'log', Mock(), home=tmp_path, system='linux')
    write_json(manager.selection, {'owner': service.LABEL, 'data_root': str(tmp_path / 'offline-drive')})
    assert manager.recorded()['data_root'] == str(tmp_path / 'offline-drive')


def test_ffmpeg_probe_checks_every_required_encoder_and_analysis_filter():
    encoders = '\n'.join(' V..... ' + name + ' description' for name in tools.ENCODERS)
    filters = '\n'.join(' ... ' + name + ' description' for name in tools.FILTERS)
    tools.probe_ffmpeg(encoders, filters)
    for name in tools.ENCODERS | tools.FILTERS:
        with pytest.raises(ValueError, match=name):
            tools.probe_ffmpeg(encoders.replace(name, 'absent'), filters.replace(name, 'absent'))
    with pytest.raises(ValueError, match='libx264'):
        tools.probe_ffmpeg('ffmpeg version 7.0.2', 'ffmpeg version 7.0.2')


@pytest.mark.parametrize('name', ['../escape', '/absolute', 'C:/windows', 'safe\\..\\escape'])
def test_downloaded_archives_cannot_escape_owned_staging_directory(tmp_path, name):
    archive = tmp_path / 'payload.zip'
    with zipfile.ZipFile(archive, 'w') as bundle:
        bundle.writestr(name, 'payload')
    with pytest.raises(ValueError, match='Unsafe archive'):
        tools.extract(archive, tmp_path / 'extract')
    assert not (tmp_path / 'escape').exists()


def test_tar_never_materializes_links_and_keeps_official_node_npm_entrypoint(tmp_path):
    archive = tmp_path / 'node.tar.gz'
    with tarfile.open(archive, 'w:gz') as bundle:
        entry = tarfile.TarInfo('node/bin/npm')
        entry.type = tarfile.SYMTYPE
        entry.linkname = '../lib/node_modules/npm/bin/npm-cli.js'
        bundle.addfile(entry)
        entry = tarfile.TarInfo('node/lib/node_modules/npm/bin/npm-cli.js')
        entry.size = 4
        bundle.addfile(entry, io.BytesIO(b'npm!'))
    tools.extract(archive, tmp_path / 'extract')
    assert not (tmp_path / 'extract/node/bin/npm').exists()
    assert tools.binary(tmp_path / 'extract', 'npm-cli.js').read_bytes() == b'npm!'


def test_uninstall_only_removes_owned_files_and_reports_windows_lock_residue(tmp_path, monkeypatch):
    program, data = tmp_path / 'program', tmp_path / 'data'
    program.mkdir()
    data.mkdir()
    sentinel = data / 'film'
    sentinel.write_text('keep')
    backend = Backend(program, tmp_path / 'log', tmp_path / 'uv')
    with pytest.raises(ValueError, match='unowned'):
        backend.remove(program, data, delete_data=False)
    write_json(program / 'install.json', {'owner': service.LABEL})
    (program / 'versions').mkdir()
    monkeypatch.setattr('shutil.rmtree', Mock(side_effect=PermissionError('locked DLL')))
    assert backend.remove(program, data, delete_data=False) == [program / 'versions']
    assert read_json(program / 'install.json')['owner'] == service.LABEL
    assert sentinel.read_text() == 'keep'


def test_backend_native_failures_reach_transaction_and_log_details(tmp_path, monkeypatch):
    backend = Backend(tmp_path / 'program', tmp_path / 'install.log', tmp_path / 'uv')
    monkeypatch.setattr(subprocess, 'run', Mock(return_value=subprocess.CompletedProcess(['tool'], 19, 'failure details')))
    with pytest.raises(subprocess.CalledProcessError) as error:
        backend.run(['tool'])
    assert error.value.returncode == 19
    assert 'failure details' in (tmp_path / 'install.log').read_text()


def test_stage_service_declaration_uses_candidate_and_preserves_chat_settings(tmp_path, monkeypatch):
    backend = Backend(tmp_path / 'program', tmp_path / 'install.log', tmp_path / 'uv')
    old_profile = tmp_path / 'old' / 'profile.json'
    old_services = tmp_path / 'old' / 'services.json'
    old_profile.parent.mkdir()
    old_profile.write_text(json.dumps({'service_declaration': str(old_services)}))
    old_services.write_text(json.dumps({'version': 1, 'services': [{
        'name': 'chat', 'env': {
            'ASTRID_ACP_CWD': str(tmp_path / 'previous chat cwd'),
            'ASTRID_ACP_SESSION_DIR': str(tmp_path / 'chat sessions'),
            'ASTRID_ACP_PROFILE': 'film-profile',
            'ASTRID_ACP_COMMAND': str(tmp_path / 'old astrid command'),
        },
    }]}))
    old = Version(tmp_path / 'old', tmp_path / 'old' / 'py' / 'bin' / 'python', old_profile, {})
    candidate_python = tmp_path / 'candidate' / 'py' / 'bin' / 'python'
    candidate_agent = tmp_path / 'candidate' / 'a'
    candidate_python.parent.mkdir(parents=True)
    (tmp_path / 'candidate').mkdir(exist_ok=True)
    candidate_data = tmp_path / 'workspace data'
    pinned_bun = str(tmp_path / 'candidate' / 'tools' / 'bun' / 'bun')
    recorded = {}

    def run(argv, *, extra_env, **_kwargs):
        recorded['argv'] = argv
        recorded['env'] = extra_env
        Path(argv[-1]).write_text('{"version":1,"services":[]}', encoding='utf-8')
        return ''

    monkeypatch.setattr(backend, 'run', run)
    services_path = backend._declare_services(
        folder=tmp_path / 'candidate', agent=candidate_agent, data=candidate_data,
        python=candidate_python, bun=pinned_bun, env={'PATH': str(candidate_python.parent)}, old=old,
    )

    assert recorded['argv'][0] == candidate_python
    assert recorded['argv'][1:3] == ['-I', '-c']
    assert 'from astrid.services import service_declaration' in recorded['argv'][3]
    assert str(candidate_agent) in recorded['argv'] and str(candidate_data) in recorded['argv']
    assert pinned_bun in recorded['argv']
    assert recorded['env']['ASTRID_ACP_SESSION_DIR'] == str(tmp_path / 'chat sessions')
    assert recorded['env']['ASTRID_ACP_PROFILE'] == 'film-profile'
    assert recorded['env']['ASTRID_ACP_COMMAND'] == str(tmp_path / 'old astrid command')
    assert Path(recorded['argv'][-2]) == tmp_path / 'previous chat cwd'
    assert services_path == tmp_path / 'candidate' / 'services.json'
    assert json.loads(services_path.read_text(encoding='utf-8')) == {'version': 1, 'services': []}


@pytest.mark.parametrize('prior_port,requested_port,expected_port', [
    (None, None, '17340'), ('17391', None, '17391'), ('17391', '17393', '17393'),
])
def test_stage_profile_points_to_candidate_service_declaration(tmp_path, monkeypatch, prior_port, requested_port, expected_port):
    backend = Backend(tmp_path / 'program', tmp_path / 'install.log', tmp_path / 'uv')
    prior = Version(tmp_path / 'prior', tmp_path / 'python', tmp_path / 'profile',
                    {'ASTRID_RUNTIME_DOOR_PORT': prior_port}) if prior_port else None
    monkeypatch.setattr(backend, 'existing', lambda _data: prior)
    if requested_port:
        monkeypatch.setenv('ASTRID_RUNTIME_DOOR_PORT', requested_port)
    else:
        monkeypatch.delenv('ASTRID_RUNTIME_DOOR_PORT', raising=False)
    def source(name, _sha, destination):
        if name == 'agent':
            (destination / 'remotion/node_modules').mkdir(parents=True)
            (destination / 'remotion/package-lock.json').write_text('{}\n')
        return destination
    monkeypatch.setattr('astrid.installer.backend.source', source)
    monkeypatch.setattr('astrid.installer.backend.install_tools', lambda *_args: {
        'PATH': str(tmp_path / 'tools'),
        'ASTRID_NODE_EXECUTABLE': str(tmp_path / 'tools/node'),
        'ASTRID_BUN_EXECUTABLE': str(tmp_path / 'tools/bun'),
    })
    monkeypatch.setattr('astrid.installer.backend.binary', lambda *_args: tmp_path / 'npm-cli.js')
    calls = []

    def run(argv, **_kwargs):
        calls.append(argv)
        if argv[0] == backend.uv and argv[1:] == ['--version']:
            return 'uv 1.0.0'
        if len(argv) > 3 and argv[1:3] == ['-I', '-c'] and 'service_declaration' in argv[3]:
            Path(argv[-1]).write_text('{"version":1,"services":[]}', encoding='utf-8')
        if len(argv) > 3 and argv[1:3] == ['-I', '-c'] and 'source_checkout_digest' in argv[3]:
            return 'digest-value'
        return ''

    monkeypatch.setattr(backend, 'run', run)
    manifest = {
        'agent': {'sha': 'a' * 40}, 'runtime': {'sha': 'b' * 40},
        'tools': {'uv': {'version': '1.0.0'}, 'python': {'version': '3.11.9'}},
    }

    version = backend.stage(manifest, tmp_path / 'program', tmp_path / 'workspace')

    assert version.env['ASTRID_RUNTIME_DOOR_PORT'] == expected_port
    profile = json.loads(version.profile.read_text(encoding='utf-8'))
    declaration = Path(profile['service_declaration'])
    assert declaration == version.root / 'services.json'
    assert json.loads(declaration.read_text(encoding='utf-8')) == {'version': 1, 'services': []}
    assert any('service_declaration' in argv[3] for argv in calls if len(argv) > 3 and argv[1:3] == ['-I', '-c'])


def test_linux_backup_temporarily_starts_only_neutral_daemon_and_snapshots_stopped_support(tmp_path, monkeypatch):
    program, data, realm = tmp_path / 'program', tmp_path / 'data', tmp_path / 'data/runtime/realms/one'
    realm.mkdir(parents=True)
    (realm / 'film').write_text('large media')
    write_json(data / 'runtime/catalog.json', {'selected': 'one'})
    backend = Backend(program, tmp_path / 'log', tmp_path / 'uv')
    backend.service.system = 'linux'
    calls = []
    def runtime(version, root, *args, **kwargs):
        calls.append(args)
        if args[0] == 'backup':
            destination = Path(args[2])
            destination.mkdir(parents=True)
            (destination / 'authenticated-backup').write_text('verified')
        return {}
    backend.runtime = runtime
    backend.stop = lambda version, root: calls.append(('stop',))
    version = Version(program / 'versions/old', tmp_path / 'python', tmp_path / 'profile', {})
    result = backend.backup(version, data, {'realm_root': str(realm)})
    assert [call[0] for call in calls] == ['up', 'backup', 'stop']
    assert calls[0] == ('up', '--hosted-origin', 'off')
    snapshot = Path(result['root']) / 'support'
    assert read_json(snapshot / 'runtime/catalog.json') == {'selected': 'one'}
    assert not (snapshot / 'runtime/realms').exists()
    assert (realm / 'film').read_text() == 'large media'


def test_mac_backup_uses_runtime_fsclone_only_after_stop_and_never_starts_daemon(tmp_path, monkeypatch):
    import shutil
    program, data, realm = tmp_path / 'program', tmp_path / 'data', tmp_path / 'realm'
    realm.mkdir()
    (realm / 'realm.sqlite3').write_text('stopped db')
    write_json(data / 'runtime/catalog.json', {'selected': 'one'})
    copies = []
    def clone(src, dst):
        copies.append((src, dst))
        shutil.copy2(src, dst)
    monkeypatch.setattr('runtime_protocol.fsclone.clone_or_copy_file', clone)
    backend = Backend(program, tmp_path / 'log', tmp_path / 'uv')
    backend.service.system = 'darwin'
    backend.runtime = Mock(side_effect=AssertionError('must remain stopped'))
    version = Version(program / 'versions/old', tmp_path / 'python', tmp_path / 'profile', {})
    result = backend.backup(version, data, {'realm_root': str(realm)})
    assert result['kind'] == 'clone'
    assert {src.name for src, dst in copies} == {'realm.sqlite3', 'catalog.json'}
    assert (Path(result['root']) / 'realm/realm.sqlite3').read_text() == 'stopped db'


def test_clone_rollback_restores_workspace_and_support_without_deleting_other_realms(tmp_path, monkeypatch):
    import shutil
    program, data = tmp_path / 'program', tmp_path / 'data'
    realm = data / 'runtime/realms/one'
    other = data / 'runtime/realms/two'
    realm.mkdir(parents=True)
    other.mkdir()
    (realm / 'realm.sqlite3').write_text('original')
    (other / 'unselected').write_text('keep')
    write_json(data / 'runtime/catalog.json', {'selected': 'one'})
    write_json(data / 'runtime/source-profiles/astrid.json', {'version': 'old'})
    monkeypatch.setattr('runtime_protocol.fsclone.clone_or_copy_file', lambda src, dst: shutil.copy2(src, dst))
    backend = Backend(program, tmp_path / 'log', tmp_path / 'uv')
    backend.service.system = 'darwin'
    version = Version(program / 'versions/old', tmp_path / 'python', tmp_path / 'profile', {})
    identity = {'realm_root': str(realm)}
    backup = backend.backup(version, data, identity)
    (realm / 'realm.sqlite3').write_text('migrated')
    write_json(data / 'runtime/source-profiles/astrid.json', {'version': 'new'})
    backend.restore(version, data, identity, backup)
    assert (realm / 'realm.sqlite3').read_text() == 'original'
    assert read_json(data / 'runtime/source-profiles/astrid.json') == {'version': 'old'}
    assert (other / 'unselected').read_text() == 'keep'
    assert any((p / 'realm.sqlite3').read_text() == 'migrated' for p in realm.parent.glob('one.failed-*'))


@pytest.mark.parametrize('mismatch', [None, 'realm', 'instance', 'port', 'owner', 'restart-required'])
@pytest.mark.parametrize('door_port', [17340, 17391])
def test_door_verification_requires_selected_realm_recorded_instance_and_live_owner(tmp_path, monkeypatch, mismatch, door_port):
    import json

    from astrid.installer import backend as module
    program, data = tmp_path / 'program', tmp_path / 'data'
    version = Version(program / 'versions/old', tmp_path / 'python', tmp_path / 'profile',
                      {'ASTRID_RUNTIME_DOOR_PORT': str(door_port)})
    discovery = {'active_realm': 'selected', 'runtime_instance_id': 'owned-instance',
                 'door_endpoint': f'http://127.0.0.1:{door_port}'}
    if mismatch == 'port':
        discovery['door_endpoint'] = 'http://127.0.0.1:9999'
    write_json(data / 'runtime/discovery.json', discovery)
    health = {'status': 'ok', 'runtime_instance_id': 'foreign' if mismatch == 'instance' else 'owned-instance'}
    realm = {'realm_id': 'other' if mismatch == 'realm' else 'selected'}
    def opening(request, timeout):
        if mismatch == 'restart-required':
            raise HTTPError(request.full_url, 503, 'Service Unavailable', {},
                            io.BytesIO(b'{"error":"runtime_identity_mismatch"}'))
        return io.BytesIO(json.dumps(health if request.full_url.endswith('/health') else realm).encode())
    opener = Mock()
    opener.open.side_effect = opening
    monkeypatch.setattr(module.urllib.request, 'build_opener', Mock(return_value=opener))
    ticks = iter([0, 0, 91])
    monkeypatch.setattr(module.time, 'monotonic', lambda: next(ticks))
    monkeypatch.setattr(module.time, 'sleep', lambda value: None)
    backend = Backend(program, tmp_path / 'log', tmp_path / 'uv')
    backend.runtime = Mock(return_value={'discovery': discovery, 'support': {'pid_alive': mismatch != 'owner'}})
    if mismatch:
        with pytest.raises(RuntimeError, match='door did not become ready'):
            backend.verify(version, data, 'selected')
    else:
        backend.verify(version, data, 'selected')
        assert all(call.args[0].headers['Origin'] == 'https://astrid.haus' for call in opener.open.call_args_list)


def test_door_verification_retries_status_observed_before_discovery_was_published(tmp_path, monkeypatch):
    from astrid.installer import backend as module

    program, data = tmp_path / "program", tmp_path / "data"
    version = Version(program / "versions/old", tmp_path / "python", tmp_path / "profile", {})
    discovery = {"active_realm": "selected", "runtime_instance_id": "owned-instance",
                 "door_endpoint": "http://127.0.0.1:17340"}
    write_json(data / "runtime/discovery.json", discovery)
    health = {"status": "ok", "runtime_instance_id": "owned-instance"}
    realm = {"realm_id": "selected"}
    opener = Mock()
    opener.open.side_effect = lambda request, timeout: io.BytesIO(json.dumps(
        health if request.full_url.endswith("/health") else realm).encode())
    monkeypatch.setattr(module.urllib.request, "build_opener", Mock(return_value=opener))
    monkeypatch.setattr(module.time, "monotonic", lambda: 0)
    sleep = Mock()
    monkeypatch.setattr(module.time, "sleep", sleep)
    backend = Backend(program, tmp_path / "log", tmp_path / "uv")
    backend.runtime = Mock(side_effect=[
        {"discovery": None, "support": {"pid_alive": True}},
        {"discovery": discovery, "support": {"pid_alive": True}},
    ])

    backend.verify(version, data, "selected")

    assert backend.runtime.call_count == 2
    assert opener.open.call_count == 4
    sleep.assert_called_once_with(0.25)


def test_source_archives_are_flattened_to_shallow_managed_paths(tmp_path, monkeypatch):
    def unpack(url, destination):
        child = destination / ('astrid-agent-' + 'a' * 40)
        child.mkdir(parents=True)
        (child / 'pyproject.toml').write_text('source')
    monkeypatch.setattr(tools, 'fetch_archive', unpack)
    root = tools.source('agent', 'a' * 40, tmp_path / 'a')
    assert root == tmp_path / 'a'
    assert (root / 'pyproject.toml').read_text() == 'source'
    assert not (tmp_path / 'a.unpack').exists()


def test_python311_windows_reparse_points_are_rejected_using_mocked_file_attributes(monkeypatch):
    import stat
    from types import SimpleNamespace

    from astrid.installer.core import absolute, linked
    monkeypatch.setattr(Path, 'is_symlink', lambda path: False)
    monkeypatch.setattr(Path, 'lstat', lambda path: SimpleNamespace(st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT))
    assert linked(Path('/junction'))
    with pytest.raises(ValueError, match='cannot contain links'):
        absolute('/junction/workspace')


def test_delete_data_includes_identity_checked_external_attached_workspace(tmp_path, monkeypatch):
    program, data, external = tmp_path / 'program', tmp_path / 'support', tmp_path / 'film'
    data.mkdir()
    external.mkdir()
    (external / 'film.mov').write_text('media')
    write_json(program / 'install.json', {'owner': service.LABEL, 'data_root': str(data)})
    backend = Backend(program, tmp_path / 'log', tmp_path / 'uv')
    version = Version(program / 'versions/old', tmp_path / 'python', tmp_path / 'profile', {})
    monkeypatch.setattr(backend, 'existing', lambda path: version)
    monkeypatch.setattr(backend, 'workspace', lambda version, root: {'realm_root': str(external)})
    assert backend.remove(program, data, delete_data=True) == []
    assert not program.exists() and not data.exists() and not external.exists()


def test_no_systemd_starts_foreground_runtime_supervisor_with_recorded_birth_identity(tmp_path, monkeypatch):
    from runtime_protocol import platform
    run = Mock(side_effect=FileNotFoundError('no systemd'))
    manager = service.Service(tmp_path / 'program', tmp_path / 'log', run, home=tmp_path, system='linux')
    version = Version(tmp_path / 'program/versions/v1', tmp_path / 'python', tmp_path / 'profile', {'PATH': '/tools:/bin'})
    manager.register(version, tmp_path / 'data')
    spawn = Mock(return_value=Mock(pid=123))
    monkeypatch.setattr(platform, 'spawn_tree', spawn)
    monkeypatch.setattr(platform, 'process_identity', lambda pid: {'birth_id': 'birth-123'})
    manager.start_foreground(version, tmp_path / 'data')
    # The foreground owner loads the documented settings before invoking Runtime.
    assert spawn.call_args.args[0][1:3] == ['-I', '-c']
    assert 'from astrid_runtime.cli import main' in spawn.call_args.args[0][3]
    assert str(manager.env_file) in spawn.call_args.args[0][3]
    assert spawn.call_args.kwargs['detached'] is True
    assert spawn.call_args.kwargs['env']['PATH'] == '/tools:/bin'
    assert read_json(manager.selection)['foreground'] == {'pid': 123, 'birth_id': 'birth-123'}
    # PID reuse cannot make uninstall wait for an unrelated process.
    monkeypatch.setattr(platform, 'process_identity', lambda pid: {'birth_id': 'new-owner'})
    manager.stop()


def test_kept_reinstall_retains_explicit_chat_choices_from_external_selection(tmp_path, monkeypatch):
    backend = Backend(tmp_path / 'program', tmp_path / 'install.log', tmp_path / 'uv')
    data = tmp_path / 'workspace'
    settings = {'ASTRID_ACP_CWD': str(tmp_path / 'selected cwd'),
                'ASTRID_ACP_SESSION_DIR': str(tmp_path / 'sessions'),
                'ASTRID_ACP_PROFILE': 'selected-profile', 'UNRELATED_SECRET': 'excluded'}
    record = {'owner': service.LABEL, 'program_root': str(backend.root),
              'data_root': str(data), 'uninstalled': True, 'chat_settings': settings}
    monkeypatch.setattr(backend.service, 'recorded', lambda: record)

    cwd, options = backend._selected_chat_settings(None, data)

    assert cwd == tmp_path / 'selected cwd'
    assert options == {'ASTRID_ACP_SESSION_DIR': str(tmp_path / 'sessions'),
                       'ASTRID_ACP_PROFILE': 'selected-profile'}
    assert backend._selected_chat_settings(None, tmp_path / 'different workspace') == (
        tmp_path / 'different workspace' / 'chat', {})


def test_chat_declaration_failure_can_retry_without_claiming_fresh_workspace(tmp_path, monkeypatch):
    backend = Backend(tmp_path / 'program', tmp_path / 'install.log', tmp_path / 'uv')
    data = tmp_path / 'workspace'
    folder = tmp_path / 'candidate'
    folder.mkdir()
    monkeypatch.setattr(backend.service, 'recorded', lambda: {})
    failure = [True]

    def run(argv, **_kwargs):
        assert Path(argv[-2]) == data / 'chat'
        if failure[0]:
            raise subprocess.CalledProcessError(1, argv)
        Path(argv[-1]).write_text(json.dumps({'services': [{'name': 'chat', 'env': {
            'ASTRID_ACP_CWD': str(data / 'chat'),
        }}]}))
        return ''

    monkeypatch.setattr(backend, 'run', run)
    kwargs = dict(folder=folder, agent=folder / 'a', data=data, python=folder / 'python',
                  bun=str(folder / 'bun'), env={}, old=None)
    with pytest.raises(subprocess.CalledProcessError):
        backend._declare_services(**kwargs)
    assert not backend.has_workspace(data)
    failure[0] = False
    declaration = backend._declare_services(**kwargs)
    assert not backend.has_workspace(data)
    profile = folder / 'profile.json'
    profile.write_text(json.dumps({'service_declaration': str(declaration)}))
    version = Version(folder, folder / 'python', profile, {})
    backend.service.selection = tmp_path / 'outside' / 'selection.json'
    monkeypatch.setattr(backend.service, 'register', lambda *_args: (
        None if (data / 'chat').is_dir() else pytest.fail('managed chat cwd absent at registration')))
    monkeypatch.setattr(backend, 'workspace', lambda *_args: {'realm_id': 'selected', 'realm_root': str(data / 'realm')})
    backend.register(version, data)
    assert read_json(backend.service.selection)['chat_settings']['ASTRID_ACP_CWD'] == str(data / 'chat')
