"""GL.2's five mutations use the real scanner and the trusted checker CLI."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from scripts.reshape import check_structure_ceiling as gate

ROOT = Path(__file__).resolve().parents[2]
POLICY = ROOT / "scripts/reshape/check_structure_ceiling.py"
KNOWN = "top-level astrid module must move to a canonical package: astrid/agent_context.py"
OTHER = "top-level astrid module must move to a canonical package: astrid/omp_agent.py"
NEW = "top-level astrid module must move to a canonical package: astrid/gl2_new.py"


def write_ceiling(root: Path, records: list[str]) -> None:
    document = json.loads((ROOT / gate.CEILING).read_text())
    document["violations"] = sorted(records)
    path = root / gate.CEILING
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document) + "\n")


def run_gate(root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-I", str(POLICY), "--root", str(root)],
        capture_output=True, text=True, check=False,
    )


@pytest.fixture
def candidate(tmp_path: Path) -> Path:
    for directory in ("astrid", "astrid/core", "astrid/packs"):
        path = tmp_path / directory
        path.mkdir(parents=True, exist_ok=True)
        (path / "__init__.py").write_text("")
    for name in ("agent_context.py", "omp_agent.py"):
        (tmp_path / "astrid" / name).write_text("value = 1\n")
    write_ceiling(tmp_path, [KNOWN, OTHER])
    control = run_gate(tmp_path)
    assert control.returncode == 0, control.stdout + control.stderr
    assert "candidate=2 current=2 errors=0" in control.stdout
    return tmp_path


@pytest.mark.parametrize(
    ("mutation", "exit_code", "message"),
    [
        ("new-finding", 1, "new structure finding: " + NEW),
        ("add-entry", 1, "ceiling entry added or edited: " + NEW),
        ("edit-entry", 1, "ceiling entry added or edited: " + NEW),
        ("remove-live-entry", 1, "ceiling entry removed while its finding remains: " + KNOWN),
        ("fix-and-remove", 0, "candidate=1 current=1 errors=0"),
    ],
)
def test_packet_injections(candidate: Path, mutation: str, exit_code: int, message: str) -> None:
    if mutation in {"new-finding", "add-entry", "edit-entry"}:
        (candidate / "astrid/gl2_new.py").write_text("value = 2\n")
    if mutation == "add-entry":
        write_ceiling(candidate, [KNOWN, OTHER, NEW])
    if mutation == "edit-entry":
        (candidate / "astrid/agent_context.py").unlink()
        write_ceiling(candidate, [OTHER, NEW])
    if mutation in {"remove-live-entry", "fix-and-remove"}:
        write_ceiling(candidate, [OTHER])
    if mutation == "fix-and-remove":
        (candidate / "astrid/agent_context.py").unlink()
    result = run_gate(candidate)
    assert result.returncode == exit_code, result.stdout + result.stderr
    assert message in result.stdout + result.stderr


def test_removed_entry_cannot_return_after_the_next_merge() -> None:
    origin = gate.load_ceiling(ROOT / gate.CEILING).origin
    accepted = gate.Ceiling(origin, (OTHER,))
    restored = gate.Ceiling(origin, tuple(sorted([KNOWN, OTHER])))
    errors = gate.compare_ceiling(accepted, restored, [KNOWN, OTHER])
    assert "ceiling entry added or edited: " + KNOWN in errors


def test_fixing_all_remaining_findings_allows_an_empty_ceiling(candidate: Path) -> None:
    for name in ("agent_context.py", "omp_agent.py"):
        (candidate / "astrid" / name).unlink()
    write_ceiling(candidate, [])
    result = run_gate(candidate)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "candidate=0 current=0 errors=0" in result.stdout


@pytest.mark.parametrize("contents", [None, "", '{"tool":"structure",', "{}", "[]"])
def test_missing_empty_or_truncated_ceiling_fails(candidate: Path, contents: str | None) -> None:
    path = candidate / gate.CEILING
    if contents is None:
        path.unlink()
    else:
        path.write_text(contents)
    result = run_gate(candidate)
    assert result.returncode == 2, result.stdout + result.stderr
    assert "Structure ceiling failed:" in result.stderr


def test_candidate_cannot_run_its_own_gate_or_scanner(candidate: Path) -> None:
    (candidate / "astrid/gl2_new.py").write_text("value = 2\n")
    for relative in ("scripts/reshape/check_structure_ceiling.py", "astrid/core/structure.py"):
        (candidate / relative).write_text("raise SystemExit(0)\n")
    result = run_gate(candidate)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "new structure finding: " + NEW in result.stderr
    assert "Structure scanner source: " + str(ROOT) in result.stdout


def test_missing_source_tree_is_not_a_clean_scan(candidate: Path) -> None:
    (candidate / "astrid/core/__init__.py").unlink()
    result = run_gate(candidate)
    assert result.returncode == 2, result.stdout + result.stderr
    assert "incomplete source tree" in result.stderr


def test_early_success_exit_is_not_a_completed_scan(candidate: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def incomplete_scan(root: Path) -> list[str]:
        raise SystemExit(0)

    monkeypatch.setattr(gate, "scan", incomplete_scan)
    assert gate.main(["--root", str(candidate)]) == 2


def test_policy_cannot_read_its_own_ceiling_as_the_base() -> None:
    result = run_gate(ROOT)
    assert result.returncode == 2, result.stdout + result.stderr
    assert "not the candidate's own copy" in result.stderr


@pytest.mark.parametrize("has_checker", [False, True])
def test_actual_workflow_never_falls_back_to_candidate_gate(tmp_path: Path, has_checker: bool) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    path = tmp_path / "scripts/reshape/check_structure_ceiling.py"
    path.parent.mkdir(parents=True)
    (tmp_path / "README.md").write_text("fixture\n")
    if has_checker:
        path.write_text("print('trusted base refuses')\nraise SystemExit(1)\n")
    for arguments in (["add", "."], ["-c", "user.name=GL2 Fixture", "-c", "user.email=gl2@example.invalid",
                                   "commit", "-qm", "trusted base"]):
        subprocess.run(["git", "-C", str(tmp_path), *arguments], check=True, capture_output=True)
    base = subprocess.check_output(["git", "-C", str(tmp_path), "rev-parse", "HEAD"], text=True).strip()
    path.write_text("print('candidate falsely passes')\nraise SystemExit(0)\n")
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    step = next(step for step in workflow["jobs"]["checks"]["steps"]
                if step.get("name") == "Structure ceiling policy from merge base")
    assert step["if"] == "${{ !cancelled() }}"
    assert "continue-on-error" not in step
    result = subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", step["run"]], cwd=tmp_path,
        env={**os.environ, "RUNNER_TEMP": str(tmp_path), "ASTRID_GATE_BASE": base,
             "GITHUB_WORKSPACE": str(tmp_path)}, capture_output=True, text=True, check=False,
    )
    assert result.returncode != 0, result.stdout + result.stderr
    assert "candidate falsely passes" not in result.stdout + result.stderr
    if has_checker:
        assert "trusted base refuses" in result.stdout


def test_default_ceiling_cannot_be_regenerated() -> None:
    from scripts.reshape import compare_structure_baseline

    result = subprocess.run(
        [sys.executable, str(Path(compare_structure_baseline.__file__)), "--write-baseline"],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 2, result.stdout + result.stderr
    assert "approved structure ceiling cannot be regenerated" in result.stderr


def test_structure_scanner_changes_require_exact_head_owner_approval(candidate: Path) -> None:
    subprocess.run(["git", "init", "-q", str(candidate)], check=True)
    scanner = candidate / "astrid/core/structure.py"
    scanner.write_text("# accepted scanner\n")
    for arguments in (["add", "."], ["-c", "user.name=GL2 Fixture", "-c", "user.email=gl2@example.invalid",
                                   "commit", "-qm", "trusted scanner"]):
        subprocess.run(["git", "-C", str(candidate), *arguments], check=True, capture_output=True)
    base = subprocess.check_output(["git", "-C", str(candidate), "rev-parse", "HEAD"], text=True).strip()
    scanner.write_text("# weakened scanner\n")
    for arguments in (["add", "."], ["-c", "user.name=GL2 Fixture", "-c", "user.email=gl2@example.invalid",
                                   "commit", "-qm", "candidate scanner"]):
        subprocess.run(["git", "-C", str(candidate), *arguments], check=True, capture_output=True)
    result = subprocess.run(
        [sys.executable, "-I", str(ROOT / "scripts/ci/protect_gate_paths.py"), "--base-ref", base],
        cwd=candidate, capture_output=True, text=True, check=False,
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert "astrid/core/structure.py" in result.stdout
    assert "approval of this exact head" in result.stdout
