"""Test-only macOS Pinokio lifecycle row for the shared test-only driver.

The installer is the ``install`` sibling of the initial manifest.  The install
root must be <pinokio home>/api/<app>/app/astrid, as in the launcher.
The task command must emit JSON lines with a task id and state. During the busy
case it must emit a running state while still alive and then a terminal state
for the same task. A process exit alone cannot establish task admission.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import urllib.request


class RowError(RuntimeError):
    pass


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def run_process(argv, *, env, cwd, timeout=900):
    # File-backed output remains open in a timed-out child after this runner
    # returns; closing a pipe could otherwise signal it with SIGPIPE.
    with tempfile.NamedTemporaryFile(mode='w+', encoding='utf-8',
                                     dir=env['HOME'], prefix='row-command-', delete=False) as output:
        proc = subprocess.Popen(list(map(str, argv)), env=env, cwd=cwd,
                                stdout=output, stderr=subprocess.STDOUT, text=True)
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            raise RowError(f"Command timed out (pid {proc.pid} retained; log {output.name}): {argv[0]}") from exc
        output.flush()
        output.seek(0)
        out = output.read()
    if proc.returncode:
        raise RowError(f"Command failed (exit {proc.returncode}): {argv[0]}: {out[-8000:]}")
    return out


def other_runtimes(session_pid):
    """Snapshot other Runtime owners, including process creation time and command.

    Fail closed if their data-root discovery cannot be inspected. The session's
    descendants are excluded, so the row-owned Runtime may exit during Stop.
    """
    output = subprocess.check_output(
        ['ps', '-ww', '-axo', 'pid=,ppid=,lstart=,command='], text=True)
    records = {}
    for line in output.splitlines():
        match = re.match(r'^\s*(\d+)\s+(\d+)\s+(\S+\s+\S+\s+\d+\s+\S+\s+\d+)\s+(.*)$', line)
        if match:
            pid, parent, birth, command = match.groups()
            pid = int(pid)
            records[pid] = [int(parent), birth, command]
        elif records:
            records[pid][-1] += '\n' + line
    def owned(pid):
        seen = set()
        while pid and pid not in seen:
            if pid == session_pid:
                return True
            seen.add(pid)
            pid = records.get(pid, [0])[0]
        return False
    result = {}
    for pid, (_, birth, command) in records.items():
        if owned(pid) or not ('-m runtime_protocol start' in command or
            ('from astrid_runtime.cli import main' in command and re.search(r'main\(\s*\[\s*[\'"]run[\'"]', command))):
            continue
        # macOS exposes the environment in ps eww; require a data root and a
        # readable discovery instead of assuming a PID alone proves identity.
        details = subprocess.check_output(['ps', 'eww', '-p', str(pid), '-o', 'command='], text=True)
        match = re.search(r'(?:^|\s)(?:ASTRID_RUNTIME_DATA_ROOT|ASTRID_SUPPORT_ROOT)=([^\s]+)', details)
        if not match:
            raise RowError(f'Cannot identify other Runtime data root for pid {pid}')
        data = Path(match.group(1))
        if not data.is_absolute():
            raise RowError(f'Invalid other Runtime data root for pid {pid}')
        content = (data / 'runtime/discovery.json').read_bytes()
        if not json.loads(content).get('runtime_instance_id'):
            raise RowError(f'Cannot identify other Runtime instance for pid {pid}')
        result[pid] = (birth, command, str(data), hashlib.sha256(content).hexdigest())
    return result


class MacOSRow:
    def __init__(self, config):
        self.config = config
        self.root = Path(config['install_root'])
        self.pinokio = Path(config['pinokio_root'])
        self.app = self.root.parent.parent
        self.manifest = Path(config['manifest'])
        self.standin = Path(config['standin_manifest'])
        self.installer = self.manifest.parent / 'install'
        self.scratch = self.root.parents[4] / 'pinokio-macos-row'
        self.task = config['task_command']
        self.session = None
        self.start_count = 0
        self.data = None
        self.identity = None
        self.marker = None
        self.env = None

    def command(self, *args, timeout=900):
        return run_process(args, env=self.env, cwd=self.env['HOME'], timeout=timeout)

    def prepare(self):
        if sys.platform != 'darwin' or self.config.get('row') != 'pinokio-macos':
            raise RowError('Expected the pinokio-macos row on macOS')
        for key in ('agent_root', 'pinokio_root', 'install_root', 'manifest', 'standin_manifest'):
            if not isinstance(self.config.get(key), str) or not Path(self.config[key]).is_absolute():
                raise RowError(f'{key} must be an absolute path')
        if self.root.name != 'astrid' or self.root.parent.name != 'app' or self.app.parent.name != 'api':
            raise RowError('install_root must be <Pinokio home>/api/<app>/app/astrid')
        if not isinstance(self.task, list) or not self.task or not all(isinstance(x, str) and x for x in self.task):
            raise RowError('task_command must be a non-empty argv list')
        if not Path(self.config['agent_root']).is_dir() or not self.installer.is_file():
            raise RowError(f'Agent checkout or candidate installer missing: {self.installer}')
        initial, standin = read_json(self.manifest), read_json(self.standin)
        if initial.get('release') != 1 or standin.get('release') != 2 or not standin.get('qualification_marker'):
            raise RowError('Expected Release 1 and generated Release 2 stand-in manifests')
        home = self.scratch / 'home'
        if home.exists():
            raise RowError(f'Isolated HOME already exists; refusing reuse: {home}')
        home.mkdir(parents=True)
        if self.root.exists():
            raise RowError('Install root already exists; refusing to modify it')
        self.app.mkdir(parents=True, exist_ok=True)
        inherited = dict(os.environ, HOME=str(home))
        code = "const p=require(process.argv[1]); console.log(JSON.stringify(p.buildEnvironment({appDirectory:process.argv[2]})))"
        out = run_process(['node', '-e', code, self.pinokio / 'pinokio/environment.js', self.app], env=inherited, cwd=home, timeout=30)
        self.env = json.loads(out)
        if self.env.get('HOME') != str(home):
            raise RowError('Candidate environment rejected isolated HOME')

    def install(self, manifest=None, uninstall=False):
        args = ['sh', self.installer, '--root', self.root, '--managed-by', 'pinokio']
        if uninstall:
            args.append('--uninstall')
        else:
            args.extend(['--manifest', manifest or self.manifest])
        try:
            self.command(*args)
            if not uninstall:
                _, version, _ = self.record()
                installed = read_json(version / 'manifest.json')
                expected = read_json(manifest or self.manifest)
                if any(installed.get(key) != value for key, value in expected.items()):
                    raise RowError('Installed manifest differs from requested manifest')
        except RowError as exc:
            log = Path(self.env['HOME']) / '.local/state/astrid-runtime/logs/install.log'
            if log.is_file():
                detail = log.read_text(encoding='utf-8', errors='replace')[-8000:]
                raise RowError(f'{exc}\nInstaller log: {detail}') from exc
            raise

    def record(self):
        install = read_json(self.root / 'install.json')
        if install.get('owner') != 'haus.astrid.runtime' or install.get('managed_by') != 'pinokio':
            raise RowError('Install record does not identify this Pinokio program root')
        data = Path(install['data_root'])
        if not data.is_absolute() or not data.is_relative_to(Path(self.env['HOME'])):
            raise RowError(f'Install data root is outside isolated HOME: {data}')
        current = (self.root / 'current').read_text().strip()
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}', current):
            raise RowError('Invalid current version')
        version = self.root / 'versions' / current
        record = read_json(version / 'version.json')
        if Path(record['root']) != version:
            raise RowError('Version root mismatch')
        return data, version, record

    def start(self):
        self.data, version, record = self.record()
        node = Path(record['env']['ASTRID_NODE_EXECUTABLE'])
        if not node.is_absolute() or not node.is_relative_to(version):
            raise RowError('Pinned Node is outside the selected version')
        self.start_count += 1
        log = self.scratch / f'session-{self.start_count}.log'
        with log.open('w', encoding='utf-8') as output:
            self.session = subprocess.Popen([str(node), str(self.pinokio / 'pinokio/session.mjs'), 'serve', str(self.root), str(self.root.parent / 'editor'), 'backend'], env=self.env, cwd=self.env['HOME'], stdout=output, stderr=subprocess.STDOUT)
        deadline = time.monotonic() + 180
        last = None
        while time.monotonic() < deadline:
            if self.session.poll() is not None:
                raise RowError(f'Candidate session exited {self.session.returncode} before readiness: {log.read_text(encoding="utf-8", errors="replace")[-8000:]}')
            try:
                identity = self.health()
                if 'ASTRID_PINOKIO_READY' not in log.read_text(encoding='utf-8', errors='replace'):
                    raise RowError('Launcher has not emitted its readiness sentinel')
                control = read_json(self.root / 'pinokio-control.json')
                if control.get('pid') != self.session.pid or not control.get('token') or not control.get('port'):
                    raise RowError('Controller does not identify the child session')
                self.identity = identity
                return
            except (OSError, ValueError, KeyError, RowError) as exc:
                last = exc
            time.sleep(.5)
        raise RowError(f'Candidate session did not become ready: {last}')

    def health(self):
        discovery = read_json(self.data / 'runtime/discovery.json')
        request = urllib.request.Request('http://127.0.0.1:17340/api/runtime/v1/health', headers={'Origin': 'https://astrid.haus'})
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=3) as response:
            health = json.load(response)
        instance = discovery.get('runtime_instance_id')
        _, _, version = self.record()
        identity = version.get('identity')
        if (not instance or health.get('status') != 'ok' or health.get('protocol') != 'workspace.v1' or health.get('runtime_instance_id') != instance
                or not isinstance(identity, dict) or identity.get('managed_by') != 'pinokio'
                or health.get('identity') != identity or discovery.get('identity') != identity):
            raise RowError('Runtime health, release identity and selected discovery do not match')
        realm = Path(discovery['realm_root'])
        if not realm.is_absolute() or not realm.is_relative_to(self.data.parent):
            raise RowError('Runtime realm is outside selected data root')
        return discovery

    def task_env(self):
        code = "const m=await import(process.argv[1]); console.log(JSON.stringify(m.installation(process.argv[2]).env))"
        return json.loads(self.command('node', '--input-type=module', '-e', code, (self.pinokio / 'pinokio/session.mjs').as_uri(), self.root, timeout=30))

    @staticmethod
    def task_events(output):
        events = []
        for line in output.splitlines():
            try:
                value = json.loads(line)
            except ValueError:
                continue
            if not isinstance(value, dict):
                continue
            data = value.get('data', value)
            if isinstance(data, dict):
                task_id = data.get('task_id') or data.get('id')
                state = data.get('state') or data.get('status')
                if isinstance(task_id, str) and task_id and isinstance(state, str):
                    events.append((task_id, state))
        return events

    def task_once(self):
        output = run_process(self.task, env=self.task_env(), cwd=self.env['HOME'], timeout=180)
        events = self.task_events(output)
        if not events or events[-1][1] not in ('succeeded', 'completed'):
            raise RowError('Task command did not report a successful terminal task with an id')

    def stop(self):
        before = self.health()
        if before['runtime_instance_id'] != self.identity['runtime_instance_id'] or before['realm_root'] != self.identity['realm_root']:
            raise RowError('Selected Runtime identity changed before stop')
        data, _, _ = self.record()
        if data != self.data:
            raise RowError('Selected data root changed before stop')
        others = other_runtimes(self.session.pid)
        # Invoke the exact launcher functions, including preflight and lock wait.
        code = """const m=await import(process.argv[1]); const root=process.argv[2], appDirectory=process.argv[3]; const o={appDirectory}; await m.preflightStop(root,o); const r=await m.requestGracefulStop(root,o); if(r.absent) throw Error('Active session controller absent'); await m.waitForInstallerLock(root,o);"""
        self.command('node', '--input-type=module', '-e', code, (self.pinokio / 'pinokio/lifecycle.mjs').as_uri(), self.root, self.app, timeout=90)
        try:
            rc = self.session.wait(timeout=15)
        except subprocess.TimeoutExpired as exc:
            raise RowError('Session did not exit after graceful stop; retained') from exc
        if rc != 0:
            raise RowError(f'Session failed after stop: exit {rc}')
        if self.record()[0] != data:
            raise RowError('Selected data root changed during stop')
        discovery_path = data / 'runtime/discovery.json'
        if discovery_path.exists() and read_json(discovery_path).get('runtime_instance_id') not in (None, before['runtime_instance_id']):
            raise RowError('A different Runtime appeared at the selected root during stop')
        if other_runtimes(self.session.pid) != others:
            raise RowError('Another Runtime process changed across stop')

    def busy_stop(self):
        realm = Path(self.identity['realm_root'])
        self.marker = realm / 'pinokio-macos-row-marker.txt'
        self.marker.write_text('pinokio macos row data\n', encoding='utf-8')
        log = self.scratch / 'busy-task.jsonl'
        with log.open('w', encoding='utf-8') as output:
            task = subprocess.Popen(self.task, env=self.task_env(), cwd=self.env['HOME'], stdout=output, stderr=subprocess.STDOUT)
        deadline = time.monotonic() + 30
        task_id = None
        while time.monotonic() < deadline:
            events = self.task_events(log.read_text(encoding='utf-8', errors='replace'))
            if events and events[-1][1] in ('running', 'in_progress') and task.poll() is None:
                task_id = events[-1][0]
                break
            if task.poll() is not None:
                raise RowError(f'Task exited {task.returncode} before busy stop; no busy task observed')
            time.sleep(.1)
        if task_id is None:
            raise RowError(f'Task did not report admission and running state; pid {task.pid} retained')
        self.stop()
        try:
            result = task.wait(timeout=120)
        except subprocess.TimeoutExpired as exc:
            raise RowError(f'Task did not reach a terminal outcome; pid {task.pid} retained') from exc
        if result < 0:
            raise RowError(f'Task terminated by signal {-result}, not a clean outcome')
        events = self.task_events(log.read_text(encoding='utf-8', errors='replace'))
        if not events or events[-1][0] != task_id or events[-1][1] not in ('succeeded', 'completed', 'failed', 'cancelled', 'canceled'):
            raise RowError('Busy task did not report a terminal outcome for the admitted task')

    def verify_marker(self):
        if not self.marker or self.marker.read_text(encoding='utf-8') != 'pinokio macos row data\n':
            raise RowError('Workspace marker did not survive busy stop')
        if self.health()['realm_root'] != str(self.marker.parent):
            raise RowError('Restart selected a different workspace')

    def release2(self):
        _, version, record = self.record()
        installed = read_json(version / 'manifest.json')
        expected = read_json(self.standin)
        if installed != expected or record.get('identity') != {'release': 2, 'managed_by': 'pinokio'}:
            raise RowError('Installed Release 2 manifest or identity differs from stand-in')
        self.health()
        self.verify_marker()
        self.task_once()

    def cleanup_check(self):
        if self.root.exists() and any((self.root / name).exists() for name in ('current', 'install.json', 'versions', 'run')):
            raise RowError('Uninstall left program files or installation identity')
        if not self.marker or self.marker.read_text(encoding='utf-8') != 'pinokio macos row data\n':
            raise RowError('Uninstall did not preserve the workspace marker')


def run(config):
    """Return ordered stage observations, stopping on the first failure."""
    steps = []
    try:
        row = MacOSRow(config)
        stages = [
            ('install', [('prepare', row.prepare), ('install', row.install)]),
            ('start_health_task', [('start', row.start), ('task', row.task_once)]),
            ('stop_start', [('stop', row.stop), ('restart', row.start)]),
            ('busy_stop', [('busy_stop', row.busy_stop), ('restart_after_busy', row.start), ('data_marker', row.verify_marker)]),
            ('update', [('stop_for_update', row.stop), ('update', lambda: row.install(row.standin))]),
            ('release2_health_task', [('release2_start', row.start), ('release2_readiness_task', row.release2)]),
            ('uninstall_cleanup', [('stop_for_uninstall', row.stop), ('uninstall', lambda: row.install(uninstall=True)), ('cleanup', row.cleanup_check)]),
        ]
        for name, operations in stages:
            try:
                for phase, operation in operations:
                    operation()
            except Exception as exc:
                steps.append({'name': name, 'status': 'fail', 'error': f'{phase}: {type(exc).__name__}: {exc}'})
                return steps
            steps.append({'name': name, 'status': 'pass', 'error': None})
    except Exception as exc:
        steps.append({'name': 'install', 'status': 'fail', 'error': f'{type(exc).__name__}: {exc}'})
    return steps
