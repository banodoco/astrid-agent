import errno
import json
import mmap
import os
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from astrid.core.execution import generic_host
from astrid.core.execution.generic_host import GenericPackHost, HostCancelled, HostError
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

    def capture_at_start(record, observed, generation=None):
        value = (
            capture(record, observed)
            if generation is None else capture(record, observed, generation)
        )
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


def _real_host_task(runtime, capability, task_id):
    task = {"task": {
        "id": task_id, "attempt_id": f"attempt-{task_id}", "fence": 3,
        "capability": capability, "project_id": "demo", "run_id": f"run-{task_id}",
        "spec": {"spec": {"inputs": {}}},
    }}
    runtime.tasks[task_id] = task
    return task


def _write_action_pack(pack_root):
    pack_root.mkdir(parents=True)
    (pack_root / "actions").mkdir()
    (pack_root / "actions" / "run.py").write_text("def run(): return 'ok'\n", encoding="utf-8")
    (pack_root / "generation.txt").write_text("generation one\n", encoding="utf-8")
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
    (pack_root / "pack.yaml").write_text(yaml.safe_dump(pack_manifest), encoding="utf-8")
    return pack_root


def _write_legacy_pack(pack_root):
    pack_root.mkdir(parents=True)
    pack_manifest = {
        "schema_version": 3, "id": "hello_ext", "name": "Hello", "version": "1.0.0",
        "pack_api": "1.0", "documentation": {"kind": "none", "reason": "Fixture pack."},
        "documents": {"note": {"format_version": 1, "schema": {"type": "object"}}},
    }
    (pack_root / "pack.yaml").write_text(yaml.safe_dump(pack_manifest), encoding="utf-8")
    _write_manifest(pack_root / "executors" / "echo", capability_id="hello_ext.run")
    return pack_root


def _replace_selected_pack(pack_root):
    replacement = pack_root.with_name("hello_ext.replacement")
    retired = pack_root.with_name("hello_ext.retired")
    shutil.copytree(pack_root, replacement)
    (replacement / "generation.txt").write_text("generation two\n", encoding="utf-8")
    write_pin(replacement / "PIN", {
        "version": 1, "source": "https://example.invalid/packs.git#hello_ext",
        "commit": "2" * 40, "digest_rule": "pack-tree-v1",
        "digest": pack_digest(replacement), "origin": "default",
    })
    pack_root.rename(retired)
    replacement.rename(pack_root)
    shutil.rmtree(retired)


@pytest.mark.parametrize("phase", ["between-digest-and-pin", "during-digest-traversal"])
@pytest.mark.parametrize("change", ["replaced", "removed"])
@pytest.mark.parametrize("route", ["selected-action", "legacy-executor"])
def test_real_host_refuses_collection_change_during_capture(tmp_path, monkeypatch, phase, change, route):
    pack_root = (
        _write_action_pack(tmp_path / "packs" / "hello_ext")
        if route == "selected-action"
        else _write_legacy_pack(tmp_path / "packs" / "hello_ext")
    )
    installed = pack_digest(pack_root)
    write_pin(pack_root / "PIN", {
        "version": 1, "source": "https://example.invalid/packs.git#hello_ext",
        "commit": "1" * 40, "digest_rule": "pack-tree-v1",
        "digest": installed, "origin": "user",
    })
    runtime = FakeRuntime()
    runtime.INLINE_SETTLEMENT_OUTPUTS = True
    run_root = tmp_path / "run"
    host = GenericPackHost(pack_roots=[tmp_path / "packs"], client=runtime, attempt_root=run_root)
    host.discover()
    assert "hello_ext.run" in host.capabilities
    task = _real_host_task(runtime, "hello_ext.run", "task-race")
    monkeypatch.setattr(generic_host, "process_birth_identity", lambda: "fixture-host-birth")
    command_calls = []

    def execute(_record, _inputs, output_root, _attempt, **_kwargs):
        command_calls.append(True)
        output_root.mkdir(parents=True, exist_ok=True)
        (output_root / "answer.txt").write_text("done\n", encoding="utf-8")
        return SimpleNamespace(payload={}, process_id=12345, returncode=0)

    monkeypatch.setattr(host, "_run_command_definition", execute)

    def mutate():
        if change == "removed":
            shutil.rmtree(pack_root)
        else:
            _replace_selected_pack(pack_root)

    if phase == "between-digest-and-pin":
        from astrid.core.pack import identity

        original_digest = identity.pack_digest
        mutated = False

        def digest_then_mutate(root):
            nonlocal mutated
            digest = original_digest(root)
            if not mutated and Path(root).resolve() == pack_root.resolve():
                # Keep the real digest result and change the selected tree
                # immediately after measurement, before the PIN read.
                mutated = True
                mutate()
            return digest

        monkeypatch.setattr(identity, "pack_digest", digest_then_mutate)
    else:
        original_read = Path.read_bytes
        mutated = False

        def read_and_mutate(path):
            nonlocal mutated
            content = original_read(path)
            if not mutated and path.is_relative_to(pack_root) and path.name != "PIN":
                mutated = True
                mutate()
            return content

        monkeypatch.setattr(Path, "read_bytes", read_and_mutate)

    refused = False
    try:
        host.run_task(task, lease_token="lease-race")
    except (HostError, ValueError):
        refused = True

    receipt_path = run_root / "result.json"
    if receipt_path.exists():
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        assert not receipt.get("pack_provenance")
    assert refused
    assert command_calls == []
    assert runtime.failures


