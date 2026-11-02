"""M2-11a parity: pre-swap ``run_task`` (OLD) versus the session channel (NEW).

Both arms enter through ``GenericPackHost.run_task`` with the same task, the
same fake runtime client and the same fake engine edge. The engine edge is the
native Wan2GP invoke (``invoke_wan_session``); it returns the one-frame evidence
built from ``fixtures/one_frame_engine_result.json``. Nothing above the edge is
faked: admission, the managed tool session, the worker process, output
materialization, upload and settlement all run for real.

OLD arm (frozen). ``fixtures/parity_golden_old_run_task.json`` was captured by
running ``run_task_arm`` on the pre-swap tree (commit 6fe378a61), where the wan
branch called ``GenericPackHost._run_wan_session`` directly with the raw task
inputs. After that method is deleted this golden is the reference.

NEW arm. The shipped routing at HEAD: one managed-session predicate, profile
hooks, the per-run session channel, and the worker running
``generate_video_session``.

The live ``_run_wan_session`` is still driven here, and only here, so the old
path stays reachable from this module alone.

Edges faked (both arms): the native invoke, the warm-slot ``prepare_wan_session``
(a child process cannot start without an upstream checkout), and the runtime
client. Availability and preflight are stubbed to report the route ready.
"""

from __future__ import annotations

import copy
import dataclasses
import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from astrid.core.execution.generic_host import GenericPackHost, _ManagedWanChildAdapter
from astrid.core.execution.managed_tool_session import CapabilityDescriptor, SessionBinding
from astrid.packs.wan2gp.src.driver import snapshot_native_outputs

FIXTURES = Path(__file__).parent / "fixtures"
FIXTURE = FIXTURES / "one_frame_engine_result.json"
GOLDEN = FIXTURES / "parity_golden_old_run_task.json"

# Golden inputs: exactly the fixture's inputs (prompt and model only).
# Explicit inputs: every pack field supplied, so the runner injects no defaults.
EXPLICIT_INPUTS: dict[str, Any] = {
    "prompt": "parity fixture frame",
    "model": "wan-2.2",
    "negative_prompt": "low quality",
    "resolution": "512x512",
    "frames": 9,
    "fps": "8",
    "seed": 0,
    "guidance_scale": 5.0,
    "steps": 4,
    "loras": "",
}


def _fixture() -> dict[str, Any]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


CASE_INPUTS: dict[str, dict[str, Any]] = {
    "fixture_inputs": dict(_fixture()["inputs"]),
    "explicit_inputs": dict(EXPLICIT_INPUTS),
}


# ------------------------------------------------------------- runtime client


class _RuntimeClient:
    """The runtime edge: records settlement, failure and upload calls."""

    schema_digest = "sha256:" + "1" * 64

    def __init__(self) -> None:
        self.registrations: list[Any] = []
        self.capability_registrations: list[Any] = []
        self.settlements: list[dict[str, Any]] = []
        self.failures: list[tuple[Any, ...]] = []
        self.uploaded_objects: dict[str, dict[str, Any]] = {}
        self.heartbeats: list[Any] = []
        self.tasks: dict[str, Any] = {}

    def health(self) -> dict[str, Any]:
        return {
            "status": "ok", "protocol": "workspace.v1", "schema_digest": self.schema_digest,
            "runtime_epoch": 1, "instance_id": "parity-runtime-instance",
            "runtime_instance_id": "parity-runtime-instance", "coordinator_epoch": "parity-runtime-instance",
        }

    def refresh_local_connection(self) -> bool:
        return False

    def register_executor(self, executor_id: str, **payload: Any) -> dict[str, Any]:
        self.registrations.append((executor_id, payload))
        return {"id": executor_id, "state": "registered"}

    def register_capability(self, capability_id: str, **payload: Any) -> None:
        self.capability_registrations.append((capability_id, payload))

    def heartbeat(self, task_id: str, lease_token: str, *, attempt_id: str, fence: int, progress: Any = None) -> None:
        self.heartbeats.append((task_id, lease_token, attempt_id, fence))

    def task(self, task_id: str) -> Any:
        return self.tasks[task_id]

    def settle(self, task_id: str, lease_token: str, **payload: Any) -> dict[str, Any]:
        self.settlements.append({"task_id": task_id, "lease_token": lease_token, "payload": payload})
        return {"task": {"id": task_id, "status": "completed"}}

    def fail(self, task_id: str, lease_token: str, error: Any, **kwargs: Any) -> None:
        self.failures.append((task_id, lease_token, error, kwargs))

    def claim_next(self, **payload: Any) -> None:
        return None

    def upload_object(self, path: Any, *, project_id: str, media_type: str, filename: str | None = None) -> Any:
        import hashlib
        from types import SimpleNamespace

        data = Path(path).read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        self.uploaded_objects[digest] = {
            "data": data, "filename": filename, "project_id": project_id, "media_type": media_type,
        }
        return SimpleNamespace(object_id=f"object-{digest[:12]}", digest=digest, size=len(data),
                               media_type=media_type, filename=filename, project_id=project_id)

    def withdraw_capability(self, capability_id: str, *, digest: str, reason: str) -> None:
        self.register_capability(capability_id, digest=digest, status="unavailable", unavailable_reason=reason)


