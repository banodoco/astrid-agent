"""Generated H3 masks use managed file pickers without relaxing other choices."""

from __future__ import annotations

import hashlib
import importlib
import json
import threading
from email import policy
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from astrid.packs.h3_av.actions.compile.compile import compile_preparation
from astrid.packs.vibecomfy import local_server_engine, production_engine
from astrid.packs.vibecomfy.asset_manifest import resolve_inputs
from tests.packs.h3_av.test_native_masked_edit import WATERMARK, _prepare

pytestmark = pytest.mark.integration  # pinned VibeComfy and loopback HTTP


def _object_info(workflow, bindings):
    """Supply native graph ports and ComfyUI's stale mask/channel choices."""
    api = workflow.compile("api", run_inputs=bindings)
    result = {}
    for node_id, node in workflow.nodes.items():
        schema = result.setdefault(node.class_type, {
            "input": {"required": {}},
            "output": list(node.native_output_types or ()),
            "output_name": list(node.native_output_names or ()),
        })
        for name, kind in zip(node.native_input_names or (), node.native_input_types or ()):
            value = api[node_id]["inputs"].get(name)
            group = "required" if name in api[node_id]["inputs"] else "optional"
            fields = schema["input"].setdefault(group, {})
            if kind in {"COMBO", "CHOICE"} and isinstance(value, (str, int, float)):
                choices = fields.setdefault(name, [[]])[0]
                if value not in choices:
                    choices.append(value)
            else:
                fields[name] = [kind]
    result["LoadImageMask"]["input"]["required"] = {
        "image": [["old-mask.png"], {"image_upload": True}],
        "channel": [["alpha", "red", "green", "blue"]],
    }
    return result


@pytest.mark.parametrize("case", ["accepted", "invalid_channel", "unbound_picker"])
def test_generated_static_mask_staging_and_live_preflight(tmp_path, monkeypatch, case):
    from vibecomfy.security.provenance import Provenance
    from vibecomfy.workflow_bundle import load_bundle

    monkeypatch.setenv("ASTRID_INTERNAL_INVOCATION", "1")
    monkeypatch.setenv("VIBECOMFY_HEADLESS", "1")
    raw = json.loads(json.dumps(WATERMARK))
    raw["media"][0]["edit"][0]["mask"] = {"rectangle": [0, 0, 10, 10]}
    prepared = _prepare(raw, tmp_path)
    # Synthetic video bytes: stop before prompt submission or media decoding.
    for asset in prepared["assets"]:
        path = Path(asset["path"])
        asset["path"] = str(path.rename(path.with_suffix(".mp4")))
    compiled = compile_preparation(prepared, out_dir=tmp_path / "compiled")
    workflow_path = Path(compiled["workflow"]["workflow.py"]["path"])
    bundle = load_bundle(workflow_path, trust=Provenance.USER_CONFIRMED)
    output_root = tmp_path / "attempt" / "outputs"
    action = importlib.import_module("astrid.packs.vibecomfy.actions.run.run")
    staged = action._stage_managed_asset_manifest(
        compiled["managed_assets"]["path"], readiness_profile=None, output_root=output_root,
    )
    bindings = resolve_inputs(staged.manifest, compiled["workflow_inputs"])
    mask_record = next(row for row in staged.manifest["assets"] if row["binding"] == "mask_0")
    staged_name = bindings["mask_0"]
    assert staged_name == f"{mask_record['sha256'][:16]}-resolved-mask.png"
    mask_bytes = (output_root / "engine-input" / staged_name).read_bytes()
    assert hashlib.sha256(mask_bytes).hexdigest() == mask_record["sha256"]
    mask_spec = bundle.workflow.inputs["mask_0"]
    assert mask_spec.field == "image" and mask_spec.media_semantics == "image"
    schema = _object_info(bundle.workflow, bindings)
    if case == "invalid_channel":
        schema["LoadImageMask"]["input"]["required"]["channel"] = [["alpha"]]
    calls, uploads, queue_records = [], [], []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def send(self, payload):
            data = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            calls.append(("GET", self.path))
            self.send(schema if self.path == "/object_info" else {})

        def do_POST(self):
            calls.append(("POST", self.path))
            assert self.path == "/upload/image", "this test must never submit a prompt"
            raw = self.rfile.read(int(self.headers["Content-Length"]))
            message = BytesParser(policy=policy.default).parsebytes(
                ("Content-Type: " + self.headers["Content-Type"] + "\r\n\r\n").encode() + raw,
            )
            fields = {part.get_param("name", header="content-disposition"): part
                      for part in message.iter_parts()}
            name = fields["image"].get_filename()
            subfolder = fields["subfolder"].get_payload(decode=True).decode()
            returned = Path(name).stem + "-server" + Path(name).suffix
            uploads.append((name, subfolder, fields["image"].get_payload(decode=True), returned))
            self.send({"name": returned, "subfolder": subfolder, "type": "input"})

    class QueueBoundaryReached(Exception):
        pass

    def stop_at_queue(record, queue_bundle, **kwargs):
        api = queue_bundle.workflow.compile("api", run_inputs=record.input_binding)
        mask = api[mask_spec.node_id]
        assert mask["class_type"] == "LoadImageMask"
        returned = next(row[3] for row in uploads if row[0] == staged_name)
        assert mask["inputs"] == {"image": f"astrid/task-1/{returned}", "channel": "red"}
        record.assert_matches(
            queue_bundle, run_inputs=record.input_binding, api_projection=api,
            ui_projection=queue_bundle.materialize_ui(schema_provider=kwargs["schema_provider"]),
        )
        queue_records.append(record)
        raise QueueBoundaryReached

    monkeypatch.setattr(importlib.import_module("vibecomfy.runtime.run"), "run_sync", stop_at_queue)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}"
    try:
        if case == "unbound_picker":
            unbound = {name: value for name, value in bindings.items() if name != "mask_0"}
            with pytest.raises(production_engine.ProductionEngineError, match="LoadImageMask.*image.*declared choices"):
                local_server_engine._live_preflight(bundle, unbound, url, output_root)
        else:
            expected = QueueBoundaryReached if case == "accepted" else production_engine.ProductionEngineError
            match = None if case == "accepted" else "LoadImageMask.*channel.*declared choices"
            with pytest.raises(expected, match=match):
                production_engine.run_workflow_result_path(
                    workflow_path, output_root, task_identity="task-1", comfy_url=url,
                    profile_id="local_server", workflow_input_bindings=bindings,
                )
        if case == "accepted":
            assert len(queue_records) == 1
            mask_upload = next(row for row in uploads if row[0] == staged_name)
            assert mask_upload[1:3] == ("astrid/task-1", mask_bytes)
            assert len(uploads) == len(staged.manifest["assets"])
            assert calls.index(("GET", "/object_info")) < calls.index(("POST", "/upload/image"))
        else:
            assert not uploads and not queue_records
        assert (output_root / "engine-input" / staged_name).read_bytes() == mask_bytes
        assert compiled["workflow_inputs"]["mask_0"] == staged_name
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
