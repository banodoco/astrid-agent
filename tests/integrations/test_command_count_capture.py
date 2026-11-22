"""Measure command work counts through a fresh CLI process and real Runtime."""
from __future__ import annotations

import json
import os
import re
import secrets
import subprocess
import sysconfig
from collections import Counter
from pathlib import Path

import pytest

from tests.integrations.test_runpod_host_endpoint_r5 import tuple_sources  # noqa: F401
from tests.integrations.test_runpod_slot_path_cpu import _drive

pytestmark = pytest.mark.integration


def _workflow_file() -> Path:
    from astrid.core.generation import entries

    rows = [row for row in entries.ready_entries()
            if "qwen-image-2.1-turbo" in (row["id"], row.get("registry_id"))]
    assert rows and rows[0].get("bundle"), "no ready workflow fixture"
    return Path(rows[0]["bundle"])


def _runtime_vectors(timings):
    api = Counter()
    internal = Counter()
    for row in timings:
        stage = row.get("stage", "")
        if not stage.startswith("Runtime "):
            continue
        if stage.startswith(("Runtime GET ", "Runtime POST ", "Runtime PUT ", "Runtime DELETE ")):
            _, method, path = stage.split(" ", 2)
            path = re.sub(r"/objects/sha256%3A[^/]+", "/objects/{digest}", path)
            path = re.sub(r"/projects/[^/]+", "/projects/{project}", path)
            api[f"{method} {path}"] += 1
        else:
            internal[stage] += 1
    return dict(sorted(api.items())), dict(sorted(internal.items()))


