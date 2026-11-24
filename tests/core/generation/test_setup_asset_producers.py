"""LE-1b independent producer stage: actual core authority and model lock facts."""
from __future__ import annotations

import pytest

from astrid.core.generation.asset_gaps import attached_asset_readiness, declared_model_specs
from astrid.core.generation.engine_contracts import ModelLockValidationError
from astrid.core.generation.model_declarations import IncompleteModelDeclaration, incomplete_model_repair
from astrid.core.generation.node_class_authority import core_node_classes


def test_strict_model_declaration_and_inspection_share_one_missing_fact_row():
    asset = {"name": "example.safetensors", "subdir": "checkpoints",
             "url": "https://example.invalid/model"}
    incomplete = []
    assert declared_model_specs([asset], incomplete=incomplete) == ()
    assert declared_model_specs([asset], require_complete=False) == ()
    with pytest.raises(ModelLockValidationError, match="sha256, size_bytes") as caught:
        declared_model_specs([asset], entry_id="saved")
    assert isinstance(caught.value, IncompleteModelDeclaration)
    assert caught.value.code == "model_lock_incomplete"
    assert caught.value.details == incomplete[0]
    assert caught.value.setup_fix == incomplete_model_repair(incomplete[0], "saved")
    assert caught.value.setup_fix["who"] == "human"
    assert caught.value.setup_fix["instruction"]["then_argv"] == [
        "astrid", "generate", "align", "--entry", "saved", "--json",
    ]
    assert "SHA-256" in caught.value.setup_fix["instruction"]["step"]
    assert "argv" not in caught.value.setup_fix
    assert "--apply" not in caught.value.setup_fix["instruction"]["then_argv"]


def test_unknown_entry_uses_explicit_placeholder_not_all_entries():
    repair = incomplete_model_repair({"name": "x", "subdir": None, "missing": ["url"]})
    assert "<ENTRY>" in repair["instruction"]["then_argv"]
    assert set(repair["placeholders"]) == {"ENTRY"}
    assert "--all" not in repair["instruction"]["then_argv"]


def test_core_class_evidence_keeps_known_text_but_does_not_call_unknown_core(monkeypatch):
    monkeypatch.setattr("vibecomfy.node_packs.resolve_node_packs", lambda *args, **kwargs: [])
    unknown = "AstridDefinitelyMissingNode"
    assert core_node_classes(["CLIPLoader", "SaveAudio", unknown]) == {"CLIPLoader", "SaveAudio"}
    readiness = attached_asset_readiness("example", nodes=["CLIPLoader", unknown])
    known, other = readiness["issues"]
    assert known["message"] == (
        "Your ComfyUI does not have CLIPLoader, which is part of ComfyUI itself; update ComfyUI, then retry."
    )
    assert other["code"] == "missing_node_pack"
    assert "no known providing pack" in other["message"]
    assert "Identify the providing custom node pack" in other["message"]
    assert "part of ComfyUI itself" not in other["message"]
    assert all(fix["scope"] == "attached_target" for fix in readiness["fixes"])
