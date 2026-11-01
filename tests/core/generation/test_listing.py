from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from astrid.core.generation import listing
from astrid.core.model_catalog.registry import ModelRegistry
from scripts.gen_capability_index import _in_repo_packs_only

EXACT_VAE = r"FLUX1\ae.safetensors"
TURBO_UNET = "z_image_turbo_bf16.safetensors"


def _row(**changes):
    """Synthetic row for declaration/cloud CLI tests, independent of the catalog."""
    return {
        **dict(id="example-image", kind="image", mode="t2i", backend="cloud",
               template="", source="catalog", availability="available", reason=""),
        **changes,
    }


@pytest.fixture(scope="module")
def catalog_snapshot():
    """Capture real registry and repo-local pack discovery once for this suite."""
    with _in_repo_packs_only():
        return ModelRegistry.load_default(), listing.discover()


@pytest.fixture(autouse=True)
def use_catalog_snapshot(monkeypatch, catalog_snapshot):
    registry, discovery = catalog_snapshot
    monkeypatch.setattr(ModelRegistry, "load_default", classmethod(lambda cls: registry))
    monkeypatch.setattr(listing, "discover", lambda: discovery)


def _target_info(mode="t2i", *, vaes=(EXACT_VAE,), unets=(TURBO_UNET,)):
    """Build a realistic object_info using the pinned template's node schemas."""
    from tests.core.generation.backends.test_comfy_model_resolution import target_info

    return target_info(mode, vaes=vaes, unets=unets)


@pytest.fixture
def comfy(monkeypatch):
    """A fake Windows ComfyUI discovered through the real loopback scanner."""
    class Handler(BaseHTTPRequestHandler):
        schema = _target_info()
        requests = []

        def do_GET(self):
            type(self).requests.append(("GET", self.path))
            if self.path == "/system_stats":
                body = {"system": {"os": "win32", "argv": []}, "devices": []}
            elif self.path == "/internal/folder_paths":
                body = {"custom_nodes": [r"C:\ComfyUI\custom_nodes"]}
            elif self.path == "/userdata?dir=workflows":
                body = []
            elif self.path == "/object_info":
                body = self.schema
            else:
                self.send_error(404)
                return
            data = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            type(self).requests.append(("POST", self.path))
            self.send_error(405)

        def log_message(self, *args):
            pass

    Handler.requests = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    from astrid.packs.vibecomfy import local

    monkeypatch.setattr(local, "_bounded_pinokio_discovery", lambda *args: (None, {}, [], []))
    scan = local.scan
    monkeypatch.setattr(local, "scan", lambda ports: scan([server.server_port]))
    yield server, Handler
    server.shutdown()
    thread.join()
    server.server_close()


def _turbo_rows():
    registry = ModelRegistry.load_default()
    canonical = [row for row in listing.catalog_rows()
                 if row.get("source") == "catalog-alias"
                 and row.get("registry_id") == "z-image"
                 and row.get("mode") in {"t2i", "i2i"}]
    assert canonical, "catalog_rows must expose the z-image-turbo alias backed by z-image"
    for row in canonical:
        mode = registry.get("z-image").modes[row["mode"]]
        assert row["template"] == mode.backends["local"].template
    return canonical


def _readiness_row(mode):
    rows = listing.generation_rows("image")
    return next(row for row in rows
                if row.get("source") == "catalog-alias"
                and row.get("registry_id") == "z-image"
                and row.get("mode") == mode
                and row.get("backend") == "local")


@pytest.mark.parametrize("mode", ["t2i", "i2i"])
def test_discovered_windows_target_resolves_real_turbo_roles(comfy, mode):
    comfy[1].schema = _target_info(mode)
    rows = _turbo_rows()
    assert any(row["mode"] == mode and row["template"] == (
        "image/z_image_turbo" if mode == "t2i" else "image/z_image_img2img"
    ) for row in rows)

    row = _readiness_row(mode)
    assert row["id"] == "z-image-turbo"
    assert row["target"] == f"http://127.0.0.1:{comfy[0].server_port}"
    assert row["target_kind"] == "attached"
    assert row["ready"] is True
    assert row["readiness_scope"] == "target_inventory"
    assert row["models_present"] == [EXACT_VAE, "qwen_3_4b.safetensors", TURBO_UNET]
    assert row["model_roles"] == {
        "diffusion": TURBO_UNET,
        "text_encoder": "qwen_3_4b.safetensors",
        "vae": EXACT_VAE,
    }
    assert row["model_bytes"] == "unverified_target_inventory"
    assert comfy[1].requests.count(("GET", "/object_info")) >= 1
    assert all(method == "GET" for method, _ in comfy[1].requests)
    assert not any(path in {"/prompt", "/queue"} for _, path in comfy[1].requests)


