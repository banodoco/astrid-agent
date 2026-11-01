"""Generation discovery reuse must retain admission and observe changed inputs."""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

from astrid.core.generation import cli, entries
from astrid.core.model_catalog.registry import ModelRegistry
from astrid.core.pack import PackValidationError, loader
from astrid.core.pack import discovery, source_setup


def _pack(root, pack_id, *, entry_id=None, backend_id=None):
    directory = root / pack_id
    directory.mkdir(parents=True, exist_ok=True)
    payload = {"schema_version": 3, "id": pack_id, "name": "Before", "version": "1.0.0",
               "documentation": {"kind": "none", "reason": "Metadata fixture."}}
    if entry_id:
        payload["actions"] = {"paint": {
            "description": "A discoverable generation action.",
            "invocation": {"kind": "command", "command": {"argv": ["echo", "{prompt}"]}},
            "inputs": [{"name": "prompt", "type": "string", "required": True}],
            "outputs": [],
            "metadata": {"generation_entry": {"id": entry_id, "kind": "image",
                         "modes": ["t2i"], "inputs": ["prompt"]}},
        }}
    if backend_id:
        payload["extensions"] = {"generation": {"backends": [
            {"id": backend_id, "module": "unused.backend", "class": "UnusedBackend"}]}}
    manifest = directory / "pack.yaml"
    manifest.write_text(json.dumps(payload))
    return manifest


@pytest.fixture
def corpus(tmp_path, monkeypatch):
    root = tmp_path / "source"
    root.mkdir()
    monkeypatch.setattr(loader, "packs_root", lambda: root)
    monkeypatch.setattr(discovery, "REPO_ROOT", tmp_path / "project")
    # SDK element data has its own existing cache, independent of this scope.
    from astrid.core.element.registry import clear_default_registry_cache
    clear_default_registry_cache()
    yield root
    clear_default_registry_cache()


def test_cli_scope_is_per_dry_run_and_is_reset_on_failure(corpus, tmp_path, monkeypatch, capsys):
    _pack(corpus, "demo")
    calls = []
    original = loader.load_pack_manifest

    def load(*args, **kwargs):
        calls.append(args[0])
        return original(*args, **kwargs)

    monkeypatch.setattr(loader, "load_pack_manifest", load)

    def command(argv):
        for _ in range(2):
            discovery.discover_pack_metadata(project_root=tmp_path)
        if "fail" in argv:
            raise RuntimeError("failed request")
        return 0

    monkeypatch.setattr(cli, "_main", command)
    assert cli.main(["--dry-run"]) == 0
    assert len(calls) == 1
    assert cli.main(["--dry-run"]) == 0
    assert len(calls) == 2
    assert cli.main([]) == 0
    assert len(calls) == 4
    assert cli.main(["--dry-run", "--json", "fail"]) == 2
    assert len(calls) == 5
    assert json.loads(capsys.readouterr().out)["status"] == "blocked"
    discovery.discover_pack_metadata(project_root=tmp_path)
    assert len(calls) == 6


def test_reuse_does_not_share_mutable_definitions(corpus, tmp_path):
    _pack(corpus, "demo", backend_id="studio")
    with discovery.pack_discovery_scope():
        first = discovery.discover_pack_metadata(project_root=tmp_path)
        first[0].pack.extensions["generation"]["backends"].clear()
        second = discovery.discover_pack_metadata(project_root=tmp_path)
        assert second[0].pack.extensions["generation"]["backends"][0]["id"] == "studio"


def test_every_layer_and_same_size_manifest_edits_are_observed(corpus, tmp_path, monkeypatch):
    manifests = [_pack(corpus, "bundled"),
                 _pack(tmp_path / "astrid/packs", "local"),
                 _pack(tmp_path / "extra", "extra"),
                 _pack(tmp_path / "env", "external")]
    monkeypatch.setenv("ASTRID_PACKS_PATH", os.pathsep.join(map(str, (tmp_path / "extra", tmp_path / "env"))))
    kwargs = {"project_root": tmp_path, "extra_pack_roots": (str(tmp_path / "extra"),)}
    with discovery.pack_discovery_scope():
        rows = discovery.discover_pack_metadata(**kwargs)
        assert [(row.id, row.source_kind) for row in rows] == [
            ("bundled", "source"), ("local", "local"), ("extra", "extra"), ("external", "env")]
        for manifest in manifests:
            original = manifest.stat()
            manifest.write_text(manifest.read_text().replace('"Before"', '"After!"'))
            os.utime(manifest, ns=(original.st_atime_ns, original.st_mtime_ns))
            changed = discovery.discover_pack_metadata(**kwargs)
            assert next(row for row in changed if row.pack.manifest_path == manifest).pack.name == "After!"
        added = _pack(tmp_path / "env", "added")
        assert "added" in {row.id for row in discovery.discover_pack_metadata(**kwargs)}
        added.unlink()
        assert "added" not in {row.id for row in discovery.discover_pack_metadata(**kwargs)}
        monkeypatch.delenv("ASTRID_PACKS_PATH")
        assert "external" not in {row.id for row in discovery.discover_pack_metadata(**kwargs)}


