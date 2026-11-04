"""Test-side observations of an installed Astrid product; never repairs it."""
from __future__ import annotations
import argparse
import hashlib
import ipaddress
import json
import os
import re
import shutil
from pathlib import Path
import subprocess
import sys
import urllib.request
import urllib.parse


ROOT = Path(__file__).resolve().parents[1]
RELEASES_PATH = ROOT / 'tests' / 'upgrade' / 'releases.json'


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def within(path, root):
    try:
        return Path(path).resolve().is_relative_to(Path(root).resolve())
    except (ValueError, OSError, TypeError):
        return False


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def installed_executable(executable, root):
    return Path(executable).is_absolute() and Path(executable).is_file() and within(executable, root)


def bootstrap_python_root():
    if sys.platform == 'win32':
        return Path(os.environ['LOCALAPPDATA']) / 'Astrid Installer' / 'bootstrap' / 'python'
    home = Path.home()
    data = Path(os.environ.get('XDG_DATA_HOME', str(home / '.local/share'))) if sys.platform != 'darwin' else home / '.local/share'
    return data / 'astrid' / 'bootstrap' / 'python'


def installed_python(executable, root, bootstrap_root, pin):
    """Allow the version's venv link only to its pinned installer-managed Python."""
    path = Path(executable)
    if not path.is_absolute() or not path.is_file() or not path.absolute().is_relative_to(Path(root).absolute()):
        return False
    if within(path, root):
        return True
    if not isinstance(pin, str) or not re.fullmatch(r'\d+\.\d+\.\d+', pin):
        return False
    target = path.resolve()
    bootstrap = Path(bootstrap_root).resolve()
    try:
        relative = target.relative_to(bootstrap)
    except ValueError:
        return False
    parts = relative.parts
    if len(parts) < 2 or not parts[0].startswith('cpython-' + pin + '-'):
        return False
    major, minor, _patch = pin.split('.')
    return (len(parts) == 3 and parts[1] == 'bin' and parts[2] == f'python{major}.{minor}'
            or len(parts) == 2 and parts[1].lower() == 'python.exe')


def request(url, headers=None):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect()).open(req, timeout=45) as response:
        return json.load(response)


def valid_catalog(value):
    """Check the bridge modelCatalog.ts shape and require provider discovery.

    An unsigned provider may correctly have no selectable models.
    """
    if not isinstance(value, dict) or set(value) != {'providers', 'currentModel'}:
        return False
    if value['currentModel'] is not None and not isinstance(value['currentModel'], str):
        return False
    providers = value['providers']
    if not isinstance(providers, list) or not providers:
        return False
    for provider in providers:
        if not isinstance(provider, dict):
            return False
        required = {'id', 'name', 'signedIn', 'usable', 'models'}
        if not required <= set(provider) or set(provider) - required - {'reason', 'login', 'recommended', 'usage'}:
            return False
        if any(not isinstance(provider[k], str) or not provider[k] for k in ('id', 'name')):
            return False
        if any(type(provider[k]) is not bool for k in ('signedIn', 'usable')):
            return False
        if 'reason' in provider and provider['reason'] not in (None, 'needs_sign_in', 'no_available_models'):
            return False
        if 'login' in provider and provider['login'] not in ('oauth', 'api-key', 'local'):
            return False
        if 'recommended' in provider and type(provider['recommended']) is not bool:
            return False
        models = provider['models']
        if not isinstance(models, list) or any(
            not isinstance(m, dict) or set(m) != {'id', 'name'} or
            any(not isinstance(m[k], str) or not m[k] for k in ('id', 'name')) for m in models
        ):
            return False
        if 'usage' in provider:
            usage = provider['usage']
            if not isinstance(usage, dict) or set(usage) != {'limits'} or not isinstance(usage['limits'], list):
                return False
            fields = {'unit', 'used', 'limit', 'remaining', 'usedFraction', 'remainingFraction', 'durationMs', 'resetsAt', 'status'}
            for limit in usage['limits']:
                if not isinstance(limit, dict) or set(limit) - fields:
                    return False
                if 'unit' in limit and limit['unit'] not in ('percent', 'tokens', 'requests', 'usd', 'minutes', 'bytes', 'unknown'):
                    return False
                if 'status' in limit and limit['status'] not in ('ok', 'warning', 'exhausted', 'unknown'):
                    return False
                if any(type(v) not in (int, float) for k, v in limit.items() if k not in ('unit', 'status')):
                    return False
    return True


