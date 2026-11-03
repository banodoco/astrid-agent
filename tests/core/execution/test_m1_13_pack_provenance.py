from pathlib import Path
from types import SimpleNamespace

import pytest

from astrid.core.execution import generic_host
from astrid.core.execution.terminal_result import serialize_terminal_result
from astrid.core.pack.identity import pack_digest, write_pin
from astrid.sdk.results import InvocationResult


def _receipt(provenance):
    return serialize_terminal_result(
        InvocationResult(
            capability_id="hello_ext.run",
            capability_type="executor",
            native_kind="executor",
            ok=True,
        ),
        status="succeeded",
        task_id="task-1",
        attempt_id="attempt-1",
        parent_task_id=None,
        parent_attempt_id=None,
        request_digest="sha256:" + "a" * 64,
        resolved_options={},
        cost=None,
        warnings=[],
        settled=False,
        settlement=None,
        effect=None,
        pack_provenance=provenance,
    )


def _run_pack_provenance(record, observed):
    implementation = getattr(generic_host, "_run_pack_provenance", None)
    assert callable(implementation), "host does not record selected pack provenance"
    return implementation(record, observed)


def _selected_pack(root: Path):
    root.mkdir()
    payload = root / "action.py"
    payload.write_text("VALUE = 1\n", encoding="utf-8")
    installed = pack_digest(root)
    write_pin(root / "PIN", {
        "version": 1,
        "source": "https://example.invalid/packs.git#hello_ext",
        "commit": "1" * 40,
        "digest_rule": "pack-tree-v1",
        "digest": installed,
        "origin": "user",
    })
    record = SimpleNamespace(
        definition=SimpleNamespace(metadata={
            "action_invocation": {"kind": "python"},
            "source_pack": "hello_ext",
            "pack_root": str(root),
        }),
    )
    return payload, installed, record


def test_matching_run_receipt_has_equal_installed_and_observed_digests(tmp_path):
    _payload, installed, record = _selected_pack(tmp_path / "pack")

    provenance = _run_pack_provenance(record, installed)
    receipt = _receipt(provenance)

    row = receipt["pack_provenance"][0]
    assert row["installed_digest"] == installed
    assert row["observed_digest"] == installed
    assert row["modified"] is False


def test_changed_observed_file_is_visible_as_a_digest_mismatch(tmp_path):
    payload, installed, record = _selected_pack(tmp_path / "pack")
    payload.write_text("VALUE = 2\n", encoding="utf-8")
    observed = pack_digest(payload.parent)

    receipt = _receipt(_run_pack_provenance(record, observed))

    row = receipt["pack_provenance"][0]
    assert row["installed_digest"] == installed
    assert row["observed_digest"] == observed
    assert row["installed_digest"] != row["observed_digest"]
    assert row["modified"] is True


def test_receipt_rejects_missing_observed_digest():
    with pytest.raises(ValueError, match="valid installed and observed digests"):
        _receipt([{
            "pack_id": "hello_ext",
            "origin": "user",
            "installed_digest": "a" * 64,
            "observed_digest": None,
            "modified": None,
        }])
