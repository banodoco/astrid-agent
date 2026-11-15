"""R4.A: the generation CLI fixes (scan route, readiness by placement, defaults restore, unset placement)."""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from astrid.core.generation import cli, defaults, listing, placement as placements, settings, targets
from astrid.sdk.generation import GenerationFacade
from tests.packs import test_vibecomfy_local as fake_scan


class _Client:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def run(monkeypatch, capsys, tmp_path, generation_settings_store):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("VIBECOMFY_COMFYUI_URL", raising=False)
    monkeypatch.setattr(cli, "_open_client", lambda: _Client())
    monkeypatch.setattr(settings, "transport_of", lambda client: generation_settings_store)

    def invoke(argv):
        assert cli.main(argv) == 0
        return capsys.readouterr().out

    return invoke


# --- R4.1: `astrid generate scan` reaches the scan through the launcher's entry point -----------------

def test_generate_scan_runs_through_the_launcher_entry_point(tmp_path):
    server, thread = fake_scan._server(tmp_path)
    try:
        port = server.server_address[1]
        # The launcher runs `python -I -m astrid.omp_agent ARGS`; -I drops cwd and PYTHONPATH, so the
        # package must come from the interpreter's own install, as on a real machine.
        done = subprocess.run(
            [sys.executable, "-I", "-m", "astrid.omp_agent", "generate", "scan", "--ports", str(port), "--json"],
            cwd=tmp_path, capture_output=True, text=True, timeout=60,
            env={k: v for k, v in os.environ.items() if k != "PYTHONPATH"} | {"HOME": str(tmp_path)})
        requested = list(fake_scan._ComfyHandler.requested)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
    assert done.returncode == 0, done.stderr
    payload = json.loads(done.stdout)
    [found] = [s for s in payload["servers"] if s["url"] == f"http://127.0.0.1:{port}"]
    assert [w["name"] for w in found["workflows_detail"]] == ["portrait_flux.json", "nested/wan_i2v.json"]
    assert requested  # the scan did probe the fake ComfyUI


def test_generate_scan_matches_the_pack_module_and_only_reads(tmp_path, capsys):
    server, thread = fake_scan._server(tmp_path)
    methods: list[str] = []
    original = fake_scan._ComfyHandler.do_GET
    handler = fake_scan._ComfyHandler
    for name in ("do_POST", "do_PUT", "do_DELETE"):
        setattr(handler, name, lambda self, _n=name: methods.append(_n) or self.send_error(405))
    try:
        port = server.server_address[1]
        assert cli.main(["scan", "--ports", str(port), "--json"]) == 0
        via_generate = json.loads(capsys.readouterr().out)
        assert fake_scan.local_scan.main(["scan", "--ports", str(port)]) == 0
        via_module = json.loads(capsys.readouterr().out)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        for name in ("do_POST", "do_PUT", "do_DELETE"):
            delattr(handler, name)
        assert handler.do_GET is original
    assert via_generate == via_module
    assert methods == []


# --- R4.4: readiness comes from the row of the placement that will run -------------------------------

def _row(target, ready, reason, entry="qwen-image-2.1-turbo"):
    return {"id": entry, "kind": "image", "mode": "t2i", "target": target, "target_kind": "attached",
            "ready": ready, "reason": reason}


def _slot(monkeypatch, rows, endpoint):
    monkeypatch.setattr(listing, "generation_rows", lambda kind=None: rows)
    record = targets.normalize_target({"endpoint": endpoint}) if endpoint else None
    monkeypatch.setattr(listing, "target_selection",
                        lambda client=None, request="auto", repo=None: targets.TargetSelection(
                            record, "settings" if endpoint else "default", bool(endpoint)))
    return {row["slot"]: row for row in defaults.listing()["slots"]}["image.t2i"]


def test_ready_elsewhere_does_not_make_the_slot_ready(monkeypatch, generation_settings_store):
    rows = [_row("http://127.0.0.1:8000", True, "models present"),
            _row("http://127.0.0.1:8188", False, "required models missing or ambiguous")]
    slot = _slot(monkeypatch, rows, "http://127.0.0.1:8188")
    assert (slot["ready"], slot["reason"]) == (False, "required models missing or ambiguous")


def test_a_matching_row_gives_its_ready_and_reason(monkeypatch, generation_settings_store):
    rows = [_row("http://127.0.0.1:8000", False, "required models missing or ambiguous"),
            _row("http://127.0.0.1:8188", True, "required model roles present (model bytes unverified)")]
    slot = _slot(monkeypatch, rows, "http://127.0.0.1:8188")
    assert (slot["ready"], slot["reason"]) == (True, "required model roles present (model bytes unverified)")


def test_managed_placement_is_not_established(monkeypatch, generation_settings_store):
    slot = _slot(monkeypatch, [_row("http://127.0.0.1:8188", True, "models present")], None)
    assert slot["ready"] is False
    assert slot["reason"] == "not established for the selected placement (managed)"


def test_runpod_placement_is_not_established(monkeypatch, generation_settings_store):
    settings.update(lambda document: document.__setitem__("placement", {"kind": "runpod"}))
    slot = _slot(monkeypatch, [_row("http://127.0.0.1:8188", True, "models present")], None)
    assert slot["ready"] is False
    assert slot["reason"] == "not established for the selected placement (runpod)"


