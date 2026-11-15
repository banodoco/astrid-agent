"""Offline controls for the pinned Release 12 update row."""
import argparse
import json
from pathlib import Path

import pytest

from scripts import journey_matrix, journey_version_jumps as jumps


def test_release12_source_and_matrix_are_pinned(monkeypatch, tmp_path):
    def resolve(release, output, *, app_repo):
        assert release == "release12" and app_repo == tmp_path.resolve()
        output.mkdir()
        (output / "install-manifest.json").write_text(json.dumps({"release": 12}))
        return {"status": "verified", "app_manifest_commit": jumps.RELEASE12_APP_COMMIT,
                "identity": jumps.RELEASE12_IDENTITY, "manifest_sha256": "a" * 64,
                "bootstrap": [{"path": "public/install", "sha256": "b" * 64}]}
    monkeypatch.setattr(jumps.upgrade_release, "resolve", resolve)
    _, manifest, receipt = jumps.verified_release12_source(tmp_path, tmp_path / "source")
    assert json.loads(manifest.read_text())["release"] == 12
    assert receipt["app_commit"] == jumps.RELEASE12_APP_COMMIT
    assert receipt["identity"] == jumps.RELEASE12_IDENTITY
    assert {row["job_id"] for row in journey_matrix.RELEASE12_UPDATE_MATRIX} == {
        "direct-linux-r12-update", "direct-macos-r12-update"}
    for row in journey_matrix.RELEASE12_UPDATE_MATRIX:
        assert journey_matrix.validate_job(row["job_id"], "direct")["leg"] == "release12-then-update"


def test_release12_admission_rejects_missing_scope(tmp_path):
    args = argparse.Namespace(mode='direct', job_id='direct-linux-r12-update', candidate=tmp_path,
                              release12_app_checkout=tmp_path, allow_release12_to_candidate=False)
    with pytest.raises(ValueError, match='pinned Release-12'):
        jumps.release12_admission(args, tmp_path / 'source')
