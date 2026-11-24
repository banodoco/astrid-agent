"""scripts/ci/agent_ceiling_compare.py: the Agent's ceiling comparison fails closed.

Each case builds a small log archive (one `<N>_<job>.txt` per job, GitHub timestamps on every line) and compares it
with a ceiling and a baseline pass list.
"""
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("agent_ceiling_compare", ROOT / "scripts/ci/agent_ceiling_compare.py")
cmp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cmp)

EXPECT = "unit:2,opt-in"
SPACED = "tests/t_params.py::test_map[z-image-{'prompt': 'a b'}-x - y]"
SUB = "tests/t_sub.py::TestS::test_each"
BASE_PASSED = {
    "unit shard 1 _ 2 (ubuntu-latest)": ["tests/a.py::test_one", "tests/a.py::test_two", SUB],
    "unit shard 2 _ 2 (ubuntu-latest)": ["tests/b.py::test_one"],
    "opt-in": ["tests/o.py::test_one"],
}
BASE_FAILED = {
    "unit shard 1 _ 2 (ubuntu-latest)": [(SPACED, "AssertionError: boom"), (f"{SUB} SUBFAILED(i=2)", "")],
    "unit shard 2 _ 2 (ubuntu-latest)": [("tests/b.py::test_red", "ValueError: red")],
    "opt-in": [],
}
INNER = (
    "=========== short test summary info ===========\n"
    "FAILED test_case.py::test_printed_by_a_test - inner run\n"
    "= 1 failed in 0.1s =\n"
)


def job_log(passed, failed, *, final=True, rows_drop=0, inner=False):
    """One job's log as pytest -rA prints it, with GitHub's timestamp on each line."""
    lines = ["tests/x.py::test_progress PASSED [  1%]"]
    if inner:
        lines += INNER.splitlines()
    lines += [f"{nid} [ 50%]" for nid, _ in failed if " SUBFAILED(" in nid]
    lines.append("=========== short test summary info ===========")
    lines += [f"PASSED {nid}" for nid in passed]
    summary_failed = [(n, m) for n, m in failed if " SUBFAILED(" not in n]
    rows = [f"FAILED {n}" + (f" - {m}" if m else "") for n, m in summary_failed]
    lines += rows[:len(rows) - rows_drop] if rows_drop else rows
    n_failed = len(failed)
    if final:
        lines.append(f"= {n_failed} failed, {len(passed)} passed, 5 deselected in 3.00s =" if (n_failed or passed)
                     else "= no tests ran in 0.01s =")
    lines.append("Post job cleanup.")
    return "\n".join(f"2026-11-24T08:00:{i % 60:02d}.1234567Z {line}" for i, line in enumerate(lines)) + "\n"


def write_archive(root, passed=None, failed=None, **per_job):
    """per_job maps a job name to kwargs for job_log; a job mapped to None is left out of the archive."""
    passed = BASE_PASSED if passed is None else passed
    failed = BASE_FAILED if failed is None else failed
    root.mkdir(parents=True, exist_ok=True)
    for index, name in enumerate(passed):
        if per_job.get(name, {}) is None:
            continue
        (root / f"{index}_{name}.txt").write_text(job_log(passed[name], failed.get(name, []), **per_job.get(name, {})))
    (root / "9_unit.txt").write_text("2026-11-24T08:00:00.1Z the aggregate job runs no tests\n")
    return root


def tsv(path, header, rows, comment="# a comment\n"):
    path.write_text(comment + "\t".join(header) + "\n" + "".join("\t".join(r) + "\n" for r in rows))
    return path


@pytest.fixture
def files(tmp_path):
    family = lambda job: "opt-in" if job == "opt-in" else "unit"  # noqa: E731
    ceiling = [(family(j), n) for j, ids in BASE_FAILED.items() for n, _ in ids]
    baseline = sorted((family(j), n) for j, ids in BASE_PASSED.items() for n in ids)
    return {
        "ceiling": tsv(tmp_path / "ceiling.tsv", ("job", "id"), sorted(ceiling)),
        "baseline": tsv(tmp_path / "baseline.tsv", ("job", "id"), baseline),
        "tmp": tmp_path,
    }


def run(files, archive, capsys, *extra):
    argv = [str(archive), "--ceiling", str(files["ceiling"]), "--baseline", str(files["baseline"]),
            "--expect-jobs", EXPECT, *extra]
    code = cmp.main(argv)
    return code, capsys.readouterr().out


def test_an_unchanged_tree_passes(files, capsys):
    code, out = run(files, write_archive(files["tmp"] / "cand"), capsys)
    assert code == 0, out
    assert "RESULT: PASS" in out and "new failures: 0" in out and "lost ids" in out
    assert "expected jobs: unit (2 shards), opt-in" in out


