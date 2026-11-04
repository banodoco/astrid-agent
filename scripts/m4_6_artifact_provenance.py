"""Check hosted M4-6 archives, then admit them in a later trust domain.

Three nominal roles are deliberately non-interchangeable:

* ``TrustedCollectorContext`` belongs to one live producer job and the fixed B
  platform provider.  This module never imports or accepts it.
* :func:`verify_downloaded_artifacts` accepts only an exact privately minted
  ``ArtifactCheckContext``.  It authenticates the running checker identity,
  producer success, and an external artifact-to-producer/attempt/slot join,
  then reads and hashes the downloaded archives.  It does not require or claim
  that the running checker has already completed successfully.
* :func:`admit_checked_artifacts` accepts only an exact privately minted
  ``PostCheckAdmissionContext`` from a later job/service.  That context binds
  the exact checker output to the exact checker job after the platform reports
  the job completed successfully.  Only this function returns admitted output.

Current GitHub artifact, jobs, and OIDC APIs do not expose the required
artifact-to-producer-job/run-attempt/frozen-slot join.  The provider module and
private release methods do not exist in this checkout, so real hosted checking
and admission deliberately report ``NEEDS`` before reading bytes.  Private
dataclasses and structural seams are test/integration payloads only; constructing
them is not authentication.

No receipt content, environment value, serialized capability, fixture path,
producer digest, artifact name inference, or producer-issued ``verified`` flag
is authority here.  Archive SHA-256 establishes downloaded-byte integrity only,
never producer origin or lifecycle truth.
"""

from __future__ import annotations

import hashlib
import hmac
import importlib
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Final

REQUIRED_SLOT_IDS: Final = (
    "direct-macos",
    "direct-linux",
    "direct-windows",
    "pinokio-backend-macos",
    "pinokio-backend-linux",
    "pinokio-backend-windows",
    "pinokio-fully-local-macos",
    "pinokio-fully-local-linux",
    "pinokio-fully-local-windows",
)
_CHECKER_IDENTITY_SOURCE: Final = "github-actions-oidc+workflow-jobs-api"
_ARTIFACT_METADATA_SOURCE: Final = "github-actions-artifacts-api"
_ORIGIN_JOIN_SOURCE: Final = "external-platform-artifact-producer-slot-attestation"
_DOWNLOAD_SOURCE: Final = "github-rest-api-immutable-artifact-id"
_CHECKER_COMPLETION_SOURCE: Final = "github-actions-post-check-workflow-jobs-api"
_CONTEXT_MODULE: Final = "scripts.m4_6_trusted_launcher"
_CHECK_RELEASE_METHOD: Final = "_release_m4_6_artifact_check_batch"
_ADMISSION_RELEASE_METHOD: Final = "_release_m4_6_post_check_admission_batch"
_SHA1_LENGTH: Final = 40
_SHA256_LENGTH: Final = 64
_MAX_ARCHIVE_SIZE_BYTES: Final = 64 * 1024 * 1024

ARTIFACT_ORIGIN_JOIN_NEEDS: Final = (
    "NEEDS: the fixed platform provider must privately mint an ArtifactCheckContext "
    "with authenticated checker OIDC/Actions identity and an external platform "
    "attestation joining each immutable artifact ID and name to its exact successful "
    "producer job, run attempt, and frozen M4-6 slot. GitHub artifact REST metadata, "
    "attempt-scoped jobs metadata, and OIDC claims do not provide that join; names, "
    "timestamps, logs, receipts, upload-action outputs, producer digests, and archive "
    "SHA-256 are not substitutes"
)

POST_CHECK_ADMISSION_NEEDS: Final = (
    "NEEDS: a later independent job/service must privately mint a "
    "PostCheckAdmissionContext that authenticates the exact checker job's completed "
    "success identity and binds that identity to the exact checked output. The "
    "running checker cannot attest its own terminal success"
)

# Compatibility name for older callers that only display the refusal text.
PLATFORM_AUTHORITY_NEEDS: Final = ARTIFACT_ORIGIN_JOIN_NEEDS

SECURE_ARCHIVE_IO_NEEDS: Final = (
    "NEEDS: checker platform must support O_NOFOLLOW, O_DIRECTORY, O_NONBLOCK, "
    "directory-relative os.open, and file-descriptor os.listdir for race-resistant "
    "private archive custody"
)


class ArtifactProvenanceError(ValueError):
    """An artifact batch failed closed provenance or archive validation."""


class ArtifactProvenanceNeedsError(ArtifactProvenanceError):
    """The trusted platform-side authority required for admission is absent."""


@dataclass(frozen=True, slots=True)
class CheckedArtifact:
    """One archive checked before independent post-check admission."""

    slot_id: str
    repository_id: int
    repository_full_name: str
    workflow_id: int
    workflow_ref: str
    workflow_sha: str
    run_id: int
    run_attempt: int
    head_sha: str
    producer_job_id: int
    producer_job_name: str
    checker_job_id: int
    checker_job_name: str
    artifact_id: int
    artifact_name: str
    archive_sha256: str
    archive_size_bytes: int
    archive_bytes: bytes


