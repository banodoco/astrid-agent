"""The gate policy is read from trusted code, never from the changed tree."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
POLICY = Path(os.environ.get("ASTRID_TEST_GATE_POLICY", ROOT / "scripts/ci/protect_gate_paths.py"))


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


def commit(root):
    git(root, "add", ".")
    git(root, "-c", "user.name=Gate Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "fixture")
    return git(root, "rev-parse", "HEAD")


@pytest.fixture
def repository(tmp_path):
    git(tmp_path, "init", "-q")
    (tmp_path / "pyproject.toml").write_text('[tool.ruff.lint]\nselect=["F"]\n[tool.mypy]\n')
    path = tmp_path / "scripts/reshape/identity_gate.py"
    path.parent.mkdir(parents=True)
    path.write_text("raise SystemExit(1)\n")
    return tmp_path


def policy(root, base):
    return subprocess.run([sys.executable, "-I", str(POLICY), "controls", "--base-ref", base],
                          cwd=root, capture_output=True, text=True, check=False)


def test_replacing_the_gate_with_return_zero_requires_owner_approval(repository):
    base = commit(repository)
    gate = repository / "scripts/reshape/identity_gate.py"
    gate.write_text("raise SystemExit(0)\n")
    commit(repository)
    old = subprocess.run([sys.executable, str(gate)], cwd=repository, check=False)
    assert old.returncode == 0
    result = policy(repository, base)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "scripts/reshape/identity_gate.py" in result.stdout + result.stderr


@pytest.mark.parametrize("path", [
    "scripts/ci/run_pytest.py", "scripts/ci/protect_gate_paths.py", "scripts/check_collection.py",
    "scripts/known_collection_errors.txt", "tests/lanes.py", "Makefile", ".github/workflows/ci.yml",
    ".github/workflows/gate-integrity.yml", "requirements/dev.lock", "scripts/__init__.py",
])
def test_gate_entrypoints_registry_workflows_and_tool_locks_are_protected(repository, path):
    base = commit(repository)
    target = repository / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("changed\n")
    commit(repository)
    result = policy(repository, base)
    assert result.returncode == 1, result.stdout + result.stderr
    assert path in result.stdout + result.stderr


def test_unchanged_policy_allows_ordinary_source_changes(repository):
    base = commit(repository)
    (repository / "product.py").write_text("value = 1\n")
    commit(repository)
    result = policy(repository, base)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("path", ["setup.py", "scripts/ci_install_runtime.py", "scripts/vibecomfy_pin.py", "pytest11"])
def test_install_time_execution_surfaces_require_owner_approval(repository, path):
    base = commit(repository)
    if path == "pytest11":
        config = repository / "pyproject.toml"
        config.write_text(config.read_text() + '[project.entry-points.pytest11]\nweakener="candidate_plugin"\n')
    else:
        target = repository / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("print('candidate install code')\n")
    commit(repository)
    result = policy(repository, base)
    assert result.returncode == 1, result.stdout + result.stderr
    assert path in result.stdout + result.stderr


def test_gate_tool_version_pins_require_owner_approval(repository):
    config = repository / "pyproject.toml"
    config.write_text(config.read_text() + '[project.optional-dependencies]\ndev=["ruff==0.12.0"]\n')
    base = commit(repository)
    config.write_text(config.read_text().replace("ruff==0.12.0", "ruff==0.1.0"))
    commit(repository)
    result = policy(repository, base)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "gate tool version pins" in result.stdout


@pytest.mark.parametrize("owners,reviews,expected", [
    (set(), [("owner", "APPROVED", "head")], None),
    ({"owner"}, [("other", "APPROVED", "head")], None),
    ({"owner"}, [("owner", "APPROVED", "old")], None),
    ({"owner"}, [("owner", "APPROVED", "head")], "owner"),
    ({"owner"}, [("owner", "APPROVED", "head"), ("owner", "CHANGES_REQUESTED", "head")], None),
    ({"owner"}, [("owner", "APPROVED", "head"), ("owner", "DISMISSED", "head")], None),
    ({"owner"}, [("owner", "APPROVED", "head"), ("owner", "COMMENTED", "head")], "owner"),
])
def test_approval_is_named_current_and_not_revoked(owners, reviews, expected):
    from scripts.ci.protect_gate_paths import approving_owner

    rows = [{"id": number, "user": {"login": login}, "state": state, "commit_id": sha}
            for number, (login, state, sha) in enumerate(reviews)]
    assert approving_owner("head", owners, rows) == expected


def test_integrity_workflow_never_runs_candidate_code():
    import yaml

    workflow = yaml.safe_load((ROOT / ".github/workflows/gate-integrity.yml").read_text())
    events = workflow.get("on", workflow.get(True))
    assert "pull_request_target" in events and "pull_request" not in events
    assert "release/*/plan-*" in events["pull_request_target"]["branches"]
    assert workflow["permissions"] == {"contents": "read", "pull-requests": "read"}
    steps = workflow["jobs"]["integrity"]["steps"]
    checkout = steps[0]
    assert checkout["with"] == {"ref": "${{ github.event.repository.default_branch }}",
                                "fetch-depth": 0, "persist-credentials": False}
    commands = [step["run"] for step in steps if "run" in step]
    assert commands == ['python -I scripts/ci/protect_gate_paths.py --pull-request "$PULL_REQUEST"']
    assert steps[-1]["env"]["ASTRID_GATE_OWNERS"] == "${{ vars.ASTRID_GATE_OWNERS }}"
