"""Structural tests for split M4-6 check and later-admission boundaries.

IMPORTANT: ``_structural_batch`` constructs private verifier payloads only to
exercise validation mechanics.  These fixtures are not hosted authority and
must never be used as evidence that GitHub authenticated an artifact origin.
The public boundaries remain disabled with ``NEEDS`` until the fixed provider
privately mints the distinct contexts and can authenticate both the missing
artifact-to-producer-job/attempt/slot join and later checker completion/output
binding.  Producer ``TrustedCollectorContext`` is accepted by neither boundary.
"""

from __future__ import annotations

import hashlib
import inspect
import os
import sys
from dataclasses import replace
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from scripts import m4_6_artifact_provenance as provenance

REPOSITORY_ID = 4567
REPOSITORY = "banodoco/astrid"
WORKFLOW_ID = 8901
WORKFLOW_REF = "banodoco/astrid/.github/workflows/release.yml@refs/heads/main"
WORKFLOW_SHA = "a" * 40
RUN_ID = 23456
RUN_ATTEMPT = 3
HEAD_SHA = "b" * 40
CHECKER_JOB_ID = 34567
CHECKER_JOB_NAME = "m4-6-artifact-provenance"


class _AlwaysEqualString(str):
    def __eq__(self, _other: object) -> bool:
        return True

    def __ne__(self, _other: object) -> bool:
        return False


def _archive_bytes(index: int) -> bytes:
    return f"opaque hosted archive for slot {index:02d}\n".encode()


def _structural_batch(root: Path) -> provenance._ArtifactCheckBatch:
    """Build non-authoritative structural fixtures for the private test seam."""

    root.mkdir()
    bindings = []
    rows = []
    origins = []
    for index, slot_id in enumerate(provenance.REQUIRED_SLOT_IDS, start=1):
        producer_job_id = 40_000 + index
        artifact_id = 50_000 + index
        artifact_name = f"m4-6-{slot_id}"
        archive = _archive_bytes(index)
        digest = hashlib.sha256(archive).hexdigest()
        (root / f"{artifact_id}.zip").write_bytes(archive)
        bindings.append(
            provenance._FrozenSlotBinding(
                slot_id=slot_id,
                producer_job_id=producer_job_id,
                producer_job_name=f"produce / {slot_id}",
                artifact_id=artifact_id,
                artifact_name=artifact_name,
            )
        )
        rows.append(
            provenance._AuthenticatedArtifactMetadata(
                metadata_source="github-actions-artifacts-api",
                download_source="github-rest-api-immutable-artifact-id",
                repository_id=REPOSITORY_ID,
                repository_full_name=REPOSITORY,
                workflow_id=WORKFLOW_ID,
                workflow_ref=WORKFLOW_REF,
                workflow_sha=WORKFLOW_SHA,
                run_id=RUN_ID,
                run_attempt=RUN_ATTEMPT,
                head_sha=HEAD_SHA,
                artifact_id=artifact_id,
                artifact_name=artifact_name,
                artifact_expired=False,
                downloaded_artifact_id=artifact_id,
                downloaded_archive_sha256=digest,
                downloaded_archive_size_bytes=len(archive),
            )
        )
        origins.append(
            provenance._AuthenticatedArtifactOrigin(
                origin_join_source="external-platform-artifact-producer-slot-attestation",
                repository_id=REPOSITORY_ID,
                repository_full_name=REPOSITORY,
                workflow_id=WORKFLOW_ID,
                workflow_ref=WORKFLOW_REF,
                workflow_sha=WORKFLOW_SHA,
                run_id=RUN_ID,
                run_attempt=RUN_ATTEMPT,
                head_sha=HEAD_SHA,
                producer_job_id=producer_job_id,
                producer_job_name=f"produce / {slot_id}",
                producer_job_status="completed",
                producer_job_conclusion="success",
                slot_id=slot_id,
                artifact_id=artifact_id,
                artifact_name=artifact_name,
            )
        )
    frozen = provenance._FrozenRunBinding(
        repository_id=REPOSITORY_ID,
        repository_full_name=REPOSITORY,
        workflow_id=WORKFLOW_ID,
        workflow_ref=WORKFLOW_REF,
        workflow_sha=WORKFLOW_SHA,
        run_id=RUN_ID,
        run_attempt=RUN_ATTEMPT,
        head_sha=HEAD_SHA,
        checker_job_id=CHECKER_JOB_ID,
        checker_job_name=CHECKER_JOB_NAME,
        slots=tuple(bindings),
    )
    return provenance._ArtifactCheckBatch(
        frozen=frozen,
        checker=provenance._AuthenticatedCheckerIdentity(
            identity_source="github-actions-oidc+workflow-jobs-api",
            repository_id=REPOSITORY_ID,
            repository_full_name=REPOSITORY,
            workflow_id=WORKFLOW_ID,
            workflow_ref=WORKFLOW_REF,
            workflow_sha=WORKFLOW_SHA,
            run_id=RUN_ID,
            run_attempt=RUN_ATTEMPT,
            head_sha=HEAD_SHA,
            checker_job_id=CHECKER_JOB_ID,
            checker_job_name=CHECKER_JOB_NAME,
        ),
        artifacts=tuple(rows),
        origin_joins=tuple(origins),
        archive_root=root,
    )