@dataclass(frozen=True, slots=True)
class AdmittedArtifact:
    """A checked archive admitted by the later completed-checker boundary."""

    checked: CheckedArtifact
    checker_output_sha256: str


@dataclass(frozen=True, slots=True)
class _FrozenSlotBinding:
    """Launcher expectation from fixed policy plus authenticated per-run IDs."""

    slot_id: str
    producer_job_id: int
    producer_job_name: str
    artifact_id: int
    artifact_name: str


@dataclass(frozen=True, slots=True)
class _FrozenRunBinding:
    """Expected binding assembled independently of artifact observation rows.

    Fixed repository/workflow/job/artifact names and slots come from a reviewed
    registry. Per-run IDs come from authenticated workflow/platform context and
    the immutable-ID selection, not from receipt fields or a copied observation.
    """

    repository_id: int
    repository_full_name: str
    workflow_id: int
    workflow_ref: str
    workflow_sha: str
    run_id: int
    run_attempt: int
    head_sha: str
    checker_job_id: int
    checker_job_name: str
    slots: tuple[_FrozenSlotBinding, ...]


@dataclass(frozen=True, slots=True)
class _AuthenticatedCheckerIdentity:
    """Authenticated identity of the currently running checker, not its outcome."""

    identity_source: str
    repository_id: int
    repository_full_name: str
    workflow_id: int
    workflow_ref: str
    workflow_sha: str
    run_id: int
    run_attempt: int
    head_sha: str
    checker_job_id: int
    checker_job_name: str


@dataclass(frozen=True, slots=True)
class _AuthenticatedArtifactMetadata:
    """Artifact/download observation independent of producer-origin evidence."""

    metadata_source: str
    download_source: str
    repository_id: int
    repository_full_name: str
    workflow_id: int
    workflow_ref: str
    workflow_sha: str
    run_id: int
    run_attempt: int
    head_sha: str
    artifact_id: int
    artifact_name: str
    artifact_expired: bool
    downloaded_artifact_id: int
    downloaded_archive_sha256: str
    downloaded_archive_size_bytes: int


@dataclass(frozen=True, slots=True)
class _AuthenticatedArtifactOrigin:
    """External platform join from one artifact to producer attempt and slot."""

    origin_join_source: str
    repository_id: int
    repository_full_name: str
    workflow_id: int
    workflow_ref: str
    workflow_sha: str
    run_id: int
    run_attempt: int
    head_sha: str
    producer_job_id: int
    producer_job_name: str
    producer_job_status: str
    producer_job_conclusion: str
    slot_id: str
    artifact_id: int
    artifact_name: str


@dataclass(frozen=True, slots=True)
class _ArtifactCheckBatch:
    """Private checker-provider payload; not a serialized capability.

    The release must prove a production mint, current checker process, liveness,
    expiry, non-fixture source, and the external origin join, then atomically
    consume the context so a second release is impossible.
    """

    frozen: _FrozenRunBinding
    checker: _AuthenticatedCheckerIdentity
    artifacts: tuple[_AuthenticatedArtifactMetadata, ...]
    origin_joins: tuple[_AuthenticatedArtifactOrigin, ...]
    archive_root: Path


@dataclass(frozen=True, slots=True)
class _AuthenticatedCheckerCompletion:
    """Later platform observation of the exact checker and its bound output."""

    completion_source: str
    repository_id: int
    repository_full_name: str
    workflow_id: int
    workflow_ref: str
    workflow_sha: str
    run_id: int
    run_attempt: int
    head_sha: str
    checker_job_id: int
    checker_job_name: str
    checker_job_status: str
    checker_job_conclusion: str
    checker_output_sha256: str


@dataclass(frozen=True, slots=True)
class _PostCheckAdmissionBatch:
    """Private later-admission payload; constructing it is not authority."""

    frozen: _FrozenRunBinding
    completion: _AuthenticatedCheckerCompletion
    checked_artifacts: tuple[CheckedArtifact, ...]


def _fail(message: str) -> None:
    raise ArtifactProvenanceError(message)


def _is_exact_positive_int(value: object) -> bool:
    return type(value) is int and value > 0


def _require_positive_int(value: object, field: str) -> None:
    if not _is_exact_positive_int(value):
        _fail(f"{field}: positive platform integer is required")


def _require_nonempty_string(value: object, field: str) -> None:
    if type(value) is not str or not value.strip() or "\x00" in value:
        _fail(f"{field}: non-empty platform string is required")


def _require_sha(value: object, length: int, field: str) -> None:
    if (
        type(value) is not str
        or len(value) != length
        or any(character not in "0123456789abcdef" for character in value)
    ):
        _fail(f"{field}: lowercase {length * 4}-bit hexadecimal digest is required")


