"""M3-13 joined production owners; only test-owned roots and local Git edges.

PK-11: ported from M3-13 r6 4cf47a6a (read-only reference, never merged) and
rewritten for this head; see the PK-11 commit body for each change.

Head rewrite (PK-11, STOPS S23): r6's byte-acquisition guarantees (no read of
a selected pack's undeclared/authoring files, of a hidden managed checkout,
or of a dependent's payload/actions before its dependency is admitted) are
r6 product code that this head does not carry and the plan does not merge.
Those ``_unread``/ordering assertions are dropped here; what this head
guarantees for guarded and unselected packs is pinned by
``test_pack_discovery_single_scan.py::test_guarded_and_unselected_payloads_are_never_read``.
Everything else (selection, hidden exclusion, warm == cold, tamper refusal,
dependency failure and repair, Runtime catalog and CLI admission) is kept.
"""
from __future__ import annotations

import json
import subprocess
from contextlib import nullcontext
from pathlib import Path

import pytest

from astrid.core.pack import source_setup
from astrid.core.pack.discovery import (
    discover_canonical_pack_metadata, discover_pack_metadata, pack_discovery_scope,
)
from tests.packs.test_pack_discovery_single_scan import _write_v3_pack as _write_pack, resource_accesses  # noqa: F401


def _git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


def _install(tmp_path, monkeypatch, roots):
    declarations = []
    for root in roots:
        _git(root, "init", "-q")
        _git(root, "add", ".")
        _git(root, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.test",
             "commit", "-qm", "fixture")
        declarations.append(source_setup.SourceDeclaration(root.name, str(root), _git(root, "rev-parse", "HEAD")))
    state = tmp_path / "sources.json"
    monkeypatch.setenv("ASTRID_SOURCE_STATE", str(state))
    assert source_setup.provision(declarations, state_path=state, data_root=tmp_path / "installed")["ok"]
    return {row.pack_id: row.pack_root for row in source_setup.active_source_inventory().sources}


