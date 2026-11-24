"""Walk the "Set up my generation" commands against a fake ComfyUI, CPU only.

Covers the steps that exist on this branch: discover (scan), preview a saved
workflow as an entry without writing it, and describe it. The defaults steps
(`defaults set`/`unset` and the undo check) need `generate defaults` from Plan 32
T6d and are added when that lands, not as a skip.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from astrid.core.generation import cli
from astrid.packs.vibecomfy import local as local_scan
from tests.packs.test_vibecomfy_local import _server


def _run(capsys, argv: list[str]) -> tuple[int, object]:
    code = cli.main(argv)
    out = capsys.readouterr().out
    return code, json.loads(out) if out.strip() else None


def test_setup_walk_discover_preview_and_undo(tmp_path: Path, monkeypatch, capsys, loopback_comfy) -> None:
    server, thread = _server(tmp_path)
    try:
        # 1. Discover: the fake ComfyUI and its two saved workflows, read-only.
        scan = local_scan.scan([server.server_address[1]])
        assert scan["errors"] == []
        found = scan["servers"][0]
        assert found["workflows"] == ["portrait_flux.json", "nested/wan_i2v.json"]
        assert found["manager_present"] is True
        assert found["url"] == f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    # 2. Preview a saved workflow as an entry: validates, writes nothing, runs nothing.
    entries_dir = tmp_path / "entries"
    monkeypatch.setenv("VIBECOMFY_ENTRIES_DIR", str(entries_dir))
    monkeypatch.setattr(cli, "_open_client", lambda: pytest.fail("preview opened Runtime"))
    from vibecomfy.registry.ready import workflow_from_ready
    from vibecomfy.workflow_bundle import load_bundle

    workflow = tmp_path / "portrait_flux.json"
    workflow.write_text(json.dumps(load_bundle(workflow_from_ready("image/z_image_turbo")).materialize_ui()))
    code, preview = _run(capsys, ["workflow", str(workflow), "--as-entry", "portrait_flux",
                                  "--placement", loopback_comfy.url, "--dry-run", "--json"])
    assert code == 0, preview
    assert preview["written"] is False and preview["would_save_entry"] == "portrait_flux"
    assert preview["status"] in {"ready", "ready_with_warnings"}
    assert any(call[:2] == ("GET", "/object_info") for call in loopback_comfy.calls)
    assert not any(call[0] == "POST" for call in loopback_comfy.calls)
    assert not loopback_comfy.prompts
    assert not entries_dir.exists()

    # 3. Describe the saved workflow, as the skill tells the user to.
    code, described = _run(capsys, ["describe", str(workflow), "--json"])
    assert code == 0 and described is not None


def test_as_entry_no_run_saves_and_queues_nothing(
    tmp_path: Path, monkeypatch, capsys, loopback_comfy, ready_workflow_schema,
) -> None:
    from vibecomfy.entries import load_entry
    from vibecomfy.registry.ready import workflow_from_ready
    from vibecomfy.workflow_bundle import load_bundle

    entries_dir = tmp_path / "entries"
    monkeypatch.setenv("VIBECOMFY_ENTRIES_DIR", str(entries_dir))
    monkeypatch.setattr(cli, "_open_client", lambda: pytest.fail("--no-run opened Runtime"))
    monkeypatch.setattr(cli, "_generate_facade", lambda: pytest.fail("--no-run ran the workflow"))

    ready = workflow_from_ready("image/flux2_klein_4b_t2i")
    bundle = load_bundle(ready)
    loopback_comfy.schema = ready_workflow_schema(bundle.workflow)
    workflow = tmp_path / "lighthouse.json"
    workflow.write_text(json.dumps(bundle.materialize_ui()))
    before = workflow.read_bytes()
    code, saved = _run(capsys, ["workflow", str(workflow), "--as-entry", "lighthouse",
                                "--no-run", "--placement", loopback_comfy.url, "--json"])

    assert code == 0, saved
    assert saved == {"saved": True, "ran": False, "entry": "lighthouse", "replaced": False}
    seen = [(method, path) for method, path, _ in loopback_comfy.calls]
    assert [request for request in seen if request[1].startswith("/prompt")] == []
    assert seen == [("GET", "/object_info")]
    assert not any(method == "POST" for method, _ in seen)
    assert not loopback_comfy.prompts
    assert entries_dir.exists() and any(entries_dir.iterdir())
    entry = load_entry(entries_dir / "lighthouse")
    assert (Path(entry["bundle"]) / "source.json").read_bytes() == before
    assert workflow.read_bytes() == before


@pytest.mark.parametrize("flag", ["--no-run", "--dry-run"])
@pytest.mark.parametrize(("status", "response"), [
    (404, b""),
    (200, b'{"SaveImage":42}'),
    (200, b"[]"),
    (200, b"{}"),
    (200, b"not-json"),
], ids=["404", "malformed-row", "not-object", "empty-schema", "invalid-json"])
def test_as_entry_refuses_unavailable_or_malformed_attached_schema(
    tmp_path: Path, monkeypatch, capsys, flag, status, response,
) -> None:
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from vibecomfy.porting import import_service

    seen = []

    class Unavailable(BaseHTTPRequestHandler):
        def reply(self):
            seen.append((self.command, self.path))
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)

        do_GET = do_POST = reply

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Unavailable)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    target = f"http://127.0.0.1:{server.server_address[1]}"
    root = tmp_path / "entries"
    monkeypatch.setenv("VIBECOMFY_ENTRIES_DIR", str(root))
    monkeypatch.setattr(cli, "_open_client", lambda: pytest.fail("schema refusal opened Runtime"))
    monkeypatch.setattr(cli, "_generate_facade", lambda: pytest.fail("schema refusal entered generation"))
    monkeypatch.setattr(import_service, "import_workflow_bytes",
                        lambda *a, **kw: pytest.fail("schema refusal entered the importer"))
    workflow = tmp_path / "input.json"
    before = json.dumps({
        "1": {"class_type": "LoadImage", "inputs": {"image": "input.png"}},
        "2": {"class_type": "SaveImage", "inputs": {"images": ["1", 0], "filename_prefix": "out"}},
    }).encode()
    workflow.write_bytes(before)
    try:
        code, payload = _run(capsys, ["workflow", str(workflow), "--as-entry", "mine",
                                      "--placement", target, flag, "--json"])
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert code == 2, payload
    assert payload["status"] == "blocked"
    issue = payload["issues"][0]
    assert issue["code"] == "target_schema_unavailable"
    assert issue["severity"] == "error"
    assert issue["details"]["field"] == "target"
    assert issue["details"]["endpoint"] == target
    assert issue["fix_ids"] == ["inspect-attached-schema"]
    assert payload["fixes"][0]["who"] == "human"
    assert payload["fixes"][0]["scope"] == "read_only"
    assert payload["fixes"][0]["instruction"]["then_argv"] == [
        "astrid", "generate", "targets", "--placement", target, "--json",
    ]
    assert seen == [("GET", "/object_info")]
    assert not root.exists()
    assert workflow.read_bytes() == before

def test_setup_walk_defaults_pin_then_undo_restores_previous_state(monkeypatch, capsys, generation_settings_store):
    from astrid.core.generation import cli, settings

    class Client:
        def __enter__(self): return self
        def __exit__(self, *exc): return False

    monkeypatch.setattr(cli, "_open_client", lambda: Client())
    monkeypatch.setattr(settings, "transport_of", lambda client: generation_settings_store)

    def slots() -> dict:
        assert cli.main(["defaults", "--json"]) == 0
        rows = json.loads(capsys.readouterr().out)["slots"]
        # Compare what each slot resolves to and why; the document version in `origin` moves on every write.
        return {row["slot"]: (row["entry"], row["source"]) for row in rows}

    before = slots()
    assert cli.main(["defaults", "set", "image", "t2i", "z-image", "--json"]) == 0
    capsys.readouterr()
    assert slots()["image.t2i"] == ("z-image", "user pin")
    assert cli.main(["defaults", "unset", "image", "t2i", "--json"]) == 0
    capsys.readouterr()
    assert slots() == before
