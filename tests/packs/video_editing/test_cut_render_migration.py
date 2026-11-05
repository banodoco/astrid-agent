"""Cut remains a result-only task worker after timeline-authority cutover."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from astrid.core import timeline
from astrid.core.contracts.errors import AstridError
from astrid.packs.training.actions.pool_merge import run as pool_merge
from astrid.packs.video_editing.actions.cut import resume as cut_resume
from astrid.packs.video_editing.actions.cut import run as cut_run


def test_cut_cli_emits_only_attempt_outputs(tmp_path: Path) -> None:
    inputs = _write_visual_only_inputs(tmp_path)
    theme = _write_theme(tmp_path)
    out = tmp_path / "out"

    assert cut_run.main(
        [
            "--pool", str(inputs["pool"]),
            "--arrangement", str(inputs["arrangement"]),
            "--brief", str(inputs["brief"]),
            "--theme", str(theme),
            "--out", str(out),
        ]
    ) == 0

    assert (out / "hype.timeline.json").is_file()
    assert (out / "hype.assets.json").is_file()
    emitted_timeline = json.loads((out / "hype.timeline.json").read_text(encoding="utf-8"))
    assert emitted_timeline["theme"] == "materialized-fixture"
    assert not list(tmp_path.rglob("assembly.json"))
    assert not list(tmp_path.rglob("events.jsonl"))


def test_cut_rejects_generative_arrangement_without_explicit_theme(tmp_path: Path) -> None:
    inputs = _write_visual_only_inputs(tmp_path)

    with pytest.raises(AstridError, match="explicit absolute runtime-materialized theme.json"):
        cut_run.main(
            [
                "--pool", str(inputs["pool"]),
                "--arrangement", str(inputs["arrangement"]),
                "--brief", str(inputs["brief"]),
                "--out", str(tmp_path / "out"),
            ]
        )


@pytest.mark.parametrize("value", ["banodoco-default", "themes/my-theme", "https://example.test/theme.json"])
def test_cut_theme_resolver_rejects_slug_directory_and_url(value: str) -> None:
    with pytest.raises(AstridError, match="absolute runtime-materialized theme.json"):
        cut_run._resolve_theme_path(value)


def test_cut_theme_resolver_requires_existing_theme_json(tmp_path: Path) -> None:
    with pytest.raises(AstridError, match="theme file not found or invalid"):
        cut_run._resolve_theme_path(str((tmp_path / "missing.json").resolve()))
    directory = tmp_path / "theme-dir"
    directory.mkdir()
    with pytest.raises(AstridError, match="theme file not found or invalid"):
        cut_run._resolve_theme_path(str(directory.resolve()))


def test_cut_theme_id_comes_from_materialized_document_not_directory() -> None:
    assert cut_run._theme_id({"id": "canonical-theme"}) == "canonical-theme"


def test_source_only_cut_timeline_does_not_inject_a_theme() -> None:
    config = cut_run.build_multitrack_timeline(
        {"clips": []},
        {"entries": []},
        {"assets": {}},
        None,
        compiled_plan=[],
    )
    assert "theme" not in config
    assert "banodoco-default" not in config


def test_cut_resume_copies_materialized_artifacts_without_workspace_lookup(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "hype.timeline.json").write_text(
        Path("examples/hype.timeline.json").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (source / "hype.assets.json").write_text(
        Path("examples/hype.assets.json").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    registry = json.loads((source / "hype.assets.json").read_text(encoding="utf-8"))
    for key, entry in registry["assets"].items():
        materialized = source / f"{key}.mp4"
        materialized.write_bytes(b"runtime-materialized fixture")
        entry["file"] = str(materialized)
    (source / "hype.assets.json").write_text(json.dumps(registry), encoding="utf-8")
    out = tmp_path / "out"
    args = cut_run.build_parser().parse_args(
        ["--timeline", str(source / "hype.timeline.json"), "--out", str(out)]
    )

    result = cut_resume.execute_resume_mode(args)

    assert result.timeline_path == out / "hype.timeline.json"
    assert result.assets_path == out / "hype.assets.json"
    assert not list(tmp_path.rglob("assembly.json"))
    assert not list(tmp_path.rglob("events.jsonl"))


def test_cut_sources_have_no_workspace_mutation_gateway() -> None:
    for module_path in (
        Path("astrid/packs/video_editing/actions/cut/run.py"),
        Path("astrid/packs/video_editing/actions/cut/resume.py"),
        Path("astrid/packs/editorial/shared/timeline_build.py"),
    ):
        source = module_path.read_text(encoding="utf-8")
        assert "pack_write_gateway" not in source
        assert "append_event" not in source
        assert "regenerate_projection" not in source
        assert "managed_binding" not in source


def _write_visual_only_inputs(root: Path) -> dict[str, Path]:
    pool = pool_merge.merge_pool(
        {
            "version": timeline.POOL_VERSION,
            "generated_at": "2026-04-21T12:00:00Z",
            "entries": [],
        }
    )
    arrangement = {
        "version": timeline.ARRANGEMENT_VERSION,
        "generated_at": "2026-04-21T12:00:00Z",
        "brief_text": "Make a quote card.",
        "target_duration_sec": 4.0,
        "clips": [
            {
                "uuid": "a3f4b21c",
                "order": 1,
                "audio_source": None,
                "visual_source": {
                    "pool_id": "pool_g_text_card",
                    "role": "primary",
                    "params": {"content": "Hello"},
                },
                "text_overlay": None,
                "rationale": "Use a generated quote card.",
            }
        ],
    }
    pool_path = root / "pool.json"
    arrangement_path = root / "arrangement.json"
    brief_path = root / "brief.txt"
    timeline.save_pool(pool, pool_path)
    timeline.save_arrangement(
        arrangement,
        arrangement_path,
        {entry["id"] for entry in pool["entries"]},
    )
    brief_path.write_text("Make a quote card.\n", encoding="utf-8")
    return {
        "pool": pool_path,
        "arrangement": arrangement_path,
        "brief": brief_path,
    }


def _write_theme(root: Path) -> Path:
    path = root / "materialized" / "theme.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "id": "materialized-fixture",
                "visual": {
                    "color": {"fg": "#fff", "bg": "#000", "accent": "#f00"},
                    "type": {
                        "families": {"heading": "Arial", "body": "Arial"},
                        "size": {"base": 16, "small": 12, "large": 24},
                        "weight": {"normal": 400, "bold": 700},
                        "lineHeight": 1.5,
                    },
                    "motion": {"fadeMs": 300},
                    "canvas": {"width": 1920, "height": 1080, "fps": 30},
                },
            }
        ),
        encoding="utf-8",
    )
    return path.resolve()
