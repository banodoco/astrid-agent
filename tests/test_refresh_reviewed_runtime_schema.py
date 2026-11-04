"""The reviewed row generator is deterministic and fails closed on bad layout."""
import json
from pathlib import Path
import subprocess
import sys

import pytest

from astrid.core.generation.vibecomfy_dependency import VIBECOMFY_ENGINE_REVISION


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts' / 'refresh_reviewed_runtime_schema.py'


def _git_checkout(path):
    path.mkdir(parents=True)
    subprocess.run(['git', 'init', '-q', str(path)], check=True)
    subprocess.run(['git', '-C', str(path), 'config', 'user.email', 'fixture@example.invalid'], check=True)
    subprocess.run(['git', '-C', str(path), 'config', 'user.name', 'Fixture'], check=True)
    (path / 'source.txt').write_text('installed source\n')
    subprocess.run(['git', '-C', str(path), 'add', 'source.txt'], check=True)
    subprocess.run(['git', '-C', str(path), 'commit', '-qm', 'installed source'], check=True)
    return subprocess.run(['git', '-C', str(path), 'rev-parse', 'HEAD'], check=True,
                          capture_output=True, text=True).stdout.strip()


def _installed_tree(tmp_path):
    program = tmp_path / 'program'
    version = program / 'versions' / 'build-unique-123'
    (version / 'a').mkdir(parents=True)
    (version / 'bin').mkdir()
    agent_sha = _git_checkout(version / 'a' / 'source')
    runtime_sha = _git_checkout(version / 'r')
    profile = {'source_checkout': str(version / 'a' / 'source'),
               'runtime_checkout': str(version / 'r')}
    (version / 'profile.json').write_text(json.dumps(profile))
    python = version / 'py' / 'bin' / 'python'
    python.parent.mkdir(parents=True)
    contract = {
        'python': str(python), 'python_version': '3.11.13',
        'workspace_schema_version': 33, 'protocol': 'workspace.v1',
        'schema_digest': 'sha256:' + '1' * 64,
        'astrid': str(version / 'site-packages/astrid/__init__.py'),
        'astrid_runtime': str(version / 'site-packages/astrid_runtime/__init__.py'),
        'runtime_protocol': str(version / 'site-packages/runtime_protocol/__init__.py'),
        'astrid_runtime_client': str(version / 'site-packages/astrid_runtime_client/__init__.py'),
    }
    for key in ('astrid', 'astrid_runtime', 'runtime_protocol', 'astrid_runtime_client'):
        Path(contract[key]).parent.mkdir(parents=True, exist_ok=True)
        Path(contract[key]).write_text('# fixture module\n')
    python.write_text(f'#!{sys.executable}\nimport json,sys\nprint(json.dumps({contract!r}))\n')
    python.chmod(0o755)
    manifest = {
        'schema_version': 1,
        'agent': {'sha': agent_sha},
        'runtime': {'sha': runtime_sha, 'protocol': 'workspace.v1',
                    'schema_digest': contract['schema_digest']},
        'tools': {'python': {'version': '3.11.13'}},
        'release': 1,
        'vibecomfy': {'sha': VIBECOMFY_ENGINE_REVISION},
    }
    (version / 'manifest.json').write_text(json.dumps(manifest, indent=2, sort_keys=True) + '\n')
    version_record = {'root': str(version), 'python': str(python),
                      'profile': str(version / 'profile.json'), 'env': {},
                      'identity': {'release': 1, 'managed_by': None}}
    (version / 'version.json').write_text(json.dumps(version_record))
    (program / 'current').write_text(version.name)
    runtime = version / 'bin' / 'runtime'
    runtime.write_text('#!/bin/sh\nexit 0\n')
    (program / 'bin').mkdir()
    fixed_launcher = program / 'bin' / 'runtime'
    fixed_launcher.write_text(
        '#!/bin/sh\nset -eu\n'
        'root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd -P)\n'
        'version=$(cat "$root/current")\n'
        'case "$version" in ""|[!A-Za-z0-9]*|*[!A-Za-z0-9._-]*) exit 2;; esac\n'
        '[ ${#version} -le 64 ] || exit 2\n'
        'exec "$root/versions/$version/bin/runtime" "$@"\n'
    )
    fixed_launcher.chmod(0o755)
    return program


def _command(program, evidence, releases):
    return [sys.executable, str(SCRIPT), '--program-root', str(program),
            '--app-manifest-commit', 'c' * 40, '--evidence', str(evidence),
            '--releases', str(releases)]


def test_generator_command_is_reproducible_and_retains_raw_evidence(tmp_path):
    program = _installed_tree(tmp_path)
    evidence = tmp_path / 'identity-evidence.json'
    releases = json.loads((ROOT / 'tests/upgrade/releases.json').read_text())
    releases_path = tmp_path / 'releases.json'
    releases_path.write_text(json.dumps(releases, indent=2) + '\n')
    command = _command(program, evidence, releases_path)
    first = subprocess.run(command, cwd=ROOT, capture_output=True, text=True)
    assert first.returncode == 0, first.stderr
    evidence_1 = evidence.read_bytes()
    releases_1 = releases_path.read_bytes()
    second = subprocess.run(command, cwd=ROOT, capture_output=True, text=True)
    assert second.returncode == 0, second.stderr
    assert evidence.read_bytes() == evidence_1
    assert releases_path.read_bytes() == releases_1
    captured = json.loads(evidence_1)
    assert captured['manifest_agent_sha']
    assert captured['manifest_runtime_sha']
    assert captured['runtime_contract']['workspace_schema_version'] == 33
    assert captured['raw']['manifest_json_base64']
    assert captured['reviewed_schema']['installed_layout']['launcher_target'] == 'dispatches-current-to-selected-version'


@pytest.mark.posix_native
def test_generator_refuses_wrong_current_and_preserves_reviewed_row(tmp_path):
    program = _installed_tree(tmp_path)
    (program / 'current').write_text('missing-version')
    evidence = tmp_path / 'evidence.json'
    releases = json.loads((ROOT / 'tests/upgrade/releases.json').read_text())
    releases_path = tmp_path / 'releases.json'
    releases_path.write_text(json.dumps(releases, indent=2) + '\n')
    before = releases_path.read_bytes()
    result = subprocess.run(_command(program, evidence, releases_path), cwd=ROOT,
                            capture_output=True, text=True)
    assert result.returncode == 2
    assert 'current' in result.stderr
    assert releases_path.read_bytes() == before
    assert not evidence.exists()


@pytest.mark.posix_native
def test_generator_refuses_wrong_fixed_launcher_dispatch_and_preserves_reviewed_row(tmp_path):
    program = _installed_tree(tmp_path)
    launcher = program / 'bin' / 'runtime'
    launcher.write_text(launcher.read_text().replace(
        'exec "$root/versions/$version/bin/runtime" "$@"',
        'exec "$root/versions/other/bin/runtime" "$@"'))
    evidence = tmp_path / 'evidence.json'
    releases = json.loads((ROOT / 'tests/upgrade/releases.json').read_text())
    releases_path = tmp_path / 'releases.json'
    releases_path.write_text(json.dumps(releases, indent=2) + '\n')
    before = releases_path.read_bytes()
    result = subprocess.run(_command(program, evidence, releases_path), cwd=ROOT,
                            capture_output=True, text=True)
    assert result.returncode == 2
    assert 'current-selection dispatch' in result.stderr
    assert releases_path.read_bytes() == before
    assert not evidence.exists()