def _require_safe_label(value: object, field: str) -> None:
    _require_nonempty_string(value, field)
    assert isinstance(value, str)
    if value in {".", ".."} or "/" in value or "\\" in value:
        _fail(f"{field}: path separators and relative path labels are forbidden")


def _require_exact_literal(value: object, expected: str, field: str) -> None:
    if type(value) is not str or value != expected:
        _fail(f"{field}: exact platform value {expected!r} is required")


def _check_frozen_run(frozen: object) -> _FrozenRunBinding:
    if type(frozen) is not _FrozenRunBinding:
        _fail("frozen: exact private frozen-run binding is required")
    assert isinstance(frozen, _FrozenRunBinding)
    for field in ("repository_id", "workflow_id", "run_id", "run_attempt", "checker_job_id"):
        _require_positive_int(getattr(frozen, field), f"frozen.{field}")
    for field in ("repository_full_name", "workflow_ref", "checker_job_name"):
        _require_nonempty_string(getattr(frozen, field), f"frozen.{field}")
    _require_sha(frozen.workflow_sha, _SHA1_LENGTH, "frozen.workflow_sha")
    _require_sha(frozen.head_sha, _SHA1_LENGTH, "frozen.head_sha")
    if type(frozen.slots) is not tuple:
        _fail("frozen.slots: exact immutable tuple is required")
    if len(frozen.slots) != len(REQUIRED_SLOT_IDS):
        _fail(f"frozen.slots: exactly {len(REQUIRED_SLOT_IDS)} slot bindings are required")

    observed_slots: list[str] = []
    producer_ids: set[int] = set()
    producer_names: set[str] = set()
    artifact_ids: set[int] = set()
    artifact_names: set[str] = set()
    for index, binding in enumerate(frozen.slots):
        prefix = f"frozen.slots[{index}]"
        if type(binding) is not _FrozenSlotBinding:
            _fail(f"{prefix}: exact private frozen-slot binding is required")
        _require_safe_label(binding.slot_id, f"{prefix}.slot_id")
        _require_positive_int(binding.producer_job_id, f"{prefix}.producer_job_id")
        _require_nonempty_string(binding.producer_job_name, f"{prefix}.producer_job_name")
        _require_positive_int(binding.artifact_id, f"{prefix}.artifact_id")
        _require_safe_label(binding.artifact_name, f"{prefix}.artifact_name")
        observed_slots.append(binding.slot_id)
        if binding.producer_job_id in producer_ids:
            _fail(f"{prefix}.producer_job_id: duplicate frozen producer job")
        if binding.producer_job_name in producer_names:
            _fail(f"{prefix}.producer_job_name: duplicate frozen producer job")
        if binding.artifact_id in artifact_ids:
            _fail(f"{prefix}.artifact_id: duplicate frozen artifact")
        if binding.artifact_name in artifact_names:
            _fail(f"{prefix}.artifact_name: duplicate frozen artifact")
        producer_ids.add(binding.producer_job_id)
        producer_names.add(binding.producer_job_name)
        artifact_ids.add(binding.artifact_id)
        artifact_names.add(binding.artifact_name)
    if tuple(observed_slots) != REQUIRED_SLOT_IDS:
        _fail("frozen.slots: closed-world slot order/set differs from the exact nine M4-6 slots")
    return frozen


def _compare_run_fields(
    frozen: _FrozenRunBinding,
    observed: object,
    prefix: str,
) -> None:
    for field in (
        "repository_id",
        "repository_full_name",
        "workflow_id",
        "workflow_ref",
        "workflow_sha",
        "run_id",
        "run_attempt",
        "head_sha",
    ):
        expected = getattr(frozen, field)
        actual = getattr(observed, field)
        if type(actual) is not type(expected) or actual != expected:
            _fail(f"{prefix}.{field}: observed {actual!r}, expected frozen {expected!r}")


def _check_checker_identity(
    frozen: _FrozenRunBinding,
    checker: object,
) -> _AuthenticatedCheckerIdentity:
    if type(checker) is not _AuthenticatedCheckerIdentity:
        _fail("checker: exact private authenticated checker identity is required")
    assert isinstance(checker, _AuthenticatedCheckerIdentity)
    _require_exact_literal(
        checker.identity_source,
        _CHECKER_IDENTITY_SOURCE,
        "checker.identity_source",
    )
    _compare_run_fields(frozen, checker, "checker")
    for field in ("checker_job_id", "checker_job_name"):
        expected = getattr(frozen, field)
        actual = getattr(checker, field)
        if type(actual) is not type(expected) or actual != expected:
            _fail(f"checker.{field}: observed {actual!r}, expected frozen {expected!r}")
    return checker


