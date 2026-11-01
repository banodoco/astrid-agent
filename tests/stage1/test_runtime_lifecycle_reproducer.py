from __future__ import annotations

import json
import shutil
from pathlib import Path
import subprocess
import sys

import pytest

from astrid.sdk import autobootstrap
from astrid.sdk.client import AstridClient
from astrid.sdk.workspace_client import PROTOCOL
from tests.lanes import unavailable


ROOT = Path(__file__).resolve().parents[2]
HARNESS = ROOT / "scripts" / "diagnostics" / "runtime_lifecycle_reproducer.py"


def _pinned_launcher() -> Path:
    """The astrid-runtime console script the pinned dev extra installs: beside this interpreter, else on PATH."""
    sibling = Path(sys.executable).parent / "astrid-runtime"
    if sibling.is_file():
        return sibling
    found = shutil.which("astrid-runtime")
    if found is None:
        unavailable("astrid-runtime is not installed; install the dev extra")
    return Path(found)


def _run_harness(tmp_path: Path, mode: str) -> dict[str, object]:
    output = tmp_path / f"{mode}.json"
    result = subprocess.run(
        [sys.executable, str(HARNESS), "--mode", mode, "--callers", "3", "--output", str(output)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
        timeout=20,
    )
    assert json.loads(result.stdout) == json.loads(output.read_text(encoding="utf-8"))
    return json.loads(result.stdout)


@pytest.mark.timeout(30)
def test_cold_concurrent_acquisition_serializes_to_one_owner(tmp_path: Path) -> None:
    report = _run_harness(tmp_path, "cold")
    assert report["owner_count"] == 1
    assert report["launcher_invocations"] == 3
    assert report["discovery"]["runtime_instance_id"] == "fixture-instance"
    assert report["classification"]["lifecycle"]["attempted"] == 3
    assert report["classification"]["lifecycle"]["successes"] == 3
    assert report["classification"]["lifecycle"]["failures"] == 0
    assert sorted(report["lifecycle_events"]) == [
        "reconnected",
        "reconnected",
        "started",
    ]
    assert report["classification"]["pack_worker"]["attempted"] == 0
    assert report["classification"]["timeline_visualization"]["attempted"] == 0
    errors = [item["error"] for item in report["outcomes"] if not item["ok"]]
    assert all(error["type"] == "AutoBootstrapError" for error in errors)
    assert all("not ready" in error["message"] for error in errors)


@pytest.mark.timeout(30)
def test_warm_concurrent_acquisition_reconnects_without_lifecycle_churn(tmp_path: Path) -> None:
    report = _run_harness(tmp_path, "warm")
    assert report["owner_count"] == 1
    assert report["launcher_invocations"] == 3
    assert report["lifecycle_events"] == ["reconnected", "reconnected", "reconnected"]
    assert report["classification"]["lifecycle"] == {
        "attempted": 3,
        "successes": 3,
        "failures": 0,
    }
    assert all(item["ok"] for item in report["outcomes"])
    assert all(item["result"]["status"] == "reconnected" for item in report["outcomes"])


def test_explicit_client_path_does_not_bootstrap(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    class Workspace:
        def __init__(self, endpoint: str, token: str) -> None:
            assert endpoint == "http://127.0.0.1:43123"
            assert token == "fixture-token"

        def health(self) -> dict[str, object]:
            calls.append("health")
            from astrid_runtime_client.contract_metadata import SCHEMA_DIGEST

            return {
                "status": "ok",
                "protocol": PROTOCOL,
                "schema_digest": SCHEMA_DIGEST,
                "runtime_epoch": 1,
                "runtime_session_id": "fixture-runtime-session",
                "runtime_instance_id": "fixture-instance",
            }

        def handshake(self, *_args: object) -> dict[str, object]:
            calls.append("handshake")
            from astrid_runtime_client.contract_metadata import SCHEMA_DIGEST

            return {
                "protocol": PROTOCOL,
                "schema_digest": SCHEMA_DIGEST,
                "session_id": "fixture-session",
                "actor_id": "fixture-actor",
                "realm_id": "fixture-realm",
                "scopes": [
                    "projects:read",
                    "projects:write",
                    "objects:read",
                    "objects:write",
                    "tasks:read",
                    "tasks:write",
                ],
                "capabilities": ["execution_binding.targeted.v1"],
            }

    monkeypatch.setattr(autobootstrap, "ensure_runtime", lambda: pytest.fail("explicit open bootstrapped"))
    monkeypatch.setattr("astrid.sdk.workspace_client.resolve_runtime_connection", lambda *_args: ("http://127.0.0.1:43123", "fixture-token"))
    monkeypatch.setattr("astrid.sdk.workspace_client.WorkspaceClient", Workspace)
    AstridClient.open(
        endpoint="http://127.0.0.1:43123",
        credential="fixture-token",
        realm_id="fixture-realm",
        actor_id="fixture-actor",
        client_name="fixture",
        client_version="1",
        protocol_version=PROTOCOL,
    )
    assert calls == ["health", "handshake"]


@pytest.mark.darwin_native
@pytest.mark.parametrize("case", ["stale", "interrupted"])
def test_pinned_launcher_recovers_disposable_partial_state(case: str) -> None:
    result = subprocess.run(
        [sys.executable, str(HARNESS), "--recovery-case", case, "--pinned-launcher", str(_pinned_launcher())],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
        timeout=600,
    )
    report = json.loads(result.stdout)
    print(json.dumps({key: report.get(key) for key in ("initial", "recovery", "prepared_state")}, indent=1))
    assert report["initial"]["ok"] is True, report["initial"]
    assert report["prepared_state"]["discovery_present"] is True
    assert report["prepared_state"]["partial_instance_lock"] is (case == "interrupted")
    assert report["recovery"]["ok"] is True, (report["initial"], report["recovery"])
    assert report["recovery"]["result"]["status"] in {"started", "reconnected"}
    assert report["owner_telemetry"]["owner_lock_count"] == 1
    assert report["owner_telemetry"]["discovery_pid_alive_before_cleanup"] is True
    assert report["owner_telemetry"]["duplicate_owner_detected"] is False
