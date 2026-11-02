"""Pack workers expose result-only arguments, never workspace write bindings."""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from astrid.packs.editorial.actions.refine.run import build_parser as refine_parser
from astrid.packs.iteration.api import build_assemble_parser as assemble_parser
from astrid.packs.video_editing.actions.cut.run import build_parser as cut_parser
from astrid.packs.video_editing.actions.hype.steps import build_pool_cut_cmd


@pytest.mark.parametrize(
    ("parser_factory", "base"),
    [
        (cut_parser, ["--out", "/tmp/out"]),
        (
            refine_parser,
            [
                "--arrangement", "/tmp/arrangement.json",
                "--pool", "/tmp/pool.json",
                "--timeline", "/tmp/timeline.json",
                "--assets", "/tmp/assets.json",
                "--metadata", "/tmp/metadata.json",
                "--transcript", "/tmp/transcript.json",
                "--out", "/tmp/out",
            ],
        ),
        (assemble_parser, ["--prepare-dir", "/tmp/prepare", "--out", "/tmp/out"]),
    ],
)
@pytest.mark.parametrize("flag", ["--project", "--timeline-slug", "--actor-via"])
def test_worker_parsers_reject_workspace_write_bindings(
    parser_factory,
    base: list[str],
    flag: str,
) -> None:
    with pytest.raises(SystemExit):
        parser_factory().parse_args([*base, flag, "legacy"])


def test_hype_child_command_never_forwards_workspace_identity() -> None:
    args = argparse.Namespace(
        out=Path("/tmp/hype-out"),
        brief_out=Path("/tmp/hype-brief-out"),
        brief_copy=Path("/tmp/hype-brief-copy"),
        python_exec=None,
        video=None,
        audio=None,
        skip=set(),
        asset_pairs=[],
        theme_explicit=False,
        theme=None,
        primary_asset=None,
        project="runtime-project",
        timeline_slug="runtime-timeline",
        actor_via={"id": "legacy"},
    )

    command = build_pool_cut_cmd(args)

    assert "--project" not in command
    assert "--timeline-slug" not in command
    assert "--actor-via" not in command
    assert "--pool" in command
    assert "--arrangement" in command
