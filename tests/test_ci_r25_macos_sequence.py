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
    embedded = workflow.split('          "$RUNNER_TOOL_CACHE/Python/3.11.18/arm64/bin/python3.11" - <<\'PY\'\n', 1)[1].split("\n          PY\n", 1)[0]
    assert textwrap.dedent(embedded).strip() == (root / "scripts/ci_r25_macos_sequence.py").read_text().strip()
    assert r25.PIN == "856980b6388ab4d57578b87c659364e3ef82a118"
    assert "actions/upload-artifact@v4" in workflow and "if: always()" in workflow


def test_success_is_rejected_when_any_stage_exit_is_missing(tmp_path):
    log = tmp_path / "log"
    log.write_text("ok")
    status = r25.stage_evidence(None, tmp_path / "artifact", tmp_path, log, {"status": "complete"})
    assert status["status"] == "failed" and "missing or nonzero" in status["reason"]


def test_redacts_environment_values_unquoted_secrets_and_url_credentials(tmp_path):
    text = ('TOKEN=alpha PASSWORD="two words" api_key:bravo '
            'https://user:pass@example.test/path https://access@example.test/')
    output = r25.redact(text, tmp_path)
    for secret in ("alpha", "two words", "bravo", "user:pass", "access@"):
        assert secret not in output
    assert output.count("<REDACTED>") >= 5


def test_subprocess_environment_ignores_user_config_and_uses_owned_temp_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "sensitive")
    env = r25.build_env(tmp_path / "home", tmp_path, "/usr/bin:/bin")
    assert "GITHUB_TOKEN" not in env
    assert env["PIP_CONFIG_FILE"] == "/dev/null"
    npm_user = Path(env["NPM_CONFIG_USERCONFIG"])
    npm_global = Path(env["NPM_CONFIG_GLOBALCONFIG"])
    assert npm_user != npm_global
    for config in (npm_user, npm_global):
        assert config.is_relative_to(tmp_path / "r25-git-config")
        assert config.read_text() == "" and config.stat().st_mode & 0o777 == 0o600
    cache = tmp_path / "r25-private-cache"
    assert cache.stat().st_mode & 0o777 == 0o700
    for name in ("NPM_CONFIG_CACHE", "PIP_CACHE_DIR", "UV_CACHE_DIR", "BUN_INSTALL_CACHE_DIR"):
        assert Path(env[name]).is_relative_to(cache)


def test_actual_npm_version_with_isolated_distinct_configs(tmp_path):
    import os
    import shutil
    import subprocess
    import pytest
    if shutil.which("npm") is None:
        pytest.skip("npm unavailable")
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env = r25.build_env(home, tmp_path, os.environ.get("PATH", "/usr/bin:/bin"))
    result = subprocess.run(["npm", "--version"], env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip()


def test_find_early_run_root_from_trace_without_trusting_other_paths(tmp_path, monkeypatch):
    import os
    monkeypatch.setattr(r25, "UID", os.getuid())
    parent = tmp_path / "r25-option-b-runs"
    parent.mkdir()
    root = parent / "run.ABC123xy"
    root.mkdir()
    trace = f"+ run_root={root}\n+ test 40700 = 700\n"
    assert r25.find_run_root(trace, tmp_path) == root
    assert r25.find_run_root(trace + "+ run_root=/tmp/other\n", tmp_path) is None
    root.rmdir()
    root.symlink_to(tmp_path, target_is_directory=True)
    assert r25.find_run_root(trace, tmp_path) is None


def test_r4_requires_dry_preflight_and_runner_owned_tools():
    source = (Path(__file__).parents[1] / "scripts/ci_r25_macos_sequence.py").read_text()
    assert "6d9afca52867b7b039958784ca754ed71286f1916ffaf903618a8fdd34c51f3c" in source
    assert "8b89801f527a85e3f7de539512c9dde08673ac99db6458cc7cc627e05d707054" in source
    assert '"R25_PREFLIGHT_ONLY": "1"' in source
    assert source.index('"R25_PREFLIGHT_ONLY": "1"') < source.index('subprocess.Popen(')
    assert 'dry_dir.rmdir()' in source
    assert 'actions-runner/_work/_tool/Python/3.11.18/arm64' in source
