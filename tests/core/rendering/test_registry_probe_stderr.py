"""A registry probe that exits non-zero keeps its stderr in a private log the error names."""
import os
from types import SimpleNamespace

from astrid.core.rendering import remotion_runtime as rr


def test_failed_probe_stderr_is_kept_and_named(tmp_path, monkeypatch):
    monkeypatch.setattr(rr.tempfile, "gettempdir", lambda: str(tmp_path))
    error = rr.RemotionValidationError("owned validation lacks positive original wait; probe_check={}")
    rr._keep_probe_stderr(error, SimpleNamespace(stderr="Traceback ...\nImportError: boom\n"), phase="registry_discovery")
    assert "probe stderr kept in " in str(error)
    path = error.probe_stderr_path
    assert open(path).read().endswith("ImportError: boom\n")
    assert os.stat(path).st_mode & 0o077 == 0
    assert os.stat(os.path.dirname(path)).st_mode & 0o077 == 0


def test_no_stderr_leaves_the_error_unchanged(tmp_path, monkeypatch):
    monkeypatch.setattr(rr.tempfile, "gettempdir", lambda: str(tmp_path))
    error = rr.RemotionValidationError("unchanged")
    rr._keep_probe_stderr(error, SimpleNamespace(stderr=""), phase="registry_discovery")
    assert str(error) == "unchanged" and not hasattr(error, "probe_stderr_path")
