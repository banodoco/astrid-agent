"""Installer failures keep the shared, actionable TLS error at the CLI boundary."""

import ssl
import urllib.error

import pytest

from astrid.installer import cli
from astrid.installer.support import FAILURE_MESSAGE


@pytest.mark.parametrize("wrapped", [False, True], ids=["direct", "urlerror"])
def test_installer_renders_the_shared_tls_error(tmp_path, monkeypatch, capsys, wrapped):
    from runtime_protocol import platform

    error = ssl.SSLCertVerificationError(1, "CERTIFICATE_VERIFY_FAILED")
    if wrapped:
        error = urllib.error.URLError(error)

    def cannot_connect(*_args, **_kwargs):
        raise error

    monkeypatch.setattr(platform, "log_dir", lambda: tmp_path / "logs")
    monkeypatch.setattr(cli, "Backend", cannot_connect)

    assert cli.main(["--root", str(tmp_path / "program")]) == 1

    output = capsys.readouterr()
    assert FAILURE_MESSAGE in output.out
    assert "code: tls_verification_failed" in output.err
    assert "antivirus or a company proxy" in output.err
    assert "fix:" in output.err
    assert "unstructured - this is a bug" not in output.err
    assert "Setup did not finish" in output.out