def test_inner_pytest_output_spaces_and_subtests_are_read_correctly(files, capsys):
    archive = write_archive(files["tmp"] / "cand", **{"unit shard 1 _ 2 (ubuntu-latest)": {"inner": True}})
    code, out = run(files, archive, capsys)
    assert code == 0, out  # test_printed_by_a_test is not counted; the spaced id and the SUBFAILED id are on the ceiling
    assert "test_printed_by_a_test" not in out


def test_one_passing_test_turning_red_fails(files, capsys):
    failed = {**BASE_FAILED, "opt-in": [("tests/o.py::test_one", "AssertionError: now red")]}
    passed = {**BASE_PASSED, "opt-in": ["tests/o.py::test_other"]}
    archive = write_archive(files["tmp"] / "cand", passed=passed, failed=failed)
    code, out = run(files, archive, capsys)
    assert code == 1
    assert "FAIL opt-in | tests/o.py::test_one | AssertionError: now red" in out


def test_a_new_failing_subtest_fails(files, capsys):
    failed = {**BASE_FAILED}
    failed["unit shard 1 _ 2 (ubuntu-latest)"] = BASE_FAILED["unit shard 1 _ 2 (ubuntu-latest)"] + [(f"{SUB} SUBFAILED(i=3)", "")]
    code, out = run(files, write_archive(files["tmp"] / "cand", failed=failed), capsys)
    assert code == 1 and f"{SUB} SUBFAILED(i=3)" in out


def test_a_lost_id_fails(files, capsys):
    passed = {**BASE_PASSED, "unit shard 2 _ 2 (ubuntu-latest)": ["tests/b.py::test_new_name"]}  # test_one is gone
    code, out = run(files, write_archive(files["tmp"] / "cand", passed=passed), capsys)
    assert code == 1
    assert "FAIL unit | tests/b.py::test_one" in out


def test_a_cancelled_shard_fails(files, capsys):
    archive = write_archive(files["tmp"] / "cand", **{"unit shard 2 _ 2 (ubuntu-latest)": {"final": False}})
    code, out = run(files, archive, capsys)
    assert code == 1
    assert "no final pytest result line" in out


def test_a_shard_missing_from_the_archive_fails(files, capsys):
    archive = write_archive(files["tmp"] / "cand", **{"unit shard 2 _ 2 (ubuntu-latest)": None})
    code, out = run(files, archive, capsys)
    assert code == 1
    assert "unit shard 2 of 2: absent from the archive" in out


def test_a_truncated_summary_fails(files, capsys):
    archive = write_archive(files["tmp"] / "cand", **{"unit shard 1 _ 2 (ubuntu-latest)": {"rows_drop": 1}})
    code, out = run(files, archive, capsys)
    assert code == 1
    assert "truncated or misread summary" in out


def test_zero_tests_fails(files, capsys):
    passed = {**BASE_PASSED, "opt-in": []}
    failed = {**BASE_FAILED, "opt-in": []}
    code, out = run(files, write_archive(files["tmp"] / "cand", passed=passed, failed=failed), capsys)
    assert code == 1
    assert "zero tests ran" in out


def test_a_grown_ceiling_fails_and_a_shrunk_one_does_not(files, capsys):
    archive = write_archive(files["tmp"] / "cand")
    base = files["ceiling"].read_text().splitlines()
    base_file = files["tmp"] / "base-ceiling.tsv"
    base_file.write_text("\n".join(base[:-1]) + "\n")  # the base lacks the last entry: the candidate's ceiling grew
    code, out = run(files, archive, capsys, "--base-ceiling", str(base_file))
    assert code == 1 and "ceiling entries added over the base: 1" in out
    base_file.write_text("\n".join(base) + "\nunit\ttests/gone.py::test_x\n")  # the candidate removed one: fine
    code, out = run(files, archive, capsys, "--base-ceiling", str(base_file))
    assert code == 0, out


def test_a_test_that_moves_between_shards_is_no_change(files, capsys):
    passed = {**BASE_PASSED}
    passed["unit shard 1 _ 2 (ubuntu-latest)"] = [i for i in BASE_PASSED["unit shard 1 _ 2 (ubuntu-latest)"] if i != "tests/a.py::test_two"]
    passed["unit shard 2 _ 2 (ubuntu-latest)"] = BASE_PASSED["unit shard 2 _ 2 (ubuntu-latest)"] + ["tests/a.py::test_two"]
    failed = {**BASE_FAILED}
    moved = BASE_FAILED["unit shard 1 _ 2 (ubuntu-latest)"][0]
    failed["unit shard 1 _ 2 (ubuntu-latest)"] = BASE_FAILED["unit shard 1 _ 2 (ubuntu-latest)"][1:]
    failed["unit shard 2 _ 2 (ubuntu-latest)"] = BASE_FAILED["unit shard 2 _ 2 (ubuntu-latest)"] + [moved]
    code, out = run(files, write_archive(files["tmp"] / "cand", passed=passed, failed=failed), capsys)
    assert code == 0, out


