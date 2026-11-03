from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

import pytest

os.environ.setdefault("ASTRID_INTERNAL_INVOCATION", "1")

from astrid.packs.discord_local.actions.command.run import (
    _parser,
    _sanitize_text,
    _validate_args,
    execute,
    parse_command,
)
from astrid.packs.iteration.api import prepare_experiment


CHANNEL = "https://discord.com/channels/1501633423859650610/1530264299581345822"
CHANNEL_V2 = "https://discord.com/channels/1501633423859650610/1527793344431001692"


def _fake_runner(path: Path, *, outcome: str) -> Path:
    body = f"""\
import argparse
import json
import sys
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--output-dir", required=True)
parser.add_argument("--fetch-only", action="store_true")
args, _ = parser.parse_known_args()
run_dir = Path(args.output_dir) / "2026-07-28T00-00-00-000Z"
run_dir.mkdir(parents=True, exist_ok=True)
outcome = {outcome!r}
if outcome == "success":
    media = run_dir / "result.mp4"
    media.write_bytes(b"generated-video")
    payload = {{
        "fetchedAt": "2026-07-28T00:00:00Z",
        "responseMessageId": "1531448135707263151",
        "responsePreview": "completed seed 35635346",
        "match": "35635346",
        "linkMatch": ".mp4",
        "downloads": [{{
            "path": str(media.resolve()),
            "sourceUrl": "https://cdn.discordapp.com/attachments/secret/result.mp4?token=secret",
            "contentType": "video/mp4",
        }}],
    }}
    (run_dir / "result.json").write_text(json.dumps(payload), encoding="utf-8")
elif outcome == "preview":
    (run_dir / "before-submit.png").write_bytes(b"png")
elif outcome == "timeout":
    (run_dir / "fetch-timeout.png").write_bytes(b"png")
    print("No matching completed attachment appeared within 30s", file=sys.stderr)
    raise SystemExit(1)
"""
    path.write_text(body, encoding="utf-8")
    return path


def _command_file(root: Path, count: int = 4) -> tuple[Path, list[Path]]:
    sources = []
    options = []
    for index in range(1, count + 1):
        source = root / f"reference-{index}.png"
        source.write_bytes(f"image-{index}".encode())
        sources.append(source)
        key = "input_media" if index == 1 else f"input_media_{index}"
        options.append(f"{key}:@{source}")
    command = (
        "/gen prompt:One uninterrupted desert plant growth shot. "
        + " ".join(options)
        + " resolution:720p duration:20s aspect_ratio:16:9 seed:35635346"
    )
    command_path = root / "command.txt"
    command_path.write_text(command, encoding="utf-8")
    return command_path, sources


def _args(
    tmp_path: Path,
    *,
    mode: str,
    runner: Path,
    command_file: Path | None = None,
):
    argv = [
        "--out",
        str(tmp_path / "out"),
        "--mode",
        mode,
        "--channel-url",
        CHANNEL,
        "--runner-script",
        str(runner),
        "--node-bin",
        sys.executable,
        "--timeout-seconds",
        "30",
    ]
    if command_file:
        argv.extend(["--command-file", str(command_file)])
    if mode == "fetch":
        argv.extend(
            [
                "--after",
                "2026-07-27T23:35:00Z",
                "--match",
                "35635346",
                "--link-match",
                ".mp4",
                "--response-message-id",
                "1531448135707263151",
            ]
        )
    parser = _parser()
    args = parser.parse_args(argv)
    _validate_args(args, parser)
    return args


def test_parse_command_preserves_prompt_and_ten_attachment_slots(tmp_path: Path):
    command_file, _ = _command_file(tmp_path, count=10)
    parsed = parse_command(command_file.read_text(encoding="utf-8"))
    assert parsed.prompt == "One uninterrupted desert plant growth shot."
    assert [key for key, _ in parsed.options if key.startswith("input_media")] == [
        "input_media",
        "input_media_2",
        "input_media_3",
        "input_media_4",
        "input_media_5",
        "input_media_6",
        "input_media_7",
        "input_media_8",
        "input_media_9",
        "input_media_10",
    ]


def test_debug_text_redacts_explicit_runner_and_user_profile_paths(tmp_path: Path):
    runner = tmp_path / "external" / "discord-command-poc.mjs"
    profile = tmp_path / "user-state" / "chrome-profile"
    rendered = _sanitize_text(
        f"runner={runner} profile={profile}",
        out=tmp_path / "out",
        redact_paths=(runner, profile),
    )

    assert str(runner) not in rendered
    assert str(profile) not in rendered
    assert rendered.count("$LOCAL_PATH") == 2


def test_model_variants_switch_channels_without_explicit_url(tmp_path: Path):
    runner = _fake_runner(tmp_path / "fake_runner.py", outcome="preview")
    command_file, _ = _command_file(tmp_path)
    parser = _parser()
    for variant, expected in (("v1", CHANNEL), ("v2", CHANNEL_V2)):
        args = parser.parse_args(
            [
                "--out",
                str(tmp_path / variant),
                "--mode",
                "preview",
                "--variant",
                variant,
                "--command-file",
                str(command_file),
                "--runner-script",
                str(runner),
            ]
        )
        _validate_args(args, parser)
        assert args.variant == variant
        assert args.channel_url == expected


