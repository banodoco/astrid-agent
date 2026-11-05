"""Test-only Linux Pinokio lifecycle row for the shared row driver.

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
import time
import urllib.request


class RowError(RuntimeError):
    pass


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def run_process(argv, *, env, cwd, timeout=900):
    # Never terminate a process on timeout: preserve evidence and stop authority.
    proc = subprocess.Popen(list(map(str, argv)), env=env, cwd=cwd,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        out, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise RowError(f"Command timed out (pid {proc.pid} retained): {argv[0]}") from exc
    if proc.returncode:
        raise RowError(f"Command failed (exit {proc.returncode}): {argv[0]}: {out[-8000:]}")
    return out


def other_runtimes(session_pid):
    """Snapshot Linux Runtime owners by PID, birth tick and command line."""
    records = {}
    parents = {}
    for proc in Path('/proc').iterdir():
        if not proc.name.isdigit():
            continue
        try:
            stat = (proc / 'stat').read_text()
            fields = stat[stat.rfind(')') + 2:].split()
            parents[int(proc.name)] = int(fields[1])
            cmd = (proc / 'cmdline').read_bytes().replace(b'\0', b' ').decode('utf-8', 'replace')
            if ('-m runtime_protocol start' in cmd or
                ('from astrid_runtime.cli import main' in cmd and re.search(r'main\(\s*\[\s*[\'"]run[\'"]', cmd))):
                records[int(proc.name)] = (fields[19], cmd, proc)
        except FileNotFoundError:
            continue
        except PermissionError as exc:
            raise RowError(f'Cannot inspect process {proc.name}: {exc}') from exc
    def owned(pid):
        seen = set()
        while pid and pid not in seen:
            if pid == session_pid:
                return True
            seen.add(pid)
            pid = parents.get(pid, 0)
        return False
    snapshot = {}
    for pid, (birth, cmd, proc) in records.items():
        if owned(pid):
            continue
        try:
            environ = dict(entry.split(b'=', 1) for entry in (proc / 'environ').read_bytes().split(b'\0') if b'=' in entry)
            data = environ.get(b'ASTRID_RUNTIME_DATA_ROOT') or environ.get(b'ASTRID_SUPPORT_ROOT')
            if not data:
                match = re.search(r'(?:^|\s)--data-root(?:=|\s+)([^\s]+)', cmd)
                data = os.fsencode(match.group(1)) if match else None
            if not data or not Path(os.fsdecode(data)).is_absolute():
                raise RowError(f'Cannot identify other Runtime data root for pid {pid}')
            discovery = Path(os.fsdecode(data)) / 'runtime/discovery.json'
            content = discovery.read_bytes()
            selected = json.loads(content)
            if not selected.get('runtime_instance_id'):
                raise RowError(f'Cannot identify other Runtime instance for pid {pid}')
            snapshot[pid] = (birth, cmd, os.fsdecode(data), hashlib.sha256(content).hexdigest())
        except (OSError, ValueError) as exc:
            raise RowError(f'Cannot inspect other Runtime pid {pid}: {exc}') from exc
    return snapshot


class LinuxRow:
    def __init__(self, config):
        self.config = config
        self.root = Path(config['install_root'])
        self.pinokio = Path(config['pinokio_root'])
        self.app = self.root.parent.parent
        self.manifest = Path(config['manifest'])
        self.standin = Path(config['standin_manifest'])
        self.installer = self.manifest.parent / 'install'
        self.work = self.standin.parent / 'pinokio-linux-row'
        self.input_manifest = self.work / 'install-manifest.json'
        self.task = config['task_command']
        self.session = None
        self.data = None
        self.identity = None
        self.marker = None
        self.env = None

    def command(self, *args, timeout=900):
        return run_process(args, env=self.env, cwd=self.env['HOME'], timeout=timeout)

    def prepare(self):
        if sys.platform != 'linux' or self.config.get('row') != 'pinokio-linux':
            raise RowError('Expected the pinokio-linux row on Linux')
        for key in ('agent_root', 'pinokio_root', 'install_root', 'manifest', 'standin_manifest'):
            if not isinstance(self.config.get(key), str) or not Path(self.config[key]).is_absolute():
                raise RowError(f'{key} must be an absolute path')
        if self.root.resolve() != self.root or self.work.resolve() != self.work:
            raise RowError('Install and row work paths must be canonical and contain no symlinks')
        if self.root.name != 'astrid' or self.root.parent.name != 'app' or self.app.parent.name != 'api':
            raise RowError('install_root must be <Pinokio home>/api/<app>/app/astrid')
        if not isinstance(self.task, list) or not self.task or not all(isinstance(x, str) and x for x in self.task):
            raise RowError('task_command must be a non-empty argv list')
        if not Path(self.config['agent_root']).is_dir() or not self.installer.is_file():
            raise RowError(f'Agent checkout or candidate installer missing: {self.installer}')
        initial, standin = read_json(self.manifest), read_json(self.standin)
        if initial.get('release') != 1 or standin.get('release') != 2 or not standin.get('qualification_marker'):
            raise RowError('Expected Release 1 and generated Release 2 stand-in manifests')
        if self.root.parent.exists() or self.work.exists():
            raise RowError('Program directory or row work directory already exists; refusing reuse')
        if self.standin.resolve() == self.manifest.resolve():
            raise RowError('Stand-in and candidate manifest must be distinct')
        self.work.mkdir(mode=0o700)
        home = self.work / 'home'
        home.mkdir(mode=0o700)
        self.input_manifest.write_bytes(self.manifest.read_bytes())
        self.app.mkdir(parents=True, exist_ok=True)
        inherited = dict(os.environ, HOME=str(home))
        code = "const p=require(process.argv[1]); console.log(JSON.stringify(p.buildEnvironment({appDirectory:process.argv[2]})))"
        out = run_process(['node', '-e', code, self.pinokio / 'pinokio/environment.js', self.app], env=inherited, cwd=home, timeout=30)
        self.env = json.loads(out)
        if self.env.get('HOME') != str(home):
            raise RowError('Candidate environment rejected isolated HOME')
        if self.root.exists():
            raise RowError('Install root already exists; refusing to modify it')

    def install(self, manifest=None, uninstall=False):
        args = ['sh', self.installer, '--root', self.root, '--managed-by', 'pinokio']
        if uninstall:
            args.extend(['--uninstall', '--manifest', self.input_manifest])
        else:
            if manifest is not None:
                self.input_manifest.write_bytes(Path(manifest).read_bytes())
            args.extend(['--manifest', self.input_manifest])
        try:
            self.command(*args)
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
        data, version, record = self.record()
        prior = self.identity
        if self.data is not None and data != self.data:
            raise RowError('Selected data root changed before restart')
        self.data = data
        node = Path(record['env']['ASTRID_NODE_EXECUTABLE'])
        if not node.is_absolute() or not node.is_relative_to(version):
            raise RowError('Pinned Node is outside the selected version')
        log = self.work / 'session.log'
        with log.open('w', encoding='utf-8') as output:
            self.session = subprocess.Popen([str(node), str(self.pinokio / 'pinokio/session.mjs'), 'serve', str(self.root), str(self.root.parent / 'editor'), 'backend'], env=self.env, cwd=self.env['HOME'], stdout=output, stderr=subprocess.STDOUT)
        deadline = time.monotonic() + 180
        last = None
        while time.monotonic() < deadline:
            if self.session.poll() is not None:
                raise RowError(f'Candidate session exited {self.session.returncode} before readiness: {log.read_text(encoding="utf-8", errors="replace")[-8000:]}')
            try:
                observed = self.health()
                if prior and observed['realm_root'] != prior['realm_root']:
                    raise RowError('Restart selected a different workspace')
                if 'ASTRID_PINOKIO_READY' not in log.read_text(encoding='utf-8', errors='replace'):
                    raise RowError('Launcher readiness sentinel not emitted')
                control = read_json(self.root / 'pinokio-control.json')
                if control.get('pid') != self.session.pid or not control.get('token') or not control.get('port'):
                    raise RowError('Controller does not identify the child session')
                self.identity = observed
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
        if (not instance or health.get('status') != 'ok' or health.get('runtime_instance_id') != instance
                or not isinstance(identity, dict) or identity.get('managed_by') != 'pinokio'
                or health.get('identity') != identity):
            raise RowError('Runtime health, release identity and selected discovery do not match')
        realm = Path(discovery['realm_root'])
        if not realm.is_absolute() or not realm.is_relative_to(self.data.parent):
            raise RowError('Runtime realm is outside selected data root')
        return discovery

    def task_env(self):
        code = "const m=await import(process.argv[1]); console.log(JSON.stringify(m.installation(process.argv[2], {appDirectory:process.argv[3]}).env))"
        return json.loads(self.command('node', '--input-type=module', '-e', code, (self.pinokio / 'pinokio/session.mjs').as_uri(), self.root, self.app, timeout=30))

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
        before_record = self.record()
        data = before_record[0]
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
        if self.record() != before_record:
            raise RowError('Selected install root or version identity changed during stop')
        discovery_path = data / 'runtime/discovery.json'
        if discovery_path.exists() and read_json(discovery_path).get('runtime_instance_id') not in (None, before['runtime_instance_id']):
            raise RowError('A different Runtime appeared at the selected root during stop')
        if other_runtimes(self.session.pid) != others:
            raise RowError('Another Runtime process changed across stop')

    def busy_stop(self):
        realm = Path(self.identity['realm_root'])
        self.marker = realm / 'pinokio-linux-row-marker.txt'
        with self.marker.open('x', encoding='utf-8') as output:
            output.write('pinokio linux row data\n')
        log = self.work / 'busy-task.jsonl'
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
        if not self.marker or self.marker.read_text(encoding='utf-8') != 'pinokio linux row data\n':
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

    def launcher_call(self, body, *args):
        code = "const lib=require(process.argv[1]); const app=lib.createApp({appDirectory:process.argv[2]}); " + body
        return self.command('node', '-e', code, self.pinokio / 'pinokio/lib.js', self.app, *args, timeout=90)

    def install_initial(self):
        self.launcher_call("app.ensureDoorPortFree().catch(e=>{console.error(e); process.exitCode=1})")
        self.install()
        self.data, _, record = self.record()
        if record.get('identity') != {'release': 1, 'managed_by': 'pinokio'}:
            raise RowError('Installed release or manager identity differs from candidate')
        self.launcher_call("app.saveModeStep('backend').method().catch(e=>{console.error(e); process.exitCode=1})")

    def update(self):
        self.launcher_call("app.preflightStop().catch(e=>{console.error(e); process.exitCode=1})")
        self.install(self.standin)

    def uninstall(self):
        # Session was stopped by the same graceful sequence immediately above.
        snapshot = self.launcher_call("console.log(JSON.stringify(app.assertSafeRemoval()))").strip()
        self.launcher_call("app.stopForUninstall({},null,{api:{running:{}}}).catch(e=>{console.error(e); process.exitCode=1})")
        self.launcher_call("app.assertSafeRemoval(JSON.parse(process.argv[3])); app.ensureDoorPortFree().catch(e=>{console.error(e); process.exitCode=1})", snapshot)
        self.install(uninstall=True)
        self.launcher_call("app.finishUninstall(JSON.parse(process.argv[3]))", snapshot)

    def cleanup_check(self):
        if self.root.exists() and any((self.root / name).exists() for name in ('current', 'install.json', 'versions', 'run')):
            raise RowError('Uninstall left program files or installation identity')
        if not self.marker or self.marker.read_text(encoding='utf-8') != 'pinokio linux row data\n':
            raise RowError('Uninstall did not preserve the workspace marker')


def run(config):
    """Return ordered driver stage objects, stopping at the first failure."""
    steps = []
    try:
        row = LinuxRow(config)
        stages = [
            ('install', [('prepare', row.prepare), ('install', row.install_initial)]),
            ('start_health_task', [('start', row.start), ('task', row.task_once)]),
            ('stop_start', [('stop', row.stop), ('restart', row.start)]),
            ('busy_stop', [('busy_stop', row.busy_stop), ('restart_after_busy', row.start), ('data_marker', row.verify_marker)]),
            ('update', [('stop_for_update', row.stop), ('update', row.update)]),
            ('release2_health_task', [('release2_start', row.start), ('release2_readiness_task', row.release2)]),
            ('uninstall_cleanup', [('stop_for_uninstall', row.stop), ('uninstall', row.uninstall), ('cleanup', row.cleanup_check)]),
        ]
        for name, operations in stages:
            for phase, operation in operations:
                try:
                    operation()
                except Exception as exc:
                    steps.append({'name': name, 'status': 'fail', 'error': f'{phase}: {type(exc).__name__}: {exc}'})
                    return steps
            steps.append({'name': name, 'status': 'pass', 'error': None})
    except Exception as exc:
        steps.append({'name': 'install', 'status': 'fail', 'error': f'{type(exc).__name__}: {exc}'})
    return steps
