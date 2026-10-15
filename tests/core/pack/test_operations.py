"""Actual V3 admission to command runner proofs on disposable fixtures."""
from __future__ import annotations

import copy
import hashlib
import json
import shutil
from pathlib import Path

import pytest
import yaml

from astrid.core.pack.canonical import CanonicalPackValidationError, validate_canonical_pack
from astrid.core.pack.loader import load_pack_manifest
from astrid.sdk import timeline_checkout as tc
from astrid.sdk.timeline import commands, journal, store
from astrid.sdk.timeline._common import TimelineEditError
from astrid.sdk.timeline.checkout import Checkout
from astrid.sdk.timeline.commands import CommandError
from astrid.sdk.timeline.tools import (
    UnsupportedOperation,
    invoke_operation,
    plan_operation,
    resolve_operation,
)

REPO = Path(__file__).resolve().parents[3]
SAMPLE = REPO / "examples/packs/beat_tools"
FIXTURE = REPO / "tests/editing_scenarios/fixtures/tiny_beats.json"
INPUTS = {"clips": ["c1.rocket", "c1.label"],
          "beats": ["beat 1 after c1", "beat 2 after c1", "beat 3 after c1"]}
IDENTITY = "timeline.operation/beat_tools.fit_to_beats"


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("BANODOCO_LOCAL_DATA_ROOT", str(tmp_path / "data"))
    monkeypatch.delenv("ASTRID_PACKS_PATH", raising=False)
    root = tmp_path / "packs"
    shutil.copytree(SAMPLE, root / "beat_tools")
    bundle = json.loads(FIXTURE.read_text())
    for seam in (tc, store):
        monkeypatch.setattr(seam, "resolve_ids", lambda *a, **k: ("p", "t", "rev-0"))
        monkeypatch.setattr(seam, "fetch_bundle", lambda *a, **k: copy.deepcopy(bundle))
    tl = Checkout.draft("P", "t")
    kwargs = dict(project_root=tmp_path / "empty-project", extra_pack_roots=(str(root),))
    return tl, resolve_operation(IDENTITY, **kwargs), kwargs


def _files(root):
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def _descriptor(op, change):
    path = op.pack_root / op.descriptor_path
    value = yaml.safe_load(path.read_text())
    change(value)
    path.write_text(yaml.safe_dump(value))


def test_manifest_discovery_schema_commands_preview_atomic_undo_redo(setup):
    tl, op, _kwargs = setup
    pack = load_pack_manifest(op.pack_root / "pack.yaml")
    assert pack.extensions["timeline"]["operations"] == [op.descriptor_path]
    assert op.eligible and op.source_kind == "extra" and op.identity == IDENTITY
    before = tl.document()
    data_root = Path(tl.path).parents[3]
    disk = _files(data_root)
    calls = plan_operation(tl, op, INPUTS)
    assert [c.op for c in calls] == ["clip.on", "clip.until", "clip.on", "clip.until"]
    preview = invoke_operation(tl, op, INPUTS, dry_run=True)
    assert preview.changed and len(preview.calls) == 4
    assert op.identity in preview.label and op.implementation_digest in preview.label
    assert set(preview.digests) == {"before", "after"}
    assert preview.direct_writes and preview.consequences
    assert tl.document() == before and _files(data_root) == disk
    result = invoke_operation(tl, op, INPUTS)
    assert result.changed and len(result.calls) == 4
    assert result.calls[0].resolved == ("occurrence:occ-A/clip:a-rocket",)
    assert result.calls[0].expect and result.calls[0].authorship
    assert tl.clip("c1.rocket").moments["on"] == "beat 1 after c1"
    assert (tl.clip("c1.rocket").start, tl.clip("c1.rocket").end) == (0.5, 1.0)
    assert (tl.clip("c1.label").start, tl.clip("c1.label").end) == (1.0, 1.5)
    assert len(list(store._history_dir(Path(tl.path)).glob("*.json"))) == 1
    after = tl.document()
    assert not invoke_operation(tl, op, INPUTS).changed
    assert len(list(store._history_dir(Path(tl.path)).glob("*.json"))) == 1
    tl.undo()
    assert tl.document() == before
    tl.redo()
    assert tl.document() == after
    records = [row.to_json() for row in journal.read(journal.journal_path("p", "t"))]
    assert any(op.identity in row["label"] and op.implementation_digest in row["label"] for row in records)


