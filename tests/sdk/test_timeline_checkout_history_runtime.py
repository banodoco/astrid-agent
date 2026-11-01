"""Public checkout history against the Runtime paired with the generated client."""
from __future__ import annotations

import copy
import hashlib
import sys
import tarfile
from dataclasses import dataclass
from pathlib import Path

import pytest

from tests.editing_conformance.test_commit_runtime_m1 import (
    assert_readback,
    cli,
    open_client,
    parent_of,
    renderer_off,
)
from tests.helpers.runtime import use_runtime

pytestmark = pytest.mark.integration
# Keep this bounded real-HTTP qualification on the reviewed current Runtime
# reader; do not switch either side of the public handshake to a fake route.
CATALOG_RUNTIME = "d9e2a71e87c827290a3ff58a94f6fb8b4bc11e0b"
RUNTIME = use_runtime(CATALOG_RUNTIME)
import runtime_protocol  # noqa: E402, F401, I001  # lock the daemon fixture to CATALOG_RUNTIME


AUTHENTIC_ARCHIVE = Path(
    "/home/user/timeline-hub/hub/design/"
    "g5-sdk-authentic-capture-20261023-b6069d1d.tar.gz"
)
AUTHENTIC_ARCHIVE_SHA256 = "2915bd4ac0d46954d19c30f87a517b82074218699978e650fab03fcba81b72d3"
AUTHENTIC_BACKUP_KEY = bytes.fromhex(
    "00112233445566778899aabbccddeeff"
    "ffeeddccbbaa99887766554433221100"
)
AUTHENTIC_PARENT_REVISION = "a3e671256f1c42b8837d3e118c97b21b"
AUTHENTIC_PARENT_PUBLICATION = "txn-11c90075be6c4c8bb9e2627366f0fe4f"
AUTHENTIC_PARENT_DIGEST = "sha256:b6069d1ddccc0279e871b142773b63b73b107ebf9d0adc11ab73832b69097afb"


@dataclass(frozen=True)
class AuthenticFilmFixture:
    duplicate_root: Path
    project_id: str
    timeline_id: str
    parent_revision_id: str


def _extract_authentic_archive(destination: Path) -> Path:
    destination.mkdir()
    with tarfile.open(AUTHENTIC_ARCHIVE, "r:gz") as archive:
        members = archive.getmembers()
        names = {member.name.rstrip("/") for member in members}
        expected = {"backup/manifest.json", "backup/cas-manifest.json", "backup/realm.sqlite3"}
        if not expected.issubset(names) or any(
            member.name != member.name.lstrip("/")
            or ".." in Path(member.name).parts
            or member.issym()
            or member.islnk()
            or not (member.isdir() or member.isfile())
            for member in members
        ):
            raise AssertionError("authentic G5 archive has an unsafe or incomplete layout")
        for member in members:
            if sys.version_info >= (3, 12):
                archive.extract(member, destination, filter="data")
            else:
                archive.extract(member, destination)
    return destination / "backup"


@pytest.fixture
def film(tmp_path):
    from runtime_protocol.backup import restore_backup, verify_backup
    from runtime_protocol.service import RuntimeService
    from runtime_protocol.timeline_commit_baseline import verify_baseline

    if not AUTHENTIC_ARCHIVE.is_file():
        raise AssertionError(f"missing qualified authentic G5 archive: {AUTHENTIC_ARCHIVE}")
    if hashlib.sha256(AUTHENTIC_ARCHIVE.read_bytes()).hexdigest() != AUTHENTIC_ARCHIVE_SHA256:
        raise AssertionError("qualified authentic G5 archive bytes changed")

    backup = _extract_authentic_archive(tmp_path / "authentic-archive")
    verified = verify_backup(backup, key=AUTHENTIC_BACKUP_KEY)
    restored_root = tmp_path / "authentic-realm"
    restore_backup(backup, restored_root, key=AUTHENTIC_BACKUP_KEY)

    service = RuntimeService(restored_root)
    try:
        projects = service.list_projects()["items"]
        assert len(projects) == 1
        project_id = projects[0]["project_id"]
        timelines = service.list_timelines(project_id)["items"]
        assert len(timelines) == 1
        timeline = timelines[0]
        timeline_id = timeline["timeline_id"]
        revision_id = timeline["head_revision_id"]
        parent = service.get_project_parent_composition_revision(project_id, timeline_id, revision_id)
        baseline = verify_baseline(
            service.store.conn,
            project_id,
            timeline_id,
            read_object=service.cas.read,
        )
    finally:
        service.close()

    assert verified["manifest"]["schema"]["version"] == 31
    assert revision_id == AUTHENTIC_PARENT_REVISION
    assert baseline["head"] == {
        "revision_id": AUTHENTIC_PARENT_REVISION,
        "publication_id": AUTHENTIC_PARENT_PUBLICATION,
    }
    assert parent["content_digest"] == AUTHENTIC_PARENT_DIGEST
    return (
        AuthenticFilmFixture(restored_root, project_id, timeline_id, revision_id),
        copy.deepcopy(parent["payload"]),
        {
            "data": {"accepted_revision_id": baseline["head"]["revision_id"]},
            "receipt": {"receipt_id": baseline["head"]["publication_id"]},
        },
    )


