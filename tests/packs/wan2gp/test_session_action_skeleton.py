"""M2-11a skeleton tests: ``generate_video_session`` against a fake session.

The fake session records calls and returns canned native evidence. It is not
the real ``SessionClient``. Error text and top-level return keys are checked
against ``GenericPackHost._run_wan_session``, run with only the engine edge
replaced.

Known differences from ``_run_wan_session`` (the skeleton is not yet parity):

- No ``expected_identity`` is passed to ``materialize_host_result``. The worker
  holds no admission token, so the identity check has to come from the host.
- ``verify_host_result`` is called right after materialize. The host calls it
  later, at settlement, on the same mapping.
- No ``cancelled()`` callable: the host's cancelled branches
  (``HostCancelled`` and the cancelled payload) have no equivalent here.
- No managed-tool-session ``adapter_for`` calls. Those are host-only.
- Errors are ``RuntimeError``, not ``HostError``. The pack does not import the host.
- Returns a dict with the host's four keys, not a ``SimpleNamespace``.
- Takes ``output_root`` and ``timeout_seconds`` as keyword-only inputs. The host
  derives them from its own deadline and output root.
"""

from __future__ import annotations

import copy
import json
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from astrid.core.execution.generic_host import GenericPackHost, _ManagedWanChildAdapter
from astrid.core.execution.managed_tool_session import AdmissionToken
from astrid.packs.wan2gp.actions.generate_video_session.run import generate_video_session
from astrid.packs.wan2gp.src.driver import compile_host_settings, snapshot_native_outputs

FIXTURE = Path(__file__).parent / "fixtures" / "one_frame_engine_result.json"

TOKEN = AdmissionToken(
    token_id="tok-skeleton-1",
    session_id="sess-skeleton-1",
    capability_id="wan2gp",
    invocation_id="inv-skeleton-1",
    generation=1,
    binding_identity=("binding", 1),
)

INPUTS: dict[str, Any] = {"prompt": "skeleton frame", "model": "wan-2.2"}
HOST_MESSAGE_PREFIX = "native Wan2GP job failed: "


# ----------------------------------------------------------------- helpers


def _fixture() -> dict[str, Any]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _evidence(source_root: Path, *, success: bool = True, errors: list[str] | None = None) -> dict[str, Any]:
    """Native evidence for one frame, shaped like the child's terminal observation."""
    fixture = _fixture()
    source_root.mkdir(parents=True, exist_ok=True)
    generated = list(fixture["generated_files"])
    for name in generated:
        (source_root / name).write_bytes(str(fixture["output_bytes_text"]).encode("utf-8"))
    return {
        **_ManagedWanChildAdapter.identity(TOKEN),
        "native_job_id": fixture["native_job_id"],
        "events": [],
        "result": {"success": success, "errors": list(errors or []), "generated_files": generated},
        "birth": fixture["birth"],
        "process_id": fixture["process_id"],
        "returncode": fixture["returncode"],
        "terminal": True,
        "source_root": str(source_root.absolute()),
        "output_snapshots": snapshot_native_outputs(generated, source_root),
        "native_error": None,
    }


class FakeSession:
    """Records every call. ``submit`` returns a deep copy of canned evidence."""

    def __init__(self, evidence: Any = None, *, submit_error: Exception | None = None,
                 cancel_error: Exception | None = None) -> None:
        self._evidence = copy.deepcopy(evidence)
        self._submit_error = submit_error
        self._cancel_error = cancel_error
        self.submit_calls: list[dict[str, Any]] = []
        self.cancel_calls: list[str] = []

    def submit(self, settings: Any, timeout_seconds: float) -> Any:
        self.submit_calls.append({"settings": dict(settings), "timeout_seconds": timeout_seconds})
        if self._submit_error is not None:
            raise self._submit_error
        return copy.deepcopy(self._evidence)

    def cancel(self, reason: str) -> dict[str, Any]:
        self.cancel_calls.append(reason)
        if self._cancel_error is not None:
            raise self._cancel_error
        return {"state": "cancel_requested"}

    def progress(self) -> dict[str, Any]:  # pragma: no cover - not used by the action
        raise AssertionError("the action must not poll progress")


def _run_action(tmp_path: Path, session: FakeSession, **overrides: Any) -> Any:
    kwargs: dict[str, Any] = {
        **INPUTS,
        "output_root": tmp_path / "out",
        "timeout_seconds": 30.0,
        "session": session,
    }
    kwargs.update(overrides)
    return generate_video_session(**kwargs)


class _FakeManagedToolSession:
    def adapter_for(self, token: AdmissionToken, begin: bool = False) -> None:
        return None


def _host_outcome(tmp_path: Path, evidence: dict[str, Any]) -> Any:
    """Run the real ``_run_wan_session`` with only the engine edge faked."""
    host = GenericPackHost.__new__(GenericPackHost)
    host.managed_tool_session = _FakeManagedToolSession()
    host.invoke_wan_session = lambda token, settings, **_: copy.deepcopy(evidence)
    return host._run_wan_session(
        inputs=dict(INPUTS),
        output_root=tmp_path / "host-out",
        attempt=tmp_path / "attempt",
        managed_token=TOKEN,
        execution_deadline=time.monotonic() + 30.0,
        cancelled=lambda: False,
    )