# ---------------------------------------------------------------- fake edges


def _engine_edge(native_root: Path, calls: list[dict[str, Any]]):
    """The native invoke. Writes the one-frame output and returns native evidence."""
    fixture = _fixture()

    def invoke(token: Any, settings: Any, *, timeout: float, cancelled: Any, progress_path: Any = None) -> dict[str, Any]:
        calls.append(copy.deepcopy(dict(settings)))
        native_root.mkdir(parents=True, exist_ok=True)
        generated = list(fixture["generated_files"])
        for name in generated:
            (native_root / name).write_bytes(str(fixture["output_bytes_text"]).encode("utf-8"))
        return {
            **_ManagedWanChildAdapter.identity(token),
            "native_job_id": fixture["native_job_id"],
            "events": [],
            "result": {"success": True, "errors": [], "generated_files": generated},
            "birth": fixture["birth"],
            "process_id": fixture["process_id"],
            "returncode": fixture["returncode"],
            "terminal": True,
            "source_root": str(native_root.absolute()),
            "output_snapshots": snapshot_native_outputs(generated, native_root),
            "native_error": None,
        }

    return invoke


class _NoopWanAdapter:
    """Child lifecycle stub: the warm slot is held, nothing is started."""

    def observe(self, **_: Any) -> dict[str, Any]:
        return {"ok": True}

    def release(self, **_: Any) -> dict[str, Any]:
        return {"ok": True}

    def cancel(self, **_: Any) -> dict[str, Any]:
        return {"ok": True}


def _fake_prepare(host: GenericPackHost):
    def prepare_wan_session(*, owner_dir: Any, root: Any, python: Any, source_digest: str, config_digest: str,
                            readiness_timeout: float, release_timeout: float, init_options: Any = None) -> SessionBinding:
        binding = SessionBinding(
            session_id=str(owner_dir), runtime_instance_id="parity-runtime-instance",
            process_birth_id="parity-birth", endpoint="pipe:parity-birth",
            source_digest=source_digest, config_digest=config_digest,
            execution_identity="parity-identity", launch_generation="parity-birth",
            engine_birth_id="parity-birth",
        )
        host.managed_tool_session.open(
            capability=CapabilityDescriptor("wan2gp.generate_video", warm_reuse_expected=True),
            binding=binding, adapter=_NoopWanAdapter(),
        )
        return binding

    return prepare_wan_session


def _task(tmp_path: Path, inputs: dict[str, Any]) -> dict[str, Any]:
    upstream = tmp_path / "upstream"
    (upstream / "shared").mkdir(parents=True, exist_ok=True)
    (upstream / "shared" / "api.py").write_text("", encoding="utf-8")
    config = tmp_path / "config.json"
    config.write_text("{}", encoding="utf-8")
    python = tmp_path / "python"
    python.write_text("", encoding="utf-8")
    return {"task": {
        "id": "wan-task", "capability": "wan2gp.generate_video", "project_id": "demo",
        "attempt_id": "wan-attempt", "fence": 1,
        "wan_session": {
            "root": str(upstream), "config_path": str(config), "python": str(python),
            "owner_dir": str(tmp_path / "owner"), "source_digest": "parity-source",
            "config_digest": "parity-config", "readiness_timeout": 5, "release_timeout": 5,
        },
        "spec": {"spec": {"inputs": dict(inputs)}},
    }}


# ------------------------------------------------------------------ arm runner