def test_ratchet_candidates_are_reported_and_written_but_do_not_fail(files, capsys):
    failed = {**BASE_FAILED, "unit shard 2 _ 2 (ubuntu-latest)": []}
    passed = {**BASE_PASSED}
    passed["unit shard 2 _ 2 (ubuntu-latest)"] = BASE_PASSED["unit shard 2 _ 2 (ubuntu-latest)"] + ["tests/b.py::test_red"]
    out_dir = files["tmp"] / "out"
    code, out = run(files, write_archive(files["tmp"] / "cand", passed=passed, failed=failed), capsys, "--out", str(out_dir))
    assert code == 0, out
    assert "ratchet candidates (on the ceiling, now passing): 1" in out
    assert (out_dir / "RATCHET.tsv").read_text().splitlines() == ["job\tid", "unit\ttests/b.py::test_red"]


def moving_file(files):
    return tsv(files["tmp"] / "moving.tsv", ("group", "id pattern", "message substring"),
               [("flaky", "tests/m.py::*", "listener owner")], comment="# moving\n")


def test_a_moving_group_member_with_its_message_is_moving_not_new(files, capsys):
    failed = {**BASE_FAILED, "opt-in": [("tests/m.py::test_member", "AdapterError: not the listener owner")]}
    code, out = run(files, write_archive(files["tmp"] / "cand", failed=failed), capsys, "--moving", str(moving_file(files)))
    assert code == 0, out
    assert "moving opt-in | tests/m.py::test_member | moving group flaky" in out


def test_a_moving_group_member_with_another_message_is_new(files, capsys):
    failed = {**BASE_FAILED, "opt-in": [("tests/m.py::test_member", "KeyError: something else")]}
    code, out = run(files, write_archive(files["tmp"] / "cand", failed=failed), capsys, "--moving", str(moving_file(files)))
    assert code == 1
    assert "member of moving group flaky failed with another message" in out


def test_unreadable_inputs_exit_2(files, capsys):
    assert cmp.main([str(files["tmp"] / "nope"), "--ceiling", str(files["ceiling"])]) == 2
    capsys.readouterr()


def test_write_passed_lists_only_ids_that_passed_and_did_not_fail(files, capsys):
    archive = write_archive(files["tmp"] / "cand")
    target = files["tmp"] / "passed.tsv"
    assert cmp.main([str(archive), "--ceiling", str(files["ceiling"]), "--expect-jobs", EXPECT, "--write-passed", str(target)]) == 0
    rows = target.read_text().splitlines()
    assert rows[0] == "job\tid" and "unit\ttests/a.py::test_one" in rows and "opt-in\ttests/o.py::test_one" in rows


def test_a_passing_test_turned_xfail_fails(files, capsys):
    archive = write_archive(files["tmp"] / "cand")
    log = next(archive.glob("*_opt-in.txt"))
    log.write_text(log.read_text().replace("PASSED tests/o.py::test_one", "XFAIL tests/o.py::test_one - hidden"))
    code, out = run(files, archive, capsys)
    assert code == 1, out
    assert "tests/o.py::test_one | passed on the baseline, now XFAIL" in out


def test_a_deselected_or_skipped_test_is_a_lost_id(files, capsys):
    archive = write_archive(files["tmp"] / "cand")
    log = next(archive.glob("*_opt-in.txt"))
    log.write_text(log.read_text().replace("PASSED tests/o.py::test_one", "SKIPPED [1] tests/o.py:3: skipped"))
    code, out = run(files, archive, capsys)
    assert code == 1, out
    assert "FAIL opt-in | tests/o.py::test_one" in out


def _stamp(text):
    return "".join(f"2026-11-24T09:00:{i % 60:02d}.1Z {line}\n" for i, line in enumerate(text.splitlines()))


def _session(rows, final):
    return ("============================= test session starts ==============================\n"
            "tests/x.py::test_progress PASSED\n"
            "=========================== short test summary info ============================\n"
            + "".join(r + "\n" for r in rows) + final + "\n")


