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


def test_strict_incomplete_model_failure_reaches_the_same_validated_envelope():
    from astrid.core.generation.preflight import generation_error
    from astrid.core.generation.setup_contract import validate_setup_envelope

    asset = {"name": "example.safetensors", "subdir": "checkpoints",
             "url": "https://example.invalid/model"}
    with pytest.raises(ModelLockValidationError) as caught:
        declared_model_specs([asset], entry_id="saved")
    result = generation_error(caught.value, entry={"id": "saved"}).to_envelope()
    validate_setup_envelope(result)
    assert result["issues"][0]["code"] == "model_lock_incomplete"
    assert result["issues"][0]["details"] == caught.value.details
    assert result["fixes"][0] == caught.value.setup_fix


def test_each_attached_node_and_model_links_to_its_specific_manual_repair(monkeypatch):
    from types import SimpleNamespace
    from astrid.core.generation.setup_contract import validate_setup_envelope

    pack = SimpleNamespace(name="FixturePack", repo="https://github.com/example/FixturePack",
                           classes={"FixtureOne", "FixtureTwo"})
    monkeypatch.setattr("vibecomfy.node_packs.resolve_node_packs", lambda names, **kwargs:
                        [pack] if names & pack.classes else [])
    monkeypatch.setattr("astrid.core.generation.asset_gaps._locked_commit",
                        lambda *args: "a" * 40)
    result = attached_asset_readiness("saved", nodes=["FixtureOne", "FixtureTwo", "CLIPLoader"],
                                      models=["model.safetensors"],
                                      target={"endpoint": "http://127.0.0.1:8188"})
    validate_setup_envelope(result)
    assert len(result["issues"]) == 4 and len(result["fixes"]) == 3
    first, second, core, model = result["issues"]
    assert first["fix_ids"] == second["fix_ids"] == [result["fixes"][0]["id"]]
    assert core["fix_ids"] == [result["fixes"][1]["id"]]
    assert model["fix_ids"] == [result["fixes"][2]["id"]]
    assert all(fix["who"] == "human" and "argv" not in fix for fix in result["fixes"])
    assert all(fix["instruction"]["then_argv"][-3:] ==
               ["--placement", "http://127.0.0.1:8188", "--json"] for fix in result["fixes"])
