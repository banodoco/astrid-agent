"""RA-2a adapter/store proof: real portable recipes, isolated local data roots."""
from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from astrid.core.generation import prepared_templates as prepared
from astrid.sdk.command_timing import (
    TimingCollector, TimingOutput, print_cache_events, timing_scope,
)
from vibecomfy.prepared_template import ValueSlot, prepare_template
from vibecomfy.workflow import VibeInput, VibeWorkflow, WorkflowSource


def _workflow():
    workflow = VibeWorkflow("agent-prepared", WorkflowSource("agent-prepared"))
    workflow.add_node("Integer", _id="1", uid="seed-node", value=7)
    workflow.inputs["seed"] = VibeInput("seed", "1", "value", 7, type="INT", default=7)
    workflow._manual_input_names.add("seed")
    return workflow


def _slots():
    return (ValueSlot("seed", (("nodes", "1", "inputs", "value"),
                               ("inputs", "seed", "value")), "integer"),)


def _context(source: Path):
    return {
        "compiler_identity": {"content": "fixture-compiler-v1"},
        "source_provenance": {"content": prepared._digest(source.read_bytes())},
        "vibecomfy_version": "fixture-v1",
        "role_plan": {"model": "fixture", "roles": []},
        "node_list_digest": prepared._digest(b'{"Integer":{}}'),
    }


@pytest.fixture
def harness(tmp_path, monkeypatch):
    source = tmp_path / "source.py"
    source.write_text("fixed source v1\n")
    root = tmp_path / "runtime-data"
    monkeypatch.setenv("ASTRID_RUNTIME_DATA_ROOT", str(root))
    store = prepared.PreparedTemplateStore()
    builds = []
    context = _context(source)

    def witness():
        return {**context, "source_provenance": {"content": prepared._digest(source.read_bytes())}}

    def build():
        builds.append("build")
        return _workflow()

    def use(seed=11, **kwargs):
        return prepared.prepare_workflow(identity={"source": "fixture"}, witness=witness,
            build=build, store=store, slots=_slots(), values={"seed": seed}, **kwargs)

    return source, store, builds, context, use


def _events(collector):
    return [(row["outcome"], row.get("reason")) for row in collector.cache_event_snapshot()]


def test_changed_values_fill_without_rebuilding_and_use_existing_data_root(harness):
    source, store, builds, _, use = harness
    first = use(11)
    first.published()
    collector = TimingCollector()
    with timing_scope(collector):
        second = use(29)
    assert builds == ["build"]
    assert first.workflow.nodes["1"].inputs["value"] == 11
    assert second.workflow.nodes["1"].inputs["value"] == 29
    assert second.workflow.inputs["seed"].default == 7
    assert first.filled.snapshot().key == second.filled.snapshot().key
    assert store.root == source.parent / "runtime-data/cache/prepared-template/v1/comfyui"
    assert _events(collector) == [("hit", None)]
    assert any(row["stage"] == "prepared template key" for row in collector.snapshot())
    assert not any(row["stage"] == "workflow build" for row in collector.snapshot())


@pytest.mark.parametrize("field", ["compiler_identity", "vibecomfy_version", "role_plan", "node_list_digest"])
def test_current_key_input_change_rebuilds_before_hit(harness, field):
    _, _, builds, context, use = harness
    use().published()
    context[field] = prepared._digest(b"new nodes") if field == "node_list_digest" else (
        "fixture-v2" if field == "vibecomfy_version" else {"changed": True})
    collector = TimingCollector()
    with timing_scope(collector):
        use(17).published()
    assert builds == ["build", "build"]
    assert _events(collector)[:2] == [("miss", "key_changed:" + field),
                                     ("recompute", "key_changed:" + field)]
    assert ("hit", None) not in _events(collector)


def test_current_source_edit_rebuilds(harness):
    source, _, builds, _, use = harness
    use().published()
    source.write_text("fixed source v2\n")
    collector = TimingCollector()
    with timing_scope(collector):
        use().published()
    assert builds == ["build", "build"]
    assert _events(collector)[:2] == [("miss", "key_changed:source_provenance"),
                                     ("recompute", "key_changed:source_provenance")]


@pytest.mark.parametrize("damage", ["truncated", "foreign", "digest", "duplicate", "wrong_object"])
def test_bad_or_foreign_entry_is_an_explicit_miss(harness, damage):
    _, store, builds, _, use = harness
    first = use()
    first.published()
    path = store.root / "objects" / (first.filled.snapshot().key[7:] + ".json")
    if damage == "truncated":
        path.write_bytes(b'{"format":')
    elif damage == "duplicate":
        path.write_bytes(b'{"format":"one","format":"two"}')
    else:
        record = json.loads(path.read_bytes())
        if damage == "foreign":
            record["backend"] = "another-backend"
        elif damage == "digest":
            record["content_digest"] = prepared._digest(b"wrong")
        else:
            record["key"] = prepared._digest(b"another object")
        path.write_text(json.dumps(record))
    collector = TimingCollector()
    with timing_scope(collector):
        current = use(23)
        current.published()
    assert current.workflow.inputs["seed"].value == 23
    assert builds == ["build", "build"]
    assert _events(collector)[:2] == [("miss", "entry_invalid"), ("recompute", "entry_invalid")]


