import errno
import json
import mmap
import os
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from astrid.core.execution import generic_host
from astrid.pack_api import VERSION
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
            "pack_api": ".".join(map(str, VERSION)), "documentation": {"kind": "none", "reason": "Fixture pack."},
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
            "version": "1.0.0", "pack_api": ".".join(map(str, VERSION)),
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
        "pack_api": ".".join(map(str, VERSION)), "documentation": {"kind": "none", "reason": "Fixture pack."},
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
        "pack_api": ".".join(map(str, VERSION)), "documentation": {"kind": "none", "reason": "Fixture pack."},
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


@pytest.mark.parametrize("construction_platform", ["linux", "darwin", "win32"])
def test_host_retains_capture_mechanism_selected_at_construction(
    tmp_path, monkeypatch, construction_platform,
):
    root = _write_action_pack(tmp_path / "packs" / "hello_ext")
    runtime = FakeRuntime()
    runtime.INLINE_SETTLEMENT_OUTPUTS = True
    with monkeypatch.context() as selection:
        selection.setattr(generic_host.sys, "platform", construction_platform)
        host = GenericPackHost(pack_roots=[root], client=runtime, attempt_root=tmp_path / "attempt")
    capture_policy = host._pack_capture
    host.discover()
    task = _real_host_task(runtime, "hello_ext.run", "construction-policy")
    monkeypatch.setattr(generic_host, "process_birth_identity", lambda: "fixture-host-birth")
    dispatched = []

    def execute(_record, _inputs, output_root, _attempt, **_kwargs):
        assert host._pack_capture is capture_policy
        selected_root = Path(_record.definition.metadata["pack_root"])
        assert (selected_root == root) == (construction_platform == "linux")
        assert pack_digest(selected_root) == pack_digest(root)
        dispatched.append(True)
        output_root.mkdir(parents=True, exist_ok=True)
        return SimpleNamespace(payload={}, process_id=12345, returncode=0)

    monkeypatch.setattr(host, "_run_command_definition", execute)
    # Exercise host admission after changing the construction-time inputs.
    with monkeypatch.context() as changed:
        changed.setattr(generic_host.sys, "platform",
                        "unsupported" if construction_platform == "linux" else "linux")
        host.run_task(task, lease_token="construction-lease")
    receipt = json.loads((tmp_path / "attempt" / "result.json").read_text())
    assert dispatched == [True]
    assert receipt["status"] == "succeeded"
    assert receipt["pack_provenance"][0]["observed_digest"] == pack_digest(root)


def _composition_pack(tmp_path, route):
    """Use the same test-owned packs as focused coverage, with a real child."""
    root = (
        _write_action_pack(tmp_path / "packs" / "hello_ext")
        if route == "selected-action"
        else _write_legacy_pack(tmp_path / "packs" / "hello_ext")
    )
    sentinel = tmp_path / "child-side-effect"
    script = (
        "import json,sys; from pathlib import Path; "
        "Path(sys.argv[1]).write_text('child ran'); "
        "Path(sys.argv[2]).mkdir(parents=True,exist_ok=True); "
        "Path(sys.argv[2], 'answer.txt').write_text('composition answer'); "
        "print(json.dumps(dict(pack_provenance=[dict(pack_id='forged')]))); "
    )
    outputs = [{"name": "answer", "type": "file", "path_template": "{out}/answer.txt",
                "artifact_type": "text/plain"}]
    if route == "selected-action":
        # Exercise the real selected-pack command loader in the worker process.
        (root / "emit.py").write_text(script, encoding="utf-8")
        manifest = yaml.safe_load((root / "pack.yaml").read_text(encoding="utf-8"))
        manifest["actions"]["run"]["invocation"]["command"]["argv"] = [
            "{python_exec}", "-m", "astrid.packs.hello_ext.emit", str(sentinel), "{out}",
        ]
        manifest["actions"]["run"]["outputs"] = outputs
        (root / "pack.yaml").write_text(yaml.safe_dump(manifest), encoding="utf-8")
    else:
        path = root / "executors" / "echo" / "executor.yaml"
        manifest = json.loads(path.read_text(encoding="utf-8"))
        manifest["command"]["argv"] = ["{python_exec}", "-c", script, str(sentinel), "{out}"]
        path.write_text(json.dumps(manifest), encoding="utf-8")
    (root / "generation.txt").write_text("generation one\n", encoding="utf-8")
    installed = pack_digest(root)
    write_pin(root / "PIN", {
        "version": 1, "source": "https://example.invalid/packs.git#hello_ext",
        "commit": "1" * 40, "digest_rule": "pack-tree-v1", "digest": installed,
        "origin": "user",
    })
    return root, sentinel, installed