def test_checkout_reload_undo_cli_redo_uses_retained_history(
    tmp_path, monkeypatch, capsys, film, renderer_off, real_runtime,
):
    from astrid.sdk.timeline_checkout import Checkout
    from astrid.sdk.timeline.sync import TimelineSession
    from astrid.sdk.timeline_sheet import find_version, sheet_version

    fixture, original_parent, _ = film
    monkeypatch.setenv('ASTRID_RUNTIME_DATA_ROOT', str(tmp_path / 'client-data'))
    daemon = real_runtime(fixture.duplicate_root, support_root=tmp_path / 'support', authoring_profile='astrid')
    with open_client(daemon) as client:
        original = Checkout.open(fixture.project_id, fixture.timeline_id, client=client)
        version = sheet_version(original)
        session = TimelineSession.open(fixture.project_id, fixture.timeline_id, client=client)
        accepted = session.move('parent/clip:music-bed', 2)
        disjoint = session.move('parent/clip:music-other', 3)
        reopened = Checkout.open(fixture.project_id, fixture.timeline_id, client=client)
        # No local history exists: an older sheet's exact basis comes from the
        # Runtime's retained revision path, including its original source pins.
        restored_basis = find_version(reopened, version, client=client)
        assert restored_basis is not None
        assert restored_basis.document() == original.document()
        undone = reopened.undo(publications=[accepted['receipt']['receipt_id']], client=client)
        expected = copy.deepcopy(parent_of(disjoint))
        expected['clips'][0] = copy.deepcopy(original_parent['clips'][0])
        expected['config']['clips'] = copy.deepcopy(expected['clips'])
        assert parent_of(undone) == expected
        assert_readback(client, fixture, undone)
        redone = cli(client, fixture, capsys, 'redo', fixture.timeline_id,
                     '--commit', undone['receipt']['receipt_id'])
        assert parent_of(redone) == parent_of(disjoint)
        assert_readback(client, fixture, redone)
        assert Checkout.open(fixture.project_id, fixture.timeline_id, client=client).base_revision == redone['data']['accepted_revision_id']