def test_fresh_process_command_counts(tmp_path, monkeypatch, real_runtime, tuple_sources):  # noqa: F811
    """Capture live counts before asserting. Discovery cache hits reuse admitted metadata but still fingerprint roots and hash files. In this pinned capture each command observed 27 tree-fingerprint calls, 6,165 file hashes, and 629,870,103 bytes hashed; those totals are informational because the admitted file set can vary. The 15 discovery calls split into seven cache-hit returns and eight full admission walks. ContextVar reset does not clear captured dicts: a copied async context outliving the command can retain its dict; clearing on exit is a possible Plan 82/review follow-up. The two resolver calls are separate safety checks: CLI validation at cli.py:748 and remote-target archive revalidation at sdk/generation.py:2234."""
    ROOT = Path(__file__).resolve().parents[2]

    runtime_root = tmp_path / "machine" / "runtime"

    def runtime(*args, **kwargs):
        runtime_checkout = os.environ["ASTRID_RUNTIME_CHECKOUT"]
        manifest = runtime_root / "source-profiles" / "astrid.json"
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text(json.dumps({
            "profile": "astrid", "runtime_checkout": runtime_checkout,
            "source_checkout": str(ROOT),
        }), encoding="utf-8")
        daemon = real_runtime(*args, support_root=runtime_root,
            owner_lock=runtime_root / "instance.lock", runtime_checkout=runtime_checkout,
            agent_checkout=str(ROOT), authoring_profile="astrid", **kwargs)
        token = secrets.token_hex(32)
        daemon.credentials.provision_static("count-user", token, [
            "admin", "handshake", "projects:read", "projects:write", "objects:read",
            "objects:write", "tasks:read", "tasks:write", "credentials:provision",
        ])
        credentials = runtime_root.parent / "credentials"
        credentials.mkdir(mode=0o700)
        path = credentials / "astrid.json"
        path.write_text(json.dumps({"version": 1, "scope": "astrid",
            "actor_id": "count-user", "token": token}), encoding="utf-8")
        path.chmod(0o600)
        return daemon

    def measure(_facade, controls, _tmp):
        instrument = tmp_path / "instrument"
        instrument.mkdir()
        (instrument / "sitecustomize.py").write_text(r'''import atexit, hashlib, json, os, sys
from collections import Counter
counts = Counter()
provenance = {}
import astrid, runtime_protocol, vibecomfy
provenance.update(agent=str(astrid.__file__), runtime=str(runtime_protocol.__file__), vibecomfy=str(vibecomfy.__file__))
fingerprint_depth = 0
import astrid.core.pack.discovery as discovery
original_tree_fingerprint = discovery._tree_fingerprint
original_file_digest = hashlib.file_digest
def counted_tree_fingerprint(root):
    global fingerprint_depth
    counts["tree_fingerprint_calls"] += 1
    fingerprint_depth += 1
    try: return original_tree_fingerprint(root)
    finally: fingerprint_depth -= 1
def counted_file_digest(stream, digest, *args, **kwargs):
    if fingerprint_depth:
        counts["tree_hash_calls"] += 1
        try: counts["tree_hash_bytes"] += os.fstat(stream.fileno()).st_size
        except (AttributeError, OSError, ValueError): pass
    return original_file_digest(stream, digest, *args, **kwargs)
discovery._tree_fingerprint = counted_tree_fingerprint
hashlib.file_digest = counted_file_digest
original_deepcopy = discovery.deepcopy
def counted_deepcopy(value):
    caller = sys._getframe(1)
    if caller.f_code.co_name == "discover_pack_metadata" and caller.f_globals.get("__name__") == "astrid.core.pack.discovery":
        cache = caller.f_locals.get("cache")
        key = caller.f_locals.get("fingerprint")
        if key is not None and cache is not None and cache.get(key) is value:
            counts["pack_inventory_cached_hit"] += 1
    return original_deepcopy(value)
discovery.deepcopy = counted_deepcopy
def profile(frame, event, arg):
    if event != "call": return
    module = frame.f_globals.get("__name__", "")
    name = frame.f_code.co_name
    if module == "astrid.core.generation.entries" and name == "resolve_entry_inputs" and "self" not in frame.f_locals:
        counts["entry_resolution"] += 1
    if module == "astrid.core.generation.entries" and name in {"build_entry", "prepare"}:
        counts["workflow_build"] += 1
    if module == "astrid.core.generation.entries" and name in {"find_entry", "_ready_entry_rows", "_complete_ready_entry", "_load_entry_workflow"}:
        counts["entry_" + name.lstrip("_")] += 1
    if module == "astrid.core.model_catalog.registry" and name == "load_default":
        counts["model_registry_load"] += 1
    if module == "astrid.core.pack.discovery" and name == "discover_pack_metadata":
        counts["pack_inventory"] += 1
sys.setprofile(profile)
def save():
    path = os.environ.get("ASTRID_COUNT_CAPTURE")
    if path:
        counts["pack_inventory_full_walk"] = counts["pack_inventory"] - counts["pack_inventory_cached_hit"]
        with open(path, "w", encoding="utf-8") as output:
            json.dump({"counts": dict(counts), "provenance": provenance}, output, sort_keys=True)
atexit.register(save)
''', encoding="utf-8")
        workflow = _workflow_file()
        counts_per_command = []
        payloads = []
        for index, prompt in enumerate(("A red cube on a white tabletop.",
                                        "A blue cube on a white tabletop.")):
            count_path = tmp_path / f"calls-{index}.json"
            receipt_path = tmp_path / f"receipt-{index}.json"
            command = [str(Path(sysconfig.get_path("scripts")) / "astrid"), "generate", "image",
                "--entry", "image/qwen_image_2_1_turbo", "--placement", controls["placement"], "--project", controls["project"],
                "--prompt", prompt, "--recovery-path", str(receipt_path),
                "--json", "--timings", "--no-wait"]
            env = os.environ.copy()
            env["PYTHONPATH"] = str(instrument) + os.pathsep + env.get("PYTHONPATH", "")
            env["ASTRID_COUNT_CAPTURE"] = str(count_path)
            result = subprocess.run(command, cwd=ROOT, env=env, capture_output=True, text=True, timeout=180)
            (tmp_path / f"cli-{index}.stdout").write_text(result.stdout)
            (tmp_path / f"cli-{index}.stderr").write_text(result.stderr)
            assert result.returncode == 0, (result.stdout, result.stderr)
            payload = json.loads(result.stdout)
            assert payload.get("ok") is True and payload.get("kernel_task_id"), payload
            assert count_path.is_file(), "fresh CLI process did not write count vector"
            capture = json.loads(count_path.read_text(encoding="utf-8"))
            count_vector = capture["counts"]
            timings = payload.get("timings", [])
            request_counts, internal_counts = _runtime_vectors(timings)
            assert receipt_path.is_file(), "accepted command did not persist its Runtime receipt"
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            assert receipt["locator"]["task_id"] == payload["kernel_task_id"]
            counts_per_command.append({**count_vector, "runtime_api_requests": request_counts,
                "runtime_internal_stages": internal_counts, "source_provenance": capture["provenance"]})
            payloads.append(payload)
        return counts_per_command, payloads

    try:
        with _drive(("image", "t2i"), tmp_path, monkeypatch, runtime, tuple_sources,
                    call=measure, target_kind="attached") as (client, _project, _provider, measured):
            vectors, payloads = measured
            task_ids = [payload["kernel_task_id"] for payload in payloads]
            assert len(set(task_ids)) == 2
            print("COMMAND_COUNT_CAPTURE " + json.dumps(vectors, sort_keys=True), flush=True)
            expected_counts = {
                "entry_resolution": 2,
                "entry_find_entry": 3,
                "entry_ready_entry_rows": 1,
                "entry_complete_ready_entry": 1,
                "entry_load_entry_workflow": 1,
                "workflow_build": 1,
                "model_registry_load": 1,
                "pack_inventory": 15,
                "pack_inventory_cached_hit": 7,
                "pack_inventory_full_walk": 8,
            }
            expected_api = {
                "GET /v1/app-state/user/generation": 3,
                "GET /v1/capabilities": 1,
                "GET /v1/health": 1,
                "GET /v1/objects/{digest}": 9,
                "GET /v1/projects/{project}": 2,
                "POST /v1/handshake": 2,
                "POST /v1/projects/{project}/objects": 3,
                "POST /v1/tasks": 1,
            }
            expected_internal = {
                "Runtime acquisition lock": 1,
                "Runtime connection": 1,
                "Runtime launcher connection": 1,
            }
            expected_provenance = {
                "agent": str(ROOT / "astrid" / "__init__.py"),
                "runtime": str(Path(os.environ["ASTRID_RUNTIME_CHECKOUT"]) / "runtime_protocol" / "__init__.py"),
                "vibecomfy": str(Path(os.environ["ASTRID_VIBECOMFY_CHECKOUT"]) / "vibecomfy" / "__init__.py"),
            }
            for vector in vectors:
                for name, expected in expected_counts.items():
                    assert vector[name] == expected, (name, vector)
                assert vector["runtime_api_requests"] == expected_api, vector
                assert vector["runtime_internal_stages"] == expected_internal, vector
                assert vector["source_provenance"] == expected_provenance, vector["source_provenance"]
            for task_id in task_ids:
                task = client.tasks.show(task_id)
                assert task.ok and task.data["state"] in {"queued", "running", "succeeded"}
    finally:
        state = runtime_root / "generic-host.json"
        if state.exists():
            from astrid.sdk.host_bootstrap import _stop_registered_host
            _stop_registered_host(json.loads(state.read_text(encoding="utf-8")))
