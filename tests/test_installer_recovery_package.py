"""The operator package forwards only explicit recovery to a pinned new CLI."""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from pathlib import Path

import pytest

SOURCE = Path(__file__).resolve().parents[1] / 'scripts/operator_recovery'


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def test_launcher_sources_have_exact_pins_and_native_command_files():
    script = (SOURCE / 'Invoke-AstridRecovery.ps1').read_text()
    for name, length in [('AgentSha', 40), ('RuntimeSha', 40), ('BootstrapHash', 64), ('ManifestHash', 64)]:
        assert re.search(rf"\$Expected{name} = '[0-9a-f]{{{length}}}'", script)
    assert '__AGENT_' not in script and '__MANIFEST_' not in script
    for name, action in [('0-Inspect.cmd', 'status'), ('1-Prepare.cmd', 'prepare'), ('2-Resume.cmd', 'resume')]:
        value = (SOURCE / name).read_bytes()
        assert b'\r\n' in value and b'\\r\\n' not in value
        assert b'\n' not in value.replace(b'\r\n', b'')
        assert f'-Action {action}'.encode() in value
        assert b'%~dp0Invoke-AstridRecovery.ps1' in value
        assert b'exit /b %recovery_exit%' in value


@pytest.fixture
def native_package(tmp_path):
    """Real PS5.1 launcher, isolated bootstrap stub; never runs managed Astrid."""
    powershell = Path(os.environ['SystemRoot']) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
    assert powershell.is_file()
    package = tmp_path / "Operator's $HOME recovery package"
    (package / 'bootstrap').mkdir(parents=True)
    source = (SOURCE / 'Invoke-AstridRecovery.ps1').read_text()
    pins = {name: re.search(rf"\$Expected{name}Sha = '([0-9a-f]{{40}})'", source).group(1)
            for name in ('Agent', 'Runtime')}
    bootstrap = package / 'bootstrap/install.ps1'
    bootstrap.write_text("Write-Output ('FORWARDED:' + (ConvertTo-Json -Compress -InputObject @($args)))\n"
                         "if ($env:ASTRID_TEST_BOOTSTRAP_EXIT) { exit ([int]$env:ASTRID_TEST_BOOTSTRAP_EXIT) }\nexit 0\n")
    manifest = package / 'recovery-manifest.json'
    manifest.write_text(json.dumps({'agent': {'sha': pins['Agent']}, 'runtime': {'sha': pins['Runtime']}}))
    source = re.sub(r"\$ExpectedBootstrapHash = '[0-9a-f]{64}'",
                    "$ExpectedBootstrapHash = '" + digest(bootstrap.read_bytes()) + "'", source)
    source = re.sub(r"\$ExpectedManifestHash = '[0-9a-f]{64}'",
                    "$ExpectedManifestHash = '" + digest(manifest.read_bytes()) + "'", source)
    wrapper = package / 'Invoke-AstridRecovery.ps1'
    wrapper.write_text(source)
    root = tmp_path / "Existing Astrid's program"
    root.mkdir()
    # The old selected command must never be called by this launch chain.
    old = root / 'run-astrid.cmd'
    old.write_text('@echo OLD_SELECTED_CLI_WAS_CALLED\nexit /b 99\n')
    return powershell, package, wrapper, root, old.read_bytes()


def run_package(fixture, action='status', *, extra_env=None, root=None):
    powershell, _package, wrapper, program, _old = fixture
    result = subprocess.run([str(powershell), '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
                             '-File', str(wrapper), '-Action', action, '-ProgramRoot', str(root or program)],
                            env={**os.environ, **(extra_env or {})}, stdin=subprocess.DEVNULL,
                            capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=30)
    assert 'OLD_SELECTED_CLI_WAS_CALLED' not in result.stdout + result.stderr
    assert sorted(path.name for path in program.iterdir()) == ['run-astrid.cmd']
    assert (program / 'run-astrid.cmd').read_bytes() == fixture[-1]
    return result


