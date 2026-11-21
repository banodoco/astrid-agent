"""R14-FIX-17 offline checks for exact R13 source, row routing and fail-closed save receipts.

These do NOT qualify installed R13->R14 updates. Native matrix results must follow FIX-15.
"""
import json

import pytest

from scripts import check_journey_evidence as gate
from scripts import journey_live_workspace as live
from scripts import journey_matrix as matrix
from scripts import journey_version_jumps as jumps
from scripts import journey_windows_r13_update as windows


APP = "de7dd1e668aa6dcf3cbce013c701fa7c76615f99"
AGENT = "9814193a73f606719e81883a8be197d27dd361d4"
RUNTIME = "6855dfa8df65cf8ae4c002b57134d6505493c17c"
SCHEMA = "sha256:9628bb5fd6e1dbbec392dd39f2e01031622dcf52d7ccff7cd1f0ad10743dcb1c"


def test_r13_pin_is_exactly_the_published_previous_release():
    assert jumps.RELEASE13_APP_COMMIT == gate.RELEASE13_APP_COMMIT == APP
    assert jumps.RELEASE13_RELEASE == gate.RELEASE13_RELEASE == 13
    expected = {"agent": AGENT, "runtime": RUNTIME, "schema_digest": SCHEMA}
    assert jumps.RELEASE13_IDENTITY == gate.RELEASE13_IDENTITY == expected


def test_three_direct_previous_release_rows_are_native_and_separate():
    expected = {
        "direct-linux-r13-update": ("linux", "ubuntu-24.04", "sh"),
        "direct-macos-r13-update": ("macos", "macos-15", "sh"),
        "direct-windows-r13-update": ("windows", "windows-2022", "powershell.exe"),
    }
    assert {row["job_id"] for row in matrix.RELEASE13_UPDATE_MATRIX} == set(expected)
    for row in matrix.RELEASE13_UPDATE_MATRIX:
        assert row["mode"] == "direct"
        assert row["leg"] == "release13-then-update"
        assert (row["os"], row["runner"], row["shell"]) == expected[row["job_id"]]
        assert matrix.validate_job(row["job_id"], "direct") == row


def test_evidence_gate_requires_both_save_and_reopen():
    for checks in (gate.RELEASE13_CHECKS, gate.RELEASE13_WINDOWS_CHECKS, windows.CHECKS):
        assert "timeline_saved" in checks
        assert "timeline_reopened" in checks
    errors = gate._release13_row_errors("direct-linux-r13-update", {}, 14, "linux")
    assert any("timeline edit/save evidence" in line for line in errors)
    assert any("timeline reopen" in line for line in errors)


def test_windows_step_sequence_keeps_original_assertions_and_adds_save():
    names = [name for name, _ in windows.STEPS]
    assert names[:9] == [
        "admission", "r13_bootstrap", "r13_start", "r13_seed",
        "update", "release_check", "doctor", "dry_run", "data_check",
    ]
    assert names[-2:] == ["save_timeline", "reopen_timeline"]
    for original in ("release13_install", "update_exit", "release", "one_runtime",
                     "service", "health", "doctor", "dry_run", "data_marker", "unrelated_sentinel"):
        assert original in windows.CHECKS


class FakeTimeline:
    def __init__(self):
        self.row = {"name": "User edit release13", "head_revision_id": None}
        self.calls = []

    def request(self, method, path, body=None, *, key=None):
        self.calls.append((method, path, body, key))
        if method == "GET":
            return dict(self.row)
        if method == "PATCH":
            assert key == "fix17-save-release13"
            self.row["name"] = body["name"]
            return {"data": dict(self.row)}
        raise AssertionError(f"unexpected request: {method}")


def test_save_uses_real_patch_and_a_separate_reopen_observation():
    client = FakeTimeline()
    keys = {"project": "p", "timeline": "t", "head": None, "generation": "release13"}
    saved = live.save_timeline_after_update(client, keys)
    assert saved["saved"] is True
    assert saved["saved_name"] == "Saved after update release13"
    assert len([c for c in client.calls if c[0] == "PATCH"]) == 1
    # Another LiveWorkspace client process would use the same persisted Runtime.
    reopened = live.reopen_saved_timeline(client, saved)
    assert reopened["reopened"] is True
    assert reopened["saved_name"] == saved["saved_name"]
    assert client.calls[-1][0] == "GET"


def test_save_and_reopen_do_not_accept_stale_or_missing_data():
    client = FakeTimeline()
    keys = {"project": "p", "timeline": "t", "head": None, "generation": "release13"}
    client.row["name"] = "foreign edit"
    with pytest.raises(ValueError, match="not the seeded timeline"):
        live.save_timeline_after_update(client, keys)
    client.row["name"] = "wrong after save"
    with pytest.raises(ValueError, match="lost the saved edit"):
        live.reopen_saved_timeline(client, {
            "project": "p", "timeline": "t", "head": None, "saved_name": "saved",
        })


def test_release13_source_resolver_uses_pinned_app(monkeypatch, tmp_path):
    def resolve(release, output, *, app_repo):
        assert release == "release13"
        assert app_repo == tmp_path.resolve()
        output.mkdir()
        (output / "install-manifest.json").write_text(json.dumps({"release": 13}))
        return {"status": "verified", "app_manifest_commit": APP,
                "identity": jumps.RELEASE13_IDENTITY, "manifest_sha256": "a" * 64,
                "bootstrap": [{"path": "public/install", "sha256": "b" * 64}]}
    monkeypatch.setattr(jumps.upgrade_release, "resolve", resolve)
    _, manifest, receipt = jumps.verified_release13_source(tmp_path, tmp_path / "source")
    assert json.loads(manifest.read_text())["release"] == 13
    assert receipt["app_commit"] == APP
    assert receipt["identity"] == jumps.RELEASE13_IDENTITY
