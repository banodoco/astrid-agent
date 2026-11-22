"""A delay in real workflow input resolution is visible in command timings."""
import json
import time
from contextlib import nullcontext
from io import StringIO
from pathlib import Path

import pytest

from astrid import console
from astrid.core import gateway
from astrid.core.execution.generic_host import GenericPackHost, RuntimeProtocolClient
from astrid.core.generation import entries
from astrid.sdk import command_timing
from astrid.sdk.client import AstridClient
from tests.core.generation.backends.test_comfy_model_resolution import install_target, target_info
from tests.core.generation.test_engine_endpoint_transport import (
    engine_configuration,  # noqa: F401
    network,  # noqa: F401
    observation_configuration,  # noqa: F401
)


@pytest.mark.integration
def test_workflow_input_delay_is_attributed_on_real_admission(network, real_runtime, tmp_path, monkeypatch, capsys):  # noqa: F811
    comfy, _ = install_target(network, target_info(vaes=("ae.safetensors",)))
    daemon = real_runtime()
    client = AstridClient.open(
        endpoint=daemon.endpoint, credential=daemon.credential_path,
        realm_id=daemon.service.realm["id"], actor_id="owner",
        client_name="workflow-input-delay", client_version="test", protocol_version="workspace.v1",
    )
    project = client.projects.create(slug="workflow-input-delay", name="Workflow input delay",
                                     idempotency_key="workflow-input-delay-project")
    assert project.ok, project

    import vibecomfy
    from vibecomfy.registry.ready import workflow_from_ready
    from vibecomfy.runtime.session import current_source_content_digest, current_source_revision
    from vibecomfy.workflow_bundle import emit_bundle

    monkeypatch.setenv("ASTRID_VIBECOMFY_CHECKOUT", str(Path(vibecomfy.__file__).resolve().parents[1]))
    monkeypatch.setenv("ASTRID_VIBECOMFY_CANDIDATE_KIND", "local_snapshot")
    monkeypatch.setenv("ASTRID_VIBECOMFY_CANDIDATE_REVISION", current_source_revision())
    monkeypatch.setenv("ASTRID_VIBECOMFY_CANDIDATE_CONTENT_DIGEST", current_source_content_digest())
    host = GenericPackHost(
        pack_roots=[Path(__file__).resolve().parents[2] / "astrid" / "packs" / "vibecomfy"],
        attempt_root=tmp_path / "attempt", client=RuntimeProtocolClient(daemon.endpoint, daemon.worker_token),
    )
    host.discover()
    host.register()
    monkeypatch.setattr(AstridClient, "open_from_launcher", lambda **_kwargs: nullcontext(client))
    workflow = emit_bundle(workflow_from_ready("image/z_image_turbo"),
                           tmp_path / "workflow" / "workflow.py", {"operation": "authored"}).python_path
    command = ["generate", "workflow", str(workflow), "--placement", comfy.target.url,
               "--project", project.data["project_id"], "--prompt", "a silver paper boat",
               "--no-wait", "--json", "--timings"]

    def run():
        start = time.monotonic()
        code = console.main(command)
        wall = time.monotonic() - start
        captured = capsys.readouterr()
        assert code == 0, captured.out + captured.err
        payload = json.loads(captured.out)
        assert payload.get("ok") and payload.get("kernel_task_id"), payload
        rows = payload["timings"]
        assert abs(sum(row["seconds"] for row in rows) - wall) <= 0.1 * wall
        matching = [row for row in rows if row["stage"] == "workflow inputs"]
        assert len(matching) == 1 and matching[0]["parent"] is None, matching
        return payload["kernel_task_id"], rows, matching[0]["seconds"]

    try:
        baseline_id, baseline_rows, baseline = run()
        resolve = entries.resolve_workflow_inputs
        calls = []

        def delayed_resolve(*args, **kwargs):
            calls.append(True)
            time.sleep(1.5)
            return resolve(*args, **kwargs)

        monkeypatch.setattr(entries, "resolve_workflow_inputs", delayed_resolve)
        delayed_id, rows, delayed = run()
        assert calls == [True]
        assert delayed_id != baseline_id
        assert delayed - baseline >= 1.4, (baseline, delayed)
        monkeypatch.setattr(entries, "resolve_workflow_inputs", resolve)
        dispatch = gateway.main
        dispatch_calls = []

        def delayed_dispatch(*args, **kwargs):
            # Gateway dispatch starts before generation's TimingCollector exists.
            assert command_timing._current.get() is None
            dispatch_calls.append(True)
            time.sleep(1.5)
            return dispatch(*args, **kwargs)

        monkeypatch.setattr(gateway, "main", delayed_dispatch)
        outside_id, outside_rows, _ = run()
        assert dispatch_calls == [True]
        assert outside_id not in {baseline_id, delayed_id}
        def stage_seconds(rows, name):
            return sum(row["seconds"] for row in rows if row["stage"] == name)
        assert stage_seconds(outside_rows, "command dispatch") - stage_seconds(baseline_rows, "command dispatch") >= 1.4
        diagnostic = StringIO()
        command_timing.print_timings(rows, file=diagnostic)
        assert "workflow inputs:" in diagnostic.getvalue(), diagnostic.getvalue()
    finally:
        host.shutdown()
