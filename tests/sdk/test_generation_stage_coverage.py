"""Every named generation stage is exercised by both public admission routes."""
from __future__ import annotations

import json
from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path

import pytest

from astrid import console
from astrid.core.execution.generic_host import GenericPackHost, RuntimeProtocolClient
from astrid.sdk.client import AstridClient
from tests.core.generation.backends.test_comfy_model_resolution import install_target, target_info
from tests.core.generation.test_engine_endpoint_transport import (
    engine_configuration, network, observation_configuration,
)  # noqa: F401


def _stage_rows(capsys, command: list[str]) -> list[dict]:
    code = console.main(command)
    captured = capsys.readouterr()
    assert code == 0, captured.out + captured.err
    payload = json.loads(captured.out)
    assert payload.get("ok", True) is True, payload
    rows = payload.get("timings")
    assert isinstance(rows, list) and rows, payload
    return rows


def _assert_stage_parents(rows: list[dict], expected: dict[str, set[str | None]]) -> None:
    actual: dict[str, list[dict]] = {}
    for row in rows:
        actual.setdefault(row["stage"], []).append(row)
    for name, parents in expected.items():
        assert name in actual, f"missing generation timing stage: {name}"
        assert all(row.get("parent") in parents for row in actual[name]), (
            f"{name} has unexpected parent(s): {[row.get('parent') for row in actual[name]]}; "
            f"expected one of {sorted(map(str, parents))}"
        )
        assert all(row.get("seconds", 0) > 0 and row.get("total_seconds", 0) > 0
                   for row in actual[name]), f"{name} reported no positive work"


