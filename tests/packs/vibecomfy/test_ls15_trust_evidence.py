"""Trust-mode resident jobs retain honest model evidence on cold and warm runs."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from astrid.core.execution.resident_sessions import ResidentJobContext, _JOB
from astrid.core.generation.target_inventory import POD_ALIGN
from astrid.packs.vibecomfy.resident import VibeComfySession
from tests.packs.vibecomfy import test_ls15_resident_host

comfy = test_ls15_resident_host.comfy
resident_comfy = test_ls15_resident_host.resident_comfy
pytestmark = pytest.mark.integration


def test_cold_and_warm_trusted_resident_jobs_keep_declared_model_evidence(
    resident_comfy, tmp_path, monkeypatch,
):
    from vibecomfy.nodes.core import EmptyImage, SaveImage
    from vibecomfy.security.provenance import Provenance
    from vibecomfy.templates import InputSpec, OutputSpec, ReadyMetadata, new_workflow
    from vibecomfy.workflow_bundle import emit_bundle_with_candidate

    from astrid.packs.vibecomfy import local_server_engine

    _host, _runtime, peer, _comfy = resident_comfy
    asset = {"name": "absent-trusted.bin", "subdir": "checkpoints",
             "url": "https://example.invalid/absent-trusted.bin",
             "sha256": hashlib.sha256(b"declared bytes").hexdigest(), "size_bytes": 14}
    with new_workflow(ReadyMetadata.build(template_id="ls15-trust", capability="image",
                                         model_assets=[asset])) as workflow:
        source = EmptyImage(_id="1", width=16, height=16, batch_size=1, color=0)
        saved = SaveImage(_id="2", images=source.out("IMAGE"), filename_prefix="trusted")
        workflow.finalize({"color": InputSpec(node=source, field="color", default=0, type="INT")},
                          outputs=[OutputSpec(node=saved)])
    authored = tmp_path / "trusted-source"
    authored.mkdir()
    emit_bundle_with_candidate(workflow, authored / "workflow.py", Provenance.USER_CONFIRMED, None)
    for name, value in {"VIBECOMFY_COMFY_CONFIGURATION": "{}", "VIBECOMFY_WARM": "never",
                        "VIBECOMFY_SCHEMA_WARN_ONLY": "0"}.items():
        monkeypatch.setenv(name, value)
    resident = VibeComfySession(tmp_path / "retained")
    align_token = POD_ALIGN.set("trust")
    paths = []
    try:
        for number in (1, 2):
            destination = tmp_path / f"trust-attempt-{number}" / "outputs"
            destination.mkdir(parents=True)
            job = ResidentJobContext(
                task_id=f"trust-task-{number}", attempt_id=f"trust-attempt-{number}", fence=number,
                runtime_epoch=1, attempt_root=destination.parent, output_root=destination,
                cancelled=lambda: False, session_identity=("trusted-resident", "project"),
                capability_identity="vibecomfy.run", settings={"align": "trust", "entry": "workflow"},
                on_cancel=lambda _callback: lambda: None,
            )
            token = _JOB.set(job)
            try:
                result = resident.run(authored / "workflow.py", destination, comfy_url=peer.url,
                    task_identity=job.task_id, attempt_identity=job.attempt_id,
                    workflow_input_bindings={"color": number})
            finally:
                _JOB.reset(token)
            path = Path(result.managed_generation_result_path)
            payload = json.loads(path.read_bytes())
            diagnostics = payload["evidence"]["producer"]["runtime"]["schema_provenance"].get(
                "approval_diagnostics", [])
            rows = [row for row in diagnostics if row.get("code") == "target_model_asset"]
            assert len(rows) == 1, diagnostics
            assert rows[0]["name"] == asset["name"] and rows[0]["sha256"] == asset["sha256"]
            assert rows[0]["status"] == rows[0]["model_bytes_status"] == "declared_unverified"
            assert "verified_target_provisioning" not in json.dumps(payload)
            for kind in ("attempt", "metadata"):
                evidence = payload["evidence"]["producer"][kind]
                for document in (evidence["record"], json.loads(Path(evidence["path"]).read_bytes())):
                    assert document["schema_provenance"]["approval_diagnostics"] == rows
                    assert document["runtime_evidence"]["schema_provenance"]["approval_diagnostics"] == rows
            assert payload["task_id"] == job.task_id and payload["attempt_id"] == job.attempt_id
            assert payload["inputs"]["color"] == number
            assert payload["outputs"] and all((path.parent / row["path"]).is_file()
                                               for row in payload["outputs"])
            paths.append(path)

            def forbid_warm_approval(*_args, **_kwargs):
                raise AssertionError("a same-key warm job repeated full approval")

            monkeypatch.setattr(local_server_engine, "_approve_live", forbid_warm_approval)
        assert paths[0] != paths[1]
        assert len(peer.posts) == 2 and peer.counts()["GET /ws"] == 1
        assert peer.posts[0]["prompt"]["2"]["inputs"]["filename_prefix"] != (
            peer.posts[1]["prompt"]["2"]["inputs"]["filename_prefix"])
    finally:
        POD_ALIGN.reset(align_token)
        resident.close()
