"""Adversarial tests for the fixed GitHub attestation verifier.

The fake command transport is a private test seam, not an authority provider.
One test also invokes the installed real ``gh attestation verify`` command and
proves a malformed bundle is rejected by its cryptographic verifier.
"""

from __future__ import annotations

import copy
import hashlib
import json
import pickle
import shutil
from dataclasses import replace
from pathlib import Path

import pytest

from scripts import m4_6_artifact_provenance as provenance
from scripts import m4_6_github_attestation as attestation
from scripts import m4_6_trusted_launcher as launcher

RUN_ID = 8_000_001
RUN_ATTEMPT = 3
CHECKER_JOB_ID = 8_100_001
WORKFLOW_ID = 8_200_001
SOURCE_SHA = "a" * 40
WORKFLOW_SHA = "b" * 40
SIGNER_SHA = "c" * 40
SOURCE_REF = "refs/heads/m4-6-trusted-test"
WORKFLOW_PATH = ".github/workflows/m4-6-trusted-attestor.yml"
WORKFLOW_REF = f"{attestation.REPOSITORY_FULL_NAME}/{WORKFLOW_PATH}@{SOURCE_REF}"
SIGNER_REF = f"{attestation.REPOSITORY_FULL_NAME}/{attestation.SIGNER_WORKFLOW_PATH}@{SOURCE_REF}"


def _request() -> attestation.VerificationRequest:
    return attestation.VerificationRequest(
        run_id=RUN_ID,
        run_attempt=RUN_ATTEMPT,
        checker_job_id=CHECKER_JOB_ID,
        artifact_ids=tuple(9_000_000 + index for index in range(1, 10)),
        source_sha=SOURCE_SHA,
        source_ref=SOURCE_REF,
        source_event="workflow_dispatch",
        workflow_id=WORKFLOW_ID,
        workflow_path=WORKFLOW_PATH,
        workflow_ref=WORKFLOW_REF,
        workflow_sha=WORKFLOW_SHA,
        signer_workflow_ref=SIGNER_REF,
        signer_workflow_sha=SIGNER_SHA,
    )


def _archive_bytes(index: int) -> bytes:
    return f"opaque-m4-6-archive-{index}\n".encode()


