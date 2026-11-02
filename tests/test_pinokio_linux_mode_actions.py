"""Focused tests for bounded Linux mode-action harness integration."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from scripts import ci_journey, ci_journey_r24 as r24
from scripts import journey_predicates
from scripts import pinokio_linux_mode_actions as actions


def _r24_record() -> dict[str, object]:
    return {
        "schema": "astrid.r25.pinokio-r24-seam.v1",
        "candidate": dict(r24.R24_CANDIDATE),
        "r24": {"run_id": "a" * 32, "request_sha256": "b" * 64,
                "session": "c" * 32, "producer_ref": "d" * 40,
                "producer_sources": {}, "interface_sources": {}, "export": []},
    }


def _provider_result(request: dict, mode: str) -> dict:
    operations = actions.OPERATIONS[mode]
    return {
        "schema": actions.RESULT_SCHEMA,
        "request_sha256": actions._sha(actions._canonical(request)),
        "r24_request_sha256": request["r24_request_sha256"],
        "r24_seam_sha256": request["r24_seam_sha256"],
        "run_id": request["run_id"],
        "session": "e" * 32,
        "slot_id": actions.MODES[mode],
        "mode": mode,
        "actions": [{"operation": operation, "uri": f"~/api/astrid/{operation}.js",
                     "request_id": f"request-{operation}", "refusal_sha256": "f" * 64,
                     "fenced": True} for operation in operations],
        "protected": {"before_sha256": "1" * 64, "after_sha256": "1" * 64,
                      "unchanged": True},
        "scenarios": {
            "stop_safety": {"status": "partial"},
            "interruption_recovery": {"status": "not-run"},
            "failed_candidate_rollback": {"status": "not-reached-after-preflight-refusal"},
            "keep_data_uninstall": {"status": "busy-refusal-observed",
                                    "successful_uninstall": False},
        },
        "boundary": {
            "authenticated_custody": False,
            "qualification": "not-evaluated",
            "m4_6_receipt": False,
            "missing": "R-36 authenticated platform session/removal authorization/finalization interface",
        },
    }


@pytest.mark.parametrize("mode", ("backend", "fully-local"))
def test_executes_r24_once_then_mode_provider_without_receipt(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    run_root = tmp_path / "run"
    (run_root / "evidence/r24-live").mkdir(parents=True)
    r24_request = {"schema": "r24-fixture", "run_id": "a" * 32}
    (run_root / "evidence/r24-live/request.json").write_text(json.dumps(r24_request))
    candidate = tmp_path / "candidate.json"
    candidate.write_text(json.dumps(r24.R24_CANDIDATE))
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"fixture": True}))
    producer = tmp_path / "r24-producer"
    mode_producer = tmp_path / "mode-producer"
    producer.mkdir(); mode_producer.mkdir()
    checkouts = {name: tmp_path / name for name in ("app", "agent", "runtime", "pinokio")}
    for path in checkouts.values(): path.mkdir()
    calls = []

    def seam_execute(**kwargs):
        calls.append(kwargs)
        value = _r24_record()
        kwargs["output"].write_bytes(actions._canonical(value) + b"\n")
        return value

    monkeypatch.setattr(actions.seam, "execute", seam_execute)
    monkeypatch.setattr(actions, "_checkout", lambda path, ref, sources: path)
    monkeypatch.setattr(actions, "_runner_identity",
                        lambda: {"head": "9" * 40, "sources": {}})

    def invoke(argv, **_kwargs):
        request = json.loads(Path(argv[4]).read_text())
        return subprocess.CompletedProcess(argv, 0,
                                           actions._canonical(_provider_result(request, mode)), b"")

    monkeypatch.setattr(actions.subprocess, "run", invoke)
    output = tmp_path / "output"
    result = actions.execute(
        mode=mode, run_root=run_root, candidate_file=candidate, config_file=config,
        r24_producer_root=producer, mode_producer_root=mode_producer,
        checkouts=checkouts, output=output)
    assert len(calls) == 1
    assert result["slot_id"] == actions.MODES[mode]
    assert result["boundary"]["authenticated_custody"] is False
    assert result["boundary"]["m4_6_receipt"] is False
    assert json.loads((output / "mode-action-seam.json").read_text()) == result
    accepted, reasons = journey_predicates.check_m4_6(result)
    assert accepted is False
    assert any("independent section 5" in reason for reason in reasons)


def test_result_validation_rejects_promoted_protected_claims() -> None:
    request = {"r24_request_sha256": "1" * 64, "r24_seam_sha256": "2" * 64,
               "run_id": "3" * 32}
    result = _provider_result(request, "backend")
    result["request_sha256"] = actions._sha(actions._canonical(request))
    result["boundary"]["authenticated_custody"] = True
    with pytest.raises(actions.LinuxModeActionError, match="boundary differs"):
        actions._validate_result(result, request, "backend")


def test_shared_executor_routes_exact_mode_and_producer_paths(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    captured = {}
    monkeypatch.setattr("scripts.pinokio_linux_mode_actions.execute",
                        lambda **kwargs: captured.update(kwargs))
    argv = ["r25-linux-mode-actions", "--mode", "backend",
            "--run-root", str(tmp_path / "run"), "--candidate", str(tmp_path / "candidate"),
            "--config", str(tmp_path / "config"), "--producer-checkout", str(tmp_path / "r24"),
            "--mode-producer-checkout", str(tmp_path / "mode-provider"),
            "--app-checkout", str(tmp_path / "app"), "--agent-checkout", str(tmp_path / "agent"),
            "--runtime-checkout", str(tmp_path / "runtime"),
            "--pinokio-checkout", str(tmp_path / "pinokio"), "--output", str(tmp_path / "output")]
    assert ci_journey._shared_executor_main(argv) == 0
    assert captured["mode"] == "backend"
    assert captured["r24_producer_root"] == tmp_path / "r24"
    assert captured["mode_producer_root"] == tmp_path / "mode-provider"
