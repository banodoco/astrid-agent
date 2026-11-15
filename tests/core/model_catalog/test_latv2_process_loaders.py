"""Process loader reuse preserves content freshness and caller isolation."""
from copy import deepcopy
from unittest.mock import patch

import pytest

from astrid.core.extensions import registry as extensions
from astrid.core.model_catalog import registry as models


@pytest.fixture(autouse=True)
def reset_loaders():
    for module in (models, extensions):
        reset = getattr(module, '_reset_for_tests', lambda: None)
        reset()
    yield
    for module in (models, extensions):
        getattr(module, '_reset_for_tests', lambda: None)()


def test_model_default_parses_once_per_content_key(tmp_path, monkeypatch):
    with patch.object(models, '_load_yaml', wraps=models._load_yaml) as read:
        first = models.ModelRegistry.load_default(project_root=tmp_path)
        expected = deepcopy(first.list_all(include_closed=True))
        for _ in range(3):
            assert models.ModelRegistry.load_default(project_root=tmp_path).list_all(include_closed=True) == expected
        assert read.call_count == 1
        first.list_all(include_closed=True)[0].modes.clear()
        first._by_id.clear()
        assert models.ModelRegistry.load_default(project_root=tmp_path).list_all(include_closed=True) == expected
        monkeypatch.setenv('ASTRID_PACKS_PATH', str(tmp_path / 'different'))
        assert models.ModelRegistry.load_default(project_root=tmp_path).list_all(include_closed=True) == expected
        assert read.call_count == 2
        models.ModelRegistry.load_default(project_root=tmp_path / 'other')
        assert read.call_count == 3


def test_extensions_scan_once_across_instances_and_isolate_mutation(tmp_path):
    with patch.object(extensions.ExtensionRegistry, '_packs', autospec=True,
                      side_effect=extensions.ExtensionRegistry._packs) as scan:
        first = extensions.ExtensionRegistry().load()
        expected = deepcopy(first.points())
        for _ in range(3):
            assert extensions.ExtensionRegistry().load().points() == expected
        assert scan.call_count == 1
        first.points()[0].schema['caller mutation'] = True
        first._snap.points.clear()
        first._winners.clear()
        assert extensions.ExtensionRegistry().load().points() == expected
        assert scan.call_count == 1


