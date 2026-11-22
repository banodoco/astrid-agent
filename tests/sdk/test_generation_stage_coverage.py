"""Every named generation stage is exercised by both public admission routes."""
from __future__ import annotations

import json
from contextlib import nullcontext
from pathlib import Path

import pytest

from astrid import console
from astrid.core.execution.generic_host import GenericPackHost, RuntimeProtocolClient
from astrid.core.generation import cli
from astrid.sdk.client import AstridClient
from tests.core.generation.backends.test_comfy_model_resolution import install_target, target_info
from tests.core.generation.backends.test_comfy_target import network  # noqa: F401


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
        assert all(row.get("total_seconds", 0) >= 0 for row in actual[name])


@pytest.mark.integration
def test_workflow_and_image_admissions_report_generation_stage_tree(
    network, real_runtime, tmp_path, monkeypatch, capsys,
):
    """Exercise workflow and prompt-only image through real Runtime admission."""
    comfy, _ = install_target(network, target_info())
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
    from astrid.core.generation import preflight
    from astrid.core.model_catalog.registry import ModelRegistry
    import astrid.sdk as sdk
    import vibecomfy
    from vibecomfy.runtime.session import current_source_content_digest, current_source_revision

    monkeypatch.setattr(preflight, "_module_available", lambda name: name == "vibecomfy")
    def unexpected_managed_probe(*_args, **_kwargs):
        raise AssertionError("attached generation probed managed ComfyUI")

    monkeypatch.setattr(preflight, "_comfyui_runtime_available", unexpected_managed_probe)
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
    monkeypatch.setattr(cli, "_open_client", lambda: nullcontext(client))

    from vibecomfy.registry.ready import repo_ready_template_discovery, resolve_ready_template

    workflow = resolve_ready_template("image/z_image_turbo", repo_ready_template_discovery()).path
    workflow_rows = _stage_rows(capsys, [
        "generate", "workflow", str(workflow), "--placement", comfy.target.url,
        "--project", project.data["project_id"], "--prompt", "a silver paper boat",
        "--no-wait", "--json", "--timings",
    ])
    image_rows = _stage_rows(capsys, [
        "generate", "image", "a silver paper boat", "--placement", comfy.target.url,
        "--project", project.data["project_id"], "--no-wait", "--json", "--timings",
    ])
    entry_rows = _stage_rows(capsys, [
        "generate", "image", "a silver paper boat", "--entry", "image/z_image_turbo",
        "--placement", comfy.target.url, "--project", project.data["project_id"],
        "--no-wait", "--json", "--timings",
    ])

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
        "model registry": {"workflow build"},
        "target node list": {"candidate preparation", "workflow build"},
        "workflow emission": {"candidate preparation", "bundle materialisation"},
        "approval compile": {"candidate preparation"},
        "readiness description": {None},
        "bundle materialisation": {None},
        "object import": {"source retention", "invocation", "bundle materialisation"},
        "invocation": {None},
        "capability registries": {"invocation"},
        "capability lookup": {"invocation"},
        "admission preflight": {"invocation"},
        "ComfyUI GET /system_stats": {"placement", "candidate preparation"},
        "ComfyUI GET /object_info": {"candidate preparation", "workflow build", "target node list"},
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
        "admission preflight": {"invocation"},
    })

    # Each command created a distinct admitted Runtime task.
    tasks = client.tasks.list(project.data["project_id"])
    assert tasks.ok, tasks
    assert len(tasks.data[0][0]) == 3
    assert len({row["task_id"] for row in tasks.data[0][0]}) == 3
    host.shutdown()
