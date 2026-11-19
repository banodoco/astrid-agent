"""A private workflow snapshot is admitted once and remains byte-bound at use."""
import os

import pytest

from astrid.sdk.generation import GenerationInputError, _snapshot_generation_workflow


def _source(tmp_path, *, source_member=True):
    from vibecomfy.workflow import VibeWorkflow, WorkflowSource
    from vibecomfy.workflow_bundle import emit_bundle

    workflow = VibeWorkflow("ls12-snapshot", WorkflowSource("ls12-snapshot"))
    workflow.add_node("SaveImage", _id="1", uid="save", filename_prefix="original")
    bundle = emit_bundle(workflow, tmp_path / "workflow.py", {"operation": "authored"})
    if source_member:
        bundle.python_path.with_name("source.json").write_bytes(b'{"source":"old"}')
    return bundle.python_path


def test_nested_preparation_reuses_one_admitted_private_pair(tmp_path, monkeypatch):
    from vibecomfy import workflow_bundle as bundles

    source = _source(tmp_path)
    calls = []
    load, publish = bundles.load_bundle, bundles._atomic_publish_pair

    def observed_load(*args, **kwargs):
        calls.append("load")
        return load(*args, **kwargs)

    def observed_publish(*args, **kwargs):
        calls.append("publish")
        return publish(*args, **kwargs)

    monkeypatch.setattr(bundles, "load_bundle", observed_load)
    monkeypatch.setattr(bundles, "_atomic_publish_pair", observed_publish)
    with _snapshot_generation_workflow(source) as frozen:
        assert frozen != source
        with _snapshot_generation_workflow(frozen) as nested:
            assert nested == frozen
            assert calls == ["load", "publish"]
            assert nested.read_bytes() == source.read_bytes()
            assert nested.with_name("source.json").read_bytes() == b'{"source":"old"}'
        assert frozen.is_file()
    assert not frozen.exists()
    with _snapshot_generation_workflow(source) as later:
        assert later != frozen
        assert calls == ["load", "publish", "load", "publish"]
    assert not later.exists()


@pytest.mark.parametrize("member", ["python", "companion", "source", "absent_source"])
def test_nested_first_use_refuses_edited_admitted_members(tmp_path, member):
    source = _source(tmp_path, source_member=member != "absent_source")
    with _snapshot_generation_workflow(source) as frozen:
        path = {"python": frozen, "companion": frozen.with_suffix(".vibe.json"),
                "source": frozen.with_name("source.json"),
                "absent_source": frozen.with_name("source.json")}[member]
        if member == "absent_source":
            path.write_bytes(b'{"source":"new"}')
        else:
            stamp = path.stat()
            original = path.read_bytes()
            old, new = {"python": (b"original", b"tampered"),
                        "companion": (b'"format_version":2', b'"format_version":3'),
                        "source": (b"old", b"new")}[member]
            changed = original.replace(old, new, 1)
            assert changed != original and len(changed) == len(original)
            path.write_bytes(changed)
            os.utime(path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
            assert path.stat().st_size == stamp.st_size
            assert path.stat().st_mtime_ns == stamp.st_mtime_ns
        with pytest.raises(GenerationInputError, match="admitted workflow source changed"):
            with _snapshot_generation_workflow(frozen):
                pytest.fail("edited bytes were reused as the admitted snapshot")


def test_nested_json_snapshot_is_held_only_for_the_outer_operation(tmp_path):
    source = tmp_path / "workflow.json"
    source.write_bytes(b'{"nodes":[]}')
    with _snapshot_generation_workflow(source) as frozen:
        with _snapshot_generation_workflow(frozen) as nested:
            assert nested == frozen and nested.read_bytes() == source.read_bytes()
    with pytest.raises(GenerationInputError):
        with _snapshot_generation_workflow(frozen):
            pytest.fail("a completed operation retained authority over its deleted snapshot")


def test_snapshot_reuse_normalizes_the_temporary_directory(tmp_path, monkeypatch):
    import tempfile

    source = tmp_path / "workflow.json"
    source.write_bytes(b'{"nodes":[]}')
    (tmp_path / "alias").mkdir()
    # The system temp root can have an alias on macOS. A lexical alias is
    # portable and exercises normalization without needing symlink privileges.
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path / "alias" / ".."))
    with _snapshot_generation_workflow(source) as frozen:
        assert frozen == frozen.resolve()
        with _snapshot_generation_workflow(frozen) as nested:
            assert nested == frozen and nested.read_bytes() == source.read_bytes()
