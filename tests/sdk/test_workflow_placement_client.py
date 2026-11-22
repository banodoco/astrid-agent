"""Workflow schema lookup must use the command's Runtime settings store."""

from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

from astrid.core.generation import settings
from astrid.sdk.generation import GenerationFacade


def _store(endpoint: str):
    store = SimpleNamespace(reads=0)

    def get_user_app_state(_key):
        store.reads += 1
        return {
            "content": {"placement": {"kind": "attached", "endpoint": endpoint},
                        "defaults": {}, "machines": {}, "models": {}},
            "version": store.reads,
        }

    store.get_user_app_state = get_user_app_state
    return store


def _client(store, marker="client"):
    def import_file(*, project, path):
        import hashlib

        return SimpleNamespace(ok=True, data={"digest": "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()})

    return SimpleNamespace(get_user_app_state=store.get_user_app_state,
                           media=SimpleNamespace(import_file=import_file), marker=marker)


def test_fresh_cli_uses_its_client_for_authored_ui_with_conflicting_stores(tmp_path):
    """Exercise ``python -m astrid generate workflow`` in a new interpreter."""
    from tests.core.generation.test_user_workflow_attached import (
        OBJECT_INFO_STOCK,
        _Fake,
        _ui_workflow,
    )

    target_client, workflow = _Fake(OBJECT_INFO_STOCK, _ui_workflow()).start(), None
    other_info = copy.deepcopy(OBJECT_INFO_STOCK)
    other_info.pop("AstridCustomSampler")
    target_ambient = _Fake(other_info, _ui_workflow()).start()
    try:
        workflow = tmp_path / "authored-ui.json"
        workflow.write_text(json.dumps(_ui_workflow()), encoding="utf-8")
        trace_path = tmp_path / "trace.json"
        sitecustomize = tmp_path / "sitecustomize.py"
        sitecustomize.write_text(
            "import hashlib, json, os\n"
            "from pathlib import Path\n"
            "from types import SimpleNamespace\n"
            "from astrid.sdk.client import AstridClient\n"
            "from astrid.sdk.generation import GenerationFacade\n"
            "state = {'opens': 0, 'reads': {'command': 0, 'ambient': 0}}\n"
            "def save(): Path(os.environ['F1_TRACE']).write_text(json.dumps(state))\n"
            "class Client:\n"
            "  def __init__(self, marker, endpoint): self.marker, self.endpoint = marker, endpoint; self.media = SimpleNamespace(import_file=self.import_file)\n"
            "  def __enter__(self): return self\n"
            "  def __exit__(self, *_args): return None\n"
            "  def import_file(self, *, project, path): return SimpleNamespace(ok=True, data={'digest': 'sha256:' + hashlib.sha256(path.read_bytes()).hexdigest()})\n"
            "  def get_user_app_state(self, _key):\n"
            "    state['reads'][self.marker] += 1; save()\n"
            "    return {'content': {'placement': {'kind': 'attached', 'endpoint': self.endpoint}, 'defaults': {}, 'machines': {}, 'models': {}}, 'version': 1}\n"
            "def open_from_launcher(cls, **_kwargs):\n"
            "  state['opens'] += 1; marker = 'command' if state['opens'] == 1 else 'ambient'; save()\n"
            "  key = 'F1_TARGET_' + marker.upper(); return Client(marker, os.environ[key])\n"
            "AstridClient.open_from_launcher = classmethod(open_from_launcher)\n"
            "def generate(self, **kwargs): return {'ok': True, 'marker': kwargs['client'].marker, 'inputs': kwargs['inputs']}\n"
            "GenerationFacade._generate_on_target = generate\n"
            "save()\n",
            encoding="utf-8",
        )
        repo = Path(__file__).resolve().parents[2]
        import vibecomfy

        vibecomfy_root = Path(vibecomfy.__file__).resolve().parents[1]
        env = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": str(tmp_path / "home"),
            "PYTHONPATH": os.pathsep.join((str(tmp_path), str(repo), str(vibecomfy_root))),
            "PYTHONDONTWRITEBYTECODE": "1",
            "F1_TRACE": str(trace_path),
            "F1_TARGET_COMMAND": target_client.url,
            "F1_TARGET_AMBIENT": target_ambient.url,
        }
        runtime_checkout = os.environ.get("ASTRID_RUNTIME_CHECKOUT")
        if runtime_checkout:
            env["ASTRID_RUNTIME_CHECKOUT"] = runtime_checkout
        Path(env["HOME"]).mkdir(parents=True, exist_ok=True)
        child = subprocess.run(
            [sys.executable, "-m", "astrid", "generate", "workflow", str(workflow),
             "--placement", "auto", "--project", "f1-test", "--input", "seed=99",
             "--no-wait", "--json"],
            cwd=repo, env=env, capture_output=True, text=True, timeout=45,
        )

        assert child.returncode == 0, child.stdout + child.stderr
        payload = json.loads(child.stdout)
        assert payload["marker"] == "command"
        assert payload["inputs"]["seed"] == 99
        assert ("GET", "/object_info") in target_client.requests
        assert target_ambient.requests == []
        assert json.loads(trace_path.read_text(encoding="utf-8")) == {
            "opens": 1, "reads": {"command": 1, "ambient": 0}}
    finally:
        target_client.stop()
        target_ambient.stop()


def test_omitted_client_keeps_ambient_store_and_sequential_clients_are_isolated(
    monkeypatch, tmp_path,
):
    from tests.core.generation.test_user_workflow_attached import (
        OBJECT_INFO_STOCK,
        _Fake,
        _ui_workflow,
    )

    target_a = _Fake(OBJECT_INFO_STOCK, _ui_workflow()).start()
    target_b = _Fake(OBJECT_INFO_STOCK, _ui_workflow()).start()
    ambient_target = _Fake(OBJECT_INFO_STOCK, _ui_workflow()).start()
    try:
        source = tmp_path / "authored-ui.json"
        source.write_text(json.dumps(_ui_workflow()), encoding="utf-8")
        store_a, store_b = _store(target_a.url), _store(target_b.url)
        ambient = _store(ambient_target.url)
        monkeypatch.setattr(settings, "_store_factory", lambda: ambient)
        monkeypatch.setattr(settings, "_opened", None)
        monkeypatch.setattr(GenerationFacade, "_generate_on_target",
                            lambda self, **kwargs: {"ok": True, "marker": kwargs["client"].marker})

        result_a = GenerationFacade().workflow(
            workflow=source, placement="auto", project="f1-test", client=_client(store_a, "a"))
        result_b = GenerationFacade().workflow(
            workflow=source, placement="auto", project="f1-test", client=_client(store_b, "b"))

        assert result_a["marker"] == "a" and result_b["marker"] == "b"
        assert ("GET", "/object_info") in target_a.requests
        assert ("GET", "/object_info") in target_b.requests
        assert ambient_target.requests == []
        assert (store_a.reads, store_b.reads, ambient.reads) == (1, 1, 0)

        # The optional client is intentionally optional: legacy callers still use the factory.
        from astrid.core.generation import placement

        assert placement.attached_comfy_url("auto") == ambient_target.url
        assert ambient.reads == 1

        monkeypatch.setattr(GenerationFacade, "_generate_on_target",
                            lambda self, **_kwargs: {"ok": True})
        dry = GenerationFacade().workflow(
            workflow=source, placement="auto", project="f1-test", dry_run=True, client=None)
        assert dry["ok"] is True
        assert ambient.reads == 2
        assert ("GET", "/object_info") in ambient_target.requests
    finally:
        target_a.stop()
        target_b.stop()
        ambient_target.stop()


def test_existing_workflow_refusal_precedes_placement_store_access(monkeypatch, tmp_path):
    from astrid.core.generation.preflight import GenerationReadinessError

    source = tmp_path / "workflow.json"
    source.write_text("{}", encoding="utf-8")
    source.with_suffix(".vibe.json").write_text(json.dumps({"outputs": [
        {"node_id": "9", "output_type": "SaveImage"},
        {"node_id": "10", "output_type": "SaveVideo"},
    ]}), encoding="utf-8")
    monkeypatch.setattr(settings, "_store_factory",
                        lambda: (_ for _ in ()).throw(AssertionError("placement was resolved early")))

    try:
        GenerationFacade().workflow(workflow=source, placement="auto", client=_client(_store("unused")))
    except GenerationReadinessError as exc:
        assert exc.code == "unsupported_output_cardinality"
        assert "produces 2 outputs; Phase 1 supports exactly one" in str(exc)
    else:
        raise AssertionError("the existing two-output refusal was not preserved")


def test_sdk_open_uses_one_health_and_handshake_for_workflow_schema_lookup(
    monkeypatch, tmp_path,
):
    """Exercise the real AstridClient open/validation path over a local transport stand-in."""
    from tests.core.generation.test_user_workflow_attached import (
        OBJECT_INFO_STOCK,
        _Fake,
        _ui_workflow,
    )
    from astrid.sdk.client import AstridClient
    from astrid.sdk.workspace_client import WorkspaceClient
    from astrid_runtime_client.contract_metadata import PROTOCOL, SCHEMA_DIGEST

    target_command = _Fake(OBJECT_INFO_STOCK, _ui_workflow()).start()
    target_ambient = _Fake(OBJECT_INFO_STOCK, _ui_workflow()).start()
    try:
        source = tmp_path / "authored-ui.json"
        source.write_text(json.dumps(_ui_workflow()), encoding="utf-8")
        counts = {"connections": 0, "health": 0, "handshake": 0, "reads": {}}

        def init_transport(self, endpoint, _token):
            counts["connections"] += 1
            self.marker = "command" if counts["connections"] == 1 else "ambient"
            self.endpoint = endpoint
            self.actor_id = None

        def health(self):
            counts["health"] += 1
            return {"status": "ok", "protocol": PROTOCOL, "schema_digest": SCHEMA_DIGEST,
                    "runtime_epoch": 1, "runtime_instance_id": "f1", "runtime_session_id": "session",
                    "identity": None}

        def handshake(self, _client_name, _client_version, _requested_scopes):
            counts["handshake"] += 1
            return {"protocol": PROTOCOL, "schema_digest": SCHEMA_DIGEST, "session_id": "session",
                    "actor_id": "f1-actor", "realm_id": "f1-realm",
                    "scopes": ["objects:read", "objects:write", "projects:read", "projects:write",
                               "tasks:read", "tasks:write"],
                    "capabilities": ["execution_binding.targeted.v1"]}

        def generated(self, operation, *_args, **_kwargs):
            if operation != "get_user_app_state":
                raise AssertionError(f"unexpected Runtime operation: {operation}")
            counts["reads"][self.marker] = counts["reads"].get(self.marker, 0) + 1
            endpoint = target_command.url if self.marker == "command" else target_ambient.url
            return {"content": {"placement": {"kind": "attached", "endpoint": endpoint},
                                "defaults": {}, "machines": {}, "models": {}},
                    "version": counts["reads"][self.marker]}

        monkeypatch.setattr(WorkspaceClient, "__init__", init_transport)
        monkeypatch.setattr(WorkspaceClient, "health", health)
        monkeypatch.setattr(WorkspaceClient, "handshake", handshake)
        monkeypatch.setattr(WorkspaceClient, "_call_generated", generated)
        monkeypatch.setattr(settings, "_store_factory", None)
        monkeypatch.setattr(settings, "_opened", None)

        def open_from_launcher(cls, **_kwargs):
            return cls.open(endpoint="http://127.0.0.1:1", credential="f1-test-token",
                            realm_id="f1-realm", actor_id="f1-actor", client_name="f1",
                            client_version="test", protocol_version=PROTOCOL)

        monkeypatch.setattr(AstridClient, "open_from_launcher", classmethod(open_from_launcher))
        client = AstridClient.open(
            endpoint="http://127.0.0.1:1", credential="f1-test-token", realm_id="f1-realm",
            actor_id="f1-actor", client_name="f1", client_version="test", protocol_version=PROTOCOL,
        )
        monkeypatch.setattr(GenerationFacade, "_generate_on_target",
                            lambda self, **_kwargs: {"ok": True})
        result = GenerationFacade().workflow(
            workflow=source, placement="auto", project="f1-test", dry_run=True, client=client)

        assert result["ok"] is True
        assert counts == {"connections": 1, "health": 1, "handshake": 1,
                          "reads": {"command": 1}}
        assert ("GET", "/object_info") in target_command.requests
        assert target_ambient.requests == []
    finally:
        target_command.stop()
        target_ambient.stop()