def bridge_observation(chat, path):
    endpoint = urllib.parse.urlsplit(chat['endpoint'])
    try:
        loopback = ipaddress.ip_address(endpoint.hostname or '').is_loopback
        port = endpoint.port
    except ValueError:
        loopback, port = False, None
    if (endpoint.scheme != 'http' or not loopback or not port or endpoint.username is not None
            or endpoint.password is not None or endpoint.path not in ('', '/')
            or endpoint.query or endpoint.fragment):
        raise ValueError('chat endpoint must be an HTTP loopback origin with a port')
    headers = {'Authorization': 'Bearer ' + chat['token'], 'X-Astrid-Bridge-Version': 'v1'}
    value = request(chat['endpoint'].rstrip('/') + '/' + path, headers)
    if path == 'agents':
        valid = isinstance(value.get('agents'), list) and any(r.get('id') == 'astrid' for r in value['agents'])
    else:
        valid = valid_catalog(value)
    return valid, value


def env_port(version):
    return str(version.get('env', {}).get('ASTRID_RUNTIME_DOOR_PORT', '17340'))


def installed_runtime_contract(python, root, env, data_root):
    probe = (
        "import importlib.util,json,sys; "
        "from runtime_protocol.store import SCHEMA_VERSION; "
        "from astrid_runtime_client.contract_metadata import PROTOCOL,SCHEMA_DIGEST; "
        "print(json.dumps({'python':sys.executable,'python_version':'.'.join(map(str,sys.version_info[:3])),"
        "'workspace_schema_version':SCHEMA_VERSION,'protocol':PROTOCOL,'schema_digest':SCHEMA_DIGEST,"
        "**{n:importlib.util.find_spec(n).origin for n in ('astrid','astrid_runtime','runtime_protocol','astrid_runtime_client')}}))"
    )
    result = subprocess.run([str(python), '-I', '-c', probe], capture_output=True, text=True,
                            env=env, cwd=str(data_root), timeout=30, check=True)
    value = json.loads(result.stdout)
    origins = {key: value[key] for key in ('astrid', 'astrid_runtime', 'runtime_protocol', 'astrid_runtime_client')}
    if (not installed_python(value['python'], root, bootstrap_python_root(),
                             read(Path(root) / 'manifest.json')['tools']['python']['version'])
            or any(not within(path, root) for path in origins.values())):
        raise ValueError('Runtime contract modules do not originate from the installed version')
    return value


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
        profile_value = read(profile)
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
            manager = read(program_root / 'install.json').get('managed_by')
        except (OSError, ValueError, TypeError):
            manager = None
        expected_identity = {'release': manifest.get('release', 0), 'managed_by': manager}
        if version['identity'] != expected_identity:
            problems.append('version identity does not match installed manifest/manager provenance')
    return problems


def _reviewed_runtime_schema(mode):
    try:
        releases = json.loads(RELEASES_PATH.read_text(encoding='utf-8'))
        rows = releases['format_review']['supported_runtime_schemas']
        matches = [row for row in rows if row.get('mode') == mode]
        return matches[0] if len(matches) == 1 else None
    except (OSError, ValueError, KeyError, TypeError):
        return None


def identity_documents_match(health, discovery, version):
    health_identity = health.get('identity')
    discovery_identity = discovery.get('identity')
    version_identity = version.get('identity')
    return (isinstance(health_identity, dict) and isinstance(discovery_identity, dict)
            and isinstance(version_identity, dict) and health_identity == discovery_identity
            and health_identity == version_identity)


