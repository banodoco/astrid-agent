"""Combined front-door and delegated readback custody guards."""
import pytest

import astrid
from tests.sdk.test_child_bridge_d18 import isolated, channel, call, write_pack


@pytest.mark.parametrize("fault", ["foreign_status", "publication"])
def test_child_status_and_publication_validators_both_apply(tmp_path, isolated, channel, fault):
    root = write_pack(tmp_path)
    bridge, messages, terminal = channel
    if fault == "foreign_status":
        terminal["task_id"] = "T-foreign"
    else:
        terminal["publication_readback"] = {}
    with pytest.raises(astrid.CapabilityInvocationError,
                       match="foreign task|publication readback"):
        call(root, inputs={"message": "hello"}, wait=True, stage="prepare", input_refs=[])
    assert [item["op"] for item in messages][:2] == ["submit", "status"]
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