def _replace_row(
    batch: provenance._ArtifactCheckBatch,
    index: int = 0,
    **changes: object,
) -> provenance._ArtifactCheckBatch:
    rows = list(batch.artifacts)
    rows[index] = replace(rows[index], **changes)
    return replace(batch, artifacts=tuple(rows))


def _replace_origin(
    batch: provenance._ArtifactCheckBatch,
    index: int = 0,
    **changes: object,
) -> provenance._ArtifactCheckBatch:
    origins = list(batch.origin_joins)
    origins[index] = replace(origins[index], **changes)
    return replace(batch, origin_joins=tuple(origins))


def _admission_batch(root: Path) -> provenance._PostCheckAdmissionBatch:
    artifact_batch = _structural_batch(root)
    checked = provenance._check_authenticated_batch(artifact_batch)
    return provenance._PostCheckAdmissionBatch(
        frozen=artifact_batch.frozen,
        completion=provenance._AuthenticatedCheckerCompletion(
            completion_source="github-actions-post-check-workflow-jobs-api",
            repository_id=REPOSITORY_ID,
            repository_full_name=REPOSITORY,
            workflow_id=WORKFLOW_ID,
            workflow_ref=WORKFLOW_REF,
            workflow_sha=WORKFLOW_SHA,
            run_id=RUN_ID,
            run_attempt=RUN_ATTEMPT,
            head_sha=HEAD_SHA,
            checker_job_id=CHECKER_JOB_ID,
            checker_job_name=CHECKER_JOB_NAME,
            checker_job_status="completed",
            checker_job_conclusion="success",
            checker_output_sha256=provenance._checked_output_sha256(checked),
        ),
        checked_artifacts=checked,
    )