@pytest.mark.parametrize(
    ("unets", "vaes", "missing"),
    [
        (("z_image_bf16.safetensors",), (EXACT_VAE,), ["diffusion"]),
        ((TURBO_UNET,), ("FLUX1/ae.safetensors",), ["vae"]),
        ((TURBO_UNET,), (), ["vae"]),
        ((TURBO_UNET,), ("ae.safetensors", EXACT_VAE), ["vae"]),
    ],
)
def test_base_missing_and_ambiguous_models_fail_closed(comfy, unets, vaes, missing):
    comfy[1].schema = _target_info(unets=unets, vaes=vaes)
    row = _readiness_row("t2i")
    assert row["ready"] is False
    assert row["reason"] == "required models missing or ambiguous"
    assert row["missing_roles"] == missing
    assert row["fix"]


def test_target_probe_is_single_snapshot_and_never_starts_or_submits(comfy, monkeypatch):
    from astrid.core.generation.engine_custody import LinuxEngineServiceClient

    supervisor_call = Mock(side_effect=AssertionError("readiness called the engine supervisor"))
    monkeypatch.setattr(LinuxEngineServiceClient, "call", supervisor_call)
    _readiness_row("t2i")
    object_info = [path for method, path in comfy[1].requests
                   if method == "GET" and path == "/object_info"]
    assert len(object_info) == 1
    assert all(method == "GET" for method, _ in comfy[1].requests)
    assert not any(path in {"/prompt", "/queue", "/shutdown"}
                   for _, path in comfy[1].requests)
    supervisor_call.assert_not_called()


def test_no_target_and_malformed_probe_preserve_unready_rows(monkeypatch, comfy):
    actual_targets = listing._targets
    monkeypatch.setattr(listing, "_targets", lambda: [])
    no_target = _readiness_row("t2i")
    assert no_target["ready"] is False
    assert isinstance(no_target["target"], str)
    assert "target" in no_target["reason"].lower()

    monkeypatch.setattr(listing, "_targets", actual_targets)
    # The discovered target stays visible when its inventory is not an object.
    comfy[1].schema = ["malformed object_info"]
    row = _readiness_row("t2i")
    assert row["ready"] is False
    assert row["target"] == f"http://127.0.0.1:{comfy[0].server_port}"


def test_missing_vae_loader_and_none_object_info_fail_closed_with_fix(comfy):
    schema = _target_info()
    del schema["VAELoader"]
    comfy[1].schema = schema
    row = _readiness_row("t2i")
    assert row["ready"] is False
    assert row["fix"]

    comfy[1].schema = None
    row = _readiness_row("t2i")
    assert row["ready"] is False
    assert row["fix"]


def test_unknown_model_roles_fail_closed_without_claiming_readiness(comfy, monkeypatch):
    rows = _turbo_rows()
    target = next(row for row in rows if row["mode"] == "t2i")
    changed = dict(target, registry_id="unknown-model", id="unknown-model")
    monkeypatch.setattr(listing, "catalog_rows", lambda: [changed])
    row = listing.generation_rows("image")[0]
    assert row["ready"] is False
    assert row.get("model_roles", {}) == {}


def test_stale_direct_image_aliases_are_not_represented_as_resolved_turbo(comfy):
    rows = listing.catalog_rows()
    assert any(row.get("source") == "catalog-alias"
               and row.get("registry_id") == "z-image"
               and row.get("template") == "image/z_image_turbo" for row in rows)
    stale = [row for row in rows if row.get("source") == "direct-image"
             and row.get("template") == "image/z_image"]
    assert not any(row.get("registry_id") == "z-image" for row in stale)


def test_catalog_has_no_duplicate_entries():
    rows = listing.catalog_rows()
    identities = [(row["id"], row["kind"], row["mode"], row["backend"], row["template"])
                  for row in rows]
    assert len(identities) == len(set(identities))


def test_probe_failure_keeps_target_without_echoing_endpoint_error(comfy, monkeypatch):
    def fail(*args, **kwargs):
        raise ValueError("untrusted endpoint details must stay private")

    monkeypatch.setattr(listing.ComfyTargetClient, "request_json", fail)
    row = _readiness_row("t2i")
    assert row["target"] == f"http://127.0.0.1:{comfy[0].server_port}"
    assert row["ready"] is False and row["fix"]
    assert "probe unavailable" in row["reason"]
    assert "untrusted endpoint" not in json.dumps(row)