@pytest.mark.parametrize("damage", ["resource", "alternate", "symlink", "duplicate_yaml"])
def test_changed_tree_is_readmitted_and_invalid_inputs_fail_closed(corpus, tmp_path, damage):
    manifest = _pack(corpus, "demo")
    docs = manifest.parent / "docs/SKILL.md"
    docs.parent.mkdir()
    docs.write_text("# Demo")
    payload = json.loads(manifest.read_text())
    payload["documentation"] = {"kind": "skill", "path": "docs/SKILL.md"}
    manifest.write_text(json.dumps(payload))
    with discovery.pack_discovery_scope():
        discovery.discover_pack_metadata(project_root=tmp_path)
        discovery.discover_pack_metadata(project_root=tmp_path)
        if damage == "resource":
            docs.unlink()
        elif damage == "alternate":
            (manifest.parent / "pack.yml").write_text("{}")
        elif damage == "symlink":
            (manifest.parent / "link").symlink_to(docs)
        else:
            manifest.write_text("schema_version: 3\nid: demo\nid: demo\nname: Demo\nversion: 1.0.0\n")
        with pytest.raises(PackValidationError):
            discovery.discover_pack_metadata(project_root=tmp_path)


def test_changed_schema_is_readmitted(corpus, tmp_path, monkeypatch):
    from astrid.core.pack import canonical
    schemas = tmp_path / "schemas"
    shutil.copytree(canonical._SCHEMA_ROOT, schemas)
    monkeypatch.setattr(canonical, "_SCHEMA_ROOT", schemas)
    monkeypatch.setattr(discovery, "__file__", str(tmp_path / "discovery.py"))
    _pack(corpus, "demo")
    with discovery.pack_discovery_scope():
        discovery.discover_pack_metadata(project_root=tmp_path)
        schema_path = schemas / "v3/pack.json"
        schema = json.loads(schema_path.read_text())
        schema["properties"]["name"] = {"const": "Forbidden"}
        schema_path.write_text(json.dumps(schema))
        with pytest.raises(PackValidationError, match="Forbidden"):
            discovery.discover_pack_metadata(project_root=tmp_path)


def test_scanner_overrides_are_not_memoized(corpus, tmp_path):
    calls = []
    def scanner(*args):
        calls.append(args)
        return ()
    with discovery.pack_discovery_scope():
        for _ in range(2):
            discovery.discover_pack_metadata(project_root=tmp_path, discover_packs_fn=scanner)
    assert len(calls) == 2


def test_symlinked_external_root_tracks_target_contents(corpus, tmp_path, monkeypatch):
    target = tmp_path / "external"
    manifest = _pack(target, "demo")
    link = tmp_path / "external-link"
    link.symlink_to(target, target_is_directory=True)
    monkeypatch.setenv("ASTRID_PACKS_PATH", str(link))
    with discovery.pack_discovery_scope():
        assert discovery.discover_pack_metadata(project_root=tmp_path)[0].pack.name == "Before"
        manifest.write_text(manifest.read_text().replace("Before", "After!"))
        assert discovery.discover_pack_metadata(project_root=tmp_path)[0].pack.name == "After!"


def test_literal_project_root_does_not_alias_expanded_home(corpus, tmp_path, monkeypatch):
    # discover_pack_metadata historically resolves project_root literally;
    # only external pack roots expand '~'. The cache must use the same inputs.
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    _pack(tmp_path / "~/project/astrid/packs", "local")
    with discovery.pack_discovery_scope():
        assert [row.id for row in discovery.discover_pack_metadata(project_root="~/project")] == ["local"]
        assert discovery.discover_pack_metadata(project_root=Path.home() / "project") == ()


def test_changes_during_admission_do_not_publish_a_snapshot(corpus, tmp_path, monkeypatch):
    manifest = _pack(corpus, "demo")
    original = loader.load_pack_manifest
    calls = []
    def load(*args, **kwargs):
        result = original(*args, **kwargs)
        calls.append(result.name)
        if len(calls) == 1:
            manifest.write_text(manifest.read_text().replace("Before", "After!"))
        return result
    monkeypatch.setattr(loader, "load_pack_manifest", load)
    with discovery.pack_discovery_scope():
        assert discovery.discover_pack_metadata(project_root=tmp_path)[0].pack.name == "Before"
        assert discovery.discover_pack_metadata(project_root=tmp_path)[0].pack.name == "After!"
    assert calls == ["Before", "After!"]