class _FakeCommandRunner:
    def __init__(self, *, checker_status: str = "in_progress") -> None:
        self.request = _request()
        self.checker_status = checker_status
        self.commands: list[tuple[str, ...]] = []
        self.downloads: list[tuple[tuple[str, ...], Path]] = []
        self.crypto_error = False
        self.artifact_bytes = {
            artifact_id: _archive_bytes(index)
            for index, artifact_id in enumerate(self.request.artifact_ids, start=1)
        }
        self.repository = {
            "id": attestation.REPOSITORY_ID,
            "full_name": attestation.REPOSITORY_FULL_NAME,
            "private": False,
            "visibility": "public",
            "owner": {
                "id": attestation.REPOSITORY_OWNER_ID,
                "login": attestation.REPOSITORY_OWNER,
            },
        }
        self.run = {
            "id": RUN_ID,
            "run_attempt": RUN_ATTEMPT,
            "workflow_id": WORKFLOW_ID,
            "head_sha": SOURCE_SHA,
            "head_branch": SOURCE_REF.removeprefix("refs/heads/"),
            "event": "workflow_dispatch",
            "path": WORKFLOW_PATH,
            "status": "in_progress",
            "conclusion": None,
            "repository": {
                "id": attestation.REPOSITORY_ID,
                "full_name": attestation.REPOSITORY_FULL_NAME,
            },
            "head_repository": {
                "id": attestation.REPOSITORY_ID,
                "full_name": attestation.REPOSITORY_FULL_NAME,
            },
        }
        producer_jobs = [
            {
                "id": 8_300_000 + index,
                "name": policy.producer_job_name,
                "status": "completed",
                "conclusion": "success",
                "run_id": RUN_ID,
                "run_attempt": RUN_ATTEMPT,
            }
            for index, policy in enumerate(attestation.TRUSTED_SLOT_REGISTRY, start=1)
        ]
        self.jobs = producer_jobs + [
            {
                "id": CHECKER_JOB_ID,
                "name": attestation.CHECKER_JOB_NAME,
                "status": checker_status,
                "conclusion": "success" if checker_status == "completed" else None,
                "run_id": RUN_ID,
                "run_attempt": RUN_ATTEMPT,
            },
            {
                "id": 8_400_001,
                "name": "m4-6-trusted-attestor",
                "status": "completed",
                "conclusion": "success",
                "run_id": RUN_ID,
                "run_attempt": RUN_ATTEMPT,
            },
        ]
        self.artifacts = []
        for policy, artifact_id in zip(
            attestation.TRUSTED_SLOT_REGISTRY,
            self.request.artifact_ids,
            strict=True,
        ):
            body = self.artifact_bytes[artifact_id]
            self.artifacts.append(
                {
                    "id": artifact_id,
                    "name": policy.artifact_name,
                    "size_in_bytes": len(body),
                    "expired": False,
                    "digest": f"sha256:{hashlib.sha256(body).hexdigest()}",
                    "expires_at": "2099-01-01T00:00:00Z",
                    "workflow_run": {
                        "id": RUN_ID,
                        "repository_id": attestation.REPOSITORY_ID,
                        "head_repository_id": attestation.REPOSITORY_ID,
                        "head_sha": SOURCE_SHA,
                    },
                }
            )
        self.output = self._verification_output()

    def _verification_output(self) -> list[object]:
        subjects = []
        rows = []
        for index, (policy, artifact) in enumerate(
            zip(attestation.TRUSTED_SLOT_REGISTRY, self.artifacts, strict=True),
            start=1,
        ):
            digest = str(artifact["digest"]).removeprefix("sha256:")
            subjects.append({"name": policy.artifact_name, "digest": {"sha256": digest}})
            producer = self.jobs[index - 1]
            rows.append(
                {
                    "slot": policy.slot,
                    "matrix": None,
                    "producerJob": {
                        "id": producer["id"],
                        "name": policy.producer_job_name,
                        "status": "completed",
                        "conclusion": "success",
                    },
                    "artifact": {
                        "id": artifact["id"],
                        "name": policy.artifact_name,
                        "digest": artifact["digest"],
                        "sizeBytes": artifact["size_in_bytes"],
                    },
                }
            )
        predicate = {
            "version": 1,
            "registryDigest": attestation.REGISTRY_DIGEST,
            "repository": {
                "id": attestation.REPOSITORY_ID,
                "fullName": attestation.REPOSITORY_FULL_NAME,
                "ownerId": attestation.REPOSITORY_OWNER_ID,
                "ownerName": attestation.REPOSITORY_OWNER,
            },
            "workflow": {
                "id": WORKFLOW_ID,
                "path": WORKFLOW_PATH,
                "ref": WORKFLOW_REF,
                "sha": WORKFLOW_SHA,
            },
            "run": {
                "id": RUN_ID,
                "attempt": RUN_ATTEMPT,
                "headSha": SOURCE_SHA,
                "sourceRef": SOURCE_REF,
                "event": "workflow_dispatch",
            },
            "artifacts": rows,
        }
        certificate = {
            "certificateIssuer": "CN=sigstore-intermediate,O=sigstore.dev",
            "subjectAlternativeName": f"https://github.com/{SIGNER_REF}",
            "issuer": attestation.OIDC_ISSUER,
            "githubWorkflowTrigger": "workflow_dispatch",
            "githubWorkflowSHA": SOURCE_SHA,
            "githubWorkflowRepository": attestation.REPOSITORY_FULL_NAME,
            "githubWorkflowRef": SOURCE_REF,
            "buildSignerURI": f"https://github.com/{SIGNER_REF}",
            "buildSignerDigest": SIGNER_SHA,
            "runnerEnvironment": "github-hosted",
            "sourceRepositoryURI": attestation.REPOSITORY_URI,
            "sourceRepositoryDigest": SOURCE_SHA,
            "sourceRepositoryRef": SOURCE_REF,
            "sourceRepositoryIdentifier": str(attestation.REPOSITORY_ID),
            "sourceRepositoryOwnerURI": attestation.REPOSITORY_OWNER_URI,
            "sourceRepositoryOwnerIdentifier": str(attestation.REPOSITORY_OWNER_ID),
            "buildConfigURI": f"https://github.com/{WORKFLOW_REF}",
            "buildConfigDigest": WORKFLOW_SHA,
            "buildTrigger": "workflow_dispatch",
            "runInvocationURI": (
                f"{attestation.REPOSITORY_URI}/actions/runs/{RUN_ID}/attempts/{RUN_ATTEMPT}"
            ),
            "sourceRepositoryVisibilityAtSigning": "public",
        }
        bundle = {
            "mediaType": "application/vnd.dev.sigstore.bundle.v0.3+json",
            "verificationMaterial": {
                "x509CertificateChain": {"certificates": [{"rawBytes": "fixture"}]},
                "tlogEntries": [
                    {
                        "logIndex": "42",
                        "integratedTime": "1893456000",
                        "inclusionPromise": {"signedEntryTimestamp": "fixture-set"},
                    }
                ],
            },
            "dsseEnvelope": {"payload": "fixture", "signatures": [{"sig": "fixture"}]},
        }
        result = {
            "mediaType": "application/vnd.dev.sigstore.verificationresult+json;version=0.1",
            "signature": {"certificate": certificate},
            "verifiedTimestamps": [
                {
                    "type": "Tlog",
                    "uri": "https://rekor.sigstore.dev",
                    "timestamp": "2030-01-01T00:00:00Z",
                }
            ],
            "verifiedIdentity": {
                "issuer": {"issuer": attestation.OIDC_ISSUER},
                "subjectAlternativeName": {
                    "subjectAlternativeName": f"https://github.com/{SIGNER_REF}"
                },
            },
            "statement": {
                "_type": "https://in-toto.io/Statement/v1",
                "subject": subjects,
                "predicateType": attestation.PREDICATE_TYPE,
                "predicate": predicate,
            },
        }
        return [
            {
                "attestation": {"bundle": bundle, "bundle_url": "", "initiator": ""},
                "verificationResult": result,
            }
        ]

    def json(self, arguments: tuple[str, ...]) -> object:
        self.commands.append(arguments)
        if arguments[:2] == ("attestation", "verify"):
            if self.crypto_error:
                raise attestation.GitHubAttestationCommandError(
                    "GitHub CLI rejected authenticated verification"
                )
            return copy.deepcopy(self.output)
        assert arguments[0] == "api"
        assert arguments[1:3] == ("--hostname", "github.com")
        endpoint = arguments[3]
        if endpoint == f"repos/{attestation.REPOSITORY_FULL_NAME}":
            return copy.deepcopy(self.repository)
        if endpoint == f"repos/{attestation.REPOSITORY_FULL_NAME}/actions/runs/{RUN_ID}":
            return copy.deepcopy(self.run)
        if endpoint == (
            f"repos/{attestation.REPOSITORY_FULL_NAME}/actions/runs/{RUN_ID}/attempts/{RUN_ATTEMPT}"
        ):
            return copy.deepcopy(self.run)
        if endpoint.endswith(f"attempts/{RUN_ATTEMPT}/jobs?per_page=100"):
            return {"total_count": len(self.jobs), "jobs": copy.deepcopy(self.jobs)}
        if endpoint.endswith(f"runs/{RUN_ID}/artifacts?per_page=100"):
            return {
                "total_count": len(self.artifacts),
                "artifacts": copy.deepcopy(self.artifacts),
            }
        prefix = f"repos/{attestation.REPOSITORY_FULL_NAME}/actions/artifacts/"
        if endpoint.startswith(prefix):
            artifact_id = int(endpoint.removeprefix(prefix))
            return copy.deepcopy(next(row for row in self.artifacts if row["id"] == artifact_id))
        raise AssertionError(f"unexpected endpoint: {endpoint}")

    def download(self, arguments: tuple[str, ...], destination: Path) -> None:
        self.downloads.append((arguments, destination))
        assert arguments[:3] == ("api", "--hostname", "github.com")
        artifact_id = int(arguments[3].rsplit("/", 2)[-2])
        destination.write_bytes(self.artifact_bytes[artifact_id])

    def attestation_bundles(
        self,
        subject: Path,
        destination: Path,
        predicate_type: str,
    ) -> tuple[dict[str, object], ...]:
        assert subject.is_file()
        assert predicate_type in (attestation.PREDICATE_TYPE, attestation.CANARY_PREDICATE_TYPE)
        return tuple(copy.deepcopy(candidate["attestation"]["bundle"]) for candidate in self.output)