def run_task_arm(tmp_path: Path, inputs: dict[str, Any], monkeypatch: Any) -> dict[str, Any]:
    """Run one wan task through ``run_task`` and return the canonical observable outputs."""
    import astrid.core.generation.availability as availability
    from astrid.core.pack.loader import _default_pack_roots

    monkeypatch.setattr(availability, "action_availability", lambda _route: {"available": True})
    client = _RuntimeClient()
    host = GenericPackHost(pack_roots=_default_pack_roots(), client=client)
    host.discover()

    calls: list[dict[str, Any]] = []
    edge = _engine_edge(tmp_path / "native", calls)
    # OLD routing calls the instance attribute; NEW routing calls the profile's invoke.
    host.invoke_wan_session = edge
    profiles = getattr(host, "_managed_tool_profiles", None)
    if profiles and "wan2gp" in profiles:
        monkeypatch.setitem(profiles, "wan2gp", dataclasses.replace(profiles["wan2gp"], invoke=edge))
    monkeypatch.setattr(host, "prepare_wan_session", _fake_prepare(host))
    monkeypatch.setattr(host, "preflight", lambda *_: None)
    # Record the real pid of each worker the host launches (the receipt's process_id source).
    import astrid.core.execution.generic_host as gh_module

    spawned_pids: list[int] = []
    real_spawn = gh_module.spawn_task_scope

    def recording_spawn(*args: Any, **kwargs: Any) -> Any:
        process = real_spawn(*args, **kwargs)
        spawned_pids.append(process.pid)
        return process

    monkeypatch.setattr(gh_module, "spawn_task_scope", recording_spawn)
    host.capabilities["wan2gp.generate_video"] = dataclasses.replace(
        host.capabilities["wan2gp.generate_video"], ready=True)

    task = _task(tmp_path, inputs)
    client.tasks["wan-task"] = task
    raised = None
    try:
        host.run_task(task, lease_token="lease")
    except Exception as exc:  # the runtime records the failure; keep only its type
        raised = type(exc).__name__

    return {
        "observed_worker_pids": list(spawned_pids),
        "raised": raised,
        "submitted_settings": [_canonical_settings(call) for call in calls],
        "uploads": sorted(
            ({"digest": digest, "size": len(row["data"]), "filename": row["filename"],
              "media_type": row["media_type"], "project_id": row["project_id"]}
             for digest, row in client.uploaded_objects.items()),
            key=lambda item: item["digest"],
        ),
        "settlements": [_canonical(item["payload"], tmp_path) for item in client.settlements],
        "failures": [_canonical_failure(item, tmp_path) for item in client.failures],
    }


# Run-specific values that never change the observable result. Roots become
# placeholders, stat tuples keep only size (as the existing harness does), and
# the random admission token and wall-clock manifest time are dropped.
_DROPPED_KEYS = frozenset({"token_id", "admission_token", "created"})
_ATTEMPT_ROOT = re.compile(r"/tmp/astrid-attempt-[A-Za-z0-9_]+")
_STAT_SIZE_INDEX = 2


def _canonical(value: Any, tmp_root: Path) -> Any:
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for key in sorted(value, key=str):
            if key in _DROPPED_KEYS:
                continue
            child = value[key]
            if key == "stat" and isinstance(child, list):
                out[key] = [child[_STAT_SIZE_INDEX]]
            else:
                out[key] = _canonical(child, tmp_root)
        return out
    if isinstance(value, list):
        return [_canonical(item, tmp_root) for item in value]
    if isinstance(value, str):
        text = value.replace(str(tmp_root), "<tmp>")
        return _ATTEMPT_ROOT.sub("<attempt>", text)
    return value


def _canonical_settings(settings: dict[str, Any]) -> dict[str, Any]:
    return {key: settings[key] for key in sorted(settings)}


def _canonical_failure(item: tuple[Any, ...], tmp_root: Path) -> dict[str, Any]:
    _task_id, _lease, error, kwargs = item
    return {"error": _canonical(str(error), tmp_root), "kwargs": _canonical(kwargs, tmp_root)}


# ------------------------------------------------------------ observable fields


