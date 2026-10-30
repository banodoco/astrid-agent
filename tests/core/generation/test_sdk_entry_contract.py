"""Explicit SDK entries share the metadata contract before any admission."""
from types import SimpleNamespace

import pytest

from astrid.core.generation import entries
from astrid.core.model_catalog.registry import ModelRegistry
from astrid.sdk.exceptions import CapabilityPreconditionError, CapabilityValidationError
from astrid.sdk.generation import GenerationFacade


@pytest.fixture
def isolated_catalog(monkeypatch):
    import astrid.sdk as sdk
    registry = ModelRegistry.load_default()
    monkeypatch.setattr(sdk, "_load_model_registry", lambda **kwargs: registry)
    monkeypatch.setattr(entries, "user_entries", lambda: [])
    monkeypatch.setattr(entries, "action_entries", lambda: [])
    return registry


def test_catalog_entry_resolves_before_existing_target_route(isolated_catalog, monkeypatch):
    calls = []
    monkeypatch.setattr(GenerationFacade, "_generate_on_target",
                        lambda self, **kwargs: calls.append(kwargs) or kwargs["inputs"])
    expected_description, expected = entries.resolve_entry_inputs(
        "z-image", {"prompt": "silver boat", "seed": 12}, kind="image", registry=isolated_catalog,
    )
    result = GenerationFacade().image(entry="z-image", prompt="silver boat", seed=12,
                                     target="auto", dry_run=True)
    assert result == expected
    assert len(calls) == 1
    assert calls[0]["model"] == "z-image"
    assert calls[0]["mode"] == expected_description["mode"] == "t2i"


@pytest.mark.parametrize("values", [
    {}, {"prompt": "boat", "unknown_knob": 2}, {"prompt": "boat", "seed": "twelve"},
    {"prompt": "boat", "seed": True}, {"prompt": ["boat"]},
    {"prompt": "boat", "size": "512x512", "width": 512, "height": 512},
])
def test_invalid_sdk_entry_never_reaches_target_or_admission(isolated_catalog, monkeypatch, values):
    def forbidden(*args, **kwargs):
        pytest.fail("invalid entry reached target or admission")
    monkeypatch.setattr(GenerationFacade, "_generate_on_target", forbidden)
    monkeypatch.setattr(GenerationFacade, "_invoke", forbidden)
    with pytest.raises(CapabilityValidationError):
        GenerationFacade().image(entry="z-image", target="auto", **values)


def test_sdk_entry_selector_conflict_precedes_target(isolated_catalog, monkeypatch):
    monkeypatch.setattr(GenerationFacade, "_generate_on_target",
                        lambda *a, **k: pytest.fail("selector conflict reached target"))
    with pytest.raises(CapabilityValidationError, match="model and entry"):
        GenerationFacade().image(entry="z-image", model="other", target="auto", prompt="boat")


def test_h3_explicit_sdk_uses_shared_validation_and_retains_parent_boundary(monkeypatch):
    import astrid.sdk as sdk
    from astrid.sdk.client import AstridClient
    digest = "sha256:" + "1" * 64
    action = {"id": "h3", "kind": "video", "modes": ["transform"], "action": "h3_av.transform",
              "inputs": [{"name": name, "type": "file", "required": True}
                         for name in ("request", "asset_map")]}
    monkeypatch.setattr(entries, "user_entries", lambda: [])
    monkeypatch.setattr(entries, "action_entries", lambda: [action])
    monkeypatch.setattr(entries, "ready_entries", lambda: [])
    monkeypatch.setattr(GenerationFacade, "_invoke", lambda *a, **k: pytest.fail("untracked H3 invoke"))
    monkeypatch.setattr(sdk, "invoke_result", lambda *a, **k: pytest.fail("H3 bypassed parent admission"))
    monkeypatch.setattr(AstridClient, "open_from_launcher",
                        lambda *a, **k: pytest.fail("H3 bootstrapped Runtime before parent admission"))
    with pytest.raises(CapabilityValidationError, match="managed Runtime object"):
        GenerationFacade().video(entry="h3", request="/tmp/local.json", asset_map=digest)
    with pytest.raises(CapabilityPreconditionError, match="Runtime-admitted parent"):
        GenerationFacade().video(entry="h3", request={"digest": digest}, asset_map=digest)


