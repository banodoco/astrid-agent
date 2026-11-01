"""Combined front-door and delegated readback custody guards."""
import pytest

import astrid
from tests.sdk.test_child_bridge_d18 import isolated, channel, call, write_pack


@pytest.mark.parametrize("fault", ["foreign_status", "publication", "invalid_state", "malformed_state",
                                    "foreign_outputs", "timeout_foreign", "timeout_state"])
def test_child_status_and_publication_validators_both_apply(tmp_path, isolated, channel, monkeypatch, fault):
    from astrid.sdk import invocation, _child_bridge

    root = write_pack(tmp_path)
    bridge, messages, terminal = channel
    monkeypatch.setattr(invocation, "_kernel_invoke", lambda *a, **kw: pytest.fail("standalone fallback"))
    if fault == "foreign_status":
        terminal["task_id"] = "T-foreign"
    elif fault == "publication":
        terminal["publication_readback"] = {}
    elif fault in {"invalid_state", "malformed_state"}:
        terminal["state"] = "invented" if fault == "invalid_state" else []
    elif fault == "foreign_outputs":
        monkeypatch.setattr(bridge, "outputs", lambda *a, **kw: [{"task_id": "foreign"}])
    else:
        def late_status(child_key, task_id, **kwargs):
            data = {"task_id": task_id, "run_id": "R-" + child_key, "state": "completed"}
            data.update({"task_id": "foreign"} if fault == "timeout_foreign" else {"state": []})
            now = invocation.time.monotonic() + 100
            raise _child_bridge._BridgeTimeout("late status", observed_at=now) from (
                _child_bridge._BridgeReplyTimeout(data, observed_at=now))
        monkeypatch.setattr(bridge, "status", late_status)
    expected = {"foreign_status": "foreign task", "publication": "publication readback",
                "invalid_state": "invalid task state", "malformed_state": "invalid task state",
                "foreign_outputs": "foreign or invalid outputs", "timeout_foreign": "foreign task",
                "timeout_state": "invalid task state"}[fault]
    if fault == "foreign_outputs":
        result = call(root, inputs={"message": "hello"}, wait=True, stage="prepare", input_refs=[])
        assert not result.ok
        assert result.error["code"] == "managed_output_readback_unavailable"
        assert result.error["sdk_category"] == "runtime"
    else:
        with pytest.raises(astrid.CapabilityInvocationError, match=expected):
            call(root, inputs={"message": "hello"}, wait=True, stage="prepare", input_refs=[])
    assert [item["op"] for item in messages][:2] == (["submit"] if fault.startswith("timeout") else ["submit", "status"])
    assert bridge._closed


@pytest.mark.parametrize("identifier", ["h3_av.transform"])
def test_generation_and_render_guards_preserve_capability_rules(tmp_path, isolated, monkeypatch, identifier):
    from astrid.sdk import invocation
    root = write_pack(tmp_path, pack_id="h3_av", actions={"transform": {
        "description": "H3 fixture", "invocation": {"kind": "python", "path": "actions/echo.py", "function": "echo"},
        "inputs": [{"name": "host_output_root", "type": "string"}], "metadata": {"project_scope": "optional"},
    }})
    monkeypatch.setattr(invocation, "_kernel_invoke", lambda *a, **kw: pytest.fail("mutation before guard"))
    with pytest.raises(astrid.CapabilityValidationError, match="host.*output|host-owned"):
        astrid.invoke(identifier, kind="action", extra_pack_roots=(str(root),),
                      inputs={"host_output_root": str(tmp_path / "forged")})
