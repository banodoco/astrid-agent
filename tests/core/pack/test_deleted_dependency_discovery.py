from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from astrid.core.pack import cli, loader
from astrid.core.pack.discovery import discover_pack_metadata, pack_discovery_scope
from astrid.core.pack.source_setup import (
    SourceDeclaration, SourceSetupError, active_source_inventory, provision,
)


def _pack(root: Path, name: str, dependencies=()) -> Path:
    pack = root / name
    pack.mkdir(parents=True)
    (pack / 'pack.yaml').write_text(json.dumps({
        'schema_version': 3, 'id': name, 'name': name, 'version': '1.0.0',
        'pack_api': '2.0', 'resources': [],
        'dependencies': {'packs': list(dependencies)},
    }))
    return pack


def _managed_provider(tmp_path: Path, monkeypatch, *, with_consumer=False) -> Path:
    repo = tmp_path / 'repository'
    repo.mkdir()
    _pack(repo / 'packs', 'provider')
    if with_consumer:
        _pack(repo / 'packs', 'managed_consumer', ('provider',))
    def git(*args):
        return subprocess.check_output(['git', '-C', str(repo), *args], text=True).strip()
    git('init', '-q', '-b', 'main')
    git('config', 'user.email', 'fixture@example.test')
    git('config', 'user.name', 'Fixture')
    git('add', '.')
    git('commit', '-qm', 'fixture')
    declaration = SourceDeclaration.from_mapping({
        'pack_id': 'provider', 'repository': str(repo), 'revision': git('rev-parse', 'HEAD'),
        'pack_subpath': 'packs/provider',
    })
    declarations = [declaration]
    if with_consumer:
        declarations.append(SourceDeclaration.from_mapping({
            'pack_id': 'managed_consumer', 'repository': str(repo),
            'revision': git('rev-parse', 'HEAD'), 'pack_subpath': 'packs/managed_consumer',
        }))
    state = tmp_path / 'state.json'
    monkeypatch.setenv('ASTRID_SOURCE_STATE', str(state))
    provision(tuple(declarations), state_path=state, data_root=tmp_path / 'data')
    return next(source.pack_root for source in active_source_inventory().sources
                if source.pack_id == 'provider')


@pytest.mark.parametrize('managed', [False, True], ids=['collection', 'managed'])
def test_raw_deleted_dependency_is_quarantined_across_discovery_list_and_check(
    tmp_path, monkeypatch, capsys, managed,
):
    home = tmp_path / 'home'
    home.mkdir(exist_ok=True)
    monkeypatch.setattr(Path, 'home', classmethod(lambda cls: home))
    monkeypatch.delenv('ASTRID_PACKS_PATH', raising=False)
    monkeypatch.setenv('ASTRID_SOURCE_STATE', str(tmp_path / 'state.json'))
    collection = home / 'Astrid' / 'packs'
    consumer = _pack(collection, 'consumer', ('provider',))
    _pack(collection, 'transitive', ('consumer',))
    _pack(collection, 'unrelated')
    provider = (_managed_provider(tmp_path, monkeypatch, with_consumer=True)
                if managed else _pack(collection, 'provider'))

    def ids():
        return {item.id for item in discover_pack_metadata(project_root=tmp_path)}

    with pack_discovery_scope():
        assert {'provider', 'consumer', 'transitive', 'unrelated', 'rendering'} <= ids()
        assert {'provider', 'consumer', 'transitive', 'unrelated', 'rendering'} <= ids()
        if managed:
            assert 'managed_consumer' in ids()
        assert cli.main(['list', '--json']) == 0
        before_list = {item['id'] for item in json.loads(capsys.readouterr().out)['packs']}
        assert {'provider', 'consumer', 'transitive', 'unrelated', 'rendering'} <= before_list
        assert cli.main(['check', str(consumer), '--json']) == 0
        capsys.readouterr()
        shutil.rmtree(provider)
        for _ in range(2):
            remaining = ids()
            assert {'unrelated', 'rendering'} <= remaining
            assert not {'provider', 'consumer', 'transitive', 'managed_consumer'} & remaining
        quarantined = {item['pack_id']: item for item in loader.pack_quarantine_report()}
        assert 'provider' in quarantined['consumer']['error']
        assert 'Install the dependency' in quarantined['consumer']['error']
        assert 'consumer' in quarantined['transitive']['error']
        assert 'unrelated' not in quarantined and 'rendering' not in quarantined
        if managed:
            assert 'provider' in quarantined['managed_consumer']['error']
        assert cli.main(['list', '--json']) == 0
        payload = capsys.readouterr().out
        assert 'unrelated' in payload and 'rendering' in payload
        assert 'consumer' not in payload and 'transitive' not in payload
        assert cli.main(['check', str(consumer), '--json']) == 1
        errors = json.loads(capsys.readouterr().out)['errors']
        assert any('provider' in error and 'Install the dependency' in error for error in errors)
        if managed:
            managed_consumer = next(source.pack_root for source in active_source_inventory().sources
                                    if source.pack_id == 'managed_consumer')
            assert cli.main(['check', str(managed_consumer), '--json']) == 1
            errors = json.loads(capsys.readouterr().out)['errors']
            assert any('provider' in error and 'Install the dependency' in error for error in errors)


@pytest.mark.parametrize('damage', ['tamper', 'symlink', 'missing_and_tamper', 'ancestor_symlink'])
def test_managed_damage_still_refuses_discovery(tmp_path, monkeypatch, damage):
    root = _managed_provider(tmp_path, monkeypatch)
    with pack_discovery_scope():
        assert 'provider' in {item.id for item in discover_pack_metadata(project_root=tmp_path)}
        if damage == 'tamper':
            (root / 'pack.yaml').write_text((root / 'pack.yaml').read_text() + '\n')
        elif damage == 'symlink':
            target = root.with_name('moved')
            root.rename(target)
            root.symlink_to(target, target_is_directory=True)
        elif damage == 'missing_and_tamper':
            (root.parents[1] / 'unexpected').write_text('tamper')
            shutil.rmtree(root)
        else:
            parent = root.parent
            target = parent.with_name('moved')
            parent.rename(target)
            parent.symlink_to(target, target_is_directory=True)
        with pytest.raises(SourceSetupError):
            discover_pack_metadata(project_root=tmp_path)


def test_check_preserves_sibling_dependency_context(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv('ASTRID_SOURCE_STATE', str(tmp_path / 'state.json'))
    consumer = _pack(tmp_path / 'drafts', 'consumer', ('provider',))
    _pack(tmp_path / 'drafts', 'provider')
    assert cli.main(['check', str(consumer), '--json']) == 0
    assert not json.loads(capsys.readouterr().out)['errors']