def _check_metadata(
    frozen: _FrozenRunBinding,
    binding: _FrozenSlotBinding,
    observed: object,
) -> _AuthenticatedArtifactMetadata:
    prefix = f"artifacts[{binding.slot_id}]"
    if type(observed) is not _AuthenticatedArtifactMetadata:
        _fail(f"{prefix}: exact private authenticated metadata row is required")
    assert isinstance(observed, _AuthenticatedArtifactMetadata)
    _require_exact_literal(
        observed.metadata_source,
        _ARTIFACT_METADATA_SOURCE,
        f"{prefix}.metadata_source",
    )
    _require_exact_literal(
        observed.download_source,
        _DOWNLOAD_SOURCE,
        f"{prefix}.download_source",
    )
    _compare_run_fields(frozen, observed, prefix)

    for field in ("artifact_id", "artifact_name"):
        expected = getattr(binding, field)
        actual = getattr(observed, field)
        if type(actual) is not type(expected) or actual != expected:
            _fail(f"{prefix}.{field}: observed {actual!r}, expected frozen {expected!r}")
    if type(observed.artifact_expired) is not bool or observed.artifact_expired:
        _fail(f"{prefix}.artifact_expired: artifact is expired or the platform claim is missing")
    if (
        type(observed.downloaded_artifact_id) is not int
        or observed.downloaded_artifact_id != binding.artifact_id
    ):
        _fail(f"{prefix}.downloaded_artifact_id: immutable-ID download does not match artifact")
    _require_sha(
        observed.downloaded_archive_sha256,
        _SHA256_LENGTH,
        f"{prefix}.downloaded_archive_sha256",
    )
    _require_positive_int(
        observed.downloaded_archive_size_bytes,
        f"{prefix}.downloaded_archive_size_bytes",
    )
    if observed.downloaded_archive_size_bytes > _MAX_ARCHIVE_SIZE_BYTES:
        _fail(
            f"{prefix}.downloaded_archive_size_bytes: exceeds the verifier safety limit "
            f"of {_MAX_ARCHIVE_SIZE_BYTES} bytes"
        )
    return observed


def _check_origin_join(
    frozen: _FrozenRunBinding,
    binding: _FrozenSlotBinding,
    observed: object,
) -> _AuthenticatedArtifactOrigin:
    prefix = f"origin_joins[{binding.slot_id}]"
    if type(observed) is not _AuthenticatedArtifactOrigin:
        raise ArtifactProvenanceNeedsError(
            f"{ARTIFACT_ORIGIN_JOIN_NEEDS}; {prefix} exact authenticated join is absent"
        )
    assert isinstance(observed, _AuthenticatedArtifactOrigin)
    if (
        type(observed.origin_join_source) is not str
        or observed.origin_join_source != _ORIGIN_JOIN_SOURCE
    ):
        raise ArtifactProvenanceNeedsError(
            f"{ARTIFACT_ORIGIN_JOIN_NEEDS}; {prefix}.origin_join_source is absent or unauthenticated"
        )
    _compare_run_fields(frozen, observed, prefix)
    for field in (
        "slot_id",
        "producer_job_id",
        "producer_job_name",
        "artifact_id",
        "artifact_name",
    ):
        expected = getattr(binding, field)
        actual = getattr(observed, field)
        if type(actual) is not type(expected) or actual != expected:
            _fail(f"{prefix}.{field}: observed {actual!r}, expected frozen {expected!r}")
    _require_exact_literal(
        observed.producer_job_status,
        "completed",
        f"{prefix}.producer_job_status",
    )
    _require_exact_literal(
        observed.producer_job_conclusion,
        "success",
        f"{prefix}.producer_job_conclusion",
    )
    return observed


def _require_secure_archive_io() -> None:
    required_flags = ("O_NOFOLLOW", "O_DIRECTORY", "O_NONBLOCK")
    if any(not _is_exact_positive_int(getattr(os, name, None)) for name in required_flags):
        raise ArtifactProvenanceNeedsError(SECURE_ARCHIVE_IO_NEEDS)
    if os.open not in getattr(os, "supports_dir_fd", set()):
        raise ArtifactProvenanceNeedsError(SECURE_ARCHIVE_IO_NEEDS)
    if os.listdir not in getattr(os, "supports_fd", set()):
        raise ArtifactProvenanceNeedsError(SECURE_ARCHIVE_IO_NEEDS)


