"""Verify the fixed M4-6 GitHub artifact attestation trust boundary.

This module deliberately treats the custom predicate as a statement made by
the reviewed attestor, not as a GitHub-certified lifecycle fact.  It first asks
the installed GitHub CLI to perform Sigstore/Fulcio and Rekor-or-trusted-
timestamp verification for the downloaded bytes. Only then does it parse the
predicate and reconcile every assertion against authenticated, attempt-scoped
Actions REST observations.

The public result is an opaque, process-local, expiring receipt.  A launcher
must atomically claim that exact registered object via :func:`_claim_verified_batch`.
Constructed, copied, serialized, duck-typed, expired, or already-consumed
objects never carry authority.  Post-check admission uses a different receipt
and independently repeats all verification after the checker has completed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Final, Protocol

REPOSITORY_FULL_NAME: Final = "banodoco/astrid-agent"
REPOSITORY_ID: Final = 1_223_136_945
REPOSITORY_OWNER: Final = "banodoco"
REPOSITORY_OWNER_ID: Final = 134_059_142
REPOSITORY_URI: Final = f"https://github.com/{REPOSITORY_FULL_NAME}"
REPOSITORY_OWNER_URI: Final = f"https://github.com/{REPOSITORY_OWNER}"

OIDC_ISSUER: Final = "https://token.actions.githubusercontent.com"
# Fulcio validates this audience at certificate issuance.  The current Fulcio
# GitHub certificate mapping does not preserve ``aud`` as an extension; if a
# verifier starts returning it, the value is checked below instead of ignored.
OIDC_AUDIENCE: Final = "sigstore"
PREDICATE_TYPE: Final = "https://astrid.dev/attestations/m4-6/trusted-artifact-set/v1"
PREDICATE_VERSION: Final = 1
SIGNER_WORKFLOW_PATH: Final = ".github/workflows/m4-6-trusted-attestor.yml"
CHECKER_JOB_NAME: Final = "m4-6-artifact-checker"
CANARY_WORKFLOW_PATH: Final = ".github/workflows/m4-6-attestation-canary.yml"
CANARY_PREDICATE_TYPE: Final = "https://astrid.dev/attestations/m4-6/nonrelease-synthetic-canary/v1"
CANARY_ARTIFACT_NAME: Final = "m4-6-nonrelease-synthetic-canary"
GH_CLI_VERSION: Final = "2.86.0"
GITHUB_API_VERSION: Final = "2026-03-10"
MAX_ARTIFACT_SIZE_BYTES: Final = 64 * 1024 * 1024
MAX_BUNDLE_SIZE_BYTES: Final = 16 * 1024 * 1024
MAX_ATTESTATION_METADATA_BYTES: Final = 4 * 1024 * 1024
MAX_ATTESTATION_CANDIDATES: Final = 1000
_RECEIPT_TTL_SECONDS: Final = 300.0
_SHA1_RE: Final = re.compile(r"[0-9a-f]{40}\Z")
_SHA256_RE: Final = re.compile(r"[0-9a-f]{64}\Z")
_TOKEN_RE: Final = re.compile(
    r"(?:github_pat_[A-Za-z0-9_]+|gh[pousr]_[A-Za-z0-9_]+|Bearer\s+\S+)",
    re.IGNORECASE,
)
_GH_AUTH_ENVIRONMENT: Final = frozenset(
    {
        "GH_TOKEN",
        "GITHUB_TOKEN",
        "GH_ENTERPRISE_TOKEN",
        "GITHUB_ENTERPRISE_TOKEN",
        "GH_HOST",
        "GITHUB_HOST",
    }
)
_ATTESTATION_BUNDLE_HOST: Final = "tmaproduction.blob.core.windows.net"
_PATH_TYPE: Final = type(Path())


@dataclass(frozen=True, slots=True)
class SlotPolicy:
    """One row in the immutable nine-slot registry."""

    slot: str
    producer_job_name: str
    artifact_name: str
    matrix: None = None


TRUSTED_SLOT_REGISTRY: Final = tuple(
    SlotPolicy(
        slot=slot,
        producer_job_name=f"m4-6-producer-{slot}",
        artifact_name=f"m4-6-{slot}",
    )
    for slot in (
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
)


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


_REGISTRY_DOCUMENT: Final = [
    {
        "artifactName": row.artifact_name,
        "matrix": row.matrix,
        "producerJobName": row.producer_job_name,
        "slot": row.slot,
    }
    for row in TRUSTED_SLOT_REGISTRY
]
REGISTRY_DIGEST: Final = f"sha256:{hashlib.sha256(_canonical_json(_REGISTRY_DOCUMENT)).hexdigest()}"


class GitHubAttestationError(ValueError):
    """The attestation or authenticated platform observations failed closed."""


class GitHubAttestationCommandError(GitHubAttestationError):
    """The installed GitHub CLI could not complete an authenticated operation."""


class GitHubAttestationPolicyError(GitHubAttestationError):
    """Cryptographically verified material did not satisfy the fixed policy."""


@dataclass(frozen=True, slots=True)
class VerificationRequest:
    """Runtime selectors and exact reviewed workflow revisions.

    These values select and constrain authenticated evidence.  They are not a
    capability and do not bypass certificate, API, byte, or registry checks.
    """

    run_id: int
    run_attempt: int
    checker_job_id: int
    artifact_ids: tuple[int, ...]
    source_sha: str
    source_ref: str
    source_event: str
    workflow_id: int
    workflow_path: str
    workflow_ref: str
    workflow_sha: str
    signer_workflow_ref: str
    signer_workflow_sha: str


@dataclass(frozen=True, slots=True)
class _PreparationRequest:
    """Real API/output selectors used while the attestor prepares its predicate."""

    run_id: int
    run_attempt: int
    artifact_ids: tuple[int, ...]
    source_sha: str
    source_ref: str
    source_event: str
    workflow_id: int
    workflow_path: str
    workflow_ref: str
    workflow_sha: str


@dataclass(frozen=True, slots=True)
class CanaryVerificationRequest:
    """Exact runtime selectors for the isolated one-subject canary."""

    run_id: int
    run_attempt: int
    artifact_id: int
    source_sha: str
    source_ref: str
    source_event: str
    workflow_id: int
    workflow_ref: str
    workflow_sha: str


@dataclass(frozen=True, slots=True)
class _CertificatePolicy:
    run_id: int
    run_attempt: int
    source_sha: str
    source_ref: str
    source_event: str
    workflow_ref: str
    workflow_sha: str
    signer_workflow_ref: str
    signer_workflow_sha: str


@dataclass(frozen=True, slots=True)
class VerifiedArtifactFacts:
    """Readable facts for one verified artifact; never a capability itself."""

    slot: str
    repository_id: int
    repository_full_name: str
    workflow_id: int
    workflow_path: str
    workflow_ref: str
    workflow_sha: str
    run_id: int
    run_attempt: int
    head_sha: str
    producer_job_id: int
    producer_job_name: str
    producer_job_status: str
    producer_job_conclusion: str
    checker_job_id: int
    checker_job_name: str
    checker_job_status: str
    checker_job_conclusion: str | None
    artifact_id: int
    artifact_name: str
    api_digest: str
    artifact_size_bytes: int
    artifact_expired: bool
    artifact_expires_at: str
    downloaded_artifact_id: int
    downloaded_sha256: str
    downloaded_size_bytes: int
    downloaded_path: Path


@dataclass(frozen=True, slots=True)
class VerifiedAttestationSummary:
    """Non-authoritative reporting view of a registered verification."""

    repository_id: int
    repository_full_name: str
    workflow_id: int
    workflow_path: str
    workflow_ref: str
    workflow_sha: str
    run_id: int
    run_attempt: int
    head_sha: str
    checker_job_id: int
    checker_job_name: str
    bundle_sha256: str
    registry_digest: str
    checked_output_sha256: str
    artifacts: tuple[VerifiedArtifactFacts, ...]


@dataclass(frozen=True, slots=True)
class CanaryVerificationSummary:
    """Non-authoritative evidence view for the isolated synthetic canary."""

    repository_id: int
    workflow_id: int
    workflow_ref: str
    workflow_sha: str
    run_id: int
    run_attempt: int
    artifact_id: int
    artifact_name: str
    artifact_sha256: str
    artifact_size_bytes: int
    bundle_sha256: str


class VerifiedAttestationReceipt:
    """Opaque exact registered receipt for the running-checker launcher."""

    __slots__ = ("_summary",)

    def __init__(self, _summary: VerifiedAttestationSummary) -> None:
        raise TypeError("VerifiedAttestationReceipt is minted only by successful verification")

    @property
    def summary(self) -> VerifiedAttestationSummary:
        return self._summary

    def __reduce__(self) -> object:
        raise TypeError("verified attestation receipts are not serializable")

    def __reduce_ex__(self, _protocol: int) -> object:
        raise TypeError("verified attestation receipts are not serializable")

    def __copy__(self) -> object:
        raise TypeError("verified attestation receipts are not copyable")

    def __deepcopy__(self, _memo: object) -> object:
        raise TypeError("verified attestation receipts are not copyable")


class PostCheckAdmissionReceipt:
    """Opaque exact registered receipt for later completed-checker admission."""

    __slots__ = ("_summary",)

    def __init__(self, _summary: VerifiedAttestationSummary) -> None:
        raise TypeError("PostCheckAdmissionReceipt is minted only by later verification")

    @property
    def summary(self) -> VerifiedAttestationSummary:
        return self._summary

    def __reduce__(self) -> object:
        raise TypeError("post-check receipts are not serializable")

    def __reduce_ex__(self, _protocol: int) -> object:
        raise TypeError("post-check receipts are not serializable")

    def __copy__(self) -> object:
        raise TypeError("post-check receipts are not copyable")

    def __deepcopy__(self, _memo: object) -> object:
        raise TypeError("post-check receipts are not copyable")


@dataclass(frozen=True, slots=True)
class _VerifiedBatch:
    summary: VerifiedAttestationSummary
    custody_root: Path
    provenance_batch: object
    owner_pid: int
    minted_at_monotonic: float
    expires_at_monotonic: float


@dataclass(frozen=True, slots=True)
class _PostCheckBatch:
    verified: _VerifiedBatch
    checker_output_sha256: str
    provenance_batch: object


_RECEIPT_LOCK: Final = threading.Lock()
_CHECK_RECEIPTS: Final[dict[int, tuple[VerifiedAttestationReceipt, _VerifiedBatch]]] = {}
_POST_RECEIPTS: Final[dict[int, tuple[PostCheckAdmissionReceipt, _PostCheckBatch]]] = {}


def _new_check_receipt(batch: _VerifiedBatch) -> VerifiedAttestationReceipt:
    receipt = object.__new__(VerifiedAttestationReceipt)
    object.__setattr__(receipt, "_summary", batch.summary)
    with _RECEIPT_LOCK:
        _CHECK_RECEIPTS[id(receipt)] = (receipt, batch)
    return receipt


def _new_post_receipt(batch: _PostCheckBatch) -> PostCheckAdmissionReceipt:
    receipt = object.__new__(PostCheckAdmissionReceipt)
    object.__setattr__(receipt, "_summary", batch.verified.summary)
    with _RECEIPT_LOCK:
        _POST_RECEIPTS[id(receipt)] = (receipt, batch)
    return receipt


def _claim_verified_batch(receipt: VerifiedAttestationReceipt) -> _VerifiedBatch:
    """Atomically consume one exact running-checker receipt."""

    if type(receipt) is not VerifiedAttestationReceipt:
        raise GitHubAttestationPolicyError("an exact verified attestation receipt is required")
    with _RECEIPT_LOCK:
        registered = _CHECK_RECEIPTS.pop(id(receipt), None)
    if registered is None or registered[0] is not receipt:
        raise GitHubAttestationPolicyError("verified attestation receipt is unknown or consumed")
    batch = registered[1]
    if os.getpid() != batch.owner_pid:
        raise GitHubAttestationPolicyError(
            "verified attestation receipt belongs to another process"
        )
    if time.monotonic() >= batch.expires_at_monotonic:
        raise GitHubAttestationPolicyError("verified attestation receipt expired")
    return batch


def _claim_post_check_batch(receipt: PostCheckAdmissionReceipt) -> _PostCheckBatch:
    """Atomically consume one exact later-admission receipt."""

    if type(receipt) is not PostCheckAdmissionReceipt:
        raise GitHubAttestationPolicyError("an exact post-check admission receipt is required")
    with _RECEIPT_LOCK:
        registered = _POST_RECEIPTS.pop(id(receipt), None)
    if registered is None or registered[0] is not receipt:
        raise GitHubAttestationPolicyError("post-check admission receipt is unknown or consumed")
    batch = registered[1]
    if os.getpid() != batch.verified.owner_pid:
        raise GitHubAttestationPolicyError(
            "post-check admission receipt belongs to another process"
        )
    if time.monotonic() >= batch.verified.expires_at_monotonic:
        raise GitHubAttestationPolicyError("post-check admission receipt expired")
    return batch


def _claim_artifact_check_receipt(receipt: VerifiedAttestationReceipt) -> object:
    """Consume a receipt and return the exact provenance ``_ArtifactCheckBatch``."""

    batch = _claim_verified_batch(receipt)
    from scripts import m4_6_artifact_provenance as provenance

    if type(batch.provenance_batch) is not provenance._ArtifactCheckBatch:
        raise GitHubAttestationPolicyError("registered artifact-check batch type mismatch")
    return batch.provenance_batch


def _claim_post_check_receipt(receipt: PostCheckAdmissionReceipt) -> object:
    """Consume a receipt and return the exact provenance post-check batch."""

    batch = _claim_post_check_batch(receipt)
    from scripts import m4_6_artifact_provenance as provenance

    if type(batch.provenance_batch) is not provenance._PostCheckAdmissionBatch:
        raise GitHubAttestationPolicyError("registered post-check batch type mismatch")
    return batch.provenance_batch


class _CommandRunner(Protocol):
    def json(self, arguments: tuple[str, ...]) -> object: ...

    def download(self, arguments: tuple[str, ...], destination: Path) -> None: ...

    def attestation_bundles(
        self,
        subject: Path,
        destination: Path,
        predicate_type: str,
    ) -> tuple[dict[str, object], ...]: ...


def _redacted_error(stderr: str) -> str:
    return _TOKEN_RE.sub("<redacted>", stderr.replace("\n", " ").strip())[:500]


def _offline_gh_environment(config_directory: Path) -> dict[str, str]:
    """Return an environment in which offline verification has no GitHub credentials."""

    environment = {
        key: value
        for key, value in os.environ.items()
        if key not in _GH_AUTH_ENVIRONMENT and key != "GH_CONFIG_DIR"
    }
    environment["GH_CONFIG_DIR"] = str(config_directory)
    return environment


class _GitHubCliRunner:
    """Production command transport.  It never invokes a shell."""

    def __init__(self) -> None:
        executable = shutil.which("gh")
        if executable is None:
            raise GitHubAttestationCommandError("installed GitHub CLI `gh` was not found")
        completed = subprocess.run(
            (executable, "version"),
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
            shell=False,
        )
        first_line = completed.stdout.splitlines()[0] if completed.stdout else ""
        if completed.returncode != 0 or not first_line.startswith(f"gh version {GH_CLI_VERSION} "):
            raise GitHubAttestationCommandError(
                f"GitHub CLI must be the source-reviewed version {GH_CLI_VERSION}"
            )
        self._executable = executable

    def json(self, arguments: tuple[str, ...]) -> object:
        if arguments[:1] == ("attestation",):
            with tempfile.TemporaryDirectory(prefix="astrid-m4-6-gh-noauth-") as config:
                completed = subprocess.run(
                    (self._executable, *arguments),
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=180,
                    shell=False,
                    env=_offline_gh_environment(Path(config)),
                )
        else:
            completed = subprocess.run(
                (self._executable, *arguments),
                check=False,
                capture_output=True,
                text=True,
                timeout=180,
                shell=False,
            )
        if completed.returncode != 0:
            detail = _redacted_error(completed.stderr)
            raise GitHubAttestationCommandError(
                f"GitHub CLI rejected authenticated verification{': ' + detail if detail else ''}"
            )
        try:
            return json.loads(completed.stdout)
        except (json.JSONDecodeError, UnicodeError) as exc:
            raise GitHubAttestationCommandError(
                "GitHub CLI returned malformed JSON after verification"
            ) from exc

    def _bounded_json(self, arguments: tuple[str, ...], *, max_bytes: int, timeout: int) -> object:
        """Run one JSON command without allowing capture_output to grow unbounded."""

        process = subprocess.Popen(
            (self._executable, *arguments),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
        )
        if process.stdout is None or process.stderr is None:
            process.kill()
            raise GitHubAttestationCommandError("bounded GitHub CLI pipes were unavailable")
        stderr_bytes = bytearray()

        def drain_stderr() -> None:
            while chunk := process.stderr.read(8192):
                remaining = 4096 - len(stderr_bytes)
                if remaining > 0:
                    stderr_bytes.extend(chunk[:remaining])

        stderr_thread = threading.Thread(target=drain_stderr, daemon=True)
        stderr_thread.start()
        timed_out = threading.Event()

        def kill_on_timeout() -> None:
            timed_out.set()
            process.kill()

        timer = threading.Timer(timeout, kill_on_timeout)
        timer.daemon = True
        timer.start()
        output = bytearray()
        overflow = False
        try:
            while chunk := process.stdout.read(65536):
                if len(output) + len(chunk) > max_bytes:
                    overflow = True
                    process.kill()
                    continue
                output.extend(chunk)
            returncode = process.wait()
        finally:
            timer.cancel()
            stderr_thread.join(timeout=5)
        if overflow:
            raise GitHubAttestationPolicyError(
                "authenticated attestation API metadata exceeded the fixed bound"
            )
        if timed_out.is_set():
            raise GitHubAttestationCommandError("authenticated attestation API timed out")
        if returncode != 0:
            detail = _redacted_error(stderr_bytes.decode("utf-8", errors="replace"))
            raise GitHubAttestationCommandError(
                f"authenticated attestation API failed{': ' + detail if detail else ''}"
            )
        try:
            return json.loads(output)
        except (json.JSONDecodeError, UnicodeError) as exc:
            raise GitHubAttestationCommandError(
                "authenticated attestation API returned malformed JSON"
            ) from exc

    def download(self, arguments: tuple[str, ...], destination: Path) -> None:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(destination, flags, 0o600)
        stderr_bytes = bytearray()
        overflow = threading.Event()
        timed_out = threading.Event()
        try:
            with os.fdopen(descriptor, "wb", closefd=True) as output:
                process = subprocess.Popen(
                    (self._executable, *arguments),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    shell=False,
                )
                if process.stdout is None or process.stderr is None:
                    raise GitHubAttestationCommandError(
                        "immutable-ID artifact download pipes were unavailable"
                    )

                def drain_stderr() -> None:
                    while chunk := process.stderr.read(8192):
                        remaining = 4096 - len(stderr_bytes)
                        if remaining > 0:
                            stderr_bytes.extend(chunk[:remaining])

                stderr_thread = threading.Thread(target=drain_stderr, daemon=True)
                stderr_thread.start()

                def kill_on_timeout() -> None:
                    timed_out.set()
                    process.kill()

                timer = threading.Timer(300, kill_on_timeout)
                timer.daemon = True
                timer.start()
                total = 0
                try:
                    while chunk := process.stdout.read(1024 * 1024):
                        total += len(chunk)
                        if total > MAX_ARTIFACT_SIZE_BYTES:
                            overflow.set()
                            process.kill()
                            continue
                        output.write(chunk)
                    returncode = process.wait()
                finally:
                    timer.cancel()
                    stderr_thread.join(timeout=5)
        except BaseException:
            destination.unlink(missing_ok=True)
            raise
        if overflow.is_set():
            destination.unlink(missing_ok=True)
            raise GitHubAttestationPolicyError(
                "immutable-ID artifact download exceeded the fixed 64 MiB bound"
            )
        if timed_out.is_set():
            destination.unlink(missing_ok=True)
            raise GitHubAttestationCommandError("immutable-ID artifact download timed out")
        if returncode != 0:
            destination.unlink(missing_ok=True)
            detail = _redacted_error(stderr_bytes.decode("utf-8", errors="replace"))
            raise GitHubAttestationCommandError(
                f"immutable-ID artifact download failed{': ' + detail if detail else ''}"
            )

    def attestation_bundles(
        self,
        subject: Path,
        destination: Path,
        predicate_type: str,
    ) -> tuple[dict[str, object], ...]:
        """Fetch the complete candidate set through the authenticated GitHub API.

        GitHub CLI 2.86's generic ``api`` command is source-reviewed here only for
        the fixed github.com API host.  Bundle URLs are then fetched without any
        credentials, avoiding the affected CLI attestation HTTP transport.
        """

        del destination
        digest = _hash_file(subject)[0]
        encoded_predicate = urllib.parse.quote(predicate_type, safe="")
        endpoint = (
            f"repos/{REPOSITORY_FULL_NAME}/attestations/sha256:{digest}"
            f"?per_page=100&predicate_type={encoded_predicate}"
        )
        value = self._bounded_json(
            (
                "api",
                "--hostname",
                "github.com",
                "--paginate",
                "--slurp",
                endpoint,
                "-H",
                "Accept: application/vnd.github+json",
                "-H",
                f"X-GitHub-Api-Version: {GITHUB_API_VERSION}",
            ),
            max_bytes=MAX_ATTESTATION_METADATA_BYTES,
            timeout=180,
        )
        if type(value) is not list or not value:
            _fail("authenticated attestation API pagination returned no pages")
        rows: list[dict[str, object]] = []
        for page in value:
            if type(page) is not dict or set(page) != {"attestations"}:
                _fail("authenticated attestation API page has an unexpected schema")
            page_rows = page["attestations"]
            if type(page_rows) is not list:
                _fail("authenticated attestation API rows are malformed")
            for row in page_rows:
                if type(row) is not dict or set(row) != {
                    "repository_id",
                    "bundle_url",
                    "initiator",
                }:
                    _fail("authenticated attestation API row has an unexpected schema")
                if row["repository_id"] != REPOSITORY_ID:
                    _fail("authenticated attestation API row repository ID mismatch")
                if row["initiator"] != "actions":
                    _fail("authenticated attestation API row has an unexpected initiator")
                rows.append(row)
                if len(rows) >= MAX_ATTESTATION_CANDIDATES:
                    _fail("attestation candidate cap reached; exhaustiveness is unprovable")

        bundles: list[dict[str, object]] = []
        compressed_total = 0
        decoded_total = 0
        canonical_total = 0
        for row in rows:
            bundle_url = row["bundle_url"]
            if type(bundle_url) is not str or not bundle_url:
                _fail("authenticated attestation row contains no usable bundle")
            remaining = min(
                MAX_BUNDLE_SIZE_BYTES - compressed_total,
                MAX_BUNDLE_SIZE_BYTES - decoded_total,
                MAX_BUNDLE_SIZE_BYTES - canonical_total,
            )
            if remaining <= 0:
                _fail("attestation candidate set exceeds the fixed cumulative bound")
            bundle, compressed_size, decoded_size = _download_public_attestation_bundle(
                bundle_url, max_bytes=remaining
            )
            canonical_size = len(_canonical_json(bundle)) + 1
            compressed_total += compressed_size
            decoded_total += decoded_size
            canonical_total += canonical_size
            if max(compressed_total, decoded_total, canonical_total) > MAX_BUNDLE_SIZE_BYTES:
                _fail("attestation candidate set exceeds the fixed cumulative bound")
            bundles.append(bundle)
        if not bundles:
            _fail("authenticated attestation API returned no candidates")
        return tuple(bundles)


def _snappy_varint(data: bytes, offset: int) -> tuple[int, int]:
    value = 0
    shift = 0
    for _ in range(5):
        if offset >= len(data):
            _fail("public attestation bundle has a truncated Snappy length")
        byte = data[offset]
        offset += 1
        value |= (byte & 0x7F) << shift
        if byte < 0x80:
            return value, offset
        shift += 7
    _fail("public attestation bundle has an invalid Snappy length")


def _snappy_decompress(data: bytes, *, max_bytes: int = MAX_BUNDLE_SIZE_BYTES) -> bytes:
    """Decode the raw Snappy block format emitted by GitHub's bundle store."""

    expected, offset = _snappy_varint(data, 0)
    if not 0 < expected <= max_bytes:
        _fail("public attestation bundle has an invalid decoded size")
    output = bytearray()
    while offset < len(data):
        tag = data[offset]
        offset += 1
        kind = tag & 0x03
        if kind == 0:
            encoded_length = tag >> 2
            if encoded_length < 60:
                length = encoded_length + 1
            else:
                width = encoded_length - 59
                if width > 4 or offset + width > len(data):
                    _fail("public attestation bundle has a malformed Snappy literal")
                length = int.from_bytes(data[offset : offset + width], "little") + 1
                offset += width
            if offset + length > len(data) or len(output) + length > expected:
                _fail("public attestation bundle has a truncated Snappy literal")
            output.extend(data[offset : offset + length])
            offset += length
            continue

        if kind == 1:
            length = 4 + ((tag >> 2) & 0x07)
            if offset >= len(data):
                _fail("public attestation bundle has a truncated Snappy copy")
            distance = ((tag & 0xE0) << 3) | data[offset]
            offset += 1
        elif kind == 2:
            length = 1 + (tag >> 2)
            if offset + 2 > len(data):
                _fail("public attestation bundle has a truncated Snappy copy")
            distance = int.from_bytes(data[offset : offset + 2], "little")
            offset += 2
        else:
            length = 1 + (tag >> 2)
            if offset + 4 > len(data):
                _fail("public attestation bundle has a truncated Snappy copy")
            distance = int.from_bytes(data[offset : offset + 4], "little")
            offset += 4
        if distance <= 0 or distance > len(output) or len(output) + length > expected:
            _fail("public attestation bundle has an invalid Snappy copy")
        for _ in range(length):
            output.append(output[-distance])
    if len(output) != expected:
        _fail("public attestation bundle decoded size mismatch")
    return bytes(output)