def _cli_composition(tmp_path, monkeypatch, real_runtime, route, scenario):
    """Real CLI, Runtime realm/discovery/catalog, Agent loader, host and child.

    No component under test is substituted. The only race injection delegates
    every filesystem read, then attempts a competing nonblocking write after
    the final verification traversal has read its last fixture source bytes.
    """
    import signal
    import sys
    from astrid.core.gateway.dispatch import compose_profile_handoff
    from astrid_runtime_client import WorkspaceClient

    monkeypatch.delenv("ASTRID_PACKS_PATH", raising=False)
    monkeypatch.delenv("ASTRID_PACK_HOST_SERVICE_MODE", raising=False)
    monkeypatch.setenv("ASTRID_HOST_LOG_MB", "0")
    root, sentinel, installed = _composition_pack(tmp_path, route)
    if scenario == "modified":
        (root / "generation.txt").write_text("generation two\n", encoding="utf-8")
    observed = pack_digest(root)
    support = tmp_path / "support"
    daemon = real_runtime(tmp_path / "realm", support_root=support,
                          log_directory=tmp_path / "runtime-logs")
    # These publications are made by the real Runtime, not authored by the test.
    discovery_path = support / "discovery.json"
    discovery = daemon.discovery.read()
    catalog = daemon.catalog.read()
    assert discovery["endpoint"] == daemon.endpoint
    assert catalog["selected_realm_id"] == discovery["active_realm"]
    boot = support / "astrid-host" / "boot-manifest.json"
    compose_profile_handoff(boot, support_root=support)
    attempt = tmp_path / "attempt"
    argv = ["astrid-generic-host", "--pack-root", str(root),
            "--runtime-endpoint", daemon.endpoint,
            "--runtime-discovery-file", str(discovery_path),
            "--credential-file", str(daemon.worker_credential_path),
            "--support-root", str(support), "--boot-manifest-path", str(boot),
            "--attempt-root", str(attempt), "--register"]
    # CLI installs process handlers; restore the caller's exact dispositions.
    handlers = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
    previous_journal = generic_host._task_scope._journal
    try:
        monkeypatch.setattr(sys, "argv", argv)
        assert generic_host._cli() == 0
        generated = WorkspaceClient(daemon.endpoint, daemon.token)
        capabilities, cursor = generated.list_capabilities()
        selected = next(cap for cap in capabilities if cap.capability_id == "hello_ext.run")
        assert selected.status == "ready" and cursor is None
        registered = generated.get_executor("astrid-pack-host")
        assert any(cap.capability_id == selected.capability_id and
                   cap.definition_digest == selected.definition_digest
                   for cap in registered.capabilities)
        project = generated.create_project("Composition", slug="composition", idempotency_key="project")
        task = generated.admit_task(
            capability_id=selected.capability_id, capability_digest=selected.definition_digest,
            input_object_ids=[], project_id=project.project_id, idempotency_key="task",
        )
        changed = []
        original_read = Path.read_bytes

        def final_read_mutation(path):
            content = original_read(path)
            if scenario == "final-read-write" and path == root / "pack.yaml" and not changed:
                frame = sys._getframe(1)
                while frame is not None:
                    if (frame.f_code is generic_host._run_pack_provenance.__code__
                            and "pin_path" in frame.f_locals):
                        # pack.yaml is last in pack_digest's sorted source list;
                        # generation.txt has already been read on this traversal.
                        changed.append("attempted")
                        try:
                            fd = os.open(root / "generation.txt", os.O_WRONLY | os.O_TRUNC | os.O_NONBLOCK)
                        except BlockingIOError:
                            changed.append("blocked")
                        else:
                            with os.fdopen(fd, "wb") as stream:
                                stream.write(b"generation two\n")
                            changed.append("written")
                        break
                    frame = frame.f_back
            return content

        monkeypatch.setattr(Path, "read_bytes", final_read_mutation)
        monkeypatch.setattr(sys, "argv", [*argv, "run", "--once"])
        if scenario == "final-read-write":
            with pytest.raises(HostError, match="write attempted during capture"):
                generic_host._cli()
        else:
            assert generic_host._cli() == 0
        terminal = generated.get_task(task.task_id)
        receipt = json.loads((attempt / "result.json").read_text(encoding="utf-8"))
        assert receipt["task_id"] == task.task_id
        assert receipt["attempt_id"] == terminal.attempt_id
        assert receipt["capability_id"] == "hello_ext.run"
        if scenario == "final-read-write":
            assert changed == ["attempted", "blocked"]
            assert terminal.state == receipt["status"] == "failed"
            assert not sentinel.exists()
            assert not receipt.get("pack_provenance")
            assert not receipt.get("outputs", {}).get("artifacts")
        else:
            assert sentinel.read_text() == "child ran"
            assert terminal.state == receipt["status"] == "succeeded"
            assert receipt["pack_provenance"] == [{
                "pack_id": "hello_ext", "origin": "user", "installed_digest": installed,
                "observed_digest": observed, "modified": scenario == "modified",
            }]
            stdout = (attempt / "task-stdout-tail.log").read_text(encoding="utf-8")
            assert json.loads(stdout)["pack_provenance"] == [{"pack_id": "forged"}]
            process = receipt["raw_result"]["process_evidence"]
            assert process["child_boundary"] == "subprocess" and process["returncode"] == 0
            assert process["process_id"] > 0
            import hashlib
            digest = "sha256:" + hashlib.sha256(b"composition answer").hexdigest()
            assert generated.get_object(digest).data == b"composition answer"
    finally:
        generic_host._task_scope.set_orphan_journal(previous_journal)
        for sig, handler in handlers.items():
            signal.signal(sig, handler)