def test_cli_json_and_sdk_entry_resolve_identically(isolated_catalog, monkeypatch, tmp_path, capsys):
    from astrid.core.generation import cli
    import astrid
    calls = []
    monkeypatch.setitem(astrid.__dict__, "generate", GenerationFacade())
    monkeypatch.setattr(cli, "_open_client", lambda: pytest.fail("dry-run connected to Runtime"))
    def capture(self, **kwargs):
        calls.append(kwargs)
        return {"model": kwargs["model"], "mode": kwargs["mode"], "inputs": kwargs["inputs"]}
    monkeypatch.setattr(GenerationFacade, "_generate_on_target", capture)
    values = {"prompt": "silver boat", "seed": 12}
    file = tmp_path / "inputs.json"
    import json
    file.write_text(json.dumps(values))
    assert cli.main(["image", "--entry", "z-image", "--inputs-json", str(file), "--dry-run"]) == 0
    cli_result = json.loads(capsys.readouterr().out)
    sdk_result = GenerationFacade().image(entry="z-image", target="auto", dry_run=True, **values)
    assert cli_result == sdk_result
    assert len(calls) == 2 and calls[0]["inputs"] == calls[1]["inputs"]


@pytest.mark.parametrize("mode", ["t2i", "edit"])
def test_actual_catalog_projection_and_context_help_agree_without_probes(monkeypatch, capsys, mode):
    from astrid.core.generation import cli, preflight, listing
    from astrid.core.model_catalog.schema import ModelEntry, ModeSpec
    from astrid.sdk.client import AstridClient
    from astrid.core.generation.backends.comfy_target import ComfyTargetClient
    registry = ModelRegistry([ModelEntry(id="declared", modality="image", modes={
        "t2i": ModeSpec(supports=("prompt", "seed", "count"), requires=("prompt",)),
        "edit": ModeSpec(supports=("prompt", "image_ref", "steps"), requires=("prompt", "image_ref")),
    })])
    monkeypatch.setattr(ModelRegistry, "load_default", classmethod(lambda cls, **kwargs: registry))
    monkeypatch.setattr(entries, "user_entries", lambda: [])
    monkeypatch.setattr(entries, "action_entries", lambda: [])
    def forbidden(*args, **kwargs):
        pytest.fail("metadata help performed execution, readiness, provider, or installation operation")
    monkeypatch.setattr(cli, "_open_client", forbidden)
    monkeypatch.setattr(AstridClient, "open_from_launcher", forbidden)
    monkeypatch.setattr(GenerationFacade, "_invoke", forbidden)
    monkeypatch.setattr(preflight, "select_generation_target", forbidden)
    monkeypatch.setattr(preflight, "require_local_generation_readiness", forbidden)
    monkeypatch.setattr(preflight, "require_generation_target_readiness", forbidden)
    monkeypatch.setattr(ComfyTargetClient, "request_json", forbidden)
    monkeypatch.setattr(listing, "_targets", forbidden)
    import subprocess
    monkeypatch.setattr(subprocess, "run", forbidden)
    assert cli.main(["describe", "--entry", "declared", "--mode", mode, "--json"]) == 0
    import json
    description = json.loads(capsys.readouterr().out)
    with pytest.raises(SystemExit) as result:
        cli.main(["image", "--entry", "declared", "--mode", mode, "--help"])
    assert result.value.code == 0
    help_text = capsys.readouterr().out
    for port in description["inputs"]:
        assert f"{port['name']} ({port['type']}; " in help_text
        assert ("required" if port["required"] else "optional") in help_text
    if mode == "edit":
        assert "seed (integer;" not in help_text
    else:
        assert "image_ref (string;" not in help_text