def _reviewed_schema_problems(schema, mode, health, version, manifest, manifest_bytes, runtime_contract):
    problems = []
    if not isinstance(schema, dict):
        return ['no unique reviewed Runtime schema declaration']
    version_root = Path(version.get('root', '')).resolve()
    version_id_prefix = schema.get('version_id_prefix')
    if not isinstance(version_id_prefix, str) or not version_root.name.startswith(version_id_prefix):
        problems.append('version directory provenance does not match reviewed schema')
    checks = {
        'schema mode': (schema.get('mode'), mode),
        'install manifest schema': (manifest.get('schema_version'), schema.get('install_manifest_schema_version')),
        'Runtime protocol': (runtime_contract.get('protocol'), schema.get('protocol')),
        'Runtime schema digest': (runtime_contract.get('schema_digest'), schema.get('schema_digest')),
        'workspace schema version': (runtime_contract.get('workspace_schema_version'), schema.get('workspace_schema_version')),
        'health protocol': (health.get('protocol'), schema.get('protocol')),
        'health schema digest': (health.get('schema_digest'), schema.get('schema_digest')),
        'version record fields': (sorted(version), sorted(schema.get('version_record_keys', []))),
    }
    # Both reviewed installers write parsed JSON with this exact format after
    # adding their default VibeComfy pin. Bind the installed representation,
    # not the downloaded App manifest bytes.
    installed_manifest = dict(manifest)
    default_vibecomfy = schema.get('installed_manifest_vibecomfy_default_sha')
    if default_vibecomfy and 'vibecomfy' not in installed_manifest:
        installed_manifest['vibecomfy'] = {'sha': default_vibecomfy}
    canonical_manifest_bytes = (json.dumps(installed_manifest, indent=2, sort_keys=True) + '\n').encode()
    installed_manifest_sha = hashlib.sha256(canonical_manifest_bytes).hexdigest()
    if manifest_bytes != canonical_manifest_bytes:
        problems.append('installed manifest representation does not match reviewed installer writer')
    if installed_manifest_sha != schema.get('installed_manifest_sha256'):
        problems.append('installed manifest content does not match reviewed schema')
    if 'manifest_release' in schema:
        checks['manifest release'] = (manifest.get('release', 0), schema['manifest_release'])
    for label, (actual, expected) in checks.items():
        if actual != expected:
            problems.append(f'{label} does not match reviewed schema')
    source_refs = schema.get('reviewed_source_refs', {})
    for name in ('agent', 'runtime'):
        if source_refs.get(name) and manifest.get(name, {}).get('sha') != source_refs[name]:
            problems.append(f'manifest {name} source does not match reviewed schema')
    if mode == 'identity-aware-version-record' and (
            schema.get('health_identity_emitted') is not True
            or schema.get('discovery_identity_emitted') is not True):
        problems.append('reviewed Runtime sources do not emit the identity fields required for strict comparison')
    return problems


