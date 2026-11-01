from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from astrid.core.gateway import dispatch
from astrid.core.gateway import main as gateway_main
from astrid.sdk.client import AstridClient
from astrid_runtime_client import ApiError, WorkspaceClient
from tests.helpers.runtime import use_runtime, vendored_runtime_commit

pytestmark = pytest.mark.integration  # needs the Runtime from a second checkout (real_runtime fixture)
# The Runtime as of the commit the vendored workspace client was produced from, so the daemon's schema
# digest cannot drift from the client's.
RUNTIME = use_runtime(vendored_runtime_commit())


@pytest.fixture(autouse=True)
def _isolate_runtime_support_root(tmp_path, monkeypatch):
    """Never let this acceptance module resolve Astrid's installed support root."""
    monkeypatch.setenv("ASTRID_RUNTIME_DATA_ROOT", str((tmp_path / "runtime-support").resolve()))


def _fake_runtime_cli(monkeypatch, *, data, returncode=0):
    """Inject only the current read-only Runtime observer boundary."""
    from astrid.runtime_cli import RuntimeResult

    calls = []

    class FakeRuntimeCLI:
        def observe(self, command, *, support_root, timeout=5.0):
            calls.append((command, str(support_root), timeout))
            return RuntimeResult(("fake-astrid-runtime", command), returncode, data)

    monkeypatch.setattr("astrid.runtime_cli.RuntimeCLI", FakeRuntimeCLI)
    return calls


def _pin_runtime_cli_subprocess(monkeypatch):
    """Run the operational doctor subprocess from this test's exact archive."""
    from astrid import runtime_cli

    def runner(argv, **kwargs):
        env = os.environ.copy()
        existing_pythonpath = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = str(RUNTIME) + (os.pathsep + existing_pythonpath if existing_pythonpath else "")
        return subprocess.run(argv, cwd=str(RUNTIME), env=env, **kwargs)

    original = runtime_cli.RuntimeCLI

    class PinnedRuntimeCLI(original):
        def __init__(self):
            super().__init__(
                command=[sys.executable, "-B", "-m", "astrid_runtime"],
                runner=runner,
            )

    monkeypatch.setattr(runtime_cli, "RuntimeCLI", PinnedRuntimeCLI)


def _open_client(daemon) -> AstridClient:
    """Open with the runtime-issued identity and canonical protocol explicitly."""
    return AstridClient.open(
        endpoint=daemon.endpoint,
        credential=daemon.credential_path,
        realm_id=daemon.service.realm["id"],
        actor_id="owner",
        client_name="astrid-stage1-tests",
        client_version="stage1",
        protocol_version="workspace.v1",
    )


def _register_render_basic(daemon) -> None:
    """Seed the capability catalog required by task-admission journeys."""
    daemon.service.register_capability({
        "capability_id": "render.basic",
        "definition_digest": "sha256:" + hashlib.sha256(b"render.basic").hexdigest(),
        "status": "ready",
    })


def _use_explicit_gateway_connection(monkeypatch: pytest.MonkeyPatch, daemon) -> None:
    """Keep gateway integration online without asking the launcher to boot."""
    monkeypatch.setattr(
        AstridClient,
        "open_from_launcher",
        classmethod(lambda cls, **_kwargs: _open_client(daemon)),
    )