def test_open_bundle_keeps_http_rows_raw_and_round_trips_canonical_images(
    tmp_path, monkeypatch, film, real_runtime,
):
    from astrid.core.timeline.authoring_bundle import open_authoring_bundle
    from astrid.sdk.timeline.commit import (
        INTERNAL_IMAGE_FIELDS,
        PARENT_IMAGE_FIELDS,
        SHOT_IMAGE_FIELDS,
    )

    fixture, _, _ = film
    daemon = real_runtime(
        fixture.duplicate_root,
        support_root=tmp_path / "support",
        authoring_profile="astrid",
    )
    with open_client(daemon) as client:
        transport = client._remote._transport
        revision = client.timelines.history(
            fixture.project_id, fixture.timeline_id, limit=1,
        ).data["head"]["revision_id"]
        parent = transport.get_project_parent_composition_revision(
            fixture.project_id, fixture.timeline_id, revision,
        )
        shots = {}
        internals = {}
        for occurrence in parent["payload"]["occurrences"]:
            shot_revision = occurrence["shot_revision_id"]
            shot = transport.get_project_shot_revision(
                fixture.project_id, occurrence["shot_id"], shot_revision,
            )
            shots[shot_revision] = shot
            internal_revision = shot["internal_timeline_revision_id"]
            internals[internal_revision] = transport.get_project_timeline_revision(
                fixture.project_id, fixture.timeline_id, internal_revision,
            )
        raw_rows = (copy.deepcopy(parent), copy.deepcopy(shots), copy.deepcopy(internals))

        opened = client.timelines.open_bundle(
            fixture.project_id, fixture.timeline_id, revision_id=revision,
        )
        assert opened.ok, opened.as_dict()
        expected = open_authoring_bundle(
            {
                **{key: parent[key] for key in PARENT_IMAGE_FIELDS},
                "project_id": fixture.project_id,
                "timeline_id": fixture.timeline_id,
            },
            shot_revisions=[
                {
                    **{key: row[key] for key in SHOT_IMAGE_FIELDS},
                    "project_id": row.get("project_id", fixture.project_id),
                }
                for row in shots.values()
            ],
            internal_timeline_revisions=[
                {
                    **{key: row[key] for key in INTERNAL_IMAGE_FIELDS},
                    "project_id": row.get("project_id", fixture.project_id),
                }
                for row in internals.values()
            ],
        )
        assert opened.data["bundle"] == expected

        assert transport.get_project_parent_composition_revision(
            fixture.project_id, fixture.timeline_id, revision,
        ) == raw_rows[0]
        assert {
            revision: transport.get_project_shot_revision(
                fixture.project_id, row["shot_id"], revision,
            )
            for revision, row in shots.items()
        } == raw_rows[1]
        assert {
            revision: transport.get_project_timeline_revision(
                fixture.project_id, row["timeline_id"], revision,
            )
            for revision, row in internals.items()
        } == raw_rows[2]


def test_retained_restore_does_not_reopen_consumed_draft_steps(
    tmp_path, monkeypatch, film, renderer_off, real_runtime,
):
    from astrid.sdk.timeline import store
    from astrid.sdk.timeline._common import TimelineEditError
    from astrid.sdk.timeline.sync import TimelineSession
    from astrid.sdk.timeline_checkout import Checkout

    fixture, _, _ = film
    monkeypatch.setenv('ASTRID_RUNTIME_DATA_ROOT', str(tmp_path / 'client-data'))
    daemon = real_runtime(fixture.duplicate_root, support_root=tmp_path / 'support', authoring_profile='astrid')
    with open_client(daemon) as client:
        draft = Checkout.draft(fixture.project_id, fixture.timeline_id, client=client)
        original = copy.deepcopy(draft.bundle)

        def local_move(on):
            with draft.step('move music'):
                draft.bundle['parent']['clips'][0]['at'] = on
                draft.bundle['parent']['config']['clips'] = copy.deepcopy(draft.bundle['parent']['clips'])
            draft.save(quiet=True)

        local_move(1)
        local_move(2)
        assert len(list(store._history_dir(draft.path).glob('*.json'))) == 2
        session = TimelineSession.open(fixture.project_id, fixture.timeline_id, client=client)
        accepted = session.move('parent/clip:music-bed', 2)
        draft._reset_base(client=client)
        assert draft.base_revision == accepted['data']['accepted_revision_id']
        restore_session = TimelineSession.open(fixture.project_id, fixture.timeline_id, client=client)
        restored = restore_session.restore(
            original['base_parent']['revision_id'],
            original['base_parent']['content_digest'],
            idempotency_key='restore-retained-draft-base',
        )
        assert restored['data']['accepted_revision_id'] == original['base_parent']['revision_id']
        assert restored['receipt']['receipt_id'] != accepted['receipt']['receipt_id']
        draft._reset_base(client=client)
        draft = Checkout.load(draft.path)
        new_publication = restored['receipt']['receipt_id']
        assert new_publication != original['base_parent']['publication_id']
        expected = copy.deepcopy(original)
        expected['base_parent']['publication_id'] = new_publication
        assert draft.bundle['base_parent'] == expected['base_parent']
        assert draft.document() == Checkout(expected).document()
        assert draft.bundle['source_mapping']['_runtime_document'] == (
            original['source_mapping']['_runtime_document']
        )
        for operation in (draft.undo, draft.redo):
            with pytest.raises(TimelineEditError, match='no unsent draft edit'):
                operation()
        base = draft.document()
        local_move(3)
        edited = draft.document()
        assert len(draft.undo(100)) == 1
        assert draft.document() == base
        draft.redo()
        assert draft.document() == edited