def test_fetch_writes_experiment_ready_manifest_with_four_ordered_inputs(tmp_path: Path):
    command_file, sources = _command_file(tmp_path)
    runner = _fake_runner(tmp_path / "fake_runner.py", outcome="success")
    args = _args(
        tmp_path,
        mode="fetch",
        runner=runner,
        command_file=command_file,
    )

    returncode, manifest = execute(args)

    assert returncode == 0
    assert manifest["status"] == "completed"
    assert manifest["kind"] == "discord_browser.generate"
    assert manifest["inputs"]["prompt"] == (
        "One uninterrupted desert plant growth shot."
    )
    assert manifest["inputs"]["prompt_capture"] == "exact"
    assert manifest["inputs"]["seed"] == 35635346
    ordered = manifest["inputs"]["ordered_artifacts"]
    assert [item["ordinal"] for item in ordered] == [1, 2, 3, 4]
    assert [item["role"] for item in ordered] == ["appearance_reference"] * 4
    assert all(item["path"].startswith("inputs/") for item in ordered)
    assert all(item["content_hash"].startswith("sha256:") for item in ordered)
    assert manifest["outputs"][0]["path"] == "outputs/result.mp4"
    assert manifest["outputs"][0]["content_hash"].startswith("sha256:")
    assert (
        manifest["provider_extension"]["watcher"]["target_message_id"]
        == "1531448135707263151"
    )

    out = args.out
    durable_text = "\n".join(
        path.read_text(encoding="utf-8", errors="replace")
        for path in out.rglob("*")
        if path.is_file() and path.suffix in {".json", ".txt", ".log"}
    )
    assert "cdn.discordapp.com" not in durable_text
    assert "token=secret" not in durable_text
    assert all(str(source.resolve()) not in durable_text for source in sources)
    assert "@inputs/01-reference-1.png" in (out / "command.txt").read_text()

    run_id = "00123456789ABCDEFGHJKMNPQR"
    runs_dir = tmp_path / "runs"
    shutil.copytree(out, runs_dir / run_id)
    experiment = {
        "schema_version": 1,
        "experiment_id": "discord-local-roundtrip",
        "project_slug": "discord-local-test",
        "title": "Discord local round-trip",
        "question": "Does the adapter preserve experiment provenance?",
        "hypotheses": [],
        "factors": [{"id": "method", "values": ["discord"]}],
        "rubric": [
            {
                "id": "continuity",
                "label": "Continuity",
                "scale": {"min": 1, "max": 5},
            }
        ],
        "cases": [
            {
                "case_id": "seed-35635346",
                "label": "Seed 35635346",
                "run_id": run_id,
                "factors": {"method": "discord"},
                "relationship": {"type": "baseline", "case_id": None},
                "expected_input_roles": ["appearance_reference"],
            }
        ],
        "created": "2026-07-28T00:00:00Z",
    }
    experiment_path = tmp_path / "experiment.json"
    experiment_path.write_text(json.dumps(experiment), encoding="utf-8")
    review_dir = tmp_path / "review"
    assert prepare_experiment(
        [
            "--experiment",
            str(experiment_path),
            "--runs-dir",
            str(runs_dir),
            "--out",
            str(review_dir),
        ]
    ) == 0
    review = json.loads((review_dir / "review.json").read_text())
    case = review["cases"][0]
    assert case["provider"] == "discord_browser"
    assert case["status"] == "completed"
    assert case["prompt"] == "One uninterrupted desert plant growth shot."
    assert len(case["inputs"]) == 4
    assert [item["ordinal"] for item in case["inputs"]] == [1, 2, 3, 4]
    assert all(item["verified"] for item in case["inputs"])
    assert len(case["outputs"]) == 1
    assert case["outputs"][0]["verified"] is True


def test_preview_is_a_terminal_draft_with_manifest(tmp_path: Path):
    command_file, _ = _command_file(tmp_path)
    runner = _fake_runner(tmp_path / "fake_runner.py", outcome="preview")
    args = _args(
        tmp_path,
        mode="preview",
        runner=runner,
        command_file=command_file,
    )

    returncode, manifest = execute(args)

    assert returncode == 0
    assert manifest["status"] == "draft"
    assert manifest["outputs"] == []
    assert (args.out / "manifest.json").is_file()
    assert list((args.out / "debug").glob("*/before-submit.png"))


def test_fetch_timeout_still_writes_terminal_manifest(tmp_path: Path):
    runner = _fake_runner(tmp_path / "fake_runner.py", outcome="timeout")
    args = _args(tmp_path, mode="fetch", runner=runner)

    returncode, manifest = execute(args)

    assert returncode == 0
    assert manifest["status"] == "timed_out"
    assert manifest["outputs"] == []
    assert "No matching completed attachment" in manifest["error"]
    persisted = json.loads((args.out / "manifest.json").read_text())
    assert persisted["status"] == "timed_out"
    assert list((args.out / "debug").glob("*/fetch-timeout.png"))


def test_fetch_requires_after_boundary(tmp_path: Path):
    runner = _fake_runner(tmp_path / "fake_runner.py", outcome="success")
    parser = _parser()
    args = parser.parse_args(
        [
            "--out",
            str(tmp_path / "out"),
            "--mode",
            "fetch",
            "--channel-url",
            CHANNEL,
            "--runner-script",
            str(runner),
        ]
    )
    with pytest.raises(SystemExit):
        _validate_args(args, parser)
