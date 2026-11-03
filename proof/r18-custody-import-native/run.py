import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import traceback

workspace = Path.cwd()
out = workspace / 'out'
out.mkdir(exist_ok=True)
scratch = workspace / 'scratch'
scratch.mkdir(exist_ok=True)
tmp = scratch / 'tmp'
tmp.mkdir(exist_ok=True)
env = os.environ.copy()
env.update(TMPDIR=str(tmp), PYTHONDONTWRITEBYTECODE='1', PIP_DISABLE_PIP_VERSION_CHECK='1')
expected = {'agent': '23252c5da5b89b87ea38570b4b87b3a66f9e955e',
            'runtime': 'a95e0e836234bc6d27ba557ba1297da95ed55290'}
records = []

def run(name, command, cwd=workspace, extra_env=None):
    e = env.copy()
    if extra_env:
        e.update(extra_env)
    print(json.dumps({'step': name, 'command': list(map(str, command)), 'cwd': str(cwd)}), flush=True)
    p = subprocess.run(list(map(str, command)), cwd=cwd, env=e, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    (out / (name + '.stdout')).write_text(p.stdout)
    (out / (name + '.stderr')).write_text(p.stderr)
    records.append({'step': name, 'command': list(map(str, command)), 'cwd': str(cwd), 'exit': p.returncode})
    (out / 'steps.json').write_text(json.dumps(records, indent=2) + '\n')
    print(json.dumps({'step': name, 'exit': p.returncode}), flush=True)
    return p

try:
    info = {'uid': os.getuid(), 'euid': os.geteuid(), 'platform': platform.platform(),
            'mac_ver': platform.mac_ver(), 'python': sys.version, 'executable': sys.executable}
    (out / 'environment.json').write_text(json.dumps(info, indent=2) + '\n')
    if sys.platform != 'darwin' or os.getuid() == 0 or os.geteuid() == 0:
        raise RuntimeError('requires non-root macOS')
    for name, sha in expected.items():
        p = run('checkout-' + name, ['git', '-C', workspace / name, 'rev-parse', 'HEAD'])
        if p.returncode or p.stdout.strip() != sha:
            raise RuntimeError(name + ' checkout mismatch')
    venv = scratch / 'venv'
    py = venv / 'bin' / 'python'
    if run('venv', [sys.executable, '-m', 'venv', venv]).returncode:
        raise RuntimeError('venv creation failed')
    if run('dependencies', [py, '-m', 'pip', 'install', 'pytest==9.0.2', 'pytest-timeout>=2.4', 'jsonschema>=4', 'PyYAML>=6', 'filelock>=3.13']).returncode:
        raise RuntimeError('dependency installation failed')
    for name in ('runtime', 'agent'):
        if run('install-' + name, [py, '-m', 'pip', 'install', '--no-deps', workspace / name]).returncode:
            raise RuntimeError(name + ' install failed')
    helper = run('installed-helper', [py, '-I', '-B', workspace / 'venue/proof/r18-custody-import-native/helper.py', scratch, workspace / 'agent/astrid/installer/custody.py'])
    selectors = [
        'tests/test_installer_pinokio_custody.py::test_custody_identity_requires_the_same_user_process_incarnation',
        'tests/test_installer_birth_identity.py::test_exact_birth_identity_remains_valid_without_runtime_bridge_call',
        'tests/test_installer_birth_identity.py::test_legacy_linux_bridge_refuses_invalid_owner_coordinates_without_runtime_call',
        'tests/test_installer_birth_identity.py::test_custody_and_generated_launcher_keep_pid_uid_gates_around_birth_bridge',
    ]
    tests = run('focused-controls', [py, '-m', 'pytest', '-p', 'no:cacheprovider', '-q', *selectors],
                cwd=workspace / 'agent', extra_env={'PYTHONPATH': str(workspace / 'agent')})
    if helper.returncode or tests.returncode:
        raise RuntimeError('installed helper or focused controls failed')
except Exception:
    traceback.print_exc()
    sys.exit(1)
