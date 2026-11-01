"""Focused controls for the bounded authentic release-0 -> release-1 leg."""
import argparse
from pathlib import Path

import pytest

from scripts import journey_version_jumps as jumps


def test_release0_fixture_is_the_approved_hashed_input_set():
    root, manifest, receipt = jumps.verified_release0_fixture(jumps.RELEASE0_FIXTURE)
    assert root == jumps.RELEASE0_FIXTURE.resolve()
    assert manifest == root / "served" / "install-manifest.json"
    assert receipt["fixture_sums_sha256"] == jumps.RELEASE0_SUMS_SHA256
    assert receipt["sources"] == jumps.RELEASE0_REFS
    assert set(receipt["bootstrap_sha256"]) == {"install", "install.ps1"}


def test_release0_bootstrap_uses_the_frozen_served_script_without_a_url_fallback(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(jumps.sys, "platform", "linux")
    monkeypatch.setattr(jumps.journey, "run", lambda argv, log, **kwargs: calls.append((argv, log, kwargs)))
    root, manifest, _receipt = jumps.verified_release0_fixture(jumps.RELEASE0_FIXTURE)
    program, data = tmp_path / "program", tmp_path / "data"
    jumps.bootstrap_release0(root, manifest, "sh", tmp_path / "evidence", program, data)
    argv, log, _kwargs = calls[0]
    assert argv == ["sh", root / "served" / "install", "--manifest", manifest,
                    "--root", program, "--data-root", data]
    assert log.name == "release0-bootstrap.log"


@pytest.mark.parametrize("changes", (
    {"mode": "pinokio-backend"},
    {"allow_release0_to_release1": False},
    {"source_override": True},
    {"release0_fixture": None},
))
def test_release0_admission_is_narrow_and_rejects_general_bypasses(changes):
    values = {"mode": "direct", "job_id": "direct-linux", "candidate": Path("candidate.json"),
              "allow_release0_to_release1": True, "release0_fixture": jumps.RELEASE0_FIXTURE,
              "source_override": False}
    values.update(changes)
    with pytest.raises(ValueError, match="release0-to-release1"):
        jumps.release0_admission(argparse.Namespace(**values))


def test_special_leg_lifts_only_its_windows_historical_refusal(monkeypatch, tmp_path):
    args = argparse.Namespace(leg="release0-to-release1")
    monkeypatch.setattr(jumps.sys, "platform", "win32")
    monkeypatch.setattr(jumps, "execute_release0_to_release1", lambda *unused: {"status": "special-windows-admitted"})
    assert jumps.execute_owned(args, tmp_path / "program", tmp_path / "data", tmp_path / "evidence",
                               argparse.Namespace(pid=1), "birth") == {"status": "special-windows-admitted"}


def test_semantic_snapshot_requires_every_seeded_data_class():
    before = {"project": "project", "timeline": "timeline", "preferences": "preferences",
              "task": "task", "cas_sha256": "cas"}
    after = {**before, "schema_version": 2, "target_schema_version": 2}
    jumps.assert_preserved(before, after)
    with pytest.raises(RuntimeError, match="seeded workspace task"):
        jumps.assert_preserved({key: value for key, value in before.items() if key != "task"}, after)


# These are synthetic filesystem controls for journal admission only. They do
# not exercise a native service, interruption, recovery, or produce evidence.
def _installed_transaction_fixture(tmp_path):
    import json

    root, data = tmp_path / "program", tmp_path / "data"
    data.mkdir()
    versions = root / "versions"
    versions.mkdir(parents=True)
    documents = {}
    for name in ("old", "candidate"):
        version = versions / name
        version.mkdir()
        (version / "manifest.json").write_text(json.dumps({"version": name}) + "\n", encoding="utf-8")
        documents[name] = {"root": str(version), "python": str(version / "py" / "bin" / "python"),
                           "profile": str(version / "profile.json"), "env": {}}
    journal = {"format": "astrid-install-update/v1", "operation_id": "a" * 32,
               "step": "switched", "transaction_kind": "migration", "data_root": str(data),
               **documents}
    path = root / "update-transaction.json"
    path.write_text(json.dumps(journal) + "\n", encoding="utf-8")
    return root, data, journal, path


def _write_journal(path, journal):
    import json
    path.write_text(json.dumps(journal) + "\n", encoding="utf-8")


def test_installed_transaction_observation_binds_journal_and_manifests(tmp_path):
    import hashlib

    root, data, journal, path = _installed_transaction_fixture(tmp_path)
    observed = jumps.observe_installed_transaction(root, data, expected_step="switched",
                                                   expected_operation_id="a" * 32)
    assert observed["journal"] == journal
    assert observed["journal_sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    for name in ("old", "candidate"):
        digest = hashlib.sha256((Path(journal[name]["root"]) / "manifest.json").read_bytes()).hexdigest()
        assert observed["manifest_sha256"][name] == digest


@pytest.mark.parametrize("case", [
    "missing_journal", "invalid_json", "wrong_format", "wrong_data", "wrong_step",
    "malformed_operation", "wrong_operation", "missing_old_manifest", "missing_candidate_manifest",
    "same_version", "outside_version", "nested_version", "relative_version", "symlink_version",
    "symlink_manifest", "symlink_journal",
])
def test_installed_transaction_observation_fails_closed(tmp_path, case):
    root, data, journal, path = _installed_transaction_fixture(tmp_path)
    expected_operation_id = "a" * 32
    if case == "missing_journal":
        path.unlink()
    elif case == "invalid_json":
        path.write_text("{", encoding="utf-8")
    elif case == "wrong_format":
        journal["format"] = "other"
    elif case == "wrong_data":
        journal["data_root"] = str(tmp_path / "other-data")
    elif case == "wrong_step":
        journal["step"] = "stopped"
    elif case == "malformed_operation":
        journal["operation_id"] = "not-a-durable-id"
    elif case == "wrong_operation":
        expected_operation_id = "b" * 32
    elif case.startswith("missing_") and case.endswith("_manifest"):
        name = case.removeprefix("missing_").removesuffix("_manifest")
        (Path(journal[name]["root"]) / "manifest.json").unlink()
    elif case == "same_version":
        journal["candidate"]["root"] = journal["old"]["root"]
    elif case == "outside_version":
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "manifest.json").write_text("{}", encoding="utf-8")
        journal["candidate"]["root"] = str(outside)
    elif case == "nested_version":
        nested = Path(journal["old"]["root"]) / "nested"
        nested.mkdir()
        (nested / "manifest.json").write_text("{}", encoding="utf-8")
        journal["candidate"]["root"] = str(nested)
    elif case == "relative_version":
        journal["candidate"]["root"] = "versions/candidate"
    elif case == "symlink_version":
        link = root / "versions" / "link"
        try:
            link.symlink_to(Path(journal["candidate"]["root"]), target_is_directory=True)
        except OSError:
            pytest.skip("symlink creation unavailable")
        journal["candidate"]["root"] = str(link)
    elif case == "symlink_manifest":
        manifest = Path(journal["candidate"]["root"]) / "manifest.json"
        manifest.unlink()
        try:
            manifest.symlink_to(Path(journal["old"]["root"]) / "manifest.json")
        except OSError:
            pytest.skip("symlink creation unavailable")
    elif case == "symlink_journal":
        alternate = root / "alternate.json"
        path.rename(alternate)
        try:
            path.symlink_to(alternate)
        except OSError:
            pytest.skip("symlink creation unavailable")
    if case not in {"missing_journal", "invalid_json", "symlink_journal"}:
        _write_journal(path, journal)
    with pytest.raises((ValueError, OSError)):
        jumps.observe_installed_transaction(root, data, expected_step="switched",
                                           expected_operation_id=expected_operation_id)


def test_installed_transaction_reobserves_same_operation_through_terminal_step(tmp_path):
    root, data, journal, path = _installed_transaction_fixture(tmp_path)
    journal["step"] = "staged"
    _write_journal(path, journal)
    staged = jumps.observe_installed_transaction(root, data, expected_step="staged")
    journal["step"] = "rolled_back"
    _write_journal(path, journal)
    terminal = jumps.observe_installed_transaction(root, data, expected_step="rolled_back",
                                                   expected_operation_id=staged["journal"]["operation_id"])
    assert terminal["journal"]["operation_id"] == staged["journal"]["operation_id"]
    assert terminal["journal_sha256"] != staged["journal_sha256"]
    assert terminal["manifest_sha256"] == staged["manifest_sha256"]
    journal["operation_id"] = "b" * 32
    _write_journal(path, journal)
    with pytest.raises(ValueError):
        jumps.observe_installed_transaction(root, data, expected_step="rolled_back",
                                           expected_operation_id=staged["journal"]["operation_id"])


def test_release1_transaction_journal_is_staged_byte_for_byte(tmp_path):
    from scripts import ci_journey as journey

    evidence = tmp_path / "evidence" / "release0-to-release1"
    journal = evidence / "release1" / "update-transaction.json"
    journal.parent.mkdir(parents=True)
    original = b'{ "operation_id": "aabb",\r\n  "step": "complete" }\n'
    journal.write_bytes(original)
    journey.retain_named_evidence(evidence, "before-cleanup")
    retained = evidence.parent / "evidence-upload" / "before-cleanup" / "release1" / journal.name
    assert retained.read_bytes() == original