def test_unknown_task_only_provenance_is_omitted_by_real_host(tmp_path, monkeypatch):
    executor_root = tmp_path / "packs" / "task_only" / "executors" / "echo"
    _write_manifest(executor_root, capability_id="task_only.echo")
    runtime = FakeRuntime()
    runtime.INLINE_SETTLEMENT_OUTPUTS = True
    run_root = tmp_path / "run"
    host = GenericPackHost(pack_roots=[tmp_path / "packs"], client=runtime, attempt_root=run_root)
    host.discover()
    assert "task_only.echo" in host.capabilities
    task = _real_host_task(runtime, "task_only.echo", "task-only")
    monkeypatch.setattr(generic_host, "process_birth_identity", lambda: "fixture-host-birth")

    def execute(_record, _inputs, output_root, _attempt, **_kwargs):
        output_root.mkdir(parents=True, exist_ok=True)
        (output_root / "answer.txt").write_text("done\n", encoding="utf-8")
        return SimpleNamespace(payload={}, process_id=12345, returncode=0)

    monkeypatch.setattr(host, "_run_command_definition", execute)
    host.run_task(task, lease_token="lease-task-only")

    receipt = json.loads((run_root / "result.json").read_text(encoding="utf-8"))
    assert receipt["status"] == "succeeded"
    assert "pack_provenance" not in receipt


def test_unpinned_selected_pack_origin_and_digest_are_written_by_real_host(tmp_path, monkeypatch):
    pack_root = _write_action_pack(tmp_path / "packs" / "hello_ext")
    observed = pack_digest(pack_root)
    runtime = FakeRuntime()
    runtime.INLINE_SETTLEMENT_OUTPUTS = True
    run_root = tmp_path / "run"
    host = GenericPackHost(pack_roots=[tmp_path / "packs"], client=runtime, attempt_root=run_root)
    host.discover()
    assert "hello_ext.run" in host.capabilities
    task = _real_host_task(runtime, "hello_ext.run", "task-unpinned")
    monkeypatch.setattr(generic_host, "process_birth_identity", lambda: "fixture-host-birth")

    def execute(_record, _inputs, output_root, _attempt, **_kwargs):
        output_root.mkdir(parents=True, exist_ok=True)
        (output_root / "answer.txt").write_text("done\n", encoding="utf-8")
        return SimpleNamespace(payload={}, process_id=12345, returncode=0)

    monkeypatch.setattr(host, "_run_command_definition", execute)
    host.run_task(task, lease_token="lease-unpinned")

    receipt = json.loads((run_root / "result.json").read_text(encoding="utf-8"))
    assert receipt["pack_provenance"] == [{
        "pack_id": "hello_ext", "origin": "folder", "installed_digest": None,
        "observed_digest": observed, "modified": None,
    }]


