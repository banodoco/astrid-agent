"""Focused public timeline-visualization grammar checks for V1-01."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from astrid.packs.timeline.cli import build_parser


@pytest.fixture()
def parser():
    return build_parser(SimpleNamespace())


@pytest.mark.parametrize(
    "argv",
    [
        [
            "visualize",
            "main",
            "--project",
            "demo",
            "--view",
            "filmstrip",
            "--render-run",
            "latest",
            "--every",
            "0.5",
            "--include-media",
        ],
        [
            "visualize",
            "main",
            "--project",
            "demo",
            "--view",
            "filmstrip",
            "--render-run",
            "run-123",
            "--range",
            "10..20",
            "--every-frames",
            "6",
            "--resolution",
            "320x180",
        ],
    ],
)
def test_documented_visualize_commands_parse(parser, argv):
    parsed = parser.parse_args(argv)
    assert parsed.handler.__name__ == "_cmd_visualize"


def test_visualize_help_exposes_managed_render_provenance(parser):
    help_text = " ".join(
        parser._subparsers._group_actions[0].choices["visualize"].format_help().split()
    )
    assert "--render-run" in help_text
    assert "latest" in help_text
    assert "only view" in help_text
    assert "structure" not in help_text
    # --preset names intents (overview/scan/motion/beat/frame/cut/compare), not the removed structural presets
    assert "--preset {overview,scan,motion,beat,frame,cut,compare}" in help_text
    assert "--resolution" in help_text


def test_visualize_default_view_follows_the_window(parser):
    from astrid.packs.timeline.cli import _resolve_view

    def resolved(*argv):
        parsed = parser.parse_args(["visualize", "main", "--project", "demo", *argv])
        _resolve_view(parsed)
        return parsed.view, parsed.preset

    assert resolved() == ("contact", None)
    assert resolved("--range", "5..10") == ("motion", "scan")
    assert resolved("--at", "7.2") == ("motion", "frame")
    assert resolved("--at", "7.2", "--preset", "motion") == ("motion", "motion")
    assert resolved("--cut", "17") == ("motion", "cut")
    assert resolved("--range", "5..10", "--show", "inputs") == ("filmstrip", None)
    assert resolved("--preset", "overview") == ("contact", "overview")
    help_text = " ".join(
        parser._subparsers._group_actions[0].choices["visualize"].format_help().split()
    )
    assert "Rendered paired filmstrip" in help_text
    assert "synchronized inputs" in help_text


def test_visualize_cut_address_selects_the_middle_frame(parser, monkeypatch):
    from astrid.packs.timeline import cli
    from astrid.sdk.contracts import DomainResult
    from tests.sdk.test_timeline_show_views import bundle

    doc = bundle()
    doc["shots"]["A"]["internal_timeline"]["clips"][0]["app"] = {"cut": "c01", "layer": "picture"}
    timelines = SimpleNamespace(
        open_bundle=lambda project, ref, revision_id=None: DomainResult.success({"bundle": doc})
    )
    monkeypatch.setattr("astrid.sdk.timeline_checkout.find_draft", lambda *args, **kwargs: None)
    parsed = parser.parse_args(["visualize", "t", "--project", "p", "--at", "c01"])
    parsed.client = SimpleNamespace(timelines=timelines)

    note = cli._resolve_visualize_addresses(parsed)
    cli._resolve_view(parsed)

    assert round(float(parsed.at) * 30) == 59
    assert "middle of c01, frame 59" in note
    assert (parsed.view, parsed.preset) == ("motion", "frame")


def test_structural_view_is_not_a_public_cli_route(parser):
    with pytest.raises(SystemExit):
        parser.parse_args(
            ["visualize", "main", "--project", "demo", "--view", "structure"]
        )


@pytest.mark.parametrize("flag", ["--all", "--from-view", "--focus", "--layout", "--filmstrip"])
def test_structural_navigation_flags_are_not_public_cli_routes(parser, flag):
    argv = ["visualize", "main", "--project", "demo", flag]
    if flag in {"--from-view", "--focus", "--layout", "--filmstrip"}:
        argv.append("value")
    with pytest.raises(SystemExit):
        parser.parse_args(argv)


def test_executor_manifest_only_advertises_filmstrip_surface():
    import yaml

    root = Path(__file__).resolve().parents[3]
    pack = yaml.safe_load((root / "astrid/packs/rendering/pack.yaml").read_text(encoding="utf-8"))
    manifest = pack["actions"]["timeline_visualize"]
    input_names = {item["name"] for item in manifest["inputs"]}
    assert not input_names & {"all", "scope", "layout", "filmstrip", "from_view", "focus", "refresh_root"}
    assert manifest["outputs"][0]["path_template"] == "{out}/filmstrip-view"
    assert manifest["outputs"][1]["path_template"] == "{out}/filmstrip-view/manifest.json"