def _verify(tmp_path: Path, runner: _FakeCommandRunner) -> attestation.VerifiedAttestationReceipt:
    tmp_path.mkdir(parents=True, exist_ok=True)
    return attestation.verify_github_attestation(
        _request(),
        tmp_path / "private-custody",
        _command_runner=runner,
    )


def test_exact_nine_verification_mints_one_shot_provenance_batch(tmp_path: Path) -> None:
    runner = _FakeCommandRunner()
    receipt = _verify(tmp_path, runner)

    assert type(receipt) is attestation.VerifiedAttestationReceipt
    assert receipt.summary.registry_digest == attestation.REGISTRY_DIGEST
    assert len(receipt.summary.artifacts) == 9
    assert tuple(row.slot for row in receipt.summary.artifacts) == tuple(
        row.slot for row in attestation.TRUSTED_SLOT_REGISTRY
    )
    verify_commands = [
        command for command in runner.commands if command[:2] == ("attestation", "verify")
    ]
    assert len(verify_commands) == 9
    for command in verify_commands:
        assert "--bundle" in command
        assert "--deny-self-hosted-runners" in command
        assert command[command.index("--signer-digest") + 1] == SIGNER_SHA
        assert command[command.index("--source-ref") + 1] == SOURCE_REF
        assert command[command.index("--predicate-type") + 1] == attestation.PREDICATE_TYPE
    assert len(runner.downloads) == 9
    assert all(
        command[:3] == ("api", "--hostname", "github.com")
        and "/actions/artifacts/" in command[3]
        and command[3].endswith("/zip")
        for command, _ in runner.downloads
    )

    context = launcher._artifact_check_context_from_verified_receipt(receipt)
    checked = provenance.verify_downloaded_artifacts(context)
    assert len(checked) == 9
    assert tuple(row.slot_id for row in checked) == provenance.REQUIRED_SLOT_IDS
    with pytest.raises(attestation.GitHubAttestationPolicyError, match="unknown or consumed"):
        attestation._claim_artifact_check_receipt(receipt)


def test_receipts_are_nonconstructible_noncopyable_and_nonserializable(tmp_path: Path) -> None:
    with pytest.raises(TypeError, match="minted only"):
        attestation.VerifiedAttestationReceipt(  # type: ignore[call-arg]
            object()  # type: ignore[arg-type]
        )
    receipt = _verify(tmp_path, _FakeCommandRunner())
    with pytest.raises(TypeError, match="not copyable"):
        copy.copy(receipt)
    with pytest.raises(TypeError, match="not serializable"):
        pickle.dumps(receipt)


def test_running_checker_does_not_require_its_own_terminal_success(tmp_path: Path) -> None:
    receipt = _verify(tmp_path, _FakeCommandRunner(checker_status="in_progress"))
    assert receipt.summary.artifacts[0].checker_job_status == "in_progress"


def test_post_check_repeats_verification_and_requires_completed_success(tmp_path: Path) -> None:
    first = _verify(tmp_path / "first", _FakeCommandRunner())
    output_digest = first.summary.checked_output_sha256
    completed = _FakeCommandRunner(checker_status="completed")

    receipt = attestation.verify_post_check_admission(
        _request(),
        tmp_path / "post-custody",
        output_digest,
        _command_runner=completed,
    )
    context = launcher._post_check_admission_context_from_verified_receipt(receipt)
    admitted = provenance.admit_checked_artifacts(context)
    assert len(admitted) == 9
    assert all(row.checker_output_sha256 == output_digest for row in admitted)
    with pytest.raises(attestation.GitHubAttestationPolicyError, match="unknown or consumed"):
        attestation._claim_post_check_receipt(receipt)