@pytest.mark.parametrize("route", ["selected-action", "legacy-executor"])
@pytest.mark.parametrize("change", ["ordinary-edit", "replacement", "removal"])
def test_real_host_refuses_change_after_last_capture_read(tmp_path, monkeypatch, route, change):
    """Mutate immediately after pack_digest reads its last source byte."""
    pack_root = (
        _write_action_pack(tmp_path / "packs" / "hello_ext")
        if route == "selected-action"
        else _write_legacy_pack(tmp_path / "packs" / "hello_ext")
    )
    payload = pack_root / "generation.txt"
    payload.write_text("generation one\n", encoding="utf-8")
    installed = pack_digest(pack_root)
    write_pin(pack_root / "PIN", {
        "version": 1, "source": "https://example.invalid/packs.git#hello_ext",
        "commit": "1" * 40, "digest_rule": "pack-tree-v1",
        "digest": installed, "origin": "user",
    })
    runtime = FakeRuntime()
    runtime.INLINE_SETTLEMENT_OUTPUTS = True
    run_root = tmp_path / "run"
    host = GenericPackHost(pack_roots=[tmp_path / "packs"], client=runtime, attempt_root=run_root)
    host.discover()
    task = _real_host_task(runtime, "hello_ext.run", "task-last-read")
    monkeypatch.setattr(generic_host, "process_birth_identity", lambda: "fixture-host-birth")
    command_calls = []

    def execute(_record, _inputs, output_root, _attempt, **_kwargs):
        command_calls.append(True)
        output_root.mkdir(parents=True, exist_ok=True)
        (output_root / "answer.txt").write_text("done\n", encoding="utf-8")
        return SimpleNamespace(payload={}, process_id=12345, returncode=0)

    monkeypatch.setattr(host, "_run_command_definition", execute)
    original_read = Path.read_bytes
    digest_active = False
    selected_digest_calls = 0
    changed = False

    def last_read_mutation(path):
        nonlocal changed
        content = original_read(path)
        # pack.yaml sorts after the other regular fixture files in both routes.
        # Mutate only after its bytes have actually been read by a real digest.
        if digest_active and path == pack_root / "pack.yaml" and not changed:
            changed = True
            if change == "ordinary-edit":
                try:
                    fd = os.open(payload, os.O_WRONLY | os.O_TRUNC | os.O_NONBLOCK)
                except BlockingIOError:
                    pass  # A file lease refused the competing write.
                else:
                    with os.fdopen(fd, "wb") as stream:
                        stream.write(b"generation two\n")
            elif change == "replacement":
                _replace_selected_pack(pack_root)
            else:
                shutil.rmtree(pack_root)
        return content

    monkeypatch.setattr(Path, "read_bytes", last_read_mutation)
    from astrid.core.pack import identity

    original_digest = identity.pack_digest

    def digest_with_last_read(root):
        nonlocal digest_active, selected_digest_calls
        is_selected = Path(root).resolve() == pack_root.resolve()
        previous = digest_active
        if is_selected:
            selected_digest_calls += 1
            # The first selected traversal produces the observed digest. The
            # second is the final verification traversal whose last-read race
            # previously escaped the digest comparison.
            digest_active = selected_digest_calls == 2
        try:
            return original_digest(root)
        finally:
            digest_active = previous

    monkeypatch.setattr(identity, "pack_digest", digest_with_last_read)
    refused = False
    try:
        host.run_task(task, lease_token="lease-last-read")
    except (HostError, ValueError):
        refused = True

    assert changed, "mutation hook did not run after the selected pack's last read"
    receipt_path = run_root / "result.json"
    if receipt_path.exists():
        assert not json.loads(receipt_path.read_text(encoding="utf-8")).get("pack_provenance")
    assert refused
    assert command_calls == []
    assert runtime.failures


