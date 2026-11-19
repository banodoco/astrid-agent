"""Verified managed exports retain ordinary caller-umask permissions."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


pytestmark = pytest.mark.integration


_EXPORT_PROBE = r"""
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import sys
from types import SimpleNamespace

from PIL import Image
from astrid_runtime_client import WorkspaceClient as GeneratedClient
import astrid_runtime_client.generated as generated
from astrid.core.generation.delivery import deliver_generation
from astrid.sdk.remote import RemoteMedia
from astrid.sdk.workspace_client import WorkspaceClient

root = Path(sys.argv[1])
mask = int(sys.argv[2])
endpoint = os.environ["ASTRID_EXPORT_TEST_ENDPOINT"]
token = os.environ["ASTRID_EXPORT_TEST_TOKEN"]
api = GeneratedClient(endpoint, token)
project = api.create_project("Export permissions", slug="export-permissions",
                             idempotency_key="export-project")
image = io.BytesIO()
Image.new("RGB", (4, 3), (31, 73, 127)).save(image, format="PNG")
data = image.getvalue()
stored = api.ingest_project_object(project.project_id, data,
    media_type="image/png", filename="picture.png", idempotency_key="export-picture")
object_id = stored["digest"]
manifest = json.dumps({"kind": "managed-generation-result.v1", "schema_version": 1,
    "outputs": [{"path": "outputs/picture.png", "sha256": object_id.removeprefix("sha256:"),
                 "bytes": len(data), "media_type": "image/png", "role": "result"}]}).encode()
manifest_id = api.ingest_project_object(project.project_id, manifest,
    media_type="application/json", filename="manifest.json",
    idempotency_key="export-manifest")["digest"]
transfers = []

class RecordingWorkspaceClient(WorkspaceClient):
    def get_object_file(self, object_id, **kwargs):
        receipt = super().get_object_file(object_id, **kwargs)
        assert receipt["stream_verified"] is True, receipt
        transfers.append({"object_id": object_id, "digest": receipt["digest"],
                          "size": receipt["size"], "stream_verified": receipt["stream_verified"]})
        return receipt

transport = RecordingWorkspaceClient(endpoint, token)
client = SimpleNamespace(media=RemoteMedia(transport))
payload = {"task_id": "permission-fixture", "manifest_ref": manifest_id,
           "manifest": {"outputs": [{"object_id": object_id, "digest": object_id,
               "size": len(data), "filename": "picture.png", "media_type": "image/png",
               "project_id": project.project_id, "role": "result"}]}}
destination = root / "delivered.png"
ordinary = root / "ordinary.png"
previous_umask = os.umask(mask)
try:
    ordinary.write_bytes(data)
    result = deliver_generation(payload, client=client, out=destination,
                                project=project.project_id)
finally:
    os.umask(previous_umask)
assert result["image_paths"] == [str(destination)]
assert destination.read_bytes() == data
assert destination.stat().st_nlink == 1
assert [item["object_id"] for item in transfers] == [manifest_id, object_id]
assert [item["size"] for item in transfers] == [len(manifest), len(data)]
assert all(item["digest"] == item["object_id"] for item in transfers)
mode = stat.S_IMODE(destination.stat().st_mode)
ordinary_mode = stat.S_IMODE(ordinary.stat().st_mode)
destination.write_bytes(b"caller changed its exported copy")
assert api.get_object(object_id).data == data
assert api.get_object(manifest_id).data == manifest
assert sorted(path.name for path in root.iterdir()) == ["delivered.png", "ordinary.png"]
print(json.dumps({"mask": mask, "mode": mode, "ordinary_mode": ordinary_mode,
    "independent_file": True, "cas_unchanged_after_caller_mutation": True,
    "transfers": transfers, "generated_sdk_path": generated.__file__,
    "generated_sdk_sha256": hashlib.sha256(Path(generated.__file__).read_bytes()).hexdigest()},
    sort_keys=True))
"""


@pytest.mark.parametrize("mask", [0o022, 0o027, 0o077], ids=["022", "027", "077"])
def test_verified_managed_export_honors_caller_umask(tmp_path, real_runtime, mask):
    daemon = real_runtime()
    caller = tmp_path / "caller"
    caller.mkdir()
    environment = os.environ.copy()
    environment["ASTRID_EXPORT_TEST_ENDPOINT"] = daemon.endpoint
    environment["ASTRID_EXPORT_TEST_TOKEN"] = daemon.token
    agent_root = Path(__file__).resolve().parents[3]
    python_path = [part for part in environment.get("PYTHONPATH", "").split(os.pathsep) if part]
    environment["PYTHONPATH"] = os.pathsep.join([*python_path, str(agent_root)])
    completed = subprocess.run(
        [sys.executable, "-c", _EXPORT_PROBE, str(caller), str(mask)],
        cwd=caller, env=environment, capture_output=True, text=True,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    observed = json.loads(completed.stdout)
    print(json.dumps(observed, sort_keys=True))
    assert observed["mask"] == mask
    assert observed["independent_file"] is True
    assert observed["cas_unchanged_after_caller_mutation"] is True
    assert len(observed["transfers"]) == 2
    assert all(receipt["stream_verified"] is True for receipt in observed["transfers"])
    assert observed["mode"] == observed["ordinary_mode"], observed
    if os.name == "posix":
        assert observed["mode"] == 0o666 & ~mask
