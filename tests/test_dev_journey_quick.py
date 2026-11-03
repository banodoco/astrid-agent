from __future__ import annotations

import sys
from pathlib import Path

import pytest

from astrid.core.dev.journey import (
    JourneyFailure,
    Runner,
    _isolate_quick_pack_home,
    _keep_failure_logs,
)
from astrid.core.pack.source_setup import source_data_root, source_state_path


def test_quick_pack_home_keeps_mutable_pack_paths_under_disposable_data(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "journey"
    env = {name: str(tmp_path / "global") for name in (
        "HOME", "ASTRID_HOME", "ASTRID_ENV_FILE", "ASTRID_PACKS_PATH",
        "ASTRID_SOURCE_DECLARATIONS", "ASTRID_SOURCE_DATA", "ASTRID_SOURCE_STATE",
        "ASTRID_USER_PACKS", "ASTRID_HIVEMIND_REPOSITORY", "ASTRID_HIVEMIND_REVISION")}
    _isolate_quick_pack_home(env, root)
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    packhome = root / "data" / "packhome"
    data_root = source_data_root()
    state_path = source_state_path()
    user_root = Path(env["ASTRID_USER_PACKS"])

    assert data_root == packhome / "data" / "astrid" / "packs"
    assert state_path == packhome / "state" / "astrid" / "pack-sources.json"
    assert user_root == packhome / "config" / "astrid" / "packs"
    assert Path(env["HOME"]) == packhome
    assert Path(env["ASTRID_HOME"]) == packhome / ".astrid"
    for key in ("ASTRID_ENV_FILE", "ASTRID_PACKS_PATH", "ASTRID_SOURCE_DECLARATIONS",
                "ASTRID_HIVEMIND_REPOSITORY", "ASTRID_HIVEMIND_REVISION"):
        assert key not in env
    for path in (data_root, state_path, user_root):
        path.relative_to(root / "data")


def test_quick_failing_command_output_is_in_failure_evidence(tmp_path: Path) -> None:
    root = tmp_path / "journey"
    root.mkdir()
    runner = Runner(root, tmp_path / "summary.json", tier="quick")

    with pytest.raises(JourneyFailure, match="exit 3"):
        runner.command("first_open", [sys.executable, "-c", "import sys; print('open-out'); print('open-err', file=sys.stderr); sys.exit(3)"], {})

    evidence = Path(_keep_failure_logs(root, tmp_path / "summary.json"))
    assert (evidence / "first_open.stdout.log").read_text().strip() == "open-out"
    assert (evidence / "first_open.stderr.log").read_text().strip() == "open-err"


def test_a_failed_journey_exits_nonzero_through_the_dev_command(tmp_path: Path, monkeypatch) -> None:
    """The process status is the gate: a summary that says ok=false must never come with exit 0."""
    import json

    from astrid.core.dev import journey
    from astrid.core.gateway import dev_operator

    summary = tmp_path / "quick.json"

    def failing_run(*_args, **_kwargs) -> int:
        summary.write_text(json.dumps({"ok": False, "outcome": "failure", "error": "quick_browser: exit 1"}))
        return 1

    monkeypatch.setattr(journey, "run", failing_run)
    argv = ["journey", "--tier", "quick", "--reference", "generated", "--summary", str(summary)]
    assert dev_operator.main(argv) == 1
    assert json.loads(summary.read_text())["ok"] is False


# ---- the saved-revision render proof reads where the Runtime records the pin ------------------

REVISION = "astrid.editor.timeline.b1b9e54b"


def _shown(revision: str = REVISION, *, wrapped: bool = True) -> dict:
    inputs = {
        "timeline_authority": {"parent_revision_id": revision, "project_id": "kernel-p", "timeline_id": "main"},
        "timeline_snapshot_ref": {"revision_id": revision, "project_id": "kernel-p", "timeline_id": "main"},
    }
    spec = {"spec": {"inputs": inputs}} if wrapped else {"inputs": inputs}
    return {"data": {"spec": spec}}


def _excerpt(tmp_path: Path, monkeypatch, shown: dict) -> None:
    from astrid.core.dev import journey as module

    output = tmp_path / "out.mp4"
    output.write_bytes(b"x")
    monkeypatch.setattr(module, "_find_output", lambda *_a: output)
    monkeypatch.setattr(module, "_duration", lambda *_a: 1.0)
    runner = Runner(tmp_path, tmp_path / "summary.json", "quick")
    monkeypatch.setattr(runner, "command", lambda *_a, **_k: shown)
    journey = module.Journey(runner=runner, root=tmp_path, env={}, project="journey", timeline="main",
                             start=0, end=1, kill_start=0, checkout=tmp_path)
    monkeypatch.setattr(journey, "render", lambda *_a, **_k: ({"data": {"kernel_task_id": "t1", "state": "completed"}}, "o.mp4"))
    journey.excerpt("quick_render", "saved", [], revision_id=REVISION, project_id="kernel-p")


def test_the_render_proof_reads_the_admitted_spec_the_runtime_records(tmp_path: Path, monkeypatch) -> None:
    _excerpt(tmp_path, monkeypatch, _shown())


def test_the_render_proof_rejects_a_task_pinned_to_another_revision(tmp_path: Path, monkeypatch) -> None:
    with pytest.raises(JourneyFailure, match="did not pin the saved revision"):
        _excerpt(tmp_path, monkeypatch, _shown("astrid.editor.timeline.other"))


def test_the_render_proof_rejects_a_task_with_no_pin(tmp_path: Path, monkeypatch) -> None:
    with pytest.raises(JourneyFailure, match="did not pin the saved revision"):
        _excerpt(tmp_path, monkeypatch, {"data": {"spec": {"spec": {"inputs": {}}}}})