def legacy_identity_observation(health, discovery, version, manifest, manifest_bytes,
                                version_path, runtime_contract):
    """Allow identity omission only for an exact, release-listed legacy bundle."""
    supported = None
    try:
        releases = json.loads(RELEASES_PATH.read_text(encoding='utf-8'))
        served = next(row for row in releases['releases'] if row.get('id') == 'served')
        supported = served.get('supported_legacy_identity_schema')
    except (OSError, ValueError, KeyError, StopIteration):
        supported = None
    identity_values = (health.get('identity'), discovery.get('identity'), version.get('identity'))
    identities_absent = all(value is None for value in identity_values)
    health_identity = health.get('identity')
    if not identities_absent:
        expected = version.get('identity')
        problems = []
        if not identity_documents_match(health, discovery, version):
            problems.append('health/discovery/version identities are not exactly equal')
        problems.extend(_reviewed_schema_problems(
            _reviewed_runtime_schema('identity-aware-version-record'), 'identity-aware-version-record', health,
            version, manifest, manifest_bytes, runtime_contract))
        if health.get('status') != 'ok':
            problems.append('Runtime health is not ok')
        if health.get('protocol') != runtime_contract.get('protocol'):
            problems.append('health protocol does not match installed Runtime contract')
        expected_digest = manifest.get('runtime', {}).get('schema_digest')
        if (not expected_digest or health.get('schema_digest') != expected_digest
                or runtime_contract.get('schema_digest') != expected_digest):
            problems.append('health/installed Runtime schema digest does not match manifest')
        problems.extend(_version_provenance_problems(version, manifest, version_path))
        instance = health.get('runtime_instance_id')
        discovered_instance = discovery.get('runtime_instance_id')
        if not isinstance(instance, str) or not instance.strip() or instance != discovered_instance:
            problems.append('health/discovery runtime instance ids do not match')
        return not problems, {'mode': 'identity-aware', 'live': health_identity,
                           'discovery': discovery.get('identity'), 'version': expected,
                           'health_runtime_instance_id': instance,
                           'discovery_runtime_instance_id': discovered_instance,
                           'health_protocol': health.get('protocol'),
                           'health_schema_digest': health.get('schema_digest'),
                           'health_runtime_epoch': health.get('runtime_epoch'),
                           'health_runtime_session_id': health.get('runtime_session_id'),
                           'manifest_sha256': hashlib.sha256(manifest_bytes).hexdigest(),
                           'manifest_agent_sha': manifest.get('agent', {}).get('sha'),
                           'manifest_runtime_sha': manifest.get('runtime', {}).get('sha'),
                           'runtime_contract': {key: runtime_contract.get(key) for key in ('protocol', 'schema_digest', 'workspace_schema_version')},
                           'version_record_fields': sorted(version),
                           'version_root': version.get('root'),
                           'version_python': version.get('python'),
                           'version_profile': version.get('profile'),
                           'version_env_fields': sorted(version.get('env', {})) if isinstance(version.get('env'), dict) else None,
                           'problems': problems}

    problems = []
    if not isinstance(supported, dict):
        problems.append('no supported legacy schema declaration')
    else:
        problems.extend(_reviewed_schema_problems(
            _reviewed_runtime_schema('supported-legacy-schema'), 'supported-legacy-schema',
            health, version, manifest, manifest_bytes, runtime_contract))
        checks = {
            'installed_manifest_sha256': hashlib.sha256(manifest_bytes).hexdigest(),
            'install_manifest_schema_version': manifest.get('schema_version'),
            'agent_sha': manifest.get('agent', {}).get('sha'),
            'runtime_sha': manifest.get('runtime', {}).get('sha'),
            'schema_digest': manifest.get('runtime', {}).get('schema_digest'),
            'manifest_release': manifest.get('release', 0),
            'protocol': runtime_contract.get('protocol'),
            'workspace_schema_version': runtime_contract.get('workspace_schema_version'),
        }
        for key, actual in checks.items():
            if actual != supported.get(key):
                problems.append(f'{key} mismatch')
        if health.get('protocol') != supported.get('protocol'):
            problems.append('health protocol mismatch')
        if health.get('schema_digest') != supported.get('schema_digest'):
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
    if health.get('status') != 'ok':
        problems.append('Runtime health is not ok')
    if not isinstance(instance, str) or not instance.strip():
        problems.append('health runtime instance id is missing')
    if not isinstance(discovered_instance, str) or not discovered_instance.strip() or instance != discovered_instance:
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


