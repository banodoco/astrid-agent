from types import SimpleNamespace

import pytest

from astrid.sdk.generation import GenerationFacade
from astrid.sdk.exceptions import CapabilityValidationError


def test_h3_entry_passes_local_producer_files_to_one_action(monkeypatch):
    import astrid.sdk as sdk
    from astrid.core.generation import entries
    description = {"id": "h3", "kind": "video", "source": "action", "action": "h3_av.transform",
                   "mode": "transform", "modes": ["transform"], "inputs": [
                       {"name": "request", "type": "file", "required": True},
                       {"name": "asset_map", "type": "file", "required": True}]}
    monkeypatch.setattr(entries, "describe_entry", lambda *a, **kw: description)
    monkeypatch.setattr(sdk, "_load_model_registry", lambda **kwargs: object())
    calls = []
    client = SimpleNamespace(invoke_result=lambda *a, **kw: calls.append((a, kw)) or "result")
    assert GenerationFacade().video(entry="h3", request="request.json", asset_map="assets.json", project="p", client=client) == "result"
    assert len(calls) == 1 and calls[0][0] == ("h3_av.transform",)
    assert calls[0][1]["inputs"] == {"request": "request.json", "asset_map": "assets.json"}
    with pytest.raises(CapabilityValidationError):
        GenerationFacade().video(entry="h3", request="request.json", client=client)
    assert len(calls) == 1