# ----------------------------------------------------------------- success


def test_success_submits_once_and_never_cancels(tmp_path: Path) -> None:
    session = FakeSession(_evidence(tmp_path / "src"))

    result = _run_action(tmp_path, session)

    assert len(session.submit_calls) == 1
    assert session.submit_calls[0]["settings"] == compile_host_settings(INPUTS)
    assert session.submit_calls[0]["timeout_seconds"] == 30.0
    assert session.cancel_calls == []
    assert (tmp_path / "out" / "frame_000.mp4").is_file()
    assert result["output_root"] == tmp_path / "out"
    assert result["payload"]["wan_mapping"]["output_root"] == str((tmp_path / "out").absolute())


def test_success_return_keys_match_run_wan_session(tmp_path: Path) -> None:
    evidence = _evidence(tmp_path / "src")

    skeleton = _run_action(tmp_path, FakeSession(evidence))
    host = _host_outcome(tmp_path, evidence)

    assert isinstance(host, SimpleNamespace)
    assert set(skeleton) == set(vars(host))
    assert set(skeleton["payload"]) == set(host.payload)
    assert skeleton["process_id"] == host.process_id
    assert skeleton["returncode"] == host.returncode


# ----------------------------------------------------------------- failure after submit


def test_failed_result_raises_host_message_and_cancels_once(tmp_path: Path) -> None:
    evidence = _evidence(tmp_path / "src", success=False, errors=["oom", "retry exhausted"])
    session = FakeSession(evidence)

    with pytest.raises(RuntimeError) as raised:
        _run_action(tmp_path, session)

    assert str(raised.value) == HOST_MESSAGE_PREFIX + "oom; retry exhausted"
    assert len(session.submit_calls) == 1
    assert len(session.cancel_calls) == 1


def test_failed_result_message_matches_run_wan_session(tmp_path: Path) -> None:
    evidence = _evidence(tmp_path / "src", success=False, errors=["oom", "retry exhausted"])

    with pytest.raises(RuntimeError) as skeleton_error:
        _run_action(tmp_path, FakeSession(evidence))
    with pytest.raises(Exception) as host_error:
        _host_outcome(tmp_path, evidence)

    assert str(skeleton_error.value) == str(host_error.value)


def test_native_error_fallback_message_matches_run_wan_session(tmp_path: Path) -> None:
    evidence = _evidence(tmp_path / "src", success=False, errors=[])
    evidence["native_error"] = "child exited"

    with pytest.raises(RuntimeError) as skeleton_error:
        _run_action(tmp_path, FakeSession(evidence))
    with pytest.raises(Exception) as host_error:
        _host_outcome(tmp_path, evidence)

    assert str(skeleton_error.value) == HOST_MESSAGE_PREFIX + "child exited"
    assert str(skeleton_error.value) == str(host_error.value)


def test_non_mapping_evidence_fails_closed_and_cancels_once(tmp_path: Path) -> None:
    session = FakeSession("not-a-mapping")

    with pytest.raises(RuntimeError, match=r"^native Wan2GP job failed: $"):
        _run_action(tmp_path, session)

    assert len(session.submit_calls) == 1
    assert len(session.cancel_calls) == 1


def test_output_tampered_after_terminal_observation_cancels_once(tmp_path: Path) -> None:
    evidence = _evidence(tmp_path / "src")
    (tmp_path / "src" / "frame_000.mp4").write_bytes(b"changed after observation")
    session = FakeSession(evidence)

    with pytest.raises(RuntimeError, match="changed"):
        _run_action(tmp_path, session)

    assert len(session.submit_calls) == 1
    assert len(session.cancel_calls) == 1


def test_cancel_failure_does_not_mask_original_error(tmp_path: Path) -> None:
    evidence = _evidence(tmp_path / "src", success=False, errors=["boom"])
    session = FakeSession(evidence, cancel_error=RuntimeError("channel closed"))

    with pytest.raises(RuntimeError) as raised:
        _run_action(tmp_path, session)

    assert str(raised.value) == HOST_MESSAGE_PREFIX + "boom"
    assert len(session.cancel_calls) == 1


# ----------------------------------------------------------------- failure before submit


def test_failed_submit_propagates_without_cancel(tmp_path: Path) -> None:
    session = FakeSession(submit_error=ConnectionError("transport_uncertain"))

    with pytest.raises(ConnectionError, match="transport_uncertain"):
        _run_action(tmp_path, session)

    assert len(session.submit_calls) == 1
    assert session.cancel_calls == []


def test_invalid_inputs_never_submit_or_cancel(tmp_path: Path) -> None:
    session = FakeSession(_evidence(tmp_path / "src"))

    with pytest.raises(ValueError):
        _run_action(tmp_path, session, prompt="")

    assert session.submit_calls == []
    assert session.cancel_calls == []