def test_post_check_rejects_running_checker_and_wrong_output(tmp_path: Path) -> None:
    with pytest.raises(attestation.GitHubAttestationPolicyError, match="completed/success"):
        attestation.verify_post_check_admission(
            _request(),
            tmp_path / "running",
            "d" * 64,
            _command_runner=_FakeCommandRunner(),
        )
    with pytest.raises(attestation.GitHubAttestationPolicyError, match="does not bind"):
        attestation.verify_post_check_admission(
            _request(),
            tmp_path / "wrong-output",
            "d" * 64,
            _command_runner=_FakeCommandRunner(checker_status="completed"),
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("issuer", "https://evil.example"),
        ("sourceRepositoryURI", "https://github.com/other/repository"),
        ("sourceRepositoryIdentifier", "1"),
        ("sourceRepositoryOwnerURI", "https://github.com/other"),
        ("sourceRepositoryOwnerIdentifier", "1"),
        ("sourceRepositoryDigest", "d" * 40),
        ("sourceRepositoryRef", "refs/heads/other"),
        ("buildTrigger", "push"),
        ("runnerEnvironment", "self-hosted"),
        ("buildSignerURI", "https://github.com/other/workflow.yml@refs/heads/main"),
        ("buildSignerDigest", "d" * 40),
        ("buildConfigURI", "https://github.com/other/config.yml@refs/heads/main"),
        ("buildConfigDigest", "d" * 40),
        ("runInvocationURI", f"{attestation.REPOSITORY_URI}/actions/runs/{RUN_ID}/attempts/2"),
        ("sourceRepositoryVisibilityAtSigning", "private"),
    ],
)
def test_certificate_claim_mutations_fail_closed(tmp_path: Path, field: str, value: str) -> None:
    runner = _FakeCommandRunner()
    certificate = runner.output[0]["verificationResult"]["signature"]["certificate"]
    certificate[field] = value
    with pytest.raises(attestation.GitHubAttestationPolicyError, match=field):
        _verify(tmp_path, runner)


def test_certificate_audience_is_enforced_if_verifier_represents_it(tmp_path: Path) -> None:
    accepted = _FakeCommandRunner()
    accepted.output[0]["verificationResult"]["signature"]["certificate"]["audience"] = "sigstore"
    _verify(tmp_path / "accepted", accepted)

    rejected = _FakeCommandRunner()
    rejected.output[0]["verificationResult"]["signature"]["certificate"]["audience"] = "other"
    with pytest.raises(attestation.GitHubAttestationPolicyError, match="audience"):
        _verify(tmp_path / "rejected", rejected)


def test_real_gh_certificate_san_shape_and_attestation_wrapper_are_exact(tmp_path: Path) -> None:
    wrong_san = _FakeCommandRunner()
    wrong_san.output[0]["verificationResult"]["signature"]["certificate"][
        "subjectAlternativeName"
    ] = "https://github.com/other/repository/.github/workflows/evil.yml@refs/heads/main"
    with pytest.raises(attestation.GitHubAttestationPolicyError, match="SAN"):
        _verify(tmp_path / "san", wrong_san)

    wrong_wrapper = _FakeCommandRunner()
    wrong_wrapper.output[0]["attestation"]["extra"] = "untrusted"
    with pytest.raises(attestation.GitHubAttestationPolicyError, match="wrapper"):
        _verify(tmp_path / "wrapper", wrong_wrapper)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda p: p.update(version=2), "version"),
        (lambda p: p.update(registryDigest="sha256:" + "0" * 64), "registry digest"),
        (lambda p: p["repository"].update(ownerId=1), "repository identity"),
        (lambda p: p["workflow"].update(sha="d" * 40), "workflow identity"),
        (lambda p: p["run"].update(attempt=2), "run identity"),
        (lambda p: p["artifacts"][0].update(slot="direct-linux"), "slot order"),
        (lambda p: p["artifacts"][0].update(matrix={"os": "macos"}), "slot order"),
        (lambda p: p["artifacts"][0]["producerJob"].update(id=99), "producer assertion"),
        (lambda p: p["artifacts"][0]["artifact"].update(id=99), "artifact assertion"),
        (lambda p: p["artifacts"][0]["artifact"].update(sizeBytes=99), "artifact assertion"),
        (lambda p: p.update(extra=True), "non-closed schema"),
        (lambda p: p["artifacts"].pop(), "exactly nine artifact"),
    ],
)
def test_predicate_mutations_fail_closed(
    tmp_path: Path,
    mutate: object,
    message: str,
) -> None:
    runner = _FakeCommandRunner()
    predicate = runner.output[0]["verificationResult"]["statement"]["predicate"]
    mutate(predicate)  # type: ignore[operator]
    with pytest.raises(attestation.GitHubAttestationPolicyError, match=message):
        _verify(tmp_path, runner)