def test_cloud_presence_only_json_and_filter(monkeypatch, capsys):
    monkeypatch.setattr(listing, "catalog_rows", lambda: [
        _row(id="cloud-model"), _row(kind="video", id="video-model"),
    ])
    monkeypatch.setattr(listing, "_targets", Mock(side_effect=AssertionError("cloud listing probed targets")))
    monkeypatch.delenv("FAL_KEY", raising=False)
    assert listing.main(["list", "--kind", "image", "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert len(rows) == 1 and rows[0]["reason"] == "no key"
    assert "FAL_KEY" in rows[0]["fix"]
    monkeypatch.setenv("FAL_KEY", "synthetic-secret-must-not-appear")
    listing.main(["list", "--json"])
    text = capsys.readouterr().out
    assert "synthetic-secret-must-not-appear" not in text
    assert all(row["ready"] for row in json.loads(text))


@pytest.mark.parametrize(
    ("backend", "key_name"),
    [("cloud", "FAL_KEY"), ("wavespeed", "WAVESPEED_API_KEY")],
)
def test_cloud_readiness_checks_names_without_reading_credentials(monkeypatch, backend, key_name):
    class NamesOnlyEnvironment:
        def __init__(self, names):
            self.names = tuple(names)

        def __iter__(self):
            return iter(self.names)

        def __contains__(self, key):
            raise AssertionError("containment may read a credential value")

        def __getitem__(self, key):
            raise AssertionError("credential value read")

        def get(self, *args, **kwargs):
            raise AssertionError("credential value read")

        def items(self):
            raise AssertionError("credential values enumerated")

        def values(self):
            raise AssertionError("credential values enumerated")

    monkeypatch.setattr(listing, "catalog_rows", lambda: [_row(id="provider", backend=backend)])
    monkeypatch.setattr(listing, "os", SimpleNamespace(environ=NamesOnlyEnvironment([key_name])))
    present = listing.generation_rows("image")[0]
    assert present["ready"] is True
    assert present["readiness_scope"] == "key_presence"
    monkeypatch.setattr(listing, "os", SimpleNamespace(environ=NamesOnlyEnvironment([])))
    missing = listing.generation_rows("image")[0]
    assert missing["ready"] is False
    assert key_name in missing["fix"]


def test_kind_filter_rejects_unknown_value(capsys):
    with pytest.raises(SystemExit) as raised:
        listing.main(["list", "--kind", "model"])
    assert raised.value.code == 2
    assert "invalid choice" in capsys.readouterr().err


def test_unavailable_and_fixture_declarations(monkeypatch):
    records = [
        _row(id="unavailable", backend="", availability="unavailable", reason="not implemented"),
        _row(id="fixture", kind="fixture", backend="", availability="fixture-only", reason="fixture-only route"),
    ]
    monkeypatch.setattr(listing, "catalog_rows", lambda: records)
    rows = listing.generation_rows()
    assert all(not row["ready"] and row["fix"] for row in rows)
    assert {row["availability"] for row in rows} == {"unavailable", "fixture-only"}


def test_gateway_and_console_dispatch(monkeypatch, capsys):
    from astrid import console
    from astrid.core import gateway

    monkeypatch.setattr(listing, "generation_rows", lambda kind=None: [])
    assert console.main(["generate", "list", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == []
    assert gateway.main(["generate", "list", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == []


def test_real_registry_consumes_availability_and_cloud(monkeypatch):
    from scripts.gen_capability_index import _in_repo_packs_only

    monkeypatch.setattr(listing, "_targets", lambda: [])
    monkeypatch.delenv("FAL_KEY", raising=False)
    with _in_repo_packs_only():
        rows = listing.generation_rows()
    unavailable = next(row for row in rows if row["id"] == "video_editing.thumbnail_maker")
    assert unavailable["availability"] == "unavailable" and not unavailable["ready"]
    assert "not implemented" in unavailable["reason"]
    fixture = next(row for row in rows if row["id"] == "astrid_motion:preview/scenes.json")
    assert fixture["availability"] == "fixture-only" and not fixture["ready"]
    cloud = next(row for row in rows if row["id"] == "z-image" and row["backend"] == "cloud")
    assert not cloud["ready"] and cloud["reason"] == "no key" and "FAL_KEY" in cloud["fix"]


def test_text_and_json_listings_are_deterministic(monkeypatch, capsys):
    rows = [_row(id="z-model"), _row(id="a-model")]
    monkeypatch.setattr(listing, "catalog_rows", lambda: rows)
    monkeypatch.setattr(listing, "_targets", lambda: [])
    listing.main(["list", "--json"])
    first_json = capsys.readouterr().out
    listing.main(["list", "--json"])
    assert capsys.readouterr().out == first_json
    decoded = json.loads(first_json)
    assert [row["id"] for row in decoded] == ["a-model", "z-model"]
    listing.main(["list"])
    first_text = capsys.readouterr().out
    listing.main(["list"])
    assert capsys.readouterr().out == first_text
