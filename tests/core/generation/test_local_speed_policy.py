"""Practical policy edits and incomplete ordinary results must fail closed."""

import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from tests.local_speed_meter_run import parse_reports, require_junit
from tests.local_speed_policy import check_policy


def git(root, *args):
    return subprocess.check_output(
        ["git", "-C", str(root), "-c", "user.name=Meter policy proof",
         "-c", "user.email=meter-policy@example.invalid", *args], text=True).strip()


def write(root, path, content):
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)


def commit(root, message):
    git(root, "add", ".")
    git(root, "commit", "-qm", message)
    return git(root, "rev-parse", "HEAD")


def proposed_change(root, path, content):
    git(root, "init", "-q")
    write(root, "astrid/product.py", b"unchanged product\n")
    write(root, path, b"accepted bytes\n")
    base = commit(root, "accepted base")
    write(root, path, content)
    head = commit(root, "proposed change")
    git(root, "checkout", "--detach", base)
    return base, head


@pytest.mark.parametrize("path,content", [
    ("tests/_local_speed_baseline.py", b"TOLERANCE = 10\n"),
    ("tests/fixtures/local-speed-baselines/linux.json", b'{"ceiling": 99999}\n'),
    ("tests/integrations/test_local_speed_meter.py", b"pytestmark = pytest.mark.skip\n"),
    ("tests/core/generation/test_local_speed_census.py", b"pytestmark = pytest.mark.xfail\n"),
    ("tests/lanes.py", b"drop_meter_tests = True\n"),
    ("pytest.ini", b"[pytest]\naddopts = -k only_green\n"),
    ("tests/integrations/pytest.ini", b"[pytest]\naddopts = -m cheap\n"),
    ("pyproject.toml", b'[tool.pytest.ini_options]\naddopts = "--ignore=tests"\n'),
    ("tests/conftest.py", b"def pytest_collection_modifyitems(items): items.clear()\n"),
    (".github/workflows/plan55-native-flush.yml", b"env:\n  PYTEST_ADDOPTS: -k green\n"),
])
def test_base_policy_rejects_realistic_weakening(tmp_path, path, content):
    base, head = proposed_change(tmp_path, path, content)
    with pytest.raises(ValueError, match="require Lead review") as error:
        check_policy(tmp_path, base, head)
    assert path in str(error.value)


def test_candidate_cannot_replace_the_checker_that_grades_it(tmp_path):
    checker = "tests/local_speed_policy.py"
    git(tmp_path, "init", "-q")
    source = Path(__file__).parents[2] / "local_speed_policy.py"
    write(tmp_path, checker, source.read_bytes())
    base = commit(tmp_path, "accepted checker")
    write(tmp_path, checker, b'print("PASS")\n')
    head = commit(tmp_path, "proposed self-approving checker")
    candidate = subprocess.run([sys.executable, checker], cwd=tmp_path, capture_output=True, text=True)
    assert candidate.returncode == 0 and candidate.stdout.strip() == "PASS"
    git(tmp_path, "checkout", "--detach", base)
    accepted = subprocess.run([sys.executable, checker, "--base", base, "--head", head],
                              cwd=tmp_path, capture_output=True, text=True)
    assert accepted.returncode == 1
    assert checker in accepted.stdout and '"status": "FAIL"' in accepted.stdout


@pytest.mark.parametrize("operation", ["add", "delete"])
def test_added_configuration_and_deleted_gate_do_not_avoid_protection(tmp_path, operation):
    git(tmp_path, "init", "-q")
    path = "tests/integrations/conftest.py"
    write(tmp_path, "astrid/product.py", b"product\n")
    if operation == "delete":
        write(tmp_path, path, b"accepted fixture\n")
    base = commit(tmp_path, "base")
    if operation == "delete":
        (tmp_path / path).unlink()
    else:
        write(tmp_path, path, b"drop = True\n")
    head = commit(tmp_path, "changed selection")
    git(tmp_path, "checkout", "--detach", base)
    with pytest.raises(ValueError, match="require Lead review"):
        check_policy(tmp_path, base, head)


def test_unchanged_gate_accepts_an_ordinary_product_change(tmp_path):
    base, head = proposed_change(tmp_path, "astrid/product.py", b"improved product\n")
    receipt = check_policy(tmp_path, base, head)
    assert receipt == {"status": "PASS", "policy_source": base, "candidate": head,
                       "protected_changes": [], "decision": "6979024059"}


def test_policy_refuses_to_run_from_the_candidate_checkout(tmp_path):
    base, head = proposed_change(tmp_path, "astrid/product.py", b"product change\n")
    git(tmp_path, "checkout", "--detach", head)
    with pytest.raises(ValueError, match="clean accepted base"):
        check_policy(tmp_path, base, head)


@pytest.mark.parametrize("result", ["missing", "empty", "truncated", "skip", "xfail", "deselect"])
def test_ordinary_incomplete_meter_results_fail_closed(tmp_path, result):
    path = tmp_path / "results.xml"
    if result == "empty":
        path.write_text("", encoding="utf-8")
    elif result == "truncated":
        path.write_text('<testsuites><testsuite tests="10"', encoding="utf-8")
    elif result in {"skip", "xfail", "deselect"}:
        count, skipped = (9, 0) if result == "deselect" else (10, 1)
        path.write_text(f'<testsuites><testsuite tests="{count}" failures="0" errors="0" '
                        f'skipped="{skipped}"/></testsuites>', encoding="utf-8")
    with pytest.raises((OSError, ET.ParseError, ValueError)):
        require_junit(path, samples=5)


@pytest.mark.parametrize("result", ["missing", "empty", "truncated"])
def test_ordinary_incomplete_observations_fail_closed(tmp_path, result):
    path = tmp_path / "observations.log"
    if result == "empty":
        path.write_text("", encoding="utf-8")
    elif result == "truncated":
        path.write_text('LOCAL_SPEED_OBSERVATION {"caller":', encoding="utf-8")
    with pytest.raises((OSError, ValueError)):
        parse_reports(path, instrumented=True, samples=5, require_gate=True)
