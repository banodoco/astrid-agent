"""Focused checks for the exact-candidate R-24 to Agent-harness seam."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import ci_journey, ci_journey_provider as provider
from scripts import ci_journey_r24 as r24
from scripts import pinokio_r24_journey_seam as seam
from scripts import journey_predicates


def _captured_receipt() -> dict[str, object]:
    exports = []
    for index, name in enumerate(sorted(seam._EXPORT_NAMES), 1):
        exports.append({"path": name, "bytes": index, "sha256": f"{index:x}" * 64})
    return {
        "schema": "astrid.r24.journey-provider-invocation.v1",
        "status": "captured-untrusted",
        "candidate": dict(r24.R24_CANDIDATE),
        "run_id": "a" * 32,
        "request_sha256": "b" * 64,
        "session": "c" * 32,
        "provider": {
            "ref": provider.LIVE_PRODUCER_REF,
            "sources": dict(provider.LIVE_PRODUCER_FILES),
            "interface": dict(provider.LIVE_INTERFACE_FILES),
        },
        "invocation": {"status": "completed", "exit": 0},
        "admission": {"status": "awaiting-evidence", "authenticated_custody": False,
                      "qualification": "not-evaluated"},
        "export": {"path": "provider-export", "files": exports},
    }


def _paths(tmp_path: Path) -> dict[str, Path]:
    paths = {name: tmp_path / name for name in ("app", "agent", "runtime", "pinokio")}
    for path in paths.values():
        path.mkdir()
    return paths


def test_exact_candidate_provider_and_harness_constants_are_identical() -> None:
    assert seam.SCHEMA == "astrid.r25.pinokio-r24-seam.v1"
    assert r24.R24_CANDIDATE == provider.LIVE_CANDIDATE
    assert provider.LIVE_PRODUCER_REF == "93c21a16dc94269c0c575f1a721e9b01ef1f386b"
    assert r24.R24_CANDIDATE == {
        "app": "bfb15570d72c39fbdc32f6209ecec7c929b71d5c",
        "agent": "69dda026212ca16c37cc8239d06ccd159603aa24",
        "runtime": "2776ba973b6c8f3555b5629b0840d9691a98d3c2",
        "pinokio": "7955800419fc92b11e553d6f7acb7613090617e6",
        "manifest_sha256": "09707100ad238cf206085aa5b267ca4108e482ac50e06068a3475932180b65bd",
    }


def test_executes_existing_live_hook_once_and_keeps_the_m4_6_boundary_closed(tmp_path, monkeypatch) -> None:
    calls = []

    def live_hook(**kwargs):
        calls.append(kwargs)
        return _captured_receipt()

    monkeypatch.setattr(seam.r24, "execute_live_hook", live_hook)
    paths = _paths(tmp_path)
    output = tmp_path / "seam.json"
    result = seam.execute(
        run_root=tmp_path,
        candidate_file=tmp_path / "candidate.json",
        config_file=tmp_path / "config.json",
        producer_root=tmp_path,
        checkouts=paths,
        output=output,
    )
    assert len(calls) == 1
    assert calls[0]["checkouts"] == paths
    assert json.loads(output.read_text()) == result
    assert result["candidate"] == r24.R24_CANDIDATE
    assert result["scope"] == {
        "observations": ["busy", "unknown", "restoration"],
        "operation": "pinokio-stop-preflight",
        "m4_6_mode_binding": "absent",
    }
    assert result["boundary"] == {
        "authenticated_custody": False,
        "qualification": "not-evaluated",
        "missing": [
            "mode-specific backend or fully-local M4-6 action binding",
            "M4-6 interruption, rollback, and keep-data-uninstall observations",
            "independent R-30 section 5 observation proofs and frozen R-36 bindings",
        ],
    }
    accepted, reasons = journey_predicates.check_m4_6(result)
    assert accepted is False
    assert any("slot_id" in reason for reason in reasons)
    assert any("independent section 5" in reason for reason in reasons)


@pytest.mark.parametrize(
    "mutate, message",
    [
        (lambda value: value.__setitem__("status", "complete"), "did not capture"),
        (lambda value: value["candidate"].__setitem__("agent", "0" * 40), "candidate differs"),
        (lambda value: value["provider"].__setitem__("ref", "0" * 40), "provider binding differs"),
        (lambda value: value["admission"].__setitem__("authenticated_custody", True), "admission boundary"),
        (lambda value: value["export"]["files"].pop(), "export inventory differs"),
    ],
)
def test_rejects_any_attempt_to_promote_or_incomplete_r24_capture(mutate, message) -> None:
    receipt = _captured_receipt()
    mutate(receipt)
    with pytest.raises(seam.PinokioR24SeamError, match=message):
        seam._captured(receipt)


def test_rejects_unsafe_or_reused_output_before_live_hook(tmp_path, monkeypatch) -> None:
    called = False

    def live_hook(**_kwargs):
        nonlocal called
        called = True
        return _captured_receipt()

    monkeypatch.setattr(seam.r24, "execute_live_hook", live_hook)
    paths = _paths(tmp_path)
    output = tmp_path / "seam.json"
    output.write_text("existing")
    with pytest.raises(seam.PinokioR24SeamError, match="absent absolute"):
        seam.execute(run_root=tmp_path, candidate_file=output, config_file=output,
                     producer_root=tmp_path, checkouts=paths, output=output)
    assert called is False


def test_shared_executor_exposes_one_linux_only_nonreceipt_route(tmp_path, monkeypatch) -> None:
    captured = {}

    def execute(**kwargs):
        captured.update(kwargs)
        return {}

    monkeypatch.setattr("scripts.pinokio_r24_journey_seam.execute", execute)
    args = [
        "r25-r24-seam", "--run-root", str(tmp_path), "--candidate", str(tmp_path / "candidate"),
        "--config", str(tmp_path / "config"), "--producer-checkout", str(tmp_path),
        "--app-checkout", str(tmp_path / "app"), "--agent-checkout", str(tmp_path / "agent"),
        "--runtime-checkout", str(tmp_path / "runtime"), "--pinokio-checkout", str(tmp_path / "pinokio"),
        "--output", str(tmp_path / "output"),
    ]
    assert ci_journey._shared_executor_main(args) == 0
    assert captured["run_root"] == tmp_path
    assert captured["checkouts"] == {
        "app": tmp_path / "app", "agent": tmp_path / "agent",
        "runtime": tmp_path / "runtime", "pinokio": tmp_path / "pinokio",
    }