def media_digests(capture: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Every object the host uploaded: digest, size and media identity."""
    return list(capture["uploads"])


def delivery_rows(capture: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The outputs the settlement delivers. A run that never settles delivers none."""
    if not capture["settlements"]:
        return []
    return list(capture["settlements"][-1].get("outputs", []))


def receipt(capture: Mapping[str, Any]) -> dict[str, Any]:
    """The settlement receipt without its delivery rows, or the failure receipt."""
    if capture["settlements"]:
        settled = dict(capture["settlements"][-1])
        settled.pop("outputs", None)
        return {"raised": capture["raised"], "outcome": "settled", "settlement": settled}
    if not capture["failures"]:
        return {"raised": capture["raised"], "outcome": "none"}
    kwargs = capture["failures"][-1]["kwargs"]
    return {
        "raised": capture["raised"],
        "outcome": "failed",
        "attempt_id": kwargs.get("attempt_id"),
        "fence": kwargs.get("fence"),
        "retryable": kwargs.get("retryable"),
        "status": (kwargs.get("result") or {}).get("status"),
        "error_code": ((kwargs.get("result") or {}).get("error") or {}).get("code"),
    }


# ------------------------------------------------------------------------ tests


@pytest.fixture(scope="module")
def golden() -> dict[str, Any]:
    return json.loads(GOLDEN.read_text(encoding="utf-8"))


@pytest.mark.parametrize("case", list(CASE_INPUTS))
def test_media_digests_match_frozen_old_route(case: str, tmp_path: Path, monkeypatch: Any, golden: dict[str, Any]) -> None:
    new = run_task_arm(tmp_path, CASE_INPUTS[case], monkeypatch)
    assert media_digests(new) == media_digests(golden[case])


@pytest.mark.parametrize("case", list(CASE_INPUTS))
def test_submitted_settings_match_frozen_old_route(case: str, tmp_path: Path, monkeypatch: Any, golden: dict[str, Any]) -> None:
    new = run_task_arm(tmp_path, CASE_INPUTS[case], monkeypatch)
    assert new["submitted_settings"] == golden[case]["submitted_settings"]


# Intended deltas from the frozen pre-swap route, each asserted EXACTLY (review 88 /
# M-GPU 12:14Z). Anything else in the receipt must equal the golden.
#
# 1. The swap changed the wan definition, so both digests change. These are the new
#    exact values, pinned here, not the pre-swap golden values.
NEW_CAPABILITY_DIGEST = "sha256:b79846b685c49acea3f173116132214af789b0d976b3b663769aebb91b5a420d"
NEW_SOURCE_DIGEST = "3561beb52368eb6e6214ba9c1e93b9ee4a8409f71b63899489445f80982ba20b"
# 2. Native admission identifiers stay on the host (review S-3): they must be ABSENT.
REMOVED_IDENTITY_KEYS = ("invocation_id", "session_id", "generation", "binding_identity")


@pytest.mark.parametrize("case", list(CASE_INPUTS))
def test_receipt_matches_frozen_old_route(case: str, tmp_path: Path, monkeypatch: Any, golden: dict[str, Any]) -> None:
    from astrid.packs.wan2gp.src.driver import compile_host_settings

    new = run_task_arm(tmp_path, CASE_INPUTS[case], monkeypatch)
    actual = receipt(new)
    expected = copy.deepcopy(receipt(golden[case]))

    actual_result = actual["settlement"]["result"]
    expected_result = expected["settlement"]["result"]

    # Delta 1: the new digests, exactly.
    assert actual_result["capability_digest"] == NEW_CAPABILITY_DIGEST
    assert actual_result["source_digest"] == NEW_SOURCE_DIGEST
    expected_result["capability_digest"] = NEW_CAPABILITY_DIGEST
    expected_result["source_digest"] = NEW_SOURCE_DIGEST

    # Delta 2: identity keys are absent from the consumer-visible native result.
    for key in REMOVED_IDENTITY_KEYS:
        assert key not in actual_result["wan_native"], key
        expected_result["wan_native"].pop(key, None)

    # Delta 3: compiled_settings is the host's compile of the admitted inputs, exactly.
    assert actual_result["wan_native"]["compiled_settings"] == compile_host_settings(dict(CASE_INPUTS[case]))
    expected_result["wan_native"]["compiled_settings"] = compile_host_settings(dict(CASE_INPUTS[case]))

    # Delta 4: process_id is the pid the test observed when the host launched the worker.
    observed = new["observed_worker_pids"]
    assert len(observed) == 1, observed
    assert actual_result["process_evidence"]["process_id"] == observed[0]
    expected_result["process_evidence"]["process_id"] = observed[0]

    assert actual == expected


@pytest.mark.parametrize("case", list(CASE_INPUTS))
def test_delivery_rows_match_frozen_old_route(case: str, tmp_path: Path, monkeypatch: Any, golden: dict[str, Any]) -> None:
    new = run_task_arm(tmp_path, CASE_INPUTS[case], monkeypatch)
    assert delivery_rows(new) == delivery_rows(golden[case])


def test_golden_is_the_pre_swap_run_task_capture(golden: dict[str, Any]) -> None:
    """The golden records which tree produced it; it must not be silently regenerated."""
    assert set(golden) - {"_meta"} == set(CASE_INPUTS)
    assert golden["fixture_inputs"]["raised"] is None
    assert len(golden["fixture_inputs"]["settlements"]) == 1
    assert len(golden["explicit_inputs"]["settlements"]) == 1


def test_live_old_path_still_produces_the_frozen_media(tmp_path: Path, golden: dict[str, Any]) -> None:
    """The old method stays reachable from this module only; its live output must match the golden media."""
    from tests.packs.wan2gp.test_session_parity_harness import (
        FakeEngineSession, TOKEN, _load_fixture, _native_evidence, run_host_path,
    )

    fixture = _load_fixture()
    evidence = _native_evidence(TOKEN, tmp_path / "native-spool", fixture)
    payload = run_host_path(
        dict(fixture["inputs"]), token=TOKEN, attempt=tmp_path / "attempt",
        output_root=tmp_path / "out", session=FakeEngineSession(evidence),
    )
    staged = payload["wan_mapping"]["staged_outputs"]
    live_media = [{"digest": item["sha256"], "size": item["stat"][_STAT_SIZE_INDEX]} for item in staged]
    frozen = [{"digest": row["digest"], "size": row["size"]} for row in golden["fixture_inputs"]["uploads"]]
    assert live_media == frozen
