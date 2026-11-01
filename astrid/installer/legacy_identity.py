"""Admission of the one reviewed identityless Release-0 predecessor.

The frozen values are from tests/upgrade/releases.json's served legacy schema;
that test-only manifest is not shipped with an installed Agent.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from collections.abc import Mapping
from importlib import resources
from pathlib import Path

def _policy():
    """Load the closed, shipped predecessor list; malformed data refuses admission."""
    try:
        rows = json.loads(resources.files("astrid.installer").joinpath("predecessors.json").read_text(encoding="utf-8"))
        if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
            return None
        return rows[0]
    except (OSError, ValueError, TypeError):
        return None


# These names are kept for callers that report the reviewed values.
_row = _policy() or {}
MANIFEST_SHA256 = _row.get("installed_manifest_sha256")
AGENT_SHA = _row.get("agent_sha")
RUNTIME_SHA = _row.get("runtime_sha")
SCHEMA_DIGEST = _row.get("schema_digest")
PROTOCOL = _row.get("protocol")


def _contract(version):
    script = (
        "import importlib.util,json,sys; "
        "from runtime_protocol.store import SCHEMA_VERSION; "
        "from astrid_runtime_client.contract_metadata import PROTOCOL,SCHEMA_DIGEST; "
        "print(json.dumps({'python':sys.executable,'python_version':'.'.join(map(str,sys.version_info[:3])),"
        "'workspace_schema_version':SCHEMA_VERSION,'protocol':PROTOCOL,'schema_digest':SCHEMA_DIGEST,"
        "**{n:importlib.util.find_spec(n).origin for n in ('astrid','astrid_runtime','runtime_protocol','astrid_runtime_client')}}))"
    )
    result = subprocess.run([str(version.python), "-I", "-c", script], check=True,
                            capture_output=True, text=True, timeout=30,
                            env={**os.environ, **version.env}, cwd=str(version.root))
    return json.loads(result.stdout)


def _installed_python(python, root):
    if not python.is_absolute() or not python.is_file() or not python.is_relative_to(root):
        return False
    target = python.resolve()
    if target.is_relative_to(root):
        return True
    if sys.platform == "win32":
        bootstrap = Path(os.environ["LOCALAPPDATA"]) / "Astrid Installer" / "bootstrap" / "python"
    else:
        home = Path.home()
        data = (Path(os.environ.get("XDG_DATA_HOME", str(home / ".local/share")))
                if sys.platform != "darwin" else home / ".local/share")
        bootstrap = data / "astrid" / "bootstrap" / "python"
    try:
        parts = target.relative_to(bootstrap.resolve()).parts
    except ValueError:
        return False
    return (len(parts) >= 2 and parts[0].startswith("cpython-3.11.13-")
            and (len(parts) == 3 and parts[1:] == ("bin", "python3.11")
                 or len(parts) == 2 and parts[1].lower() == "python.exe"))


def _version_provenance_problems(version, manifest, version_path):
    problems = []
    version_root = Path(version.get('root', '')).resolve()
    record_root = Path(version_path).resolve().parent
    if version_root != record_root:
        problems.append('version root does not match selected version record')
    program_root = Path(version_path).resolve().parent.parent.parent
    try:
        selected = (program_root / 'current').read_text(encoding='utf-8').strip()
    except OSError:
        selected = None
    if selected != version_root.name:
        problems.append('current selection does not match version record')
    profile = Path(version.get('profile', '')).absolute()
    python = Path(version.get('python', '')).absolute()
    if (not profile.is_relative_to(Path(version_root).absolute()) or not profile.is_file()
            or not profile.resolve().is_relative_to(Path(version_root).resolve())):
        problems.append('version profile path is not an installed file')
    if not python.is_relative_to(Path(version_root).absolute()) or not python.is_file():
        problems.append('version Python path is not declared inside the installed version')
    env = version.get('env')
    if not isinstance(env, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in env.items()):
        problems.append('version environment provenance is malformed')
    # The served installer extracts Runtime into <version>/r and records that
    # checkout in profile.json; it does not persist runtime.lock.json.
    try:
        profile_value = json.loads(profile.read_text(encoding="utf-8-sig"))
        if not isinstance(profile_value, dict):
            raise ValueError('profile must be an object')
        expected_runtime_path = version_root / 'r'
        runtime_checkout_value = Path(profile_value.get('runtime_checkout', '')).absolute()
        runtime_checkout = runtime_checkout_value.resolve()
        if (runtime_checkout_value != expected_runtime_path.absolute() or expected_runtime_path.is_symlink()
                or not runtime_checkout.is_relative_to(version_root) or not runtime_checkout.is_dir()):
            problems.append('profile Runtime checkout provenance does not match installed version')
        expected_environment_path = version_root / 'py'
        runtime_environment_value = Path(profile_value.get('runtime_environment', '')).absolute()
        runtime_environment = runtime_environment_value.resolve()
        if (runtime_environment_value != expected_environment_path.absolute() or expected_environment_path.is_symlink()
                or not runtime_environment.is_relative_to(version_root) or not runtime_environment.is_dir()):
            problems.append('profile Runtime environment provenance does not match installed version')
        for key in ('source_checkout', 'service_declaration'):
            value = profile_value.get(key)
            path = Path(value).resolve() if isinstance(value, str) else None
            wrong_kind = (not path.is_dir() if key == 'source_checkout' else not path.is_file()) if path else True
            if path is None or not path.is_relative_to(version_root) or wrong_kind:
                problems.append(f'profile {key} provenance is missing, invalid, or outside installed version')
    except (OSError, ValueError, TypeError):
        problems.append('installed profile provenance is missing or malformed')
    if isinstance(version.get('identity'), dict):
        try:
            manager = json.loads((program_root / 'install.json').read_text()).get('managed_by')
        except (OSError, ValueError, TypeError):
            manager = None
        expected_identity = {'release': manifest.get('release', 0), 'managed_by': manager}
        if version['identity'] != expected_identity:
            problems.append('version identity does not match installed manifest/manager provenance')
    return problems


def classify(health, discovery, version, manifest, manifest_bytes, version_path, runtime_contract, *, stopped=False):
    """Classify only the exact reviewed identityless predecessor and explain refusal."""
    supported = _policy()
    problems = []
    if not isinstance(supported, dict):
        problems.append('no supported legacy schema declaration')
    else:
        installed_manifest = dict(manifest)
        if 'vibecomfy' not in installed_manifest:
            installed_manifest['vibecomfy'] = {'sha': supported.get('installed_manifest_vibecomfy_default_sha')}
        canonical = (json.dumps(installed_manifest, indent=2, sort_keys=True) + "\n").encode()
        if manifest_bytes != canonical:
            problems.append('installed manifest representation does not match reviewed installer writer')
        if hashlib.sha256(manifest_bytes).hexdigest() != supported.get('installed_manifest_sha256'):
            problems.append('installed manifest content does not match reviewed schema')
        if (version.get('identity') is not None or health.get('identity') is not None
                or discovery.get('identity') is not None):
            problems.append('identity is present on identityless predecessor')
        checks = {
            'installed_manifest_sha256': hashlib.sha256(manifest_bytes).hexdigest(),
            'install_manifest_schema_version': manifest.get('schema_version'),
            'agent_sha': manifest.get('agent', {}).get('sha'),
            'runtime_sha': manifest.get('runtime', {}).get('sha'),
            'manifest_schema_digest': manifest.get('runtime', {}).get('schema_digest'),
            'manifest_release': manifest.get('release', 0),
            'protocol': runtime_contract.get('protocol'),
            'schema_digest': runtime_contract.get('schema_digest'),
            'workspace_schema_version': runtime_contract.get('workspace_schema_version'),
        }
        for key, actual in checks.items():
            if actual != supported.get('schema_digest' if key == 'manifest_schema_digest' else key):
                problems.append(f'{key} mismatch')
        if not stopped and health.get('protocol') != supported.get('protocol'):
            problems.append('health protocol mismatch')
        if not stopped and health.get('schema_digest') != supported.get('schema_digest'):
            problems.append('health schema digest mismatch')
        if set(version) != set(supported.get('version_record_keys', [])):
            problems.append('legacy version record fields mismatch')
        version_root = Path(version.get('root', '')).resolve()
        if not version_root.name.startswith(supported.get('version_id_prefix', '\0')):
            problems.append('version directory provenance mismatch')
        problems.extend(_version_provenance_problems(version, manifest, version_path))
        if manifest.get('tools', {}).get('python', {}).get('version') != supported.get('python_version'):
            problems.append('manifest Python provenance mismatch')
    instance = health.get('runtime_instance_id')
    discovered_instance = discovery.get('runtime_instance_id')
    if not stopped and health.get('status') != 'ok':
        problems.append('Runtime health is not ok')
    if not stopped and (not isinstance(instance, str) or not instance.strip()):
        problems.append('health runtime instance id is missing')
    if not stopped and (not isinstance(discovered_instance, str) or not discovered_instance.strip() or instance != discovered_instance):
        problems.append('health/discovery runtime instance ids do not match')
    return not problems, {'mode': 'supported-legacy-schema', 'release_id': supported.get('id') if supported else None,
                          'health_protocol': health.get('protocol'), 'health_schema_digest': health.get('schema_digest'),
                          'health_status': health.get('status'), 'health_runtime_epoch': health.get('runtime_epoch'),
                          'health_runtime_session_id': health.get('runtime_session_id'),
                          'health_runtime_instance_id': instance, 'discovery_runtime_instance_id': discovered_instance,
                          'health_identity': health.get('identity'), 'discovery_identity': discovery.get('identity'),
                          'version_identity': version.get('identity'), 'version_record_fields': sorted(version),
                          'version_root': version.get('root'), 'version_python': version.get('python'),
                          'version_profile': version.get('profile'),
                          'version_env_fields': sorted(version.get('env', {})) if isinstance(version.get('env'), dict) else None,
                          'manifest_sha256': hashlib.sha256(manifest_bytes).hexdigest(),
                          'manifest_agent_sha': manifest.get('agent', {}).get('sha'),
                          'manifest_runtime_sha': manifest.get('runtime', {}).get('sha'),
                          'manifest_schema_digest': manifest.get('runtime', {}).get('schema_digest'),
                          'runtime_contract': {key: runtime_contract.get(key) for key in ('protocol', 'schema_digest', 'workspace_schema_version')},
                          'problems': problems}


def admitted(version, health, discovery, *, program_root, stopped=False):
    """Match the selected direct install; stopped callers must independently prove quiescence."""
    if version is None or version.release != 0 or version.managed_by is not None:
        return False
    if not isinstance(health, Mapping) or not isinstance(discovery, Mapping):
        return False
    try:
        root = version.root
        if (root.parent != program_root / "versions"
                or (program_root / "current").read_text(encoding="utf-8").strip() != root.name):
            return False
        marker = json.loads((program_root / "install.json").read_text(encoding="utf-8"))
        if marker.get("owner") != "haus.astrid.runtime" or marker.get("managed_by") is not None:
            return False
        record = json.loads((root / "version.json").read_text(encoding="utf-8"))
        if (Path(record["root"]).resolve() != root
                or Path(record["python"]) != version.python or Path(record["profile"]) != version.profile):
            return False
        raw = (root / "manifest.json").read_bytes()
        manifest = json.loads(raw)
        if (not _installed_python(version.python, root)
                or not version.profile.is_file() or not version.profile.resolve().is_relative_to(root)):
            return False
        contract = _contract(version)
        matches, _ = classify(health, discovery, record, manifest, raw, root / "version.json", contract,
                              **({"stopped": True} if stopped else {}))
        if not matches:
            return False
        if (contract.get("python") != str(version.python) or contract.get("python_version") != "3.11.13"
                or type(contract.get("workspace_schema_version")) is not int):
            return False
        for name in ("astrid", "astrid_runtime", "runtime_protocol", "astrid_runtime_client"):
            if not Path(contract[name]).resolve().is_relative_to(root):
                return False
        return True
    except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError):
        return False