@pytest.fixture
def git_operations(monkeypatch):
    operations = []
    original = subprocess.run

    def observe(argv, *args, **kwargs):
        if isinstance(argv, (list, tuple)) and argv and argv[0] == "git":
            operations.append(tuple(argv))
        return original(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", observe)
    return operations


def _clear(events):
    for values in events.values():
        values.clear()


def _unread(events, files):
    for path, payload in files.items():
        assert path not in events["opens"], path
        assert path not in events["file_digests"], path
        assert not any(payload in chunk for chunk in events["hash_bytes"]), path


def _managed(tmp_path, monkeypatch):
    selected = _write_pack(tmp_path / "repos", "selected", resources=[
        {"path": "runtime.bin", "kind": "example"}], authoring_only=[
        {"path": "draft.bin", "kind": "source", "reason": "Draft."}])
    hidden = _write_pack(tmp_path / "repos", "hidden", visibility="hidden", resources=[
        {"path": "runtime.bin", "kind": "example"}])
    for root in (selected, hidden):
        (root / "runtime.bin").write_bytes(b"runtime-" + root.name.encode())
        (root / "undeclared.bin").write_bytes(b"undeclared-" + root.name.encode())
    (selected / "draft.bin").write_bytes(b"authoring-must-stay-unread")
    roots = _install(tmp_path, monkeypatch, (selected, hidden))
    forbidden = {roots[name] / filename: (roots[name] / filename).read_bytes()
                 for name, filename in (("selected", "undeclared.bin"), ("selected", "draft.bin"),
                                        ("hidden", "runtime.bin"), ("hidden", "undeclared.bin"),
                                        ("hidden", "rendering/a.yaml"))}
    return roots, forbidden


def test_real_managed_cold_warm_selection_and_default_custody(tmp_path, monkeypatch, resource_accesses, git_operations):
    roots, _forbidden = _managed(tmp_path, monkeypatch)
    selected = roots["selected"] / "runtime.bin"
    git_operations.clear()
    _clear(resource_accesses)
    with pack_discovery_scope():
        first = discover_pack_metadata(project_root=tmp_path)
        assert "selected" in {row.id for row in first}
        assert "hidden" not in {row.id for row in first}
        assert selected in resource_accesses["opens"]
        _clear(resource_accesses)
        assert discover_pack_metadata(project_root=tmp_path) == first
        assert selected in resource_accesses["opens"]  # custody is verified on cache hits too
        # (r6-only, dropped: unselected checkout drift is ignored. This head
        # verifies every managed checkout and refuses the dirty one.)
        selected.write_bytes(b"tampered-selected")
        with pytest.raises(source_setup.SourceSetupError, match="digest mismatch|dirty"):
            discover_pack_metadata(project_root=tmp_path)
    # (r6-only, dropped: discovery runs no `git status`. This head checks each
    # managed checkout for dirt with git during discovery.)
    with pytest.raises(source_setup.SourceSetupError, match="dirty"):
        source_setup.active_source_inventory()


@pytest.mark.parametrize("route", ["extra", "env", "managed"])
@pytest.mark.parametrize("cached", [False, True])
def test_real_external_dependency_failure_repair(tmp_path, monkeypatch, resource_accesses, route, cached):
    extras = tmp_path / "external"
    a = _write_pack(tmp_path / "repos" if route == "managed" else extras, "a", dependencies={"packs": ["b"]}, resources=[
        {"path": "payload.bin", "kind": "example"}])
    b = _write_pack(extras, "b", resources=[{"path": "payload.bin", "kind": "example"}])
    _write_pack(extras, "neighbor")
    for root in (a, b):
        (root / "payload.bin").write_bytes(b"dependent-payload-" + root.name.encode())
    if route == "managed":
        a = _install(tmp_path, monkeypatch, (a,))["a"]
    descriptor = b / "rendering/a.yaml"
    valid = descriptor.read_bytes()
    kwargs = {"project_root": tmp_path}
    if route in {"extra", "managed"}:
        kwargs["extra_pack_roots"] = (str(extras),)
    else:
        monkeypatch.setenv("ASTRID_PACKS_PATH", str(extras))
    with pack_discovery_scope() if cached else nullcontext():
        for broken in (True, False, True, False):
            descriptor.write_bytes(b'{"id":"wrong.renderer"}' if broken else valid)
            _clear(resource_accesses)
            rows = discover_pack_metadata(**kwargs)
            ids = {row.id for row in rows}
            assert "neighbor" in ids
            if broken:
                assert not {"a", "b"} & ids
            else:
                assert {"a", "b"} <= ids
            # (r6-only, dropped: the dependent's payload is never read before
            # its dependency is admitted, and the dependency's payload is read first.)
            canonical = {row.id for row in discover_canonical_pack_metadata(**kwargs)}
            assert ({"a", "b"} <= canonical) is not broken


def test_loader_discovery_runtime_catalog_cli_composition(tmp_path, monkeypatch, capsys, real_runtime, resource_accesses):
    """Real local source custody, dependency admission, Runtime catalog and CLI.

    The host executes no action. Its external action pack contains only declared
    inputs; managed sentinel packs have no host actions. No production owner is
    replaced by a fake, including loader, discovery, host, Runtime or CLI.
    """
    from astrid.core.cli.domain_tasks import build_parser
    from astrid.core.execution.generic_host import GenericPackHost, RuntimeProtocolClient
    from astrid.core.rendering.cli import main as rendering_cli
    from astrid.sdk import AstridClient
    from astrid_runtime_client import WorkspaceClient

    roots, _forbidden = _managed(tmp_path, monkeypatch)
    extras = tmp_path / "external"
    client_pack = _write_pack(extras, "client", dependencies={"packs": ["dependency"]}, actions={
        "echo": {"description": "Fixture echo.", "invocation": {
            "kind": "python", "path": "actions/echo.py", "function": "echo"},
            "inputs": [], "outputs": []}})
    (client_pack / "actions").mkdir()
    (client_pack / "actions/echo.py").write_text("def echo():\n    return {}\n")
    dependency = _write_pack(extras, "dependency")
    descriptor = dependency / "rendering/a.yaml"
    valid = descriptor.read_bytes()
    descriptor.write_text('{"id":"wrong.renderer"}')
    daemon = real_runtime()
    generated = WorkspaceClient(daemon.endpoint, daemon.token)
    generated.handshake("selected-source-composition", "1", ["projects:read", "worker:execute"])
    host = GenericPackHost(pack_roots=[extras], client=RuntimeProtocolClient(daemon.endpoint, daemon.worker_token),
                           attempt_base=tmp_path / "attempts")
    _clear(resource_accesses)
    try:
        with AstridClient.open(endpoint=daemon.endpoint, credential=daemon.credential_path,
                               realm_id=daemon.service.realm["id"], actor_id="owner",
                               client_name="composition-cli", client_version="1", protocol_version="workspace.v1") as client, pack_discovery_scope():
            project = client.projects.create(slug="composition", name="Composition", idempotency_key="project")
            assert project.ok, project.error
            parser = build_parser(client)

            def cli_admit(key):
                args = parser.parse_args(["create", "--project", project.data["project_id"],
                                          "--capability", "client.echo", "--spec", "{}",
                                          "--idempotency-key", key, "--json"])
                code = args.handler(args)
                return code, json.loads(capsys.readouterr().out)

            assert rendering_cli(["list", "--pack-root", str(extras), "--json"]) == 0
            ids = json.loads(capsys.readouterr().out)["ids"]
            assert "selected.renderer_0" in ids
            assert "client.renderer_0" not in ids
            assert "client.echo" not in {row.id for row in host.discover()}
            host.register()
            catalog, _ = generated.list_capabilities(limit=100)
            assert "client.echo" not in {row.capability_id for row in catalog}
            code, refused = cli_admit("broken-dependency")
            assert code != 0 and refused["ok"] is False
            assert refused["error"]["code"] == "not_found"
            # (r6-only, dropped: the quarantined client's action module and the
            # managed forbidden files stay unread.)

            descriptor.write_bytes(valid)
            assert rendering_cli(["list", "--pack-root", str(extras), "--json"]) == 0
            ids = json.loads(capsys.readouterr().out)["ids"]
            assert {"selected.renderer_0", "client.renderer_0", "dependency.renderer_0"} <= set(ids)
            record = next(row for row in host.discover() if row.id == "client.echo")
            registration = host.register(deliberate=True)
            assert any(row.capability_id == record.id for row in registration["registration"].capabilities)
            catalog, _ = generated.list_capabilities(limit=100)
            assert any(row.capability_id == record.id and row.definition_digest == record.capability_digest
                       for row in catalog)
            code, result = cli_admit("repaired-dependency")
            assert code == 0 and result["ok"] is True
            assert result["data"]["capability_id"] == record.id
            assert generated.get_task(result["data"]["task_id"]).capability_id == record.id
    finally:
        host.shutdown()


def test_external_payload_io_failure_closes_dependents_before_reads(tmp_path, monkeypatch, resource_accesses):
    import io

    extras = tmp_path / "external"
    a = _write_pack(extras, "a", dependencies={"packs": ["b"]}, resources=[
        {"path": "payload.bin", "kind": "example"}])
    b = _write_pack(extras, "b", resources=[{"path": "payload.bin", "kind": "example"}])
    _write_pack(extras, "neighbor")
    for root in (a, b):
        (root / "payload.bin").write_bytes(b"io-failure-payload-" + root.name.encode())
    real_open = io.open

    def unavailable(file, *args, **kwargs):
        if not isinstance(file, int) and Path(file) == b / "payload.bin":
            raise PermissionError("controlled external filesystem failure")
        return real_open(file, *args, **kwargs)

    _clear(resource_accesses)
    with monkeypatch.context() as edge:
        edge.setattr(io, "open", unavailable)
        ids = {row.id for row in discover_pack_metadata(extra_pack_roots=(str(extras),))}
    assert "neighbor" in ids
    assert not {"a", "b"} & ids
    # (r6-only, dropped: the dependent's payload stays unread after the dependency's I/O failure.)
    assert {"a", "b"} <= {row.id for row in discover_pack_metadata(extra_pack_roots=(str(extras),))}
