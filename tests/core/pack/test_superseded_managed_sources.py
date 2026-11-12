from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

import astrid
from astrid.core.auth import _auth_cli_candidates
from astrid.core.execution.custody_broker import _inventory_launch_policy
from astrid.core.execution.executor.registry import load_default_registry
from astrid.core.pack.discovery import discover_packs_ordered
from astrid.core.pack.source_setup import (
    SourceDeclaration,
    SourceSetupError,
    _validate_checkout,
    active_source_inventory,
    active_sources,
    source_data_root,
    source_state_path,
)


def _git(root: Path, *args: str) -> str:
    return subprocess.check_output(['git', '-C', str(root), *args], text=True).strip()


@pytest.fixture
def legacy(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[Path, Path]:
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('XDG_STATE_HOME', str(tmp_path / 'state'))
    monkeypatch.setenv('XDG_DATA_HOME', str(tmp_path / 'data'))
    monkeypatch.delenv('ASTRID_SOURCE_STATE', raising=False)
    monkeypatch.delenv('ASTRID_SOURCE_DATA', raising=False)
    monkeypatch.delenv('ASTRID_PACKS_PATH', raising=False)
    root = source_data_root() / 'hivemind'
    root.parent.mkdir(parents=True)
    shutil.copytree(Path(astrid.__file__).parent / 'core' / 'hivemind', root, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    (root / 'cli.py').write_text('# legacy helper\n')
    _git(root, 'init', '-q', '-b', 'main')
    _git(root, 'config', 'user.email', 'fixture@example.test')
    _git(root, 'config', 'user.name', 'Fixture')
    _git(root, 'add', '.')
    _git(root, 'commit', '-q', '-m', 'fixture')
    rev = _git(root, 'rev-parse', 'HEAD')
    source = _validate_checkout(root, SourceDeclaration('hivemind', str(root), rev))
    state = source_state_path()
    state.parent.mkdir(parents=True)
    state.write_text(json.dumps({'version': 1, 'active': {'hivemind': source.to_dict()}, 'cached': {}, 'disabled': []}))
    return root, state


def _snapshot(root: Path) -> dict[str, bytes]:
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob('*') if p.is_file()}


@pytest.mark.parametrize('condition', ['clean', 'dirty', 'missing'])
def test_legacy_copy_is_set_aside(legacy: tuple[Path, Path], condition: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, state = legacy
    if condition == 'dirty':
        (root / 'untracked').write_text('keep me')
    elif condition == 'missing':
        shutil.rmtree(root)
    before_state = state.read_bytes()
    before_checkout = _snapshot(root) if root.exists() else None
    packs = [pack for pack in discover_packs_ordered() if pack.id == 'hivemind']
    assert len(packs) == 1
    assert Path(packs[0].root).resolve() == (Path(astrid.__file__).parent / 'core' / 'hivemind').resolve()
    load_default_registry()
    assert all(s.pack_id != 'hivemind' for s in active_source_inventory().sources)
    if condition == 'clean':
        assert any(str(root / 'cli.py') in c for c in _auth_cli_candidates())
    assert _inventory_launch_policy(dict(os.environ), str(tmp_path)) is None
    assert state.read_bytes() == before_state
    assert (_snapshot(root) if root.exists() else None) == before_checkout


def test_other_managed_source_still_verified(legacy: tuple[Path, Path], tmp_path: Path) -> None:
    _, state = legacy
    other = tmp_path / 'example_ext'
    other.mkdir()
    (other / 'pack.yaml').write_text('schema_version: 2\nid: example_ext\nname: Example\nversion: 1.0.0\ncapabilities: [testing]\n')
    _git(other, 'init', '-q', '-b', 'main')
    _git(other, 'config', 'user.email', 'fixture@example.test')
    _git(other, 'config', 'user.name', 'Fixture')
    _git(other, 'add', '.')
    _git(other, 'commit', '-q', '-m', 'fixture')
    source = _validate_checkout(other, SourceDeclaration('example_ext', str(other), _git(other, 'rev-parse', 'HEAD')))
    data = json.loads(state.read_text())
    data['active']['example_ext'] = source.to_dict()
    state.write_text(json.dumps(data))
    assert [s.pack_id for s in active_sources()] == ['example_ext']
    (other / 'untracked').write_text('dirty')
    with pytest.raises(SourceSetupError, match='dirty'):
        active_sources()
