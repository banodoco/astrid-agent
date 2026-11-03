"""Unit-level fast-lane timing and snapshot provenance checks.

VW-LABEL: metadata-only/legacy inputs remain usable as UNCERTIFIED previews.
The host-minted positive path is covered in test_live_source_label_native.py.
"""

from __future__ import annotations

import os
import time

from astrid.core import gateway
from astrid.packs.rendering.actions.timeline_visualize.inspection_contract import render_status
from astrid.packs.rendering.actions.timeline_visualize.provenance import snapshot_label


def _snapshot(parent: str | None, *, selection: str = "explicit_historical_render", **metadata):
    canonical = {}
    if parent is not None:
        canonical["parent_revision_id"] = parent
    return {
        "metadata": {
            "selection": selection,
            "canonical_timeline": canonical,
            **metadata,
        }
    }


def _candidate_marker(*, base: str | None = "R1", candidate: str = "C1", label: str | None = "Unpublished candidate preview"):
    marker = {
        "candidate_digest": "sha256:" + "a" * 64,
        "publication_digest": "sha256:" + "b" * 64,
        "candidate_parent_revision_id": candidate,
    }
    if label is not None:
        marker["label"] = label
    if base is not None:
        marker["base_parent"] = {"revision_id": base, "content_digest": "sha256:" + "c" * 64}
    return marker


def test_gateway_main_stamps_the_cli_start_for_the_check_timing(monkeypatch):
    monkeypatch.delenv("ASTRID_CLI_START_MONO", raising=False)
    before = time.monotonic()
    assert gateway.main(["--version"]) == 0
    stamp = float(os.environ.pop("ASTRID_CLI_START_MONO"))
    assert stamp <= time.monotonic()
    # The first call stamps the gateway import; it is never later than this test's start by a wide margin.
    assert stamp <= before + 1.0


def test_historical_snapshot_label_uses_the_revision_actually_rendered():
    label = snapshot_label(_snapshot("R1", requested_revision_id="R2", current_head_revision_id="R2"))

    assert label == {
        "status": "uncertified",
        "label": "UNCERTIFIED preview (claimed revision R1)",
        "actual_revision_id": None,
        "base_revision_id": None,
    }


def test_input_only_parent_closure_label_uses_immutable_parent_not_current_head():
    label = snapshot_label(
        {
            "metadata": {
                "selection": "input_only",
                "authority": "canonical_parent_revision_closure",
                "parent_revision_id": "R1",
                "timeline_identity": {"head_event_id": "R2", "config_version": 7},
                "requested_revision_id": "R2",
            }
        }
    )

    assert label == {
        "status": "uncertified",
        "label": "UNCERTIFIED preview (claimed revision R1)",
        "actual_revision_id": None,
        "base_revision_id": None,
    }


def test_input_only_candidate_uses_only_working_copy_base_parent():
    marker = _candidate_marker(base="R1", candidate="R2", label=None)
    label = snapshot_label(
        {
            "metadata": {
                "selection": "input_only",
                "authority": "canonical_parent_revision_closure",
                "parent_revision_id": "R1",
                "working_copy": marker,
            }
        }
    )

    assert label["status"] == "uncertified"
    assert label["base_revision_id"] == "R1"
    assert label["actual_revision_id"] is None
    assert "R2" in label["label"]
    assert "committed" not in label["label"]


def test_managed_candidate_label_uses_marker_base_not_candidate_parent():
    label = snapshot_label(
        {
            "metadata": {
                "selection": "explicit_candidate_preview",
                "canonical_timeline": {
                    "head_event_id": "R1",
                    "render_mode": "authoring_candidate_preview",
                    "authoring_preview": _candidate_marker(),
                },
            }
        }
    )

    assert label == {
        "status": "uncertified",
        "label": "Unpublished candidate preview (base R1)",
        "actual_revision_id": None,
        "base_revision_id": "R1",
    }


def test_composed_candidate_label_uses_working_copy_base_not_capture_parent():
    marker = _candidate_marker(label=None)
    label = snapshot_label(
        {
            "metadata": {
                "selection": "composed_frame_capture",
                "canonical_timeline": {"parent_revision_id": "C1"},
                "capture_authority": {
                    "parent_revision_id": "C1",
                    "working_copy": marker,
                },
                "working_copy": marker,
            }
        }
    )

    assert label["status"] == "uncertified"
    assert label["base_revision_id"] == "R1"
    assert "C1" in label["label"]
    assert "committed" not in label["label"]


def test_candidate_with_missing_marker_base_stays_unsaved_without_fabricating_candidate_id():
    label = snapshot_label(
        {
            "metadata": {
                "selection": "explicit_candidate_preview",
                "canonical_timeline": {
                    "parent_revision_id": "C1",
                    "authoring_preview": _candidate_marker(base=None),
                },
            }
        }
    )

    assert label == {
        "status": "uncertified",
        "label": "Unpublished candidate preview (base revision unavailable)",
        "actual_revision_id": None,
        "base_revision_id": None,
    }


def test_authoring_preview_only_is_candidate_and_uses_its_marker_base():
    label = snapshot_label(
        {
            "metadata": {
                "canonical_timeline": {"parent_revision_id": "C1"},
                "capture_authority": {"authoring_preview": _candidate_marker()},
            }
        }
    )

    assert label["status"] == "uncertified"
    assert label["base_revision_id"] == "R1"


def test_conflicting_admitted_candidate_bases_are_unavailable():
    label = snapshot_label(
        {
            "metadata": {
                "working_copy": _candidate_marker(base="R1"),
                "canonical_timeline": {"authoring_preview": _candidate_marker(base="R2")},
            }
        }
    )

    assert label == {
        "status": "uncertified",
        "label": "Unpublished candidate preview (base revision unavailable: contradictory markers)",
        "actual_revision_id": None,
        "base_revision_id": None,
    }


def test_requested_revision_without_actual_snapshot_identity_is_unavailable():
    label = snapshot_label(_snapshot(None, requested_revision_id="R2"))

    assert label["status"] == "uncertified"
    assert label["actual_revision_id"] is None
    assert "R2" not in label["label"]


def test_no_cache_has_no_revision_label():
    label = snapshot_label(None)

    assert label == {
        "status": "unavailable",
        "label": "revision unavailable",
        "actual_revision_id": None,
        "base_revision_id": None,
    }


def test_mismatched_requested_revision_cannot_relabel_actual_snapshot():
    snapshot = _snapshot(
        "R1",
        capture_authority={"parent_revision_id": "R1"},
        requested_revision_id="R2",
    )

    assert snapshot_label(snapshot)["label"] == "UNCERTIFIED preview (claimed revision R1)"


def test_renderer_outage_status_says_output_is_unavailable():
    status = render_status(lifecycle="absent", output={"timeline": "cut"}, project="demo")

    assert status["kind"] == "absent"
    assert "No successful render is available" in status["label"]
    assert status["output"]["available"] is False
