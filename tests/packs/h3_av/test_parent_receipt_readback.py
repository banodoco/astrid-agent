"""The helper observes Runtime settlement truth after host scratch deletion."""
from threading import RLock
from types import SimpleNamespace

import pytest

from tests.helpers.h3_verified_parent import VerifiedH3Host


@pytest.mark.parametrize("state", ["succeeded", "failed"])
def test_receipt_readback_preserves_terminal_state_and_host_evidence(tmp_path, state):
    host_result = {"process_evidence": {"process_id": 123}, "status": "candidate_verified"}
    parent = {"id": "parent", "attempt_id": "attempt", "state": state, "result": host_result}
    child = {"id": "child", "attempt_id": "child-attempt", "state": "succeeded", "result": host_result}
    service = SimpleNamespace(store=SimpleNamespace(
        _mutex=RLock(), get_task=lambda task: parent,
        delegated_children=lambda task, attempt: [child],
    ), _task_resource=lambda row: {**row, "task_id": row["id"]})
    helper = object.__new__(VerifiedH3Host)
    helper.root = tmp_path
    helper.daemon = SimpleNamespace(service=service)
    helper.qualification = {"task_id": "parent"}
    rows = helper.receipts()
    assert rows[0]["status"] == state
    assert rows[0]["result"] == host_result
    assert [row["task_id"] for row in rows] == ["parent", "child"]
    assert (tmp_path / "h3-terminal-receipts.json").is_file()