def test_returned_commands_use_the_same_runner_and_music_retime_keeps_references(setup):
    tl, op, _kwargs = setup
    reference = Checkout(copy.deepcopy(tl.bundle))
    calls = plan_operation(tl, op, INPUTS)
    commands.run(reference, {"op": "timeline.batch", "select": None,
                             "args": {"commands": [c.to_json() for c in calls]}})
    invoke_operation(tl, op, INPUTS)
    assert reference.document() == tl.document()
    commands.run(tl, {"op": "clip.set-beats", "select": "a-music", "args": {"source": {
        "beats": [0.6, 1.2, 1.8, 2.4, 3.0], "downbeats": [0.6, 3.0],
        "bpm": 100, "time": "cue_seconds"}}})
    assert tl.clip("c1.rocket").start == 0.6
    assert tl.clip("c1.label").end == 1.8
    assert len(tl.cuts) == 2
    assert tl.clip("c1.label").moments["until"] == "beat 3 after c1"


@pytest.mark.parametrize("inputs", [
    {"clips": ["c1.rocket"], "beats": []},
    {"clips": ["c1.rocket", "a-rocket"], "beats": INPUTS["beats"]},
    {"clips": ["c1.rocket", "c2.card"], "beats": INPUTS["beats"]},
    {"clips": ["c1.rocket", "c1.plate"], "beats": INPUTS["beats"]},
    {"clips": ["c1.rocket", "a-music"], "beats": INPUTS["beats"]},
    {"clips": ["c1.rocket", "missing"], "beats": INPUTS["beats"]},
    {"clips": ["c1.rocket"], "beats": ["beat 2 after c1", "beat 1 after c1"]},
    {"clips": ["c1.rocket"], "beats": ["beat 1 after c1", "beat 1 after c1"]},
    {"clips": ["c1.rocket"], "beats": ["beat 1 after c1", "beat 99 after c1"]},
    {"clips": ["c1.rocket"], "beats": ["beat wrong", "beat 2 after c1"]},
    {"clips": [], "beats": ["beat 1 after c1"]},
    {"clips": ["c1.rocket"], "beats": ["c1", "c2"]},
    {"clips": ["c1.rocket"], "beats": ["beat 1 after c1", "beat 2 after c1"], "extra": True},
])
def test_refusals_leave_memory_disk_and_undo_unchanged(setup, inputs):
    tl, op, _kwargs = setup
    before, disk = tl.document(), _files(Path(tl.path).parents[3])
    with pytest.raises(TimelineEditError):
        invoke_operation(tl, op, inputs)
    assert tl.document() == before
    assert _files(Path(tl.path).parents[3]) == disk


def test_empty_selection_no_step(setup):
    tl, op, _kwargs = setup
    before = tl.document()
    assert not invoke_operation(tl, op, {"clips": [], "beats": []}).changed
    assert tl.document() == before
    assert not list(store._history_dir(Path(tl.path)).glob("*.json"))


@pytest.mark.parametrize("host", ["app", "cli", "sheet", "remote", "untrusted-python"])
def test_unsupported_hosts_are_explicit_typed_refusals(setup, host):
    tl, op, _kwargs = setup
    with pytest.raises(UnsupportedOperation, match="unsupported") as error:
        invoke_operation(tl, op, INPUTS, host=host)
    assert error.value.to_json()["code"] == "unsupported"