def test_backup_export_rejects_unsupported_out_without_opening_client(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(
        AstridClient,
        "open_from_launcher",
        classmethod(lambda cls, **_kwargs: pytest.fail("invalid export arguments must not open the client")),
    )

    assert gateway_main(["backup", "export", "--out", str(tmp_path / "export.json")]) == 2

    captured = capsys.readouterr()
    assert "usage: astrid backup" in captured.err
    assert "backup export does not support --out" in captured.err


def test_backup_export_without_out_preserves_plain_and_json_behavior(monkeypatch, capsys):
    calls = []

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def export_realm(self):
            calls.append("export")
            return {"format_version": 1, "realm": {"id": "test-realm"}}

    def open_fake_client(cls, **kwargs):
        calls.append(("open", kwargs))
        return FakeClient()

    monkeypatch.setattr(AstridClient, "open_from_launcher", classmethod(open_fake_client))

    assert gateway_main(["backup", "export"]) == 0
    assert capsys.readouterr().out == "Astrid backup: export complete\n"

    assert gateway_main(["backup", "export", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "ok": True,
        "format_version": 1,
        "realm": {"id": "test-realm"},
    }
    assert calls == [
        ("open", {"start_pack_host": False}),
        "export",
        ("open", {"start_pack_host": False}),
        "export",
    ]


@pytest.mark.parametrize(
    ("args", "error_fragment"),
    [
        (["create"], "backup create requires DESTINATION or --out"),
        (["restore", "bundle"], "backup restore requires DESTINATION or --destination"),
        (["restore", ""], "backup restore requires BACKUP"),
        (["recover", "--expected-realm-id", "realm", "--expected-version", "1"], "--confirm"),
        (["recover", "--expected-realm-id", "realm", "--confirm", "RECOVER realm"], "--expected-version"),
        (["recover", "--expected-realm-id", "realm", "--expected-version", "1", "--confirm", "RECOVER other"], "backup recover requires --confirm"),
        (["recover", "--expected-realm-id", "realm", "--expected-version", "0", "--confirm", "RECOVER realm"], "must be positive"),
        (["tombstone", "--expected-version", "0"], "must be positive"),
        (["purge", ""], "non-empty confirmation"),
    ],
)
def test_backup_invalid_arguments_fail_before_runtime_open(args, error_fragment, monkeypatch, capsys):
    monkeypatch.setattr(
        AstridClient,
        "open_from_launcher",
        classmethod(lambda cls, **_kwargs: pytest.fail("invalid backup arguments must not open Runtime")),
    )

    assert gateway_main(["backup", *args]) == 2
    assert error_fragment in capsys.readouterr().err


def test_backup_gateway_routes_forward_runtime_arguments_without_pack_host(monkeypatch, capsys):
    calls = []

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def create_backup(self, destination):
            calls.append(("create", destination))
            return {"destination": destination}

        def restore_backup(self, backup, destination):
            calls.append(("restore", backup, destination))
            return {"backup": backup, "destination": destination}

        def export_realm(self):
            calls.append(("export",))
            return {"format_version": 1}

        def tombstone_realm(self, *, reason=None, expected_version=None):
            calls.append(("tombstone", reason, expected_version))
            return {"state": "tombstoned"}

        def recover_realm(self, *, expected_realm_id, expected_version, confirmation=None, noninteractive=False):
            calls.append(("recover", expected_realm_id, expected_version, confirmation, noninteractive))
            return {"state": "active"}

        def purge_realm(self, confirmation):
            calls.append(("purge", confirmation))
            return {"state": "offline_only"}

    def open_fake_client(cls, **kwargs):
        calls.append(("open", kwargs))
        return FakeClient()

    monkeypatch.setattr(AstridClient, "open_from_launcher", classmethod(open_fake_client))
    cases = [
        (["create", "/tmp/backup"], {"destination": "/tmp/backup"}),
        (["restore", "/tmp/backup", "/tmp/restored"], {"backup": "/tmp/backup", "destination": "/tmp/restored"}),
        (["export"], {"format_version": 1}),
        (["tombstone", "--reason", "maintenance", "--expected-version", "4"], {"state": "tombstoned"}),
        (["recover", "--expected-realm-id", "realm-1", "--expected-version", "5", "--confirm", "RECOVER realm-1"], {"state": "active"}),
        (["purge", "PURGE realm-1"], {"state": "offline_only"}),
    ]

    for args, expected in cases:
        assert gateway_main(["backup", *args, "--json"]) == 0
        assert json.loads(capsys.readouterr().out) == {"ok": True, **expected}

    assert calls == [
        ("open", {"start_pack_host": False}),
        ("create", "/tmp/backup"),
        ("open", {"start_pack_host": False}),
        ("restore", "/tmp/backup", "/tmp/restored"),
        ("open", {"start_pack_host": False}),
        ("export",),
        ("open", {"start_pack_host": False}),
        ("tombstone", "maintenance", 4),
        ("open", {"start_pack_host": False}),
        ("recover", "realm-1", 5, "RECOVER realm-1", False),
        ("open", {"start_pack_host": False}),
        ("purge", "PURGE realm-1"),
    ]


def test_product_client_requires_bootstrap_with_exact_next_action(tmp_path, monkeypatch):
    monkeypatch.setenv("ASTRID_RUNTIME_CREDENTIAL", str(tmp_path / "missing-credential.json"))
    monkeypatch.delenv("ASTRID_RUNTIME_ENDPOINT", raising=False)
    with pytest.raises(Exception, match=r"run the Astrid install line again"):
        AstridClient.open()


def test_product_client_crosses_real_daemon_and_returns_stable_envelopes(tmp_path, monkeypatch, real_runtime):
    daemon = real_runtime()
    _register_render_basic(daemon)
    monkeypatch.setenv("ASTRID_RUNTIME_ENDPOINT", daemon.endpoint)
    monkeypatch.setenv("ASTRID_RUNTIME_CREDENTIAL", str(tmp_path / "support" / "credentials" / "owner.token"))
    try:
        client = _open_client(daemon)
        created = client.projects.create(slug="demo", name="Demo", idempotency_key="project-1")
        assert set(created.as_dict()) == {"ok", "data", "error", "receipt", "idempotency_key"}
        assert created.ok and created.data["name"] == "Demo"
        media_path = tmp_path / "clip.bin"
        media_path.write_bytes(b"hello")
        imported = client.media.import_file(project="demo", path=media_path, idempotency_key="media-1")
        assert imported.ok and imported.data["digest"].startswith("sha256:")
        task = client.tasks.create(project_id="demo", capability="render.basic", spec={}, idempotency_key="task-1")
        assert task.ok and task.data["capability_id"] == "render.basic"
        assert client.tasks.show(task.data["task_id"]).ok
    finally:
        daemon.stop()


def test_retired_timeline_create_is_absent_from_cli_and_sdk(tmp_path, monkeypatch, capsys, real_runtime):
    """Timeline documents are created through the canonical composition flow, not the retired route."""
    daemon = real_runtime()
    _use_explicit_gateway_connection(monkeypatch, daemon)
    try:
        assert gateway_main(
            ["projects", "create", "demo", "--name", "Demo", "--json"]
        ) == 0
        project = json.loads(capsys.readouterr().out)
        assert project["ok"]

        assert gateway_main(
            [
                "timelines",
                "create",
                "--project",
                "demo",
                "primary",
                "--name",
                "Primary",
                "--default",
                "--json",
            ]
        ) == 2
        err = capsys.readouterr().err
        assert "usage: astrid timelines" in err
        # `--name` is a deliberate flag of the canonical create verb (display name,
        # schema v30); `--default` belongs to the retired create route and stays absent.
        assert "unrecognized arguments: --default" in err
        assert "--name" not in err.split("unrecognized arguments:")[1]

        client = _open_client(daemon)
        retired = client.timelines.create(
            project="demo", config={}, registry={}, slug="primary", idempotency_key="retired-create"
        )
        assert not retired.ok
        assert retired.error.code == "retired_route"
        assert retired.error.details["replacement"] == "timelines.inspect/open_composition"
    finally:
        daemon.stop()


def test_cold_mutations_expose_server_receipts_on_cli_sdk_replay(tmp_path, monkeypatch, capsys, real_runtime):
    daemon = real_runtime()
    _register_render_basic(daemon)
    monkeypatch.setenv("ASTRID_RUNTIME_ENDPOINT", daemon.endpoint)
    monkeypatch.setenv("ASTRID_RUNTIME_CREDENTIAL", str(tmp_path / "support" / "credentials" / "owner.token"))
    _use_explicit_gateway_connection(monkeypatch, daemon)
    try:
        with _open_client(daemon) as client:
            assert gateway_main(["projects", "create", "cold", "--name", "Cold", "--idempotency-key", "cold-project", "--json"]) == 0
            created = json.loads(capsys.readouterr().out)
            assert created["ok"] and created["data"]["slug"] == "cold"
            assert created["receipt"] is not None
            assert created["receipt"]["command_kind"] == "project.create"
            project_id = created["data"]["project_id"]
            replay = client.projects.create(slug="cold", name="Cold", idempotency_key="cold-project")
            assert replay.ok and replay.receipt is not None
            assert replay.receipt.command_kind == "project.create"
            assert replay.receipt.as_dict() == created["receipt"]

            assert gateway_main(["projects", "select", project_id, "--scope", "workspace", "--json"]) == 0
            selected = json.loads(capsys.readouterr().out)
            assert selected["ok"] and selected["data"]["project"]["project_id"] == project_id
            sdk_selected = client.projects.select(project_id, scope="workspace", idempotency_key=selected["idempotency_key"])
            assert sdk_selected.ok

            assert gateway_main(["tasks", "create", "--project", project_id, "--capability", "render.basic", "--spec", "{}", "--idempotency-key", "cold-task", "--json"]) == 0
            admitted = json.loads(capsys.readouterr().out)
            assert admitted["ok"] and admitted["data"]["capability_id"] == "render.basic"
            assert admitted["receipt"] is not None
            assert admitted["receipt"]["command_kind"] == "task.create"
            sdk_task = client.tasks.create(project_id=project_id, capability="render.basic", spec={}, idempotency_key="cold-task")
            assert sdk_task.ok and sdk_task.receipt is not None
            assert sdk_task.receipt.command_kind == "task.create"
            assert sdk_task.receipt.as_dict() == admitted["receipt"]

            before = daemon.service.store.conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
            daemon.service.register_capability({
                "capability_id": "cold.gpu",
                "definition_digest": "sha256:" + hashlib.sha256(b"cold.gpu").hexdigest(),
                "status": "unavailable",
                "unavailable_reason": "gpu_not_configured",
            })
            failed = client.tasks.create(project_id=project_id, capability="cold.gpu", spec={}, idempotency_key="cold-gpu")
            assert not failed.ok and failed.receipt is None and failed.error.code == "unavailable"
            assert daemon.service.store.conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == before
    finally:
        daemon.stop()


def test_projects_selection_and_unready_admission_are_typed_on_real_daemon(tmp_path, monkeypatch, real_runtime):
    daemon = real_runtime()
    monkeypatch.setenv("ASTRID_RUNTIME_ENDPOINT", daemon.endpoint)
    monkeypatch.setenv("ASTRID_RUNTIME_CREDENTIAL", str(tmp_path / "support" / "credentials" / "owner.token"))
    try:
        with _open_client(daemon) as client:
            project = client.projects.create(slug="selected", name="Selected", idempotency_key="selected").data
            selected = client.projects.select(project["project_id"], scope="workspace")
            assert selected.ok and selected.data["project"]["project_id"] == project["project_id"]
            current = client.projects.current()
            assert current.ok and current.data["scope"] == "workspace"
            assert current.data["project"]["project_id"] == project["project_id"]

            import hashlib

            digest = "sha256:" + hashlib.sha256(b"acceptance.gpu").hexdigest()
            daemon.service.register_capability({
                "capability_id": "acceptance.gpu",
                "definition_digest": digest,
                "status": "unavailable",
                "unavailable_reason": "gpu_not_configured",
            })
            before = daemon.service.store.conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
            blocked = client.tasks.create(project_id=project["project_id"], capability="acceptance.gpu", spec={}, idempotency_key="blocked-gpu")
            assert not blocked.ok and blocked.error.code == "unavailable"
            assert blocked.error.details["next_action"] == "fix: gpu_not_configured; run `astrid doctor --json`, then retry"
            assert daemon.service.store.conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == before
    finally:
        daemon.stop()


def test_documented_project_cli_mutations_return_committed_receipts(tmp_path, monkeypatch, capsys, real_runtime):
    daemon = real_runtime()
    monkeypatch.setenv("ASTRID_RUNTIME_ENDPOINT", daemon.endpoint)
    monkeypatch.setenv("ASTRID_RUNTIME_CREDENTIAL", str(tmp_path / "support" / "credentials" / "owner.token"))
    _use_explicit_gateway_connection(monkeypatch, daemon)
    _use_explicit_gateway_connection(monkeypatch, daemon)
    try:
        client = _open_client(daemon)
        assert client.projects.create(slug="cli", name="CLI", idempotency_key="cli-project").ok
        first_path, second_path = tmp_path / "first.bin", tmp_path / "second.bin"
        first_path.write_bytes(b"first")
        second_path.write_bytes(b"second")
        first = client.media.import_file(project="cli", path=first_path, idempotency_key="cli-media-1").data
        second = client.media.import_file(project="cli", path=second_path, idempotency_key="cli-media-2").data
        ref_a = client.references.create(project="cli", kind="character", name="A", media_id=first["object_id"], idempotency_key="cli-ref-a").data
        ref_b = client.references.create(project="cli", kind="character", name="B", media_id=second["object_id"], idempotency_key="cli-ref-b").data
        association = client.references.associate("cli", ref_a["reference_id"], media_id=second["object_id"], idempotency_key="cli-association").data
        shot = client.shots.create(project="cli", name="Shot", idempotency_key="cli-shot").data
        first_item = client.shots.add_item("cli", shot["shot_id"], media_id=first["object_id"], idempotency_key="cli-item-1").data
        second_item = client.shots.add_item("cli", shot["shot_id"], media_id=second["object_id"], idempotency_key="cli-item-2").data
        assert association["media_references"]

        assert gateway_main(["media", "references", "link", "--project", "cli", "--from", ref_a["reference_id"], "--to", ref_b["reference_id"], "--kind", "related_to", "--idempotency-key", "cli-link", "--json"]) == 0
        link_result = json.loads(capsys.readouterr().out)
        assert link_result["ok"]

        assert gateway_main(["media", "references", "set-primary", ref_a["reference_id"], "--project", "cli", "--media-reference", association["media_references"][-1]["association_id"], "--idempotency-key", "cli-primary", "--json"]) == 0
        primary_result = json.loads(capsys.readouterr().out)
        assert primary_result["ok"]

        assert gateway_main(["timelines", "shots", "reorder", shot["shot_id"], "--project", "cli", "--items", f"{second_item['items'][1]['item_id']},{first_item['items'][0]['item_id']}", "--idempotency-key", "cli-reorder", "--json"]) == 0
        reorder_result = json.loads(capsys.readouterr().out)
        assert reorder_result["ok"]
        assert set(reorder_result) == {"ok", "data", "error", "receipt", "idempotency_key"}
    finally:
        daemon.stop()


def test_project_shot_scope_stays_with_project_route(tmp_path, monkeypatch, real_runtime):
    daemon = real_runtime()
    monkeypatch.setenv("ASTRID_RUNTIME_ENDPOINT", daemon.endpoint)
    monkeypatch.setenv("ASTRID_RUNTIME_CREDENTIAL", str(tmp_path / "support" / "credentials" / "owner.token"))

    def assert_not_found(result):
        assert not result.ok
        assert result.error is not None and result.error.code == "not_found"
        assert result.receipt is None

    try:
        client = _open_client(daemon)
        owner = client.projects.create(slug="shot-owner", name="Shot owner", idempotency_key="shot-owner").data
        other = client.projects.create(slug="shot-other", name="Shot other", idempotency_key="shot-other").data
        legacy = client.shots.create(
            project=owner["project_id"],
            shot={"shot_id": "collision", "name": "Owner shot"},
            idempotency_key="legacy-shot",
        )
        assert legacy.ok
        before = client._remote._transport.get_project_shot(owner["project_id"], "collision")

        # A project-scoped read and every version-resolving mutation must stay
        # in the project route.  The colliding legacy child is not in other.
        assert_not_found(client.shots.show(other["project_id"], "collision"))
        assert_not_found(client.shots.update(other["project_id"], "collision", name="Hacked", idempotency_key="wrong-update"))
        assert_not_found(client.shots.archive(other["project_id"], "collision", idempotency_key="wrong-archive"))
        assert_not_found(client.shots.recover(other["project_id"], "collision", idempotency_key="wrong-recover"))
        assert client._remote._transport.get_project_shot(owner["project_id"], "collision") == before

        # Project-owned mutations remain receipt-bearing on success.
        owned = client.shots.create(project=other["project_id"], name="Owned", idempotency_key="owned-shot")
        assert owned.ok
        updated = client.shots.update(other["project_id"], owned.data["shot_id"], name="Owned 2", idempotency_key="owned-update")
        assert updated.ok
        archived = client.shots.archive(other["project_id"], owned.data["shot_id"], idempotency_key="owned-archive")
        assert archived.ok
        recovered = client.shots.recover(other["project_id"], owned.data["shot_id"], idempotency_key="owned-recover")
        assert recovered.ok
    finally:
        daemon.stop()


def test_project_reference_scope_stays_with_project_route(tmp_path, monkeypatch, real_runtime):
    daemon = real_runtime()
    monkeypatch.setenv("ASTRID_RUNTIME_ENDPOINT", daemon.endpoint)
    monkeypatch.setenv("ASTRID_RUNTIME_CREDENTIAL", str(tmp_path / "support" / "credentials" / "owner.token"))

    def assert_not_found(result):
        assert not result.ok
        assert result.error is not None and result.error.code == "not_found"
        assert result.receipt is None

    try:
        client = _open_client(daemon)
        owner = client.projects.create(slug="reference-owner", name="Reference owner", idempotency_key="reference-owner").data
        other = client.projects.create(slug="reference-other", name="Reference other", idempotency_key="reference-other").data
        owner_path, other_path = tmp_path / "owner.bin", tmp_path / "other.bin"
        owner_path.write_bytes(b"owner")
        other_path.write_bytes(b"other")
        owner_media = client.media.import_file(project=owner["project_id"], path=owner_path, idempotency_key="owner-media").data
        other_media = client.media.import_file(project=other["project_id"], path=other_path, idempotency_key="other-media").data
        legacy = client.references.create(
            project=owner["project_id"],
            reference_id="collision",
            kind="character",
            name="Owner reference",
            media_id=owner_media["object_id"],
            idempotency_key="legacy-reference",
        )
        assert legacy.ok
        before = client._remote._transport.get_project_reference(owner["project_id"], "collision")

        assert_not_found(client.references.show(other["project_id"], "collision"))
        assert_not_found(client.references.update(other["project_id"], "collision", name="Hacked", idempotency_key="wrong-update"))
        assert_not_found(client.references.archive(other["project_id"], "collision", idempotency_key="wrong-archive"))
        assert_not_found(client.references.recover(other["project_id"], "collision", idempotency_key="wrong-recover"))
        assert client._remote._transport.get_project_reference(owner["project_id"], "collision") == before

        owned = client.references.create(
            project=other["project_id"],
            reference_id="owned-reference",
            kind="character",
            name="Owned",
            media_id=other_media["object_id"],
            idempotency_key="owned-reference",
        )
        assert owned.ok
        updated = client.references.update(other["project_id"], "owned-reference", name="Owned 2", idempotency_key="owned-update")
        assert updated.ok
        archived = client.references.archive(other["project_id"], "owned-reference", idempotency_key="owned-archive")
        assert archived.ok
        recovered = client.references.recover(other["project_id"], "owned-reference", idempotency_key="owned-recover")
        assert recovered.ok
    finally:
        daemon.stop()


def test_remote_reads_are_scoped_and_unsupported_operations_fail_honestly(tmp_path, monkeypatch, real_runtime):
    daemon = real_runtime()
    monkeypatch.setenv("ASTRID_RUNTIME_ENDPOINT", daemon.endpoint)
    monkeypatch.setenv("ASTRID_RUNTIME_CREDENTIAL", str(tmp_path / "support" / "credentials" / "owner.token"))
    try:
        client = _open_client(daemon)
        project = client.projects.create(slug="scoped", name="Scoped", idempotency_key="p")
        source = tmp_path / "scoped.bin"
        source.write_bytes(b"scoped")
        imported = client.media.import_file(project="scoped", path=source, idempotency_key="m")
        assert imported.ok
        # The managed copy, not the caller's file, is the durable object: with
        # the source gone the generated client still reads the exact bytes and
        # an exact byte range from the realm's CAS.
        source.unlink()
        generated = WorkspaceClient(daemon.endpoint, daemon.token)
        assert generated.get_object(imported.data["object_id"]).data == b"scoped"
        ranged = generated.get_object(imported.data["object_id"], byte_range=(0, 3))
        assert ranged.status == 206 and ranged.data == b"scop"
        assert generated.head_object(imported.data["object_id"]).etag
        listed = client.media.list("scoped")
        assert listed.ok and any(item.get("digest") == imported.data["digest"] for item in listed.data[0])
        verified = client.media.verify("scoped", imported.data["digest"])
        assert verified.ok and verified.data["verified"] is True
        tasks = client.tasks.list("scoped")
        assert tasks.ok and tasks.data[0] == []
        missing_save = client.timelines.save("scoped", "missing", config={}, registry={})
        assert not missing_save.ok
    finally:
        daemon.stop()


def test_client_reopens_after_close_against_same_daemon(tmp_path, monkeypatch, real_runtime):
    daemon = real_runtime()
    monkeypatch.setenv("ASTRID_RUNTIME_ENDPOINT", daemon.endpoint)
    monkeypatch.setenv("ASTRID_RUNTIME_CREDENTIAL", str(tmp_path / "support" / "credentials" / "owner.token"))
    try:
        first = _open_client(daemon)
        assert isinstance(first.health(), dict)
        second = _open_client(daemon)
        assert isinstance(second.health(), dict)
    finally:
        daemon.stop()


def test_serve_is_not_a_public_dispatch_command(capsys):
    assert "serve" not in dispatch._top_level_commands()
    assert dispatch._top_level_commands() == frozenset({
        "projects", "documents", "preferences", "timelines", "media", "tasks", "runs",
        "dev", "doctor", "backup",
    })


def test_remote_boundary_has_no_storage_or_runtime_imports():
    source = "\n".join((Path(__file__).parents[2] / "astrid" / "sdk" / name).read_text() for name in ("remote.py", "workspace_client.py"))
    assert "import sqlite" not in source and "import cas" not in source
    assert "runtime_protocol" not in source


def test_sdk_client_import_does_not_construct_local_authority():
    import subprocess

    result = subprocess.run(
        [sys.executable, "-c", "import sys; import astrid.sdk.client; print([name for name in sys.modules if name == 'astrid.application' or name.startswith('astrid.core.threads') or name == 'sqlite3'])"],
        capture_output=True,
        text=True,
        check=True,
        cwd=str(Path(__file__).parents[2]),
    )
    assert result.stdout.strip() == "[]"


def test_client_boundary_has_no_local_authority_escape_hatch():
    source = (Path(__file__).parents[2] / "astrid" / "sdk" / "client.py").read_text()
    assert "compose_standard_application" not in source
    assert "database_path" not in source
    assert "projects_root" not in source
    with pytest.raises(TypeError):
        AstridClient.open(projects_root="/tmp/should-not-open")


def test_retired_public_commands_are_absent():
    from astrid.core.cli.domain_media import build_parser as media_parser
    from astrid.core.pack.cli_parser import build_parser

    pack_commands = next(action for action in build_parser()._actions if isinstance(getattr(action, "choices", None), dict)).choices
    assert not {"install", "update", "rollback", "uninstall"} & set(pack_commands)
    media_commands = next(action for action in media_parser(None)._actions if isinstance(getattr(action, "choices", None), dict)).choices
    assert "relocate" not in media_commands


def test_doctor_and_backup_never_open_local_storage(capsys, monkeypatch, tmp_path):
    monkeypatch.delenv("ASTRID_RUNTIME_ENDPOINT", raising=False)
    monkeypatch.setenv("ASTRID_RUNTIME_CREDENTIAL", str(tmp_path / "missing.token"))
    calls = _fake_runtime_cli(
        monkeypatch,
        data={
            "ok": False,
            "problem_code": "runtime_unavailable",
            "error": "runtime unavailable",
            "next_action": "astrid-runtime up",
        },
        returncode=1,
    )

    assert dispatch._dispatch_doctor(["--json"]) == 1
    doctor = json.loads(capsys.readouterr().out)
    assert doctor["next_action"] == "astrid-runtime up"
    assert len(calls) == 1 and calls[0][0] == "doctor"
    assert dispatch._dispatch_backup(["--json"]) == 1
    assert "run the Astrid install line again" in capsys.readouterr().out


def test_doctor_uses_the_read_only_runtime_observer(capsys, monkeypatch):
    monkeypatch.setattr("astrid.core.rendering.remotion_runtime.remotion_install_problem", lambda _project: None)
    payload = {
        "healthy": True,
        "recovery_action": "No recovery action required.",
    }
    calls = _fake_runtime_cli(monkeypatch, data=payload)
    monkeypatch.setattr(
        AstridClient,
        "open_from_launcher",
        classmethod(lambda cls, **_kwargs: pytest.fail("doctor must not open the product client")),
    )

    assert dispatch._dispatch_doctor(["--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["healthy"] is True
    assert report["recovery_action"] == payload["recovery_action"]
    assert report["effects"] == ["observe"]
    assert report["authorization_required"] is False
    assert report["diagnostic"]["facts"]["runtime"]["value"] == "ready"
    # The doctor observation is followed by the read-only status observer for runtime health.
    assert [name for name, _root, _timeout in calls] == ["doctor", "status"]


def test_doctor_preserves_observer_failure_and_diagnostic(capsys, monkeypatch):
    payload = {
        "ok": False,
        "problem_code": "permission_limited",
        "error": "runtime observer permission denied",
    }
    calls = _fake_runtime_cli(monkeypatch, data=payload, returncode=1)
    monkeypatch.setattr(
        AstridClient,
        "open_from_launcher",
        classmethod(lambda cls, **_kwargs: pytest.fail("doctor must not open the product client")),
    )

    assert dispatch._dispatch_doctor(["--json"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["problem_code"] == "permission_limited"
    assert report["error"] == payload["error"]
    assert report["diagnostic"]["problemCode"] == "permission_limited"
    assert report["diagnostic"]["failureBoundary"] == "runtime-permission"
    assert len(calls) == 1 and calls[0][0] == "doctor"


def test_remote_domains_use_generated_runtime_and_reopen(tmp_path, monkeypatch, real_runtime):
    daemon = real_runtime()
    _register_render_basic(daemon)
    monkeypatch.setenv("ASTRID_RUNTIME_ENDPOINT", daemon.endpoint)
    monkeypatch.setenv("ASTRID_RUNTIME_CREDENTIAL", str(tmp_path / "support" / "credentials" / "owner.token"))
    try:
        client = _open_client(daemon)
        project = client.projects.create(slug="journey", name="Journey", idempotency_key="journey-project")
        assert project.ok
        retired = client.timelines.create(project="journey", config={}, registry={}, slug="main", idempotency_key="journey-timeline")
        assert not retired.ok and retired.error.code == "retired_route"
        listed = client.timelines.list("journey")
        assert listed.ok and listed.data[0] == []
        task = client.tasks.create(project_id="journey", capability="render.basic", spec={}, idempotency_key="journey-task")
        assert task.ok
        assert client.tasks.show(task.data["task_id"]).ok
        assert client.tasks.events(task.data["task_id"]).ok
        invoked = client.invoke("render.basic", project_id="journey", spec={}, idempotency_key="journey-invoke")
        assert invoked.ok
        assert not hasattr(client, "render")
        run = client.runs.show(task.data["run_id"])
        assert run.ok
        assert client.runs.events(task.data["run_id"]).ok
        daemon.stop()
        daemon = real_runtime()
        monkeypatch.setenv("ASTRID_RUNTIME_ENDPOINT", daemon.endpoint)
        reopened = _open_client(daemon)
        assert reopened.projects.show("journey").ok
        assert reopened.timelines.list("journey").ok
    finally:
        daemon.stop()


def test_editor_domain_reads_and_media_relations_use_generated_operations(tmp_path, monkeypatch, real_runtime):
    daemon = real_runtime()
    _register_render_basic(daemon)
    monkeypatch.setenv("ASTRID_RUNTIME_ENDPOINT", daemon.endpoint)
    monkeypatch.setenv("ASTRID_RUNTIME_CREDENTIAL", str(tmp_path / "support" / "credentials" / "owner.token"))
    try:
        client = _open_client(daemon)
        project = client.projects.create(slug="editor", name="Editor", idempotency_key="project").data
        project_id = project["project_id"]
        first_path, second_path = tmp_path / "first.bin", tmp_path / "second.bin"
        first_path.write_bytes(b"first")
        second_path.write_bytes(b"second")
        first = client.media.import_file(project=project_id, path=first_path, idempotency_key="first").data
        second = client.media.import_file(project=project_id, path=second_path, idempotency_key="second").data
        relation = client.media.relate(
            project_id,
            from_object_id=first["object_id"],
            to_object_id=second["object_id"],
            kind="derived_from",
            metadata={"source": "test"},
            idempotency_key="relation",
        )
        assert relation.ok and client.media.list_relations(project_id).data[0][0]["kind"] == "derived_from"

        shot_result = client.shots.create(project=project_id, name="Shot", idempotency_key="shot")
        reference_result = client.references.create(project=project_id, reference_id="reference", kind="character", name="Reference", media_id=first["object_id"], idempotency_key="reference")
        assert shot_result.ok and reference_result.ok
        shot, reference = shot_result.data, reference_result.data
        assert client.shots.list(project_id).data[0][0]["shot_id"] == shot["shot_id"]
        assert client.references.list(project_id).data[0][0]["reference_id"] == reference["reference_id"]
        shown_shot = client.shots.show(project_id, shot["shot_id"])
        assert shown_shot.ok and shown_shot.data["shot_id"] == shot["shot_id"] and shown_shot.data["name"] == "Shot"
        shown_reference = client.references.show(project_id, reference["reference_id"])
        assert shown_reference.ok and shown_reference.data["reference_id"] == reference["reference_id"]
        assert shown_reference.data["name"] == "Reference"
        # A stale expected_version is the runtime's 409; Astrid must surface it as 'conflict'.
        stale = client.shots.update(project_id, shot["shot_id"], expected_version=shown_shot.data["version"] + 7, name="Stale", idempotency_key="shot-stale-update")
        assert stale.ok is False and stale.error.code == "conflict"
        assert client.shots.show(project_id, shot["shot_id"]).data["name"] == "Shot"
        assert client.shots.update(project_id, shot["shot_id"], name="Shot 2", idempotency_key="shot-update").ok
        assert client.shots.archive(project_id, shot["shot_id"], idempotency_key="shot-archive").ok
        assert client.shots.recover(project_id, shot["shot_id"], idempotency_key="shot-recover").ok
        assert client.references.update(project_id, reference["reference_id"], name="Reference 2").ok
        assert client.references.archive(project_id, reference["reference_id"], idempotency_key="reference-archive").ok
        assert client.references.recover(project_id, reference["reference_id"], idempotency_key="reference-recover").ok

        task = client.tasks.create(project_id=project_id, capability="render.basic", spec={"prompt": "editor"}, idempotency_key="task").data
        assert client.tasks.list(project_id).data[0][0]["task_id"] == task["task_id"]
        assert client.runs.list(project_id).data[0][0]["id"] == task["run_id"]
    finally:
        daemon.stop()


def test_operational_gateway_uses_typed_runtime_backup_and_lifecycle(tmp_path, monkeypatch, capsys, real_runtime):
    monkeypatch.setattr("astrid.core.rendering.remotion_runtime.remotion_install_problem", lambda _project: None)
    support = tmp_path / "support" / "runtime"
    daemon = real_runtime(tmp_path / "realm", support_root=support, owner_lock=support / "instance.lock")
    monkeypatch.setenv("ASTRID_RUNTIME_ENDPOINT", daemon.endpoint)
    monkeypatch.setenv("ASTRID_RUNTIME_CREDENTIAL", str(support / "credentials" / "owner.token"))
    monkeypatch.setenv("ASTRID_RUNTIME_DATA_ROOT", str((tmp_path / "support").resolve()))
    # The typed backup and lifecycle operations below exercise the real daemon.
    # Keep the doctor observer deterministic here; its separate subprocess
    # argv/deadline boundary is covered by the RuntimeCLI tests. Cold imports
    # here otherwise consume C2's fixed five-second observation budget.
    from astrid_runtime_client.contract_metadata import PROTOCOL, SCHEMA_DIGEST
    observer_calls = _fake_runtime_cli(
        monkeypatch,
        data={
            "healthy": True,
            "issues": [],
            "recovery_action": "No recovery action required.",
            "compatibility": {
                "protocol": PROTOCOL,
                "schema_digest": SCHEMA_DIGEST,
                "compatible": True,
            },
        },
    )
    _use_explicit_gateway_connection(monkeypatch, daemon)
    try:
        assert dispatch._dispatch_doctor(["--json"]) == 0
        doctor = json.loads(capsys.readouterr().out)
        assert doctor["healthy"] is True
        assert doctor["issues"] == []
        assert doctor["diagnostic"]["facts"]["runtime"]["value"] == "ready"
        assert [call[0] for call in observer_calls] == ["doctor"]

        backup_path = tmp_path / "backup"
        assert dispatch._dispatch_backup(["create", str(backup_path), "--json"]) == 0
        backup = json.loads(capsys.readouterr().out)
        assert backup["ok"] is True and "manifest" in backup and "cas_manifest" in backup

        client = _open_client(daemon)
        exported = client.export_realm()
        assert exported["format_version"] == 1 and "realm" in exported
        tombstoned = client.tombstone_realm(reason="acceptance")
        assert tombstoned["state"] == "tombstoned" and tombstoned["reason"] == "acceptance"
        realm_id = daemon.service.realm["id"]
        recovered = client.recover_realm(expected_realm_id=realm_id, expected_version=tombstoned["version"], confirmation=f"RECOVER {realm_id}")
        assert recovered["state"] == "active"

        restored_path = tmp_path / "restored"
        restored = client.restore_backup(str(backup_path), str(restored_path))
        assert restored["destination"] == str(restored_path)
        assert restored["verification"]["realm"]["id"] == backup["manifest"]["realm"]["id"]
    finally:
        daemon.stop()


def test_daemon_binds_loopback_only_and_keeps_credentials_private(tmp_path, monkeypatch, real_runtime):
    with pytest.raises(ValueError, match="runtime daemon only binds to loopback"):
        real_runtime(tmp_path / "public-realm", host="0.0.0.0")

    daemon = real_runtime()
    try:
        assert daemon.endpoint.startswith("http://127.0.0.1:")
        credentials = tmp_path / "support" / "credentials"
        assert stat.S_IMODE(credentials.stat().st_mode) == 0o700
        owner_token, worker_token = Path(daemon.credential_path), Path(daemon.worker_credential_path)
        assert owner_token == credentials / "owner.token" and worker_token.parent == credentials
        for token_file in (owner_token, worker_token):
            assert stat.S_IMODE(token_file.stat().st_mode) == 0o600
        owner_secret = owner_token.read_text(encoding="utf-8").strip()
        worker_secret = worker_token.read_text(encoding="utf-8").strip()
        assert owner_secret and worker_secret and owner_secret != worker_secret
        # Discovery names the credential files, never their bearer values.
        discovery_file = tmp_path / "support" / "discovery.json"
        assert stat.S_IMODE(discovery_file.stat().st_mode) == 0o600
        discovery = json.loads(discovery_file.read_text(encoding="utf-8"))
        assert discovery["endpoint"] == daemon.endpoint
        assert discovery["credential_file"] == str(owner_token)
        assert discovery["worker_credential_file"] == str(worker_token)
        assert owner_secret not in discovery_file.read_text(encoding="utf-8")
        assert worker_secret not in discovery_file.read_text(encoding="utf-8")
    finally:
        daemon.stop()


def test_empty_timeline_opens_as_composition_on_real_daemon(tmp_path, monkeypatch, real_runtime):
    daemon = real_runtime()
    try:
        client = _open_client(daemon)
        project_id = client.projects.create(slug="composition", name="Composition", idempotency_key="project").data["project_id"]
        created = client.timelines.create_empty(
            project=project_id,
            timeline_id="main-timeline",
            config={"tracks": []},
            idempotency_key="timeline",
        )
        assert created.ok
        assert created.data["timeline_id"] == "main-timeline"
        assert created.data["project_id"] == project_id
        revision_id = created.data["head_revision_id"]
        assert revision_id
        opened = client.timelines.open_composition(project_id, "main-timeline", revision_id=revision_id)
        assert opened.ok and isinstance(opened.data, dict)
        assert client.timelines.show(project_id, "main-timeline").ok
        listed = client.timelines.list(project_id)
        assert [item["timeline_id"] for item in listed.data[0]] == ["main-timeline"]
        missing = client.timelines.open_composition(project_id, "no-such-timeline", revision_id=revision_id)
        assert not missing.ok and missing.error.code == "not_found"
    finally:
        daemon.stop()


def test_promote_candidate_receipt_replay_and_foreign_provenance_rejection(tmp_path, monkeypatch, real_runtime):
    """Astrid's recipe reaches the runtime-owned atomic promotion; 422 surfaces as validation_error."""
    from astrid.packs.generation.actions.generate_image.task_adapter import (
        validate_shot_generation_recipe,
    )

    daemon = real_runtime()
    try:
        astrid = _open_client(daemon)
        project_id = astrid.projects.create(slug="promotion", name="Promotion", idempotency_key="project").data["project_id"]
        shot_id = "shot-generation-bridge"
        assert astrid.shots.create(project=project_id, shot={"shot_id": shot_id, "name": "Generation Bridge"}, idempotency_key="shot").ok

        def ingest(name: str, content: bytes) -> str:
            path = tmp_path / name
            path.write_bytes(content)
            result = astrid.media.import_file(project=project_id, path=path, idempotency_key=f"media-{name.replace('.', '-')}")
            assert result.ok
            return result.data["object_id"]

        def bare(media_id: str) -> str:
            return media_id.removeprefix("sha256:")

        old_media = ingest("old.png", b"old-primary")
        candidate_media = ingest("candidate.png", b"generated-candidate")
        plate_media = ingest("plate.png", b"derived-plate")
        proxy_media = ingest("proxy.png", b"derived-proxy")
        prompt_media = ingest("prompt.txt", b"a precise generated candidate")
        reference_media = ingest("reference.png", b"reference-input")
        recipe = {
            "schema": "astrid.shot-generation-recipe/v1",
            "project_id": project_id,
            "shot_id": shot_id,
            "target_role": "primary_visual",
            "prompt_binding": {"id": "binding-generation-bridge", "head": 1, "media_id": prompt_media, "content_sha256": bare(prompt_media)},
            "generator": {"capability_id": "generation.generate_image", "model": "z-image", "backend": "cloud", "mode": "t2i", "settings": {"seed": 42}},
            "inputs": [{"ordinal": 0, "role": "style", "reference_id": "reference-generation-bridge", "media_id": reference_media, "content_sha256": bare(reference_media)}],
            "parent_media_id": old_media,
            "parent_content_sha256": bare(old_media),
        }
        assert validate_shot_generation_recipe(
            recipe, project_id=project_id, model="z-image", mode="t2i", execution="cloud", resolved_settings={"seed": 42},
        ) == recipe

        def add_item(media_id: str, metadata: dict, key: str):
            added = astrid.shots.add_item(project_id, shot_id, media_id=media_id, metadata=metadata, idempotency_key=key)
            assert added.ok
            return added, next(item for item in added.data["items"] if item["media_id"] == bare(media_id))

        _primary, primary_item = add_item(old_media, {"role": "primary_visual", "status": "primary"}, "primary")
        _plate, plate_item = add_item(plate_media, {
            "kind": "plate", "source_item_id": primary_item["item_id"],
            "source_media_id": old_media, "source_content_sha256": bare(old_media),
        }, "plate")
        _proxy, proxy_item = add_item(proxy_media, {
            "kind": "proxy", "source_item_id": plate_item["item_id"],
            "source_media_id": plate_media, "source_content_sha256": bare(plate_media),
        }, "proxy")
        candidate, candidate_item = add_item(candidate_media, {
            "role": "primary_visual", "status": "candidate", "content_sha256": bare(candidate_media), "recipe": recipe,
            "provenance": {"project_id": project_id, "shot_id": shot_id, "target_role": "primary_visual", "media_id": candidate_media},
        }, "candidate")
        timeline_assets = [{
            "id": "timeline-generation-bridge",
            "metadata": {"source_item_id": primary_item["item_id"], "source_media_id": old_media, "source_content_sha256": bare(old_media)},
        }]

        def promote():
            return astrid.shots.promote_candidate(
                project_id, shot_id, candidate_item["item_id"],
                expected_head_seq=candidate.data["version"], timeline_assets=timeline_assets,
                idempotency_key="promotion-command",
            )

        promoted = promote()
        assert promoted.ok and promoted.receipt is not None
        assert promoted.receipt.command_kind == "shot.promote_candidate"
        assert promoted.receipt.result == promoted.data
        assert promoted.data["promotion"]["primary_item_id"] == candidate_item["item_id"]
        stale = {entry.get("item_id") or entry.get("asset_id") for entry in promoted.data["invalidation"]["stale"]}
        assert stale >= {plate_item["item_id"], proxy_item["item_id"], "timeline-generation-bridge"}

        replay = promote()
        assert replay.ok and replay.data == promoted.data
        assert replay.receipt is not None and replay.receipt.to_json() == promoted.receipt.to_json()
        primary_items = [
            item for item in astrid.shots.show(project_id, shot_id).data["items"]
            if item["metadata"].get("role") == "primary_visual"
        ]
        assert sum(item["metadata"].get("status") == "primary" for item in primary_items) == 1
        assert any(
            item["item_id"] == primary_item["item_id"] and item["metadata"].get("status") == "superseded"
            for item in primary_items
        )

        foreign_media = ingest("foreign.png", b"foreign-provenance-candidate")
        foreign, foreign_item = add_item(foreign_media, {
            "role": "primary_visual", "status": "candidate",
            "provenance": {"project_id": "foreign-project", "shot_id": shot_id, "target_role": "primary_visual", "media_id": foreign_media},
        }, "foreign-candidate")
        rejected = astrid.shots.promote_candidate(
            project_id, shot_id, foreign_item["item_id"],
            expected_head_seq=foreign.data["version"], idempotency_key="foreign-command",
        )
        assert not rejected.ok and rejected.error.code == "validation_error"
        unchanged = astrid.shots.show(project_id, shot_id)
        assert unchanged.data["version"] == foreign.data["version"]
        assert next(item for item in unchanged.data["items"] if item["item_id"] == foreign_item["item_id"])["metadata"]["status"] == "candidate"
    finally:
        daemon.stop()


def _write_cpu_executor(pack: Path, *, capability_id: str, argv: list[str], outputs: list[dict]) -> None:
    pack.mkdir()
    (pack / "executor.yaml").write_text(
        json.dumps({
            "schema_version": 1,
            "id": capability_id,
            "name": capability_id,
            "kind": "external",
            "version": "1.0",
            "command": {"argv": argv},
            "outputs": outputs,
            "metadata": {"adapter_family": "cpu", "resource_keys": ["cpu"]},
        }),
        encoding="utf-8",
    )


def test_generic_host_cpu_route_claims_settles_and_fences_stale_attempt_across_restart(tmp_path, monkeypatch, real_runtime):
    """Shared generic-host route: discover, register, claim, subprocess, CAS settle, restart, stale fence."""
    from astrid.core.execution.generic_host import GenericPackHost, RuntimeProtocolClient

    pack = tmp_path / "cpu-pack"
    _write_cpu_executor(
        pack,
        capability_id="acceptance.cold_echo",
        argv=["{python_exec}", "-c", "from pathlib import Path; Path('{out}/answer.txt').write_text('cold worker')"],
        outputs=[{"name": "answer", "type": "file", "path_template": "{out}/answer.txt", "artifact_type": "text/plain"}],
    )
    support = tmp_path / "support"
    daemon = real_runtime(tmp_path / "realm", support_root=support)
    try:
        client = _open_client(daemon)
        project_id = client.projects.create(slug="cpu-route", name="CPU Route", idempotency_key="project").data["project_id"]
        seed = tmp_path / "seed.txt"
        seed.write_text("cold input", encoding="utf-8")
        seed_id = client.media.import_file(project=project_id, path=seed, idempotency_key="seed").data["object_id"]

        host = GenericPackHost(
            pack_roots=[pack],
            client=RuntimeProtocolClient(daemon.endpoint, daemon.worker_token),
            executor_id="astrid-pack-host",
            attempt_root=tmp_path / "attempt",
        )
        record = host.discover()[0]
        assert record.id == "acceptance.cold_echo"
        assert host.preflight(record.id)[0].ready is True
        assert host.register(deliberate=True)["registration"].executor_id == "astrid-pack-host"
        generated = WorkspaceClient(daemon.endpoint, daemon.token)
        registered = {item.capability_id: item for item in generated.list_capabilities()[0]}
        assert registered[record.id].definition_digest == record.capability_digest

        admitted = client.tasks.create(project_id=project_id, capability=record.id, spec={}, input_manifest=[seed_id], idempotency_key="task")
        assert admitted.ok
        settled = host.run(once=True)
        assert len(settled) == 1 and settled[0].state == "succeeded"
        assert client.tasks.show(admitted.data["task_id"]).data["state"] == "succeeded"
        assert generated.get_run(admitted.data["run_id"])["task_ids"] == [admitted.data["task_id"]]
        event_page, event_cursor = generated.list_run_events(admitted.data["run_id"])
        assert event_cursor is None
        assert {event.event_type for event in event_page} >= {"task.admitted", "task.completed"}
        output_digest = "sha256:" + hashlib.sha256(b"cold worker").hexdigest()
        assert host.client.generated.get_object(output_digest).data == b"cold worker"

        # Cancel and retry travel the same admission route with exact states.
        probe = client.tasks.create(project_id=project_id, capability=record.id, spec={}, idempotency_key="cancel-probe")
        probe_id = probe.data["task_id"]
        assert client.tasks.cancel(probe_id, idempotency_key="cancel").data["state"] == "cancelled"
        assert client.tasks.retry(probe_id, idempotency_key="retry").data["state"] == "queued"
        assert client.tasks.cancel(probe_id, idempotency_key="cancel-again").data["state"] == "cancelled"

        # Claim without settling, then restart the real daemon: the old attempt is
        # fenced by the new runtime epoch and a fresh host resumes the work.
        recovery = client.tasks.create(project_id=project_id, capability=record.id, spec={}, input_manifest=[seed_id], idempotency_key="recovery")
        assert recovery.ok
        old_epoch = int(host.client.health()["runtime_epoch"])
        claim = host.client.claim_next(executor_id="astrid-pack-host", capability_ids=[record.id], idempotency_key="recovery-claim")
        assert claim is not None and claim.task_id == recovery.data["task_id"]

        daemon.stop()
        daemon = real_runtime(tmp_path / "realm", support_root=support)
        restarted = WorkspaceClient(daemon.endpoint, daemon.worker_token)
        assert int(restarted.health()["runtime_epoch"]) > old_epoch
        with pytest.raises(ApiError) as fenced:
            restarted.settle_attempt(
                claim.attempt_id,
                {"lease_id": claim.lease_id, "fence": claim.fence, "runtime_epoch": old_epoch, "outputs": []},
                idempotency_key="stale-settlement",
            )
        assert (fenced.value.status, fenced.value.code) == (409, "lease_fenced")
        assert restarted.get_task(recovery.data["task_id"]).state != "succeeded"

        resumed_host = GenericPackHost(
            pack_roots=[pack],
            client=RuntimeProtocolClient(daemon.endpoint, daemon.worker_token),
            executor_id="astrid-pack-host",
            attempt_root=tmp_path / "attempt-restarted",
        )
        resumed_host.discover()
        resumed_host.register(deliberate=True)
        resumed = resumed_host.run(once=True)
        assert len(resumed) == 1 and resumed[0].state == "succeeded"
        assert restarted.get_task(recovery.data["task_id"]).state == "succeeded"
        assert restarted.get_object(output_digest).data == b"cold worker"
    finally:
        daemon.stop()


def test_generic_host_render_route_runs_real_ffmpeg_and_settles_boot_manifest_provenance(tmp_path, monkeypatch, real_runtime):
    """Render-family route on a real daemon: real FFmpeg child, CAS-published outputs, stamped provenance."""
    from astrid.core._shared.boot_manifest import load_boot_manifest_hash
    from astrid.core.execution.generic_host import GenericPackHost, RuntimeProtocolClient
    from astrid.core.gateway.dispatch import compose_profile_handoff

    ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
    assert ffmpeg and ffprobe, "the render-family proof requires ffmpeg and ffprobe"
    render_code = (
        "from pathlib import Path; import hashlib, json, subprocess; "
        "out=Path('{out}'); video=out/'render.mp4'; "
        f"subprocess.run([{ffmpeg!r}, '-hide_banner', '-loglevel', 'error', '-y', '-f', 'lavfi', '-i', 'color=c=blue:s=160x90:r=10:d=1', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(video)], check=True); "
        "provenance=out/'render.mp4.provenance.json'; "
        "provenance.write_text(json.dumps({'renderer':'ffmpeg','sha256':hashlib.sha256(video.read_bytes()).hexdigest()})); "
        "(out/'manifest.json').write_text(json.dumps({'outputs':["
        "{'name':'render','path':'render.mp4','artifact_type':'video/mp4','content_hash':'sha256:'+hashlib.sha256(video.read_bytes()).hexdigest(),'bytes':video.stat().st_size,'ordinal':0,'role':'result','is_primary':True},"
        "{'name':'provenance','path':'render.mp4.provenance.json','artifact_type':'application/json','content_hash':'sha256:'+hashlib.sha256(provenance.read_bytes()).hexdigest(),'bytes':provenance.stat().st_size,'ordinal':1,'role':'auxiliary','is_primary':False}"
        "]}, sort_keys=True))"
    )
    pack = tmp_path / "ffmpeg-pack"
    _write_cpu_executor(
        pack,
        capability_id="acceptance.cold_ffmpeg",
        argv=[sys.executable, "-c", render_code],
        outputs=[
            {"name": "render", "type": "file", "path_template": "{out}/render.mp4", "artifact_type": "video/mp4"},
            {"name": "provenance", "type": "file", "path_template": "{out}/render.mp4.provenance.json", "artifact_type": "application/json"},
        ],
    )
    support = tmp_path / "support"
    daemon = real_runtime(tmp_path / "realm", support_root=support)
    try:
        client = _open_client(daemon)
        project_id = client.projects.create(slug="render-route", name="Render Route", idempotency_key="project").data["project_id"]
        boot_manifest = support / "astrid-host" / "boot-manifest.json"
        compose_profile_handoff(boot_manifest, support_root=support)
        boot_hash = load_boot_manifest_hash(boot_manifest, support_root=support)
        attempt_root = tmp_path / "render-attempt"
        host = GenericPackHost(
            pack_roots=[pack],
            client=RuntimeProtocolClient(daemon.endpoint, daemon.worker_token),
            executor_id="astrid-pack-host",
            attempt_root=attempt_root,
            boot_manifest_path=boot_manifest,
            boot_manifest_hash=boot_hash,
        )
        record = host.discover()[0]
        assert host.preflight(record.id)[0].ready is True
        host.register()
        admitted = client.tasks.create(project_id=project_id, capability=record.id, spec={}, idempotency_key="render-task")
        assert admitted.ok
        results = host.run(once=True)
        assert len(results) == 1 and results[0].state == "succeeded"

        video = next(attempt_root.rglob("render.mp4"))
        provenance = next(attempt_root.rglob("render.mp4.provenance.json"))
        probe = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries", "format=format_name", "-of", "default=nw=1:nk=1", str(video)],
            check=True, capture_output=True, text=True,
        )
        assert "mp4" in probe.stdout
        generated = WorkspaceClient(daemon.endpoint, daemon.token)
        for artifact in (video, provenance):
            digest = "sha256:" + hashlib.sha256(artifact.read_bytes()).hexdigest()
            assert generated.get_object(digest).data == artifact.read_bytes()
        assert json.loads(provenance.read_text(encoding="utf-8"))["renderer"] == "ffmpeg"

        task = generated.get_task(admitted.data["task_id"])
        assert task.state == "succeeded"
        completion = task.result
        assert completion["adapter_family"] == "cpu"
        assert completion["capability_digest"] == record.capability_digest
        assert completion["source_digest"] == record.source_digest
        assert completion["dependency_digest"] == record.dependency_digest
        assert completion["provenance"] == {"kind": "astrid.boot_manifest", "sha256": boot_hash}
        evidence = completion["process_evidence"]
        assert evidence["child_boundary"] == "subprocess" and evidence["returncode"] == 0 and evidence["attempt_id"]
    finally:
        daemon.stop()


def test_shot_items_and_reference_associations_use_versioned_routes_and_survive_restart(tmp_path, monkeypatch, real_runtime):
    support = tmp_path / "support"
    daemon = real_runtime(tmp_path / "realm", support_root=support)
    try:
        astrid = _open_client(daemon)
        project_id = astrid.projects.create(slug="items", name="Items", idempotency_key="project").data["project_id"]
        other_id = astrid.projects.create(slug="items-other", name="Items Other", idempotency_key="other").data["project_id"]

        def ingest(name: str, content: bytes) -> str:
            path = tmp_path / name
            path.write_bytes(content)
            result = astrid.media.import_file(project=project_id, path=path, idempotency_key=f"media-{name}")
            assert result.ok
            return result.data["object_id"]

        first_media, second_media = ingest("first.bin", b"items-media"), ingest("second.bin", b"items-media-2")
        shot_args = {"project": project_id, "shot": {"shot_id": "item-shot", "name": "Item Shot"}, "idempotency_key": "shot"}
        created = astrid.shots.create(**shot_args)
        reference = astrid.references.create(project=project_id, reference_id="item-ref", kind="character", name="Item", media_id=first_media, idempotency_key="reference")
        assert created.ok and reference.ok
        # Replaying the same idempotency key returns the original result.
        replayed = astrid.shots.create(**shot_args)
        assert replayed.ok and replayed.data == created.data
        assert [item["shot_id"] for item in astrid.shots.list(project_id).data[0]] == ["item-shot"]
        assert astrid.shots.list(other_id).data[0] == []

        added_first = astrid.shots.add_item(project_id, "item-shot", media_id=first_media, position=0, idempotency_key="item-1")
        added_second = astrid.shots.add_item(project_id, "item-shot", media_id=second_media, position=1, idempotency_key="item-2")
        assert added_first.ok and added_second.ok
        item_ids = [item["item_id"] for item in added_second.data["items"]]
        assert len(item_ids) == 2
        reordered = astrid.shots.reorder(project_id, "item-shot", item_ids=list(reversed(item_ids)), expected_version=added_second.data["version"], idempotency_key="reorder")
        assert reordered.ok and [item["item_id"] for item in reordered.data["items"]] == list(reversed(item_ids))
        removed = astrid.shots.remove_item(project_id, "item-shot", item_ids[0], expected_version=reordered.data["version"], idempotency_key="remove")
        assert removed.ok and [item["item_id"] for item in removed.data["items"]] == [item_ids[1]]

        stale = astrid.shots.update(project_id, "item-shot", expected_version=1, name="Stale", idempotency_key="stale-update")
        assert stale.ok is False and stale.error.code == "conflict"
        updated = astrid.shots.update(project_id, "item-shot", expected_version=removed.data["version"], name="Item Updated", idempotency_key="update")
        assert updated.ok and updated.data["name"] == "Item Updated"
        archived = astrid.shots.archive(project_id, "item-shot", expected_version=updated.data["version"], idempotency_key="archive")
        recovered = astrid.shots.recover(project_id, "item-shot", expected_version=archived.data["version"], idempotency_key="recover")
        assert archived.ok and archived.data["archived"] is True
        assert recovered.ok and recovered.data["archived"] is False

        association = astrid.references.associate(project_id, "item-ref", media_id=second_media, role="depicts", idempotency_key="associate")
        assert association.ok
        association_id = association.data["media_references"][-1]["association_id"]
        secondary = astrid.references.create(project=project_id, reference_id="item-ref-2", kind="object", name="Prop", media_id=first_media, idempotency_key="reference-2")
        primary = astrid.references.set_primary(project_id, "item-ref", association_id=association_id, expected_version=association.data["version"], idempotency_key="primary")
        link = astrid.references.link(project=project_id, from_reference_id="item-ref", to_reference_id="item-ref-2", kind="associated_with", idempotency_key="link")
        assert secondary.ok and primary.ok and link.ok

        daemon.stop()
        daemon = real_runtime(tmp_path / "realm", support_root=support)
        reopened = _open_client(daemon)
        shown = reopened.shots.show(project_id, "item-shot")
        assert shown.ok and shown.data["name"] == "Item Updated" and shown.data["archived"] is False
        assert [item["item_id"] for item in shown.data["items"]] == [item_ids[1]]
        assert reopened.references.show(project_id, "item-ref").ok
    finally:
        daemon.stop()
