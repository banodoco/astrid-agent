from __future__ import annotations

from pathlib import Path

import pytest

from scripts.reshape import check_repo_hygiene


def _touch(root: Path, relpath: str, text: str = "") -> None:
    path = root / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_find_root_generated_artifacts_flags_root_reports_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _touch(tmp_path, "agentic-20260528.report.md")
    _touch(tmp_path, "report-demo.md")
    _touch(tmp_path, "nested/report-demo.md")
    monkeypatch.setattr(check_repo_hygiene, "REPO_ROOT", tmp_path)

    assert check_repo_hygiene.find_root_generated_artifacts() == [
        "agentic-20260528.report.md",
        "report-demo.md",
    ]


def test_unknown_root_entries_flag_unapproved_root_files_and_dirs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(check_repo_hygiene, "REPO_ROOT", tmp_path)
    _touch(tmp_path, "README.md", "# ok\n")
    _touch(tmp_path, "idea.md", "scratch\n")
    _touch(tmp_path, "out/report.md", "generated\n")

    findings = check_repo_hygiene.find_unknown_root_entries(
        ["README.md", "idea.md", "out/report.md"]
    )

    assert findings == ["idea.md", "out/"]


def test_intentional_tracked_root_directories_are_allowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tracked_paths = [
        "examples/p7/composition.json",
        "guides/operator-notes.md",
    ]
    for relpath in tracked_paths:
        _touch(tmp_path, relpath)

    monkeypatch.setattr(check_repo_hygiene, "REPO_ROOT", tmp_path)

    assert check_repo_hygiene.find_unknown_root_entries(tracked_paths) == []


