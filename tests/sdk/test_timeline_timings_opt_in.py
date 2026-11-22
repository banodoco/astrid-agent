from __future__ import annotations

import json

import pytest

from astrid.core.gateway import dispatch
from astrid.sdk.command_timing import stage


@pytest.mark.parametrize(
    ("args", "forwarded"),
    [
        (["timelines", "show", "--json"], ["timelines", "show", "--json"]),
        (["timelines", "show", "--json", "--timings"], ["timelines", "show", "--json"]),
        (["timelines", "check", "--at", "1", "--timings"], ["timelines", "check", "--at", "1", "--timings"]),
        (["timelines", "edit", "--verify", "--timings"], ["timelines", "edit", "--verify", "--timings"]),
    ],
)
def test_timeline_timings_are_opt_in_and_verification_receives_flag(monkeypatch, capsys, args, forwarded):
    def run(actual):
        assert actual == forwarded
        with stage("timeline work"):
            print(json.dumps({"ok": True, "data": {}}))
        return 0

    monkeypatch.setattr(dispatch, "_dispatch_product_inner", run)
    assert dispatch._dispatch_product(args) == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["ok"] is True
    if "--timings" in args:
        assert any(row["stage"] == "timeline work" for row in payload["timings"])
        assert "timeline work:" in captured.err
    else:
        assert "timings" not in payload
        assert captured.err == ""