@pytest.mark.parametrize("route", ["selected-action", "legacy-executor"])
def test_real_host_refuses_capture_while_writable_mmap_is_open(tmp_path, monkeypatch, route):
    """An existing writable mapping prevents acquisition of a complete read fence."""
    pack_root = (
        _write_action_pack(tmp_path / "packs" / "hello_ext")
        if route == "selected-action"
        else _write_legacy_pack(tmp_path / "packs" / "hello_ext")
    )
    payload = pack_root / "generation.txt"
    payload.write_text("generation one\n", encoding="utf-8")
    installed = pack_digest(pack_root)
    write_pin(pack_root / "PIN", {
        "version": 1, "source": "https://example.invalid/packs.git#hello_ext",
        "commit": "1" * 40, "digest_rule": "pack-tree-v1",
        "digest": installed, "origin": "user",
    })
    runtime = FakeRuntime()
    runtime.INLINE_SETTLEMENT_OUTPUTS = True
    run_root = tmp_path / "run"
    host = GenericPackHost(pack_roots=[tmp_path / "packs"], client=runtime, attempt_root=run_root)
    host.discover()
    task = _real_host_task(runtime, "hello_ext.run", "task-writable-mmap")
    monkeypatch.setattr(generic_host, "process_birth_identity", lambda: "fixture-host-birth")
    command_calls = []

    def execute(_record, _inputs, output_root, _attempt, **_kwargs):
        command_calls.append(True)
        output_root.mkdir(parents=True, exist_ok=True)
        return SimpleNamespace(payload={}, process_id=12345, returncode=0)

    monkeypatch.setattr(host, "_run_command_definition", execute)
    refused = False
    stream = payload.open("r+b")
    mapped = mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_WRITE)
    stream.close()
    try:
        mapped[:] = b"generation two\n"
        mapped.flush()
        try:
            host.run_task(task, lease_token="lease-writable-mmap")
        except (HostError, ValueError):
            refused = True
    finally:
        mapped.close()

    receipt_path = run_root / "result.json"
    if receipt_path.exists():
        assert not json.loads(receipt_path.read_text(encoding="utf-8")).get("pack_provenance")
    assert refused
    assert command_calls == []
    assert runtime.failures


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux file leases are required")
@pytest.mark.parametrize("route", ["selected-action", "legacy-executor"])
def test_real_host_refuses_when_selected_pack_lease_is_unavailable(
    tmp_path, monkeypatch, route,
):
    import fcntl

    pack_root = (
        _write_action_pack(tmp_path / "packs" / "hello_ext")
        if route == "selected-action"
        else _write_legacy_pack(tmp_path / "packs" / "hello_ext")
    )
    installed = pack_digest(pack_root)
    write_pin(pack_root / "PIN", {
        "version": 1, "source": "https://example.invalid/packs.git#hello_ext",
        "commit": "1" * 40, "digest_rule": "pack-tree-v1",
        "digest": installed, "origin": "user",
    })
    runtime = FakeRuntime()
    runtime.INLINE_SETTLEMENT_OUTPUTS = True
    run_root = tmp_path / "run"
    host = GenericPackHost(pack_roots=[tmp_path / "packs"], client=runtime, attempt_root=run_root)
    host.discover()
    task = _real_host_task(runtime, "hello_ext.run", "task-no-lease")
    monkeypatch.setattr(generic_host, "process_birth_identity", lambda: "fixture-host-birth")
    command_calls = []

    def execute(_record, _inputs, output_root, _attempt, **_kwargs):
        command_calls.append(True)
        output_root.mkdir(parents=True, exist_ok=True)
        return SimpleNamespace(payload={}, process_id=12345, returncode=0)

    monkeypatch.setattr(host, "_run_command_definition", execute)
    original_fcntl = fcntl.fcntl
    lease_calls = []

    def no_lease(fd, operation, argument=0):
        if operation == fcntl.F_SETLEASE:
            lease_calls.append(fd)
            raise OSError(errno.EOPNOTSUPP, "fixture lease unsupported")
        return original_fcntl(fd, operation, argument)

    monkeypatch.setattr(fcntl, "fcntl", no_lease)
    refused = False
    try:
        host.run_task(task, lease_token="lease-no-lease")
    except HostError:
        refused = True

    receipt_path = run_root / "result.json"
    if receipt_path.exists():
        assert not json.loads(receipt_path.read_text(encoding="utf-8")).get("pack_provenance")
    assert lease_calls
    assert refused
    assert command_calls == []
    assert runtime.failures


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux file leases are required")
@pytest.mark.parametrize("route", ["selected-action", "legacy-executor"])
def test_real_host_releases_partial_selected_pack_leases_on_failure(
    tmp_path, monkeypatch, route,
):
    import fcntl

    pack_root = (
        _write_action_pack(tmp_path / "packs" / "hello_ext")
        if route == "selected-action"
        else _write_legacy_pack(tmp_path / "packs" / "hello_ext")
    )
    payload = pack_root / "generation.txt"
    payload.write_text("generation one\n", encoding="utf-8")
    installed = pack_digest(pack_root)
    write_pin(pack_root / "PIN", {
        "version": 1, "source": "https://example.invalid/packs.git#hello_ext",
        "commit": "1" * 40, "digest_rule": "pack-tree-v1",
        "digest": installed, "origin": "user",
    })
    runtime = FakeRuntime()
    runtime.INLINE_SETTLEMENT_OUTPUTS = True
    run_root = tmp_path / "run"
    host = GenericPackHost(pack_roots=[tmp_path / "packs"], client=runtime, attempt_root=run_root)
    host.discover()
    task = _real_host_task(runtime, "hello_ext.run", "task-partial-lease")
    monkeypatch.setattr(generic_host, "process_birth_identity", lambda: "fixture-host-birth")
    command_calls = []

    def execute(_record, _inputs, output_root, _attempt, **_kwargs):
        command_calls.append(True)
        output_root.mkdir(parents=True, exist_ok=True)
        return SimpleNamespace(payload={}, process_id=12345, returncode=0)

    monkeypatch.setattr(host, "_run_command_definition", execute)
    original_fcntl = fcntl.fcntl
    original_open = os.open
    lease_calls = 0
    leased_fds = []
    fd_paths = {}

    def track_open(path, flags, *args, **kwargs):
        fd = original_open(path, flags, *args, **kwargs)
        fd_paths[fd] = Path(path)
        return fd

    def fail_later_lease(fd, operation, argument=0):
        nonlocal lease_calls
        if operation == fcntl.F_SETLEASE:
            lease_calls += 1
            if lease_calls == 4:
                raise OSError(errno.EOPNOTSUPP, "fixture later lease unsupported")
            result = original_fcntl(fd, operation, argument)
            leased_fds.append((fd, fd_paths[fd]))
            return result
        return original_fcntl(fd, operation, argument)

    monkeypatch.setattr(os, "open", track_open)
    monkeypatch.setattr(fcntl, "fcntl", fail_later_lease)
    refused = False
    try:
        host.run_task(task, lease_token="lease-partial-lease")
    except HostError:
        refused = True

    assert lease_calls == 4
    assert len(leased_fds) == 3
    leased_sources = [path for _fd, path in leased_fds if path == payload]
    assert leased_sources == [payload]
    for fd, _path in leased_fds:
        with pytest.raises(OSError) as closed:
            os.fstat(fd)
        assert closed.value.errno == errno.EBADF
    writer_fd = original_open(payload, os.O_WRONLY | os.O_NONBLOCK)
    os.close(writer_fd)

    receipt_path = run_root / "result.json"
    if receipt_path.exists():
        assert not json.loads(receipt_path.read_text(encoding="utf-8")).get("pack_provenance")
    assert refused
    assert command_calls == []
    assert runtime.failures


