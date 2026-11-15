"""CPU proof that CLI auto placement follows catalog execution declarations."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

_REGISTRY = None


def _catalog_rows():
    from astrid.core.generation.listing import catalog_rows
    from astrid.core.model_catalog.registry import ModelRegistry
    from scripts.gen_capability_index import _in_repo_packs_only

    global _REGISTRY
    with _in_repo_packs_only():
        _REGISTRY = ModelRegistry.load_default()
        original_load_default = ModelRegistry.load_default.__func__
        ModelRegistry.load_default = classmethod(lambda cls, **_kw: _REGISTRY)
        try:
            rows = catalog_rows()
        finally:
            ModelRegistry.load_default = classmethod(original_load_default)
    indexed = {}
    for row in rows:
        if row.get("source") == "catalog":
            indexed[(row["id"], row["mode"], row["backend"])] = row
    selected = []
    for entry in _REGISTRY.list_all(include_closed=True):
        for mode, spec in entry.modes.items():
            backend = _declared_execution(spec)
            selected.append(indexed[(entry.id, mode, backend)])
    return selected


def _registry():
    global _REGISTRY
    if _REGISTRY is None:
        from astrid.core.model_catalog.registry import ModelRegistry
        _REGISTRY = ModelRegistry.load_default()
    return _REGISTRY


def _declared_execution(mode_spec):
    from astrid.core.model_catalog.taxonomy import CODEX_BACKEND_ID, LOCAL_BACKEND_ID

    backend_ids = list(mode_spec.backends)
    if LOCAL_BACKEND_ID in backend_ids:
        return LOCAL_BACKEND_ID
    available = [backend for backend in backend_ids if backend != CODEX_BACKEND_ID] or backend_ids
    assert len(available) == 1
    return available[0]


_ROWS = _catalog_rows()


def _json_document(stdout):
    start = stdout.rfind("\n{")
    return json.loads(stdout[start + 1:] if start >= 0 else stdout)


def _input_value(name):
    fixtures = Path(__file__).resolve().parents[2] / "fixtures"
    if name.endswith("_refs"):
        return [str(fixtures / "storyboard-assets" / "open.png")]
    if "video" in name and "ref" in name:
        path = fixtures / "reshape" / "hype_regression" / "main.mp4"
        return str(path)
    if "audio" in name and "ref" in name:
        return str(fixtures / "storyboard-assets" / "open.wav")
    if "image" in name and "ref" in name:
        return str(fixtures / "storyboard-assets" / "open.png")
    if name.endswith("_ref"):
        return str(fixtures / "storyboard-assets" / "open.png")
    if name in {"seed", "count", "steps", "bitrate", "target_resolution", "width", "height"}:
        return 1
    if name in {"instrumental", "generate_audio", "enable_safety_checker"}:
        return True
    if name in {"duration", "strength", "guidance_scale", "noise_scale", "fps"}:
        return 1.0
    return "placement proof"


@pytest.mark.parametrize("selection", ["--model", "--entry"])
@pytest.mark.parametrize("placement", [None, "auto", "http://127.0.0.1:8199"],
                         ids=["default-placement", "auto-placement", "attached-placement"])
@pytest.mark.parametrize("row", _ROWS, ids=lambda row: f"{row['id']}:{row['mode']}")
def test_cli_auto_dispatches_each_catalog_mode_by_declared_execution(
    row, selection, placement, tmp_path, monkeypatch, capsys, record_property,
):
    """Exercise CLI parsing and SDK routing while provider and Comfy effects are intercepted."""
    from astrid.core.generation import cli
    from astrid.core.generation import placement as placements
    from astrid.core.model_catalog.registry import ModelRegistry
    from astrid.sdk.generation import GenerationFacade

    registry = _registry()
    monkeypatch.setattr(ModelRegistry, "load_default", classmethod(lambda cls, **_kw: registry))
    entry, mode_spec = registry.get_by_mode(row["id"], row["mode"])
    declared = _declared_execution(mode_spec)
    captured = []

    def invoke(self, capability_id, /, **kwargs):
        provider_inputs = kwargs.get("inputs", {})
        from astrid.core.generation.backends.registry import load_default_generation_backend_registry
        from tests.helpers.generation_provider_fake import exercise_provider_request
        provider = exercise_provider_request(
            entry, row["mode"], provider_inputs, tmp_path, monkeypatch,
            backend_registry=load_default_generation_backend_registry(),
        )
        captured.append(("provider", capability_id, provider_inputs, provider))
        return {"dry_run": True, "capability": capability_id}

    def target(self, **kwargs):
        resolved = placements.validate_placement(placements.resolve_placement(
            placements.request_from_selector(kwargs.get("placement")),
            client=kwargs.get("client"), repo=kwargs.get("project_root"),
        ))
        captured.append(("comfy", resolved.kind, kwargs.get("model")))
        return {"dry_run": True, "model": kwargs.get("model"), "mode": kwargs.get("mode")}

    monkeypatch.setattr(GenerationFacade, "_invoke", invoke)
    monkeypatch.setattr(GenerationFacade, "_generate_on_target_impl", target)
    monkeypatch.setattr(placements, "_discovered_attachment", lambda *_a, **_k: None)
    monkeypatch.setattr("astrid.core.generation.settings.placement_default", lambda *_a, **_k: {})
    inputs = {
        name: _input_value(name)
        for name in mode_spec.requires
    }
    assignments = [value for name, item in inputs.items()
                   if (value := f"{name}={json.dumps(item)}")]
    monkeypatch.setattr("astrid.sdk.generation._resolve_invoke_destination",
                        lambda **kw: (kw.get("out"), "proof-project"))
    prompt = ([] if selection == "--entry" or "prompt" in inputs or entry.modality != "image"
              else ["placement proof"])
    argv = [entry.modality, *prompt, selection, entry.id, "--mode", row["mode"],
            *[part for assignment in assignments for part in ("--input", assignment)],
            *(["--placement", placement] if placement is not None else []),
            "--dry-run", "--json"]

    code = cli.main(argv)
    stdout = capsys.readouterr().out
    if selection == "--entry":
        from astrid.core.generation.entries import describe_entry
        description = describe_entry(entry.id, row["mode"], registry=registry, _native=True)
        unknown_inputs = [port["name"] for port in description["inputs"]
                          if port.get("type") == "unknown" and port["name"] in inputs]
        if unknown_inputs:
            assert code == 2, stdout
            payload = _json_document(stdout)
            issue = payload["issues"][0]
            expected = f"entry input {unknown_inputs[0]!r} has no declared scalar type (unknown)"
            assert issue["code"] == "input_rejected"
            assert issue["message"] == expected
            assert issue["details"]["field"] == unknown_inputs[0]
            assert not captured
            record_property("entry", entry.id)
            record_property("mode", row["mode"])
            record_property("result", "unknown_input_type_refusal")
            record_property("reason", expected)
            return
    if placement == "http://127.0.0.1:8199" and declared != "local":
        assert code == 2, stdout
        payload = _json_document(stdout)
        assert entry.id in payload["issues"][0]["message"]
        assert "valid placements" in payload["issues"][0]["message"]
        assert not captured
        record_property("entry", entry.id)
        record_property("mode", row["mode"])
        record_property("execution", declared)
        record_property("selector", placement)
        record_property("result", "placement_conflict")
        return
    if row.get("availability") == "unavailable" and code != 0:
        assert not captured
        payload = _json_document(stdout)
        issue = payload["issues"][0]
        expected = "generation_route_unqualified"
        assert issue["code"] == expected
        result = f"refused:{expected}"
        record_property("result", result)
        record_property("entry", row["id"])
        record_property("mode", row["mode"])
        record_property("execution", declared)
        record_property("declared_executions", ",".join(mode_spec.backends))
        record_property("availability", row["availability"])
        record_property("path", result)
        return
    assert code == 0, stdout
    output = _json_document(stdout)
    if declared == "local":
        expected_kind = "attached" if placement == "http://127.0.0.1:8199" else "local"
        assert captured == [("comfy", expected_kind, entry.id)]
        resolved_path = f"comfy:{expected_kind}"
    else:
        assert captured and captured[0][0] == "provider"
        assert captured[0][2].get("execution") == declared
        assert captured[0][3]["backend"].__name__
        resolved_path = captured[0][1]
    assert output["dry_run"] is True
    record_property("entry", entry.id)
    record_property("mode", row["mode"])
    record_property("execution", declared)
    record_property("declared_executions", ",".join(mode_spec.backends))
    record_property("path", resolved_path)
    record_property("result", "dry_run")
    record_property("availability", row["availability"])


@pytest.mark.parametrize("selector", ["runpod", "http://127.0.0.1:8199"])
@pytest.mark.parametrize("selection", ["--model", "--entry"])
def test_concrete_placement_conflicts_with_generic_provider_entry(selector, selection, tmp_path, monkeypatch, capsys, record_property):
    import json
    from astrid.core.generation import cli
    from astrid.core.generation import placement
    from astrid.core.model_catalog.registry import ModelRegistry
    from astrid.core.generation import settings
    from astrid.sdk.generation import GenerationFacade

    registry = ModelRegistry.load_default()
    entry = mode = spec = None
    for candidate in registry.list_all(include_closed=True):
        for candidate_mode, candidate_spec in candidate.modes.items():
            if ("local" in candidate_spec.backends or len(candidate_spec.backends) != 1
                    or "prompt" not in candidate_spec.requires):
                continue
            entry, mode, spec = candidate, candidate_mode, candidate_spec
            break
        if entry is not None:
            break
    assert entry is not None
    # The conflicting saved default remains visible to the resolver. The explicit selector
    # must be rejected based on the catalog declaration before any provider/Comfy request.
    monkeypatch.setattr(placement, "_discovered_attachment", lambda *_a, **_k: None)
    monkeypatch.setattr(settings, "placement_default", lambda *_a, **_k: {
        "kind": "attached", "endpoint": "http://127.0.0.1:8199"
    })
    monkeypatch.setattr(GenerationFacade, "_invoke", lambda *a, **k: pytest.fail("provider dispatched"))
    monkeypatch.setattr(GenerationFacade, "_generate_on_target_impl",
                        lambda *a, **k: pytest.fail("Comfy/provider target path reached"))
    monkeypatch.setattr(ModelRegistry, "load_default", classmethod(lambda cls, **_kw: registry))
    monkeypatch.setattr("astrid.packs.runpod.machine.journal_leases", lambda: [])
    values = {
        name: _input_value(name)
        for name in spec.requires
    }
    assignments = [f"{name}={json.dumps(value)}" for name, value in values.items()]
    code = cli.main([entry.modality, selection, entry.id, "--mode", mode,
                     *[part for assignment in assignments for part in ("--input", assignment)],
                     "--placement", selector, "--dry-run", "--json"])
    output = capsys.readouterr().out
    assert code == 2
    payload = _json_document(output)
    message = payload["issues"][0]["message"]
    assert entry.id in message
    assert next(iter(spec.backends)) in message
    assert "valid placements" in message
    record_property("entry", entry.id)
    record_property("mode", mode)
    record_property("execution", next(iter(spec.backends)))
    record_property("declared_executions", ",".join(spec.backends))
    record_property("selector", selector)
    record_property("result", "placement_conflict")


def test_provider_audio_without_mode_resolves_same_default_path_for_none_and_auto(monkeypatch):
    from astrid.core.generation import settings
    from astrid.core.model_catalog.registry import ModelRegistry
    from astrid.core.generation.availability import model_availability
    from astrid.sdk.generation import GenerationFacade

    registry = _registry()
    candidate = next(
        (entry, mode, spec, backend)
        for entry in registry.list_by_modality("audio", include_closed=True)
        if len(entry.modes) == 1
        for mode, spec in entry.modes.items()
        for backend in (_declared_execution(spec),)
        if backend != "local"
        if model_availability(entry.id, mode, backend).get("available") is True
        and "prompt" in spec.requires
    )
    entry, mode, spec, backend = candidate
    monkeypatch.setattr(ModelRegistry, "load_default", classmethod(lambda cls, **_kw: registry))
    monkeypatch.setattr(settings, "placement_default", lambda *_a, **_k: {})
    captured = []

    def invoke(self, capability_id, /, **kwargs):
        captured.append((capability_id, dict(kwargs["inputs"])))
        return {"dry_run": True, "capability": capability_id}

    monkeypatch.setattr(GenerationFacade, "_invoke", invoke)
    monkeypatch.setattr(GenerationFacade, "_generate_on_target_impl",
                        lambda *a, **k: pytest.fail("provider audio entered Comfy target path"))
    monkeypatch.setattr("astrid.sdk.generation._resolve_invoke_destination",
                        lambda **kw: (kw.get("out"), "proof-project"))
    request = {name: _input_value(name) for name in spec.requires}

    for selector in (None, "auto"):
        result = GenerationFacade().audio(
            model=entry.id, placement=selector, dry_run=True, project="proof-project", **request,
        )
        assert result["dry_run"] is True

    assert len(captured) == 2
    assert captured[0] == captured[1]
    assert captured[0][1]["mode"] == mode
    assert captured[0][1]["execution"] == backend