def test_model_registry_rediscovers_added_changed_and_duplicate_backends(corpus, tmp_path, monkeypatch):
    from tests.core.model_catalog.test_registry import _make_v2_payload
    raw = _make_v2_payload(modes={"t2i": {"supports": ["prompt"], "requires": ["prompt"],
                                       "backends": {"studio": {"param_map": {"prompt": "prompt"}}}}})
    monkeypatch.setattr("astrid.core.model_catalog.registry._load_yaml", lambda path: raw)
    extra = tmp_path / "extra"
    def load():
        return ModelRegistry.load_default(project_root=tmp_path, extra_pack_roots=(str(extra),))
    with discovery.pack_discovery_scope():
        with pytest.raises(ValueError, match="unknown backend"):
            load()
        manifest = _pack(extra, "extension", backend_id="studio")
        assert load().backend_available("test-model", "t2i", "studio")
        assert load().backend_available("test-model", "t2i", "studio")
        _pack(extra, "collision", backend_id="studio")
        with pytest.raises(ValueError, match="duplicate generation backend id"):
            load()
        shutil.rmtree(extra / "collision")
        manifest.write_text(manifest.read_text().replace('"studio"', '"otherx"'))
        with pytest.raises(ValueError, match="unknown backend"):
            load()
    with discovery.pack_discovery_scope():
        with pytest.raises(ValueError, match="unknown backend"):
            load()


def test_action_projection_reuse_rediscovers_changes_and_ambiguity(corpus, tmp_path, monkeypatch):
    import astrid.sdk.invocation as invocation
    _pack(corpus, "demo", entry_id="paint")
    original = invocation.discover
    calls = []
    def discover(**kwargs):
        calls.append(kwargs)
        return original(**kwargs)
    monkeypatch.setattr(invocation, "discover", discover)
    monkeypatch.setattr(entries, "user_entries", lambda: [])
    with discovery.pack_discovery_scope():
        first = entries.action_entries()
        assert first[0]["id"] == "paint"
        first[0]["id"] = "poisoned"
        assert entries.action_entries()[0]["id"] == "paint"
        assert len(calls) == 1
        _pack(corpus, "demo", entry_id="newid")
        assert entries.action_entries()[0]["id"] == "newid"
        assert len(calls) == 2
        _pack(corpus, "collision", entry_id="newid")
        with pytest.raises(ValueError, match="ambiguous generation entry"):
            entries.describe_entry("newid", registry=ModelRegistry([]))
        assert len(calls) == 3
    entries.action_entries()
    assert len(calls) == 4


@pytest.mark.parametrize("collision", ["user", "catalog", "ready"])
def test_action_collisions_remain_ambiguous_on_reuse(corpus, monkeypatch, collision):
    _pack(corpus, "demo", entry_id="paint")
    monkeypatch.setattr(entries, "user_entries", lambda: [{"id": "paint"}] if collision == "user" else [])
    monkeypatch.setattr(entries, "_ready_entries_for", lambda entry_id: [{"id": "paint"}] if collision == "ready" else [])
    def get(self, entry_id):
        if collision == "catalog":
            return object()
        raise KeyError(entry_id)
    monkeypatch.setattr(ModelRegistry, "get", get)
    monkeypatch.setattr(ModelRegistry, "load_default", classmethod(lambda cls: ModelRegistry([])))
    with discovery.pack_discovery_scope():
        entries.action_entries()
        with pytest.raises(ValueError, match="ambiguous generation entry"):
            entries.find_entry("paint")


def test_find_entry_loads_only_the_requested_ready_workflow(monkeypatch):
    calls = []
    monkeypatch.setattr(entries, "user_entries", lambda: [])
    monkeypatch.setattr(entries, "action_entries", lambda: [])
    monkeypatch.setattr(entries, "ready_entries", lambda: pytest.fail("built unrelated workflows"))
    monkeypatch.setattr(entries, "_ready_entries_for", lambda entry_id: calls.append(entry_id) or [{"id": entry_id}])
    assert entries.find_entry("image/example") == {"id": "image/example"}
    assert calls == ["image/example"]


@pytest.mark.parametrize("lookup", ["packs", "actions"])
def test_managed_checkout_is_verified_even_on_snapshot_hits(corpus, tmp_path, monkeypatch, lookup):
    from tests.core.pack.test_source_setup import _fixture_repo
    repository = tmp_path / "repository"
    revision = _fixture_repo(repository)
    state = tmp_path / "state.json"
    monkeypatch.setenv("ASTRID_SOURCE_STATE", str(state))
    declaration = source_setup.SourceDeclaration("demo", str(repository), revision)
    source_setup.provision((declaration,), state_path=state, data_root=tmp_path / "managed")
    root = source_setup.active_source_inventory().sources[0].pack_root
    def read():
        return (discovery.discover_pack_metadata(project_root=tmp_path) if lookup == "packs"
                else entries.action_entries())
    with discovery.pack_discovery_scope():
        read()
        read()
        (root / "executors/echo/executor.yaml").write_text("tampered")
        with pytest.raises(source_setup.SourceSetupError, match="dirty"):
            read()
