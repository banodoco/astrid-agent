"""Focused public timeline-visualization grammar checks for V1-01."""

from __future__ import annotations

import json
import shlex
from pathlib import Path
from types import SimpleNamespace

import pytest

from astrid.packs.timeline.cli import build_parser


# Captured from the untouched base commit cd4f4642b112f2de7e9a4295143f47018e069b43.
# Each row is options, dest, repr(default), type, choices, required.
BASELINE_VISUALIZE_ACTIONS = """\
-h,--help|help|'==SUPPRESS=='|-|-|False
--project|project|None|-|-|False
--timeline-slug|timeline_slug|None|-|-|False
--shot|shot|None|-|-|False
--range|range|None|-|-|False
--at|at|None|-|-|False
--frame|frame|None|builtins.int|-|False
--revision-id|revision_id|None|-|-|False
--clip|clip|None|-|-|False
--occurrence|occurrence|None|-|-|False
--asset|asset|None|-|-|False
--show|show|None|-|-|False
--hide|hide|None|-|-|False
--track|track|None|-|-|False
--detail|detail|None|-|-|False
--context|context|None|builtins.float|-|False
--neighbors|neighbors|None|builtins.int|-|False
--format|formats|None|builtins.function|-|False
--mode|mode|'auto'|-|auto,inputs,composed|False
--view|view|None|-|filmstrip,contact,overview,motion,diff|False
--preset|preset|None|-|overview,scan,motion,beat,frame,cut,compare|False
--before-revision-id|before_revision_id|None|-|-|False
--after-revision-id|after_revision_id|None|-|-|False
--cut|cut|None|-|-|False
--layer|layers|None|-|-|False
--labels|labels|False|-|-|False
--beats|beats|None|builtins.function|-|False
--frame-budget|frame_budget|None|builtins.int|-|False
--window|window_s|None|builtins.float|-|False
--rules|rules|None|builtins.function|-|False
--cuts|cuts|None|-|-|False
--preview|preview|None|-|-|False
--sample|sample|None|-|interval,clips,cuts,shots|False
--every|every|None|builtins.float|-|False
--every-frames|every_frames|None|builtins.int|-|False
--include-cuts|include_cuts|None|-|-|False
--render-run|render_run|None|-|-|False
--columns|columns|None|builtins.int|-|False
--page-size|page_size|None|builtins.int|-|False
--resolution|resolution|None|-|-|False
--include-media|include_media|None|-|-|False
--out|out|None|-|-|False
--published|published|False|-|-|False
--draft|draft|None|-|-|False
--every-cut|all_cuts|False|-|-|False
--compare|compare_with|None|-|published|False
--highlight|highlight|None|-|-|False
--overlay|overlay|None|-|-|False
--size|size|None|-|-|False
--plan|plan|False|-|-|False
--list-layers|list_layers|False|-|-|False
--shot-plans|shot_plans|False|-|-|False
--region|region|None|-|-|False
--zoom|zoom|None|-|-|False
--mask|mask|None|-|-|False
--compare-media|compare_media|None|-|-|False
--against|against|None|-|-|False
--no-rules|no_rules|False|-|-|False
--render-every|render_every|None|builtins.float|-|False
--edited|edited|None|-|-|False
--no-capture|no_capture|False|-|-|False
--from|from_revision|None|-|-|False
--to|to_revision|None|-|-|False
--json|json|False|-|-|False
"""


def _visualize_parser(parser):
    return parser._subparsers._group_actions[0].choices["visualize"]


def _action_type(action):
    value = action.type
    if value is None:
        return "-"
    if isinstance(value, type):
        return f"{value.__module__}.{value.__qualname__}"
    return f"{type(value).__module__}.{type(value).__qualname__}"


def _action_snapshot(parser):
    rows = {}
    for action in _visualize_parser(parser)._actions:
        for option in action.option_strings:
            choices = ",".join(map(str, action.choices)) if action.choices is not None else "-"
            rows[option] = (action.dest, repr(action.default), _action_type(action), choices, action.required)
    return rows


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


def test_visualize_argument_contract_matches_untouched_snapshot(parser):
    expected = {}
    for line in BASELINE_VISUALIZE_ACTIONS.splitlines():
        options, dest, default, value_type, choices, required = line.split("|")
        for option in options.split(","):
            expected[option] = (dest, default, value_type, choices, required == "True")
    actual = _action_snapshot(parser)
    assert set(actual) == set(expected)
    assert actual == expected


VISUALIZE_EXAMPLES = (
    "One clean composed frame",
    "Review a range or shot",
    "Inspect inputs without rendering",
    "Compare before and after",
)


def test_visualize_worked_examples_parse(parser):
    help_text = _visualize_parser(parser).format_help()
    lines = help_text.splitlines()
    for heading in VISUALIZE_EXAMPLES:
        index = next(i for i, line in enumerate(lines) if line.strip() == f"{heading}:")
        command = shlex.split(lines[index + 1].strip())
        assert command[:3] == ["astrid", "timelines", "visualize"]
        parsed = parser.parse_args(command[2:])
        assert parsed.handler.__name__ == "_cmd_visualize"


def test_visualize_help_groups_flags_and_names_examples(parser):
    help_text = _visualize_parser(parser).format_help()
    for title in ("Selection", "Mode and view", "Layers and overlays", "Sampling", "Comparison", "Output"):
        assert f"{title}:" in help_text
    for heading in VISUALIZE_EXAMPLES:
        assert f"{heading}:" in help_text
    assert "Hide panels, not page chrome" in help_text
    assert "--preset frame --at TIME --show output" in help_text


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