# --- R4.5: `defaults set` reports the pin it replaced and how to restore it --------------------------

def test_defaults_set_reports_the_previous_pin_and_restore_restores_it(run, generation_settings_store):
    run(["defaults", "set", "image", "t2i", "z-image"])
    out = json.loads(run(["defaults", "set", "image", "t2i", "qwen-image-2512", "--json"]))
    assert out["previous"] == {"pack": "generation", "entry": "z-image"}
    assert out["restore"] == "astrid generate defaults set image t2i z-image"
    assert out["restore"].startswith("astrid generate ")
    run(out["restore"].split()[2:])
    assert generation_settings_store.documents["generation"][0]["defaults"] == {
        "image.t2i": {"pack": "generation", "entry": "z-image"}}


def test_defaults_set_with_no_pin_restores_by_unset(run, generation_settings_store):
    out = json.loads(run(["defaults", "set", "image", "t2i", "z-image", "--json"]))
    assert out["previous"] is None
    assert out["restore"] == "astrid generate defaults unset image t2i"
    run(out["restore"].split()[2:])
    assert generation_settings_store.documents["generation"][0]["defaults"] == {}


def test_defaults_set_text_shows_previous_and_restore(run):
    run(["defaults", "set", "image", "t2i", "z-image"])
    text = run(["defaults", "set", "image", "t2i", "qwen-image-2512"])
    assert "Previous pin: generation/z-image; restore: astrid generate defaults set image t2i z-image" in text
    run(["defaults", "unset", "image", "t2i"])
    assert "Previous pin: none; restore: astrid generate defaults unset image t2i" in run(
        ["defaults", "set", "image", "t2i", "z-image"])


# --- R4.13: unset placement drops the whole place, keeps policy --------------------------------------

def test_unset_placement_drops_machine_and_engine_and_keeps_policy(generation_settings_store):
    policy = {"idle_timeout_s": 30.0}
    settings.update(lambda document: document.__setitem__(
        "placement", {"kind": "runpod", "machine": "m1", "engine": "comfyui", "device": 0, "policy": policy}))
    result = settings.unset_placement()
    assert result["placement"] == {"policy": policy}
    assert result["previous"]["machine"] == "m1"
    assert generation_settings_store.documents["generation"][0]["placement"] == {"policy": policy}


# --- P4.2: a RunPod placement is untouched by the attached-placement commands ------------------------

def test_runpod_placement_resolution_and_targets_are_unchanged(run, generation_settings_store, tmp_path):
    config = tmp_path / ".vibecomfy" / "config.toml"
    config.parent.mkdir()
    config.write_bytes(b'[comfyui]\nurl = "http://127.0.0.1:18111"\n# keep me\n')
    before = config.read_bytes()
    settings.update(lambda document: document.__setitem__("placement", {"kind": "runpod"}))
    assert placements.resolve_placement().kind == "runpod"
    first = GenerationFacade().targets()
    assert GenerationFacade().targets() == first
    run(["settings", "set", "placement", "http://127.0.0.1:9123"])
    run(["settings", "unset", "placement"])
    assert config.read_bytes() == before


# --- P4.4: describe FILE --placement URL is loopback-only, read-only and a plain local read -----------

class _Recorder:
    def __init__(self):
        self.requests: list[tuple[str, str]] = []
        rec = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_a):
                return

            def do_GET(self):
                rec.requests.append(("GET", self.path))
                body = json.dumps({"system": {}, "devices": []} if "system_stats" in self.path else {}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                rec.requests.append(("POST", self.path))
                self.send_error(405)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


def _tree(root: Path):
    return sorted((str(p.relative_to(root)), p.is_symlink(), p.stat().st_mtime_ns if p.is_file() else 0)
                  for p in root.rglob("*"))


def test_describe_refuses_a_non_loopback_placement_and_sends_nothing(run, tmp_path, monkeypatch, capsys):
    workflow = tmp_path / "w.json"
    workflow.write_text("{}")
    attempts: list[object] = []
    monkeypatch.setattr(socket.socket, "connect", lambda self, address: attempts.append(address) or (_ for _ in ()).throw(OSError("blocked")))
    monkeypatch.setattr(socket, "create_connection", lambda address, *a, **k: attempts.append(address) or (_ for _ in ()).throw(OSError("blocked")))
    assert cli.main(["describe", str(workflow), "--placement", "http://example.com:9123", "--json"]) != 0
    assert "placement must be http://127.0.0.1:<port> or http://localhost:<port>" in capsys.readouterr().out
    assert attempts == []


def test_describe_by_a_symlinked_or_dotdot_path_is_a_plain_read_only_read(run, tmp_path, capsys):
    from tests.core.generation import test_user_workflow_attached as attached
    fake, path = attached._serve(tmp_path)
    try:
        (tmp_path / "sub").mkdir()
        (tmp_path / "link.json").symlink_to(path)
        for spelled in (tmp_path / "link.json", tmp_path / "sub" / ".." / path.name):
            before = _tree(tmp_path)
            assert cli.main(["describe", str(spelled), "--placement", fake.url, "--json"]) == 0
            description = json.loads(capsys.readouterr().out)
            assert "seed" in {item["name"] for item in description["inputs"]}
            assert _tree(tmp_path) == before
        assert fake.methods() == {"GET"}
    finally:
        fake.stop()