def test_extensions_same_timestamp_content_and_config_changes(tmp_path, monkeypatch):
    import os

    from astrid.pack_api import VERSION
    from tests.core.extensions.test_registry import POINT_DATA, _contribution
    collection = tmp_path / 'packs'
    pack = collection / 'latv2'
    point = pack / 'extensions/points/widget.yaml'
    point.parent.mkdir(parents=True)
    point.write_text(POINT_DATA)
    manifest = pack / 'extensions/latv2.widget/one.yaml'
    manifest.parent.mkdir(parents=True)
    manifest.write_text(_contribution(summary='first'))
    (pack / 'pack.yaml').write_text(f'schema_version: 3\nid: latv2\nname: Test\nversion: 1.0.0\npack_api: "{VERSION[0]}.{VERSION[1]}"\nactions: {{}}\n')
    monkeypatch.setenv('ASTRID_PACKS_PATH', str(collection))
    monkeypatch.chdir(tmp_path)
    config = tmp_path / '.astrid/config.toml'
    config.parent.mkdir()
    registry = extensions.ExtensionRegistry()
    with patch.object(extensions.ExtensionRegistry, '_packs', autospec=True,
                      side_effect=extensions.ExtensionRegistry._packs) as scan:
        assert registry.load().resolve('latv2.widget', 'one').summary == 'first'
        stamp = manifest.stat()
        manifest.write_text(_contribution(summary='other'))
        os.utime(manifest, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
        assert registry.load().resolve('latv2.widget', 'one').summary == 'other'
        assert scan.call_count == 2
        config.write_text('[extensions]\ndisable = ["latv2.one"]\n')
        assert not registry.load().contributions('latv2.widget')
        config.write_text('[extensions]\ndisable = []\n')
        assert registry.load().resolve('latv2.widget', 'one').summary == 'other'
        registry.load(refresh=True)
        assert scan.call_count == 3
        # Unreadable/racing full identities must not fall back to stat reuse.
        monkeypatch.setattr(registry, '_process_key', lambda roots: None)
        manifest.write_text(_contribution(summary='third'))
        os.utime(manifest, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
        assert registry.load().resolve('latv2.widget', 'one').summary == 'third'
        assert scan.call_count == 4


def test_model_admission_is_not_repeated_and_matches_uncached(tmp_path):
    from astrid.core.pack import loader
    with patch.object(loader, 'load_pack_manifest', wraps=loader.load_pack_manifest) as read:
        first = models.ModelRegistry.load_default(project_root=tmp_path)
        admitted = read.call_count
        assert admitted > 0
        for _ in range(3):
            assert models.ModelRegistry.load_default(project_root=tmp_path).list_all() == first.list_all()
        assert read.call_count == admitted
        models._reset_for_tests()
        assert models.ModelRegistry.load_default(project_root=tmp_path).list_all() == first.list_all()
        assert read.call_count == 2 * admitted


def test_model_yaml_same_timestamp_change_is_a_miss(tmp_path, monkeypatch):
    import os
    source = models.Path(models.__file__).with_name('models.yaml')
    catalog = tmp_path / 'models.yaml'
    catalog.write_bytes(source.read_bytes())
    monkeypatch.setattr(models, '__file__', str(tmp_path / 'registry.py'))
    with patch.object(models, '_load_yaml', wraps=models._load_yaml) as read:
        expected = models.ModelRegistry.load_default(project_root=tmp_path).list_all()
        stamp = catalog.stat()
        # A YAML comment changes content without changing the registry value.
        text = catalog.read_text()
        assert '#' in text
        offset = text.index('#') + 1
        catalog.write_text(text[:offset] + ('x' if text[offset] != 'x' else 'y') + text[offset + 1:])
        os.utime(catalog, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
        assert models.ModelRegistry.load_default(project_root=tmp_path).list_all() == expected
        assert read.call_count == 2


def test_model_pack_content_change_is_a_miss(tmp_path):
    from tests.core.generation.test_discovery_scope import _pack
    root = tmp_path / 'extra'
    manifest = _pack(root, 'latv2', backend_id='studio')
    import json

    from astrid.pack_api import VERSION
    payload = json.loads(manifest.read_text())
    payload['pack_api'] = f'{VERSION[0]}.{VERSION[1]}'
    manifest.write_text(json.dumps(payload))
    kwargs = {'project_root': tmp_path, 'extra_pack_roots': (str(root),)}
    with patch.object(models, '_load_yaml', wraps=models._load_yaml) as read:
        first = models.ModelRegistry.load_default(**kwargs).list_all()
        assert models.ModelRegistry.load_default(**kwargs).list_all() == first
        assert read.call_count == 1
        manifest.write_text(manifest.read_text().replace('Before', 'After!'))
        assert models.ModelRegistry.load_default(**kwargs).list_all() == first
        assert read.call_count == 2


def test_extension_snapshot_matches_forced_reload():
    registry = extensions.ExtensionRegistry().load()
    expected = (registry.points(), registry.problems(),
                [registry.contributions(point.id) for point in registry.points()])
    registry.load(refresh=True)
    assert (registry.points(), registry.problems(),
            [registry.contributions(point.id) for point in registry.points()]) == expected


def test_extension_validator_content_changes_invalidate(tmp_path, monkeypatch):
    import os
    import shutil

    from astrid.core.extensions import model
    schemas = tmp_path / 'schemas'
    shutil.copytree(model.SCHEMA_DIR, schemas)
    monkeypatch.setattr(model, 'SCHEMA_DIR', schemas)
    schema = schemas / 'contribution.json'
    external = tmp_path / 'validator.json'
    external.write_bytes(schema.read_bytes())
    schema.unlink()
    schema.symlink_to(external)
    with patch.object(extensions, '_parse', wraps=extensions._parse) as parse:
        extensions.ExtensionRegistry().load()
        extensions.ExtensionRegistry().load()
        assert parse.call_count == 1
        schema = schemas / 'contribution.json'
        stamp = schema.stat()
        schema.write_bytes(schema.read_bytes() + b' ')
        os.utime(schema, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
        extensions.ExtensionRegistry().load()
        assert parse.call_count == 2


def test_extension_sdk_root_changes_invalidate(tmp_path):
    from tests.core.extensions.test_registry import POINT_DATA
    sdk = tmp_path / 'sdk/extensions'
    point = sdk / 'points/widget.yaml'
    point.parent.mkdir(parents=True)
    point.write_text(POINT_DATA)
    with patch.object(extensions.ExtensionRegistry, '_packs', autospec=True,
                      side_effect=extensions.ExtensionRegistry._packs) as scan:
        assert 'timeline.widget' in [p.id for p in extensions.ExtensionRegistry(sdk_root=sdk).load().points()]
        extensions.ExtensionRegistry(sdk_root=sdk).load()
        assert scan.call_count == 1
        extensions.ExtensionRegistry(sdk_root=tmp_path / 'other/extensions').load()
        assert scan.call_count == 2


def test_verified_source_pin_identity_changes_invalidate(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from astrid.core.pack import source_setup
    identity = ['pin-one']
    monkeypatch.setattr(source_setup, 'active_source_inventory',
                        lambda: SimpleNamespace(identity=identity[0], sources=()))
    with patch.object(models, '_load_yaml', wraps=models._load_yaml) as read:
        models.ModelRegistry.load_default(project_root=tmp_path)
        models.ModelRegistry.load_default(project_root=tmp_path)
        assert read.call_count == 1
        identity[0] = 'pin-two'
        models.ModelRegistry.load_default(project_root=tmp_path)
        assert read.call_count == 2
    with patch.object(extensions, '_parse', wraps=extensions._parse) as parse:
        extensions.ExtensionRegistry().load()
        extensions.ExtensionRegistry().load()
        assert parse.call_count == 1
        identity[0] = 'pin-three'
        extensions.ExtensionRegistry().load()
        assert parse.call_count == 2


def test_relative_contribution_roots_follow_cwd(tmp_path, monkeypatch):
    from pathlib import Path

    from astrid.core.extensions.scopes import Root
    from astrid.core.pack import loader
    first, second = tmp_path / 'first', tmp_path / 'second'
    first.mkdir()
    second.mkdir()
    (first / 'packs').symlink_to(loader.packs_root(), target_is_directory=True)
    (second / 'packs').mkdir()
    roots = (Root('user', Path('packs'), True, 'user'),)
    with patch.object(extensions.ExtensionRegistry, '_packs', autospec=True,
                      side_effect=extensions.ExtensionRegistry._packs) as scan:
        monkeypatch.chdir(first)
        one = extensions.ExtensionRegistry(roots=roots).load()
        contributions = [c for p in one.points() for c in one.contributions(p.id)]
        assert contributions and {c.scope for c in contributions} == {'user'}
        monkeypatch.chdir(second)
        two = extensions.ExtensionRegistry(roots=roots).load()
        contributions = [c for p in two.points() for c in two.contributions(p.id)]
        assert contributions and {c.scope for c in contributions} == {'installed'}
        assert scan.call_count == 2


def test_extension_racing_admission_cannot_relabel_or_publish_snapshot(monkeypatch):
    registry = extensions.ExtensionRegistry()
    key = registry._process_key(registry._current_roots())
    keys = iter([key, ('changed-during-admission',), key, key])
    monkeypatch.setattr(registry, '_process_key', lambda roots: next(keys))
    with patch.object(extensions, '_parse', wraps=extensions._parse) as parse:
        expected = deepcopy(registry.load().points())
        registry._snap.points.clear()
        registry._loaded['stale runtime import'] = object()
        assert registry.load().points() == expected
        assert parse.call_count == 2
        assert not registry._loaded


def test_extension_reset_does_not_publish_caller_mutated_instance():
    registry = extensions.ExtensionRegistry().load()
    expected = deepcopy(registry.points())
    registry._snap.points.clear()
    extensions._reset_for_tests()
    assert registry.load().points() == expected
    assert extensions.ExtensionRegistry().load().points() == expected
