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


def test_setup_walk_discover_preview_and_undo(tmp_path: Path, monkeypatch, capsys) -> None:
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
    monkeypatch.setattr(cli, "_generate_facade", lambda: pytest.fail("preview ran the workflow"))
    from vibecomfy.registry.ready import workflow_from_ready
    from vibecomfy.workflow_bundle import load_bundle

    workflow = tmp_path / "portrait_flux.json"
    workflow.write_text(json.dumps(load_bundle(workflow_from_ready("image/z_image_turbo")).materialize_ui()))
    code, preview = _run(capsys, ["workflow", str(workflow), "--as-entry", "portrait_flux",
                                  "--dry-run", "--json"])
    assert code == 0
    assert preview["written"] is False and preview["would_save_entry"] == "portrait_flux"
    assert not entries_dir.exists()

    # 3. Describe the saved workflow, as the skill tells the user to.
    code, described = _run(capsys, ["describe", str(workflow), "--json"])
    assert code == 0 and described is not None


def test_as_entry_no_run_saves_and_queues_nothing(tmp_path: Path, monkeypatch, capsys) -> None:
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import threading

    seen: list[tuple[str, str]] = []

    class _Recording(BaseHTTPRequestHandler):
        def _record(self) -> None:
            seen.append((self.command, self.path))
            self.send_error(404)

        do_GET = do_POST = _record

        def log_message(self, _format: str, *_args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Recording)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    entries_dir = tmp_path / "entries"
    monkeypatch.setenv("VIBECOMFY_ENTRIES_DIR", str(entries_dir))
    monkeypatch.setattr(cli, "_open_client", lambda: pytest.fail("--no-run opened Runtime"))
    monkeypatch.setattr(cli, "_generate_facade", lambda: pytest.fail("--no-run ran the workflow"))
    from vibecomfy.registry.ready import workflow_from_ready
    from vibecomfy.workflow_bundle import load_bundle

    workflow = tmp_path / "lighthouse.json"
    workflow.write_text(json.dumps(load_bundle(workflow_from_ready("image/flux2_klein_4b_t2i")).materialize_ui()))
    try:
        target = f"http://127.0.0.1:{server.server_address[1]}"
        code, saved = _run(capsys, ["workflow", str(workflow), "--as-entry", "lighthouse",
                                    "--no-run", "--placement", target, "--json"])
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert code == 0
    assert saved == {"saved": True, "ran": False, "entry": "lighthouse", "replaced": False}
    assert [request for request in seen if request[1].startswith("/prompt")] == []
    assert seen == []
    assert entries_dir.exists() and any(entries_dir.iterdir())