@pytest.mark.parametrize("route", ["selected-action", "legacy-executor"])
def test_real_host_terminal_serialization_does_not_reread_source_or_pin(
    tmp_path, monkeypatch, route,
):
    pack_root = (
        _write_action_pack(tmp_path / "packs" / "hello_ext")
        if route == "selected-action"
        else _write_legacy_pack(tmp_path / "packs" / "hello_ext")
    )
    installed = pack_digest(pack_root)
    write_pin(pack_root / "PIN", {
        "version": 1, "source": "https://example.invalid/packs.git#hello_ext",
        "commit": "1" * 40, "digest_rule": "pack-tree-v1",
        "digest": installed, "origin": "user",
    })
    runtime = FakeRuntime()
    runtime.INLINE_SETTLEMENT_OUTPUTS = True
    run_root = tmp_path / "run"
    host = GenericPackHost(pack_roots=[tmp_path / "packs"], client=runtime, attempt_root=run_root)
    host.discover()
    task = _real_host_task(runtime, "hello_ext.run", "task-terminal-read")
    monkeypatch.setattr(generic_host, "process_birth_identity", lambda: "fixture-host-birth")
    from astrid.core.pack import identity

    digest_calls = []
    pin_calls = []
    original_digest = identity.pack_digest
    original_pin = identity.read_pin

    def count_digest(root):
        if Path(root).resolve() == pack_root.resolve():
            digest_calls.append(True)
        return original_digest(root)

    def count_pin(path):
        if Path(path) == pack_root / "PIN":
            pin_calls.append(True)
        return original_pin(path)

    monkeypatch.setattr(identity, "pack_digest", count_digest)
    monkeypatch.setattr(identity, "read_pin", count_pin)
    terminal_phase = False
    original_read = Path.read_bytes

    def guard_source_reads(path):
        if terminal_phase and path.is_relative_to(pack_root):
            raise AssertionError(f"terminal serialization reread selected source: {path}")
        return original_read(path)

    monkeypatch.setattr(Path, "read_bytes", guard_source_reads)
    counts_at_execution = None

    def execute(_record, _inputs, output_root, _attempt, **_kwargs):
        nonlocal terminal_phase, counts_at_execution
        counts_at_execution = (len(digest_calls), len(pin_calls))
        output_root.mkdir(parents=True, exist_ok=True)
        (output_root / "answer.txt").write_text("done\n", encoding="utf-8")
        terminal_phase = True
        return SimpleNamespace(payload={}, process_id=12345, returncode=0)

    monkeypatch.setattr(host, "_run_command_definition", execute)
    host.run_task(task, lease_token="lease-terminal-read")

    assert counts_at_execution is not None
    assert digest_calls and pin_calls
    assert (len(digest_calls), len(pin_calls)) == counts_at_execution
    receipt = json.loads((run_root / "result.json").read_text(encoding="utf-8"))
    assert receipt["status"] == "succeeded"
    assert receipt["pack_provenance"][0]["installed_digest"] == installed


