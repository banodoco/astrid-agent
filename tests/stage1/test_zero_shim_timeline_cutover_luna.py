"""Adversarial proof that live timeline consumers are runtime-only.

These tests intentionally exercise the negative boundary as well as the happy
path: old project-tree selectors must fail closed, and a runtime materialized
timeline must remain entirely in memory until the renderer writes its own
attempt outputs.
"""

from __future__ import annotations

import importlib
import subprocess
import sys
from pathlib import Path

import pytest

import astrid.packs
from astrid.core.rendering.service import RenderService
from astrid.core.timeline.snapshot import snapshot_from_runtime
from astrid.packs.rendering.actions.timeline_visualize import select
from astrid.packs.rendering.actions.timeline_visualize.run import (
    _materialize_kernel_timeline,
)
from astrid.packs.rendering.actions.timeline_visualize.select import KernelTimeline
from astrid.sdk import invocation
from astrid.sdk.exceptions import CapabilityValidationError

ROOT = Path(__file__).resolve().parents[2]


def test_live_import_graph_does_not_load_project_timeline_authority() -> None:
    probe = """
import sys
import astrid.sdk.invocation
import astrid.packs.rendering.actions.timeline_visualize.select
import astrid.core.timeline.snapshot
for name in (
    'astrid.core.timeline.crud',
    'astrid.core.timeline.paths',
    'astrid.core.timeline.repair',
    'astrid.core.timeline._edit_helpers',
    'astrid.core.timeline.branch',
    'astrid.core.timeline.erasure',
    'astrid.core.timeline.operations',
    'astrid.core.timeline.undo',
):
    assert name not in sys.modules, name
"""
    result = subprocess.run(
        [sys.executable, "-c", probe], cwd=ROOT, capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
    for relative in (
        "astrid/core/timeline/crud.py",
        "astrid/core/timeline/paths.py",
        "astrid/core/timeline/repair.py",
        "astrid/core/timeline/_edit_helpers.py",
        "astrid/core/timeline/audio_edits.py",
        "astrid/core/timeline/branch.py",
        "astrid/core/timeline/clip_edits.py",
        "astrid/core/timeline/effect_edits.py",
        "astrid/core/timeline/erasure.py",
        "astrid/core/timeline/observability.py",
        "astrid/core/timeline/operations.py",
        "astrid/core/timeline/theme_edits.py",
        "astrid/core/timeline/track_edits.py",
        "astrid/core/timeline/transition_edits.py",
        "astrid/core/timeline/undo.py",
    ):
        assert not (ROOT / relative).exists(), relative


def test_filesystem_modes_are_rejected_without_touching_the_tree(tmp_path: Path) -> None:
    with pytest.raises(CapabilityValidationError, match="timeline_source"):
        invocation._validate_timeline_visualize_inputs(
            {"timeline_source": str(tmp_path / "assembly.jsonl")},
            project="demo",
            project_root=tmp_path,
        )
    with pytest.raises(CapabilityValidationError, match="requires a managed Runtime object with a valid SHA-256 digest"):
        invocation._prepare_managed_render_inputs(
            {"timeline": str(tmp_path / "assembly.json")},
            project="demo",
        )
    assert not hasattr(select, "select_timeline")
    assert not hasattr(select, "discover_timelines")
    assert not list(tmp_path.rglob("display.json"))
    assert not list(tmp_path.rglob("assembly.jsonl"))


def test_runtime_materialization_projects_in_memory_and_never_repairs(tmp_path: Path) -> None:
    row = KernelTimeline(
        timeline_id="11111111-1111-4111-8111-111111111111",
        timeline_ulid="01ARZ3NDEKTSV4RRFFQ69G5FAV",
        slug="main",
        name="Main",
        is_default=True,
        config={"tracks": [], "clips": []},
        registry={"assets": {}},
        config_version=3,
        head_event_id="01ARZ3NDEKTSV4RRFFQ69G5FAW",
        head_hash="a" * 64,
        head_created_at="2026-01-01T00:00:00+00:00",
    )
    destination = tmp_path / "would-be-timeline"
    selected = _materialize_kernel_timeline(
        row,
        project_root=tmp_path,
        project_slug="demo",
        destination=destination,
    )
    assert selected.timeline_dir is None
    assert selected.runtime_config == row.config
    assert selected.runtime_events
    assert not destination.exists()
    snapshot = snapshot_from_runtime(
        timeline_id=selected.timeline_id,
        timeline_ulid=selected.timeline_ulid,
        slug=selected.slug or "main",
        project_slug="demo",
        events=list(selected.runtime_events),
    )
    assert snapshot.head_version == 2
    assert snapshot.assembly == {"tracks": [], "clips": []}
    assert not list(tmp_path.rglob("display.json"))
    assert not list(tmp_path.rglob("assembly.jsonl"))


def test_replay_capture_has_no_timeline_or_theme_file_authority() -> None:
    assert RenderService._collect_replay_inputs(object(), object(), object()) == {}


def test_supported_visualization_accepts_only_frozen_runtime_identity() -> None:
    identity = {
        "stable_id": "TL01",
        "qualified_ref": "TL01",
        "uuid": "11111111-1111-4111-8111-111111111111",
        "ulid": "01ARZ3NDEKTSV4RRFFQ69G5FAV",
        "slug": "main",
    }
    frozen = select.select_from_manifest(
        {"schema_version": 1, "kind": "timeline_visualize", "timeline": identity}
    )
    assert frozen is not None
    assert frozen.is_frozen_manifest and frozen.timeline_dir is None
    assert select.select_from_manifest(
        {
            "schema_version": 1,
            "kind": "timeline_visualize",
            "timeline": {**identity, "timeline_source": "/tmp/assembly.jsonl"},
        }
    ) is None


def test_deleted_timeline_eventlog_package_is_not_importable() -> None:
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("astrid.core.timeline.eventlog")


def test_deleted_model_setup_journal_has_no_workspace_authority() -> None:
    assert not (ROOT / "astrid/core/model_setup/journal.py").exists()
    source = (ROOT / "astrid/core/model_setup/acquire.py").read_text(
        encoding="utf-8"
    )
    assert "from .journal" not in source
    assert "projects_root" not in source


def test_runaway_schema_host_and_migration_are_absent() -> None:
    runaway = ROOT / "astrid/packs/runaway"
    assert not (runaway / "schema-pack.yaml").exists()
    assert not (runaway / "migrations").exists()
    assert not (runaway / "__init__.py").exists()
    assert not hasattr(astrid.packs, "STANDARD_SCHEMA_PACKS")


def test_pack_workers_are_result_only_and_have_no_timeline_write_binding() -> None:
    worker_paths = (
        ROOT / "astrid/packs/editorial/shared/timeline_build.py",
        ROOT / "astrid/packs/iteration/actions/assemble/run.py",
        ROOT / "astrid/packs/editorial/actions/refine/run.py",
    )
    forbidden = (
        "pack_write_gateway",
        "append_event",
        "regenerate_projection",
        "--timeline-slug",
        "--actor-via",
    )
    for path in worker_paths:
        source = path.read_text(encoding="utf-8")
        for marker in forbidden:
            assert marker not in source, f"{path.relative_to(ROOT)}: {marker}"

    projection_source = (ROOT / "astrid/core/timeline/projection.py").read_text(
        encoding="utf-8"
    )
    assert "def regenerate_projection(" not in projection_source