def test_subject_mutation_duplicate_and_ambiguous_attestations_fail(tmp_path: Path) -> None:
    changed = _FakeCommandRunner()
    changed.output[0]["verificationResult"]["statement"]["subject"][0]["digest"]["sha256"] = (
        "0" * 64
    )
    with pytest.raises(attestation.GitHubAttestationPolicyError, match="subject digest"):
        _verify(tmp_path / "subject", changed)

    duplicate = _FakeCommandRunner()
    duplicate.output.append(copy.deepcopy(duplicate.output[0]))
    with pytest.raises(attestation.GitHubAttestationPolicyError, match="duplicate|exactly one"):
        _verify(tmp_path / "duplicate", duplicate)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("run_attempt", "run ID/attempt"),
        ("producer_failure", "did not complete successfully"),
        ("artifact_expired", "expired"),
        ("artifact_oversize", "64 MiB"),
        ("artifact_digest", "downloaded bytes disagree"),
        ("artifact_overwrite", "immutable selected set"),
        ("artifact_head", "head SHA"),
    ],
)
def test_api_replay_overwrite_expiry_and_byte_mutations_fail_closed(
    tmp_path: Path, mutation: str, message: str
) -> None:
    runner = _FakeCommandRunner()
    if mutation == "run_attempt":
        runner.run["run_attempt"] = RUN_ATTEMPT + 1
    elif mutation == "producer_failure":
        runner.jobs[0]["conclusion"] = "failure"
    elif mutation == "artifact_expired":
        runner.artifacts[0]["expired"] = True
    elif mutation == "artifact_oversize":
        runner.artifacts[0]["size_in_bytes"] = attestation.MAX_ARTIFACT_SIZE_BYTES + 1
    elif mutation == "artifact_digest":
        runner.artifacts[0]["digest"] = "sha256:" + "0" * 64
    elif mutation == "artifact_overwrite":
        runner.artifacts[0]["id"] = 99_999_999
    elif mutation == "artifact_head":
        runner.artifacts[0]["workflow_run"]["head_sha"] = "d" * 40
    with pytest.raises(attestation.GitHubAttestationPolicyError, match=message):
        _verify(tmp_path, runner)


def test_cross_attempt_job_and_unexpected_producer_are_rejected(tmp_path: Path) -> None:
    cross_attempt = _FakeCommandRunner()
    cross_attempt.jobs[0]["run_attempt"] = RUN_ATTEMPT - 1
    with pytest.raises(attestation.GitHubAttestationPolicyError, match="another run attempt"):
        _verify(tmp_path / "cross-attempt", cross_attempt)

    extra = _FakeCommandRunner()
    extra.jobs.append(
        {
            "id": 123,
            "name": "m4-6-producer-unreviewed",
            "status": "completed",
            "conclusion": "success",
            "run_id": RUN_ID,
            "run_attempt": RUN_ATTEMPT,
        }
    )
    with pytest.raises(attestation.GitHubAttestationPolicyError, match="unexpected producer"):
        _verify(tmp_path / "extra", extra)


def test_crypto_rejection_happens_before_predicate_parsing_or_archive_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = _FakeCommandRunner()
    runner.crypto_error = True
    runner.output[0]["verificationResult"]["statement"]["predicate"] = "malformed"
    opened = False

    def forbidden_open(_batch: object) -> object:
        nonlocal opened
        opened = True
        raise AssertionError("archive was opened before cryptographic verification")

    monkeypatch.setattr(provenance, "_check_authenticated_batch", forbidden_open)
    with pytest.raises(attestation.GitHubAttestationCommandError, match="rejected"):
        _verify(tmp_path, runner)
    assert not opened
    assert any(command[:2] == ("attestation", "verify") for command in runner.commands)


def test_missing_tlog_integrated_time_and_proof_are_rejected(tmp_path: Path) -> None:
    missing_timestamp = _FakeCommandRunner()
    missing_timestamp.output[0]["verificationResult"]["verifiedTimestamps"] = []
    with pytest.raises(attestation.GitHubAttestationPolicyError, match="timestamp"):
        _verify(tmp_path / "timestamp", missing_timestamp)

    missing_proof = _FakeCommandRunner()
    entry = missing_proof.output[0]["attestation"]["bundle"]["verificationMaterial"]["tlogEntries"][
        0
    ]
    entry.pop("inclusionPromise")
    with pytest.raises(attestation.GitHubAttestationPolicyError, match="inclusion"):
        _verify(tmp_path / "proof", missing_proof)


def test_trusted_rfc3161_timestamp_is_accepted_and_must_have_bundle_material(
    tmp_path: Path,
) -> None:
    trusted_timestamp = _FakeCommandRunner()
    trusted_timestamp.output[0]["verificationResult"]["verifiedTimestamps"] = [
        {
            "type": "TimestampAuthority",
            "uri": "https://timestamp.github.com",
            "timestamp": "2030-01-01T00:00:00Z",
        }
    ]
    material = trusted_timestamp.output[0]["attestation"]["bundle"]["verificationMaterial"]
    material.pop("tlogEntries")
    material["timestampVerificationData"] = {
        "rfc3161Timestamps": [{"signedTimestamp": "fixture-rfc3161"}]
    }
    _verify(tmp_path / "accepted", trusted_timestamp)

    missing_material = _FakeCommandRunner()
    missing_material.output[0]["verificationResult"]["verifiedTimestamps"] = [
        {
            "type": "TimestampAuthority",
            "uri": "https://timestamp.github.com",
            "timestamp": "2030-01-01T00:00:00Z",
        }
    ]
    with pytest.raises(attestation.GitHubAttestationPolicyError, match="timestamp verification"):
        _verify(tmp_path / "missing", missing_material)

    current_time = _FakeCommandRunner()
    current_time.output[0]["verificationResult"]["verifiedTimestamps"][0]["type"] = "CurrentTime"
    with pytest.raises(attestation.GitHubAttestationPolicyError, match="untrusted type"):
        _verify(tmp_path / "current-time", current_time)