@pytest.mark.integration
@pytest.mark.parametrize("route", ["selected-action", "legacy-executor"])
@pytest.mark.parametrize("scenario", ["matching", "modified", "final-read-write"])
def test_production_cli_runtime_capture_composition(tmp_path, monkeypatch, real_runtime, route, scenario):
    _cli_composition(tmp_path, monkeypatch, real_runtime, route, scenario)


@pytest.mark.parametrize("route", ["selected-action", "legacy-executor"])
@pytest.mark.parametrize("failure", [False, True])
def test_portable_host_executes_verified_copy_and_cleans_it(tmp_path, monkeypatch, route, failure):
    root, sentinel, _installed = _composition_pack(tmp_path, route)
    # A real child records its source location outside the disposable copy.
    script = ("from pathlib import Path; import sys; "
              "Path(sys.argv[1]).write_text(str(Path(__file__).resolve())); "
              "Path(sys.argv[2]).mkdir(parents=True,exist_ok=True); "
              "Path(sys.argv[2], 'answer.txt').write_text('snapshot child ran')")
    (root / "emit.py").write_text(script)
    if route == "legacy-executor":
        manifest_path = root / "executors" / "echo" / "executor.yaml"
        manifest = json.loads(manifest_path.read_text())
        manifest["command"]["argv"] = ["{python_exec}", str(root / "emit.py"), str(sentinel), "{out}"]
        manifest_path.write_text(json.dumps(manifest))
    expected_pack_digest = pack_digest(root)
    runtime = FakeRuntime()
    runtime.INLINE_SETTLEMENT_OUTPUTS = True
    with monkeypatch.context() as selection:
        selection.setattr(generic_host.sys, "platform", "darwin")
        host = GenericPackHost(pack_roots=[root], client=runtime, attempt_root=tmp_path / "attempt")
    host.discover()
    _definition, admitted = host.admit("executor", "hello_ext.run")
    task = _real_host_task(runtime, "hello_ext.run", "portable-copy")
    task["task"]["spec"]["spec"]["admitted_source_digest"] = admitted["source_digest"]
    monkeypatch.setattr(generic_host, "process_birth_identity", lambda: "fixture-host-birth")
    execute = host._run_command_definition
    snapshots = []

    def after_capture(record, *args, **kwargs):
        snapshot = Path(record.definition.metadata["pack_root"])
        snapshots.append(snapshot)
        assert snapshot != root and snapshot.is_dir()
        assert generic_host.platform.is_owner_only(snapshot.parent)
        assert snapshot.parent.parent.name == "pack-captures"
        assert pack_digest(snapshot) == expected_pack_digest
        assert not os.path.samefile(snapshot / "emit.py", root / "emit.py")
        # A write after admission cannot affect the independent executable bytes.
        (root / "emit.py").write_text("raise RuntimeError('live source executed')")
        if failure:
            raise RuntimeError("fixture child failure")
        return execute(record, *args, **kwargs)

    monkeypatch.setattr(host, "_run_command_definition", after_capture)
    if failure:
        with pytest.raises(RuntimeError, match="fixture child failure"):
            host.run_task(task, lease_token="portable-lease")
        assert not sentinel.exists()
    else:
        host.run_task(task, lease_token="portable-lease")
        assert sentinel.read_text() == str(snapshots[0] / "emit.py")
    assert len(snapshots) == 1
    assert not snapshots[0].parent.exists()
    # The reusable host keeps its live selection for the next task.
    assert host.capabilities["hello_ext.run"].source_root.is_relative_to(root)