def test_running_checker_returns_exact_nine_checked_not_admitted_artifacts(tmp_path: Path) -> None:
    batch = _structural_batch(tmp_path / "private-downloads")

    verified = provenance._check_authenticated_batch(batch)

    assert type(verified) is tuple
    assert tuple(row.slot_id for row in verified) == provenance.REQUIRED_SLOT_IDS
    assert len(verified) == 9
    for index, row in enumerate(verified, start=1):
        assert type(row) is provenance.CheckedArtifact
        assert row.repository_id == REPOSITORY_ID
        assert row.repository_full_name == REPOSITORY
        assert row.workflow_id == WORKFLOW_ID
        assert row.workflow_ref == WORKFLOW_REF
        assert row.workflow_sha == WORKFLOW_SHA
        assert row.run_id == RUN_ID
        assert row.run_attempt == RUN_ATTEMPT
        assert row.head_sha == HEAD_SHA
        assert row.checker_job_id == CHECKER_JOB_ID
        assert row.checker_job_name == CHECKER_JOB_NAME
        assert row.archive_bytes == _archive_bytes(index)
        assert row.archive_size_bytes == len(row.archive_bytes)
        assert row.archive_sha256 == hashlib.sha256(row.archive_bytes).hexdigest()
        with pytest.raises(AttributeError):
            row.artifact_id = 1  # type: ignore[misc]


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("repository_id", 9999, "repository_id"),
        ("repository_full_name", "attacker/fork", "repository_full_name"),
        ("workflow_id", 9999, "workflow_id"),
        ("workflow_ref", "attacker/workflow@refs/heads/main", "workflow_ref"),
        ("workflow_sha", "c" * 40, "workflow_sha"),
        ("run_id", RUN_ID - 1, "run_id"),
        ("run_attempt", RUN_ATTEMPT - 1, "run_attempt"),
        ("head_sha", "d" * 40, "head_sha"),
    ],
)
def test_structural_fixture_rejects_cross_run_attempt_repository_and_workflow_bindings(
    tmp_path: Path,
    field: str,
    value: object,
    message: str,
) -> None:
    batch = _replace_row(_structural_batch(tmp_path / "private-downloads"), **{field: value})

    with pytest.raises(provenance.ArtifactProvenanceError, match=message):
        provenance._check_authenticated_batch(batch)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("repository_id", 9999),
        ("repository_full_name", "attacker/fork"),
        ("workflow_id", 9999),
        ("workflow_ref", "attacker/workflow@refs/heads/main"),
        ("workflow_sha", "c" * 40),
        ("run_id", RUN_ID - 1),
        ("run_attempt", RUN_ATTEMPT - 1),
        ("head_sha", "d" * 40),
        ("checker_job_id", CHECKER_JOB_ID + 1),
        ("checker_job_name", "lookalike checker"),
    ],
)
def test_running_checker_identity_is_independent_and_has_no_terminal_outcome(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    batch = _structural_batch(tmp_path / "private-downloads")
    batch = replace(batch, checker=replace(batch.checker, **{field: value}))

    assert not hasattr(batch.checker, "checker_job_status")
    assert not hasattr(batch.checker, "checker_job_conclusion")
    with pytest.raises(provenance.ArtifactProvenanceError, match=field):
        provenance._check_authenticated_batch(batch)


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"artifact_id": 999_999}, "immutable artifact ID"),
        ({"artifact_name": "renamed-artifact"}, "artifact_name"),
        ({"downloaded_artifact_id": 999_999}, "downloaded_artifact_id"),
        ({"artifact_expired": True}, "expired"),
        ({"downloaded_archive_sha256": ""}, "downloaded_archive_sha256"),
        ({"downloaded_archive_size_bytes": 0}, "downloaded_archive_size_bytes"),
    ],
)
def test_artifact_metadata_rejects_renamed_substituted_or_missing_claims(
    tmp_path: Path,
    changes: dict[str, object],
    message: str,
) -> None:
    batch = _replace_row(_structural_batch(tmp_path / "private-downloads"), **changes)

    with pytest.raises(provenance.ArtifactProvenanceError, match=message):
        provenance._check_authenticated_batch(batch)


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"producer_job_id": 999_999}, "producer_job_id"),
        ({"producer_job_name": "wrong producer"}, "producer_job_name"),
        ({"artifact_id": 999_999}, "artifact_id"),
        ({"artifact_name": "renamed-artifact"}, "artifact_name"),
    ],
)
def test_external_origin_join_rejects_wrong_producer_or_artifact_binding(
    tmp_path: Path,
    changes: dict[str, object],
    message: str,
) -> None:
    batch = _replace_origin(_structural_batch(tmp_path / "private-downloads"), **changes)

    with pytest.raises(provenance.ArtifactProvenanceError, match=message):
        provenance._check_authenticated_batch(batch)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("repository_id", REPOSITORY_ID + 1),
        ("repository_full_name", "attacker/fork"),
        ("workflow_id", WORKFLOW_ID + 1),
        ("workflow_ref", "attacker/workflow@refs/heads/main"),
        ("workflow_sha", "c" * 40),
        ("run_id", RUN_ID - 1),
        ("run_attempt", RUN_ATTEMPT - 1),
        ("head_sha", "d" * 40),
        ("slot_id", provenance.REQUIRED_SLOT_IDS[1]),
    ],
)
def test_external_origin_join_is_bound_to_exact_run_attempt_and_frozen_slot(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    batch = _replace_origin(
        _structural_batch(tmp_path / "private-downloads"),
        **{field: value},
    )

    with pytest.raises(provenance.ArtifactProvenanceError, match=field):
        provenance._check_authenticated_batch(batch)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("slot_id", []),
        ("artifact_id", []),
        ("artifact_name", {}),
        ("producer_job_id", []),
        ("producer_job_name", {}),
    ],
)
def test_structural_fixture_rejects_unhashable_malformed_metadata_as_provenance_error(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    batch = _structural_batch(tmp_path / "private-downloads")
    if field in {"slot_id", "producer_job_id", "producer_job_name"}:
        batch = _replace_origin(batch, **{field: value})
    else:
        batch = _replace_row(batch, **{field: value})

    with pytest.raises(provenance.ArtifactProvenanceError, match=field):
        provenance._check_authenticated_batch(batch)


def test_structural_fixture_rejects_oversized_archive_claim_before_read(tmp_path: Path) -> None:
    batch = _replace_row(
        _structural_batch(tmp_path / "private-downloads"),
        downloaded_archive_size_bytes=provenance._MAX_ARCHIVE_SIZE_BYTES + 1,
    )

    with pytest.raises(provenance.ArtifactProvenanceError, match="safety limit"):
        provenance._check_authenticated_batch(batch)


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"producer_job_status": "in_progress"}, "producer_job_status"),
        ({"producer_job_conclusion": "failure"}, "producer_job_conclusion"),
    ],
)
def test_running_checker_requires_completed_success_producer_job(
    tmp_path: Path,
    changes: dict[str, object],
    message: str,
) -> None:
    batch = _replace_origin(_structural_batch(tmp_path / "private-downloads"), **changes)

    with pytest.raises(provenance.ArtifactProvenanceError, match=message):
        provenance._check_authenticated_batch(batch)


