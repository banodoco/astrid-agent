"""FIX-17 mixed-version source contracts (offline, not a native qualification).

These tests cannot substitute for two separately dispatched hosted browsers
against the Lead's frozen combined tuple. They protect the exact row selection,
historical source, injected-refusal honesty and fail-closed receipt gate.
"""
from pathlib import Path

import pytest

from scripts import check_journey_evidence as gate
from scripts import journey_matrix as matrix
from scripts import journey_mixed_versions as mixed
from scripts import journey_version_jumps as jumps


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = (ROOT / ".github" / "workflows" / "journey.yml").read_text(encoding="utf-8")
BROWSER = (ROOT / "scripts" / "journey_mixed_version_browser.mjs").read_text(encoding="utf-8")


def test_mixed_row_registry_and_candidate_independent_exact_ids():
    expected = {
        mixed.JOB_NEW_APP_OLD: (mixed.NEW_APP_OLD_RUNTIME, "ubuntu-24.04"),
        mixed.JOB_OLD_APP_NEW: (mixed.OLD_APP_NEW_RUNTIME, "ubuntu-24.04"),
    }
    assert len(matrix.FIX17_MIXED_MATRIX) == 2
    assert set(row["job_id"] for row in matrix.FIX17_MIXED_MATRIX) == set(expected)
    for row in matrix.FIX17_MIXED_MATRIX:
        assert (row["leg"], row["runner"]) == expected[row["job_id"]]
        assert (row["mode"], row["os"], row["shell"]) == ("direct", "linux", "sh")
        assert matrix.validate_job(row["job_id"], "direct") == row
        assert row["job_id"] in matrix.FIX17_JOB_IDS
        assert row["job_id"] not in matrix.RELEASE1_JOB_IDS
    assert jumps.RELEASE13_RELEASE == 13
    assert jumps.RELEASE13_APP_COMMIT == gate.RELEASE13_APP_COMMIT
    assert "de7dd1e668aa6dcf3cbce013c701fa7c76615f99" == jumps.RELEASE13_APP_COMMIT


def test_unchanged_five_row_gate_and_all_required_fix17_checks():
    assert len(matrix.RELEASE1_MATRIX) == 5
    assert set(matrix.RELEASE1_JOB_IDS) == {
        "direct-macos", "direct-linux", "direct-windows",
        "direct-macos-fresh", "direct-linux-fresh",
    }
    assert len(matrix.FIX17_JOB_IDS) == len(set(matrix.FIX17_JOB_IDS))
    assert gate.MIXED_LEGS == {mixed.NEW_APP_OLD_RUNTIME, mixed.OLD_APP_NEW_RUNTIME}
    assert set(mixed.REQUIRED_CHECKS) == gate.MIXED_LEGS
    for leg, checks in mixed.REQUIRED_CHECKS.items():
        assert checks == gate.MIXED_CHECKS[leg]
        assert "canonical_seed" in checks and "unrelated_sentinel" in checks


def test_workflow_selects_two_named_rows_without_auto_dispatch():
    for row in matrix.FIX17_MIXED_MATRIX:
        assert f"- {row['job_id']}" in WORKFLOW
    assert "startsWith(matrix.leg, 'mixed-')" in WORKFLOW
    assert "working-directory: release13-app" in WORKFLOW
    assert "ref: de7dd1e668aa6dcf3cbce013c701fa7c76615f99" in WORKFLOW
    assert "--mixed-candidate-dist" in WORKFLOW
    assert "--mixed-release13-dist" in WORKFLOW
    assert "release13-app/dist" in WORKFLOW
    assert "journey_mixed_version_browser.mjs" in (
        ROOT / "scripts" / "journey_mixed_versions.py").read_text(encoding="utf-8")
    assert "RELEASE1_AUTOMATIC_MATRIX" in WORKFLOW