@pytest.mark.parametrize("route", ["selected-action", "legacy-executor"])
def test_portable_host_refuses_change_after_admission(tmp_path, monkeypatch, route):
    root = (_write_action_pack(tmp_path / "packs" / "hello_ext") if route == "selected-action"
            else _write_legacy_pack(tmp_path / "packs" / "hello_ext"))
    runtime = FakeRuntime()
    runtime.INLINE_SETTLEMENT_OUTPUTS = True
    host = GenericPackHost(pack_roots=[root], client=runtime, attempt_root=tmp_path / "attempt",
                           pack_capture=generic_host._portable_pack_capture)
    host.discover()
    _definition, admitted = host.admit("executor", "hello_ext.run")
    task = _real_host_task(runtime, "hello_ext.run", "portable-mismatch")
    task["task"]["spec"]["spec"]["admitted_source_digest"] = admitted["source_digest"]
    (root / "generation.txt").write_text("changed after admission")
    monkeypatch.setattr(host, "_run_command_definition", lambda *a, **k: pytest.fail("launched changed source"))
    with pytest.raises(HostError, match="snapshot digest mismatch"):
        host.run_task(task, lease_token="portable-lease")
    assert runtime.failures[0][3]["structured_error"]["code"] == "selected_pack_capture_failed"
    from astrid.sdk.storage_root import resolve_runtime_data_root
    parent = resolve_runtime_data_root() / "runtime" / "pack-captures"
    assert not parent.exists() or list(parent.iterdir()) == []


@pytest.mark.parametrize("mutation", ["copy-write", "copy-error", "interrupt", "symlink", "writable-mmap"])
def test_portable_capture_fails_closed_and_cleans_partial_copy(tmp_path, monkeypatch, mutation):
    payload, expected, record = _selected_pack(tmp_path / "pack")
    generation = generic_host._selected_pack_generation(record)
    copytree = shutil.copytree
    mapping = None

    def contested_copy(source, destination, **kwargs):
        if mutation in {"copy-error", "interrupt"}:
            Path(destination).mkdir()
            if mutation == "interrupt":
                raise KeyboardInterrupt()
            raise OSError("fixture disk full")
        if mutation == "copy-write":
            payload.write_text("VALUE = 2\n")
        elif mutation == "symlink":
            payload.unlink()
            payload.symlink_to(tmp_path / "outside.py")
            (tmp_path / "outside.py").write_text("VALUE = 1\n")
        elif mutation == "writable-mmap":
            mapping[:] = b"VALUE = 2\n"
            mapping.flush()
        return copytree(source, destination, **kwargs)

    if mutation == "writable-mmap":
        with payload.open("r+b") as stream:
            mapping = mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_WRITE)
    monkeypatch.setattr(shutil, "copytree", contested_copy)
    try:
        with pytest.raises(KeyboardInterrupt if mutation == "interrupt" else generic_host.PackCaptureError) as error:
            with generic_host._portable_pack_capture(generation, expected_digest=expected):
                pytest.fail("captured contested source")
        if mutation != "interrupt":
            assert error.value.code == "selected_pack_capture_failed"
    finally:
        if mapping is not None:
            mapping.close()
    from astrid.sdk.storage_root import resolve_runtime_data_root
    assert list((resolve_runtime_data_root() / "runtime" / "pack-captures").iterdir()) == []