def _two_run_files(files, first_run_failure):
    """Baseline: a::one, a::two, b::three pass in one opt-in job. Candidate: the same job runs pytest twice."""
    tsv(files["baseline"], ("job", "id"), [("opt-in", "tests/a.py::test_one"), ("opt-in", "tests/a.py::test_two"),
                                           ("opt-in", "tests/b.py::test_three")])
    tsv(files["ceiling"], ("job", "id"), [])
    run1 = ["PASSED tests/a.py::test_one",
            "FAILED tests/a.py::test_two - boom" if first_run_failure else "PASSED tests/a.py::test_two"]
    final1 = "= 1 failed, 1 passed in 0.10s =" if first_run_failure else "= 2 passed in 0.10s ="
    return run1, final1


def test_a_failure_in_the_first_of_two_sequential_runs_counts(files, capsys):
    # The Release 16 Lead's probe: run 1 (PASSED a::one, FAILED a::two), then run 2 (PASSED b::three).
    run1, final1 = _two_run_files(files, first_run_failure=True)
    archive = files["tmp"] / "cand"
    archive.mkdir()
    log = _session(run1, final1) + _session(["PASSED tests/b.py::test_three"], "= 1 passed in 0.10s =")
    (archive / "0_opt-in.txt").write_text(_stamp(log))
    code, out = run(files, archive, capsys, "--expect-jobs", "opt-in")
    assert code == 1, out
    assert "FAIL opt-in | tests/a.py::test_two | boom" in out
    assert "1 failed, 0 errors, 2 passed" in out  # both runs' result lines are summed


def test_two_github_pytest_steps_are_both_read(files, capsys):
    run1, final1 = _two_run_files(files, first_run_failure=True)
    archive = files["tmp"] / "cand"
    archive.mkdir()
    log = ("##[group]Run python -m pytest -q -m first\n" + _session(run1, final1).split("\n", 1)[1]
           + "##[group]Run python -m pytest -q -m second\n"
           + _session(["PASSED tests/b.py::test_three"], "= 1 passed in 0.10s =").split("\n", 1)[1])
    (archive / "0_opt-in.txt").write_text(_stamp(log))
    code, out = run(files, archive, capsys, "--expect-jobs", "opt-in")
    assert code == 1, out
    assert "FAIL opt-in | tests/a.py::test_two | boom" in out


def test_a_failure_in_an_early_repeat_counts(files, capsys):
    run1, final1 = _two_run_files(files, first_run_failure=True)
    archive = files["tmp"] / "cand"
    archive.mkdir()
    ok = ["PASSED tests/a.py::test_one", "PASSED tests/a.py::test_two", "PASSED tests/b.py::test_three"]
    log = ("##[group]Run set +e\n"
           "===== selection 1 of 2: tests='tests/a.py' =====\n" + _session(run1 + ["PASSED tests/b.py::test_three"], final1.replace("1 passed", "2 passed"))
           + "===== selection 2 of 2: tests='tests/a.py' =====\n" + _session(ok, "= 3 passed in 0.10s ="))
    (archive / "0_opt-in.txt").write_text(_stamp(log))
    code, out = run(files, archive, capsys, "--expect-jobs", "opt-in")
    assert code == 1, out
    assert "FAIL opt-in | tests/a.py::test_two | boom" in out


def test_an_inner_run_with_its_own_session_header_is_not_a_run(files, capsys):
    run1, final1 = _two_run_files(files, first_run_failure=False)
    archive = files["tmp"] / "cand"
    archive.mkdir()
    inner = _session(["FAILED tests/inner.py::test_printed - inner"], "= 1 failed in 0.01s =")
    log = ("============================= test session starts ==============================\n"
           + inner + "=========================== short test summary info ============================\n"
           "PASSED tests/a.py::test_one\nPASSED tests/a.py::test_two\nPASSED tests/b.py::test_three\n"
           "= 3 passed in 0.30s =\n")
    (archive / "0_opt-in.txt").write_text(_stamp(log))
    code, out = run(files, archive, capsys, "--expect-jobs", "opt-in")
    assert code == 0, out
    assert "test_printed" not in out


def test_a_session_that_never_ends_or_a_github_cancel_fails(files, capsys):
    run1, final1 = _two_run_files(files, first_run_failure=False)
    for name, tail in (("open", "============================= test session starts ==============================\nPASSED tests/z.py::test_cut\n"),
                       ("cancel", "##[error]The operation was canceled.\n")):
        archive = files["tmp"] / name
        archive.mkdir()
        log = _session(run1 + ["PASSED tests/b.py::test_three"], "= 3 passed in 0.10s =") + tail
        (archive / "0_opt-in.txt").write_text(_stamp(log))
        code, out = run(files, archive, capsys, "--expect-jobs", "opt-in")
        assert code == 1, (name, out)
        assert "truncated" in out or "cancelled" in out, out
