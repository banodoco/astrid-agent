"""Real generate command in a fresh interpreter against an attached fake Comfy."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
from aiohttp import web

from tests.core.generation.backends.test_comfy_model_resolution import (
    install_target,
    target_info,
)
from tests.core.generation.test_engine_endpoint_transport import (
    engine_configuration,
    network,
    observation_configuration,
)  # noqa: F401


ROOT = Path(__file__).resolve().parents[2]
UNRELATED_MODULES = (
    "astrid.core.gateway.hivemind",
    "astrid.core.generation.backends.codex",
    "astrid.core.generation.backends.fal",
)

PROBE = r"""
import json
import sys

names = set(json.loads(sys.argv[1]))
before = set(sys.modules)
requested = set()


class RecordImports:
    def find_spec(self, fullname, path=None, target=None):
        if fullname in names:
            requested.add(fullname)
        return None


sys.meta_path.insert(0, RecordImports())
try:
    from astrid import console
    rc = console.main(sys.argv[2:])
finally:
    after = set(sys.modules)
    print(
        "GENERATION_IMPORT_PROBE " + json.dumps({
            "names": sorted(names),
            "before_count": len(before),
            "after_count": len(after),
            "loaded_before": sorted(names & before),
            "loaded_after": sorted(names & after),
            "requested": sorted(requested),
        }),
        file=sys.stderr,
    )
raise SystemExit(rc)
"""


@pytest.mark.integration
def test_generate_attached_does_not_load_unrelated_modules(
    real_runtime, network, monkeypatch, tmp_path,
):
    from astrid.core.execution.generic_host import GenericPackHost, RuntimeProtocolClient
    from astrid.sdk.client import AstridClient
    import vibecomfy
    from vibecomfy.runtime.session import (
        current_source_content_digest,
        current_source_revision,
    )

    comfy, requests = install_target(network, target_info())
    served_target = network["handler"]

    async def serve_target(request):
        if request.method == "GET" and request.path == "/internal/folder_paths":
            return web.json_response({
                "diffusion_models": ["/srv/ComfyUI/models/diffusion_models"],
                "vae": ["/srv/ComfyUI/models/vae"],
            })
        return await served_target(request)

    network["handler"] = serve_target
    daemon = real_runtime()
    client = AstridClient.open(
        endpoint=daemon.endpoint,
        credential=daemon.credential_path,
        realm_id=daemon.service.realm["id"],
        actor_id="owner",
        client_name="generate-import-probe",
        client_version="test",
        protocol_version="workspace.v1",
    )
    project = client.projects.create(
        slug="import-probe", name="Import probe", idempotency_key="import-probe",
    )
    assert project.ok, project
    for name, value in {
        "ASTRID_VIBECOMFY_CHECKOUT": str(Path(vibecomfy.__file__).resolve().parents[1]),
        "ASTRID_VIBECOMFY_CANDIDATE_KIND": "local_snapshot",
        "ASTRID_VIBECOMFY_CANDIDATE_REVISION": current_source_revision(),
        "ASTRID_VIBECOMFY_CANDIDATE_CONTENT_DIGEST": current_source_content_digest(),
    }.items():
        monkeypatch.setenv(name, value)

    host = GenericPackHost(
        pack_roots=[ROOT / "astrid" / "packs" / "vibecomfy"],
        attempt_root=tmp_path / "import-attempt",
        client=RuntimeProtocolClient(daemon.endpoint, daemon.worker_token),
    )
    try:
        host.discover()
        host.register()
        # Use the real CLI's explicit case-local connection, rather than boot
        # another Runtime in the subprocess. Generation and target I/O are real.
        env = os.environ.copy()
        env.update(
            ASTRID_TIMELINE_EVAL_ENDPOINT=daemon.endpoint,
            ASTRID_TIMELINE_EVAL_CREDENTIAL=str(daemon.credential_path),
            ASTRID_TIMELINE_EVAL_REALM_ID=daemon.service.realm["id"],
            ASTRID_TIMELINE_EVAL_ACTOR_ID="owner",
        )
        env["PYTHONPATH"] = os.pathsep.join(
            p for p in (str(ROOT), env.get("PYTHONPATH")) if p
        )
        result = subprocess.run(
            [
                sys.executable, "-c", PROBE, json.dumps(UNRELATED_MODULES),
                "generate", "image", "a red square on a plain background",
                "--entry", "z-image", "--mode", "t2i",
                "--placement", comfy.target.url,
                "--project", project.data["project_id"], "--no-wait", "--json",
            ],
            cwd=ROOT, env=env, capture_output=True, text=True, timeout=180,
        )
        marker = "GENERATION_IMPORT_PROBE "
        reports = [
            json.loads(line[len(marker):])
            for line in result.stderr.splitlines()
            if line.startswith(marker)
        ]
        assert len(reports) == 1, result.stderr
        report = reports[0]
        print(marker + json.dumps(report))
        assert result.returncode == 0, (result.stdout, result.stderr)
        payload = json.loads(result.stdout)
        assert payload.get("ok", True) is True, payload
        assert ("GET", "/object_info") in requests, requests
        tasks = client.tasks.list(project.data["project_id"])
        assert tasks.ok, tasks
        assert len(tasks.data[0]) == 1, tasks
        assert report["names"] == sorted(UNRELATED_MODULES)
        assert not report["loaded_before"], report
        assert not report["loaded_after"], report
        assert not report["requested"], report
    finally:
        host.shutdown()