def test_portable_standalone_provenance_uses_verified_copy(tmp_path, monkeypatch):
    _payload, expected, record = _selected_pack(tmp_path / "pack")
    monkeypatch.setattr(generic_host, "_standalone_pack_capture", generic_host._portable_pack_capture)
    assert generic_host._run_pack_provenance(record, expected)[0]["observed_digest"] == expected
    from astrid.sdk.storage_root import resolve_runtime_data_root
    assert list((resolve_runtime_data_root() / "runtime" / "pack-captures").iterdir()) == []


@pytest.mark.parametrize("retained_custody", [False, True])
def test_portable_python_action_and_dependency_use_snapshots(tmp_path, monkeypatch, retained_custody):
    import base64
    import hashlib
    from runtime_protocol.verified_host_launch import canonical
    from tests.core.execution.test_generic_host_actions_v3 import pack

    sentinel = tmp_path / "python-entry.json"
    root, _host, _runtime, _definition, _admission = pack(tmp_path)
    (root / "actions" / "hello.py").write_text(
        "from pathlib import Path\nimport json\n"
        "import astrid.packs.hello_ext.api as helper\n"
        "def run(value='default'):\n"
        f"    Path({str(sentinel)!r}).write_text(json.dumps([__file__, helper.__file__, helper.VALUE]))\n"
        "    return 'snapshot action ran'\n")
    dependency = _write_action_pack(tmp_path / "dependencies" / "hello_ext")
    (dependency / "api.py").write_text("VALUE = 'admitted'\n")
    manifest = yaml.safe_load((root / "pack.yaml").read_text())
    manifest["dependencies"] = {"packs": ["hello_ext"]}
    (root / "pack.yaml").write_text(yaml.safe_dump(manifest))
    runtime = FakeRuntime()
    runtime.INLINE_SETTLEMENT_OUTPUTS = True
    host = GenericPackHost(pack_roots=[root, dependency], client=runtime, attempt_root=tmp_path / "attempt",
                           pack_capture=generic_host._portable_pack_capture)
    host.discover()
    _definition, admission = host.admit("executor", "unrelated.calculate")
    original_record = host.capabilities["unrelated.calculate"]
    if retained_custody:
        # The Runtime authority is a fixed fixture here. Exercise the real
        # descriptor, worker grant-byte check, and relocated-definition check.
        contents = {path.relative_to(root).as_posix(): path.read_bytes()
                    for path in root.rglob("*") if path.is_file()}
        chosen = {"path": "pack.yaml", "capability_kind": "executor", "capability_id": original_record.id,
                  "definition_digest": original_record.capability_digest}
        selected = canonical({"scope": {}, "selected_profile_digest": "0" * 64, "definition": chosen,
                              "source": {"root": str(root), "definitions": [chosen],
                                         "contents": {name: base64.b64encode(data).decode() for name, data in contents.items()},
                                         "files": {name: hashlib.sha256(data).hexdigest() for name, data in contents.items()}}})
        custody = object.__new__(generic_host._RuntimeSourceCustody)

        def select(record, **kwargs):
            assert record is original_record
            return selected

        custody.select = select
        monkeypatch.setattr(generic_host, "_runtime_external_sources", custody)
    task = _real_host_task(runtime, "unrelated.calculate", "portable-python")
    task["task"]["spec"]["spec"]["admitted_source_digest"] = admission["source_digest"]
    monkeypatch.setattr(generic_host, "process_birth_identity", lambda: "fixture-host-birth")
    invoke = host.invoke_capability
    snapshots = []

    def after_capture(**kwargs):
        selected = kwargs["admission"]["selected_pack_map"]
        snapshots.extend(Path(item["root"]) for item in selected.values())
        assert len(snapshots) == 2 and all(path.is_dir() for path in snapshots)
        assert Path(selected["unrelated"]["root"]) != root
        assert Path(selected["hello_ext"]["root"]) != dependency
        (root / "actions" / "hello.py").write_text("raise AssertionError('live action')")
        (dependency / "api.py").write_text("raise AssertionError('live dependency')")
        return invoke(**kwargs)

    monkeypatch.setattr(host, "invoke_capability", after_capture)
    host.run_task(task, lease_token="portable-python-lease")
    action_path, dependency_path, value = json.loads(sentinel.read_text())
    assert value == "admitted"
    assert any(Path(action_path).is_relative_to(snapshot) for snapshot in snapshots)
    assert any(Path(dependency_path).is_relative_to(snapshot) for snapshot in snapshots)
    assert all(not snapshot.parent.exists() for snapshot in snapshots)


