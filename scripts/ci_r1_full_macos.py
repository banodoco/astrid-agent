#!/usr/bin/env python3
"""Run the requested default Agent or Runtime test suite in a fresh runner-owned sandbox."""
import hashlib
import json
import os
import platform
import pwd
import re
import shutil
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path

UID = 503
AGENT = '1f63ae926346118083417eaffb4e9bb7712e1d3f'
RUNTIME = '22b3dc98244fe3cbe8ff65a7b40190758c44e799'


def idle_port(port):
    result = subprocess.run(['/usr/sbin/lsof', '-nP', '-iTCP:' + str(port), '-sTCP:LISTEN'], capture_output=True, text=True, timeout=20)
    return result.returncode == 1 and not result.stdout.strip() and not result.stderr.strip()


def isolated_group(argv, cwd, env, logfile):
    """Stop the new child before exec, prove sole process/session leadership, then resume."""
    shell = 'kill -STOP "$$"; exec "$@"'
    log = logfile.open('wb')
    try:
        proc = subprocess.Popen(['/bin/bash', '-c', shell, 'isolated-test', *argv], cwd=str(cwd), env=env,
                                stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        deadline = time.monotonic() + 20
        while True:
            found, state = os.waitpid(proc.pid, os.WUNTRACED | os.WNOHANG)
            if found and os.WIFSTOPPED(state): break
            if found: raise RuntimeError('Test launcher exited before session verification')
            if time.monotonic() > deadline: raise RuntimeError('Test launcher did not stop for verification')
            time.sleep(0.05)
        if os.getpgid(proc.pid) != proc.pid or os.getsid(proc.pid) != proc.pid:
            raise RuntimeError('Test launcher session is not private')
        table = subprocess.check_output(['/bin/ps', '-axo', 'pid=,pgid='], text=True, timeout=10)
        members = []
        for row in table.splitlines():
            fields = row.split()
            if len(fields) != 2 or not all(part.isdecimal() for part in fields): raise RuntimeError('Process group census malformed')
            if int(fields[1]) == proc.pid: members.append(int(fields[0]))
        if members != [proc.pid]: raise RuntimeError('Test process group is not exclusive')
        os.kill(proc.pid, signal.SIGCONT)
        return proc, log
    except BaseException:
        # Only the direct child created above may be signaled before exec.
        if 'proc' in locals() and proc.poll() is None:
            try: os.kill(proc.pid, signal.SIGKILL)
            except ProcessLookupError: pass
            proc.wait()
        log.close()
        raise


def run_stage(argv, cwd, env, logpath, task, home, *, pytest=False):
    if shutil.disk_usage(home).free < 5_000_000_000: raise RuntimeError('Insufficient disk before stage')
    if pytest:
        proc, log = isolated_group(argv, cwd, env, logpath)
    else:
        log = logpath.open('wb')
        proc = subprocess.Popen(argv, cwd=str(cwd), env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    low_disk = False
    interrupted_at = None
    try:
        while proc.poll() is None:
            if not low_disk and shutil.disk_usage(home).free < 3_000_000_000:
                # The direct Popen handle identifies the child; pytest receives SIGINT to run its teardown.
                try: os.kill(proc.pid, signal.SIGINT if pytest else signal.SIGTERM)
                except ProcessLookupError: pass
                low_disk = True; interrupted_at = time.monotonic()
            elif low_disk and time.monotonic() - interrupted_at > 180:
                # Avoid unverified escalation; preserve evidence and let the job timeout bound the wait.
                time.sleep(5)
            time.sleep(3)
    finally:
        log.close()
    return {'exit': proc.returncode, 'disk_threshold_crossed': low_disk,
            'private_session_verified': bool(pytest)}


def sanitize(data, home, task):
    text = data.decode('utf-8', errors='replace').replace(str(task), '<TASK>').replace(str(home), '<RUNNER_HOME>')
    text = re.sub(r'/Users/[^/\s"\']+', '<REDACTED_HOME>', text)
    text = re.sub(r'\b(?:gh[pousr]_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]+|sk-[A-Za-z0-9_-]+)', '<REDACTED_TOKEN>', text)
    text = re.sub(r'(?i)(bearer\s+)[A-Za-z0-9._~+/-]+', r'\1<REDACTED>', text)
    text = re.sub(r'(?i)(\b[A-Z0-9_]*(?:TOKEN|SECRET|PASSWORD|API_?KEY)[A-Z0-9_]*\s*[:=]\s*)("[^"]*"|\'[^\']*\'|[^\s,;]+)', r'\1<REDACTED>', text)
    text = re.sub(r'(?i)(https?://)[^/\s:@]+(?::[^/\s@]*)?@', r'\1<REDACTED>@', text)
    return text.encode('utf-8')


def stage_logs(logs, output, home, task, status):
    for source in sorted(logs.glob('*.log')):
        if source.is_symlink() or not source.is_file(): continue
        raw = source.read_bytes(); maximum = 24_000_000
        status.setdefault('log_source_sha256', {})[source.name] = hashlib.sha256(raw).hexdigest()
        if len(raw) > maximum:
            raw = raw[:12_000_000] + b'\n<LOG MIDDLE OMITTED>\n' + raw[-12_000_000:]
            status.setdefault('truncated_logs', []).append(source.name)
        (output / source.name).write_bytes(sanitize(raw, home, task))


def task_residuals(task):
    out = subprocess.run(['/bin/ps', '-ww', '-U', str(UID), '-o', 'pid=,command='], capture_output=True, text=True, timeout=20, check=True).stdout
    pids = []
    for line in out.splitlines():
        match = re.match(r'^\s*(\d+)\s+(.*)$', line)
        if match and str(task) in match.group(2): pids.append(int(match.group(1)))
    result = subprocess.run(['/usr/sbin/lsof', '-n', '+D', str(task)], capture_output=True, text=True, timeout=300)
    no_open = result.returncode == 1 and not result.stdout.strip() and not result.stderr.strip()
    return {'task_argument_pids': pids, 'open_file_scan': 'clear' if no_open else 'busy-or-unverified'}


def main():
    suite = os.environ.get('R1_FULL_SUITE', '')
    if suite != 'runtime' or os.getuid() != UID or os.environ.get('RUNNER_NAME') != 'hannah-mac' or sys.platform != 'darwin':
        raise RuntimeError('Unexpected runner or suite')
    home = Path(pwd.getpwuid(UID).pw_dir); temp = Path(os.environ['RUNNER_TEMP'])
    if (temp != home / 'actions-runner/_work/_temp' or temp.is_symlink() or temp.resolve() != temp
            or not temp.is_dir() or temp.stat().st_uid != UID): raise RuntimeError('Unexpected runner temp')
    pyroot = home / 'actions-runner/_work/_tool/Python/3.11.18/arm64'
    bootstrap = pyroot / 'bin/python3.11'
    if (pyroot.resolve() != pyroot or not bootstrap.is_file() or bootstrap.stat().st_uid != UID
            or not bootstrap.resolve().is_relative_to(pyroot)): raise RuntimeError('Runner Python unavailable')
    run_id = os.environ.get('GITHUB_RUN_ID', '') + '-' + os.environ.get('GITHUB_RUN_ATTEMPT', '')
    if not re.fullmatch(r'[0-9]+-[0-9]+', run_id): raise RuntimeError('Invalid run id')
    task = temp / ('r1-full-' + suite + '-' + run_id); task.mkdir(mode=0o700)
    output = temp / ('r1-full-artifact-' + suite); output.mkdir(mode=0o700)
    with open(os.environ['GITHUB_ENV'], 'a') as fh: fh.write('R1_FULL_UPLOAD=' + str(output) + '\n')
    logs = task / 'logs'; logs.mkdir(mode=0o700)
    scratch = task / 'tmp'; scratch.mkdir(mode=0o700)
    sandbox_home = task / 'home'; sandbox_home.mkdir(mode=0o700)
    for sub in ('cache', 'config', 'data', 'state'): (task / sub).mkdir(mode=0o700)
    node = next((p for p in (home / 'actions-runner/externals/node24/bin', home / 'actions-runner/externals/node20/bin')
                 if (p / 'node').is_file() and (p / 'node').stat().st_uid == UID and (p / 'node').resolve().is_relative_to(home)), None)
    env = {'HOME': str(sandbox_home), 'TMPDIR': str(scratch), 'TMP': str(scratch), 'TEMP': str(scratch),
           'XDG_CACHE_HOME': str(task / 'cache'), 'XDG_CONFIG_HOME': str(task / 'config'),
           'XDG_DATA_HOME': str(task / 'data'), 'XDG_STATE_HOME': str(task / 'state'),
           'PATH': ':'.join(([str(node)] if node else []) + ['/usr/bin', '/bin', '/usr/sbin', '/sbin']),
           'GIT_CONFIG_GLOBAL': '/dev/null', 'GIT_CONFIG_SYSTEM': '/dev/null', 'GIT_CONFIG_NOSYSTEM': '1',
           'GIT_TERMINAL_PROMPT': '0', 'GIT_LFS_SKIP_SMUDGE': '1', 'GIT_NO_REPLACE_OBJECTS': '1',
           'PIP_CONFIG_FILE': '/dev/null', 'PIP_NO_INPUT': '1', 'PYTHONNOUSERSITE': '1',
           'PYTEST_ADDOPTS': '--basetemp=' + str(scratch / 'pytest')}
    status = {'status': 'not-run', 'suite': suite, 'runner': 'hannah-mac', 'uid': UID,
              'agent_sha': AGENT, 'runtime_sha': RUNTIME, 'stages': {},
              'pytest_command': 'python -m pytest -q tests/test_db_final_r1_rework.py -k backup tests/test_backup_pinned_sqlite.py', 'pytest_scope': 'targeted backup tests', 'macos_version': platform.mac_ver()[0], 'os_build': platform.platform(),
              'python_source': 'runner-owned Astral Python 3.11.18', 'node': 'runner-owned bundled Node' if node else 'unavailable'}
    try:
        for port in (17340, 5180, 42000):
            if not idle_port(port): raise RuntimeError('Required shared port occupied or unverified: ' + str(port))
        identity = json.loads(subprocess.check_output([str(bootstrap), '-I', '-c',
            "import json,platform,sys; print(json.dumps({'version':platform.python_version(),'arch':platform.machine(),'prefix':sys.prefix}))"],
            env=env, text=True, timeout=20))
        if identity != {'version': '3.11.18', 'arch': 'arm64', 'prefix': str(pyroot)}:
            raise RuntimeError('Runner Python identity differs')
        for name, sha in (('astrid-agent', AGENT), ('astrid-runtime', RUNTIME)):
            dest = task / name
            commands = [('clone', ['/usr/bin/git', '-c', 'core.hooksPath=/dev/null', 'clone', '--depth=1', '--filter=blob:none', '--no-checkout', 'https://github.com/banodoco/' + name + '.git', str(dest)]),
                        ('fetch', ['/usr/bin/git', '-C', str(dest), '-c', 'core.hooksPath=/dev/null', 'fetch', '--depth=1', 'origin', sha]),
                        ('checkout', ['/usr/bin/git', '-C', str(dest), '-c', 'core.hooksPath=/dev/null', 'checkout', '--detach', sha])]
            for phase, command in commands:
                result = run_stage(command, task, env, logs / (name + '-' + phase + '.log'), task, home)
                status['stages'][name + '-' + phase] = result
                if result['exit'] != 0 or result['disk_threshold_crossed']: raise RuntimeError('Source checkout failed: ' + name + '/' + phase)
            actual = subprocess.check_output(['/usr/bin/git', '-C', str(dest), 'rev-parse', 'HEAD'], env=env, text=True, timeout=20).strip()
            if actual != sha: raise RuntimeError('Source checkout SHA differs')
        result = run_stage([str(bootstrap), '-m', 'venv', str(task / 'venv')], task, env, logs / 'venv.log', task, home)
        status['stages']['venv'] = result
        if result['exit'] != 0: raise RuntimeError('Venv creation failed')
        py = task / 'venv/bin/python'; env['PATH'] = str(py.parent) + ':' + env['PATH']
        env['ASTRID_CHECKOUT'] = str(task / 'astrid-agent')
        env['ASTRID_RUNTIME_CHECKOUT'] = str(task / 'astrid-runtime')
        # Agent installs exactly its requested dev extra; the pinned sibling Runtime is importable from source.
        if suite == 'agent': env['PYTHONPATH'] = str(task / 'astrid-runtime')
        cwd = task / ('astrid-agent' if suite == 'agent' else 'astrid-runtime')
        install = [str(py), '-m', 'pip', 'install', '-e', '.[dev]'] if suite == 'agent' else [str(py), '-m', 'pip', 'install', '-e', '../astrid-agent', '-e', '.[test]']
        status['install_command'] = "python -m pip install -e '.[dev]'" if suite == 'agent' else "python -m pip install -e ../astrid-agent -e '.[test]'"
        result = run_stage(install, cwd, env, logs / 'install.log', task, home)
        status['stages']['install'] = result
        if result['exit'] != 0 or result['disk_threshold_crossed']: raise RuntimeError('Requested editable install failed')
        for port in (17340, 5180, 42000):
            if not idle_port(port): raise RuntimeError('Required shared port occupied or unverified: ' + str(port))
        status['stages']['pytest'] = run_stage([str(py), '-m', 'pytest', '-q', 'tests/test_db_final_r1_rework.py', '-k', 'backup', 'tests/test_backup_pinned_sqlite.py'], cwd, env, logs / 'pytest.log', task, home, pytest=True)
        status['status'] = 'completed'
    except Exception as exc:
        allowed = ('Required shared port occupied or unverified:', 'Source checkout failed:', 'Source checkout SHA differs', 'Runner Python identity differs', 'Venv creation failed', 'Requested editable install failed', 'Insufficient disk before stage', 'Test launcher')
        status['error'] = str(exc) if isinstance(exc, RuntimeError) and str(exc).startswith(allowed) else 'Unexpected stage failure'
        status['error_type'] = type(exc).__name__
    finally:
        try: status['cleanup'] = task_residuals(task)
        except Exception: status['cleanup'] = {'open_file_scan': 'unverified', 'task_argument_pids': 'unverified'}
        status['disk_free_after'] = shutil.disk_usage(home).free
        stage_logs(logs, output, home, task, status)
        (output / 'result.json').write_text(json.dumps(status, indent=2, sort_keys=True) + '\n')
        rows = [hashlib.sha256(p.read_bytes()).hexdigest() + '  ' + p.name for p in sorted(output.iterdir()) if p.is_file() and p.name != 'SHA256SUMS']
        (output / 'SHA256SUMS').write_text('\n'.join(rows) + '\n')
        print(json.dumps({'suite': suite, 'status': status['status'], 'pytest': status['stages'].get('pytest'), 'cleanup': status['cleanup']}))
    if status['status'] != 'completed' or status['stages']['pytest']['exit'] != 0 or status['cleanup'] != {'task_argument_pids': [], 'open_file_scan': 'clear'}:
        raise SystemExit(1)


if __name__ == '__main__':
    try: main()
    except Exception:
        print(json.dumps({'status': 'preflight-failed'})); raise SystemExit(1)