def observe(version_path, data_root):
    observations = {}
    details = {}
    def check(name, fn):
        try:
            ok, detail = fn()
            observations[name] = bool(ok)
            details[name] = detail
        except Exception as exc:
            observations[name] = False
            details[name] = {'error': f'{type(exc).__name__}: {exc}'}
    version = read(version_path)
    root, python = Path(version['root']), Path(version['python'])
    manifest_bytes = (root / 'manifest.json').read_bytes()
    manifest = json.loads(manifest_bytes)
    pin = manifest['tools']['python']['version']
    bootstrap_root = bootstrap_python_root()
    def is_python(path):
        return installed_python(path, root, bootstrap_root, pin)
    profile = read(version['profile'])
    declaration = read(profile['service_declaration'])
    discovery = read(Path(data_root) / 'runtime' / 'discovery.json')
    health = request('http://127.0.0.1:' + env_port(version) + '/api/runtime/v1/health', {'Origin': 'https://astrid.haus'})
    env = {k: v for k, v in os.environ.items() if k not in ('PYTHONPATH', 'PYTHONHOME')}
    env.update(version.get('env', {}))
    runtime_contract_error = None
    try:
        runtime_contract = installed_runtime_contract(python, root, env, data_root)
    except Exception as exc:
        runtime_contract = {}
        runtime_contract_error = f'{type(exc).__name__}: {exc}'
    def identity_observation():
        if not all(isinstance(value, dict) for value in (health, discovery, version, manifest)):
            return False, {'mode': 'malformed-observation', 'health_type': type(health).__name__,
                           'discovery_type': type(discovery).__name__, 'version_type': type(version).__name__,
                           'manifest_type': type(manifest).__name__}
        if runtime_contract_error:
            return False, {
                'mode': 'runtime-contract-unavailable', 'runtime_contract_error': runtime_contract_error,
                'health_status': health.get('status'), 'health_protocol': health.get('protocol'),
                'health_schema_digest': health.get('schema_digest'), 'health_runtime_epoch': health.get('runtime_epoch'),
                'health_runtime_session_id': health.get('runtime_session_id'),
                'health_runtime_instance_id': health.get('runtime_instance_id'), 'health_identity': health.get('identity'),
                'discovery_runtime_instance_id': discovery.get('runtime_instance_id'),
                'discovery_identity': discovery.get('identity'), 'version_identity': version.get('identity'),
                'version_record_fields': sorted(version), 'version_root': version.get('root'),
                'version_python': version.get('python'), 'version_profile': version.get('profile'),
                'version_env_fields': sorted(version.get('env', {})) if isinstance(version.get('env'), dict) else None,
                'manifest_sha256': hashlib.sha256(manifest_bytes).hexdigest(),
                'manifest_agent_sha': manifest.get('agent', {}).get('sha'),
                'manifest_runtime_sha': manifest.get('runtime', {}).get('sha'),
                'manifest_schema_digest': manifest.get('runtime', {}).get('schema_digest'),
            }
        return legacy_identity_observation(health, discovery, version, manifest,
                                          manifest_bytes, version_path, runtime_contract)
    check('runtime.identity', identity_observation)
    services = declaration['services']
    health_rows = {r.get('name'): r for r in health.get('services', [])}
    for service in services:
        name = service['name']
        check('service.' + name, lambda name=name: (health_rows.get(name, {}).get('state') == 'running', health_rows.get(name, {'state': 'missing'})))
        executable = service['argv'][0]
        check('origin.service.' + name, lambda executable=executable: ((is_python(executable) if Path(executable).absolute() == python.absolute() else installed_executable(executable, root)), {'executable': executable, 'resolved': str(Path(executable).resolve()), 'install_root': str(root)}))
    def origins():
        origins = {key: runtime_contract[key] for key in ('astrid', 'astrid_runtime', 'runtime_protocol', 'astrid_runtime_client')}
        value = {**origins, **{key: runtime_contract[key] for key in ('python', 'python_version', 'workspace_schema_version', 'protocol', 'schema_digest')}}
        return (is_python(value['python']) and value['python_version'] == pin and all(within(v, root) for v in origins.values()), value)
    check('origin.imports', origins)
    chat_service = next((s for s in services if s.get('name') == 'chat'), {})
    chat_env = chat_service.get('env', {})
    command = chat_env.get('ASTRID_ACP_COMMAND', 'astrid')
    command_path = command if Path(command).is_absolute() else shutil.which(command, path=chat_env.get('PATH', ''))
    check('origin.chat_command', lambda: (bool(command_path) and Path(command_path).is_file() and within(command_path, root), {'command': command, 'resolved': command_path}))
    ready = read(Path(data_root) / 'runtime' / 'generic-host.ready.json')
    rows = ready['registration']['capabilities']
    matrix = read(env['ASTRID_CAPABILITY_MATRIX_PATH'])['capabilities']
    actual = {r['id']: r for r in rows}
    required = [r['id'] for r in matrix if r.get('disposition') == 'required']
    names = sorted({name for row in matrix if row.get('disposition') == 'required' for name in row.get('required_binaries', [])})
    host_env = next((s.get('env', {}) for s in services if s.get('name') == 'pack-host'), {})
    # Runtime's declared environment inherits the installed version environment.
    service_path = host_env.get('PATH', env.get('PATH', ''))
    found = {name: shutil.which(name, path=service_path) for name in names}
    check('origin.required_tools', lambda: (all(path and within(path, root) for path in found.values()), {'tools': found, 'install_root': str(root)}))
    check('capabilities.required', lambda: (bool(required) and ready.get('status') == 'ready' and ready.get('runtime_instance_id') == health.get('runtime_instance_id') and all(actual.get(i, {}).get('ready') is True for i in required), {'required': required, 'unready': [i for i in required if actual.get(i, {}).get('ready') is not True]}))
    # Optional failure reasons must be attributable to a declared preflight class;
    # report the exact missing items, including credentials without their values.
    def optional():
        summary, unexpected = {}, []
        for row in rows:
            if row.get('disposition') != 'optional' or row.get('ready') is True:
                continue
            preflight = row.get('preflight') or {}
            failed = {k: v for k, v in preflight.items()
                      if v is False or (isinstance(v, dict) and v.get('ok') is False and not v.get('gates'))}
            allowed = {'binaries', 'packages', 'credentials', 'gpu'}
            summary[row['id']] = failed
            if not failed or set(failed) - allowed:
                unexpected.append(row['id'])
        registered_unready = {r['id'] for r in rows if r.get('ready') is not True}
        if registered_unready != set(ready.get('unready_capabilities', [])):
            unexpected.append('host unready summary disagrees with registration')
        return not unexpected, {'unready': summary, 'unexpected': unexpected, 'source': 'live host registration preflight'}
    check('capabilities.optional_summary', optional)
    unapproved = [r['id'] for r in rows if r.get('disposition') == 'unclassified']
    check('capabilities.approval', lambda: (not unapproved, {'unapproved': unapproved}))
    chat = read(Path(data_root) / 'runtime' / 'chat.json')
    for path in ('agents', 'models'):
        check('chat.' + path, lambda path=path: bridge_observation(chat, path))
    return {'schema_version': 1, 'observations': observations, 'details': details}


