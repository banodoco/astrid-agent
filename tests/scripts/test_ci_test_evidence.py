"""K0/SF3a.1 probes use real pytest children and their actual XML and log files."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

from scripts.ci.check_results import main as check_results
from scripts.ci.check_results import validate

ROOT = Path(__file__).resolve().parents[2]
LANE_HOOKS = "from tests.lanes import pytest_collection_modifyitems, pytest_configure, pytest_ignore_collect\n"


def run(root, *args, guarded=True, destination="result.xml"):
    environment = dict(os.environ, PYTHONPATH=str(ROOT))
    for key in ("PYTEST_ADDOPTS", "ASTRID_CI_JOB", "PYTEST_PLUGINS"):
        environment.pop(key, None)
    module = "scripts.ci.run_pytest" if guarded else "pytest"
    return subprocess.run(
        [sys.executable, "-m", module, "-q", "-rA", "-o", "addopts=",
         f"--junitxml={root / destination}", *args],
        cwd=root, env=environment, capture_output=True, text=True, check=False, timeout=45,
    )


@pytest.mark.parametrize("poison", ["silent-drop", "reported-drop", "collect-ignore", "collect-glob", "ignore-hook", "deselect"])
def test_old_guard_accepts_missing_test_but_collection_gate_rejects_it(tmp_path, poison):
    (tmp_path / "test_good.py").write_text("def test_good():\n    pass\n")
    (tmp_path / "test_bad.py").write_text("def test_bad():\n    assert False\n")
    conftest = LANE_HOOKS
    arguments = []
    if poison in {"silent-drop", "reported-drop"}:
        conftest += (
            "def pytest_collection_modifyitems(config, items):\n"
            "    dropped = [item for item in items if item.nodeid.startswith('test_bad.py')]\n"
            "    items[:] = [item for item in items if item not in dropped]\n"
        )
        if poison == "reported-drop":
            conftest += "    config.hook.pytest_deselected(items=dropped)\n"
    elif poison == "collect-ignore":
        conftest += "collect_ignore = ['test_bad.py']\n"
    elif poison == "collect-glob":
        conftest += "collect_ignore_glob = ['test_bad*']\n"
    elif poison == "ignore-hook":
        conftest += "def pytest_ignore_collect(collection_path, config):\n    return collection_path.name == 'test_bad.py'\n"
    else:
        arguments = ["--deselect", "test_bad.py::test_bad"]
    (tmp_path / "conftest.py").write_text(conftest)
    old = run(tmp_path, *arguments, guarded=False, destination="old.xml")
    assert old.returncode == 0, old.stdout + old.stderr
    assert "1 passed" in old.stdout
    current = run(tmp_path, *arguments)
    assert current.returncode != 0, current.stdout + current.stderr
    assert any(message in current.stdout + current.stderr for message in
               ("collection inventory mismatch", "undeclared collect_ignore", "undeclared collection exclusion", "undeclared ignored test path"))


def test_declared_lane_filters_and_shards_have_an_exact_independent_census(tmp_path):
    (tmp_path / "conftest.py").write_text(LANE_HOOKS)
    (tmp_path / "test_cases.py").write_text(
        "import pytest\n"
        "@pytest.mark.integration\ndef test_other_lane():\n    assert False\n"
        "def test_one():\n    pass\ndef test_two():\n    pass\ndef test_three():\n    pass\n"
    )
    result = run(tmp_path, "-m", "not integration", "--splits", "2", "--group", "1")
    assert result.returncode == 0, result.stdout + result.stderr
    reference = json.loads((tmp_path / "result.reference.json").read_text())
    actual = json.loads((tmp_path / "result.collection.json").read_text())
    assert reference == actual
    assert len(reference["collected"]) == 4
    assert sorted(reference["selected"] + reference["deselected"]) == reference["collected"]
    assert check_results([str(tmp_path), "--expected", "1"]) == 0


def test_collect_only_and_execution_cannot_silently_select_different_ids(tmp_path):
    (tmp_path / "conftest.py").write_text(
        "def pytest_generate_tests(metafunc):\n"
        "    metafunc.parametrize('value', [1 if metafunc.config.option.collectonly else 2])\n"
    )
    (tmp_path / "test_case.py").write_text("def test_case(value):\n    pass\n")
    result = run(tmp_path)
    assert result.returncode != 0
    assert "independent --collect-only reference" in result.stdout + result.stderr


def test_registered_lane_can_omit_a_whole_module_and_records_it(tmp_path):
    (tmp_path / "conftest.py").write_text(LANE_HOOKS)
    (tmp_path / "test_hidden.py").write_text(
        "import pytest\npytestmark = pytest.mark.integration\ndef test_hidden():\n    assert False\n"
    )
    (tmp_path / "test_visible.py").write_text("def test_visible():\n    pass\n")
    result = run(tmp_path, "-m", "not integration")
    assert result.returncode == 0, result.stdout + result.stderr
    reference = json.loads((tmp_path / "result.reference.json").read_text())
    assert reference["ignored"] == {"test_hidden.py": "integration"}
    assert reference["selected"] == ["test_visible.py::test_visible"]


def test_a_focused_job_does_not_claim_unrequested_lane_modules(tmp_path):
    (tmp_path / "conftest.py").write_text(LANE_HOOKS)
    (tmp_path / "test_unrequested.py").write_text(
        "import pytest\npytestmark = pytest.mark.integration\ndef test_hidden():\n    assert False\n"
    )
    (tmp_path / "test_requested.py").write_text("def test_requested():\n    pass\n")
    result = run(tmp_path, "test_requested.py", "-m", "not integration")
    assert result.returncode == 0, result.stdout + result.stderr
    reference = json.loads((tmp_path / "result.reference.json").read_text())
    assert reference["ignored"] == {}
    assert reference["collected"] == ["test_requested.py::test_requested"]


@pytest.fixture
def valid_evidence(tmp_path):
    (tmp_path / "test_cases.py").write_text("def test_one():\n    pass\ndef test_two():\n    pass\n")
    result = run(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert validate(tmp_path / "result.xml")["testcases"] == 2
    return tmp_path


@pytest.mark.parametrize("corruption", ["header", "missing-case", "empty", "short-stream", "wrong-summary", "missing-report"])
def test_forged_or_truncated_result_is_red(valid_evidence, corruption):
    xml = valid_evidence / "result.xml"
    tree = ET.parse(xml)
    suite = tree.getroot().find("testsuite")
    if corruption == "header":
        suite.set("tests", "999")
    elif corruption == "missing-case":
        suite.remove(suite.findall("testcase")[-1])
    elif corruption == "empty":
        for case in list(suite):
            suite.remove(case)
    elif corruption == "wrong-summary":
        log = valid_evidence / "result.log"
        log.write_text(log.read_text().replace("2 passed", "200 passed"))
    else:
        path = valid_evidence / "result.jsonl"
        events = [json.loads(line) for line in path.read_text().splitlines()]
        if corruption == "short-stream":
            events.pop()
        else:
            events = [event for event in events if event.get("nodeid") != "test_cases.py::test_two"]
        path.write_text("".join(json.dumps(event) + "\n" for event in events))
    tree.write(xml, encoding="utf-8")
    # The previous workflow only required an XML file and summed its headers.
    assert sum(int(row.get("tests", "0")) for row in tree.getroot().iter("testsuite")) > 0
    with pytest.raises(ValueError):
        validate(xml)
    assert check_results([str(valid_evidence), "--expected", "1"]) == 1


def test_empty_or_missing_result_set_is_red(tmp_path):
    assert check_results([str(tmp_path), "--expected", "1"]) == 1
    (tmp_path / "empty.xml").write_text('<testsuites><testsuite tests="0"/></testsuites>')
    assert check_results([str(tmp_path), "--expected", "1"]) == 1


def test_killed_test_leaves_flushed_partial_reports(tmp_path):
    (tmp_path / "test_cases.py").write_text(
        "import os, signal\n"
        "def test_first():\n    pass\n"
        "def test_killed():\n    os.kill(os.getpid(), getattr(signal, 'SIGKILL', signal.SIGTERM))\n"
    )
    result = run(tmp_path)
    assert result.returncode != 0, result.stdout + result.stderr
    events = [json.loads(line) for line in (tmp_path / "result.jsonl").read_text().splitlines()]
    assert any(event.get("nodeid") == "test_cases.py::test_first" and event.get("when") == "teardown" for event in events)
    assert all(event.get("type") != "finish" for event in events)
    assert not (tmp_path / "result.xml").exists()
    assert check_results([str(tmp_path), "--expected", "1"]) == 1


def test_legal_subtest_and_teardown_outcomes_are_counted_without_trusting_headers(tmp_path):
    (tmp_path / "test_cases.py").write_text(
        "import pytest, unittest\n"
        "@pytest.fixture\ndef cleanup():\n    yield\n    assert False, 'cleanup'\n"
        "def test_pass():\n    pass\n"
        "def test_pass_cleanup(cleanup):\n    pass\n"
        "def test_fail_cleanup(cleanup):\n    assert False, 'call'\n"
        "class TestSubtests(unittest.TestCase):\n"
        "    def test_sub(self):\n"
        "        with self.subTest(value=1):\n            self.assertTrue(True)\n"
        "        with self.subTest(value=2):\n            self.assertTrue(False)\n"
    )
    result = run(tmp_path)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "Verified test evidence" in result.stdout, result.stdout + result.stderr
    verified = validate(tmp_path / "result.xml")
    assert verified["items"] == 4
    assert verified["testcases"] == 5
    assert verified["exitstatus"] == 1


def test_expected_failure_and_skip_reports_match_their_junit_cases(tmp_path):
    (tmp_path / "test_cases.py").write_text(
        "import pytest\n"
        "def test_pass():\n    pass\n"
        "@pytest.mark.xfail(strict=True, reason='fixture')\n"
        "def test_expected():\n    assert False\n"
        "@pytest.mark.skip(reason='fixture')\n"
        "def test_skipped():\n    assert False\n"
    )
    result = run(tmp_path, destination="outcome.fixture.xml")
    assert result.returncode == 0, result.stdout + result.stderr
    verified = validate(tmp_path / "outcome.fixture.xml")
    assert verified["items"] == verified["testcases"] == 3
    assert verified["summary"] == "1 passed, 1 skipped, 1 xfailed"