def _open_private_archive_root(path: object) -> int:
    _require_secure_archive_io()
    if type(path) is not type(Path()) or not path.is_absolute():
        _fail("archive_root: authenticated private absolute Path is required")
    components = path.parts[1:]
    if not components or any(
        type(component) is not str or component in {"", ".", ".."} or "\x00" in component
        for component in components
    ):
        _fail("archive_root: root and relative traversal paths are forbidden")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    try:
        current_fd = os.open("/", flags)
    except (OSError, ValueError) as exc:
        _fail(f"archive_root: cannot open filesystem root securely: {exc}")
    try:
        for component in components:
            next_fd = os.open(component, flags, dir_fd=current_fd)
            os.close(current_fd)
            current_fd = next_fd
        mode = os.fstat(current_fd).st_mode
        if not stat.S_ISDIR(mode):
            _fail("archive_root: authenticated root is not a directory")
        return current_fd
    except (OSError, ValueError) as exc:
        os.close(current_fd)
        _fail(f"archive_root: symlinked, missing, or unreadable private root: {exc}")
    raise AssertionError("unreachable")


def _archive_filename(artifact_id: int) -> str:
    return f"{artifact_id}.zip"


def _read_archive(root_fd: int, filename: str, expected_size: int) -> bytes:
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    try:
        archive_fd = os.open(filename, flags, dir_fd=root_fd)
    except OSError as exc:
        _fail(f"archive {filename}: missing, symlinked, or unreadable: {exc}")
    try:
        before = os.fstat(archive_fd)
        if not stat.S_ISREG(before.st_mode):
            _fail(f"archive {filename}: regular file is required")
        if before.st_size != expected_size:
            _fail(
                f"archive {filename}: size mismatch; platform={expected_size}, downloaded={before.st_size}"
            )
        chunks: list[bytes] = []
        size = 0
        while True:
            chunk = os.read(archive_fd, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
        after = os.fstat(archive_fd)
        stable = (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        )
        if stable != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ):
            _fail(f"archive {filename}: archive changed while it was being verified")
        if size != expected_size:
            _fail(f"archive {filename}: bytes read differ from platform size")
        return b"".join(chunks)
    except OSError as exc:
        _fail(f"archive {filename}: secure read failed: {exc}")
    finally:
        os.close(archive_fd)


