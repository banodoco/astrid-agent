"""Controller hooks and transport through a registered test pack."""
from __future__ import annotations

from pathlib import Path
from shutil import copyfile
from types import SimpleNamespace

import pytest

from astrid.core.extensions import registry as registry_module
from astrid.core.extensions.registry import ExtensionRegistry
from astrid.core.extensions.scopes import Root
from astrid.core.generation import entries, workflows


@pytest.fixture
def fake_workflow_pack(monkeypatch, tmp_path):
    source = Path(__file__).resolve().parents[3] / "astrid/packs/vibecomfy/extensions/points"
    point_owner = tmp_path / "vibecomfy"
    points = point_owner / "extensions" / "points"
    points.mkdir(parents=True)
    for name in ("workflows.yaml", "workflows.schema.json"):
        copyfile(source / name, points / name)

    fake = tmp_path / "example"
    folder = fake / "extensions" / "vibecomfy.workflows"
    folder.mkdir(parents=True)
    (fake / "ready").mkdir()
    (folder / "controller.yaml").write_text(
        """api: 1
summary: Test workflow controller
example: Generate with example
effects: none
ready_root: ../../ready
entries:
  - id: example
    kind: video
    modes: [v2v]
    bundle: workflow.py
    inputs:
      - {name: source_files, type: file_map, allow_output_handle_keys: true}
      - {name: ordinary_files, type: file_map}
inspect_inputs: hooks.py:inspect_inputs
build: hooks.py:build
publish_source: true
""",
        encoding="utf-8",
    )
    (folder / "hooks.py").write_text(
        """from astrid.core.generation.workflows import BuildTransport, InputRequirements, RemoteBuildTransport

def inspect_inputs(inputs):
    if 'reserved' in inputs:
        raise ValueError('reserved input')
    if not inputs:
        return InputRequirements()
    return InputRequirements(tuple(inputs['handles']), inputs['target'], 'request.media')

def build(inputs, files, out, *, publish_source):
    path, descriptor = publish_source(out / 'prepared.mp4', 'a' * 64)
    lineage = {'source': {'digest': descriptor['digest']}}
    remote = RemoteBuildTransport({'source_files': {'video': path}}, descriptor, {'prepared': lineage})
    return {'workflow': 'graph', 'transport': BuildTransport(lineage, False, remote)}
""",
        encoding="utf-8",
    )
    root = Root("installed", tmp_path, True, "test pack")
    fake_packs = [
        SimpleNamespace(id=path.name, pack_dir=path, pack=SimpleNamespace(version="1.0"))
        for path in (point_owner, fake)
    ]
    monkeypatch.setattr(registry_module, "disabled_ids", lambda: frozenset())
    registry = ExtensionRegistry(roots=[root], sdk_root=tmp_path / "sdk", discover=lambda **_: fake_packs).load()
    assert not registry.problems()
    monkeypatch.setattr("astrid.core.extensions.scopes.contribution_roots", lambda: [root])
    monkeypatch.setattr(entries, "user_entries", entries.contributed_entries)
    monkeypatch.setattr(entries, "action_entries", lambda: [])
    return registry


def test_input_inspection_uses_the_controller_hook(fake_workflow_pack):
    def inspect(inputs):
        return entries.inspect_entry_inputs("example", inputs, registry=fake_workflow_pack)

    handles = ("run:run-37/video#1", "ref:landscape")
    assert inspect({}) == workflows.InputRequirements()
    assert inspect({"handles": handles, "target": "source_files"}) == workflows.InputRequirements(
        handles, "source_files", "request.media"
    )
    with pytest.raises(ValueError, match="reserved input"):
        inspect({"reserved": True})
    with pytest.raises(ValueError, match="declared handle-compatible file map"):
        inspect({"handles": handles, "target": "ordinary_files"})
    with pytest.raises(ValueError, match="invalid handle requirements"):
        inspect({"handles": ("not a handle",), "target": "source_files"})


def test_output_handle_keys_are_allowed_only_on_opted_in_file_maps(fake_workflow_pack, tmp_path):
    entry = entries.find_entry("example")
    handle = "run:run-37/video#1"
    files = {handle: "/absolute/source.mp4"}
    supplied = {"source_files": files}
    assert entries.validate_inputs(entry, supplied) == supplied
    with pytest.raises(ValueError, match="requires a nonempty map"):
        entries.validate_inputs(entry, {"ordinary_files": files})
    assert entries.entry_job(entry, supplied, tmp_path, hooks={})["files"] == supplied
    with pytest.raises(ValueError, match="requires a nonempty map"):
        entries.entry_job(entry, {"ordinary_files": files}, tmp_path, hooks={})

    document = tmp_path / "assets.json"
    document.write_text('{"run:run-37/video#1": "source.mp4"}', encoding="utf-8")
    job = entries.entry_job(entry, {"source_files": document}, tmp_path, hooks={})
    assert job["files"] == {"source_files": {handle: str(tmp_path / "source.mp4")}}
    with pytest.raises(ValueError, match="file map document"):
        entries.entry_job(entry, {"ordinary_files": document}, tmp_path, hooks={})


