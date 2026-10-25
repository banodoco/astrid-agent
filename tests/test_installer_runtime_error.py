from __future__ import annotations

import json

from astrid.installer.backend import _last_logged_error


def test_last_logged_runtime_error_is_found_in_pretty_json(tmp_path):
    log = tmp_path / "runtime-service.log"
    first = {"error": "older failure", "ok": False}
    last = {"error": "[Errno 13] Permission denied: '/'", "log": "/x/runtime.log", "ok": False}
    log.write_text("starting\n" + json.dumps(first, indent=2) + "\nnoise {not json}\n" + json.dumps(last, indent=2) + "\n")
    assert _last_logged_error(log) == "[Errno 13] Permission denied: '/'"


def test_no_logged_error_means_no_cause(tmp_path):
    log = tmp_path / "runtime-service.log"
    log.write_text(json.dumps({"ok": True, "status": "started"}) + "\n")
    assert _last_logged_error(log) is None
    assert _last_logged_error(tmp_path / "missing.log") is None
