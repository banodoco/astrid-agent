import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from astrid.core.execution import generic_host
from astrid.core.execution.generic_host import GenericPackHost, HostCancelled
from astrid.core.execution.terminal_result import serialize_terminal_result
from astrid.core.pack.identity import pack_digest, write_pin
from astrid.sdk.results import InvocationResult
from tests.test_generic_host import FakeRuntime, _write_manifest


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


@pytest.mark.parametrize("route", ["selected-action", "legacy-executor"])
@pytest.mark.parametrize("collection_change", ["updated", "removed"])
@pytest.mark.parametrize("terminal", ["success", "failure", "timeout"])
def test_host_receipt_reuses_start_snapshot_after_collection_change(
    tmp_path, monkeypatch, route, collection_change, terminal,
):
    """Exercise discovery, execution, and result.json through the real host root."""
    pack_root = tmp_path / "packs" / "hello_ext"
    pack_root.parent.mkdir(parents=True)
    if route == "selected-action":
        pack_root.mkdir()
        (pack_root / "actions").mkdir()
        (pack_root / "actions" / "run.py").write_text("def run(): return 'ok'\n", encoding="utf-8")
        pack_manifest = {
            "schema_version": 3, "id": "hello_ext", "name": "Hello", "version": "1.0.0",
            "pack_api": "1.0", "documentation": {"kind": "none", "reason": "Fixture pack."},
            "actions": {"run": {
                "description": "Run fixture", "short_description": "Run fixture",
                "invocation": {"kind": "command", "command": {"argv": ["echo", "fixture"]}},
                "inputs": [], "outputs": [],
            }},
            "documents": {"note": {"format_version": 1, "schema": {"type": "object"}}},
        }
        capability = "hello_ext.run"
    else:
        pack_manifest = {
            "schema_version": 3, "id": "hello_ext", "name": "Hello",
            "version": "1.0.0", "pack_api": "1.0",
            "documentation": {"kind": "none", "reason": "Fixture pack."},
            "documents": {"note": {"format_version": 1, "schema": {"type": "object"}}},
        }
        _write_manifest(pack_root / "executors" / "echo", capability_id="hello_ext.run")
        capability = "hello_ext.run"
    (pack_root / "pack.yaml").write_text(yaml.safe_dump(pack_manifest), encoding="utf-8")
    installed = pack_digest(pack_root)
    write_pin(pack_root / "PIN", {
        "version": 1, "source": "https://example.invalid/packs.git#hello_ext",
        "commit": "1" * 40, "digest_rule": "pack-tree-v1", "digest": installed,
        "origin": "user",
    })

    runtime = FakeRuntime()
    runtime.INLINE_SETTLEMENT_OUTPUTS = True
    run_root = tmp_path / "run"
    host = GenericPackHost(pack_roots=[tmp_path / "packs"], client=runtime, attempt_root=run_root)
    host.discover()
    assert capability in host.capabilities
    task = {"task": {
        "id": "task-snapshot", "attempt_id": "attempt-snapshot", "fence": 3,
        "capability": capability, "project_id": "demo", "run_id": "run-snapshot",
        "spec": {"spec": {"inputs": {}}},
    }}
    runtime.tasks["task-snapshot"] = task
    monkeypatch.setattr(generic_host, "process_birth_identity", lambda: "fixture-host-birth")
    captured = []
    capture = generic_host._run_pack_provenance

    def capture_at_start(record, observed):
        value = capture(record, observed)
        captured.append(value)
        return value

    monkeypatch.setattr(generic_host, "_run_pack_provenance", capture_at_start)

    expired = False
    monkeypatch.setattr(generic_host._AttemptLifetime, "expired", lambda _self: expired)

    def mutate_collection():
        if collection_change == "updated":
            (pack_root / "generation.txt").write_text("new generation\n", encoding="utf-8")
            write_pin(pack_root / "PIN", {
                "version": 1, "source": "https://example.invalid/packs.git#hello_ext",
                "commit": "2" * 40, "digest_rule": "pack-tree-v1",
                "digest": pack_digest(pack_root), "origin": "default",
            })
        else:
            shutil.rmtree(pack_root)

    def execute(_record, _inputs, output_root, _attempt, **_kwargs):
        nonlocal expired
        # The action-start snapshot must already exist before the child edge.
        assert captured == [[{
            "pack_id": "hello_ext", "origin": "user", "installed_digest": installed,
            "observed_digest": installed, "modified": False,
        }]]
        mutate_collection()
        if terminal == "failure":
            raise RuntimeError("fixture child failure")
        if terminal == "timeout":
            expired = True
            assert _kwargs["cancelled"]() is True
            raise HostCancelled("deadline interrupted child")
        output_root.mkdir(parents=True, exist_ok=True)
        (output_root / "answer.txt").write_text("done\n", encoding="utf-8")
        return SimpleNamespace(
            payload={"pack_provenance": [{"pack_id": "spoofed", "observed_digest": "0" * 64}]},
            process_id=12345, returncode=0,
        )

    monkeypatch.setattr(host, "_run_command_definition", execute)
    if terminal == "failure":
        with pytest.raises(RuntimeError, match="fixture child failure"):
            host.run_task(task, lease_token="lease-snapshot")
        expected_status = "failed"
    elif terminal == "timeout":
        host.run_task(task, lease_token="lease-snapshot")
        expected_status = "timeout"
    else:
        host.run_task(task, lease_token="lease-snapshot")
        expected_status = "succeeded"

    receipt = json.loads((run_root / "result.json").read_text(encoding="utf-8"))
    assert receipt["status"] == expected_status
    assert receipt["pack_provenance"] == [{
        "pack_id": "hello_ext", "origin": "user", "installed_digest": installed,
        "observed_digest": installed, "modified": False,
    }]
    if terminal == "success":
        assert receipt["raw_result"]["pack_provenance"][0]["pack_id"] == "spoofed"
    assert len(captured) == 1


def test_task_only_receipt_omits_unknown_pack_provenance():
    receipt = _receipt(None)
    assert "pack_provenance" not in receipt


def test_unpinned_selected_pack_is_explicitly_folder_origin_and_unmodified(tmp_path):
    root = tmp_path / "folder-pack"
    root.mkdir()
    (root / "action.py").write_text("VALUE = 1\n", encoding="utf-8")
    observed = pack_digest(root)
    record = SimpleNamespace(definition=SimpleNamespace(metadata={
        "action_invocation": {"kind": "python"}, "source_pack": "hello_ext", "pack_root": str(root),
    }))

    receipt = _receipt(generic_host._run_pack_provenance(record, observed))

    assert receipt["pack_provenance"] == [{
        "pack_id": "hello_ext", "origin": "folder", "installed_digest": None,
        "observed_digest": observed, "modified": None,
    }]