def _check_authenticated_batch(batch: object) -> tuple[CheckedArtifact, ...]:
    """Private running-check seam; its argument is not authority by construction."""

    if type(batch) is not _ArtifactCheckBatch:
        _fail("batch: exact private artifact-check batch is required")
    assert isinstance(batch, _ArtifactCheckBatch)
    frozen = _check_frozen_run(batch.frozen)
    _check_checker_identity(frozen, batch.checker)
    if type(batch.origin_joins) is not tuple:
        raise ArtifactProvenanceNeedsError(
            f"{ARTIFACT_ORIGIN_JOIN_NEEDS}; origin_joins exact immutable tuple is absent"
        )

    origins_by_slot: dict[str, _AuthenticatedArtifactOrigin] = {}
    seen_origin_artifact_ids: set[int] = set()
    seen_origin_artifact_names: set[str] = set()
    seen_producer_job_ids: set[int] = set()
    seen_producer_job_names: set[str] = set()
    for index, row in enumerate(batch.origin_joins):
        if type(row) is not _AuthenticatedArtifactOrigin:
            raise ArtifactProvenanceNeedsError(
                f"{ARTIFACT_ORIGIN_JOIN_NEEDS}; origin_joins[{index}] is not exact "
                "authenticated origin evidence"
            )
        _require_safe_label(row.slot_id, f"origin_joins[{index}].slot_id")
        _require_positive_int(row.artifact_id, f"origin_joins[{index}].artifact_id")
        _require_safe_label(row.artifact_name, f"origin_joins[{index}].artifact_name")
        _require_positive_int(row.producer_job_id, f"origin_joins[{index}].producer_job_id")
        _require_nonempty_string(
            row.producer_job_name,
            f"origin_joins[{index}].producer_job_name",
        )
        if row.slot_id not in REQUIRED_SLOT_IDS:
            _fail(f"origin_joins[{index}].slot_id: extra or unknown slot {row.slot_id!r}")
        if row.slot_id in origins_by_slot:
            _fail(f"origin_joins[{index}].slot_id: duplicate row for {row.slot_id!r}")
        if row.artifact_id in seen_origin_artifact_ids:
            _fail(f"origin_joins[{index}].artifact_id: duplicate observed artifact ID")
        if row.artifact_name in seen_origin_artifact_names:
            _fail(f"origin_joins[{index}].artifact_name: duplicate observed artifact name")
        if row.producer_job_id in seen_producer_job_ids:
            _fail(f"origin_joins[{index}].producer_job_id: duplicate observed producer job")
        if row.producer_job_name in seen_producer_job_names:
            _fail(f"origin_joins[{index}].producer_job_name: duplicate observed producer job")
        origins_by_slot[row.slot_id] = row
        seen_origin_artifact_ids.add(row.artifact_id)
        seen_origin_artifact_names.add(row.artifact_name)
        seen_producer_job_ids.add(row.producer_job_id)
        seen_producer_job_names.add(row.producer_job_name)
    missing_origins = [slot_id for slot_id in REQUIRED_SLOT_IDS if slot_id not in origins_by_slot]
    if missing_origins or len(batch.origin_joins) != len(REQUIRED_SLOT_IDS):
        raise ArtifactProvenanceNeedsError(
            f"{ARTIFACT_ORIGIN_JOIN_NEEDS}; missing origin joins: {', '.join(missing_origins)}"
        )
    for binding in frozen.slots:
        _check_origin_join(frozen, binding, origins_by_slot[binding.slot_id])

    if type(batch.artifacts) is not tuple:
        _fail("artifacts: exact immutable metadata tuple is required")
    artifacts_by_id: dict[int, _AuthenticatedArtifactMetadata] = {}
    seen_artifact_ids: set[int] = set()
    seen_artifact_names: set[str] = set()
    for index, row in enumerate(batch.artifacts):
        if type(row) is not _AuthenticatedArtifactMetadata:
            _fail(f"artifacts[{index}]: exact private authenticated metadata row is required")
        _require_positive_int(row.artifact_id, f"artifacts[{index}].artifact_id")
        _require_safe_label(row.artifact_name, f"artifacts[{index}].artifact_name")
        if row.artifact_id in seen_artifact_ids:
            _fail(f"artifacts[{index}].artifact_id: duplicate observed artifact ID")
        if row.artifact_name in seen_artifact_names:
            _fail(f"artifacts[{index}].artifact_name: duplicate observed artifact name")
        artifacts_by_id[row.artifact_id] = row
        seen_artifact_ids.add(row.artifact_id)
        seen_artifact_names.add(row.artifact_name)
    if len(batch.artifacts) != len(REQUIRED_SLOT_IDS):
        _fail(f"artifacts: exactly {len(REQUIRED_SLOT_IDS)} metadata rows are required")

    checked: list[tuple[_FrozenSlotBinding, _AuthenticatedArtifactMetadata]] = []
    for binding in frozen.slots:
        metadata = artifacts_by_id.get(binding.artifact_id)
        if metadata is None:
            _fail(f"artifacts: missing metadata for immutable artifact ID {binding.artifact_id}")
        checked.append((binding, _check_metadata(frozen, binding, metadata)))

    root_fd = _open_private_archive_root(batch.archive_root)
    try:
        expected_names = {_archive_filename(binding.artifact_id) for binding, _ in checked}
        try:
            actual_names = set(os.listdir(root_fd))
        except OSError as exc:
            _fail(f"archive_root: cannot enumerate dedicated private root: {exc}")
        if actual_names != expected_names:
            missing_files = sorted(expected_names - actual_names)
            extra_files = sorted(actual_names - expected_names)
            _fail(
                "archive_root: closed-world archive files differ; "
                f"missing={missing_files!r}, extra={extra_files!r}"
            )

        checked_artifacts: list[CheckedArtifact] = []
        for binding, metadata in checked:
            filename = _archive_filename(binding.artifact_id)
            archive_bytes = _read_archive(
                root_fd,
                filename,
                metadata.downloaded_archive_size_bytes,
            )
            observed_digest = hashlib.sha256(archive_bytes).hexdigest()
            if not hmac.compare_digest(observed_digest, metadata.downloaded_archive_sha256):
                _fail(
                    f"archive {filename}: SHA-256 mismatch; "
                    f"expected={metadata.downloaded_archive_sha256}, downloaded={observed_digest}"
                )
            checked_artifacts.append(
                CheckedArtifact(
                    slot_id=binding.slot_id,
                    repository_id=frozen.repository_id,
                    repository_full_name=frozen.repository_full_name,
                    workflow_id=frozen.workflow_id,
                    workflow_ref=frozen.workflow_ref,
                    workflow_sha=frozen.workflow_sha,
                    run_id=frozen.run_id,
                    run_attempt=frozen.run_attempt,
                    head_sha=frozen.head_sha,
                    producer_job_id=binding.producer_job_id,
                    producer_job_name=binding.producer_job_name,
                    checker_job_id=frozen.checker_job_id,
                    checker_job_name=frozen.checker_job_name,
                    artifact_id=binding.artifact_id,
                    artifact_name=binding.artifact_name,
                    archive_sha256=observed_digest,
                    archive_size_bytes=len(archive_bytes),
                    archive_bytes=archive_bytes,
                )
            )
        return tuple(checked_artifacts)
    finally:
        os.close(root_fd)


def _checked_field_values(artifact: CheckedArtifact) -> tuple[object, ...]:
    return (
        artifact.slot_id,
        artifact.repository_id,
        artifact.repository_full_name,
        artifact.workflow_id,
        artifact.workflow_ref,
        artifact.workflow_sha,
        artifact.run_id,
        artifact.run_attempt,
        artifact.head_sha,
        artifact.producer_job_id,
        artifact.producer_job_name,
        artifact.checker_job_id,
        artifact.checker_job_name,
        artifact.artifact_id,
        artifact.artifact_name,
        artifact.archive_sha256,
        artifact.archive_size_bytes,
        artifact.archive_bytes,
    )


