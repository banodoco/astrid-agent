"""Validate all selected inputs and installed bytes before writing evidence or APIs."""
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
import subprocess
from urllib.parse import urlsplit

from .common import GateError, exact_path, inventory, read, require, sha

SOURCES = ('agent', 'runtime', 'vibecomfy', 'lifecycle', 'runpod')
# Boundary inventory from reviewed task 54. Paths are stable; pins come from Git.
BOUNDARIES = {
    'agent': ('astrid/installer/backend.py', 'astrid/installer/service.py',
              'astrid/installer/worker_profile.py', 'astrid/services.py',
              'astrid/core/execution/generic_host.py'),
    'runtime': ('runtime_protocol/local_worker_composition.py',
                'runtime_protocol/local_execution_supervisor.py',
                'runtime_protocol/verified_host_launch.py', 'astrid_runtime/custody_broker.py',
                'runtime_protocol/process_binding.py', 'runtime_protocol/linux_process_binding.py'),
}
ASSETS = Path(__file__).parent / 'assets'


def git(root, *args):
    argv = ['git', '-C', str(root), *args]
    process = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             env={'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8',
                                  'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': '/dev/null',
                                  'GIT_OPTIONAL_LOCKS': '0'})
    require(process.returncode == 0, 'source', root,
            f'command={argv!r} error={process.stderr.decode(errors="replace")}')
    return process.stdout


def blob(source, relative):
    return git(source['root'], 'show', f"{source['sha']}:{relative}")


@dataclass(frozen=True)
class Context:
    tuple_path: Path
    task: Path
    work: Path
    payload: dict
    version: Path
    purelib: Path
    boundary: dict

    @property
    def install(self):
        return self.work / 'install'

    @property
    def evidence(self):
        return self.task / 'logs/installed-canary'

    @property
    def python(self):
        return self.version / 'py/bin/python'


