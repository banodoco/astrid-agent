"""Host-enforced settings identity (M2-11a): the worker's settings are never trusted.

(c) The host recomputes settings from its own inputs. A worker-submitted settings
mapping that differs is refused before any invoke, and a matching one reaches the
invoke path exactly once with the HOST-compiled settings object.
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from astrid.core.execution import generic_host as gh
from astrid.packs.wan2gp.src.driver import compile_host_settings

FIXTURE = Path(__file__).resolve().parents[2] / "packs" / "wan2gp" / "fixtures" / "one_frame_engine_result.json"


class _Stub:
    def __init__(self) -> None:
        self.invocations: list[dict] = []
        self.managed_tool_session = SimpleNamespace(cancel=lambda token, outcome=None: None)

    def invoke_wan_session(self, token, settings, *, timeout, cancelled, progress_path):
        self.invocations.append({"settings": settings, "timeout": timeout})
        return {"result": {"success": True}}


def _adapter(stub: _Stub, tmp_path: Path):
    inputs = json.loads(FIXTURE.read_text(encoding="utf-8"))["inputs"]
    profile = gh.ManagedToolProfile(compile_settings=compile_host_settings, invoke=stub.invoke_wan_session)
    adapter = gh._HostManagedSessionAdapter(
        profile=profile, managed_token=object(), inputs=inputs,
        progress_path=tmp_path / "progress.json", timeout_cap=30.0, cancelled=lambda: False,
        cancel_session=lambda token, reason: None)
    return adapter, inputs


def test_mismatched_worker_settings_are_refused_before_any_invoke(tmp_path: Path) -> None:
    stub = _Stub()
    adapter, inputs = _adapter(stub, tmp_path)
    forged = dict(compile_host_settings(inputs))
    forged["prompt"] = "worker-chosen prompt"
    with pytest.raises(ValueError, match="differ from the host-compiled"):
        adapter.validate_settings(forged)
    assert stub.invocations == []


def test_matching_settings_reach_invoke_once_with_host_compiled_settings(tmp_path: Path) -> None:
    stub = _Stub()
    adapter, inputs = _adapter(stub, tmp_path)
    worker_copy = dict(inputs)
    adapter.validate_settings(worker_copy)
    adapter.submit(worker_copy, timeout_seconds=5.0)
    assert len(stub.invocations) == 1
    assert stub.invocations[0]["settings"] == compile_host_settings(inputs)


def test_submit_is_allowed_once_per_run(tmp_path: Path) -> None:
    stub = _Stub()
    adapter, inputs = _adapter(stub, tmp_path)
    adapter.submit(dict(inputs), timeout_seconds=5.0)
    with pytest.raises(RuntimeError, match="once per run"):
        adapter.submit(dict(inputs), timeout_seconds=5.0)
    assert len(stub.invocations) == 1
