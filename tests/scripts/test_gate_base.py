"""A manual check covers the entire task, including findings before HEAD^1."""

import subprocess

import pytest

from scripts.ci.gate_base import resolve
from scripts.reshape import identity_gate


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


def commit(root):
    git(root, "add", ".")
    git(root, "-c", "user.name=Gate Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "fixture")
    return git(root, "rev-parse", "HEAD")


def test_manual_destination_base_catches_a_finding_before_the_last_commit(tmp_path, monkeypatch):
    git(tmp_path, "init", "-q")
    (tmp_path / "astrid").mkdir()
    (tmp_path / "astrid/example.py").write_text("value = 1\n")
    (tmp_path / "pyproject.toml").write_text('[tool.ruff]\ninclude=["astrid/**/*.py"]\n[tool.ruff.lint]\nselect=["F"]\n')
    base = commit(tmp_path)
    git(tmp_path, "branch", "release/14/plan-39", base)
    git(tmp_path, "remote", "add", "origin", str(tmp_path))
    (tmp_path / "astrid/example.py").write_text("value = missing\n")
    commit(tmp_path)
    (tmp_path / "README.md").write_text("second topic commit\n")
    commit(tmp_path)
    monkeypatch.chdir(tmp_path)
    # Previous workflow's fallback misses a finding introduced one commit ago.
    assert identity_gate.main(["ruff", "--base-ref", "HEAD^1"]) == 0
    actual = resolve("workflow_dispatch", {"inputs": {"target_branch": "release/14/plan-39"}})
    assert actual == base
    assert identity_gate.main(["ruff", "--base-ref", actual]) == 1


@pytest.mark.parametrize("event", [{}, {"inputs": {}}, {"inputs": {"target_branch": "HEAD^1"}},
                                  {"inputs": {"target_branch": "--upload-pack=bad"}}])
def test_manual_run_without_an_explicit_safe_destination_is_refused(event):
    with pytest.raises(ValueError, match="target_branch"):
        resolve("workflow_dispatch", event)


@pytest.mark.parametrize("name,event", [
    ("push", {"before": "0" * 40}), ("push", {"before": "HEAD^1"}),
    ("pull_request", {"pull_request": {"base": {"sha": "main"}}}),
])
def test_missing_or_mutable_event_commits_do_not_fall_back_to_parent(name, event):
    with pytest.raises(ValueError, match="immutable comparison commit"):
        resolve(name, event)