def _validate(tuple_path, task_dir, work_dir, *, fresh=True):
    tuple_path = exact_path(str(tuple_path), kind='file')
    task = exact_path(str(task_dir), kind='directory')
    work = exact_path(str(work_dir), kind='directory')
    require(not (task.is_relative_to(work) or work.is_relative_to(task)),
            'inputs', work, 'task and work directories must be disjoint')
    value = read(tuple_path)
    require(set(value) == {'schema_version', 'task_dir', 'work_dir', 'sources',
                           'manifest_sha256', 'matrix', 'parent_inventory'},
            'tuple', tuple_path, 'incomplete or unknown tuple fields')
    require(value['schema_version'] == 1 and value['task_dir'] == str(task)
            and value['work_dir'] == str(work), 'tuple', tuple_path, 'schema/task/work mismatch')
    sources = value['sources']
    require(isinstance(sources, dict) and set(sources) == {*SOURCES, 'agent_base'},
            'tuple', tuple_path, 'full five-source tuple and Agent base required')
    for name, source in sources.items():
        require(isinstance(source, dict) and set(source) == {'root', 'sha', 'tree'},
                'tuple', name, 'root/sha/tree required')
        root = exact_path(source['root'], kind='directory')
        require(all(isinstance(source[k], str) and re.fullmatch('[0-9a-f]{40}', source[k])
                    for k in ('sha', 'tree')), 'tuple', name, 'full lowercase commit/tree ids required')
        commit = source['sha']
        require(git(root, 'cat-file', '-t', commit).strip() == b'commit',
                'source', root, 'selected object must be a commit')
        require(git(root, 'rev-parse', commit + '^{tree}').decode().strip() == source['tree'],
                'source', root, 'frozen tree mismatch')
        if name != 'agent_base':
            require(git(root, 'rev-parse', 'HEAD').decode().strip() == commit
                    and not git(root, 'status', '--porcelain', '--untracked-files=all').strip(),
                    'source', root, 'stale HEAD or dirty selected source')
    agent, runtime, base = (sources[n] for n in ('agent', 'runtime', 'agent_base'))
    lock = json.loads(blob(agent, 'runtime.lock.json'))
    require(lock['source_commit'] == runtime['sha'], 'lock', agent['root'], 'Runtime source pin differs')
    if agent['sha'] != base['sha']:
        require(git(agent['root'], 'show', '-s', '--format=%P', agent['sha']).decode().strip() == base['sha'],
                'lock', agent['root'], 'aligned Agent must be a single child of base')
        require(git(agent['root'], 'diff', '--name-only', base['sha'], agent['sha']).strip() == b'runtime.lock.json',
                'lock', agent['root'], 'alignment changes more than runtime.lock.json')
        old_lock = json.loads(blob(base, 'runtime.lock.json'))
        require({k:v for k,v in old_lock.items() if k != 'source_commit'} ==
                {k:v for k,v in lock.items() if k != 'source_commit'},
                'lock', agent['root'], 'alignment changed lock facts beyond source_commit')
    for relative, row in lock['files'].items():
        raw = blob(runtime, row['from'])
        require(raw == blob(agent, relative) and 'sha256:' + hashlib.sha256(raw).hexdigest() == row['sha256'],
                'lock', relative, 'vendored Runtime bytes or pin differ')
    install = exact_path(str(work / 'install'), kind='directory')
    program = exact_path(str(install / 'program'), kind='directory')
    selected = exact_path(str(program / 'current'), kind='file').read_text().strip()
    require(selected and Path(selected).name == selected and selected not in {'.', '..'},
            'installed', program / 'current', 'invalid version selector')
    version = exact_path(str(program / 'versions' / selected), kind='directory')
    require(len(list((program / 'versions').iterdir())) == 1,
            'installed', program / 'versions', 'exactly one test-owned version required')
    manifest_path = exact_path(str(version / 'manifest.json'), kind='file')
    require(sha(manifest_path) == value['manifest_sha256'], 'manifest', manifest_path, 'manifest digest mismatch')
    manifest = read(manifest_path)
    require(all(manifest.get(n, {}).get('sha') == sources[n]['sha'] for n in SOURCES),
            'manifest', manifest_path, 'installed source tuple differs')
    pures = list((version / 'py/lib').glob('python*/site-packages'))
    require(len(pures) == 1, 'installed', version, 'one installed purelib required')
    purelib = exact_path(str(pures[0]), kind='directory')
    require((version / 'py/bin/python').is_file(), 'installed', version, 'installed Python absent')
    boundary = {}
    for name, relatives in BOUNDARIES.items():
        for relative in relatives:
            expected = hashlib.sha256(blob(sources[name], relative)).hexdigest()
            installed = exact_path(str(purelib / relative), kind='file')
            require(sha(installed) == expected, 'source-inventory', installed, 'installed bytes differ from frozen Git blob')
            boundary[relative] = {'source': name, 'sha256': expected}
    matrix = value['matrix']
    require(set(matrix) == {'shipped_sha256', 'selected_path', 'selected_sha256', 'added_row'},
            'matrix', tuple_path, 'incomplete matrix review')
    shipped = blob(agent, 'config/astrid-beta-capabilities.json')
    require(hashlib.sha256(shipped).hexdigest() == matrix['shipped_sha256'],
            'matrix', agent['root'], 'shipped matrix pin mismatch')
    selected_path = exact_path(matrix['selected_path'], kind='file')
    require(selected_path.is_relative_to(task) and sha(selected_path) == matrix['selected_sha256'],
            'matrix', selected_path, 'selected matrix must be task-owned and pinned')
    original, selected_matrix = json.loads(shipped), read(selected_path)
    row = matrix['added_row']
    require(set(row) == {'id', 'disposition', 'evidence_reason', 'adapter_family', 'resource_keys'}
            and row['id'] == 'installed_canary.parent' and row['disposition'] == 'optional'
            and isinstance(row['evidence_reason'], str) and row['evidence_reason'].strip()
            and row['adapter_family'] == 'cpu' and row['resource_keys'] == ['cpu'],
            'matrix', selected_path, 'reviewed optional CPU parent row required')
    prepare = [r for r in original['capabilities'] if r.get('id') == 'h3_av.prepare']
    require(len(prepare) == 1 and prepare[0].get('disposition') in {'optional', 'required'}
            and prepare[0].get('adapter_family') == 'cpu' and prepare[0].get('resource_keys') == ['cpu'],
            'matrix', selected_path, 'H3 prepare must be an advertised executable CPU route')
    expected_matrix = {**original, 'capabilities': [*original['capabilities'], row]}
    require(selected_matrix == expected_matrix
            and all(r['id'] != row['id'] for r in original['capabilities']),
            'matrix', selected_path, 'shipped rows/fields must be preserved exactly')
    install_record = read(exact_path(str(program / 'install.json'), kind='file'))
    require(install_record.get('data_root') == str(install / 'data'),
            'installed', program / 'install.json', 'installer data root is not selected test state')
    profile_path = exact_path(str(version / 'profile.json'), kind='file')
    profile = read(profile_path)
    worker_path = exact_path(profile['worker_profile'], kind='file')
    declaration_path = exact_path(profile['service_declaration'], kind='file')
    require(worker_path.is_relative_to(version) and declaration_path.is_relative_to(version)
            and profile['source_checkout'] == str(purelib)
            and profile['runtime_environment'] == str(version / 'py'),
            'installed-profile', profile_path, 'selected profile paths/identity differ')
    worker = read(worker_path)
    require(worker['execution_mode'] == 'host_only_cpu' and type(worker['max_concurrency']) is int
            and 2 <= worker['max_concurrency'] <= 16 and worker['source_checkout'] == str(purelib),
            'installed-profile', worker_path, 'installed CPU source/concurrency mismatch')
    pack_root = exact_path(worker['pack_root'], kind='directory')
    boot = exact_path(worker['boot_manifest_path'], kind='file')
    require(pack_root.is_relative_to(purelib) and boot.is_relative_to(install / 'data/runtime')
            and isinstance(worker['boot_manifest_hash'], str)
            and re.fullmatch('sha256:[0-9a-f]{64}', worker['boot_manifest_hash']),
            'installed-profile', worker_path, 'test-owned pack/boot path and boot pin required')
    environment = worker['environment']
    require(isinstance(environment, dict) and not set(environment) - {
            'PATH', 'LANG', 'LC_ALL', 'LC_CTYPE', 'TMPDIR', 'TEMP', 'TMP', 'XDG_RUNTIME_DIR'}
            and all(isinstance(v, str) for v in environment.values()),
            'installed-profile', worker_path, 'worker environment grants undeclared authority')
    installed_matrix = exact_path(worker['capability_matrix'], kind='file')
    require(installed_matrix.is_relative_to(version) and sha(installed_matrix) == matrix['shipped_sha256'],
            'matrix', installed_matrix, 'installed shipped matrix differs')
    declaration = read(declaration_path)
    hosts = [r for r in declaration['services'] if r.get('name') == 'pack-host']
    require(len(hosts) == 1 and hosts[0]['argv'][-1] == '--verified-local-worker'
            and hosts[0]['ready']['file'] == 'worker-astrid-ready.json',
            'installed-profile', declaration_path, 'verified local host declaration required')
    discovery_path = exact_path(str(install / 'data/runtime/discovery.json'), kind='file')
    discovery = read(discovery_path)
    credential = exact_path(discovery['credential_file'], kind='file')
    endpoint = urlsplit(discovery['endpoint'])
    require(credential.is_relative_to(install / 'data/runtime')
            and endpoint.scheme in {'http', 'https'} and endpoint.hostname in {'127.0.0.1', '::1', 'localhost'}
            and endpoint.username is None and endpoint.password is None
            and isinstance(discovery.get('active_realm'), str) and discovery['active_realm'].strip(),
            'discovery', discovery_path, 'local endpoint, selected credentials and active realm required')
    exact_path(str(install / 'bin/uv'), kind='file')
    require(not (install / 'canary-pack').exists(), 'inputs', install / 'canary-pack',
            'stale test pack; fresh installation state required')
    require(not fresh or not (work / 'contracts').exists(), 'inputs', work / 'contracts',
            'stale contracts state; use fresh test-owned work state')
    require(value['parent_inventory'] == inventory(ASSETS),
            'pack-inventory', ASSETS, 'reviewed exact parent bytes/membership mismatch')
    # These aliases could otherwise redirect capture or writes after input validation.
    for relative in ('data', 'data/runtime', 'home', 'tmp', 'xdg-data', 'xdg-state',
                     'xdg-config', 'xdg-cache', 'canary-pack', 'canary.log'):
        exact_path(str(install / relative))
    exact_path(str(work / 'contracts'))
    exact_path(str(task / 'logs'))
    exact_path(str(task / 'logs/installed-canary'))
    require(not fresh or not (task / 'logs/installed-canary').exists(), 'inputs', task,
            'evidence directory exists; use a fresh task/run, never overwrite stale receipts')
    receipt = read(exact_path(str(task / 'logs/installer-exit.json'), kind='file'))
    require(type(receipt.get('exit')) is int, 'inputs', task, 'installer receipt requires integer exit')
    return Context(tuple_path, task, work, value, version, purelib, boundary)


def validate(tuple_path, task_dir, work_dir, *, fresh=True):
    try:
        return _validate(tuple_path, task_dir, work_dir, fresh=fresh)
    except GateError:
        raise
    except (OSError, ValueError, TypeError, KeyError, IndexError, AttributeError) as exc:
        raise GateError(f'stage=inputs path={tuple_path}: {type(exc).__name__}: {exc}') from exc