@pytest.mark.integration
def test_workflow_and_image_admissions_report_generation_stage_tree(
    network, real_runtime, tmp_path, monkeypatch, capsys,
):
    """Exercise workflow and prompt-only image through real Runtime admission."""
    comfy, _ = install_target(network, target_info(vaes=("ae.safetensors",)))
    daemon = real_runtime()
    client = AstridClient.open(
        endpoint=daemon.endpoint,
        credential=daemon.credential_path,
        realm_id=daemon.service.realm["id"],
        actor_id="owner",
        client_name="generation-stage-coverage",
        client_version="test",
        protocol_version="workspace.v1",
    )
    project = client.projects.create(slug="stage-coverage", name="Stage coverage",
                                     idempotency_key="stage-coverage-project")
    assert project.ok, project

    # Keep model selection and the pinned VibeComfy source identical to the
    # established attached-target admission test; only ComfyUI is simulated.
    from astrid.core.model_catalog.registry import ModelRegistry
    import astrid.sdk as sdk
    import vibecomfy
    from vibecomfy.runtime.session import current_source_content_digest, current_source_revision

    registry = ModelRegistry([ModelRegistry.load_default().get("z-image")])
    monkeypatch.setattr(sdk, "_load_model_registry", lambda **_kwargs: registry)

    dependency_root = str(Path(vibecomfy.__file__).resolve().parents[1])
    monkeypatch.setenv("ASTRID_VIBECOMFY_CHECKOUT", dependency_root)
    monkeypatch.setenv("ASTRID_VIBECOMFY_CANDIDATE_KIND", "local_snapshot")
    monkeypatch.setenv("ASTRID_VIBECOMFY_CANDIDATE_REVISION", current_source_revision())
    monkeypatch.setenv("ASTRID_VIBECOMFY_CANDIDATE_CONTENT_DIGEST", current_source_content_digest())

    host = GenericPackHost(
        pack_roots=[Path(__file__).resolve().parents[2] / "astrid" / "packs" / "vibecomfy"],
        attempt_root=tmp_path / "stage-attempt",
        client=RuntimeProtocolClient(daemon.endpoint, daemon.worker_token),
    )
    host.discover()
    host.register()
    monkeypatch.setattr(AstridClient, "open_from_launcher", lambda **_kwargs: nullcontext(client))

    from vibecomfy.registry.ready import workflow_from_ready
    from vibecomfy.workflow_bundle import emit_bundle

    workflow = emit_bundle(
        workflow_from_ready("image/z_image_turbo"),
        tmp_path / "stage-workflow" / "workflow.py",
        {"operation": "authored"},
    ).python_path
    workflow_rows = _stage_rows(capsys, [
        "generate", "workflow", str(workflow), "--placement", comfy.target.url,
        "--project", project.data["project_id"], "--prompt", "a silver paper boat",
        "--no-wait", "--json", "--timings",
    ])
    install_target(network, target_info())
    image_rows = _stage_rows(capsys, [
        "generate", "image", "a silver paper boat", "--placement", comfy.target.url,
        "--project", project.data["project_id"], "--no-wait", "--json", "--timings",
    ])
    entry_rows = _stage_rows(capsys, [
        "generate", "image", "a silver paper boat", "--entry", "z-image",
        "--placement", comfy.target.url, "--project", project.data["project_id"],
        "--no-wait", "--json", "--timings",
    ])

    # A qualified catalog candidate with a missing target model is prepared,
    # rejected by the real ComfyUI resolver, and followed by the ready catalog
    # route. This proves the candidate loop and its per-candidate timing detail.
    from astrid.core.generation import defaults
    base = registry.get("z-image")
    mode_spec = base.modes["t2i"]
    local_spec = mode_spec.backends["local"]
    missing_spec = replace(local_spec, model_roles={
        key: replace(role, names=(f"missing-{role.names[0]}",))
        for key, role in local_spec.model_roles.items()
    })
    missing_entry = replace(base, id="a-missing", modes={
        "t2i": replace(mode_spec, backends={"local": missing_spec}),
    })
    fallback_registry = ModelRegistry([missing_entry, base])
    monkeypatch.setattr(sdk, "_load_model_registry", lambda **_kwargs: fallback_registry)
    monkeypatch.setattr(defaults, "dispatch_ids", lambda *args, **_kwargs: (None, ()))
    fallback_rows = _stage_rows(capsys, [
        "generate", "image", "a silver paper boat", "--placement", comfy.target.url,
        "--project", project.data["project_id"], "--no-wait", "--json", "--timings",
    ])
    candidate_rows = [row for row in fallback_rows if row["stage"] == "candidate preparation"]
    assert len(candidate_rows) == 2, candidate_rows
    assert candidate_rows[0].get("detail") == "a-missing", candidate_rows
    assert candidate_rows[1].get("detail") != "a-missing", candidate_rows
    assert all(row["seconds"] > 0 and row["total_seconds"] > 0 for row in candidate_rows)

    common = {
        "argument parsing": {None},
        "Runtime connection": {None},
        "placement": {None},
        "generation settings": {None},
        "default selection": {None},
        "candidate qualification": {None},
        "availability declaration": {"candidate qualification"},
        "candidate preparation": {None},
        "workflow build": {"candidate preparation"},
        "model registry": {None, "workflow build"},
        "target node list": {"candidate preparation", "workflow build"},
        "workflow emission": {"candidate preparation", "bundle materialisation"},
        "approval compile": {"candidate preparation"},
        "readiness description": {None},
        "bundle materialisation": {None},
        "object import": {"source retention", "invocation", "bundle materialisation"},
        "invocation": {None},
        "capability registries": {"invocation"},
        "capability lookup": {"invocation"},
        "SDK admission preflight": {"invocation"},
        "Workspace admission preflight": {"invocation"},
        "Remote admission preflight": {"invocation"},
        "ComfyUI GET /system_stats": {"target check"},
        "ComfyUI GET /object_info": {"workflow build", "target node list", "ComfyUI GET /object_info"},
    }
    workflow_expected = {
        **common,
        "workflow snapshot": {None},
        "source retention": {None, "workflow snapshot", "candidate preparation"},
        "workflow inputs": {None},
        "entry needs": {"candidate preparation"},
    }
    image_expected = {
        **common,
        "workflow snapshot": {None, "candidate preparation"},
        "source retention": {None, "workflow snapshot", "candidate preparation"},
        "entry discovery": {None},
        "pack discovery": {None},
    }
    _assert_stage_parents(workflow_rows, workflow_expected)
    _assert_stage_parents(image_rows, image_expected)
    _assert_stage_parents(entry_rows, {
        "argument parsing": {None},
        "Runtime connection": {None},
        "entry lookup": {None},
        "candidate preparation": {None},
        "invocation": {None},
        "capability registries": {"invocation"},
        "capability lookup": {"invocation"},
        "SDK admission preflight": {"invocation"},
        "Workspace admission preflight": {"invocation"},
    })

    # Each command created a distinct admitted Runtime task.
    tasks = client.tasks.list(project.data["project_id"])
    assert tasks.ok, tasks
    assert len(tasks.data[0][0]) == 4
    assert len({row["task_id"] for row in tasks.data[0][0]}) == 4
    host.shutdown()


def test_early_generation_refusal_has_parse_work_but_never_opens_runtime(monkeypatch, capsys):
    """Reject an empty named prompt during input reads before Runtime admission."""
    def forbidden_runtime_open(**_kwargs):
        pytest.fail("generation input refusal must precede the Runtime connection")

    monkeypatch.setattr(AstridClient, "open_from_launcher", forbidden_runtime_open)
    code = console.main([
        "generate", "image", "--prompt", " ", "--placement", "http://127.0.0.1:8188",
        "--json", "--timings",
    ])
    captured = capsys.readouterr()
    assert code == 2, captured.out + captured.err
    payload = json.loads(captured.out)
    rows = payload["timings"]
    assert any(row["stage"] == "argument parsing" and row["seconds"] > 0 for row in rows)
    assert any(row["stage"] == "input reads" and row["seconds"] > 0 for row in rows)
    assert not any(row["stage"] == "Runtime connection" for row in rows)
