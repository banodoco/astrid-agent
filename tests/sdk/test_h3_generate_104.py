from types import SimpleNamespace

import pytest

from astrid.sdk.generation import GenerationFacade
from astrid.sdk.exceptions import CapabilityValidationError


@pytest.mark.parametrize("selector", ["entry", "model"])
def test_h3_public_selector_passes_local_files_and_controls_to_one_action(monkeypatch, selector):
    import astrid.sdk as sdk
    from astrid.core.generation import entries
    description = {"id": "h3", "kind": "video", "source": "action", "action": "h3_av.transform",
                   "mode": "transform", "modes": ["transform"], "inputs": [
                       {"name": "request", "type": "file", "required": True, "file_custody": "producer_local"},
                       {"name": "asset_map", "type": "file", "required": True, "file_custody": "producer_local"},
                       {"name": "execution_request", "type": "file", "required": False},
                       {"name": "resume", "type": "boolean", "required": False},
                       {"name": "dry_run", "type": "boolean", "required": False}]}
    monkeypatch.setattr(entries, "describe_entry", lambda *a, **kw: description)
    monkeypatch.setattr(sdk, "_load_model_registry", lambda **kwargs: object())
    calls = []
    client = SimpleNamespace(invoke_result=lambda *a, **kw: calls.append((a, kw)) or "result")
    assert GenerationFacade().video(
        **{selector: "h3"}, request="request.json", asset_map="assets.json",
        execution_request={"target": {"kind": "default"}}, resume=True, dry_run=True,
        project="p", client=client,
    ) == "result"
    assert len(calls) == 1 and calls[0][0] == ("h3_av.transform",)
    assert calls[0][1]["inputs"] == {
        "request": "request.json", "asset_map": "assets.json",
        "execution_request": {"target": {"kind": "default"}},
    }
    assert calls[0][1]["project"] == "p"
    assert calls[0][1]["resume"] is True and calls[0][1]["dry_run"] is True
    assert calls[0][1]["wait"] is True and calls[0][1]["read_managed_outputs"] is True
    with pytest.raises(CapabilityValidationError):
        GenerationFacade().video(**{selector: "h3"}, request="request.json", client=client)
    assert len(calls) == 1


def test_h3_attached_target_is_refused_before_invocation(monkeypatch):
    import astrid.sdk as sdk
    from astrid.core.generation import entries

    description = {"id": "h3", "kind": "video", "source": "action", "action": "h3_av.transform",
                   "mode": "transform", "modes": ["transform"], "inputs": [
                       {"name": "request", "type": "file", "required": True, "file_custody": "producer_local"},
                       {"name": "asset_map", "type": "file", "required": True, "file_custody": "producer_local"},
                       {"name": "execution_request", "type": "file", "required": False},
                       {"name": "resume", "type": "boolean", "required": False},
                       {"name": "dry_run", "type": "boolean", "required": False}]}
    monkeypatch.setattr(entries, "describe_entry", lambda *a, **kw: description)
    monkeypatch.setattr(sdk, "_load_model_registry", lambda **kwargs: object())
    # Use the retained-client route as the admission/invocation boundary.
    from unittest.mock import Mock
    invoke_result = Mock(side_effect=AssertionError("attached H3 reached invocation"))
    client = SimpleNamespace(invoke_result=invoke_result)

    with pytest.raises(CapabilityValidationError) as raised:
        GenerationFacade().video(
            entry="h3", target="http://127.0.0.1:8188",
            request="request.json", asset_map="assets.json", client=client,
        )
    assert "Attached ComfyUI" in str(raised.value)
    assert "local managed" in str(raised.value)
    assert "RunPod" in str(raised.value)
    invoke_result.assert_not_called()
