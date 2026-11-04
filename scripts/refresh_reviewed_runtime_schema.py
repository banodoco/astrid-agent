"""Refresh the strict identity-aware schema row from one installed R1 tree.

See docs/testing/TESTING.md, "Refreshing the reviewed installed identity row".
This reads the selected installation only; it never starts Runtime or edits it.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from astrid.core.generation.vibecomfy_dependency import VIBECOMFY_ENGINE_REVISION
from scripts.verify_installed import installed_runtime_contract, read


RELEASES_PATH = ROOT / 'tests' / 'upgrade' / 'releases.json'
ROW_ID = 'r1-identity-aware-installed-tree'
HEX40 = re.compile(r'[0-9a-f]{40}')


def _git_head(path: Path) -> str:
    try:
        return subprocess.run(['git', '-C', str(path), 'rev-parse', '--verify', 'HEAD^{commit}'],
                              check=True, capture_output=True, text=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ValueError(f'cannot verify installed source checkout at {path}') from exc


def capture(program_root: Path, app_manifest_commit: str) -> tuple[dict, dict]:
    program_root = program_root.resolve(strict=True)
    if not HEX40.fullmatch(app_manifest_commit):
        raise ValueError('an exact 40-character App manifest source commit is required')
    current_file = program_root / 'current'
    selected = current_file.read_text(encoding='utf-8').strip()
    if not selected or selected in ('.', '..') or Path(selected).name != selected:
        raise ValueError('current must contain one version directory name')
    version_root = program_root / 'versions' / selected
    try:
        resolved_version_root = version_root.resolve(strict=True)
    except OSError as exc:
        raise ValueError('current selection does not name an installed version') from exc
    if resolved_version_root.parent != (program_root / 'versions').resolve(strict=True):
        raise ValueError('current selection resolves outside program versions')
    version_path = version_root / 'version.json'
    manifest_path = version_root / 'manifest.json'
    profile_path = version_root / 'profile.json'
    version_bytes = version_path.read_bytes()
    manifest_bytes = manifest_path.read_bytes()
    profile_bytes = profile_path.read_bytes()
    version = json.loads(version_bytes)
    manifest = json.loads(manifest_bytes)
    profile = json.loads(profile_bytes)
    if not isinstance(version, dict) or not isinstance(manifest, dict) or not isinstance(profile, dict):
        raise ValueError('installed manifest, profile, and version record must be JSON objects')

    # Verify the fixed entry point by resolving it without executing it.
    launcher = program_root / 'bin' / 'runtime'
    launcher_target = launcher.resolve(strict=True)
    try:
        launcher_target.relative_to(version_root.resolve(strict=True))
    except ValueError as exc:
        raise ValueError('bin/runtime does not resolve inside the selected version') from exc
    if not launcher.is_file():
        raise ValueError('bin/runtime is not a file')

    if Path(version.get('root', '')).resolve() != version_root.resolve():
        raise ValueError('version record root does not match current selection')
    agent_sha = manifest.get('agent', {}).get('sha')
    runtime_sha = manifest.get('runtime', {}).get('sha')
    if not isinstance(agent_sha, str) or not HEX40.fullmatch(agent_sha):
        raise ValueError('installed manifest has no exact Agent source SHA')
    if not isinstance(runtime_sha, str) or not HEX40.fullmatch(runtime_sha):
        raise ValueError('installed manifest has no exact Runtime source SHA')
    for label, source_path, expected in (
            ('Agent', profile.get('source_checkout'), agent_sha),
            ('Runtime', profile.get('runtime_checkout'), runtime_sha)):
        if not isinstance(source_path, str):
            raise ValueError(f'installed profile has no {label} source checkout')
        source = Path(source_path).resolve(strict=True)
        try:
            source.relative_to(version_root.resolve(strict=True))
        except ValueError as exc:
            raise ValueError(f'installed {label} source checkout is outside selected version') from exc
        if _git_head(source) != expected:
            raise ValueError(f'installed {label} checkout does not match manifest source SHA')

    canonical_manifest = dict(manifest)
    default_vibecomfy = None
    if 'vibecomfy' not in canonical_manifest:
        default_vibecomfy = VIBECOMFY_ENGINE_REVISION
        canonical_manifest['vibecomfy'] = {'sha': default_vibecomfy}
    canonical_bytes = (json.dumps(canonical_manifest, indent=2, sort_keys=True) + '\n').encode()
    if manifest_bytes != canonical_bytes:
        raise ValueError('installed manifest bytes are not the canonical installer representation')

    contract = installed_runtime_contract(
        Path(version['python']), version_root, version.get('env', {}), version_root)
    if contract['protocol'] != manifest.get('runtime', {}).get('protocol', contract['protocol']):
        raise ValueError('installed Runtime protocol does not match manifest')
    schema_digest = contract['schema_digest']
    if manifest.get('runtime', {}).get('schema_digest') != schema_digest:
        raise ValueError('installed Runtime schema digest does not match manifest')

    schema = {
        'id': ROW_ID,
        'mode': 'identity-aware-version-record',
        'reviewed_source_refs': {
            'app_manifest': app_manifest_commit,
            'agent': agent_sha,
            'runtime': runtime_sha,
        },
        'installed_manifest_sha256': hashlib.sha256(manifest_bytes).hexdigest(),
        'install_manifest_schema_version': manifest.get('schema_version'),
        'manifest_release': manifest.get('release', 0),
        'protocol': contract['protocol'],
        'schema_digest': schema_digest,
        'workspace_schema_version': contract['workspace_schema_version'],
        'version_record_keys': sorted(version),
        'health_identity_emitted': True,
        'discovery_identity_emitted': True,
        'installed_layout': {
            'version_root': 'versions/<current>',
            'current_file': 'current',
            'launcher': 'bin/runtime',
            'launcher_target': 'inside-selected-version',
        },
    }
    if default_vibecomfy:
        schema['installed_manifest_vibecomfy_default_sha'] = default_vibecomfy

    evidence = {
        'format': 'astrid-installed-runtime-identity-evidence/v1',
        'app_manifest_commit': app_manifest_commit,
        'program_root': str(program_root),
        'current': selected,
        'selected_version_root': str(version_root.resolve()),
        'launcher': str(launcher),
        'launcher_target': str(launcher_target),
        'manifest_sha256': schema['installed_manifest_sha256'],
        'manifest_agent_sha': agent_sha,
        'manifest_runtime_sha': runtime_sha,
        'runtime_contract': contract,
        'reviewed_schema': schema,
        'raw': {
            'manifest_json_base64': base64.b64encode(manifest_bytes).decode('ascii'),
            'version_json_base64': base64.b64encode(version_bytes).decode('ascii'),
            'profile_json_base64': base64.b64encode(profile_bytes).decode('ascii'),
            'current_bytes_base64': base64.b64encode(current_file.read_bytes()).decode('ascii'),
        },
    }
    return schema, evidence


def update_reviewed_row(schema: dict, releases_path: Path = RELEASES_PATH) -> None:
    releases = read(releases_path)
    rows = releases['format_review']['supported_runtime_schemas']
    matches = [i for i, row in enumerate(rows)
               if row.get('mode') == 'identity-aware-version-record']
    if len(matches) != 1:
        raise ValueError(f'expected one identity-aware reviewed schema row, found {len(matches)}')
    # Preserve the row's review description while replacing every derived pin.
    rows[matches[0]] = schema
    releases['format_review']['workspace_schema_version'] = schema['workspace_schema_version']
    releases_path.write_text(json.dumps(releases, indent=2) + '\n', encoding='utf-8')


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--program-root', type=Path, required=True,
                        help='installed Astrid program root containing current, versions/, and bin/runtime')
    parser.add_argument('--app-manifest-commit', required=True,
                        help='exact App commit that published this installed manifest')
    parser.add_argument('--evidence', type=Path, required=True,
                        help='path for retained raw manifest/profile/version identity evidence')
    parser.add_argument('--releases', type=Path, default=RELEASES_PATH,
                        help=argparse.SUPPRESS)
    args = parser.parse_args()
    try:
        schema, evidence = capture(args.program_root, args.app_manifest_commit)
        args.evidence.parent.mkdir(parents=True, exist_ok=True)
        args.evidence.write_text(json.dumps(evidence, indent=2) + '\n', encoding='utf-8')
        update_reviewed_row(schema, args.releases)
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError,
            subprocess.CalledProcessError) as exc:
        print(f'refresh reviewed Runtime schema: {exc}', file=sys.stderr)
        return 2
    print(f'updated {args.releases} and retained evidence at {args.evidence}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