def evaluate(receipt, mode, before=None):
    current = receipt['observations']
    if mode == 'update':
        if not before or not isinstance(before.get('observations'), dict):
            raise ValueError('update requires a prior observation receipt')
        prior = before['observations']
        # Missing observations cannot silently disappear across versions.
        regressions = sorted(k for k in set(prior) | set(current) if prior.get(k) is True and current.get(k) is not True)
        preexisting = sorted(k for k, v in prior.items() if v is False)
        new = sorted(k for k, v in current.items() if k not in prior and not v)
        failures = regressions
    else:
        preexisting = []
        new = []
        failures = sorted(k for k, v in current.items() if not v)
    receipt.update(mode=mode, failures=failures, preexisting_unhealthy=preexisting, newly_unhealthy_without_baseline=new, passed=not failures)
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode', choices=('observe', 'install', 'update'), required=True)
    parser.add_argument('--version', required=True)
    parser.add_argument('--data-root', required=True)
    parser.add_argument('--before')
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    try:
        receipt = evaluate(observe(args.version, args.data_root), args.mode, read(args.before) if args.before else None)
    except Exception as exc:
        receipt = {'schema_version': 1, 'mode': args.mode, 'passed': False, 'failures': ['verifier'], 'error': f'{type(exc).__name__}: {exc}'}
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    Path(args.output).write_text(json.dumps(receipt, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'passed': receipt['passed'], 'failures': receipt['failures'], 'output': args.output}))
    return 0 if receipt['passed'] or args.mode == 'observe' and 'observations' in receipt else 1

if __name__ == '__main__':
    sys.exit(main())