@pytest.mark.parametrize("hint", [b"{", b'[]', b'{"format":"foreign","key":"x"}',
                                    b'{"format":"x","format":"x"}'])
def test_invalid_hint_cannot_select_an_entry(harness, hint):
    _, store, builds, _, use = harness
    use().published()
    next((store.root / "sources").glob("*.json")).write_bytes(hint)
    collector = TimingCollector()
    with timing_scope(collector):
        use()
    assert len(builds) == 2
    assert _events(collector)[:2] == [("miss", "entry_invalid"), ("recompute", "entry_invalid")]


def test_store_deletion_only_causes_a_cold_preparation(harness):
    _, store, builds, _, use = harness
    use().published()
    shutil.rmtree(store.root)
    collector = TimingCollector()
    with timing_scope(collector):
        current = use(31)
        current.published()
    assert current.workflow.inputs["seed"].value == 31
    assert len(builds) == 2
    assert _events(collector)[:2] == [("miss", "absent"), ("recompute", "absent")]
    assert list((store.root / "objects").glob("*.json"))


@pytest.mark.parametrize("group", ["sources", "objects"])
def test_every_store_read_failure_is_visible_in_json_and_timings(harness, monkeypatch, group):
    _, store, builds, _, use = harness
    use().published()
    original = store._read

    def fail(path, limit):
        if path.parent.name == group:
            raise PermissionError("cache permission fixture")
        return original(path, limit)

    monkeypatch.setattr(store, "_read", fail)
    collector = TimingCollector()
    with timing_scope(collector):
        current = use(37)
    assert len(builds) == 2
    assert current.workflow.inputs["seed"].value == 37
    output, human = io.StringIO(), io.StringIO()
    TimingOutput(output, collector).write(json.dumps({"ok": True}))
    print_cache_events(collector.cache_event_snapshot(), file=human)
    assert json.loads(output.getvalue())["cache_events"][:2] == [
        {"cache": "prepared_template", "outcome": "miss", "reason": "store_unreadable"},
        {"cache": "prepared_template", "outcome": "recompute", "reason": "store_unreadable"},
    ]
    assert "cache prepared_template: miss (store_unreadable)" in human.getvalue()


@pytest.mark.parametrize("group", ["objects", "sources"])
def test_every_store_write_failure_preserves_result_and_reports_miss(harness, monkeypatch, group):
    _, _, _, _, use = harness
    current = use(41)
    original = prepared.write_bytes_atomic

    def fail(path, data):
        if path.parent.name == group:
            raise OSError("cache write fixture")
        return original(path, data)

    monkeypatch.setattr(prepared, "write_bytes_atomic", fail)
    collector = TimingCollector()
    with timing_scope(collector):
        current.published()
    assert current.workflow.inputs["seed"].value == 41
    assert ("miss", "store_unreadable") in _events(collector)


@pytest.mark.parametrize("operation", ["open", "acquire", "release", "close"])
def test_every_lock_failure_is_visible_and_does_not_swallow_build(harness, monkeypatch, operation):
    _, _, builds, _, use = harness
    real_close, real_unlock = prepared.os.close, prepared._filelock.unlock_fd

    def fail(*args, **kwargs):
        if operation == "close":
            real_close(args[0])
        if operation == "release":
            real_unlock(args[0])
        raise OSError("cache lock fixture")

    if operation == "open":
        monkeypatch.setattr(prepared.os, "open", fail)
    elif operation == "close":
        monkeypatch.setattr(prepared.os, "close", fail)
    else:
        monkeypatch.setattr(prepared._filelock, "lock_fd" if operation == "acquire" else "unlock_fd", fail)
    collector = TimingCollector()
    with timing_scope(collector):
        current = use(43)
    assert builds == ["build"]
    assert current.workflow.inputs["seed"].value == 43
    assert ("miss", "store_unreadable") in _events(collector)


def test_build_errors_keep_their_normal_refusal(tmp_path):
    source = tmp_path / "source.py"
    source.write_text("source")

    def refuse():
        raise RuntimeError("product refused current workflow")

    with pytest.raises(RuntimeError, match="product refused"):
        prepared.prepare_workflow(identity="fixture", witness=lambda: _context(source),
            build=refuse, store=prepared.PreparedTemplateStore(tmp_path))


