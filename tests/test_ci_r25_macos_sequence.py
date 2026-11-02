import hashlib
import json
import textwrap
from pathlib import Path

from scripts import ci_r25_macos_sequence as r25


def test_stage_redacts_private_paths_tokens_and_hashes_published_bytes(tmp_path):
    home = tmp_path / "runner"
    home.mkdir()
    run = home / "r25-option-b-runs" / "run.abcdef12"
    slot = run / "results" / r25.SLOTS[0]
    slot.mkdir(parents=True)
    (slot / "runner.exit").write_text("1\n")
    (slot / "runner.stderr.log").write_text(f'{home}/secret /Users/someone/file ghp_abcdefgh Bearer abc123\n')
    (run / "results/SHA256SUMS").write_text("deadbeef  original.txt\n")
    (run / "sequence.exit").write_text("1\n")
    (run / "cleanup.exit").write_text("0\n")
    outside = tmp_path / "outside.log"
    outside.write_text("must not copy")
    (slot / "escape.log").symlink_to(outside)
    supervisor = tmp_path / "supervisor.log"
    supervisor.write_text(f"R25_RUN_ROOT={run}\n")
    output = tmp_path / "artifact"
    status = r25.stage_evidence(run, output, home, supervisor, {"status": "failed"})
    assert status["sequence_exit"] == "1" and status["cleanup_exit"] == "0"
    assert status["route_exit"] == "not-recorded"
    assert status["runner_exits"][r25.SLOTS[0]] == "1"
    assert status["runner_exits"][r25.SLOTS[1]] == "not-recorded"
    text = (output / "results" / r25.SLOTS[0] / "runner.stderr.log").read_text()
    assert str(home) not in text and "/Users/someone" not in text and "ghp_abcdefgh" not in text and "abc123" not in text
    assert not (output / "results" / r25.SLOTS[0] / "escape.log").exists()
    assert (output / "source-SHA256SUMS.txt").read_text() == "deadbeef  original.txt\n"
    for row in (output / "SHA256SUMS").read_text().splitlines():
        digest, relative = row.split("  ", 1)
        assert hashlib.sha256((output / relative).read_bytes()).hexdigest() == digest


def test_early_failure_reports_missing_exits(tmp_path):
    supervisor = tmp_path / "log"
    supervisor.write_text("preflight failed")
    status = r25.stage_evidence(None, tmp_path / "artifact", tmp_path, supervisor, {"status": "failed"})
    assert status["sequence_exit"] == status["route_exit"] == status["cleanup_exit"] == "not-recorded"
    assert all(value == "not-recorded" for value in status["runner_exits"].values())


def test_workflow_embeds_exact_tested_sequence_orchestrator():
    root = Path(__file__).parents[1]
    workflow = (root / ".github/workflows/journey.yml").read_text()
    embedded = workflow.split("          /opt/homebrew/opt/python@3.11/bin/python3.11 - <<'PY'\n", 1)[1].split("\n          PY\n", 1)[0]
    assert textwrap.dedent(embedded).strip() == (root / "scripts/ci_r25_macos_sequence.py").read_text().strip()
    assert r25.PIN == "f0f51d6761f0409dba74e24c051f17b8c021b641"
    assert "actions/upload-artifact@v4" in workflow and "if: always()" in workflow


def test_success_is_rejected_when_any_stage_exit_is_missing(tmp_path):
    log = tmp_path / "log"
    log.write_text("ok")
    status = r25.stage_evidence(None, tmp_path / "artifact", tmp_path, log, {"status": "complete"})
    assert status["status"] == "failed" and "missing or nonzero" in status["reason"]
