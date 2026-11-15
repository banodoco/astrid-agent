"""Readiness and placement fields on the existing defaults, list and targets envelopes."""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

from astrid.core.generation import cli, defaults, listing, settings
from astrid.packs.vibecomfy import local
from astrid.sdk.generation import GenerationFacade


@pytest.fixture
def attached_comfy(monkeypatch):
    """A fake loopback ComfyUI on an ephemeral port, reachable only when the scan asks for that port."""
    from tests.core.generation.backends.test_comfy_model_resolution import target_info

    schema = target_info()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/system_stats":
                body = {"system": {"os": "posix", "argv": []}, "devices": []}
            elif self.path == "/object_info":
                body = schema
            else:
                self.send_error(404)
                return
            data = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_port
    probed = []
    real_scan = local.scan

    def scan(ports):
        probed.append(list(ports))
        return real_scan([p for p in ports if p == port])

    monkeypatch.setattr(local, "_bounded_pinokio_discovery", lambda *args: (None, {}, [], []))
    monkeypatch.setattr(local, "scan", scan)
    monkeypatch.delenv("VIBECOMFY_COMFYUI_URL", raising=False)
    yield SimpleNamespace(port=port, url=f"http://127.0.0.1:{port}", probed=probed)
    server.shutdown()
    thread.join()
    server.server_close()


def _run_cli(monkeypatch, capsys, argv, store):
    class Client:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(cli, "_open_client", lambda: Client())
    monkeypatch.setattr(settings, "transport_of", lambda client: store)
    assert cli.main(argv) == 0
    return json.loads(capsys.readouterr().out)


def _matching_rows(rows, slot):
    return [row for row in rows if (row["id"], row["kind"], row["mode"]) == (slot["entry"], slot["kind"], slot["mode"])]


def test_each_defaults_slot_carries_the_ready_and_reason_of_its_list_row(monkeypatch, capsys, generation_settings_store):
    # No attached ComfyUI: every local row is unready for the same reason in both envelopes.
    monkeypatch.setattr(listing, "_targets", lambda: [])
    payload = _run_cli(monkeypatch, capsys, ["defaults", "--json"], generation_settings_store)
    rows = listing.generation_rows()
    checked = 0
    for slot in payload["slots"]:
        assert "ready" in slot and "reason" in slot, slot["slot"]
        if not slot["entry"]:
            assert slot["ready"] is False
            continue
        matches = _matching_rows(rows, slot)
        assert matches, f"no list row for {slot['slot']} entry {slot['entry']}"
        assert slot["ready"] is any(row["ready"] for row in matches), slot["slot"]
        ready_row = next((row for row in matches if row["ready"]), matches[0])
        assert slot["reason"] == ready_row["reason"], slot["slot"]
        checked += 1
    assert checked >= 3


def test_list_probes_the_runtime_placement_port_and_targets_reports_it(monkeypatch, attached_comfy, generation_settings_store, tmp_path):
    # A TOML target elsewhere must lose to the Runtime placement default.
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    (tmp_path / ".vibecomfy").mkdir()
    (tmp_path / ".vibecomfy" / "config.toml").write_text('[comfyui]\nurl="http://127.0.0.1:18111"\n')
    settings.update(lambda document: document["placement"].update({"kind": "attached", "endpoint": attached_comfy.url}))

    rows = listing.generation_rows(kind="image")
    assert any(attached_comfy.port in ports for ports in attached_comfy.probed)
    local_rows = [row for row in rows if row["backend"] == "local" and row["availability"] == "available"]
    assert local_rows and all(row["target"] == attached_comfy.url for row in local_rows)

    payload = GenerationFacade().targets()
    assert payload["target"]["endpoint"] == attached_comfy.url
    assert payload["placement"] == {"resolved": {"kind": "attached", "endpoint": attached_comfy.url},
                                    "source": "settings"}


def test_targets_with_nothing_configured_reports_discovery_on_8188(monkeypatch, tmp_path, generation_settings_store):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("VIBECOMFY_COMFYUI_URL", raising=False)
    payload = GenerationFacade().targets()
    assert payload["target"]["endpoint"] == "http://127.0.0.1:8188"
    assert payload["placement"] == {"resolved": {"kind": "attached", "endpoint": "http://127.0.0.1:8188"},
                                    "source": "discovery"}
    defaults_payload = defaults.listing()
    assert defaults_payload["placement"] == payload["placement"]


def test_broken_saved_target_does_not_hide_defaults_or_list(monkeypatch, capsys, tmp_path, generation_settings_store):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("VIBECOMFY_COMFYUI_URL", raising=False)
    (tmp_path / ".vibecomfy").mkdir()
    (tmp_path / ".vibecomfy" / "config.toml").write_text('[comfyui\nurl = ')
    monkeypatch.setattr(listing, "_targets", lambda: [])
    payload = _run_cli(monkeypatch, capsys, ["defaults", "--json"], generation_settings_store)
    assert payload["placement"]["resolved"] is None and payload["placement"]["source"] == "error"
    assert payload["placement"]["reason"]
    assert payload["slots"] and all("ready" in slot and "reason" in slot for slot in payload["slots"])
    assert listing.main(["list", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)


def test_save_without_a_url_never_persists_the_runtime_placement(monkeypatch, tmp_path, generation_settings_store):
    from astrid.sdk.generation import GenerationInputError

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("VIBECOMFY_COMFYUI_URL", raising=False)
    settings.update(lambda document: document["placement"].update({"kind": "attached", "endpoint": "http://127.0.0.1:8192"}))
    with pytest.raises(GenerationInputError):
        GenerationFacade().targets(save=True)
    # The Runtime placement is not a TOML target: nothing is written, and no bytes change.
    assert not (tmp_path / ".vibecomfy" / "config.toml").exists()
    assert not (tmp_path / ".vibecomfy").exists()
