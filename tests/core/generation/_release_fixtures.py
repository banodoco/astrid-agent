"""Real CPU owner fixture; synthetic receipt/profile are explicitly labelled.

This is protocol/fencing coverage, not full installed readiness or GPU proof.
The issuer, engine, peer credentials, pidfds, process births and Unix socket
are real. No fake owner handle or boolean stop authority is used.
"""
import hashlib
import json
import os
import secrets
import time
from types import SimpleNamespace
import uuid

import pytest

from astrid.core.generation.engine_release_authority import PinnedOwner, ReleaseOffer, process_identity
from astrid.core.generation.engine_runner import _sha
from tests.core.generation.test_engine_custody import session  # noqa: F401


@pytest.fixture
def ready_owner(session):
    client, payload, recipe, root, mode, started = session
    client.call("start", payload, deadline=time.monotonic() + 10)
    registry = root / "runtime/out/sessions" / recipe.engine
    marker = json.loads((registry / "launch.json").read_bytes())
    receipt = root / ".astrid-engine/install.json"
    receipt.write_bytes(b'{"synthetic_protocol_fixture":true}')
    receipt.chmod(0o600)
    issuer = process_identity(marker["pid"])
    engine = {"installation": client.identity, "issuer": issuer,
        "engine": process_identity(marker["comfy_pid"]),
        "owner_instance": str(uuid.UUID(marker["launch_token"])),
        "launch_sha256": _sha(payload["launch"]), "endpoint": marker["url"],
        "source_revision": (registry / "source_revision").read_text(),
        "source_content_digest": (registry / "source_content_digest").read_text(),
        "config_digest": "sha256:" + hashlib.sha256((registry / "config.json").read_bytes()).hexdigest(),
        "receipt_digest": "sha256:" + hashlib.sha256(receipt.read_bytes()).hexdigest()}
    owner = PinnedOwner(client, issuer)
    yield SimpleNamespace(client=client, payload=payload, recipe=recipe, root=root, mode=mode,
        started=started, registry=registry, engine=engine, owner=owner)
    owner.close()


def issue(state):
    receipt = state.owner.request("issue", {"engine": state.engine,
        "profile_digest": "sha256:" + "a" * 64}, time.monotonic() + 5)
    return ReleaseOffer(state.owner, receipt)


def selected():
    return {"process": process_identity(os.getpid()), "nonce": secrets.token_hex(16),
            "executor_id": "selected-test-host", "source": "sha256:" + "b" * 64}


def runtime():
    return {"instance_id": str(uuid.uuid4()), "endpoint": "http://127.0.0.1:9000",
            "epoch": 7, "executor_id": "selected-test-host"}


def claim(state):
    from astrid.core.generation.engine_release_authority import ClaimedEngineRelease
    target, execution = selected(), runtime()
    offer = issue(state)
    ticket = offer.delegate(target=target, runtime=execution, deadline=time.monotonic() + 5)
    credential = {k: ticket[k] for k in ("issuance_id", "secret")}
    response = state.owner.request("claim", {**credential, "target": target, "runtime": execution}, time.monotonic() + 5)
    assert response == {"status": "claimed"}
    return ClaimedEngineRelease(state.owner, credential, target, execution)


def binding(state, *, execution="sha256:" + "c" * 64):
    from astrid.core.execution.managed_tool_session import SessionBinding
    engine = state.engine
    return SessionBinding(str(state.registry), engine["owner_instance"], engine["issuer"]["birth"],
        engine["endpoint"], engine["source_content_digest"], engine["config_digest"], execution,
        None, engine["engine"]["birth"], engine["engine"]["birth"])