def test_point_identity_neighbour_and_source_change_without_restart(setup):
    tl, op, kwargs = setup
    neighbour = op.pack_root / "extensions/timeline.view/fit_to_beats.py"
    neighbour.parent.mkdir()
    neighbour.write_text("raise AssertionError('same-name neighbour executed')\n")
    assert resolve_operation(IDENTITY, **kwargs).implementation_digest == op.implementation_digest
    assert len(plan_operation(tl, op, INPUTS)) == 4
    for wrong in ("fit_to_beats", "timeline.view/beat_tools.fit_to_beats"):
        with pytest.raises(UnsupportedOperation, match="unsupported"):
            resolve_operation(wrong, **kwargs)
    implementation = op.pack_root / op.descriptor["implementation"]["path"]
    before_source = hashlib.sha256(implementation.read_bytes()).hexdigest()
    source = implementation.read_text().replace('    clips, beats = inputs["clips"], inputs["beats"]', '    return []\n    clips, beats = inputs["clips"], inputs["beats"]')
    implementation.write_text(source)
    changed = resolve_operation(IDENTITY, **kwargs)
    assert hashlib.sha256(implementation.read_bytes()).hexdigest() != before_source
    assert changed.implementation_digest != op.implementation_digest
    with pytest.raises(CommandError, match="implementation changed"):
        plan_operation(tl, op, INPUTS)
    assert plan_operation(tl, changed, INPUTS) == ()


@pytest.mark.parametrize("change", [
    lambda d: d.update(point="timeline.view"),
    lambda d: d.update(id="neighbour.fit_to_beats"),
    lambda d: d["implementation"].update(path="undeclared.py"),
    lambda d: d["implementation"].update(path="../escape.py"),
    lambda d: d.update(inputs={"type": "not-a-type"}),
    lambda d: d.update(inputs={"$ref": "https://example.invalid/schema.json"}),
])
def test_canonical_admission_rejects_invalid_declarations_before_import(setup, change):
    _tl, op, _kwargs = setup
    _descriptor(op, change)
    with pytest.raises(CanonicalPackValidationError, match="invalid timeline operation"):
        validate_canonical_pack(op.pack_root)


def test_undeclared_descriptor_cannot_export_an_operation(setup):
    _tl, op, _kwargs = setup
    manifest = op.pack_root / "pack.yaml"
    data = yaml.safe_load(manifest.read_text())
    data["resources"] = [r for r in data["resources"] if r["path"] != op.descriptor_path]
    manifest.write_text(yaml.safe_dump(data))
    with pytest.raises(CanonicalPackValidationError, match="declared file resource"):
        validate_canonical_pack(op.pack_root)


def test_environment_discovery_is_inspect_only(setup, monkeypatch, tmp_path):
    tl, op, _kwargs = setup
    monkeypatch.setenv("ASTRID_PACKS_PATH", str(op.pack_root.parent))
    inspected = resolve_operation(IDENTITY, project_root=tmp_path / "empty-project")
    assert not inspected.eligible and inspected.source_kind == "env"
    with pytest.raises(UnsupportedOperation, match="unsupported execution"):
        plan_operation(tl, inspected, INPUTS)


def test_discovery_never_executes_python_and_bad_command_return_is_atomic(setup):
    tl, op, kwargs = setup
    path = op.pack_root / op.descriptor["implementation"]["path"]
    path.write_text("def build(ctx, inputs):\n    return [ctx.command('clip.set', 'c1.rocket', x=120), ctx.command('clip.set', 'c1.label', zzbogus=2)]\n")
    changed = resolve_operation(IDENTITY, **kwargs)
    before, disk = tl.document(), Path(tl.path).read_bytes()
    with pytest.raises(TimelineEditError):
        invoke_operation(tl, changed, INPUTS)
    assert tl.document() == before and Path(tl.path).read_bytes() == disk
    assert not list(store._history_dir(Path(tl.path)).glob("*.json"))
    path.write_text("raise AssertionError('discovery imported code')\n")
    assert resolve_operation(IDENTITY, **kwargs).identity == IDENTITY
