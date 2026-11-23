"""Measure command work counts through a fresh CLI process and real Runtime."""
from __future__ import annotations

import ast
import json
import os
import re
import secrets
import subprocess
import sys
import sysconfig
from collections import Counter
from pathlib import Path

import pytest

from tests.integrations.test_runpod_host_endpoint_r5 import tuple_sources  # noqa: F401
from tests.integrations.test_runpod_slot_path_cpu import _drive

pytestmark = pytest.mark.integration


def test_inventory_count_hook_control(tmp_path):
    """The production sitecustomize hook distinguishes one hit from full walks."""
    source = Path(__file__).read_text(encoding="utf-8")
    module = ast.parse(source)
    hook = next(
        ast.literal_eval(node.args[0])
        for node in ast.walk(module)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "write_text"
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and isinstance(node.args[0].value, str)
        and "original_tree_fingerprint = discovery._tree_fingerprint" in node.args[0].value
    )
    instrument = tmp_path / "instrument"
    instrument.mkdir()
    (instrument / "sitecustomize.py").write_text(hook, encoding="utf-8")
    capture = tmp_path / "counts.json"
    runner = r'''
import os, sys
from pathlib import Path
from types import SimpleNamespace
from astrid.core.pack import discovery, loader, source_setup

root = Path(sys.argv[1])
root.mkdir(parents=True, exist_ok=True)
home = root / "home"
home.mkdir()
Path.home = classmethod(lambda cls: home)
loader.packs_root = lambda: root / "bundled"
(root / "bundled").mkdir()
source_setup.active_source_inventory = lambda: SimpleNamespace(sources=(), identity="fixture")
discovery._discovery_fingerprint = lambda *args, **kwargs: "same-key"

walks = []
def scan(*args, **kwargs):
    walks.append(1)
    return ()
def lookup():
    return discovery.discover_pack_metadata(project_root=root, personal_packs_root=root / "personal", discover_packs_fn=scan)
with discovery.pack_discovery_scope():
    lookup()
    first = len(walks)
    assert first > 0
    lookup()
    assert len(walks) == first
lookup()
assert len(walks) > first
second = len(walks)
lookup()
assert len(walks) > second
'''
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join((str(instrument), env.get("PYTHONPATH", "")))
    env["ASTRID_COUNT_CAPTURE"] = str(capture)
    result = subprocess.run(
        [sys.executable, "-c", runner, str(tmp_path / "fixture")],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, (result.stdout, result.stderr)
    counts = json.loads(capture.read_text(encoding="utf-8"))["counts"]
    assert counts["pack_inventory"] == 4, counts
    assert counts["pack_inventory_cached_hit"] == 1, counts
    assert counts["pack_inventory_full_walk"] == 3, counts


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
    """Capture live work counts with upper bounds and required-positive work assertions.

    Discovery cache hits still fingerprint roots and hash files. The current
    ceiling is 27 tree-fingerprint calls per command; file hash counts and
    bytes are informational because the admitted file set can vary. Inventory
    calls partition into cache hits and full walks; a full walk is required.
    The two resolver calls are separate safety
    checks: CLI validation at cli.py:748 and remote-target archive revalidation
    at sdk/generation.py:2234.
    """
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
import inspect
inventory_code = inspect.unwrap(discovery.discover_pack_metadata).__code__
original_cache_read = discovery._DiscoveryCache.read
def counted_cache_read(self, key):
    result = original_cache_read(self, key)
    caller = sys._getframe(1)
    if caller.f_code is inventory_code and result is not discovery._CACHE_MISS:
        counts["pack_inventory_cached_hit"] += 1
    return result
discovery._DiscoveryCache.read = counted_cache_read
# Count the actual source scanner invocation after the cache-return branch.
# Resolve its line from source so unrelated edits cannot silently move the probe.
source_lines, first_line = inspect.getsourcelines(discovery.discover_pack_metadata)
walk_line = first_line + next(i for i, line in enumerate(source_lines) if line.strip().startswith("source_packs = ("))
walked_frames = set()
def trace(frame, event, arg):
    if frame.f_code is inventory_code:
        if event == "line" and frame.f_lineno == walk_line and frame not in walked_frames:
            counts["pack_inventory_full_walk"] += 1
            walked_frames.add(frame)
        elif event == "return":
            walked_frames.discard(frame)
        return trace
    return None
sys.settrace(trace)
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
        with open(path, "w", encoding="utf-8") as output:
            json.dump({"counts": dict(counts), "provenance": provenance}, output, sort_keys=True)
atexit.register(save)
''', encoding="utf-8")
        _workflow_file()
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
                "tree_fingerprint_calls": 27,
            }
            required_positive_counts = {
                "entry_resolution",
                "entry_find_entry",
                "entry_ready_entry_rows",
                "entry_complete_ready_entry",
                "entry_load_entry_workflow",
                "workflow_build",
                "model_registry_load",
                "pack_inventory_full_walk",
                "pack_inventory_cached_hit",
                "tree_fingerprint_calls",
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
                for name, ceiling in expected_counts.items():
                    assert 0 <= vector.get(name, 0) <= ceiling, (name, vector)
                for name in required_positive_counts:
                    assert vector.get(name, 0) > 0, (name, vector)
                assert vector["pack_inventory"] == (
                    vector.get("pack_inventory_cached_hit", 0)
                    + vector.get("pack_inventory_full_walk", 0)
                ), vector
                for actual, ceilings in ((vector["runtime_api_requests"], expected_api),
                                         (vector["runtime_internal_stages"], expected_internal)):
                    assert actual.keys() <= ceilings.keys(), actual
                    for name, ceiling in ceilings.items():
                        assert 0 <= actual.get(name, 0) <= ceiling, (name, actual)
                assert vector["runtime_api_requests"].get("POST /v1/tasks", 0) > 0, vector
                assert vector["source_provenance"] == expected_provenance, vector["source_provenance"]
            for task_id in task_ids:
                task = client.tasks.show(task_id)
                assert task.ok and task.data["state"] in {"queued", "running", "succeeded"}
    finally:
        state = runtime_root / "generic-host.json"
        if state.exists():
            from astrid.sdk.host_bootstrap import _stop_registered_host
            _stop_registered_host(json.loads(state.read_text(encoding="utf-8")))
