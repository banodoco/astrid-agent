"""PK-7: receipts from the installed wheel carry one start-time pack provenance snapshot.

In a clean interpreter running the release wheel, an installed (pinned) external pack
is admitted by the host. The provenance captured at action start records the PIN
association (origin, installed digest) and the observed source digest together; the
serialized receipt keeps that snapshot even after the collection changes, and a later
capture would differ (the receipt never re-reads the PIN at the end).
"""
from __future__ import annotations

import json

import pytest

from tests.integrations._installed_wheel import build_installed_wheel

PROBE = r"""
import json, sys
from pathlib import Path
import yaml
from astrid.core.execution import generic_host
from astrid.core.execution.generic_host import GenericPackHost
from astrid.core.execution.terminal_result import serialize_terminal_result
from astrid.core.pack.identity import pack_digest, write_pin
from astrid.sdk.results import InvocationResult

collection = Path(sys.argv[1])
pack = collection / "receipted"
pack.mkdir(parents=True)
(pack / "pack.yaml").write_text(yaml.safe_dump({
    "schema_version": 3, "id": "receipted", "name": "Receipted", "version": "1.0.0", "pack_api": "2.0",
    "documentation": {"kind": "none", "reason": "PK-7 fixture."},
    "actions": {"run": {"description": "Run", "short_description": "Run",
                         "invocation": {"kind": "command", "command": {"argv": ["echo", "ok"]}},
                         "inputs": [], "outputs": []}},
}), encoding="utf-8")
installed = pack_digest(pack)
write_pin(pack / "PIN", {"version": 1, "source": "https://example.invalid/packs.git#receipted",
                         "commit": "1" * 40, "digest_rule": "pack-tree-v1", "digest": installed, "origin": "user"})
host = GenericPackHost(pack_roots=[collection])
host.discover()
record = host.capabilities["receipted.run"]
observed = generic_host._run_source_digest(record)
start = generic_host._run_pack_provenance(record, observed)

def receipt(provenance):
    return serialize_terminal_result(
        InvocationResult(capability_id="receipted.run", capability_type="executor", native_kind="executor", ok=True),
        status="succeeded", task_id="task-1", attempt_id="attempt-1", parent_task_id=None, parent_attempt_id=None,
        request_digest="sha256:" + "a" * 64, resolved_options={}, cost=None, warnings=[], settled=False,
        settlement=None, effect=None, pack_provenance=provenance)

# The collection changes while the action runs: new bytes and a new PIN.
(pack / "later.txt").write_text("changed\n", encoding="utf-8")
write_pin(pack / "PIN", {"version": 1, "source": "https://example.invalid/packs.git#receipted",
                         "commit": "2" * 40, "digest_rule": "pack-tree-v1", "digest": pack_digest(pack),
                         "origin": "default"})
later = generic_host._run_pack_provenance(record, generic_host._run_source_digest(record))
print(json.dumps({"installed": installed, "start": start, "later": later, "receipt": receipt(start),
                  "module": generic_host.__file__}))
"""


@pytest.fixture(scope="module")
def installed(tmp_path_factory):
    return build_installed_wheel(tmp_path_factory.mktemp("installed-receipt-provenance"))


def test_installed_receipt_keeps_the_action_start_pack_snapshot(installed):
    collection = installed.root / "collection"
    result = installed.run("-c", PROBE, str(collection))
    assert result.returncode == 0, result.stdout[-2000:] + result.stderr[-3000:]
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["module"].startswith(str(installed.site))
    (start,) = payload["start"]
    assert start == {"pack_id": "receipted", "origin": "user", "installed_digest": payload["installed"],
                     "observed_digest": payload["installed"], "modified": False}
    assert payload["later"] != payload["start"]
    # The receipt carries exactly the start snapshot, not the later collection state.
    assert payload["receipt"]["pack_provenance"] == [start]