def test_real_installed_gh_cryptographic_verifier_rejects_malformed_bundle(
    tmp_path: Path,
) -> None:
    assert shutil.which("gh") is not None, "task contract requires installed GitHub CLI"
    subject = tmp_path / "subject.bin"
    subject.write_bytes(b"m4-6 malformed-bundle cryptographic rejection\n")
    bundle = tmp_path / "malformed.sigstore.json"
    bundle.write_text(json.dumps({"not": "a Sigstore bundle"}), encoding="utf-8")
    runner = attestation._GitHubCliRunner()
    request = _request()

    with pytest.raises(attestation.GitHubAttestationCommandError, match="rejected"):
        runner.json(attestation._verify_arguments(subject, bundle, request))


def test_bounded_complete_attestation_download_rejects_cap_and_duplicates(
    tmp_path: Path,
) -> None:
    runner = _FakeCommandRunner()
    subject = tmp_path / "subject.zip"
    subject.write_bytes(b"subject")
    path = attestation._write_exhaustive_attestation_bundle(
        runner,
        (("f" * 64, subject),),
        tmp_path,
        attestation.PREDICATE_TYPE,
    )
    assert len(path.read_text(encoding="utf-8").splitlines()) == 1

    duplicate = _FakeCommandRunner()
    duplicate.output.append(copy.deepcopy(duplicate.output[0]))
    with pytest.raises(attestation.GitHubAttestationPolicyError, match="duplicate"):
        attestation._write_exhaustive_attestation_bundle(
            duplicate,
            (("f" * 64, subject),),
            tmp_path / "duplicate",
            attestation.PREDICATE_TYPE,
        )

    capped = _FakeCommandRunner()
    base = capped.output[0]["attestation"]["bundle"]

    def capped_bundles(
        _subject: Path,
        _destination: Path,
        _predicate_type: str,
    ) -> tuple[dict[str, object], ...]:
        rows = []
        for index in range(attestation.MAX_ATTESTATION_CANDIDATES):
            bundle = copy.deepcopy(base)
            bundle["dsseEnvelope"]["signatures"][0]["sig"] = f"fixture-{index}"
            rows.append(bundle)
        return tuple(rows)

    capped.attestation_bundles = capped_bundles  # type: ignore[method-assign]
    with pytest.raises(attestation.GitHubAttestationPolicyError, match="cap reached"):
        attestation._write_exhaustive_attestation_bundle(
            capped,
            (("f" * 64, subject),),
            tmp_path / "capped",
            attestation.PREDICATE_TYPE,
        )


