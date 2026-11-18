"""Download progress keeps the one shared TLS explanation."""

import ssl

from astrid.core.contracts import errors
from astrid.core.generation import asset_gaps


def test_download_failure_uses_the_central_tls_explanation(tmp_path, monkeypatch):
    failure = ssl.SSLCertVerificationError("certificate verification failed")
    expected = errors.AstridError("Shared certificate guidance", code=errors.TLS_VERIFICATION_CODE)
    seen = []

    def explain(error):
        seen.append(error)
        return expected

    monkeypatch.setattr(errors, "coerce_astrid_error", explain)
    item = {"file": "model.bin", "folder": str(tmp_path)}
    stopped = asset_gaps._stopped({"files": [item]}, [], item, failure)

    assert seen == [failure]
    assert stopped.code == errors.TLS_VERIFICATION_CODE
    assert expected.cause in str(stopped)
    assert "Finished in this run: none" in str(stopped)
    assert "Run the same command again" in str(stopped)