def test_portable_worker_rejects_snapshot_different_from_retained_bytes(tmp_path):
    import base64
    import hashlib
    import tempfile
    from runtime_protocol.verified_host_launch import canonical
    from astrid.core.execution import generic_host_worker

    private = tmp_path / "private"
    generic_host.platform.private_directory(private)
    root = private / "pack"
    root.mkdir()
    (root / "code.py").write_text("unauthorized bytes")
    chosen = {"capability_id": "pack.run"}
    data = b"admitted bytes"
    selected = {"scope": {}, "selected_profile_digest": "0" * 64, "definition": chosen,
                "snapshot_root": str(root), "source": {"root": str(tmp_path / "live"),
                "definitions": [chosen], "files": {"code.py": hashlib.sha256(data).hexdigest()},
                "contents": {"code.py": base64.b64encode(data).decode()}}}
    with tempfile.TemporaryFile() as channel:
        channel.write(canonical(selected))
        channel.seek(0)
        with pytest.raises(ValueError, match="differs from Runtime-selected bytes"):
            generic_host_worker._read_captured_source(os.dup(channel.fileno()))


def test_portable_capture_cleans_read_only_source_copy(tmp_path):
    payload, expected, record = _selected_pack(tmp_path / "pack")
    payload.chmod(0o444)
    # Permission changes also change generation tokens; capture this generation.
    generation = generic_host._selected_pack_generation(record)
    try:
        with generic_host._portable_pack_capture(generation, expected_digest=expected) as captured:
            private = captured[2].parent
            assert pack_digest(captured[2]) == expected
        assert not private.exists()
        assert not payload.stat().st_mode & 0o200
    finally:
        payload.chmod(0o600)


def test_portable_capture_reports_cleanup_failure(tmp_path, monkeypatch):
    _payload, expected, record = _selected_pack(tmp_path / "pack")
    remove = generic_host._remove_pack_snapshot

    def denied(path):
        raise PermissionError("fixture cleanup denied")

    monkeypatch.setattr(generic_host, "_remove_pack_snapshot", denied)
    try:
        with pytest.raises(generic_host.PackCaptureError, match="snapshot cleanup failed"):
            with generic_host._portable_pack_capture(generic_host._selected_pack_generation(record),
                                                      expected_digest=expected) as captured:
                private = captured[2].parent
    finally:
        remove(private)


def test_linux_selector_keeps_refusal_without_lease_api(monkeypatch):
    with monkeypatch.context() as selection:
        selection.setattr(generic_host.sys, "platform", "linux")
        selection.setitem(generic_host.sys.modules, "fcntl", None)
        capture = generic_host._select_pack_capture()
    assert capture is generic_host._unavailable_pack_capture
    with pytest.raises(HostError, match="Linux file lease support"):
        with capture(object()):
            pytest.fail("Linux silently fell back from its lease policy")