def test_authenticated_attestation_transport_pins_host_and_separates_public_blob(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    subject = tmp_path / "subject.zip"
    subject.write_bytes(b"subject")
    runner = object.__new__(attestation._GitHubCliRunner)
    commands: list[tuple[str, ...]] = []
    bundle = {"mediaType": "application/vnd.dev.sigstore.bundle.v0.3+json"}

    def api_json(arguments: tuple[str, ...], *, max_bytes: int, timeout: int) -> object:
        assert max_bytes == attestation.MAX_ATTESTATION_METADATA_BYTES
        assert timeout == 180
        commands.append(arguments)
        return [
            {
                "attestations": [
                    {
                        "repository_id": attestation.REPOSITORY_ID,
                        "bundle_url": (
                            "https://tmaproduction.blob.core.windows.net/attestations/example"
                        ),
                        "initiator": "actions",
                    }
                ]
            }
        ]

    runner._bounded_json = api_json  # type: ignore[method-assign]
    seen_urls: list[str] = []

    def public_download(url: str, *, max_bytes: int) -> tuple[dict[str, object], int, int]:
        assert max_bytes == attestation.MAX_BUNDLE_SIZE_BYTES
        seen_urls.append(url)
        return bundle, 8, 16

    monkeypatch.setattr(attestation, "_download_public_attestation_bundle", public_download)
    assert runner.attestation_bundles(subject, tmp_path / "unused", attestation.PREDICATE_TYPE) == (
        bundle,
    )
    assert commands[0][:5] == (
        "api",
        "--hostname",
        "github.com",
        "--paginate",
        "--slurp",
    )
    assert "predicate_type=https%3A%2F%2Fastrid.dev%2Fattestations" in commands[0][5]
    assert seen_urls == ["https://tmaproduction.blob.core.windows.net/attestations/example"]


def test_offline_gh_environment_removes_auth_and_host_selection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in attestation._GH_AUTH_ENVIRONMENT:
        monkeypatch.setenv(name, "sensitive")
    monkeypatch.setenv("GH_CONFIG_DIR", "/sensitive/config")
    monkeypatch.setenv("M4_6_UNRELATED", "preserved")
    environment = attestation._offline_gh_environment(tmp_path)
    assert not attestation._GH_AUTH_ENVIRONMENT.intersection(environment)
    assert environment["GH_CONFIG_DIR"] == str(tmp_path)
    assert environment["M4_6_UNRELATED"] == "preserved"


def test_source_reviewed_snappy_decoder_and_bundle_url_policy() -> None:
    literal = b'{"ok":true}'
    encoded_literal = bytes((len(literal), (len(literal) - 1) << 2)) + literal
    assert attestation._snappy_decompress(encoded_literal) == literal
    assert attestation._snappy_decompress(bytes((9, 8)) + b"abc" + bytes((22, 3, 0))) == (
        b"abcabcabc"
    )
    with pytest.raises(attestation.GitHubAttestationPolicyError, match="untrusted bundle URL"):
        attestation._validated_bundle_url("https://example.invalid/bundle")
    with pytest.raises(attestation.GitHubAttestationPolicyError, match="decoded size"):
        attestation._snappy_decompress(b"\x05\x00x")


def test_production_download_stops_before_writing_over_bound(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = tmp_path / "oversize-gh"
    executable.write_text(
        "#!/usr/bin/env python3\nimport os\nos.write(1, b'x' * 65)\n",
        encoding="utf-8",
    )
    executable.chmod(0o700)
    runner = object.__new__(attestation._GitHubCliRunner)
    runner._executable = str(executable)
    monkeypatch.setattr(attestation, "MAX_ARTIFACT_SIZE_BYTES", 64)
    destination = tmp_path / "oversize.zip"

    with pytest.raises(attestation.GitHubAttestationPolicyError, match="64 MiB"):
        runner.download((), destination)
    assert not destination.exists()


def test_bounded_api_json_kills_output_before_unbounded_capture(tmp_path: Path) -> None:
    executable = tmp_path / "oversize-json"
    executable.write_text(
        "#!/usr/bin/env python3\nimport os\nos.write(1, b'x' * 65)\n",
        encoding="utf-8",
    )
    executable.chmod(0o700)
    runner = object.__new__(attestation._GitHubCliRunner)
    runner._executable = str(executable)
    with pytest.raises(attestation.GitHubAttestationPolicyError, match="metadata exceeded"):
        runner._bounded_json(("api",), max_bytes=64, timeout=10)


def test_attestation_transport_enforces_cumulative_bundle_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    subject = tmp_path / "subject.zip"
    subject.write_bytes(b"subject")
    runner = object.__new__(attestation._GitHubCliRunner)
    row = {
        "repository_id": attestation.REPOSITORY_ID,
        "bundle_url": "https://tmaproduction.blob.core.windows.net/attestations/example",
        "initiator": "actions",
    }
    runner._bounded_json = lambda *_args, **_kwargs: [  # type: ignore[method-assign]
        {"attestations": [copy.deepcopy(row), copy.deepcopy(row)]}
    ]
    calls: list[int] = []

    def public_download(_url: str, *, max_bytes: int) -> tuple[dict[str, object], int, int]:
        calls.append(max_bytes)
        if len(calls) == 1:
            return {"bundle": 1}, attestation.MAX_BUNDLE_SIZE_BYTES - 1, 12
        return {"bundle": 2}, 2, 12

    monkeypatch.setattr(attestation, "_download_public_attestation_bundle", public_download)
    with pytest.raises(attestation.GitHubAttestationPolicyError, match="cumulative bound"):
        runner.attestation_bundles(subject, tmp_path / "unused", attestation.PREDICATE_TYPE)
    assert calls == [attestation.MAX_BUNDLE_SIZE_BYTES, 1]


def test_production_workflow_never_self_selects_verifier_revision_or_truncates() -> None:
    workflow = (
        Path(__file__).parents[1] / ".github" / "workflows" / "m4-6-trusted-attestor.yml"
    ).read_text(encoding="utf-8")
    assert "--limit" not in workflow
    assert "UNCONFIGURED_INDEPENDENT_REVIEW_REQUIRED" in workflow
    assert workflow.count('--workflow-sha "${{ github.workflow_sha }}"') == 1
    assert workflow.count('--workflow-sha "$M4_6_REVIEWED_CALLER_WORKFLOW_SHA"') == 2
    assert workflow.count('--signer-workflow-sha "$M4_6_REVIEWED_SIGNER_WORKFLOW_SHA"') == 2
    assert workflow.count('--source-sha "$M4_6_REVIEWED_CALLER_WORKFLOW_SHA"') == 2
    assert workflow.count('--source-ref "$M4_6_REVIEWED_SOURCE_REF"') == 2
    assert workflow.count('--event-name "$M4_6_REVIEWED_SOURCE_EVENT"') == 2


def test_receipt_owner_pid_is_enforced(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    receipt = _verify(tmp_path, _FakeCommandRunner())
    real_pid = attestation.os.getpid()
    monkeypatch.setattr(attestation.os, "getpid", lambda: real_pid + 1)
    with pytest.raises(attestation.GitHubAttestationPolicyError, match="another process"):
        attestation._claim_artifact_check_receipt(receipt)


def test_runtime_selector_mutations_are_rejected_before_commands(tmp_path: Path) -> None:
    runner = _FakeCommandRunner()
    request = replace(_request(), artifact_ids=_request().artifact_ids[:-1])
    with pytest.raises(attestation.GitHubAttestationPolicyError, match="exactly nine"):
        attestation.verify_github_attestation(
            request,
            tmp_path / "custody",
            _command_runner=runner,
        )
    assert runner.commands == []


def test_prepare_predicate_cli_writes_closed_schema_and_subjects(tmp_path: Path) -> None:
    runner = _FakeCommandRunner()
    predicate_path = tmp_path / "attestation" / "predicate.json"
    subjects_path = tmp_path / "attestation" / "subjects.sha256"
    bindings = []
    for policy, artifact in zip(attestation.TRUSTED_SLOT_REGISTRY, runner.artifacts, strict=True):
        bindings.extend(
            [
                "--binding",
                "|".join(
                    (
                        policy.slot,
                        policy.producer_job_name,
                        policy.slot,
                        policy.artifact_name,
                        str(artifact["id"]),
                        str(artifact["digest"]),
                        str(RUN_ATTEMPT),
                    )
                ),
            ]
        )
    result = attestation._main(
        [
            "prepare-predicate",
            "--run-id",
            str(RUN_ID),
            "--run-attempt",
            str(RUN_ATTEMPT),
            "--source-sha",
            SOURCE_SHA,
            "--source-ref",
            SOURCE_REF,
            "--event-name",
            "workflow_dispatch",
            "--workflow-ref",
            WORKFLOW_REF,
            "--workflow-sha",
            WORKFLOW_SHA,
            "--predicate-output",
            str(predicate_path),
            "--subjects-output",
            str(subjects_path),
            *bindings,
        ],
        _command_runner=runner,
    )
    assert result == 0
    predicate = json.loads(predicate_path.read_text(encoding="utf-8"))
    assert set(predicate) == {
        "version",
        "registryDigest",
        "repository",
        "workflow",
        "run",
        "artifacts",
    }
    assert predicate["registryDigest"] == attestation.REGISTRY_DIGEST
    assert [row["slot"] for row in predicate["artifacts"]] == [
        row.slot for row in attestation.TRUSTED_SLOT_REGISTRY
    ]
    lines = subjects_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 9
    assert lines[0].endswith("  m4-6-direct-macos")


def test_verify_cli_resolves_checker_id_from_exact_attempt_api(
    tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    runner = _FakeCommandRunner()
    github_output = tmp_path / "github-output"
    github_output.touch()
    artifact_arguments = [
        value
        for artifact_id in _request().artifact_ids
        for value in ("--artifact-id", str(artifact_id))
    ]
    result = attestation._main(
        [
            "verify",
            "--run-id",
            str(RUN_ID),
            "--run-attempt",
            str(RUN_ATTEMPT),
            "--source-sha",
            SOURCE_SHA,
            "--source-ref",
            SOURCE_REF,
            "--event-name",
            "workflow_dispatch",
            "--workflow-path",
            WORKFLOW_PATH,
            "--workflow-ref",
            WORKFLOW_REF,
            "--workflow-sha",
            WORKFLOW_SHA,
            "--signer-workflow-ref",
            SIGNER_REF,
            "--signer-workflow-sha",
            SIGNER_SHA,
            "--download-root",
            str(tmp_path / "cli-custody"),
            "--github-output",
            str(github_output),
            *artifact_arguments,
        ],
        _command_runner=runner,
    )
    assert result == 0
    output = json.loads(capfd.readouterr().out)
    assert output["checkerJobId"] == CHECKER_JOB_ID
    written = github_output.read_text(encoding="utf-8")
    assert "checked_output_sha256=" in written
    assert f"registry_digest={attestation.REGISTRY_DIGEST}" in written


def test_isolated_canary_verifies_one_nonrelease_subject(tmp_path: Path) -> None:
    runner = _FakeCommandRunner()
    artifact = runner.artifacts[0]
    artifact["name"] = attestation.CANARY_ARTIFACT_NAME
    runner.artifacts = [artifact]
    runner.run["path"] = attestation.CANARY_WORKFLOW_PATH
    canary_ref = (
        f"{attestation.REPOSITORY_FULL_NAME}/{attestation.CANARY_WORKFLOW_PATH}@{SOURCE_REF}"
    )
    digest = str(artifact["digest"]).removeprefix("sha256:")
    result = runner.output[0]["verificationResult"]
    result["statement"] = {
        "_type": "https://in-toto.io/Statement/v1",
        "subject": [{"name": attestation.CANARY_ARTIFACT_NAME, "digest": {"sha256": digest}}],
        "predicateType": attestation.CANARY_PREDICATE_TYPE,
        "predicate": {
            "version": 1,
            "kind": "nonrelease-synthetic-canary",
            "repository": {
                "id": attestation.REPOSITORY_ID,
                "fullName": attestation.REPOSITORY_FULL_NAME,
                "ownerId": attestation.REPOSITORY_OWNER_ID,
                "ownerName": attestation.REPOSITORY_OWNER,
            },
            "workflow": {
                "id": WORKFLOW_ID,
                "path": attestation.CANARY_WORKFLOW_PATH,
                "ref": canary_ref,
                "sha": WORKFLOW_SHA,
            },
            "run": {
                "id": RUN_ID,
                "attempt": RUN_ATTEMPT,
                "headSha": SOURCE_SHA,
                "sourceRef": SOURCE_REF,
                "event": "workflow_dispatch",
            },
            "artifact": {
                "id": artifact["id"],
                "name": attestation.CANARY_ARTIFACT_NAME,
                "digest": artifact["digest"],
                "sizeBytes": artifact["size_in_bytes"],
            },
        },
    }
    certificate = result["signature"]["certificate"]
    certificate["subjectAlternativeName"] = f"https://github.com/{canary_ref}"
    certificate["buildSignerURI"] = f"https://github.com/{canary_ref}"
    certificate["buildSignerDigest"] = WORKFLOW_SHA
    certificate["buildConfigURI"] = f"https://github.com/{canary_ref}"
    certificate["buildConfigDigest"] = WORKFLOW_SHA
    summary = attestation.verify_canary_attestation(
        attestation.CanaryVerificationRequest(
            run_id=RUN_ID,
            run_attempt=RUN_ATTEMPT,
            artifact_id=int(artifact["id"]),
            source_sha=SOURCE_SHA,
            source_ref=SOURCE_REF,
            source_event="workflow_dispatch",
            workflow_id=WORKFLOW_ID,
            workflow_ref=canary_ref,
            workflow_sha=WORKFLOW_SHA,
        ),
        tmp_path / "canary-custody",
        _command_runner=runner,
    )
    assert summary.artifact_name == attestation.CANARY_ARTIFACT_NAME
    assert summary.artifact_sha256 == digest
    assert len([cmd for cmd in runner.commands if cmd[:2] == ("attestation", "verify")]) == 1