@pytest.mark.parametrize(
    "field",
    [
        "metadata_source",
        "download_source",
        "producer_job_status",
        "producer_job_conclusion",
    ],
)
def test_structural_fixture_rejects_literal_spoofing_string_subclasses(
    tmp_path: Path,
    field: str,
) -> None:
    batch = _structural_batch(tmp_path / "private-downloads")
    replace_helper = _replace_origin if field.startswith("producer_job") else _replace_row
    batch = replace_helper(batch, **{field: _AlwaysEqualString("attacker-controlled")})

    with pytest.raises(provenance.ArtifactProvenanceError, match=field):
        provenance._check_authenticated_batch(batch)


def test_running_checker_needs_external_origin_join_before_any_archive_read(
    tmp_path: Path,
    monkeypatch,
) -> None:
    batch = _replace_origin(
        _structural_batch(tmp_path / "private-downloads"),
        origin_join_source="artifact-name-and-digest-guess",
    )

    def forbidden_open(_path: object) -> int:
        raise AssertionError("archive root must not be opened without the external join")

    monkeypatch.setattr(provenance, "_open_private_archive_root", forbidden_open)
    with pytest.raises(provenance.ArtifactProvenanceNeedsError, match="external platform"):
        provenance._check_authenticated_batch(batch)


def test_artifact_metadata_and_matching_digest_cannot_replace_missing_origin_join(
    tmp_path: Path,
    monkeypatch,
) -> None:
    batch = _structural_batch(tmp_path / "private-downloads")
    batch = replace(batch, origin_joins=batch.origin_joins[:-1])

    def forbidden_open(_path: object) -> int:
        raise AssertionError("matching artifact metadata must not authorize an archive read")

    monkeypatch.setattr(provenance, "_open_private_archive_root", forbidden_open)
    with pytest.raises(provenance.ArtifactProvenanceNeedsError, match="missing origin joins"):
        provenance._check_authenticated_batch(batch)


def test_missing_origin_join_takes_precedence_over_malformed_artifact_metadata(
    tmp_path: Path,
    monkeypatch,
) -> None:
    batch = _structural_batch(tmp_path / "private-downloads")
    batch = replace(
        batch,
        origin_joins=batch.origin_joins[:-1],
        artifacts=(object(),),
    )

    def forbidden_open(_path: object) -> int:
        raise AssertionError("missing origin authority must refuse before archive access")

    monkeypatch.setattr(provenance, "_open_private_archive_root", forbidden_open)
    with pytest.raises(provenance.ArtifactProvenanceNeedsError, match="missing origin joins"):
        provenance._check_authenticated_batch(batch)


def test_running_checker_rejects_spoofed_checker_identity_source(tmp_path: Path) -> None:
    batch = _structural_batch(tmp_path / "private-downloads")
    batch = replace(
        batch,
        checker=replace(
            batch.checker,
            identity_source=_AlwaysEqualString("caller-environment"),
        ),
    )

    with pytest.raises(provenance.ArtifactProvenanceError, match="identity_source"):
        provenance._check_authenticated_batch(batch)


def test_structural_fixture_rejects_missing_row(tmp_path: Path) -> None:
    batch = _structural_batch(tmp_path / "private-downloads")
    batch = replace(batch, artifacts=batch.artifacts[:-1])

    with pytest.raises(provenance.ArtifactProvenanceError, match="exactly 9 metadata rows"):
        provenance._check_authenticated_batch(batch)