def test_retired_storyboard_root_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reject old project-specific root folders rather than allowlisting them."""
    tracked_paths = [
        "storyboards/intro.storyboard.json",
        "scratch.py",
    ]
    for relpath in tracked_paths:
        _touch(tmp_path, relpath)

    monkeypatch.setattr(check_repo_hygiene, "REPO_ROOT", tmp_path)

    assert check_repo_hygiene.find_unknown_root_entries(tracked_paths) == [
        "scratch.py",
        "storyboards/",
    ]


def test_find_tracked_ignored_artifacts_classifies_synthetic_filenames(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tracked_paths = [
        ".env.local",
        "config/dev.key",
        "runs/demo/output.json",
        "out/session-report.md",
        "cache/render-state.json",
        "astrid/packs/demo/build/compiled.json",
        "exports/preview.mp4",
        "exports/voice.mp3",
        ".desloppify/state.json",
        "notes/debug.bak",
    ]
    for relpath in tracked_paths:
        _touch(tmp_path, relpath)

    monkeypatch.setattr(check_repo_hygiene, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(check_repo_hygiene, "_tracked_files", lambda: tracked_paths)

    assert check_repo_hygiene.find_tracked_ignored_artifacts() == [
        ("local tool state", ".desloppify/state.json"),
        ("local env filename", ".env.local"),
        ("generated runtime directory", "astrid/packs/demo/build/compiled.json"),
        ("generated runtime directory", "cache/render-state.json"),
        ("credential-like filename", "config/dev.key"),
        ("tracked runtime media output", "exports/preview.mp4"),
        ("tracked runtime media output", "exports/voice.mp3"),
        ("local tool state", "notes/debug.bak"),
        ("generated runtime directory", "out/session-report.md"),
        ("generated runtime directory", "runs/demo/output.json"),
    ]


def test_find_tracked_ignored_artifacts_covers_local_state_and_outputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tracked_paths = [
        ".megaplan-agentic/brief.md",
        ".astrid/config.json",
        "mgt-abc/project.json",
        "out/report.md",
        "examples/demo.preview.full.tmp_timing.json",
        "docs/megaplan/epics/demo/chain.yaml",
    ]
    for relpath in tracked_paths:
        _touch(tmp_path, relpath)

    monkeypatch.setattr(check_repo_hygiene, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(check_repo_hygiene, "_tracked_files", lambda: tracked_paths)

    findings = check_repo_hygiene.find_tracked_ignored_artifacts()
    flagged_paths = sorted({path for _category, path in findings})

    # docs/megaplan/** is the source-of-truth directory and must NOT be flagged.
    assert flagged_paths == [
        ".astrid/config.json",
        ".megaplan-agentic/brief.md",
        "examples/demo.preview.full.tmp_timing.json",
        "mgt-abc/project.json",
        "out/report.md",
    ]
    assert ("megaplan local state", ".megaplan-agentic/brief.md") in findings
    assert ("generated runtime directory", ".astrid/config.json") in findings
    assert ("generated project worktree", "mgt-abc/project.json") in findings
    assert ("preview/temp artifact", "examples/demo.preview.full.tmp_timing.json") in findings


def test_allowlists_preserve_legitimate_tracked_fixtures_and_source_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tracked_paths = [
        ".env.example",
        "astrid/core/util/secrets.py",
        "docs/assets/astrid-orchestration.png",
        "tests/fixtures/reshape/hype_regression/main.mp4",
        "tests/packs/builtin/generate_image/fixtures/tiny.png",
    ]
    for relpath in tracked_paths:
        _touch(tmp_path, relpath)

    monkeypatch.setattr(check_repo_hygiene, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(check_repo_hygiene, "_tracked_files", lambda: tracked_paths)

    assert check_repo_hygiene.find_tracked_ignored_artifacts() == []


def test_retired_m7_megaplan_assets_and_runtime_state_are_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    retired_m7 = [
        ".megaplan/plans/m7-dogfood-and-hardening-20260820-0835/plan_v1.meta.json",
        ".megaplan/plans/m7-dogfood-and-hardening-20260820-0835/plan_v2.md",
        ".megaplan/plans/m7-dogfood-and-hardening-20260820-0835/plan_v2.meta.json",
        ".megaplan/plans/m7-dogfood-and-hardening-20260820-0835/state.json",
    ]
    runtime = [
        ".megaplan/initiatives/astrid-first/.process_adapter_wbc/events.ndjson",
        ".megaplan/plans/unapproved-runtime/state.json",
    ]
    tracked_paths = retired_m7 + runtime
    for relpath in tracked_paths:
        _touch(tmp_path, relpath)

    monkeypatch.setattr(check_repo_hygiene, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(check_repo_hygiene, "_tracked_files", lambda: tracked_paths)

    findings = check_repo_hygiene.find_tracked_ignored_artifacts()
    assert sorted({path for _category, path in findings}) == sorted(tracked_paths)
    for path in tracked_paths:
        assert ("megaplan local state", path) in findings


def test_secret_named_test_sources_are_allowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tracked_paths = [
        "tests/sdk/test_zero_secret_smoke.py",
        "tests/v10/test_m6_secret_sink.py",
    ]
    for relpath in tracked_paths:
        _touch(tmp_path, relpath)

    monkeypatch.setattr(check_repo_hygiene, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(check_repo_hygiene, "_tracked_files", lambda: tracked_paths)

    assert check_repo_hygiene.find_tracked_ignored_artifacts() == []


def test_main_reports_category_and_path_names_without_file_contents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    tracked_path = ".env.local"
    _touch(tmp_path, tracked_path, text="not-for-output\n")
    monkeypatch.setattr(check_repo_hygiene, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(check_repo_hygiene, "_tracked_files", lambda: [tracked_path])

    rc = check_repo_hygiene.main()

    assert rc == 1
    stderr = capsys.readouterr().err
    assert "[local env filename] .env.local" in stderr
    assert "not-for-output" not in stderr


def test_live_tree_passes_repo_hygiene() -> None:
    """The committed tree is clean under every hygiene rule (home of the CI hygiene lane)."""
    tracked = check_repo_hygiene._tracked_files()

    assert check_repo_hygiene.find_unknown_root_entries(tracked) == []
    assert check_repo_hygiene.find_root_generated_artifacts() == []
    assert check_repo_hygiene.find_tracked_ignored_artifacts(tracked) == []
    assert check_repo_hygiene.main() == 0


def test_pack_assets_and_credentials_docs_are_allowed_but_other_media_and_keys_are_not(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    allowed = [
        "astrid/packs/local/rendering/elements/effects/demo/assets/card-0.png",
        "astrid/packs/h3_av/workflows/demo/mask_full_frame.png",
        "docs/setup/credentials.md",
    ]
    flagged = [
        "astrid/packs/local/rendering/elements/effects/demo/render.mp4",
        "astrid/packs/h3_av/workflows/demo/output.png",
        "config/dev.key",
        "scripts/credentials.json",
        "runs/demo/output.json",
        ".venv/bin/python",
        ".env",
    ]
    tracked_paths = allowed + flagged
    for relpath in tracked_paths:
        _touch(tmp_path, relpath)
    monkeypatch.setattr(check_repo_hygiene, "REPO_ROOT", tmp_path)

    flagged_paths = {path for _category, path in check_repo_hygiene.find_tracked_ignored_artifacts(tracked_paths)}

    assert flagged_paths == set(flagged)
