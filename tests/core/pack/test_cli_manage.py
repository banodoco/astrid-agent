"""Managed pack builder selection refuses untrusted implicit defaults."""
import json
import os
from pathlib import Path

import pytest

from astrid.core.pack import cli_manage
from astrid.core.pack.install import PackInstallError


def managed(tmp_path, monkeypatch):
    root = tmp_path / 'install'
    version = root / 'versions' / 'v1'
    prefix = version / 'py'
    module = prefix / 'lib' / 'astrid' / 'core' / 'pack' / 'cli_manage.py'
    module.parent.mkdir(parents=True)
    module.write_text('')
    (version / 'pack-build').mkdir()
    descriptor = version / 'pack-build' / 'toolchain.json'
    descriptor.write_text('{}')
    (root / 'current').write_text('v1\n')
    (version / 'version.json').write_text(json.dumps({
        'root': str(version), 'python': str(prefix / 'bin' / 'python'),
        'profile': str(version / 'profile'), 'env': {},
        'identity': {'release': 1, 'managed_by': None},
    }))
    monkeypatch.setattr(cli_manage.sys, 'prefix', str(prefix))
    monkeypatch.setattr(cli_manage, '__file__', str(module))
    monkeypatch.delenv(cli_manage.TOOLCHAIN_ENV, raising=False)
    return root, version, descriptor


def test_selected_managed_descriptor_requires_exact_installed_context(tmp_path, monkeypatch):
    root, version, descriptor = managed(tmp_path, monkeypatch)
    assert cli_manage._managed_toolchain() == descriptor
    (root / 'current').write_text('other\n')
    assert cli_manage._managed_toolchain() is None
    (root / 'current').write_text('v1\n')
    monkeypatch.setattr(cli_manage, '__file__', str(tmp_path / 'foreign.py'))
    assert cli_manage._managed_toolchain() is None


@pytest.mark.skipif(os.name == 'nt', reason='POSIX mode and symlink checks')
def test_implicit_descriptor_refuses_writable_and_aliases(tmp_path, monkeypatch):
    root, version, descriptor = managed(tmp_path, monkeypatch)
    descriptor.chmod(0o666)
    assert cli_manage._managed_toolchain() is None
    descriptor.chmod(0o600)
    descriptor.unlink()
    (version / 'foreign.json').write_text('{}')
    descriptor.symlink_to(version / 'foreign.json')
    assert cli_manage._managed_toolchain() is None


def test_explicit_flag_then_environment_precede_managed(tmp_path, monkeypatch):
    _, _, descriptor = managed(tmp_path, monkeypatch)
    env = tmp_path / 'env.json'; env.write_text('{}')
    flag = tmp_path / 'flag.json'; flag.write_text('{}')
    monkeypatch.setenv(cli_manage.TOOLCHAIN_ENV, str(env))
    with pytest.raises(PackInstallError, match='flag.json'):
        cli_manage._toolchain(str(flag))
    with pytest.raises(PackInstallError, match='env.json'):
        cli_manage._toolchain(None)
    monkeypatch.delenv(cli_manage.TOOLCHAIN_ENV)
    with pytest.raises(PackInstallError, match='toolchain.json'):
        cli_manage._toolchain(None)


def test_managed_descriptor_keeps_binary_and_api_hash_checks(tmp_path, monkeypatch):
    import hashlib
    from types import SimpleNamespace
    from astrid import pack_build

    _, version, descriptor = managed(tmp_path, monkeypatch)
    node = version / 'node'; node.write_bytes(b'node')
    esbuild = version / 'esbuild' / 'bin' / 'esbuild'
    esbuild.parent.mkdir(parents=True); esbuild.write_bytes(b'esbuild')
    api = esbuild.parent.parent / 'lib' / 'main.js'; api.parent.mkdir(); api.write_bytes(b'api')
    shared = version / 'pack-build' / 'shared.json'
    shared.write_text(json.dumps({'shared_modules': {k: {} for k in pack_build._SINGLETONS}}))
    sha = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()
    descriptor.write_text(json.dumps({'version': 1, 'node_path': str(node), 'node_version': '20.19.4',
        'node_sha256': sha(node), 'esbuild_path': str(esbuild), 'esbuild_version': '0.27.7',
        'esbuild_sha256': sha(esbuild), 'shared_modules_lock': str(shared)}))
    monkeypatch.setattr(pack_build, '_API_HASH', sha(api))
    monkeypatch.setattr(cli_manage.subprocess, 'run', lambda args, **kw: SimpleNamespace(
        stdout='v20.19.4' if args[0] == str(node) else '0.27.7'))
    assert cli_manage._toolchain(None)['node_path'] == str(node)
    esbuild.write_bytes(b'tampered')
    with pytest.raises(PackInstallError, match='pinned sha256'):
        cli_manage._toolchain(None)