def test_structural_fixture_rejects_duplicate_row(tmp_path: Path) -> None:
    batch = _structural_batch(tmp_path / "private-downloads")
    batch = replace(batch, artifacts=batch.artifacts + (batch.artifacts[0],))

    with pytest.raises(provenance.ArtifactProvenanceError, match="duplicate observed"):
        provenance._check_authenticated_batch(batch)


@pytest.mark.parametrize(
    "field",
    ["artifact_id", "artifact_name", "producer_job_id", "producer_job_name"],
)
def test_structural_fixture_rejects_duplicate_identity_across_distinct_slots(
    tmp_path: Path,
    field: str,
) -> None:
    batch = _structural_batch(tmp_path / "private-downloads")
    if field.startswith("producer_job"):
        rows = list(batch.origin_joins)
        rows[1] = replace(rows[1], **{field: getattr(rows[0], field)})
        batch = replace(batch, origin_joins=tuple(rows))
    else:
        rows = list(batch.artifacts)
        rows[1] = replace(rows[1], **{field: getattr(rows[0], field)})
        batch = replace(batch, artifacts=tuple(rows))

    with pytest.raises(provenance.ArtifactProvenanceError, match="duplicate observed"):
        provenance._check_authenticated_batch(batch)


def test_structural_fixture_rejects_extra_or_wrong_slot(tmp_path: Path) -> None:
    batch = _replace_origin(
        _structural_batch(tmp_path / "private-downloads"),
        slot_id="attacker-extra-slot",
    )

    with pytest.raises(provenance.ArtifactProvenanceError, match="extra or unknown slot"):
        provenance._check_authenticated_batch(batch)


def test_structural_fixture_rejects_non_closed_world_frozen_slot_set(tmp_path: Path) -> None:
    batch = _structural_batch(tmp_path / "private-downloads")
    slots = list(batch.frozen.slots)
    slots[-1] = replace(slots[-1], slot_id="attacker-extra-slot")
    batch = replace(batch, frozen=replace(batch.frozen, slots=tuple(slots)))

    with pytest.raises(provenance.ArtifactProvenanceError, match="closed-world slot"):
        provenance._check_authenticated_batch(batch)


def test_structural_fixture_rejects_local_or_fixture_metadata(tmp_path: Path) -> None:
    batch = _replace_row(
        _structural_batch(tmp_path / "private-downloads"),
        metadata_source="local-fixture",
        download_source="fixture-path",
    )

    with pytest.raises(provenance.ArtifactProvenanceError, match="metadata_source"):
        provenance._check_authenticated_batch(batch)


def test_structural_fixture_rejects_replayed_prior_run_metadata(tmp_path: Path) -> None:
    batch = _structural_batch(tmp_path / "private-downloads")
    replayed = tuple(replace(row, run_id=RUN_ID - 1, run_attempt=1) for row in batch.artifacts)
    batch = replace(batch, artifacts=replayed)

    with pytest.raises(provenance.ArtifactProvenanceError, match="run_id"):
        provenance._check_authenticated_batch(batch)


def test_structural_fixture_rejects_archive_digest_mismatch(tmp_path: Path) -> None:
    batch = _structural_batch(tmp_path / "private-downloads")
    artifact_id = batch.frozen.slots[0].artifact_id
    original = batch.archive_root / f"{artifact_id}.zip"
    original.write_bytes(b"x" * original.stat().st_size)

    with pytest.raises(provenance.ArtifactProvenanceError, match="SHA-256 mismatch"):
        provenance._check_authenticated_batch(batch)


def test_structural_fixture_rejects_archive_size_mismatch(tmp_path: Path) -> None:
    batch = _structural_batch(tmp_path / "private-downloads")
    artifact_id = batch.frozen.slots[0].artifact_id
    with (batch.archive_root / f"{artifact_id}.zip").open("ab") as archive:
        archive.write(b"tamper")

    with pytest.raises(provenance.ArtifactProvenanceError, match="size mismatch"):
        provenance._check_authenticated_batch(batch)