def test_source_change_during_preparation_does_not_publish_stale_entry(tmp_path):
    source = tmp_path / "source.py"
    source.write_text("before")
    store = prepared.PreparedTemplateStore(tmp_path)

    def build():
        workflow = _workflow()
        source.write_text("after")
        return workflow

    collector = TimingCollector()
    with timing_scope(collector):
        current = prepared.prepare_workflow(identity="fixture", witness=lambda: _context(source),
            build=build, store=store)
        current.published()
    assert current.filled is None
    assert not list(store.root.glob("objects/*.json"))
    assert ("miss", "key_changed:source_provenance") in _events(collector)
    assert ("hit", None) not in _events(collector)


def test_current_structure_is_checked_for_already_built_workflows(harness):
    source, _, builds, _, use = harness
    use().published()
    changed = _workflow()
    changed.nodes["1"].class_type = "AnotherInteger"
    current = prepare_template(changed, _slots(), key_context=_context(source))
    collector = TimingCollector()
    with timing_scope(collector):
        use(current_template=current)
    assert len(builds) == 2
    assert _events(collector)[:2] == [("miss", "key_changed:structure"), ("recompute", "key_changed:structure")]


def test_unsupported_builder_never_claims_a_hit(harness):
    _, _, builds, _, use = harness
    use().published()
    collector = TimingCollector()
    with timing_scope(collector):
        current = use(reusable=False, detail="fixture/dynamic: executable hook")
    assert len(builds) == 2
    assert current.filled is None
    assert _events(collector) == [("miss", "entry_invalid"), ("recompute", "entry_invalid")]


_CLOSED = b'''from __future__ import annotations
from vibecomfy.templates import ReadyMetadata, new_workflow
from vibecomfy.nodes.core import EmptyImage, SaveImage
READY_METADATA = ReadyMetadata.build(capability="image", inputs={})
def build():
    wf = new_workflow(READY_METADATA, source_path=__file__)
    image = EmptyImage(_id="1", width=512, height=512)
    saved = SaveImage(_id="2", filename_prefix="test", images=image)
    return wf.finalize({}, output_node=saved, output_type="SaveImage")
'''


def test_closed_source_policy_accepts_declarative_assembly():
    assert prepared.declarative_source(_CLOSED)


@pytest.mark.parametrize("extra", [b'\nimport time\n', b'\nimport os\n',
    b'\ndef helper():\n    return 1\n', b'\nSIDE = open("other.py").read()\n',
    b'\nexec("print(1)")\n'])
def test_unwitnessed_dependencies_and_executable_source_are_not_reused(extra):
    assert not prepared.declarative_source(_CLOSED + extra)


def test_raw_response_identity_is_not_recanonicalized_or_taken_from_another_read():
    from types import SimpleNamespace
    observed = {"Integer": {}}
    first = prepared.NodeListCapture(b'{"Integer":{}}', observed, "http://fixture")
    second = prepared.NodeListCapture(b'{ "Integer": {} }\n', observed, "http://fixture")
    client = SimpleNamespace(node_list_capture=first)
    assert prepared.current_node_capture(client, observed) is first
    assert prepared.current_node_capture(client, dict(observed)) is None
    assert prepared.current_node_capture(SimpleNamespace(), observed) is None
    assert first.digest() != second.digest()


_PROCESS = r'''
import json, runpy, sys
from pathlib import Path
ns = runpy.run_path(sys.argv[1])
p = ns["prepared"]
source, root, marker = map(Path, sys.argv[2:5])
def build():
    with marker.open("a") as stream:
        stream.write("build\n")
    return ns["_workflow"]()
collector = ns["TimingCollector"]()
with ns["timing_scope"](collector):
    use = p.prepare_workflow(identity="subprocess", witness=lambda: ns["_context"](source),
        build=build, store=p.PreparedTemplateStore(root), slots=ns["_slots"](),
        values={"seed": int(sys.argv[5])})
    use.published()
print(json.dumps({"events": ns["_events"](collector),
                  "value": use.workflow.inputs["seed"].value,
                  "key": use.filled.snapshot().key}))
'''


def test_two_processes_share_entry_and_source_change_invalidates(tmp_path):
    source, root, marker = tmp_path / "source.py", tmp_path / "data", tmp_path / "builds"
    source.write_text("fixed source v1")

    def run(value):
        result = subprocess.run([sys.executable, "-c", _PROCESS, __file__, str(source), str(root),
                                 str(marker), str(value)], capture_output=True, text=True,
                                check=True, timeout=60, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
        return json.loads(result.stdout)

    cold, warm = run(53), run(59)
    assert marker.read_text().splitlines() == ["build"]
    assert cold["key"] == warm["key"]
    assert warm["value"] == 59
    assert warm["events"] == [["hit", None]]
    source.write_text("fixed source v2")
    changed = run(61)
    assert marker.read_text().splitlines() == ["build", "build"]
    assert changed["key"] != warm["key"]
    assert changed["events"][:2] == [["miss", "key_changed:source_provenance"],
                                    ["recompute", "key_changed:source_provenance"]]