def _hash_framed_value(hasher: object, value: object) -> None:
    if type(value) is bytes:
        encoded = value
    elif type(value) is int:
        encoded = str(value).encode("ascii")
    elif type(value) is str:
        encoded = value.encode("utf-8")
    else:
        _fail("checked output: exact bytes, integer, and string fields are required")
    hasher.update(len(encoded).to_bytes(8, "big"))
    hasher.update(encoded)


def _checked_output_sha256(artifacts: tuple[CheckedArtifact, ...]) -> str:
    """Hash exact checked output framing; this digest is integrity, not authority."""

    hasher = hashlib.sha256()
    hasher.update(b"astrid-m4-6-checked-output-v1\x00")
    for artifact in artifacts:
        for value in _checked_field_values(artifact):
            _hash_framed_value(hasher, value)
    return hasher.hexdigest()


def _check_checked_output(
    frozen: _FrozenRunBinding,
    artifacts: object,
) -> tuple[CheckedArtifact, ...]:
    if type(artifacts) is not tuple:
        _fail("checked_artifacts: exact immutable tuple is required")
    assert isinstance(artifacts, tuple)
    if len(artifacts) != len(frozen.slots):
        _fail(f"checked_artifacts: exactly {len(frozen.slots)} checked rows are required")
    for index, (binding, artifact) in enumerate(zip(frozen.slots, artifacts, strict=True)):
        prefix = f"checked_artifacts[{index}]"
        if type(artifact) is not CheckedArtifact:
            _fail(f"{prefix}: exact CheckedArtifact is required")
        assert isinstance(artifact, CheckedArtifact)
        expected_fields = {
            "slot_id": binding.slot_id,
            "repository_id": frozen.repository_id,
            "repository_full_name": frozen.repository_full_name,
            "workflow_id": frozen.workflow_id,
            "workflow_ref": frozen.workflow_ref,
            "workflow_sha": frozen.workflow_sha,
            "run_id": frozen.run_id,
            "run_attempt": frozen.run_attempt,
            "head_sha": frozen.head_sha,
            "producer_job_id": binding.producer_job_id,
            "producer_job_name": binding.producer_job_name,
            "checker_job_id": frozen.checker_job_id,
            "checker_job_name": frozen.checker_job_name,
            "artifact_id": binding.artifact_id,
            "artifact_name": binding.artifact_name,
        }
        for field, expected in expected_fields.items():
            actual = getattr(artifact, field)
            if type(actual) is not type(expected) or actual != expected:
                _fail(f"{prefix}.{field}: checked output differs from frozen binding")
        _require_sha(artifact.archive_sha256, _SHA256_LENGTH, f"{prefix}.archive_sha256")
        _require_positive_int(artifact.archive_size_bytes, f"{prefix}.archive_size_bytes")
        if type(artifact.archive_bytes) is not bytes:
            _fail(f"{prefix}.archive_bytes: exact immutable bytes are required")
        if len(artifact.archive_bytes) != artifact.archive_size_bytes:
            _fail(f"{prefix}.archive_size_bytes: checked output size mismatch")
        digest = hashlib.sha256(artifact.archive_bytes).hexdigest()
        if not hmac.compare_digest(digest, artifact.archive_sha256):
            _fail(f"{prefix}.archive_sha256: checked output digest mismatch")
    return artifacts


def _check_completed_checker(
    frozen: _FrozenRunBinding,
    completion: object,
) -> _AuthenticatedCheckerCompletion:
    if type(completion) is not _AuthenticatedCheckerCompletion:
        _fail("completion: exact private authenticated checker completion is required")
    assert isinstance(completion, _AuthenticatedCheckerCompletion)
    _require_exact_literal(
        completion.completion_source,
        _CHECKER_COMPLETION_SOURCE,
        "completion.completion_source",
    )
    _compare_run_fields(frozen, completion, "completion")
    for field in ("checker_job_id", "checker_job_name"):
        expected = getattr(frozen, field)
        actual = getattr(completion, field)
        if type(actual) is not type(expected) or actual != expected:
            _fail(f"completion.{field}: observed {actual!r}, expected frozen {expected!r}")
    _require_exact_literal(
        completion.checker_job_status,
        "completed",
        "completion.checker_job_status",
    )
    _require_exact_literal(
        completion.checker_job_conclusion,
        "success",
        "completion.checker_job_conclusion",
    )
    _require_sha(
        completion.checker_output_sha256,
        _SHA256_LENGTH,
        "completion.checker_output_sha256",
    )
    return completion