def test_actual_h3_cli_and_sdk_validate_same_files_and_stop_before_parent(monkeypatch, tmp_path, capsys):
    import json
    import astrid
    import astrid.sdk as sdk
    from astrid.sdk.client import AstridClient
    from astrid.core.generation import cli
    from astrid.core.contracts.errors import AstridError
    monkeypatch.setattr(entries, "user_entries", lambda: [])
    monkeypatch.setattr(sdk, "_load_model_registry", lambda **kwargs: ModelRegistry([]))
    monkeypatch.setitem(astrid.__dict__, "generate", GenerationFacade())
    def forbidden(*args, **kwargs):
        pytest.fail("H3 crossed the unavailable canonical parent boundary")
    monkeypatch.setattr(cli, "_open_client", forbidden)
    monkeypatch.setattr(GenerationFacade, "_invoke", forbidden)
    monkeypatch.setattr(sdk, "invoke_result", forbidden)
    monkeypatch.setattr(AstridClient, "open_from_launcher", forbidden)
    digest = "sha256:" + "c" * 64
    values = {"request": {"digest": digest}, "asset_map": digest}
    file = tmp_path / "h3-inputs.json"
    file.write_text(json.dumps(values))
    with pytest.raises(CapabilityPreconditionError, match="Runtime-admitted parent") as sdk_error:
        GenerationFacade().video(entry="h3", **values)
    with pytest.raises(AstridError, match="Runtime-admitted parent") as cli_error:
        cli.main(["video", "--entry", "h3", "--inputs-json", str(file)])
    assert str(sdk_error.value) == str(cli_error.value)
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("value,error,diagnostic", [
    ("/tmp/local.json", CapabilityValidationError, "managed Runtime object"),
    ({"digest": "sha256:" + "a" * 64}, CapabilityPreconditionError, "Runtime-admitted parent"),
])
def test_task129_ordinary_action_keeps_validation_and_parent_boundary(monkeypatch, value, error, diagnostic):
    import astrid.sdk as sdk
    from astrid.sdk.client import AstridClient

    action = {"id": "ordinary", "kind": "video", "modes": ["transform"], "action": "example.run",
              "inputs": [{"name": "request", "type": "file", "required": True}]}
    monkeypatch.setattr(entries, "user_entries", lambda: [])
    monkeypatch.setattr(entries, "action_entries", lambda: [action])
    monkeypatch.setattr(entries, "_ready_ids", lambda: set())
    monkeypatch.setattr(sdk, "_load_model_registry", lambda **kwargs: ModelRegistry([]))
    def forbidden(*args, **kwargs):
        pytest.fail("ordinary action crossed the parent boundary")
    monkeypatch.setattr(sdk, "invoke_result", forbidden)
    monkeypatch.setattr(AstridClient, "open_from_launcher", forbidden)
    with pytest.raises(error, match=diagnostic):
        GenerationFacade().video(entry="ordinary", request=value)


def test_task130_unsupported_action_entry_fails_before_invoke(monkeypatch):
    import astrid.sdk as sdk

    digest = "sha256:" + "d" * 64
    action = {"id": "ordinary", "kind": "video", "modes": ["transform"],
              "action": "example.transform", "inputs": [
                  {"name": "request", "type": "file", "required": True},
                  {"name": "asset_map", "type": "file", "required": True}]}
    monkeypatch.setattr(entries, "user_entries", lambda: [])
    monkeypatch.setattr(entries, "action_entries", lambda: [action])
    monkeypatch.setattr(entries, "ready_entries", lambda: [])
    monkeypatch.setattr(sdk, "_load_model_registry", lambda **kwargs: ModelRegistry([]))
    monkeypatch.setattr(sdk, "invoke_result", lambda *a, **k: pytest.fail("unsupported action was invoked"))
    client = SimpleNamespace(invoke_result=lambda *a, **k: pytest.fail("unsupported action was invoked"))

    with pytest.raises(CapabilityPreconditionError, match="Runtime-admitted parent"):
        GenerationFacade().video(entry="ordinary", request=digest, asset_map=digest, client=client)


@pytest.mark.parametrize("digest", ["sha256:" + "a" * 64, "a" * 64, "sha256:malformed"])
def test_task129_h3_mixed_digest_and_local_path_never_reaches_parent(monkeypatch, digest):
    import astrid.sdk as sdk
    from astrid.sdk.client import AstridClient

    def forbidden(*args, **kwargs):
        pytest.fail("mixed custody reached admission")
    monkeypatch.setattr(sdk, "invoke_result", forbidden)
    monkeypatch.setattr(AstridClient, "open_from_launcher", forbidden)
    with pytest.raises(CapabilityValidationError, match="producer-local file path"):
        GenerationFacade().video(entry="h3", request="/tmp/request.json", asset_map=digest)


def test_task129_h3_producer_controls_keep_declared_boolean_validation(monkeypatch):
    import astrid.sdk as sdk
    from astrid.sdk.client import AstridClient

    def forbidden(*args, **kwargs):
        pytest.fail("invalid producer control reached admission")
    monkeypatch.setattr(sdk, "invoke_result", forbidden)
    monkeypatch.setattr(AstridClient, "open_from_launcher", forbidden)
    with pytest.raises(CapabilityValidationError, match="requires a boolean"):
        GenerationFacade().video(entry="h3", request="/tmp/request.json", asset_map="/tmp/assets.json",
                                 require_worker_qualification="false")