def test_opted_in_build_returns_validated_transport(fake_workflow_pack, tmp_path):
    entry = entries.find_entry("example")
    assert entries.publishes_source(entry, registry=fake_workflow_pack)
    assert not entries.publishes_source({**entry, "id": "other"}, registry=fake_workflow_pack)
    hooks = entries.entry_hooks(entry, registry=fake_workflow_pack)
    assert set(hooks) == {"build"}
    prepared = tmp_path / "published.mp4"
    descriptor = {"digest": "sha256:" + "a" * 64}

    def publish_source(path, expected):
        assert path == tmp_path / "prepared.mp4"
        assert expected == "a" * 64
        return prepared, descriptor

    result = hooks["build"]({}, {}, tmp_path, publish_source=publish_source)
    lineage = {"source": {"digest": descriptor["digest"]}}
    assert workflows.build_transport(result) == workflows.BuildTransport(
        lineage, False, workflows.RemoteBuildTransport(
            {"source_files": {"video": prepared}}, descriptor, {"prepared": lineage},
        ),
    )
    assert workflows.build_transport({}) == workflows.BuildTransport()
    with pytest.raises(ValueError, match="invalid transport"):
        workflows.build_transport({"transport": workflows.BuildTransport(preflight_source_video=1)})
    with pytest.raises(ValueError, match="invalid remote transport source descriptor"):
        workflows.build_transport({"transport": workflows.BuildTransport(
            remote=workflows.RemoteBuildTransport({}, {"digest": "not a digest"}, {"prepared": lineage}),
        )})
    with pytest.raises(ValueError, match="inconsistent remote transport source digests"):
        workflows.build_transport({"transport": workflows.BuildTransport(
            remote=workflows.RemoteBuildTransport({}, {**descriptor, "sha256": "b" * 64}, {"prepared": lineage}),
        )})


def test_controller_resolves_handles_before_target_preflight(fake_workflow_pack, monkeypatch, tmp_path):
    from astrid.sdk.generation import GenerationFacade

    monkeypatch.setattr(ExtensionRegistry, "load", lambda _: fake_workflow_pack)
    handle = "run:run-37/video#1"
    digest = "sha256:" + "a" * 64
    output = {"run_id": "run-37", "task_id": "task-1", "output_port": "video", "ordinal": 1}
    staged = []

    def show_media(project, requested):
        assert (project, requested) == ("test-project", digest)
        return {"size_bytes": 5, "media_type": "video/mp4"}

    def open_media(project, requested, *, materialize, cache_root, preview_bytes, max_bytes):
        assert (project, requested) == ("test-project", digest)
        assert materialize and preview_bytes == 0 and max_bytes == 5
        cache_root.mkdir(parents=True)
        opened = cache_root / "opened.mp4"
        opened.write_bytes(b"media")
        staged.append(opened)
        return SimpleNamespace(ok=True, data={"local_path": str(opened)})

    def show_run(run):
        assert run == "run-37"
        return {"task_ids": ["task-1"]}

    def list_outputs(task):
        assert task == "task-1"
        return [{**output, "digest": digest}]

    client = SimpleNamespace(
        media=SimpleNamespace(show=show_media, open=open_media),
        runs=SimpleNamespace(show=show_run),
        tasks=SimpleNamespace(list_managed_outputs=list_outputs),
    )
    document = tmp_path / "assets.json"
    document.write_text('{"image": "reference.png"}', encoding="utf-8")

    def preflight(self, *, inputs, idempotency_context, dry_run, **kwargs):
        assert dry_run and kwargs["project"] == "test-project"
        assert inputs["source_files"] == {"image": str(tmp_path / "reference.png"), handle: str(staged[0])}
        assert staged[0].read_bytes() == b"media"
        assert idempotency_context == {
            "caller": True,
            "media_handles": [{"port": "request.media", "handle": handle, "digest": digest, "output": output}],
        }
        return "preflight complete"

    monkeypatch.setattr(GenerationFacade, "_generate_on_target_impl", preflight)
    result = GenerationFacade()._generate_on_target(
        model="example", dry_run=True, client=client, project="test-project",
        inputs={"handles": [handle], "target": "source_files", "source_files": str(document)},
        idempotency_context={"caller": True},
    )
    assert result == "preflight complete"
    assert len(staged) == 1 and not staged[0].exists()


def test_controller_hook_matches_the_discovered_pack(fake_workflow_pack, tmp_path):
    entry = entries.find_entry("example")
    overlapping_entry = {**entry, "pack_root": str(tmp_path / "different" / "example")}
    with pytest.raises(ValueError, match="workflow input inspection is unavailable"):
        entries.inspect_entry_inputs(overlapping_entry, {}, registry=fake_workflow_pack)
    assert not entries.publishes_source(overlapping_entry, registry=fake_workflow_pack)
