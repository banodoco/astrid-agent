import json
from pathlib import Path

import pytest

from astrid.core.gateway import doctor_bundle
from scripts.export_journey_evidence import export


def test_export_redacts_sensitive_log_and_structured_json(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    (source / "install.log").write_text(
        'Authorization: Bearer bearer-secret\n'
        'fetch https://alice:url-password@example.test/path\n'
        '-----BEGIN PRIVATE KEY-----\nprivate-key-bytes\n-----END PRIVATE KEY-----\n'
        '/home/alice/private C:\\Users\\OtherPerson\\AppData\\Local\\Astrid\n'
        '{"credentials":{"key":"nested-key"},"access_token":"json-token"}\n'
        'installation failed\n', encoding="utf-8")
    (source / "receipt.json").write_text(json.dumps({"token": "receipt-secret", "state": "fail"}))
    (source / "skip.bin").write_bytes(b"private-binary")
    destination = tmp_path / "export"
    assert export(source, destination) == ["install.log", "receipt.json"]
    output = "\n".join(p.read_text() for p in destination.rglob("*") if p.is_file())
    for value in ("bearer-secret", "alice", "url-password", "private-key-bytes", "OtherPerson", "nested-key", "json-token", "receipt-secret", "private-binary"):
        assert value not in output
    assert "installation failed" in output
    assert json.loads((destination / "receipt.json").read_text())["state"] == "fail"


def test_export_bounds_log_tail_and_omits_oversize_json(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "install.log").write_text("a" * (doctor_bundle._MAX_LOG_BYTES + 20) + "\ntail-visible\n")
    (source / "large.json").write_text('{"secret":"' + 'x' * doctor_bundle._MAX_LOG_BYTES + '"}')
    destination = tmp_path / "export"
    assert export(source, destination) == ["install.log"]
    assert (destination / "install.log").read_text() == "tail-visible\n"
    assert not (destination / "large.json").exists()


def test_export_rejects_existing_destination_and_symlink(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "receipt.json").write_text('{}')
    destination = tmp_path / "destination"
    destination.mkdir()
    with pytest.raises(ValueError, match="must not already exist"):
        export(source, destination)
    alias = tmp_path / "alias"
    alias.symlink_to(source, target_is_directory=True)
    with pytest.raises(ValueError, match="non-symlink"):
        export(alias, tmp_path / "other")
