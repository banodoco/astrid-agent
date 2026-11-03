"""Publication undo and redo reuse their exact persisted request after lost ACK."""
import json

import pytest
from astrid.sdk.timeline_checkout import Checkout
from astrid.sdk.timeline.sync import TimelineSession
from astrid.sdk.workspace_client import WorkspaceClientError
from tests.sdk.test_timeline_checkout_history import native  # noqa: F401
from tests.sdk.test_timeline_sync import connected  # noqa: F401


@pytest.mark.parametrize("operation", ["undo", "redo"])
def test_publication_retry_survives_handle_reload(operation, native, tmp_path):
    client, generated = native
    accepted = TimelineSession.open("p", "t", client=client).move("parent/clip:a", 5)
    checkout = Checkout.open("p", "t", revision_id=generated.head["revision_id"], client=client)
    publication = accepted["receipt"]["receipt_id"]
    expected = "r0"
    if operation == "redo":
        undone = checkout.undo(publications=[publication], client=client)
        publication = undone["receipt"]["receipt_id"]
        expected = "r1"
    checkout.save(tmp_path / "detached.json", quiet=True)
    saved = checkout.path.read_bytes()
    generated.lose_ack = True
    with pytest.raises(WorkspaceClientError):
        getattr(checkout, operation)(publications=[publication], client=client,
                                     idempotency_key="review-" + operation)
    flight = json.loads(generated.path.read_text())["flight"]
    reloaded = Checkout.load(checkout.path)
    result = getattr(reloaded, operation)(publications=[publication], client=client)
    assert generated.calls[-1] == generated.calls[-2]
    assert generated.calls[-1][0].decode() == flight["body"]
    assert generated.calls[-1][1] == flight["key"] == "review-" + operation
    assert generated.bundles[result["data"]["accepted_revision_id"]] == generated.bundles[expected]
    assert checkout.path.read_bytes() == saved
    state = json.loads(generated.path.read_text())
    assert state["flight"] is None and state["pending"] == []