@pytest.mark.windows_native
def test_native_ps51_has_wrapper_and_bootstrap_command_surface(native_package):
    # Preserve the inherited environment: PS7's module path previously hid the
    # Get-FileHash function from PS5.1. The remaining entrypoint surface uses
    # compiled cmdlets, including the real pinned bootstrap's preparation path.
    commands = ['Test-Path', 'Get-Item', 'Get-Content', 'ConvertFrom-Json', 'ConvertTo-Json',
                'Write-Host', 'Write-Output', 'New-Object', 'Join-Path', 'Split-Path', 'Get-Acl', 'Add-Type',
                'Get-ExecutionPolicy', 'Where-Object', 'Invoke-WebRequest',
                'Remove-Item', 'Out-Null', 'Out-String', 'Start-Sleep']
    names = ','.join("'" + name + "'" for name in commands)
    script = ("$ErrorActionPreference='Stop'; "
              "if ($PSVersionTable.PSVersion.Major -ne 5 -or $PSVersionTable.PSVersion.Minor -ne 1) { throw 'PS5.1 required' }; "
              f"foreach ($name in @({names})) {{ "
              "$command = Get-Command -Name $name -CommandType Cmdlet -ErrorAction Stop; "
              "[Console]::WriteLine($command.Name) }")
    result = subprocess.run([str(native_package[0]), '-NoProfile', '-NonInteractive', '-Command', script],
                            env=dict(os.environ), stdin=subprocess.DEVNULL, capture_output=True,
                            text=True, encoding='utf-8', errors='replace', timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.splitlines() == commands


@pytest.mark.windows_native
@pytest.mark.parametrize('action', ['status', 'prepare', 'resume'])
def test_native_package_forwards_only_explicit_recovery_to_new_bootstrap(native_package, action):
    result = run_package(native_package, action)
    assert result.returncode == 0, result.stdout + result.stderr
    forwarded = next(line.removeprefix('FORWARDED:') for line in result.stdout.splitlines() if line.startswith('FORWARDED:'))
    args = json.loads(forwarded)
    assert args == ['--manifest', str(native_package[1] / 'recovery-manifest.json'),
                    '--root', str(native_package[3]), '--recover', action]


@pytest.mark.windows_native
@pytest.mark.parametrize('tamper', ['bootstrap', 'manifest', 'pins', 'action', 'relative-root'])
def test_native_package_refuses_bad_inputs_before_launch(native_package, tamper):
    _powershell, package, wrapper, _root, _old = native_package
    expected = {
        'bootstrap': 'Package checksum mismatch: install.ps1. Nothing was launched.',
        'manifest': 'Package checksum mismatch: recovery-manifest.json. Nothing was launched.',
        'pins': 'Package source pins differ from this reviewed recovery build. Nothing was launched.',
        'action': 'Cannot validate argument on parameter',
        'relative-root': 'ProgramRoot must be an absolute local Windows path.',
    }[tamper]
    if tamper == 'bootstrap':
        (package / 'bootstrap/install.ps1').write_text('throw "unreviewed bootstrap must never execute"')
    elif tamper == 'manifest':
        (package / 'recovery-manifest.json').write_text('{}')
    elif tamper == 'pins':
        manifest = package / 'recovery-manifest.json'
        value = json.loads(manifest.read_text())
        value['agent']['sha'] = 'f' * 40
        manifest.write_text(json.dumps(value))
        # Check pin validation independently after a valid checksum.
        wrapper.write_text(re.sub(r"\$ExpectedManifestHash = '[0-9a-f]{64}'",
            "$ExpectedManifestHash = '" + digest(manifest.read_bytes()) + "'", wrapper.read_text()))
    result = run_package(native_package, 'uninstall' if tamper == 'action' else 'status',
                         root='relative-root' if tamper == 'relative-root' else None)
    assert result.returncode != 0
    assert 'FORWARDED:' not in result.stdout
    assert expected in re.sub(r'\s+', ' ', result.stdout + result.stderr)


@pytest.mark.windows_native
def test_native_package_preserves_bootstrap_failure_exit(native_package):
    result = run_package(native_package, extra_env={'ASTRID_TEST_BOOTSTRAP_EXIT': '37'})
    assert result.returncode == 37
    assert 'FORWARDED:' in result.stdout