def _validate_post_check_batch(
    batch: object,
) -> tuple[tuple[CheckedArtifact, ...], str]:
    """Validate later evidence without producing an admitted success shape."""

    if type(batch) is not _PostCheckAdmissionBatch:
        _fail("batch: exact private post-check admission batch is required")
    assert isinstance(batch, _PostCheckAdmissionBatch)
    frozen = _check_frozen_run(batch.frozen)
    checked = _check_checked_output(frozen, batch.checked_artifacts)
    completion = _check_completed_checker(frozen, batch.completion)
    observed_output_digest = _checked_output_sha256(checked)
    if not hmac.compare_digest(observed_output_digest, completion.checker_output_sha256):
        _fail("completion.checker_output_sha256: exact checked output binding mismatch")
    return checked, observed_output_digest


def _load_context_type(type_name: str, needs: str) -> type:
    try:
        context_module = importlib.import_module(_CONTEXT_MODULE)
    except ModuleNotFoundError as exc:
        if exc.name != _CONTEXT_MODULE:
            raise
        raise ArtifactProvenanceNeedsError(needs) from exc
    context_type = getattr(context_module, type_name, None)
    if type(context_type) is not type:
        raise ArtifactProvenanceNeedsError(f"{needs}; exact {type_name} type is absent")
    other_role_names = {
        "ArtifactCheckContext": ("TrustedCollectorContext", "PostCheckAdmissionContext"),
        "PostCheckAdmissionContext": ("TrustedCollectorContext", "ArtifactCheckContext"),
    }[type_name]
    aliases = [
        other_name
        for other_name in other_role_names
        if getattr(context_module, other_name, None) is context_type
    ]
    if aliases:
        raise ArtifactProvenanceNeedsError(
            f"{needs}; {type_name} aliases distinct role type(s): {', '.join(aliases)}"
        )
    return context_type


def verify_downloaded_artifacts(context: object) -> tuple[CheckedArtifact, ...]:
    """Check archives through the exact running-checker context.

    This function intentionally has no path, mapping, provider/verifier selector,
    callback, token, environment, receipt, or success parameter.
    """

    context_type = _load_context_type("ArtifactCheckContext", ARTIFACT_ORIGIN_JOIN_NEEDS)
    if type(context) is not context_type:
        raise ArtifactProvenanceError(
            f"context: exact {_CONTEXT_MODULE}.ArtifactCheckContext is required; producer "
            "TrustedCollectorContext, admission contexts, protocols, duck types, subclasses, "
            "and serialized contexts are forbidden"
        )
    release = getattr(context_type, _CHECK_RELEASE_METHOD, None)
    if not callable(release):
        raise ArtifactProvenanceNeedsError(
            f"{ARTIFACT_ORIGIN_JOIN_NEEDS}; ArtifactCheckContext lacks {_CHECK_RELEASE_METHOD}"
        )
    batch = release(context)
    if type(batch) is not _ArtifactCheckBatch:
        raise ArtifactProvenanceNeedsError(
            f"{ARTIFACT_ORIGIN_JOIN_NEEDS}; private checker release returned no exact "
            "authenticated artifact-check batch"
        )
    return _check_authenticated_batch(batch)


def admit_checked_artifacts(context: object) -> tuple[AdmittedArtifact, ...]:
    """Admit exact checked output through a later independent context."""

    context_type = _load_context_type(
        "PostCheckAdmissionContext",
        POST_CHECK_ADMISSION_NEEDS,
    )
    if type(context) is not context_type:
        raise ArtifactProvenanceError(
            f"context: exact {_CONTEXT_MODULE}.PostCheckAdmissionContext is required; "
            "producer TrustedCollectorContext, running ArtifactCheckContext, protocols, duck "
            "types, subclasses, and serialized contexts are forbidden"
        )
    release = getattr(context_type, _ADMISSION_RELEASE_METHOD, None)
    if not callable(release):
        raise ArtifactProvenanceNeedsError(
            f"{POST_CHECK_ADMISSION_NEEDS}; PostCheckAdmissionContext lacks "
            f"{_ADMISSION_RELEASE_METHOD}"
        )
    batch = release(context)
    if type(batch) is not _PostCheckAdmissionBatch:
        raise ArtifactProvenanceNeedsError(
            f"{POST_CHECK_ADMISSION_NEEDS}; private admission release returned no exact "
            "post-check batch"
        )
    checked, output_digest = _validate_post_check_batch(batch)
    return tuple(
        AdmittedArtifact(
            checked=artifact,
            checker_output_sha256=output_digest,
        )
        for artifact in checked
    )


__all__ = [
    "ARTIFACT_ORIGIN_JOIN_NEEDS",
    "AdmittedArtifact",
    "ArtifactProvenanceError",
    "ArtifactProvenanceNeedsError",
    "CheckedArtifact",
    "PLATFORM_AUTHORITY_NEEDS",
    "POST_CHECK_ADMISSION_NEEDS",
    "REQUIRED_SLOT_IDS",
    "SECURE_ARCHIVE_IO_NEEDS",
    "admit_checked_artifacts",
    "verify_downloaded_artifacts",
]