@pytest.mark.parametrize("leg,job", [
    (mixed.NEW_APP_OLD_RUNTIME, mixed.JOB_NEW_APP_OLD),
    (mixed.OLD_APP_NEW_RUNTIME, mixed.JOB_OLD_APP_NEW),
])
def test_missing_native_browser_receipt_cannot_pass(leg, job):
    row = matrix.validate_job(job, "direct")
    errors = gate._mixed_row_errors(job, row, {}, {"release": 14, "app": "a" * 40,
                                                   "agent": "b" * 40, "runtime": "c" * 40})
    assert errors
    assert any("canonical" in e for e in errors)
    assert any("installed" in e for e in errors)
    assert any("browser" in e for e in errors)
    assert any("mixed-version assertion did not pass" in e for e in errors)


def test_mixed_browser_does_not_replace_installed_runtime_or_claim_injected_rejection_as_real():
    for required in ("Real installed Runtime", "injected: true", "rejectedHttp",
                     "fix16Card", "outboxAfter", "r14-author-r14-installed",
                     "r13-app-r14-installed", "realInstalledEdit",
                     "Copy my change out", "Discard and reload the saved version",
                     "indexedDB"):
        assert required in BROWSER
    assert "page.route(EDIT_PATH" in BROWSER
    assert "status: 413" in BROWSER
    assert "caseR13ReadR14" in BROWSER
    assert "traffic.length === 0" in BROWSER
    assert "unsafeRetries" in BROWSER
    assert "chromiumArgs" in BROWSER
    assert "browserAppSha" in BROWSER


def test_mixed_source_rejects_foreground_and_any_implicit_new_tuple():
    source = (ROOT / "scripts" / "journey_mixed_versions.py").read_text(encoding="utf-8")
    for required in (
        "args.source_override", "journey.assert_direct_service_ownership",
        "jumps.release13_admission", "jumps.candidate_update",
        'manifest.get("release") != 14', "hosted.Intercept",
        "browser_app_sha", "native_end", "pinned_r13",
    ):
        assert required in source
    assert "c27fe76b79d917fd9c08246660bc77211be64146" not in source
    assert "release/14/int-59-64-65" not in source


def test_source_rejects_missing_and_mismatched_mixed_app_dist(tmp_path):
    candidate = tmp_path / "app"
    candidate.mkdir()
    dist = candidate / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<!doctype html>")
    (dist / "build.json").write_text("{}")
    with pytest.raises(RuntimeError, match="exact pinned"):
        mixed._dist(candidate, tmp_path / "other")
    foreign = tmp_path / "other"
    foreign.mkdir()
    with pytest.raises(RuntimeError, match="does not belong"):
        mixed._dist(candidate, foreign)
    assert mixed._dist(candidate, dist) == dist
    (dist / "build.json").unlink()
    with pytest.raises(RuntimeError, match="index and build"):
        mixed._dist(candidate, dist)


def test_mixed_browser_input_does_not_include_any_runtime_credential():
    source = (ROOT / "scripts" / "journey_mixed_versions.py").read_text(encoding="utf-8")
    assert '"browserAppSha"' in source
    assert '"spki"' in source
    assert '"projectId"' in source
    assert '"credential_file"' not in source
    assert '"authorization"' not in source
    assert "runner-owned" in (ROOT / "scripts" / "journey_mixed_version_browser.mjs").read_text()



def test_legacy_browser_must_read_the_original_r14_outbox_owner():
    browser = (ROOT / "scripts" / "journey_mixed_version_browser.mjs").read_text(encoding="utf-8")
    native = (ROOT / "scripts" / "journey_mixed_versions.py").read_text(encoding="utf-8")
    checker = (ROOT / "scripts" / "check_journey_evidence.py").read_text(encoding="utf-8")
    # Two fresh browser contexts would destroy the exact session being tested.
    assert "launchPersistentContext" in browser
    assert "config.profileDir" in browser
    assert "config.expectedOwnerIds" in browser
    assert "originalOwners.has(row.ownerId)" in browser
    assert '"profileDir": str(browser_profile)' in native
    assert '"expectedOwnerIds": original_owner_ids' in native
    assert "browser_profile_removed" in native
    assert "shutil.rmtree(browser_profile)" in native
    assert "original_queue_survives" in checker
    assert 'row.get("browser_profile_removed") is not True' in checker
    assert 'legacy.get("persistentProfile") is not True' in checker