def test_portable_copy_uses_the_digest_exclusion_rules(tmp_path):
    payload, _expected, record = _selected_pack(tmp_path / "pack")
    root = payload.parent
    # These names are valid, included source entries: exclusions depend on type.
    (root / ".cache").write_text("a regular source file")
    (root / "source.pyc").mkdir()
    (root / "source.pyc" / "included.py").write_text("VALUE = 1\n")
    (root / "__pycache__").mkdir()
    (root / "__pycache__" / "untrusted.pyc").write_bytes(b"ignored bytecode")
    (root / "ignored.pyc").write_bytes(b"ignored bytecode")
    expected = pack_digest(root)
    with generic_host._portable_pack_capture(generic_host._selected_pack_generation(record),
                                              expected_digest=expected) as captured:
        snapshot = captured[2]
        assert pack_digest(snapshot) == expected
        assert (snapshot / ".cache").is_file()
        assert (snapshot / "source.pyc" / "included.py").is_file()
        assert not (snapshot / "__pycache__").exists()
        assert not (snapshot / "ignored.pyc").exists()


@pytest.mark.parametrize("simulated_platform", ["darwin", "win32"])
def test_portable_real_bundled_action_executes(tmp_path, monkeypatch, simulated_platform):
    import subprocess
    import sys
    from astrid.core.pack.imports import selected_pack_environment

    root = Path(generic_host.__file__).resolve().parents[2] / "packs" / "rendering"
    with monkeypatch.context() as selection:
        selection.setattr(generic_host.sys, "platform", simulated_platform)
        host = GenericPackHost(pack_roots=[root])
    assert host._pack_capture is generic_host._portable_pack_capture
    host.discover()
    record = host.capabilities["rendering.html_canvas_effect"]
    selected = host._selected_pack_map(record)
    digest = selected["rendering"]["source_digest"]
    generation = generic_host._selected_pack_generation(record)
    out = tmp_path / "outputs"
    out.mkdir()
    with host._pack_capture(generation, expected_digest=digest) as captured:
        snapshot = captured[2]
        selected = {"rendering": {"root": str(snapshot), "source_digest": digest}}
        relocations = {"rendering": {"original_root": str(root), **selected["rendering"]}}
        control = {"pack_id": "rendering", **selected["rendering"], "map": selected,
                   "verified_relocations": relocations,
                   "argv": ["-m", "astrid.packs.rendering.actions.html_canvas_effect.run",
                            "--effect-id", "snapshot-proof", "--out", str(out / "report.json"),
                            "--timeline", str(out / "timeline.json"), "--assets", str(out / "assets.json")]}
        env = {**os.environ, **selected_pack_environment(selected, verified_relocations=relocations),
               "ASTRID_INTERNAL_INVOCATION": "1"}
        result = subprocess.run([sys.executable, "-m", "astrid.core.execution.generic_host_worker",
                                 "--pack-command", json.dumps(control)],
                                env=env, capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stderr
        report = json.loads((out / "report.json").read_text())
        assert report["ok"] is True
        assert Path(report["template"]).is_relative_to(snapshot)
        assert (out / "timeline.json").is_file() and (out / "assets.json").is_file()
    assert not snapshot.parent.exists()


@pytest.mark.parametrize("cleanup_fails", [False, True])
def test_portable_cleanup_precedes_success_publication(tmp_path, monkeypatch, cleanup_fails):
    monkeypatch.delenv("ASTRID_VIBECOMFY_CHECKOUT", raising=False)
    root, sentinel, _installed = _composition_pack(tmp_path, "selected-action")
    runtime = FakeRuntime()
    runtime.INLINE_SETTLEMENT_OUTPUTS = True
    host = GenericPackHost(pack_roots=[root], client=runtime, attempt_root=tmp_path / "attempt",
                           pack_capture=generic_host._portable_pack_capture)
    host.discover()
    task = _real_host_task(runtime, "hello_ext.run", "cleanup-order")
    monkeypatch.setattr(generic_host, "process_birth_identity", lambda: "fixture-host-birth")
    remove = generic_host._remove_pack_snapshot
    snapshots = []

    def reclaim(private):
        snapshots.append(private)
        assert not runtime.settlements
        receipt = tmp_path / "attempt" / "result.json"
        assert not receipt.exists() or json.loads(receipt.read_text())["status"] != "succeeded"
        if cleanup_fails:
            raise PermissionError("fixture cleanup denied")
        remove(private)

    monkeypatch.setattr(generic_host, "_remove_pack_snapshot", reclaim)
    try:
        if cleanup_fails:
            with pytest.raises(generic_host.PackCaptureError, match="snapshot cleanup failed"):
                host.run_task(task, lease_token="cleanup-lease")
        else:
            host.run_task(task, lease_token="cleanup-lease")
        assert sentinel.exists()  # Cleanup follows actual child execution.
        assert len(snapshots) == 1
        receipt = json.loads((tmp_path / "attempt" / "result.json").read_text())
        if cleanup_fails:
            assert snapshots[0].exists()
            assert not runtime.settlements and len(runtime.failures) == 1
            # Existing receipt semantics retain completed outputs as a partial
            # failed result; neither Runtime nor the receipt publishes success.
            assert receipt["status"] == "partial" and receipt["ok"] is False
            assert receipt["publication"]["status"] == "not_settled"
            assert runtime.failures[0][3]["structured_error"]["code"] == "selected_pack_capture_failed"
        else:
            assert not snapshots[0].exists()
            assert runtime.settlements and not runtime.failures
            assert receipt["status"] == "succeeded"
    finally:
        for private in snapshots:
            if private.exists():
                remove(private)


@pytest.mark.parametrize("simulated_platform", ["darwin", "win32"])
def test_portable_bundled_task_keeps_one_discovery_owner(tmp_path, monkeypatch, simulated_platform):
    # The real action rediscovers bundled element providers in its worker.
    monkeypatch.delenv("ASTRID_VIBECOMFY_CHECKOUT", raising=False)
    root = Path(generic_host.__file__).resolve().parents[2] / "packs" / "rendering"
    runtime = FakeRuntime()
    runtime.INLINE_SETTLEMENT_OUTPUTS = True
    with monkeypatch.context() as selection:
        selection.setattr(generic_host.sys, "platform", simulated_platform)
        host = GenericPackHost(pack_roots=[root], client=runtime, attempt_root=tmp_path / "attempt")
    host.discover()
    capability = "rendering.timeline_prepare_import"
    _definition, admission = host.admit("executor", capability)
    task = _real_host_task(runtime, capability, "bundled-task")
    task["task"]["spec"]["spec"].update(
        admitted_source_digest=admission["source_digest"],
        inputs={"project": "demo", "manifest": {"format": "astrid.timeline/1", "elements": []},
                "check_only": True},
    )
    monkeypatch.setattr(generic_host, "process_birth_identity", lambda: "fixture-host-birth")
    execute = host._run_command_definition
    snapshots = []

    def captured_action(record, *args, **kwargs):
        selected = kwargs["admission"]["selected_pack_map"]["rendering"]
        relocation = kwargs["admission"]["verified_pack_relocations"]["rendering"]
        snapshot = Path(selected["root"])
        assert snapshot != root and snapshot.is_dir()
        assert relocation == {"original_root": str(root), **selected}
        assert pack_digest(snapshot) == selected["source_digest"] == admission["source_digest"]
        assert Path(record.definition.metadata["pack_root"]) == snapshot
        snapshots.append(snapshot)
        return execute(record, *args, **kwargs)

    monkeypatch.setattr(host, "_run_command_definition", captured_action)
    host.run_task(task, lease_token="bundled-lease")
    assert runtime.settlements and not runtime.failures
    assert len(snapshots) == 1 and not snapshots[0].parent.exists()
    summary = json.loads((tmp_path / "attempt" / "outputs" / "result.json").read_text())
    assert summary["check_only"] is True
    receipt = json.loads((tmp_path / "attempt" / "result.json").read_text())
    assert receipt["status"] == "succeeded"