def test_catalog_recover_uses_real_http_and_preserves_closure_before_revision_restore(
    tmp_path, monkeypatch, film, real_runtime,
):
    """Catalog visibility recovery leaves accepted film state untouched.

    The direct recover operation is deliberately exercised through the normal
    WorkspaceClient transport.  Historical film recovery remains the one
    explicit TimelineSession restore path.
    """
    from astrid.sdk.timeline._common import TimelineEditError
    from astrid.sdk.timeline.sync import TimelineSession
    from astrid.sdk.workspace_client import WorkspaceClientError

    fixture, _, _ = film
    monkeypatch.setenv("ASTRID_RUNTIME_DATA_ROOT", str(tmp_path / "client-data"))
    daemon = real_runtime(
        fixture.duplicate_root,
        support_root=tmp_path / "support",
        authoring_profile="astrid",
        commit=CATALOG_RUNTIME,
    )
    client = open_client(daemon)
    transport = client._remote._transport

    def snapshot():
        timeline = transport.get_timeline(
            fixture.timeline_id, project_id=fixture.project_id,
        )
        revision = timeline["head_revision_id"]
        parent = transport.get_project_parent_composition_revision(
            fixture.project_id, fixture.timeline_id, revision,
        )
        manifest = parent.get("dependency_manifest", {})
        shots = {
            row["revision_id"]: transport.get_project_shot_revision(
                fixture.project_id, row["shot_id"], row["revision_id"],
            )
            for row in manifest.get("shots", [])
        }
        internal = {
            row["revision_id"]: transport.get_project_timeline_revision(
                fixture.project_id, fixture.timeline_id, row["revision_id"],
            )
            for row in manifest.get("internal_timelines", [])
        }
        media = {
            row["media_id"]: {
                key: transport.head_object(row["media_id"])["headers"].get(key)
                for key in ("ETag", "Content-Length", "Content-Type")
            }
            for row in manifest.get("media", [])
        }
        return {
            "timeline": timeline,
            "parent": parent,
            "shots": shots,
            "internal": internal,
            "media": media,
        }

    def assert_unchanged(actual):
        assert {
            key: value for key, value in actual["timeline"].items() if key != "version"
        } == {
            key: value for key, value in preserved["timeline"].items() if key != "version"
        }
        for key in ("parent", "shots", "internal", "media"):
            assert actual[key] == preserved[key]

    try:
        # Create one ordinary accepted publication so both canonical head
        # identities are populated, then retain its exact historical basis.
        session = TimelineSession.open(
            fixture.project_id, fixture.timeline_id,
            client=client, outbox_path=tmp_path / "edit-outbox.json",
        )
        before_revision = client.timelines.history(
            fixture.project_id, fixture.timeline_id, limit=1,
        ).data["head"]["revision_id"]
        before_parent = transport.get_project_parent_composition_revision(
            fixture.project_id, fixture.timeline_id, before_revision,
        )
        accepted = session.move("parent/clip:music-bed", 1, idempotency_key="recover-basis")
        assert accepted["data"]["accepted_revision_id"] != before_revision
        preserved = snapshot()
        accepted_history = client.timelines.history(
            fixture.project_id, fixture.timeline_id, limit=100,
        )
        assert accepted_history.ok, accepted_history.as_dict()

        # The producer-facing no-version call resolves the current catalog
        # version, while the explicit call sends that same version over HTTP.
        archived = client.timelines.archive(
            fixture.project_id, fixture.timeline_id, idempotency_key="catalog-archive-1",
        )
        assert archived.ok, archived.as_dict()
        archived_data = archived.data
        assert archived_data["archived"] is True
        assert archived_data["head_revision_id"] == preserved["timeline"]["head_revision_id"]
        assert archived_data["head_publication_id"] == preserved["timeline"]["head_publication_id"]

        recovered_absent = client.timelines.recover(
            fixture.project_id, fixture.timeline_id, idempotency_key="catalog-recover-absent",
        )
        assert recovered_absent.ok, recovered_absent.as_dict()
        assert recovered_absent.data["archived"] is False
        assert_unchanged(snapshot())
        assert client.timelines.history(
            fixture.project_id, fixture.timeline_id, limit=100,
        ).data == accepted_history.data

        # Repeat the catalog lifecycle through the explicit current version;
        # the exact archive/recover receipts remain replayable.
        archive_again_version = recovered_absent.data["version"]
        archived_again = client.timelines.archive(
            fixture.project_id, fixture.timeline_id,
            expected_version=archive_again_version,
            idempotency_key="catalog-archive-2",
        )
        assert archived_again.ok, archived_again.as_dict()
        recovered_current = client.timelines.recover(
            fixture.project_id, fixture.timeline_id,
            expected_version=archived_again.data["version"],
            version=archived_again.data["version"],
            idempotency_key="catalog-recover-current",
        )
        assert recovered_current.ok, recovered_current.as_dict()
        assert_unchanged(snapshot())
        assert client.timelines.history(
            fixture.project_id, fixture.timeline_id, limit=100,
        ).data == accepted_history.data
        assert client.timelines.archive(
            fixture.project_id, fixture.timeline_id,
            expected_version=archive_again_version,
            idempotency_key="catalog-archive-2",
        ).data == archived_again.data
        assert client.timelines.recover(
            fixture.project_id, fixture.timeline_id,
            expected_version=archived_again.data["version"],
            version=archived_again.data["version"],
            idempotency_key="catalog-recover-current",
        ).data == recovered_current.data

        # A stale catalog CAS and a wrong project are both refused before any
        # write.  The historical flat-version route is an explicit 410.
        stale = client.timelines.archive(
            fixture.project_id, fixture.timeline_id,
            expected_version=archived_again.data["version"],
            idempotency_key="catalog-stale-archive",
        )
        assert not stale.ok and stale.error.code == "conflict"
        assert_unchanged(snapshot())
        wrong_scope = client.timelines.recover(
            "not-this-project", fixture.timeline_id, idempotency_key="wrong-scope",
        )
        assert not wrong_scope.ok and wrong_scope.error.code == "not_found"
        assert_unchanged(snapshot())
        assert recovered_current.data["version"] > 1
        with pytest.raises(WorkspaceClientError) as historical:
            transport.recover_timeline(
                fixture.timeline_id,
                expected_version=recovered_current.data["version"],
                version=1,
                idempotency_key="historical-flat-recover",
            )
        assert historical.value.status == 410
        assert "timeline.restore" in historical.value.message
        assert_unchanged(snapshot())

        # The exact old revision is restored through the sole provider.  A
        # lost acknowledgement leaves the original flight/draft intact, and
        # a different restore cannot replace that pending intent.
        old_digest = before_parent["content_digest"]
        restore_session = TimelineSession.open(
            fixture.project_id, fixture.timeline_id,
            client=client, outbox_path=tmp_path / "restore-outbox.json",
        )
        generated = transport._generated
        request = generated._request

        def lose_restore_ack(method, url, **kwargs):
            response = request(method, url, **kwargs)
            if method == "POST" and url.endswith("/commit") and kwargs["headers"].get("Idempotency-Key") == "timeline-restore-retry":
                raise ConnectionError("response lost after durable restore")
            return response

        monkeypatch.setattr(generated, "_request", lose_restore_ack)
        with pytest.raises(WorkspaceClientError, match="response lost"):
            restore_session.restore(
                before_revision, old_digest, idempotency_key="timeline-restore-retry",
            )
        with pytest.raises(TimelineEditError, match="pending intent"):
            restore_session.restore(
                accepted["data"]["accepted_revision_id"],
                accepted["data"]["canonical_document"]["parent_revision"]["content_digest"],
                idempotency_key="different-restore",
            )
        monkeypatch.setattr(generated, "_request", request)
        restored = restore_session.flush()
        assert restored["data"]["accepted_revision_id"] == before_revision
        assert transport.get_project_parent_composition_revision(
            fixture.project_id, fixture.timeline_id, before_revision,
        ) == before_parent
        assert client.timelines.history(
            fixture.project_id, fixture.timeline_id, limit=100,
        ).data["items"][-1]["receipt"] == restored["receipt"]
    finally:
        daemon.stop()