def test_structural_fixture_rejects_archive_substituted_from_another_slot(tmp_path: Path) -> None:
    batch = _structural_batch(tmp_path / "private-downloads")
    first, second = batch.frozen.slots[:2]
    replacement = (batch.archive_root / f"{second.artifact_id}.zip").read_bytes()
    target = batch.archive_root / f"{first.artifact_id}.zip"
    target.write_bytes(replacement[: target.stat().st_size].ljust(target.stat().st_size, b"!"))

    with pytest.raises(provenance.ArtifactProvenanceError, match="SHA-256 mismatch"):
        provenance._check_authenticated_batch(batch)


def test_structural_fixture_rejects_renamed_or_extra_archive_file(tmp_path: Path) -> None:
    batch = _structural_batch(tmp_path / "private-downloads")
    artifact_id = batch.frozen.slots[0].artifact_id
    (batch.archive_root / f"{artifact_id}.zip").rename(batch.archive_root / "renamed.zip")

    with pytest.raises(provenance.ArtifactProvenanceError, match="closed-world archive files"):
        provenance._check_authenticated_batch(batch)


def test_structural_fixture_rejects_symlinked_archive(tmp_path: Path) -> None:
    batch = _structural_batch(tmp_path / "private-downloads")
    artifact_id = batch.frozen.slots[0].artifact_id
    archive = batch.archive_root / f"{artifact_id}.zip"
    target = tmp_path / "outside.zip"
    archive.rename(target)
    archive.symlink_to(target)

    with pytest.raises(provenance.ArtifactProvenanceError, match="symlinked"):
        provenance._check_authenticated_batch(batch)


def test_structural_fixture_rejects_symlinked_archive_root(tmp_path: Path) -> None:
    batch = _structural_batch(tmp_path / "private-downloads")
    real_root = tmp_path / "real-downloads"
    batch.archive_root.rename(real_root)
    batch.archive_root.symlink_to(real_root, target_is_directory=True)

    with pytest.raises(provenance.ArtifactProvenanceError, match="symlinked"):
        provenance._check_authenticated_batch(batch)


def test_structural_fixture_rejects_nonregular_archive(tmp_path: Path) -> None:
    batch = _structural_batch(tmp_path / "private-downloads")
    artifact_id = batch.frozen.slots[0].artifact_id
    archive = batch.archive_root / f"{artifact_id}.zip"
    archive.unlink()
    archive.mkdir()

    with pytest.raises(provenance.ArtifactProvenanceError, match="regular file"):
        provenance._check_authenticated_batch(batch)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO is a POSIX archive-custody case")
def test_structural_fixture_rejects_fifo_without_blocking(tmp_path: Path) -> None:
    batch = _structural_batch(tmp_path / "private-downloads")
    artifact_id = batch.frozen.slots[0].artifact_id
    archive = batch.archive_root / f"{artifact_id}.zip"
    archive.unlink()
    os.mkfifo(archive)

    with pytest.raises(provenance.ArtifactProvenanceError, match="regular file"):
        provenance._check_authenticated_batch(batch)


def test_structural_fixture_needs_secure_nofollow_platform_primitive(
    tmp_path: Path,
    monkeypatch,
) -> None:
    batch = _structural_batch(tmp_path / "private-downloads")
    monkeypatch.delattr(provenance.os, "O_NOFOLLOW")

    with pytest.raises(provenance.ArtifactProvenanceNeedsError, match="O_NOFOLLOW"):
        provenance._check_authenticated_batch(batch)


def test_structural_fixture_rejects_nul_archive_root(tmp_path: Path) -> None:
    batch = _structural_batch(tmp_path / "private-downloads")
    batch = replace(batch, archive_root=Path("/private/tmp/has\x00nul"))

    with pytest.raises(provenance.ArtifactProvenanceError, match="traversal"):
        provenance._check_authenticated_batch(batch)


