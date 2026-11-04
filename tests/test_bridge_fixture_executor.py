from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest

from scripts.bridge_fixture_executor import BridgeFixtureError, execute_fixture


FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "release1_bridge"
WORKER = FIXTURE_ROOT / "worker.py"


def run_fixture(name: str) -> dict:
    return execute_fixture(
        FIXTURE_ROOT / "scenarios" / f"{name}.json",
        [sys.executable, str(WORKER)],
    )


def test_success_proves_exact_boundary_argv_environment_and_identity() -> None:
    result = run_fixture("success")
    receipt = result["receipt"]
    assert result["qualification"] == "implementation-evidence-only"
    assert receipt["argv"][0] == "--manifest"
    assert receipt["argv"][2] == "--root"
    assert receipt["argv"][4] == "--data-root"
    assert receipt["argv"][6] == "--update-job"
    assert "with spaces" in receipt["paths"]["root"]
    assert "with spaces" in receipt["paths"]["data_root"]
    assert receipt["environment"]["ASTRID_BRIDGE_NODE_VERSION"] == "20.19.4"
    assert receipt["http"][0]["path"] == "/v1/update"
    assert receipt["http"][0]["method"] == "POST"
    assert receipt["http"][1]["path"] == "/v1/update/status"
    assert receipt["records"]["operation_id"] == result["operation_id"]


def test_refused_stop_and_lock_do_not_progress() -> None:
    receipt = run_fixture("refusal")["receipt"]
    assert receipt["http"][0]["status"] == 409
    assert receipt["http"][0]["response"]["state"] == "refused"
    assert receipt["stop"]["ack"]["status"] == "refused"
    assert receipt["stop"]["proceeded"] is False
    assert receipt["stop"]["lock"]["present"] is True


def test_lost_ack_reads_recovery_status_without_post_retry() -> None:
    receipt = run_fixture("lost-ack")["receipt"]
    assert receipt["http"][0]["observation"] == "lost-ack"
    assert receipt["http"][0]["response"] is None
    assert receipt["http"][1]["response"]["state"] == "recovery-needed"
    assert receipt["records"]["journal_path"].endswith("update-transaction.json")
    assert receipt["stop"]["proceeded"] is False


def test_release0_skip_consumes_authentic_service_and_served_inputs() -> None:
    receipt = run_fixture("release0-skip")["receipt"]
    release0 = receipt["release0"]
    assert release0["source_kind"] == "authentic-release0-fixture"
    assert len(release0["consumed"]) == 8
    assert release0["release2_skip"] == {
        "release1_skipped": True,
        "target_release": 2,
        "retained_service_files": [
            "release0/services/launchd.plist",
            "release0/services/systemd.service",
            "release0/services/task.xml",
            "release0/services/service-env.json",
        ],
    }


@pytest.mark.parametrize("name", ["malformed", "missing"])
def test_malformed_or_missing_records_fail_closed(name: str) -> None:
    with pytest.raises(BridgeFixtureError):
        run_fixture(name)


def test_synthetic_worker_tag_is_rejected(tmp_path: Path) -> None:
    worker = tmp_path / "synthetic_worker.py"
    worker.write_text(
        """
import json, os
from pathlib import Path
Path(os.environ['ASTRID_BRIDGE_RECEIPT']).write_text(json.dumps({
  'format': 'astrid-bridge-worker/v1',
  'evidence': {
    'mode': 'source-only', 'boundary': 'source', 'synthetic': True,
    'source_only': True, 'worker_pid': os.getpid(), 'token': os.environ['ASTRID_BRIDGE_TOKEN']
  },
  'environment': {}, 'fixture_provenance': {}, 'argv': [], 'paths': {}
}))
""",
        encoding="utf-8",
    )
    with pytest.raises(BridgeFixtureError, match="synthetic or source-only"):
        execute_fixture(
            FIXTURE_ROOT / "scenarios" / "success.json",
            [sys.executable, str(worker)],
        )


def test_worker_command_cannot_be_source_only() -> None:
    with pytest.raises(BridgeFixtureError, match="synthetic/source-only"):
        execute_fixture(FIXTURE_ROOT / "scenarios" / "success.json", [sys.executable, "-c", "pass"])
