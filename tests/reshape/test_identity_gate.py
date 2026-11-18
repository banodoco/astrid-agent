"""GL.1 fooling cases: the old counts pass; the identity gate refuses them."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from scripts.reshape import (
    compare_mypy_baseline,
    compare_ruff_baseline,
    count_baseline,
    identity_gate,
)


def _commit(root: Path) -> str:
    for args in (["add", "."], ["-c", "user.name=Gate Fixture", "-c", "user.email=fixture@example.invalid",
                              "commit", "-qm", "fixture"]):
        subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)
    return identity_gate.git(root, "rev-parse", "HEAD")


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    for directory in ("astrid", "scripts/reshape", "scripts/reshape/baselines"):
        (tmp_path / directory).mkdir(parents=True, exist_ok=True)
    (tmp_path / "astrid/__init__.py").write_text("")
    (tmp_path / "scripts/reshape/__init__.py").write_text("")
    (tmp_path / "scripts/reshape/baselines/count.json").write_text('{"finding_count": 1}\n')
    (tmp_path / "pyproject.toml").write_text(
        '[tool.ruff]\ninclude=["astrid/**/*.py"]\n[tool.ruff.lint]\nselect=["F"]\n'
        '[tool.mypy]\nfollow_imports="skip"\n'
    )
    return tmp_path


@pytest.mark.parametrize("tool,old,new", [
    ("ruff", "def old():\n    return missing\n", "def replacement():\n    return missing\n"),
    ("mypy", 'def old() -> None:\n    value: int = "wrong"\n',
     'def replacement() -> None:\n    value: int = "wrong"\n'),
    ("mypy", 'def same() -> None:\n    old: int = "wrong"\n',
     'def same() -> None:\n    replacement: int = "wrong"\n'),
    ("ruff", "def same():\n    return missing\n", "def same():\n    print(missing)\n    return 1\n"),
])
def test_fixed_finding_cannot_pay_for_another_in_the_same_file(
    repository: Path, monkeypatch: pytest.MonkeyPatch, tool: str, old: str, new: str,
) -> None:
    path = repository / "astrid/example.py"
    path.write_text(old)
    base = _commit(repository)
    before = identity_gate.diagnostics(tool, repository)
    path.write_text(new)
    _commit(repository)
    after = identity_gate.diagnostics(tool, repository)
    legacy, _ = count_baseline.file_code_verdict(
        count_baseline.file_code_counts(after), count_baseline.file_code_counts(before),
        count_baseline.BLOCKING_CODES[tool.title() if tool == "ruff" else "Mypy"],
    )
    assert legacy == 0
    assert len(before) == len(after) == 1
    assert before[0]["message"] == after[0]["message"]
    monkeypatch.chdir(repository)
    assert identity_gate.main([tool, "--base-ref", base]) == 1


@pytest.mark.parametrize("statement", ['number: int = "bad"\n',
                                        'def f(value: int) -> None:\n    pass\nf("bad")\n'])
def test_every_mypy_code_blocks_a_new_finding(repository: Path, monkeypatch, statement: str) -> None:
    path = repository / "astrid/example.py"
    path.write_text("number: int = 1\n")
    base = _commit(repository)
    path.write_text(statement)
    _commit(repository)
    findings = identity_gate.diagnostics("mypy", repository)
    assert {row["code"] for row in findings} <= {"assignment", "arg-type"}
    assert findings
    assert count_baseline.file_code_verdict(
        count_baseline.file_code_counts(findings), [], count_baseline.BLOCKING_CODES["Mypy"],
    )[0] == 0
    monkeypatch.chdir(repository)
    assert identity_gate.main(["mypy", "--base-ref", base]) == 1


@pytest.mark.parametrize("change", ["baseline", "ruff-ignore", "mypy-ignore", "noqa", "type-ignore", "test-ignore"])
def test_task_cannot_change_its_allowance(repository: Path, monkeypatch, change: str) -> None:
    source = repository / "astrid/example.py"
    source.write_text("value: int = 1\n")
    total = repository / "scripts/reshape/baselines/count.json"
    total.write_text('{"finding_count": 0}\n')
    pairs = repository / "scripts/reshape/baselines/pairs.json"
    pairs.write_text("[]\n")
    base = _commit(repository)
    if change == "baseline":
        source.write_text("value = missing\n")
        total.write_text('{"finding_count": 1000}\n')
        pairs.write_text(json.dumps([{"path": "astrid/example.py", "code": "F821", "count": 1}]))
    elif change == "ruff-ignore":
        source.write_text("value = missing\n")
        config = repository / "pyproject.toml"
        config.write_text(config.read_text().replace('select=["F"]', 'select=["F"]\nignore=["F821"]'))
    elif change == "mypy-ignore":
        source.write_text('value: int = "bad"\n')
        config = repository / "pyproject.toml"
        config.write_text(config.read_text() + 'ignore_errors=true\n')
    elif change == "noqa":
        source.write_text("value = missing  # noqa: F821\n")
    elif change == "test-ignore":
        config = repository / "pyproject.toml"
        config.write_text(config.read_text() + '[tool.pytest.ini_options]\nnorecursedirs=["tests"]\n')
    else:
        source.write_text('value: int = "bad"  # type: ignore[assignment]\n')
    _commit(repository)
    monkeypatch.chdir(repository)
    # Execute the unchanged baseline wrappers, not a reimplementation of them.
    tool = "mypy" if change in {"mypy-ignore", "type-ignore"} else "ruff"
    with monkeypatch.context() as legacy:
        arguments = [tool, "--file-code-baseline", str(pairs)]
        if tool == "ruff":
            arguments.extend(["--baseline", str(total)])
        legacy.setattr("sys.argv", arguments)
        assert (compare_ruff_baseline.main if tool == "ruff" else compare_mypy_baseline.main)() == 0
    if change in {"noqa", "type-ignore"}:
        assert identity_gate.diagnostics(tool, repository) == []
    assert identity_gate.main(["controls", "--base-ref", base]) == 1


def test_editing_the_structure_baseline_cannot_admit_a_new_violation(repository: Path, monkeypatch) -> None:
    from scripts.reshape import compare_structure_baseline

    baseline = repository / "scripts/reshape/baselines/structure_astrid.json"
    baseline.write_text('{"violations": []}\n')
    base = _commit(repository)
    baseline.write_text('{"violations": ["new structure violation"]}\n')
    _commit(repository)
    monkeypatch.chdir(repository)
    with monkeypatch.context() as legacy:
        legacy.setattr("sys.argv", ["structure", "--baseline", str(baseline)])
        legacy.setattr(compare_structure_baseline, "current", lambda: ["new structure violation"])
        assert compare_structure_baseline.main() == 0
    assert identity_gate.main(["controls", "--base-ref", base]) == 1


def test_moving_a_suppression_cannot_pay_for_a_new_suppressed_finding(repository: Path, monkeypatch) -> None:
    path = repository / "astrid/example.py"
    path.write_text("def value():\n    return old_missing  # noqa: F821\n")
    base = _commit(repository)
    assert identity_gate.diagnostics("ruff", repository) == []
    path.write_text("def value():\n    return new_missing  # noqa: F821\n")
    _commit(repository)
    assert identity_gate.diagnostics("ruff", repository) == []
    monkeypatch.chdir(repository)
    assert identity_gate.main(["controls", "--base-ref", base]) == 1


def test_identity_survives_line_shifts_but_preserves_message_and_multiplicity(tmp_path: Path) -> None:
    path = tmp_path / "test.py"
    path.write_text("def same():\n    return missing\n")
    finding = {"path": "test.py", "code": "F821", "message": "Undefined name `missing`", "line": 2}
    original = identity_gate.identities([finding], tmp_path)
    path.write_text("\n\n\n" + path.read_text())
    moved = {**finding, "line": 5}
    assert identity_gate.identities([moved], tmp_path) == original
    assert sum((identity_gate.identities([moved, moved], tmp_path) - original).values()) == 1
    assert identity_gate.identities([{**moved, "message": "Undefined name `different`"}], tmp_path) != original


def test_resolving_findings_passes_without_rewriting_a_baseline(repository: Path, monkeypatch) -> None:
    path = repository / "astrid/example.py"
    path.write_text("value = missing\n")
    (repository / "astrid/remaining.py").write_text("remaining = still_missing\n")
    base = _commit(repository)
    path.write_text("value = 1\n")
    _commit(repository)
    monkeypatch.chdir(repository)
    assert identity_gate.main(["ruff", "--base-ref", base]) == 0
    assert json.loads((repository / "scripts/reshape/baselines/count.json").read_text()) == {"finding_count": 1}


def test_missing_comparison_base_is_refused(repository: Path, monkeypatch) -> None:
    _commit(repository)
    monkeypatch.chdir(repository)
    assert identity_gate.main(["ruff", "--base-ref", "refs/heads/missing"]) == 2


@pytest.mark.parametrize("filename,contents", [
    ("astrid/ruff.toml", "[lint]\nselect=[]\n"),
    ("astrid/.ruff.toml", "[lint]\nselect=[]\n"),
    ("pytest.ini", '[pytest]\naddopts = -k "not victim"\n'),
    ("mypy.ini", "[mypy]\nignore_errors = True\n"),
    (".mypy.ini", "[mypy]\nignore_errors = True\n"),
    ("astrid/tox.ini", "[pytest]\npython_functions = hidden_*\n"),
    ("astrid/setup.cfg", "[mypy]\nignore_errors = True\n"),
    ("astrid/pytest.ini", "[pytest]\naddopts = -k not_victim\n"),
    ("astrid/pyproject.toml", '[tool.ruff.lint]\nselect=[]\n'),
])
def test_alternate_and_nested_tool_config_cannot_change_collection_or_findings(
    repository: Path, monkeypatch, filename: str, contents: str,
) -> None:
    base = _commit(repository)
    (repository / filename).write_text(contents)
    _commit(repository)
    monkeypatch.chdir(repository)
    assert identity_gate.main(["controls", "--base-ref", base]) == 1


def test_a_committed_gitignored_file_is_still_linted(repository: Path, monkeypatch) -> None:
    base = _commit(repository)
    (repository / ".gitignore").write_text("astrid/victim.py\n")
    (repository / "astrid/victim.py").write_text("value = missing\n")
    subprocess.run(["git", "-C", str(repository), "add", "-f", "astrid/victim.py"], check=True)
    _commit(repository)
    monkeypatch.chdir(repository)
    assert identity_gate.main(["ruff", "--base-ref", base]) == 1


def test_untracked_dependency_checkout_is_not_part_of_either_lint_tree(repository: Path, monkeypatch) -> None:
    base = _commit(repository)
    dependency = repository / "dependency"
    dependency.mkdir()
    (dependency / "ruff.toml").write_text('[lint]\nselect=["F"]\n')
    (dependency / "test_dependency.py").write_text("value = missing\n")
    monkeypatch.chdir(repository)
    assert identity_gate.main(["ruff", "--base-ref", base]) == 0


@pytest.mark.parametrize("tool", ["ruff", "mypy"])
def test_project_package_cannot_replace_the_installed_gate_tool(repository: Path, monkeypatch, tool: str) -> None:
    source = "value = missing\n" if tool == "ruff" else 'value: int = "wrong"\n'
    (repository / "astrid/victim.py").write_text(source)
    base = _commit(repository)
    package = repository / tool
    package.mkdir()
    (package / "__init__.py").write_text("")
    (package / "__main__.py").write_text("print('[]')\n" if tool == "ruff" else "print('Success: no issues found')\n")
    _commit(repository)
    monkeypatch.delenv("PYTHONSAFEPATH", raising=False)
    monkeypatch.chdir(repository)
    assert identity_gate.main([tool, "--base-ref", base]) == 1
    # Isolation must also work if a caller explicitly includes the checkout.
    monkeypatch.setenv("PYTHONPATH", str(repository))
    assert identity_gate.diagnostics(tool, repository)


@pytest.mark.parametrize("tool", ["ruff", "mypy"])
def test_a_nonzero_base_cannot_collapse_to_zero_findings(repository: Path, monkeypatch, tool: str) -> None:
    source = "value = missing\n" if tool == "ruff" else 'value: int = "wrong"\n'
    path = repository / "astrid/example.py"
    path.write_text(source)
    base = _commit(repository)
    path.write_text("value = 1\n")
    _commit(repository)
    monkeypatch.chdir(repository)
    assert identity_gate.main([tool, "--base-ref", base]) == 1


@pytest.mark.parametrize("poison", ["stub", "decorator", "decorator-alias"])
def test_stub_and_no_type_check_cannot_hide_a_typed_body(repository: Path, monkeypatch, poison: str) -> None:
    source = 'def victim() -> int:\n    value: int = "wrong"\n    return value\n'
    victim = repository / "astrid/victim.py"
    victim.write_text(source)
    (repository / "astrid/remaining.py").write_text('value: int = "also wrong"\n')
    base = _commit(repository)
    if poison == "stub":
        (repository / "astrid/victim.pyi").write_text("def victim() -> int: ...\n")
    elif poison == "decorator":
        victim.write_text("import typing\n@typing.no_type_check\n" + source)
    else:
        victim.write_text("from typing import no_type_check as unchecked\n@unchecked\n" + source)
    _commit(repository)
    assert len(identity_gate.diagnostics("mypy", repository)) == 1
    monkeypatch.chdir(repository)
    assert identity_gate.main(["controls", "--base-ref", base]) == 1


def test_suppression_comments_in_existing_stubs_are_scanned(repository: Path, monkeypatch) -> None:
    stub = repository / "astrid/interface.pyi"
    stub.write_text("value: int\n")
    base = _commit(repository)
    stub.write_text("value: int  # type: ignore[assignment]\n")
    _commit(repository)
    monkeypatch.chdir(repository)
    assert identity_gate.main(["controls", "--base-ref", base]) == 1


@pytest.mark.parametrize("definition", ["function", "class"])
def test_type_checking_stub_cannot_hide_a_runtime_definition(repository: Path, monkeypatch, definition: str) -> None:
    source = ('def victim() -> int:\n    value: int = "wrong"\n    return value\n' if definition == "function"
              else 'class Victim:\n    value: int = "wrong"\n')
    stub = "def victim() -> int: ..." if definition == "function" else "class Victim: ..."
    victim = repository / "astrid/victim.py"
    victim.write_text(source)
    (repository / "astrid/remaining.py").write_text('value: int = "also wrong"\n')
    base = _commit(repository)
    victim.write_text("from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    " + stub + "\nelse:\n"
                      + "".join("    " + line + "\n" for line in source.splitlines()))
    _commit(repository)
    assert len(identity_gate.diagnostics("mypy", repository)) == 1
    monkeypatch.chdir(repository)
    assert identity_gate.main(["controls", "--base-ref", base]) == 1