def test_private_post_check_seam_validates_without_returning_admitted_shape(
    tmp_path: Path,
) -> None:
    batch = _admission_batch(tmp_path / "private-downloads")

    checked, output_digest = provenance._validate_post_check_batch(batch)

    assert checked == batch.checked_artifacts
    assert output_digest == batch.completion.checker_output_sha256
    assert not any(type(row) is provenance.AdmittedArtifact for row in checked)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("checker_job_status", "in_progress"),
        ("checker_job_status", "queued"),
        ("checker_job_conclusion", "failure"),
        ("checker_job_conclusion", "cancelled"),
        ("checker_job_id", CHECKER_JOB_ID + 1),
        ("checker_job_name", "lookalike checker"),
        ("run_id", RUN_ID - 1),
        ("run_attempt", RUN_ATTEMPT - 1),
        ("workflow_id", WORKFLOW_ID + 1),
    ],
)
def test_later_admission_requires_exact_completed_success_checker_identity(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    batch = _admission_batch(tmp_path / "private-downloads")
    batch = replace(batch, completion=replace(batch.completion, **{field: value}))

    with pytest.raises(provenance.ArtifactProvenanceError, match=field):
        provenance._validate_post_check_batch(batch)


def test_later_admission_rejects_substituted_checker_output(tmp_path: Path) -> None:
    batch = _admission_batch(tmp_path / "private-downloads")
    first = batch.checked_artifacts[0]
    tampered = replace(first, archive_bytes=b"x" * len(first.archive_bytes))
    batch = replace(batch, checked_artifacts=(tampered, *batch.checked_artifacts[1:]))

    with pytest.raises(provenance.ArtifactProvenanceError, match="digest mismatch"):
        provenance._validate_post_check_batch(batch)


def test_later_admission_rejects_output_binding_substitution(tmp_path: Path) -> None:
    batch = _admission_batch(tmp_path / "private-downloads")
    batch = replace(
        batch,
        completion=replace(batch.completion, checker_output_sha256="f" * 64),
    )

    with pytest.raises(provenance.ArtifactProvenanceError, match="output binding mismatch"):
        provenance._validate_post_check_batch(batch)


def test_later_admission_rejects_self_consistent_substituted_checked_archive(
    tmp_path: Path,
) -> None:
    batch = _admission_batch(tmp_path / "private-downloads")
    first = batch.checked_artifacts[0]
    substituted_bytes = b"different archive with internally valid digest"
    substituted = replace(
        first,
        archive_bytes=substituted_bytes,
        archive_size_bytes=len(substituted_bytes),
        archive_sha256=hashlib.sha256(substituted_bytes).hexdigest(),
    )
    batch = replace(batch, checked_artifacts=(substituted, *batch.checked_artifacts[1:]))

    with pytest.raises(provenance.ArtifactProvenanceError, match="output binding mismatch"):
        provenance._validate_post_check_batch(batch)


def test_later_admission_rejects_spoofed_completion_source(tmp_path: Path) -> None:
    batch = _admission_batch(tmp_path / "private-downloads")
    batch = replace(
        batch,
        completion=replace(
            batch.completion,
            completion_source=_AlwaysEqualString("running-checker-self-report"),
        ),
    )

    with pytest.raises(provenance.ArtifactProvenanceError, match="completion_source"):
        provenance._validate_post_check_batch(batch)


def test_public_boundaries_explicitly_need_missing_platform_contexts(monkeypatch) -> None:
    monkeypatch.delitem(sys.modules, "scripts.m4_6_trusted_launcher", raising=False)

    with pytest.raises(provenance.ArtifactProvenanceNeedsError, match="external platform"):
        provenance.verify_downloaded_artifacts(object())
    with pytest.raises(provenance.ArtifactProvenanceNeedsError, match="later independent"):
        provenance.admit_checked_artifacts(object())


def test_public_entry_points_accept_only_the_fixed_context_parameter() -> None:
    for entry_point in (
        provenance.verify_downloaded_artifacts,
        provenance.admit_checked_artifacts,
    ):
        signature = inspect.signature(entry_point)
        assert tuple(signature.parameters) == ("context",)
        assert signature.parameters["context"].kind is inspect.Parameter.POSITIONAL_OR_KEYWORD


def test_public_boundaries_reject_context_class_aliasing_across_roles(monkeypatch) -> None:
    class AliasedContext:
        def _release_m4_6_artifact_check_batch(self):
            raise AssertionError("aliased role must not release checker evidence")

        def _release_m4_6_post_check_admission_batch(self):
            raise AssertionError("aliased role must not release admission evidence")

    launcher = ModuleType("scripts.m4_6_trusted_launcher")
    launcher.TrustedCollectorContext = AliasedContext
    launcher.ArtifactCheckContext = AliasedContext
    launcher.PostCheckAdmissionContext = AliasedContext
    monkeypatch.setitem(sys.modules, "scripts.m4_6_trusted_launcher", launcher)

    with pytest.raises(provenance.ArtifactProvenanceNeedsError, match="aliases distinct role"):
        provenance.verify_downloaded_artifacts(AliasedContext())
    with pytest.raises(provenance.ArtifactProvenanceNeedsError, match="aliases distinct role"):
        provenance.admit_checked_artifacts(AliasedContext())


def test_running_public_boundary_accepts_only_exact_artifact_check_context(
    tmp_path: Path,
    monkeypatch,
) -> None:
    batch = _structural_batch(tmp_path / "private-downloads")

    class TrustedCollectorContext:
        pass

    class PostCheckAdmissionContext:
        pass

    class ArtifactCheckContext:
        def _release_m4_6_artifact_check_batch(self):
            return batch

    class Subclass(ArtifactCheckContext):
        pass

    launcher = ModuleType("scripts.m4_6_trusted_launcher")
    launcher.TrustedCollectorContext = TrustedCollectorContext
    launcher.ArtifactCheckContext = ArtifactCheckContext
    launcher.PostCheckAdmissionContext = PostCheckAdmissionContext
    monkeypatch.setitem(sys.modules, "scripts.m4_6_trusted_launcher", launcher)

    checked = provenance.verify_downloaded_artifacts(ArtifactCheckContext())
    assert all(type(row) is provenance.CheckedArtifact for row in checked)
    assert not any(type(row) is provenance.AdmittedArtifact for row in checked)

    for value in (
        TrustedCollectorContext(),
        PostCheckAdmissionContext(),
        Subclass(),
        {"verified": True, "context": "serialized"},
        SimpleNamespace(
            verified=True,
            _release_m4_6_artifact_check_batch=lambda: pytest.fail("duck method called"),
        ),
    ):
        with pytest.raises(provenance.ArtifactProvenanceError, match="ArtifactCheckContext"):
            provenance.verify_downloaded_artifacts(value)


def test_later_public_boundary_accepts_only_exact_post_check_context(
    tmp_path: Path,
    monkeypatch,
) -> None:
    batch = _admission_batch(tmp_path / "private-downloads")

    class TrustedCollectorContext:
        pass

    class ArtifactCheckContext:
        pass

    class PostCheckAdmissionContext:
        def _release_m4_6_post_check_admission_batch(self):
            return batch

    class Subclass(PostCheckAdmissionContext):
        pass

    launcher = ModuleType("scripts.m4_6_trusted_launcher")
    launcher.TrustedCollectorContext = TrustedCollectorContext
    launcher.ArtifactCheckContext = ArtifactCheckContext
    launcher.PostCheckAdmissionContext = PostCheckAdmissionContext
    monkeypatch.setitem(sys.modules, "scripts.m4_6_trusted_launcher", launcher)

    admitted = provenance.admit_checked_artifacts(PostCheckAdmissionContext())
    assert all(type(row) is provenance.AdmittedArtifact for row in admitted)

    for value in (
        TrustedCollectorContext(),
        ArtifactCheckContext(),
        Subclass(),
        {"success": True, "context": "serialized"},
        SimpleNamespace(
            success=True,
            _release_m4_6_post_check_admission_batch=lambda: pytest.fail("duck method called"),
        ),
    ):
        with pytest.raises(provenance.ArtifactProvenanceError, match="PostCheckAdmissionContext"):
            provenance.admit_checked_artifacts(value)


@pytest.mark.parametrize(
    ("type_name", "method_name", "entry_point", "needs"),
    [
        (
            "ArtifactCheckContext",
            "_release_m4_6_artifact_check_batch",
            provenance.verify_downloaded_artifacts,
            "external platform",
        ),
        (
            "PostCheckAdmissionContext",
            "_release_m4_6_post_check_admission_batch",
            provenance.admit_checked_artifacts,
            "later independent",
        ),
    ],
)
def test_public_boundaries_need_exact_context_type_and_private_release(
    monkeypatch,
    type_name: str,
    method_name: str,
    entry_point,
    needs: str,
) -> None:
    launcher = ModuleType("scripts.m4_6_trusted_launcher")
    monkeypatch.setitem(sys.modules, "scripts.m4_6_trusted_launcher", launcher)

    with pytest.raises(provenance.ArtifactProvenanceNeedsError, match=type_name):
        entry_point(object())

    context_type = type(type_name, (), {})
    setattr(launcher, type_name, context_type)
    with pytest.raises(provenance.ArtifactProvenanceNeedsError, match=method_name):
        entry_point(context_type())

    assert any(
        needs in message
        for message in (
            provenance.ARTIFACT_ORIGIN_JOIN_NEEDS,
            provenance.POST_CHECK_ADMISSION_NEEDS,
        )
    )
