"""Plan 32 T3b: one placement decision site; an explicit local placement never reaches an attached ComfyUI."""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from astrid.core.generation import placement as placements
from astrid.core.generation import preflight


class _StandIn(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 - http.server contract
        self.server.hits += 1
        body = json.dumps({"system": {"comfyui_version": "stand-in"}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture
def attached_comfy(monkeypatch, tmp_path):
    """A stand-in attached ComfyUI, advertised through VIBECOMFY_COMFYUI_URL, counting every request."""
    server = HTTPServer(("127.0.0.1", 0), _StandIn)
    server.hits = 0
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("VIBECOMFY_COMFYUI_URL", f"http://127.0.0.1:{server.server_port}")
    monkeypatch.setattr(preflight, "_installed_managed_target", lambda **kwargs: "MANAGED")
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


def _no_default(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("")
    return path


@pytest.mark.parametrize("selector", ["local", "managed"])
def test_explicit_local_never_reaches_attached_comfy(selector, attached_comfy, tmp_path):
    request = placements.request_from_selector(selector)
    placement = placements.resolve_placement(request, path=_no_default(tmp_path))
    assert placement.kind == "local"
    assert placements.comfy_selector(placement) == "managed"
    client = preflight.select_generation_target(placements.comfy_selector(placement))
    assert client == "MANAGED"
    assert attached_comfy.hits == 0


def test_explicit_local_beats_env_even_with_no_default(attached_comfy, tmp_path):
    placement = placements.resolve_placement({"kind": "local"}, path=_no_default(tmp_path))
    assert (placement.kind, placement.endpoint) == ("local", None)
    assert preflight.select_generation_target(placements.comfy_selector(placement)) == "MANAGED"
    assert attached_comfy.hits == 0


def test_unrequested_placement_follows_bound_attachment_through_the_one_site(attached_comfy, tmp_path):
    placement = placements.resolve_placement(None, path=_no_default(tmp_path))
    assert placement.kind == "attached"
    assert placement.endpoint == f"http://127.0.0.1:{attached_comfy.server_port}"
    client = preflight.select_generation_target(placements.comfy_selector(placement))
    assert client is not None and client.target.kind == "attached"
    assert attached_comfy.hits >= 1


def test_explicit_default_kind_local_is_not_overridden_by_discovery(attached_comfy, tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[placement]\nkind = "local"\n')
    placement = placements.resolve_placement(None, path=path)
    assert placement.kind == "local"
    assert preflight.select_generation_target(placements.comfy_selector(placement)) == "MANAGED"
    assert attached_comfy.hits == 0


def test_comfy_selector_is_pure_and_refuses_non_comfy_kinds():
    assert placements.comfy_selector(placements.Placement(kind="local")) == "managed"
    assert placements.comfy_selector(placements.Placement(kind="attached", endpoint="http://127.0.0.1:8199")) == "http://127.0.0.1:8199"
    with pytest.raises(Exception) as caught:
        placements.comfy_selector(placements.Placement(kind="runpod", machine="pod-1"))
    assert "no ComfyUI target" in str(caught.value)


@pytest.mark.parametrize("placement", ["local", "managed"])
def test_facade_explicit_local_never_reaches_attached_comfy(placement, monkeypatch, tmp_path):
    """Through the facade, with no managed engine installed, an explicit local request still never touches the stand-in."""
    from astrid.core.contracts.errors import AstridError
    from astrid.sdk.generation import GenerationFacade

    server = HTTPServer(("127.0.0.1", 0), _StandIn)
    server.hits = 0
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("VIBECOMFY_COMFYUI_URL", f"http://127.0.0.1:{server.server_port}")
    monkeypatch.setattr(preflight, "_installed_managed_target", lambda **kwargs: None)
    try:
        with pytest.raises(AstridError) as caught:
            GenerationFacade().image(placement=placement, prompt="one boat", dry_run=True,
                                     project_root=tmp_path)
        assert caught.value.code == "engine_not_found"
        issue = caught.value.state_snapshot["readiness"]["issues"][0]
        assert issue["details"]["engine_code"] == "COMFY_TARGET_NOT_READY"
    finally:
        server.shutdown()
        server.server_close()
    assert server.hits == 0