@pytest.mark.parametrize("route", ["selected-action", "legacy-executor"])
def test_real_host_reports_installed_and_observed_digest_mismatch(
    tmp_path, monkeypatch, route,
):
    pack_root = (
        _write_action_pack(tmp_path / "packs" / "hello_ext")
        if route == "selected-action"
        else _write_legacy_pack(tmp_path / "packs" / "hello_ext")
    )
    installed = pack_digest(pack_root)
    write_pin(pack_root / "PIN", {
        "version": 1, "source": "https://example.invalid/packs.git#hello_ext",
        "commit": "1" * 40, "digest_rule": "pack-tree-v1",
        "digest": installed, "origin": "user",
    })
    payload = pack_root / "generation.txt"
    payload.write_text("generation two\n", encoding="utf-8")
    observed = pack_digest(pack_root)
    runtime = FakeRuntime()
    runtime.INLINE_SETTLEMENT_OUTPUTS = True
    run_root = tmp_path / "run"
    host = GenericPackHost(pack_roots=[tmp_path / "packs"], client=runtime, attempt_root=run_root)
    host.discover()
    task = _real_host_task(runtime, "hello_ext.run", "task-digest-mismatch")
    monkeypatch.setattr(generic_host, "process_birth_identity", lambda: "fixture-host-birth")

    def execute(_record, _inputs, output_root, _attempt, **_kwargs):
        output_root.mkdir(parents=True, exist_ok=True)
        (output_root / "answer.txt").write_text("done\n", encoding="utf-8")
        return SimpleNamespace(payload={}, process_id=12345, returncode=0)

    monkeypatch.setattr(host, "_run_command_definition", execute)
    host.run_task(task, lease_token="lease-digest-mismatch")

    receipt = json.loads((run_root / "result.json").read_text(encoding="utf-8"))
    assert receipt["pack_provenance"] == [{
        "pack_id": "hello_ext", "origin": "user", "installed_digest": installed,
        "observed_digest": observed, "modified": True,
    }]
