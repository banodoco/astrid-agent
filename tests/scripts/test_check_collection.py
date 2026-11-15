"""Fixture-based tests for the collection gate; no pytest subprocess is started."""

from types import SimpleNamespace

from scripts import check_collection as gate

OUTPUT = """\
________________ ERROR collecting tests/test_old.py ________________
E   ImportError: missing checkout
________________ ERROR collecting tests/test_new.py ________________
E   ImportError: bad symbol
================ short test summary info ================
ERROR tests/test_old.py - ImportError: missing checkout
ERROR tests/test_new.py - ImportError: bad symbol
12 tests collected, 2 errors in 1.0s
"""


def test_parse_headings_and_summary():
    errors = gate.parse_errors(OUTPUT + "ERROR tests/test_other.py::TestThing - broken\n")
    assert set(errors) == {"tests/test_old.py", "tests/test_new.py", "tests/test_other.py::TestThing"}
    assert "missing checkout" in errors["tests/test_old.py"][0]
    assert gate.parse_errors("ERROR collecting tests/test_plain.py\nE   broken\n") == {
        "tests/test_plain.py": ["E   broken"]
    }


def test_new_error_and_changed_cause():
    known = [("tests/test_old.py", "missing checkout")]
    status, messages = gate.evaluate(OUTPUT, 1, known)
    assert status == 1
    assert any("NEW collection error: tests/test_new.py" in message for message in messages)
    status, messages = gate.evaluate(OUTPUT, 1, [("tests/test_old.py", "different cause")])
    assert status == 1
    assert any("NEW collection error: tests/test_old.py" in message for message in messages)


def test_stale_entry_strict_only():
    known = [("tests/test_old.py", "missing checkout"), ("tests/test_new.py", "bad symbol"),
             ("tests/gone.py", "missing")]
    status, messages = gate.evaluate(OUTPUT, 1, known)
    assert status == 0 and any("stale" in message for message in messages)
    status, messages = gate.evaluate(OUTPUT, 1, known, strict=True)
    assert status == 1 and any("STALE" in message for message in messages)


def test_exit_codes_and_crash():
    assert gate.evaluate("2 tests collected in 0.1s", 0, [])[0] == 0
    assert gate.evaluate("no tests collected in 0.1s", 5, [])[0] == 1
    assert gate.evaluate("0 tests collected in 0.1s", 0, [])[0] == 1
    assert gate.evaluate("2 tests collected in 0.1s", 3, [])[0] == 1
    assert gate.evaluate("2 tests collected in 0.1s", 1, [])[0] == 1
    assert gate.evaluate(OUTPUT, 0, [])[0] == 1


def test_main_returns_gate_exit_codes(monkeypatch, tmp_path):
    known_file = tmp_path / "known.txt"
    known_file.write_text("tests/test_old.py::missing checkout\n", encoding="utf-8")
    monkeypatch.setattr(gate, "KNOWN", known_file)
    monkeypatch.setattr(gate.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(stdout=OUTPUT, returncode=1))
    assert gate.main([]) == 1
    known_file.write_text("tests/test_old.py::missing checkout\ntests/test_new.py::bad symbol\n", encoding="utf-8")
    assert gate.main([]) == 0


def test_distinct_errors_with_same_id_are_not_hidden():
    output = ("ERROR tests/test_old.py - missing checkout\n"
              "ERROR tests/test_old.py - different failure\n"
              "2 tests collected, 2 errors in 0.1s\n")
    status, messages = gate.evaluate(output, 1, [("tests/test_old.py", "missing checkout")])
    assert status == 1
    assert any("different failure" in message for message in messages)


def test_extra_summary_for_known_heading_is_detected():
    output = ("___ ERROR collecting tests/test_old.py ___\nE   missing checkout\n"
              "=== short test summary info ===\n"
              "ERROR tests/test_old.py - missing checkout\n"
              "ERROR tests/test_old.py - different failure\n"
              "2 tests collected, 2 errors in 0.1s\n")
    status, messages = gate.evaluate(output, 1, [("tests/test_old.py", "missing checkout")])
    assert status == 1
    assert any("different failure" in message for message in messages)