def _validated_bundle_url(value: str) -> None:
    parsed = urllib.parse.urlsplit(value)
    try:
        port = parsed.port
    except ValueError as exc:
        raise GitHubAttestationPolicyError(
            "authenticated attestation API returned an untrusted bundle URL"
        ) from exc
    if (
        parsed.scheme != "https"
        or parsed.hostname != _ATTESTATION_BUNDLE_HOST
        or port not in (None, 443)
        or parsed.username is not None
        or parsed.password is not None
        or not parsed.path.startswith("/")
        or parsed.fragment
    ):
        _fail("authenticated attestation API returned an untrusted bundle URL")


def _download_public_attestation_bundle(
    bundle_url: str, *, max_bytes: int
) -> tuple[dict[str, object], int, int]:
    """Fetch one authenticated-API-selected blob without forwarding credentials."""

    _validated_bundle_url(bundle_url)
    request = urllib.request.Request(
        bundle_url,
        headers={
            "Accept": "application/octet-stream",
            "User-Agent": "astrid-m4-6-attestation-verifier/1",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310
            final_url = response.geturl()
            _validated_bundle_url(final_url)
            declared = response.headers.get("Content-Length")
            if declared is not None:
                try:
                    declared_size = int(declared)
                except ValueError as exc:
                    raise GitHubAttestationPolicyError(
                        "public attestation bundle has an invalid content length"
                    ) from exc
                if not 0 < declared_size <= max_bytes:
                    _fail("public attestation bundle exceeds the fixed compressed bound")
            compressed = response.read(max_bytes + 1)
    except (OSError, urllib.error.URLError, ValueError) as exc:
        raise GitHubAttestationCommandError("public attestation bundle download failed") from exc
    if not 0 < len(compressed) <= max_bytes:
        _fail("public attestation bundle exceeds the fixed compressed bound")
    try:
        decoded = _snappy_decompress(compressed, max_bytes=max_bytes)
        bundle = json.loads(decoded)
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise GitHubAttestationPolicyError("public attestation bundle JSON is malformed") from exc
    if type(bundle) is not dict:
        _fail("public attestation bundle must be an object")
    return bundle, len(compressed), len(decoded)


def _fail(message: str) -> None:
    raise GitHubAttestationPolicyError(message)


def _exact_keys(value: object, keys: set[str], label: str) -> dict[str, object]:
    if type(value) is not dict:
        _fail(f"{label} must be an object")
    result = value
    if set(result) != keys:
        _fail(f"{label} has a non-closed schema")
    return result


def _list(value: object, label: str) -> list[object]:
    if type(value) is not list:
        _fail(f"{label} must be an array")
    return value


def _int(value: object, label: str) -> int:
    if type(value) is not int or value <= 0:
        _fail(f"{label} must be a positive integer")
    return value


def _string(value: object, label: str) -> str:
    if type(value) is not str or not value:
        _fail(f"{label} must be a non-empty string")
    return value


def _sha1(value: object, label: str) -> str:
    result = _string(value, label)
    if _SHA1_RE.fullmatch(result) is None:
        _fail(f"{label} must be a full lowercase SHA-1")
    return result


def _sha256(value: object, label: str) -> str:
    result = _string(value, label)
    if _SHA256_RE.fullmatch(result) is None:
        _fail(f"{label} must be a lowercase SHA-256")
    return result


def _digest(value: object, label: str) -> str:
    result = _string(value, label)
    if not result.startswith("sha256:"):
        _fail(f"{label} must use sha256")
    return _sha256(result.removeprefix("sha256:"), label)


def _parse_time(value: object, label: str) -> datetime:
    text = _string(value, label)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise GitHubAttestationPolicyError(f"{label} is not an RFC3339 timestamp") from exc
    if parsed.tzinfo is None:
        _fail(f"{label} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _validate_artifact_run_request(
    request: VerificationRequest | _PreparationRequest,
) -> None:
    _int(request.run_id, "run_id")
    _int(request.run_attempt, "run_attempt")
    _int(request.workflow_id, "workflow_id")
    if type(request.artifact_ids) is not tuple or len(request.artifact_ids) != 9:
        _fail("exactly nine immutable artifact IDs are required")
    if len(set(request.artifact_ids)) != 9:
        _fail("artifact IDs must be globally unique")
    for artifact_id in request.artifact_ids:
        _int(artifact_id, "artifact_id")
    _sha1(request.source_sha, "source_sha")
    _sha1(request.workflow_sha, "workflow_sha")
    _string(request.source_ref, "source_ref")
    _string(request.source_event, "source_event")
    if not request.workflow_path.startswith(
        ".github/workflows/"
    ) or not request.workflow_path.endswith((".yml", ".yaml")):
        _fail("workflow_path must be one exact repository workflow path")
    workflow_prefix = f"{REPOSITORY_FULL_NAME}/{request.workflow_path}@"
    if not request.workflow_ref.startswith(workflow_prefix):
        _fail("workflow_ref does not identify the expected top-level repository workflow")


def _validate_request(request: VerificationRequest) -> None:
    if type(request) is not VerificationRequest:
        _fail("an exact VerificationRequest is required")
    _validate_artifact_run_request(request)
    _int(request.checker_job_id, "checker_job_id")
    _sha1(request.signer_workflow_sha, "signer_workflow_sha")
    signer_prefix = f"{REPOSITORY_FULL_NAME}/{SIGNER_WORKFLOW_PATH}@"
    if not request.signer_workflow_ref.startswith(signer_prefix):
        _fail("signer_workflow_ref does not identify the fixed trusted attestor")


def _api_json(runner: _CommandRunner, endpoint: str) -> dict[str, object]:
    value = runner.json(
        (
            "api",
            "--hostname",
            "github.com",
            endpoint,
            "-H",
            "Accept: application/vnd.github+json",
            "-H",
            f"X-GitHub-Api-Version: {GITHUB_API_VERSION}",
        )
    )
    if type(value) is not dict:
        _fail(f"authenticated API {endpoint} did not return an object")
    return value


def _validate_repository(repository: dict[str, object]) -> None:
    if repository.get("id") != REPOSITORY_ID:
        _fail("repository numeric ID mismatch")
    if repository.get("full_name") != REPOSITORY_FULL_NAME:
        _fail("repository full name mismatch")
    owner = repository.get("owner")
    if type(owner) is not dict or owner.get("id") != REPOSITORY_OWNER_ID:
        _fail("repository owner numeric ID mismatch")
    if owner.get("login") != REPOSITORY_OWNER:
        _fail("repository owner name mismatch")
    if repository.get("private") is not False or repository.get("visibility") != "public":
        _fail("repository must be public")


def _validate_run_row(
    row: dict[str, object],
    request: VerificationRequest | _PreparationRequest | CanaryVerificationRequest,
    label: str,
    *,
    workflow_path: str | None = None,
) -> None:
    if row.get("id") != request.run_id or row.get("run_attempt") != request.run_attempt:
        _fail(f"{label} run ID/attempt mismatch")
    if row.get("workflow_id") != request.workflow_id:
        _fail(f"{label} workflow ID mismatch")
    if row.get("head_sha") != request.source_sha:
        _fail(f"{label} head SHA mismatch")
    if row.get("event") != request.source_event:
        _fail(f"{label} source event mismatch")
    expected_path = (
        workflow_path if workflow_path is not None else request.workflow_path  # type: ignore[union-attr]
    )
    if str(row.get("path", "")).split("@", 1)[0] != expected_path:
        _fail(f"{label} workflow path mismatch")
    repository = row.get("repository")
    if type(repository) is not dict:
        _fail(f"{label} repository row missing")
    if repository.get("id") != REPOSITORY_ID or repository.get("full_name") != REPOSITORY_FULL_NAME:
        _fail(f"{label} repository identity mismatch")
    head_repository = row.get("head_repository")
    if type(head_repository) is not dict or head_repository.get("id") != REPOSITORY_ID:
        _fail(f"{label} head repository mismatch")
    if request.source_ref.startswith("refs/heads/"):
        if row.get("head_branch") != request.source_ref.removeprefix("refs/heads/"):
            _fail(f"{label} source branch mismatch")


def _fetch_jobs(
    runner: _CommandRunner, request: VerificationRequest, *, checker_must_succeed: bool
) -> tuple[dict[str, object], dict[str, dict[str, object]]]:
    endpoint = (
        f"repos/{REPOSITORY_FULL_NAME}/actions/runs/{request.run_id}/attempts/"
        f"{request.run_attempt}/jobs?per_page=100"
    )
    response = _api_json(runner, endpoint)
    jobs = _list(response.get("jobs"), "attempt jobs")
    if response.get("total_count") != len(jobs):
        _fail("attempt jobs response was truncated or inconsistent")
    objects: list[dict[str, object]] = []
    for job in jobs:
        if type(job) is not dict:
            _fail("attempt job row must be an object")
        if job.get("run_id") not in (None, request.run_id):
            _fail("job belongs to another run")
        if job.get("run_attempt") not in (None, request.run_attempt):
            _fail("job belongs to another run attempt")
        objects.append(job)
    checker_matches = [
        job
        for job in objects
        if job.get("id") == request.checker_job_id and job.get("name") == CHECKER_JOB_NAME
    ]
    if len(checker_matches) != 1:
        _fail("exact current checker job ID/name was not found in the attempt")
    checker = checker_matches[0]
    if checker.get("status") not in {"queued", "in_progress", "completed", "waiting", "pending"}:
        _fail("checker job status is not recognized")
    if checker_must_succeed and (
        checker.get("status") != "completed" or checker.get("conclusion") != "success"
    ):
        _fail("later admission requires the checker job completed/success")

    expected_names = {row.producer_job_name for row in TRUSTED_SLOT_REGISTRY}
    unexpected_producers = [
        job
        for job in objects
        if str(job.get("name", "")).startswith("m4-6-producer-")
        and job.get("name") not in expected_names
    ]
    if unexpected_producers:
        _fail("unexpected producer job exists in the exact attempt")
    producers: dict[str, dict[str, object]] = {}
    seen_ids: set[int] = set()
    for name in expected_names:
        matches = [job for job in objects if job.get("name") == name]
        if len(matches) != 1:
            _fail(f"producer job {name} is missing or ambiguous")
        job = matches[0]
        job_id = _int(job.get("id"), f"producer job {name} ID")
        if job_id in seen_ids:
            _fail("producer job IDs must be globally unique")
        seen_ids.add(job_id)
        if job.get("status") != "completed" or job.get("conclusion") != "success":
            _fail(f"producer job {name} did not complete successfully")
        producers[name] = job
    return checker, producers


def _prepare_custody_root(root: Path) -> Path:
    if type(root) is not _PATH_TYPE:
        _fail("download_root must be an exact pathlib.Path")
    root.mkdir(mode=0o700, parents=True, exist_ok=False)
    mode = root.lstat().st_mode
    if not stat.S_ISDIR(mode) or stat.S_ISLNK(mode):
        _fail("download_root must be a new private directory")
    root.chmod(0o700)
    return root.resolve(strict=True)


def _hash_file(path: Path) -> tuple[str, int]:
    mode = path.lstat().st_mode
    if not stat.S_ISREG(mode) or stat.S_ISLNK(mode):
        _fail("immutable-ID download is not a regular file")
    size = path.stat().st_size
    if size <= 0 or size > MAX_ARTIFACT_SIZE_BYTES:
        _fail("downloaded artifact violates the fixed 64 MiB bound")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest(), size


@dataclass(frozen=True, slots=True)
class _ArtifactObservation:
    row: dict[str, object]
    path: Path
    sha256: str
    size: int
    expires_at: str


def _fetch_and_download_artifacts(
    runner: _CommandRunner,
    request: VerificationRequest | _PreparationRequest,
    custody_root: Path,
) -> tuple[_ArtifactObservation, ...]:
    list_endpoint = (
        f"repos/{REPOSITORY_FULL_NAME}/actions/runs/{request.run_id}/artifacts?per_page=100"
    )
    listing = _api_json(runner, list_endpoint)
    listed = _list(listing.get("artifacts"), "run artifacts")
    if listing.get("total_count") != len(listed):
        _fail("run artifact response was truncated or inconsistent")
    if len(listed) != 9:
        _fail("run must expose exactly the nine fixed artifacts")
    if any(type(row) is not dict for row in listed):
        _fail("run artifact row must be an object")
    listed_ids = [row.get("id") for row in listed if type(row) is dict]
    listed_names = [row.get("name") for row in listed if type(row) is dict]
    if len(set(listed_ids)) != 9 or len(set(listed_names)) != 9:
        _fail("run artifact IDs and names must be globally unique")
    if set(listed_ids) != set(request.artifact_ids):
        _fail("run artifact IDs do not equal the immutable selected set")
    if set(listed_names) != {row.artifact_name for row in TRUSTED_SLOT_REGISTRY}:
        _fail("run artifact names do not equal the fixed registry")

    now = datetime.now(timezone.utc)
    observations: list[_ArtifactObservation] = []
    for expected, artifact_id in zip(TRUSTED_SLOT_REGISTRY, request.artifact_ids, strict=True):
        endpoint = f"repos/{REPOSITORY_FULL_NAME}/actions/artifacts/{artifact_id}"
        row = _api_json(runner, endpoint)
        if row.get("id") != artifact_id or row.get("name") != expected.artifact_name:
            _fail("immutable artifact ID/name mismatch")
        if row.get("expired") is not False:
            _fail("artifact is expired")
        api_size = _int(row.get("size_in_bytes"), "artifact API size")
        if api_size > MAX_ARTIFACT_SIZE_BYTES:
            _fail("artifact API size violates the fixed 64 MiB bound")
        api_digest = _digest(row.get("digest"), "artifact API digest")
        expires_at = _string(row.get("expires_at"), "artifact expiry")
        if _parse_time(expires_at, "artifact expiry") <= now:
            _fail("artifact expiry is not in the future")
        workflow_run = row.get("workflow_run")
        if type(workflow_run) is not dict:
            _fail("artifact workflow_run association is missing")
        if workflow_run.get("id") != request.run_id:
            _fail("artifact belongs to another workflow run")
        if workflow_run.get("repository_id") != REPOSITORY_ID:
            _fail("artifact repository ID mismatch")
        if workflow_run.get("head_repository_id") != REPOSITORY_ID:
            _fail("artifact head repository ID mismatch")
        if workflow_run.get("head_sha") != request.source_sha:
            _fail("artifact head SHA mismatch")

        listed_match = [item for item in listed if item.get("id") == artifact_id]
        if len(listed_match) != 1 or listed_match[0] != row:
            _fail("artifact detail differs from the authenticated run listing")
        path = custody_root / f"{artifact_id}.zip"
        runner.download(
            (
                "api",
                "--hostname",
                "github.com",
                endpoint + "/zip",
                "-H",
                "Accept: application/vnd.github+json",
                "-H",
                f"X-GitHub-Api-Version: {GITHUB_API_VERSION}",
            ),
            path,
        )
        downloaded_digest, downloaded_size = _hash_file(path)
        if downloaded_size != api_size or downloaded_digest != api_digest:
            _fail("immutable-ID downloaded bytes disagree with artifact API digest/size")
        observations.append(
            _ArtifactObservation(
                row=row,
                path=path,
                sha256=downloaded_digest,
                size=downloaded_size,
                expires_at=expires_at,
            )
        )
    return tuple(observations)


def _write_exhaustive_attestation_bundle(
    runner: _CommandRunner,
    subjects: tuple[tuple[str, Path], ...],
    directory: Path,
    predicate_type: str,
) -> Path:
    """Download one bounded-complete candidate set through GitHub's CLI client.

    ``gh attestation download`` follows GitHub's cursor pagination and safely
    dereferences the API's bundle URLs. The CLI's documented maximum is 1,000;
    reaching it is rejected because exhaustiveness can no longer be proven.
    Below that cap, duplicate rows are rejected per subject and the union is
    given to ``gh attestation verify``.
    """

    unique_subjects: dict[str, Path] = {}
    for digest, subject in subjects:
        _sha256(digest, "attestation subject digest")
        if type(subject) is not _PATH_TYPE:
            _fail("attestation subject path must be an exact pathlib.Path")
        unique_subjects.setdefault(digest, subject)
    bundles: dict[str, dict[str, object]] = {}
    for index, (digest, subject) in enumerate(unique_subjects.items(), start=1):
        subject_hashes: set[str] = set()
        rows = runner.attestation_bundles(
            subject,
            directory / f"subject-{index}-{digest}",
            predicate_type,
        )
        if len(rows) >= MAX_ATTESTATION_CANDIDATES:
            _fail("attestation candidate cap reached; exhaustiveness is unprovable")
        for bundle in rows:
            canonical = _canonical_json(bundle)
            bundle_hash = hashlib.sha256(canonical).hexdigest()
            if bundle_hash in subject_hashes:
                _fail("duplicate attestation bundle exists for one subject")
            subject_hashes.add(bundle_hash)
            bundles.setdefault(bundle_hash, bundle)

    path = directory / "authenticated-attestations.jsonl"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    total = 0
    try:
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            for bundle_hash in sorted(bundles):
                line = _canonical_json(bundles[bundle_hash]) + b"\n"
                total += len(line)
                if total > MAX_BUNDLE_SIZE_BYTES:
                    _fail("exhaustive attestation candidate set exceeds the fixed bound")
                stream.write(line)
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    if total == 0:
        _fail("exhaustive attestation candidate set is empty")
    return path.resolve(strict=True)


def _workflow_path_from_ref(workflow_ref: str) -> str:
    prefix = f"{REPOSITORY_FULL_NAME}/"
    if not workflow_ref.startswith(prefix) or "@" not in workflow_ref:
        _fail("workflow ref must identify one exact workflow in the fixed repository")
    path, ref = workflow_ref.removeprefix(prefix).rsplit("@", 1)
    if not path.startswith(".github/workflows/") or not path.endswith((".yml", ".yaml")):
        _fail("workflow ref contains an invalid workflow path")
    if not ref:
        _fail("workflow ref must contain an exact ref")
    return path


@dataclass(frozen=True, slots=True)
class _UploadBinding:
    policy: SlotPolicy
    artifact_id: int
    artifact_digest: str


def _parse_upload_bindings(values: tuple[str, ...], run_attempt: int) -> tuple[_UploadBinding, ...]:
    if len(values) != 9:
        _fail("prepare-predicate requires exactly nine ordered upload bindings")
    parsed: list[_UploadBinding] = []
    seen_ids: set[int] = set()
    for index, (policy, value) in enumerate(zip(TRUSTED_SLOT_REGISTRY, values, strict=True)):
        parts = value.split("|")
        if len(parts) != 7:
            _fail(f"binding {index} must contain exactly seven pipe-delimited fields")
        expected_slot, expected_job, reported_slot, name, raw_id, raw_digest, raw_attempt = parts
        if expected_slot != policy.slot or reported_slot != policy.slot:
            _fail(f"binding {index} slot differs from the fixed registry")
        if expected_job != policy.producer_job_name or name != policy.artifact_name:
            _fail(f"binding {index} job/artifact name differs from the fixed registry")
        try:
            artifact_id = int(raw_id)
            reported_attempt = int(raw_attempt)
        except ValueError as exc:
            raise GitHubAttestationPolicyError(
                f"binding {index} contains a non-integer runtime ID/attempt"
            ) from exc
        _int(artifact_id, f"binding {index} artifact ID")
        if artifact_id in seen_ids:
            _fail("upload binding artifact IDs must be globally unique")
        seen_ids.add(artifact_id)
        if reported_attempt != run_attempt:
            _fail(f"binding {index} reports a stale run attempt")
        digest = raw_digest.removeprefix("sha256:")
        _sha256(digest, f"binding {index} artifact digest")
        parsed.append(
            _UploadBinding(policy=policy, artifact_id=artifact_id, artifact_digest=digest)
        )
    return tuple(parsed)


def _fetch_prepare_producers(
    runner: _CommandRunner, request: _PreparationRequest
) -> dict[str, dict[str, object]]:
    endpoint = (
        f"repos/{REPOSITORY_FULL_NAME}/actions/runs/{request.run_id}/attempts/"
        f"{request.run_attempt}/jobs?per_page=100"
    )
    response = _api_json(runner, endpoint)
    jobs = _list(response.get("jobs"), "attempt jobs")
    if response.get("total_count") != len(jobs):
        _fail("attempt jobs response was truncated or inconsistent")
    producers: dict[str, dict[str, object]] = {}
    seen_ids: set[int] = set()
    expected_names = {policy.producer_job_name for policy in TRUSTED_SLOT_REGISTRY}
    for job in jobs:
        if type(job) is not dict:
            _fail("attempt job row must be an object")
        if job.get("run_id") not in (None, request.run_id):
            _fail("producer job belongs to another run")
        if job.get("run_attempt") not in (None, request.run_attempt):
            _fail("producer job belongs to another run attempt")
        name = job.get("name")
        if str(name).startswith("m4-6-producer-") and name not in expected_names:
            _fail("unexpected producer job exists in the exact attempt")
        if name in expected_names:
            if name in producers:
                _fail(f"producer job {name} is ambiguous")
            job_id = _int(job.get("id"), f"producer job {name} ID")
            if job_id in seen_ids:
                _fail("producer job IDs must be globally unique")
            seen_ids.add(job_id)
            if job.get("status") != "completed" or job.get("conclusion") != "success":
                _fail(f"producer job {name} did not complete successfully")
            producers[str(name)] = job
    if set(producers) != expected_names:
        _fail("exact producer job set is incomplete")
    return producers


def prepare_trusted_predicate(
    *,
    runner: _CommandRunner,
    run_id: int,
    run_attempt: int,
    source_sha: str,
    source_ref: str,
    source_event: str,
    workflow_ref: str,
    workflow_sha: str,
    bindings: tuple[str, ...],
    predicate_output: Path,
    subjects_output: Path,
) -> None:
    """Reconcile producer outputs and write the closed exact-nine predicate."""

    workflow_path = _workflow_path_from_ref(workflow_ref)
    parsed_bindings = _parse_upload_bindings(bindings, run_attempt)
    run = _api_json(runner, f"repos/{REPOSITORY_FULL_NAME}/actions/runs/{run_id}")
    workflow_id = _int(run.get("workflow_id"), "workflow run workflow ID")
    request = _PreparationRequest(
        run_id=run_id,
        run_attempt=run_attempt,
        artifact_ids=tuple(binding.artifact_id for binding in parsed_bindings),
        source_sha=source_sha,
        source_ref=source_ref,
        source_event=source_event,
        workflow_id=workflow_id,
        workflow_path=workflow_path,
        workflow_ref=workflow_ref,
        workflow_sha=workflow_sha,
    )
    _validate_artifact_run_request(request)
    repository = _api_json(runner, f"repos/{REPOSITORY_FULL_NAME}")
    _validate_repository(repository)
    _validate_run_row(run, request, "workflow run")
    attempt = _api_json(
        runner,
        f"repos/{REPOSITORY_FULL_NAME}/actions/runs/{run_id}/attempts/{run_attempt}",
    )
    _validate_run_row(attempt, request, "workflow run attempt")
    producers = _fetch_prepare_producers(runner, request)
    with tempfile.TemporaryDirectory(prefix="astrid-m4-6-attestor-") as temporary:
        custody_root = _prepare_custody_root(Path(temporary) / "immutable-artifacts")
        observations = _fetch_and_download_artifacts(runner, request, custody_root)
        artifact_rows = []
        subject_lines = []
        for binding, observation in zip(parsed_bindings, observations, strict=True):
            if binding.artifact_digest != observation.sha256:
                _fail("upload output digest differs from API/downloaded immutable artifact")
            producer = producers[binding.policy.producer_job_name]
            artifact_rows.append(
                {
                    "slot": binding.policy.slot,
                    "matrix": None,
                    "producerJob": {
                        "id": producer["id"],
                        "name": binding.policy.producer_job_name,
                        "status": "completed",
                        "conclusion": "success",
                    },
                    "artifact": {
                        "id": binding.artifact_id,
                        "name": binding.policy.artifact_name,
                        "digest": f"sha256:{observation.sha256}",
                        "sizeBytes": observation.size,
                    },
                }
            )
            subject_lines.append(f"{observation.sha256}  {binding.policy.artifact_name}\n")
    predicate = {
        "version": PREDICATE_VERSION,
        "registryDigest": REGISTRY_DIGEST,
        "repository": {
            "id": REPOSITORY_ID,
            "fullName": REPOSITORY_FULL_NAME,
            "ownerId": REPOSITORY_OWNER_ID,
            "ownerName": REPOSITORY_OWNER,
        },
        "workflow": {
            "id": workflow_id,
            "path": workflow_path,
            "ref": workflow_ref,
            "sha": workflow_sha,
        },
        "run": {
            "id": run_id,
            "attempt": run_attempt,
            "headSha": source_sha,
            "sourceRef": source_ref,
            "event": source_event,
        },
        "artifacts": artifact_rows,
    }
    _write_new_file(predicate_output, _canonical_json(predicate) + b"\n")
    _write_new_file(subjects_output, "".join(subject_lines).encode("utf-8"))


def _write_new_file(path: Path, content: bytes) -> None:
    if type(path) is not _PATH_TYPE:
        _fail("output path must be an exact pathlib.Path")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(content)


def _verify_arguments(
    subject: Path, bundle_path: Path, request: VerificationRequest
) -> tuple[str, ...]:
    return (
        "attestation",
        "verify",
        str(subject),
        "--hostname",
        "github.com",
        "--repo",
        REPOSITORY_FULL_NAME,
        "--bundle",
        str(bundle_path),
        "--signer-workflow",
        f"{REPOSITORY_FULL_NAME}/{SIGNER_WORKFLOW_PATH}",
        "--signer-digest",
        request.signer_workflow_sha,
        "--source-digest",
        request.source_sha,
        "--source-ref",
        request.source_ref,
        "--cert-oidc-issuer",
        OIDC_ISSUER,
        "--deny-self-hosted-runners",
        "--predicate-type",
        PREDICATE_TYPE,
        "--format",
        "json",
    )


def _optional_audience(certificate: dict[str, object]) -> None:
    represented = [certificate[key] for key in ("audience", "oidcAudience") if key in certificate]
    for value in represented:
        if value == OIDC_AUDIENCE:
            continue
        if type(value) is list and value == [OIDC_AUDIENCE]:
            continue
        _fail("certificate OIDC audience mismatch")


def _certificate_policy(request: VerificationRequest) -> _CertificatePolicy:
    return _CertificatePolicy(
        run_id=request.run_id,
        run_attempt=request.run_attempt,
        source_sha=request.source_sha,
        source_ref=request.source_ref,
        source_event=request.source_event,
        workflow_ref=request.workflow_ref,
        workflow_sha=request.workflow_sha,
        signer_workflow_ref=request.signer_workflow_ref,
        signer_workflow_sha=request.signer_workflow_sha,
    )


def _validate_certificate(certificate: dict[str, object], request: _CertificatePolicy) -> None:
    expected = {
        "issuer": OIDC_ISSUER,
        "sourceRepositoryURI": REPOSITORY_URI,
        "sourceRepositoryIdentifier": str(REPOSITORY_ID),
        "sourceRepositoryOwnerURI": REPOSITORY_OWNER_URI,
        "sourceRepositoryOwnerIdentifier": str(REPOSITORY_OWNER_ID),
        "sourceRepositoryVisibilityAtSigning": "public",
        "sourceRepositoryDigest": request.source_sha,
        "sourceRepositoryRef": request.source_ref,
        "buildTrigger": request.source_event,
        "runnerEnvironment": "github-hosted",
        "buildSignerURI": f"https://github.com/{request.signer_workflow_ref}",
        "buildSignerDigest": request.signer_workflow_sha,
        "buildConfigURI": f"https://github.com/{request.workflow_ref}",
        "buildConfigDigest": request.workflow_sha,
        "runInvocationURI": (
            f"{REPOSITORY_URI}/actions/runs/{request.run_id}/attempts/{request.run_attempt}"
        ),
    }
    for field, expected_value in expected.items():
        if certificate.get(field) != expected_value:
            _fail(f"certificate {field} mismatch")
    # gh v2.86.0 serializes sigstore-go certificate.Summary here.  Its SAN is
    # one string, not the protobuf-style {type,value} object used in bundles.
    san = certificate.get("subjectAlternativeName")
    if type(san) is not str or san != expected["buildSignerURI"]:
        _fail("certificate SAN does not equal the signer workflow identity")
    optional_aliases = {
        "githubWorkflowSHA": request.source_sha,
        "githubWorkflowRepository": REPOSITORY_FULL_NAME,
        "githubWorkflowRef": request.source_ref,
        "githubWorkflowTrigger": request.source_event,
    }
    for field, expected_value in optional_aliases.items():
        if field in certificate and certificate[field] != expected_value:
            _fail(f"certificate {field} mismatch")
    _optional_audience(certificate)


def _verified_bundle(attestation: dict[str, object]) -> dict[str, object]:
    """Return the raw bundle from gh's api.Attestation JSON wrapper."""

    if set(attestation) != {"bundle", "bundle_url", "initiator"}:
        _fail("gh attestation wrapper has an unexpected schema")
    if type(attestation.get("bundle_url")) is not str:
        _fail("gh attestation bundle URL must be a string")
    if type(attestation.get("initiator")) is not str:
        _fail("gh attestation initiator must be a string")
    bundle = attestation.get("bundle")
    if type(bundle) is not dict:
        _fail("gh attestation wrapper omitted the Sigstore bundle")
    return bundle


def _validate_timestamp_evidence(bundle: dict[str, object], result: dict[str, object]) -> None:
    """Require gh-verified Rekor evidence or a trusted RFC3161 timestamp."""

    timestamps = _list(result.get("verifiedTimestamps"), "verified timestamps")
    tlog_timestamps = []
    authority_timestamps = []
    for timestamp in timestamps:
        if type(timestamp) is not dict:
            _fail("verified timestamp row must be an object")
        timestamp_type = timestamp.get("type")
        if timestamp_type not in ("Tlog", "TimestampAuthority"):
            _fail("verified timestamp has an untrusted type")
        _parse_time(timestamp.get("timestamp"), "verified witness timestamp")
        _string(timestamp.get("uri"), "verified witness URI")
        if timestamp_type == "Tlog":
            tlog_timestamps.append(timestamp)
        else:
            authority_timestamps.append(timestamp)
    if not tlog_timestamps and not authority_timestamps:
        _fail("a verified transparency-log or trusted-authority timestamp is required")

    material = bundle.get("verificationMaterial")
    if type(material) is not dict:
        _fail("Sigstore verification material is missing")
    if tlog_timestamps:
        entries = _list(material.get("tlogEntries"), "Sigstore transparency entries")
        if not entries:
            _fail("the bundle contains no transparency-log entry")
        for entry in entries:
            if type(entry) is not dict:
                _fail("transparency-log entry must be an object")
            try:
                integrated_time = int(entry.get("integratedTime", 0))
            except (TypeError, ValueError) as exc:
                raise GitHubAttestationPolicyError(
                    "transparency-log integrated time is invalid"
                ) from exc
            if integrated_time <= 0:
                _fail("transparency-log integrated time is missing")
            promise = entry.get("inclusionPromise")
            proof = entry.get("inclusionProof")
            has_promise = type(promise) is dict and bool(promise.get("signedEntryTimestamp"))
            has_proof = type(proof) is dict and bool(proof)
            if not has_promise and not has_proof:
                _fail("transparency-log inclusion promise/proof is missing")
    if authority_timestamps:
        timestamp_data = material.get("timestampVerificationData")
        if type(timestamp_data) is not dict:
            _fail("trusted timestamp verification material is missing")
        signed_timestamps = _list(
            timestamp_data.get("rfc3161Timestamps"),
            "Sigstore RFC3161 timestamps",
        )
        if not signed_timestamps or any(type(value) is not dict for value in signed_timestamps):
            _fail("the bundle contains no valid RFC3161 timestamp envelope")


def _cryptographically_verify(
    runner: _CommandRunner,
    bundle_path: Path,
    observations: tuple[_ArtifactObservation, ...],
    request: VerificationRequest,
) -> tuple[dict[str, object], str]:
    results: list[dict[str, object]] = []
    bundle_digests: set[str] = set()
    for observation in observations:
        output = runner.json(_verify_arguments(observation.path, bundle_path, request))
        candidates = _list(output, "gh verified attestations")
        if len(candidates) != 1:
            _fail("exactly one acceptable attestation is required for every subject")
        candidate = candidates[0]
        if type(candidate) is not dict:
            _fail("gh verified attestation result must be an object")
        if set(candidate) != {"attestation", "verificationResult"}:
            _fail("gh verified attestation result has an unexpected schema")
        attestation = candidate.get("attestation")
        result = candidate.get("verificationResult")
        if type(attestation) is not dict or type(result) is not dict:
            _fail("gh verification result omitted attestation material")
        bundle = _verified_bundle(attestation)
        signature = result.get("signature")
        if type(signature) is not dict or type(signature.get("certificate")) is not dict:
            _fail("gh verification result omitted the verified certificate")
        _validate_certificate(signature["certificate"], _certificate_policy(request))
        _validate_timestamp_evidence(bundle, result)
        bundle_digests.add(hashlib.sha256(_canonical_json(bundle)).hexdigest())
        results.append(result)
    if len(bundle_digests) != 1:
        _fail("subjects were verified by different or ambiguous attestations")
    canonical_results = {_canonical_json(result) for result in results}
    if len(canonical_results) != 1:
        _fail("subjects did not resolve to one identical verification result")
    return results[0], bundle_digests.pop()


def _validate_statement_and_predicate(
    result: dict[str, object],
    request: VerificationRequest,
    observations: tuple[_ArtifactObservation, ...],
    checker: dict[str, object],
    producers: dict[str, dict[str, object]],
) -> tuple[VerifiedArtifactFacts, ...]:
    statement = result.get("statement")
    if type(statement) is not dict:
        _fail("verified in-toto statement is missing")
    if statement.get("_type") != "https://in-toto.io/Statement/v1":
        _fail("in-toto statement type mismatch")
    if statement.get("predicateType") != PREDICATE_TYPE:
        _fail("predicate type mismatch")
    subjects = _list(statement.get("subject"), "attestation subjects")
    if len(subjects) != 9:
        _fail("attestation must contain exactly nine subjects")

    predicate = _exact_keys(
        statement.get("predicate"),
        {"version", "registryDigest", "repository", "workflow", "run", "artifacts"},
        "trusted predicate",
    )
    if predicate["version"] != PREDICATE_VERSION:
        _fail("trusted predicate version mismatch")
    if predicate["registryDigest"] != REGISTRY_DIGEST:
        _fail("trusted predicate registry digest mismatch")
    repository = _exact_keys(
        predicate["repository"], {"id", "fullName", "ownerId", "ownerName"}, "predicate repository"
    )
    if repository != {
        "id": REPOSITORY_ID,
        "fullName": REPOSITORY_FULL_NAME,
        "ownerId": REPOSITORY_OWNER_ID,
        "ownerName": REPOSITORY_OWNER,
    }:
        _fail("predicate repository identity mismatch")
    workflow = _exact_keys(
        predicate["workflow"], {"id", "path", "ref", "sha"}, "predicate workflow"
    )
    if workflow != {
        "id": request.workflow_id,
        "path": request.workflow_path,
        "ref": request.workflow_ref,
        "sha": request.workflow_sha,
    }:
        _fail("predicate workflow identity mismatch")
    run = _exact_keys(
        predicate["run"], {"id", "attempt", "headSha", "sourceRef", "event"}, "predicate run"
    )
    if run != {
        "id": request.run_id,
        "attempt": request.run_attempt,
        "headSha": request.source_sha,
        "sourceRef": request.source_ref,
        "event": request.source_event,
    }:
        _fail("predicate run identity mismatch")
    artifact_assertions = _list(predicate["artifacts"], "predicate artifacts")
    if len(artifact_assertions) != 9:
        _fail("predicate must contain exactly nine artifact rows")

    facts: list[VerifiedArtifactFacts] = []
    producer_ids: set[int] = set()
    artifact_ids: set[int] = set()
    artifact_names: set[str] = set()
    subject_names: set[str] = set()
    for index, (policy, assertion, subject, observation) in enumerate(
        zip(TRUSTED_SLOT_REGISTRY, artifact_assertions, subjects, observations, strict=True)
    ):
        assertion_row = _exact_keys(
            assertion,
            {"slot", "matrix", "producerJob", "artifact"},
            f"predicate artifact row {index}",
        )
        if assertion_row["slot"] != policy.slot or assertion_row["matrix"] is not None:
            _fail("predicate slot order/matrix differs from the fixed registry")
        producer_assertion = _exact_keys(
            assertion_row["producerJob"],
            {"id", "name", "status", "conclusion"},
            f"predicate producer row {index}",
        )
        producer = producers[policy.producer_job_name]
        expected_producer = {
            "id": producer.get("id"),
            "name": policy.producer_job_name,
            "status": "completed",
            "conclusion": "success",
        }
        if producer_assertion != expected_producer:
            _fail("predicate producer assertion disagrees with exact-attempt jobs API")
        producer_id = _int(producer.get("id"), "producer job ID")
        if producer_id in producer_ids:
            _fail("predicate producer job IDs are not unique")
        producer_ids.add(producer_id)

        artifact_assertion = _exact_keys(
            assertion_row["artifact"],
            {"id", "name", "digest", "sizeBytes"},
            f"predicate artifact identity {index}",
        )
        expected_artifact = {
            "id": observation.row.get("id"),
            "name": policy.artifact_name,
            "digest": f"sha256:{observation.sha256}",
            "sizeBytes": observation.size,
        }
        if artifact_assertion != expected_artifact:
            _fail("predicate artifact assertion disagrees with API/downloaded bytes")
        artifact_id = _int(artifact_assertion["id"], "predicate artifact ID")
        if artifact_id in artifact_ids or policy.artifact_name in artifact_names:
            _fail("predicate artifact IDs/names are not unique")
        artifact_ids.add(artifact_id)
        artifact_names.add(policy.artifact_name)

        subject_row = _exact_keys(subject, {"name", "digest"}, f"subject {index}")
        subject_digest = _exact_keys(subject_row["digest"], {"sha256"}, f"subject digest {index}")
        if subject_row["name"] != policy.artifact_name:
            _fail("subject order/name differs from fixed registry")
        if subject_digest["sha256"] != observation.sha256:
            _fail("subject digest differs from immutable-ID downloaded bytes")
        if policy.artifact_name in subject_names:
            _fail("attestation subjects are not unique")
        subject_names.add(policy.artifact_name)

        facts.append(
            VerifiedArtifactFacts(
                slot=policy.slot,
                repository_id=REPOSITORY_ID,
                repository_full_name=REPOSITORY_FULL_NAME,
                workflow_id=request.workflow_id,
                workflow_path=request.workflow_path,
                workflow_ref=request.workflow_ref,
                workflow_sha=request.workflow_sha,
                run_id=request.run_id,
                run_attempt=request.run_attempt,
                head_sha=request.source_sha,
                producer_job_id=producer_id,
                producer_job_name=policy.producer_job_name,
                producer_job_status="completed",
                producer_job_conclusion="success",
                checker_job_id=request.checker_job_id,
                checker_job_name=CHECKER_JOB_NAME,
                checker_job_status=_string(checker.get("status"), "checker status"),
                checker_job_conclusion=(
                    checker.get("conclusion") if type(checker.get("conclusion")) is str else None
                ),
                artifact_id=artifact_id,
                artifact_name=policy.artifact_name,
                api_digest=f"sha256:{observation.sha256}",
                artifact_size_bytes=observation.size,
                artifact_expired=False,
                artifact_expires_at=observation.expires_at,
                downloaded_artifact_id=artifact_id,
                downloaded_sha256=observation.sha256,
                downloaded_size_bytes=observation.size,
                downloaded_path=observation.path,
            )
        )
    return tuple(facts)


def _checked_output_digest(artifacts: tuple[VerifiedArtifactFacts, ...]) -> str:
    document = [
        {
            "artifactId": row.artifact_id,
            "artifactName": row.artifact_name,
            "archiveSha256": row.downloaded_sha256,
            "archiveSizeBytes": row.downloaded_size_bytes,
            "producerJobId": row.producer_job_id,
            "producerJobName": row.producer_job_name,
            "slot": row.slot,
        }
        for row in artifacts
    ]
    return hashlib.sha256(_canonical_json(document)).hexdigest()


def _to_provenance_batch(
    request: VerificationRequest,
    artifacts: tuple[VerifiedArtifactFacts, ...],
    custody_root: Path,
) -> object:
    """Build the exact private provenance payload only after full reconciliation."""

    from scripts import m4_6_artifact_provenance as provenance

    slots = tuple(
        provenance._FrozenSlotBinding(
            slot_id=row.slot,
            producer_job_id=row.producer_job_id,
            producer_job_name=row.producer_job_name,
            artifact_id=row.artifact_id,
            artifact_name=row.artifact_name,
        )
        for row in artifacts
    )
    frozen = provenance._FrozenRunBinding(
        repository_id=REPOSITORY_ID,
        repository_full_name=REPOSITORY_FULL_NAME,
        workflow_id=request.workflow_id,
        workflow_ref=request.workflow_ref,
        workflow_sha=request.workflow_sha,
        run_id=request.run_id,
        run_attempt=request.run_attempt,
        head_sha=request.source_sha,
        checker_job_id=request.checker_job_id,
        checker_job_name=CHECKER_JOB_NAME,
        slots=slots,
    )
    checker = provenance._AuthenticatedCheckerIdentity(
        identity_source="github-actions-oidc+workflow-jobs-api",
        repository_id=REPOSITORY_ID,
        repository_full_name=REPOSITORY_FULL_NAME,
        workflow_id=request.workflow_id,
        workflow_ref=request.workflow_ref,
        workflow_sha=request.workflow_sha,
        run_id=request.run_id,
        run_attempt=request.run_attempt,
        head_sha=request.source_sha,
        checker_job_id=request.checker_job_id,
        checker_job_name=CHECKER_JOB_NAME,
    )
    metadata = tuple(
        provenance._AuthenticatedArtifactMetadata(
            metadata_source="github-actions-artifacts-api",
            download_source="github-rest-api-immutable-artifact-id",
            repository_id=REPOSITORY_ID,
            repository_full_name=REPOSITORY_FULL_NAME,
            workflow_id=request.workflow_id,
            workflow_ref=request.workflow_ref,
            workflow_sha=request.workflow_sha,
            run_id=request.run_id,
            run_attempt=request.run_attempt,
            head_sha=request.source_sha,
            artifact_id=row.artifact_id,
            artifact_name=row.artifact_name,
            artifact_expired=row.artifact_expired,
            downloaded_artifact_id=row.downloaded_artifact_id,
            downloaded_archive_sha256=row.downloaded_sha256,
            downloaded_archive_size_bytes=row.downloaded_size_bytes,
        )
        for row in artifacts
    )
    origins = tuple(
        provenance._AuthenticatedArtifactOrigin(
            origin_join_source=provenance._ORIGIN_JOIN_SOURCE,
            repository_id=REPOSITORY_ID,
            repository_full_name=REPOSITORY_FULL_NAME,
            workflow_id=request.workflow_id,
            workflow_ref=request.workflow_ref,
            workflow_sha=request.workflow_sha,
            run_id=request.run_id,
            run_attempt=request.run_attempt,
            head_sha=request.source_sha,
            producer_job_id=row.producer_job_id,
            producer_job_name=row.producer_job_name,
            producer_job_status=row.producer_job_status,
            producer_job_conclusion=row.producer_job_conclusion,
            slot_id=row.slot,
            artifact_id=row.artifact_id,
            artifact_name=row.artifact_name,
        )
        for row in artifacts
    )
    return provenance._ArtifactCheckBatch(
        frozen=frozen,
        checker=checker,
        artifacts=metadata,
        origin_joins=origins,
        archive_root=custody_root,
    )


def _verify(
    request: VerificationRequest,
    download_root: Path,
    *,
    checker_must_succeed: bool,
    runner: _CommandRunner,
) -> _VerifiedBatch:
    _validate_request(request)

    repository = _api_json(runner, f"repos/{REPOSITORY_FULL_NAME}")
    _validate_repository(repository)
    run = _api_json(runner, f"repos/{REPOSITORY_FULL_NAME}/actions/runs/{request.run_id}")
    _validate_run_row(run, request, "workflow run")
    attempt = _api_json(
        runner,
        (
            f"repos/{REPOSITORY_FULL_NAME}/actions/runs/{request.run_id}/attempts/"
            f"{request.run_attempt}"
        ),
    )
    _validate_run_row(attempt, request, "workflow run attempt")
    checker, producers = _fetch_jobs(runner, request, checker_must_succeed=checker_must_succeed)
    custody_root = _prepare_custody_root(download_root)
    observations = _fetch_and_download_artifacts(runner, request, custody_root)
    with tempfile.TemporaryDirectory(prefix="astrid-m4-6-attestation-candidates-") as temporary:
        exhaustive_bundle = _write_exhaustive_attestation_bundle(
            runner,
            tuple((observation.sha256, observation.path) for observation in observations),
            Path(temporary),
            PREDICATE_TYPE,
        )
        result, bundle_sha256 = _cryptographically_verify(
            runner,
            exhaustive_bundle,
            observations,
            request,
        )
    artifacts = _validate_statement_and_predicate(result, request, observations, checker, producers)
    provenance_batch = _to_provenance_batch(request, artifacts, custody_root)
    from scripts import m4_6_artifact_provenance as provenance

    # This byte read occurs only after all nine certificate, API, predicate,
    # registry, size, expiry, and digest checks have passed.  It does not demand
    # the current checker job's later terminal status.
    checked = provenance._check_authenticated_batch(provenance_batch)
    checked_output_sha256 = provenance._checked_output_sha256(checked)
    summary = VerifiedAttestationSummary(
        repository_id=REPOSITORY_ID,
        repository_full_name=REPOSITORY_FULL_NAME,
        workflow_id=request.workflow_id,
        workflow_path=request.workflow_path,
        workflow_ref=request.workflow_ref,
        workflow_sha=request.workflow_sha,
        run_id=request.run_id,
        run_attempt=request.run_attempt,
        head_sha=request.source_sha,
        checker_job_id=request.checker_job_id,
        checker_job_name=CHECKER_JOB_NAME,
        bundle_sha256=bundle_sha256,
        registry_digest=REGISTRY_DIGEST,
        checked_output_sha256=checked_output_sha256,
        artifacts=artifacts,
    )
    minted = time.monotonic()
    return _VerifiedBatch(
        summary=summary,
        custody_root=custody_root,
        provenance_batch=provenance_batch,
        owner_pid=os.getpid(),
        minted_at_monotonic=minted,
        expires_at_monotonic=minted + _RECEIPT_TTL_SECONDS,
    )


def verify_github_attestation(
    request: VerificationRequest,
    download_root: Path,
    *,
    _command_runner: _CommandRunner | None = None,
) -> VerifiedAttestationReceipt:
    """Verify nine producer artifacts for the currently running checker.

    The checker job is authenticated by its exact attempt-scoped ID/name, but
    is intentionally *not* required to have completed.  A later job must use
    :func:`verify_post_check_admission`.
    """

    runner = _command_runner if _command_runner is not None else _GitHubCliRunner()
    batch = _verify(
        request,
        download_root,
        checker_must_succeed=False,
        runner=runner,
    )
    return _new_check_receipt(batch)


def verify_post_check_admission(
    request: VerificationRequest,
    download_root: Path,
    checker_output_sha256: str,
    *,
    _command_runner: _CommandRunner | None = None,
) -> PostCheckAdmissionReceipt:
    """Independently reverify after the exact checker completed successfully."""

    expected_output = _sha256(checker_output_sha256, "checker output SHA-256")
    runner = _command_runner if _command_runner is not None else _GitHubCliRunner()
    verified = _verify(
        request,
        download_root,
        checker_must_succeed=True,
        runner=runner,
    )
    if verified.summary.checked_output_sha256 != expected_output:
        _fail("checker output digest does not bind the independently verified artifact set")
    from scripts import m4_6_artifact_provenance as provenance

    artifact_batch = verified.provenance_batch
    if type(artifact_batch) is not provenance._ArtifactCheckBatch:
        _fail("internal artifact-check batch type mismatch")
    checked = provenance._check_authenticated_batch(artifact_batch)
    post_batch = provenance._PostCheckAdmissionBatch(
        frozen=artifact_batch.frozen,
        completion=provenance._AuthenticatedCheckerCompletion(
            completion_source="github-actions-post-check-workflow-jobs-api",
            repository_id=REPOSITORY_ID,
            repository_full_name=REPOSITORY_FULL_NAME,
            workflow_id=request.workflow_id,
            workflow_ref=request.workflow_ref,
            workflow_sha=request.workflow_sha,
            run_id=request.run_id,
            run_attempt=request.run_attempt,
            head_sha=request.source_sha,
            checker_job_id=request.checker_job_id,
            checker_job_name=CHECKER_JOB_NAME,
            checker_job_status="completed",
            checker_job_conclusion="success",
            checker_output_sha256=expected_output,
        ),
        checked_artifacts=checked,
    )
    return _new_post_receipt(
        _PostCheckBatch(
            verified=verified,
            checker_output_sha256=expected_output,
            provenance_batch=post_batch,
        )
    )


def verify_canary_attestation(
    request: CanaryVerificationRequest,
    download_root: Path,
    *,
    _command_runner: _CommandRunner | None = None,
) -> CanaryVerificationSummary:
    """Verify the isolated one-subject non-release canary without release authority."""

    if type(request) is not CanaryVerificationRequest:
        _fail("an exact CanaryVerificationRequest is required")
    _int(request.run_id, "canary run ID")
    _int(request.run_attempt, "canary run attempt")
    _int(request.artifact_id, "canary artifact ID")
    _int(request.workflow_id, "canary workflow ID")
    _sha1(request.source_sha, "canary source SHA")
    _sha1(request.workflow_sha, "canary workflow SHA")
    expected_ref_prefix = f"{REPOSITORY_FULL_NAME}/{CANARY_WORKFLOW_PATH}@"
    if not request.workflow_ref.startswith(expected_ref_prefix):
        _fail("canary workflow ref does not identify the isolated workflow")
    runner = _command_runner if _command_runner is not None else _GitHubCliRunner()

    repository = _api_json(runner, f"repos/{REPOSITORY_FULL_NAME}")
    _validate_repository(repository)
    run = _api_json(runner, f"repos/{REPOSITORY_FULL_NAME}/actions/runs/{request.run_id}")
    _validate_run_row(
        run,
        request,
        "canary workflow run",
        workflow_path=CANARY_WORKFLOW_PATH,
    )
    attempt = _api_json(
        runner,
        (
            f"repos/{REPOSITORY_FULL_NAME}/actions/runs/{request.run_id}/attempts/"
            f"{request.run_attempt}"
        ),
    )
    _validate_run_row(
        attempt,
        request,
        "canary workflow run attempt",
        workflow_path=CANARY_WORKFLOW_PATH,
    )

    listing = _api_json(
        runner,
        f"repos/{REPOSITORY_FULL_NAME}/actions/runs/{request.run_id}/artifacts?per_page=100",
    )
    listed = _list(listing.get("artifacts"), "canary run artifacts")
    if listing.get("total_count") != 1 or len(listed) != 1:
        _fail("canary run must contain exactly one synthetic artifact")
    endpoint = f"repos/{REPOSITORY_FULL_NAME}/actions/artifacts/{request.artifact_id}"
    artifact = _api_json(runner, endpoint)
    if listed[0] != artifact:
        _fail("canary artifact detail differs from the authenticated run listing")
    if artifact.get("id") != request.artifact_id or artifact.get("name") != CANARY_ARTIFACT_NAME:
        _fail("canary immutable artifact ID/name mismatch")
    if artifact.get("expired") is not False:
        _fail("canary artifact is expired")
    api_size = _int(artifact.get("size_in_bytes"), "canary artifact API size")
    if api_size > MAX_ARTIFACT_SIZE_BYTES:
        _fail("canary artifact violates the fixed 64 MiB bound")
    api_digest = _digest(artifact.get("digest"), "canary artifact API digest")
    if _parse_time(artifact.get("expires_at"), "canary artifact expiry") <= datetime.now(
        timezone.utc
    ):
        _fail("canary artifact expiry is not in the future")
    workflow_run = artifact.get("workflow_run")
    if type(workflow_run) is not dict:
        _fail("canary artifact workflow_run association is missing")
    if workflow_run.get("id") != request.run_id:
        _fail("canary artifact belongs to another run")
    if workflow_run.get("repository_id") != REPOSITORY_ID:
        _fail("canary artifact repository ID mismatch")
    if workflow_run.get("head_repository_id") != REPOSITORY_ID:
        _fail("canary artifact head repository ID mismatch")
    if workflow_run.get("head_sha") != request.source_sha:
        _fail("canary artifact head SHA mismatch")
    custody_root = _prepare_custody_root(download_root)
    artifact_path = custody_root / f"{request.artifact_id}.zip"
    runner.download(
        (
            "api",
            "--hostname",
            "github.com",
            endpoint + "/zip",
            "-H",
            "Accept: application/vnd.github+json",
            "-H",
            f"X-GitHub-Api-Version: {GITHUB_API_VERSION}",
        ),
        artifact_path,
    )
    downloaded_digest, downloaded_size = _hash_file(artifact_path)
    if downloaded_digest != api_digest or downloaded_size != api_size:
        _fail("canary downloaded bytes disagree with artifact API digest/size")

    with tempfile.TemporaryDirectory(prefix="astrid-m4-6-canary-candidates-") as temporary:
        exhaustive_bundle = _write_exhaustive_attestation_bundle(
            runner,
            ((downloaded_digest, artifact_path),),
            Path(temporary),
            CANARY_PREDICATE_TYPE,
        )
        output = runner.json(
            (
                "attestation",
                "verify",
                str(artifact_path),
                "--hostname",
                "github.com",
                "--repo",
                REPOSITORY_FULL_NAME,
                "--bundle",
                str(exhaustive_bundle),
                "--signer-workflow",
                f"{REPOSITORY_FULL_NAME}/{CANARY_WORKFLOW_PATH}",
                "--signer-digest",
                request.workflow_sha,
                "--source-digest",
                request.source_sha,
                "--source-ref",
                request.source_ref,
                "--cert-oidc-issuer",
                OIDC_ISSUER,
                "--deny-self-hosted-runners",
                "--predicate-type",
                CANARY_PREDICATE_TYPE,
                "--format",
                "json",
            )
        )
    candidates = _list(output, "gh verified canary attestations")
    if len(candidates) != 1:
        _fail("canary requires exactly one acceptable attestation")
    candidate = candidates[0]
    if type(candidate) is not dict or set(candidate) != {"attestation", "verificationResult"}:
        _fail("canary gh verification result has an unexpected schema")
    verified_bundle = candidate.get("attestation")
    result = candidate.get("verificationResult")
    if type(verified_bundle) is not dict or type(result) is not dict:
        _fail("canary gh verification result omitted verified material")
    bundle = _verified_bundle(verified_bundle)
    signature = result.get("signature")
    if type(signature) is not dict or type(signature.get("certificate")) is not dict:
        _fail("canary verified certificate is missing")
    _validate_certificate(
        signature["certificate"],
        _CertificatePolicy(
            run_id=request.run_id,
            run_attempt=request.run_attempt,
            source_sha=request.source_sha,
            source_ref=request.source_ref,
            source_event=request.source_event,
            workflow_ref=request.workflow_ref,
            workflow_sha=request.workflow_sha,
            signer_workflow_ref=request.workflow_ref,
            signer_workflow_sha=request.workflow_sha,
        ),
    )
    _validate_timestamp_evidence(bundle, result)
    statement = result.get("statement")
    if type(statement) is not dict:
        _fail("canary verified statement is missing")
    if statement.get("_type") != "https://in-toto.io/Statement/v1":
        _fail("canary statement type mismatch")
    if statement.get("predicateType") != CANARY_PREDICATE_TYPE:
        _fail("canary predicate type mismatch")
    subjects = _list(statement.get("subject"), "canary subjects")
    if len(subjects) != 1:
        _fail("canary requires exactly one subject")
    subject = _exact_keys(subjects[0], {"name", "digest"}, "canary subject")
    subject_digest = _exact_keys(subject["digest"], {"sha256"}, "canary subject digest")
    if subject["name"] != CANARY_ARTIFACT_NAME or subject_digest["sha256"] != downloaded_digest:
        _fail("canary subject does not bind the downloaded immutable artifact")
    predicate = _exact_keys(
        statement.get("predicate"),
        {"version", "kind", "repository", "workflow", "run", "artifact"},
        "canary predicate",
    )
    if predicate["version"] != 1 or predicate["kind"] != "nonrelease-synthetic-canary":
        _fail("canary predicate version/kind mismatch")
    if predicate["repository"] != {
        "id": REPOSITORY_ID,
        "fullName": REPOSITORY_FULL_NAME,
        "ownerId": REPOSITORY_OWNER_ID,
        "ownerName": REPOSITORY_OWNER,
    }:
        _fail("canary predicate repository mismatch")
    if predicate["workflow"] != {
        "id": request.workflow_id,
        "path": CANARY_WORKFLOW_PATH,
        "ref": request.workflow_ref,
        "sha": request.workflow_sha,
    }:
        _fail("canary predicate workflow mismatch")
    if predicate["run"] != {
        "id": request.run_id,
        "attempt": request.run_attempt,
        "headSha": request.source_sha,
        "sourceRef": request.source_ref,
        "event": request.source_event,
    }:
        _fail("canary predicate run mismatch")
    if predicate["artifact"] != {
        "id": request.artifact_id,
        "name": CANARY_ARTIFACT_NAME,
        "digest": f"sha256:{downloaded_digest}",
        "sizeBytes": downloaded_size,
    }:
        _fail("canary predicate artifact mismatch")
    return CanaryVerificationSummary(
        repository_id=REPOSITORY_ID,
        workflow_id=request.workflow_id,
        workflow_ref=request.workflow_ref,
        workflow_sha=request.workflow_sha,
        run_id=request.run_id,
        run_attempt=request.run_attempt,
        artifact_id=request.artifact_id,
        artifact_name=CANARY_ARTIFACT_NAME,
        artifact_sha256=downloaded_digest,
        artifact_size_bytes=downloaded_size,
        bundle_sha256=hashlib.sha256(_canonical_json(bundle)).hexdigest(),
    )


def _add_fixed_repository_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--repository", default=REPOSITORY_FULL_NAME)
    parser.add_argument("--repository-id", type=int, default=REPOSITORY_ID)
    parser.add_argument("--owner-id", type=int, default=REPOSITORY_OWNER_ID)
    parser.add_argument("--max-artifact-bytes", type=int, default=MAX_ARTIFACT_SIZE_BYTES)


def _add_verification_arguments(parser: argparse.ArgumentParser) -> None:
    _add_fixed_repository_arguments(parser)
    parser.add_argument("--run-id", type=int, required=True)
    parser.add_argument("--run-attempt", type=int, required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--source-ref", required=True)
    parser.add_argument("--event-name", required=True)
    parser.add_argument("--workflow-path", required=True)
    parser.add_argument("--workflow-ref", required=True)
    parser.add_argument("--workflow-sha", required=True)
    parser.add_argument("--signer-workflow-ref", required=True)
    parser.add_argument("--signer-workflow-sha", required=True)
    parser.add_argument("--artifact-id", action="append", type=int, required=True)
    parser.add_argument("--download-root", type=Path, required=True)
    parser.add_argument("--github-output", type=Path)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Fixed GitHub/Sigstore verifier for the M4-6 trust boundary"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    prepare = subparsers.add_parser("prepare-predicate")
    _add_fixed_repository_arguments(prepare)
    prepare.add_argument("--run-id", type=int, required=True)
    prepare.add_argument("--run-attempt", type=int, required=True)
    prepare.add_argument("--source-sha", required=True)
    prepare.add_argument("--source-ref", required=True)
    prepare.add_argument("--event-name", required=True)
    prepare.add_argument("--workflow-ref", required=True)
    prepare.add_argument("--workflow-sha", required=True)
    prepare.add_argument("--predicate-type", default=PREDICATE_TYPE)
    prepare.add_argument("--predicate-output", type=Path, required=True)
    prepare.add_argument("--subjects-output", type=Path, required=True)
    prepare.add_argument("--binding", action="append", required=True)

    verify = subparsers.add_parser("verify")
    _add_verification_arguments(verify)
    verify.add_argument("--predicate-type", default=PREDICATE_TYPE)
    verify.add_argument("--issuer", default=OIDC_ISSUER)

    post = subparsers.add_parser("verify-post-check")
    _add_verification_arguments(post)
    post.add_argument("--predicate-type", default=PREDICATE_TYPE)
    post.add_argument("--issuer", default=OIDC_ISSUER)
    post.add_argument("--checker-output-sha256", required=True)

    canary = subparsers.add_parser("verify-canary")
    _add_fixed_repository_arguments(canary)
    canary.add_argument("--run-id", type=int, required=True)
    canary.add_argument("--run-attempt", type=int, required=True)
    canary.add_argument("--source-sha", required=True)
    canary.add_argument("--source-ref", required=True)
    canary.add_argument("--event-name", required=True)
    canary.add_argument("--workflow-ref", required=True)
    canary.add_argument("--workflow-sha", required=True)
    canary.add_argument("--artifact-id", type=int, required=True)
    canary.add_argument("--download-root", type=Path, required=True)
    canary.add_argument("--predicate-type", default=CANARY_PREDICATE_TYPE)
    canary.add_argument("--github-output", type=Path)
    return parser


def _enforce_cli_policy(arguments: argparse.Namespace) -> None:
    if arguments.repository != REPOSITORY_FULL_NAME:
        _fail("CLI repository differs from fixed policy")
    if arguments.repository_id != REPOSITORY_ID:
        _fail("CLI repository numeric ID differs from fixed policy")
    if arguments.owner_id != REPOSITORY_OWNER_ID:
        _fail("CLI owner numeric ID differs from fixed policy")
    if arguments.max_artifact_bytes != MAX_ARTIFACT_SIZE_BYTES:
        _fail("CLI artifact bound must be exactly 64 MiB")
    if hasattr(arguments, "issuer") and arguments.issuer != OIDC_ISSUER:
        _fail("CLI OIDC issuer differs from fixed policy")
    expected_predicate = (
        CANARY_PREDICATE_TYPE if arguments.command == "verify-canary" else PREDICATE_TYPE
    )
    if hasattr(arguments, "predicate_type") and arguments.predicate_type != expected_predicate:
        _fail("CLI predicate type differs from fixed policy")


def _resolve_cli_request(
    arguments: argparse.Namespace, runner: _CommandRunner
) -> VerificationRequest:
    run = _api_json(
        runner,
        f"repos/{REPOSITORY_FULL_NAME}/actions/runs/{arguments.run_id}",
    )
    workflow_id = _int(run.get("workflow_id"), "workflow run workflow ID")
    jobs = _api_json(
        runner,
        (
            f"repos/{REPOSITORY_FULL_NAME}/actions/runs/{arguments.run_id}/attempts/"
            f"{arguments.run_attempt}/jobs?per_page=100"
        ),
    )
    job_rows = _list(jobs.get("jobs"), "attempt jobs")
    if jobs.get("total_count") != len(job_rows):
        _fail("attempt jobs response was truncated or inconsistent")
    matches = [row for row in job_rows if type(row) is dict and row.get("name") == CHECKER_JOB_NAME]
    if len(matches) != 1:
        _fail("CLI could not resolve one exact checker job from the exact-attempt API")
    checker_job_id = _int(matches[0].get("id"), "checker job ID")
    return VerificationRequest(
        run_id=arguments.run_id,
        run_attempt=arguments.run_attempt,
        checker_job_id=checker_job_id,
        artifact_ids=tuple(arguments.artifact_id),
        source_sha=arguments.source_sha,
        source_ref=arguments.source_ref,
        source_event=arguments.event_name,
        workflow_id=workflow_id,
        workflow_path=arguments.workflow_path,
        workflow_ref=arguments.workflow_ref,
        workflow_sha=arguments.workflow_sha,
        signer_workflow_ref=arguments.signer_workflow_ref,
        signer_workflow_sha=arguments.signer_workflow_sha,
    )


def _append_github_output(path: Path | None, values: dict[str, str]) -> None:
    if path is None:
        return
    if type(path) is not _PATH_TYPE:
        _fail("github output must be an exact pathlib.Path")
    with path.open("a", encoding="utf-8") as stream:
        for key, value in values.items():
            if not re.fullmatch(r"[a-z0-9_]+", key) or "\n" in value or "\r" in value:
                _fail("unsafe GitHub output key/value")
            stream.write(f"{key}={value}\n")


def _summary_document(summary: VerifiedAttestationSummary) -> dict[str, object]:
    return {
        "repositoryId": summary.repository_id,
        "repository": summary.repository_full_name,
        "workflowId": summary.workflow_id,
        "workflowRef": summary.workflow_ref,
        "workflowSha": summary.workflow_sha,
        "runId": summary.run_id,
        "runAttempt": summary.run_attempt,
        "headSha": summary.head_sha,
        "checkerJobId": summary.checker_job_id,
        "checkerJobName": summary.checker_job_name,
        "bundleSha256": summary.bundle_sha256,
        "registryDigest": summary.registry_digest,
        "checkedOutputSha256": summary.checked_output_sha256,
        "artifacts": [
            {
                "slot": row.slot,
                "producerJobId": row.producer_job_id,
                "producerJobName": row.producer_job_name,
                "artifactId": row.artifact_id,
                "artifactName": row.artifact_name,
                "sha256": row.downloaded_sha256,
                "sizeBytes": row.downloaded_size_bytes,
            }
            for row in summary.artifacts
        ],
    }


def _main(
    argv: list[str] | None = None,
    *,
    _command_runner: _CommandRunner | None = None,
) -> int:
    parser = _build_parser()
    arguments = parser.parse_args(argv)
    try:
        _enforce_cli_policy(arguments)
        runner = _command_runner if _command_runner is not None else _GitHubCliRunner()
        if arguments.command == "prepare-predicate":
            prepare_trusted_predicate(
                runner=runner,
                run_id=arguments.run_id,
                run_attempt=arguments.run_attempt,
                source_sha=arguments.source_sha,
                source_ref=arguments.source_ref,
                source_event=arguments.event_name,
                workflow_ref=arguments.workflow_ref,
                workflow_sha=arguments.workflow_sha,
                bindings=tuple(arguments.binding),
                predicate_output=arguments.predicate_output,
                subjects_output=arguments.subjects_output,
            )
            return 0
        if arguments.command in {"verify", "verify-post-check"}:
            request = _resolve_cli_request(arguments, runner)
            from scripts import m4_6_artifact_provenance as provenance
            from scripts import m4_6_trusted_launcher as launcher

            if arguments.command == "verify":
                receipt = verify_github_attestation(
                    request,
                    arguments.download_root,
                    _command_runner=runner,
                )
                summary = receipt.summary
                context = launcher._artifact_check_context_from_verified_receipt(receipt)
                checked = provenance.verify_downloaded_artifacts(context)
                if provenance._checked_output_sha256(checked) != summary.checked_output_sha256:
                    _fail("running-checker context output differs from verified summary")
            else:
                receipt = verify_post_check_admission(
                    request,
                    arguments.download_root,
                    arguments.checker_output_sha256,
                    _command_runner=runner,
                )
                summary = receipt.summary
                context = launcher._post_check_admission_context_from_verified_receipt(receipt)
                admitted = provenance.admit_checked_artifacts(context)
                if len(admitted) != len(TRUSTED_SLOT_REGISTRY):
                    _fail("post-check context did not admit the exact fixed slot set")
            _append_github_output(
                arguments.github_output,
                {
                    "checked_output_sha256": summary.checked_output_sha256,
                    "bundle_sha256": summary.bundle_sha256,
                    "registry_digest": summary.registry_digest,
                },
            )
            sys.stdout.buffer.write(_canonical_json(_summary_document(summary)) + b"\n")
            return 0
        if arguments.command == "verify-canary":
            run = _api_json(
                runner,
                f"repos/{REPOSITORY_FULL_NAME}/actions/runs/{arguments.run_id}",
            )
            request = CanaryVerificationRequest(
                run_id=arguments.run_id,
                run_attempt=arguments.run_attempt,
                artifact_id=arguments.artifact_id,
                source_sha=arguments.source_sha,
                source_ref=arguments.source_ref,
                source_event=arguments.event_name,
                workflow_id=_int(run.get("workflow_id"), "canary workflow ID"),
                workflow_ref=arguments.workflow_ref,
                workflow_sha=arguments.workflow_sha,
            )
            summary = verify_canary_attestation(
                request,
                arguments.download_root,
                _command_runner=runner,
            )
            _append_github_output(
                arguments.github_output,
                {
                    "canary_artifact_sha256": summary.artifact_sha256,
                    "canary_bundle_sha256": summary.bundle_sha256,
                },
            )
            sys.stdout.buffer.write(
                _canonical_json(
                    {
                        "repositoryId": summary.repository_id,
                        "workflowId": summary.workflow_id,
                        "workflowRef": summary.workflow_ref,
                        "workflowSha": summary.workflow_sha,
                        "runId": summary.run_id,
                        "runAttempt": summary.run_attempt,
                        "artifactId": summary.artifact_id,
                        "artifactName": summary.artifact_name,
                        "artifactSha256": summary.artifact_sha256,
                        "artifactSizeBytes": summary.artifact_size_bytes,
                        "bundleSha256": summary.bundle_sha256,
                    }
                )
                + b"\n"
            )
            return 0
        raise AssertionError("unreachable CLI command")
    except GitHubAttestationError as exc:
        print(f"m4-6 attestation verification failed: {exc}", file=sys.stderr)
        return 2


def main() -> int:
    return _main()


if __name__ == "__main__":
    raise SystemExit(main())
