"""Small, explicit lifecycle receipt for one H3 transformation."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Mapping, Sequence

from astrid.sdk.results import InvocationResult


class ReceiptError(ValueError):
    """A final H3 receipt is incomplete or internally contradictory."""


_STATES = (
    "task_succeeded",
    "raw_managed_publication",
    "candidate_verified",
    "final_composition_publication",
    "editorially_approved",
    "cleanup_verified",
)
_PUBLICATION_SEAL = object()
_SHA256_RE = re.compile(r"sha256:[0-9a-f]{64}")


class _RuntimeManagedPublication:
    """Sealed result of validating one Runtime-owned publication settlement."""

    __slots__ = ("evidence", "status")

    def __init__(
        self,
        *,
        status: str,
        evidence: Mapping[str, Any],
        _seal: object,
    ) -> None:
        if _seal is not _PUBLICATION_SEAL:
            raise ReceiptError(
                "raw managed publication evidence must be derived from a Runtime invocation result"
            )
        self.status = status
        self.evidence = dict(evidence)


def _evidence(value: Mapping[str, Any] | None) -> dict[str, Any]:
    return dict(value or {})


def _stage(
    status: str,
    evidence: Mapping[str, Any] | None = None,
    *,
    reason: str | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {"status": status, "evidence": _evidence(evidence)}
    if reason:
        result["reason"] = reason
    return result


def _nonempty(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _expected_volume_id(target: Mapping[str, Any]) -> str | None:
    """Resolve one admitted volume identity from legacy or nested target fields."""

    locations: list[Mapping[str, Any]] = [target]
    storage = target.get("storage")
    if storage is not None:
        if not isinstance(storage, Mapping):
            raise ReceiptError("admitted target storage must be an object")
        locations.append(storage)

    identities: set[str] = set()
    for location in locations:
        for field in ("backup_volume_id", "network_volume_id", "volume_id"):
            if field not in location:
                continue
            value = _nonempty(location.get(field))
            if value is None:
                raise ReceiptError(f"admitted target {field} is missing or empty")
            identities.add(value)
    if len(identities) > 1:
        raise ReceiptError("admitted target has contradictory volume identities")
    return next(iter(identities), None)


def _digest(value: Mapping[str, Any]) -> str | None:
    raw = value.get("object_id") or value.get("digest") or value.get("content_hash")
    if not isinstance(raw, str):
        return None
    normalized = raw if raw.startswith("sha256:") else "sha256:" + raw
    return normalized if _SHA256_RE.fullmatch(normalized) else None


def _failure_publication(
    runtime_result: InvocationResult,
    reason: str,
) -> _RuntimeManagedPublication:
    return _RuntimeManagedPublication(
        status="failed",
        evidence={
            "effect_type": "generation.publish_v1",
            "phase": "vibecomfy.run_settlement",
            "published_scope": "raw_generation",
            "final_composition_publication": "deferred",
            "task": {
                "run_id": runtime_result.kernel_run_id,
                "task_id": runtime_result.kernel_task_id,
                "attempt_id": runtime_result.kernel_attempt_id,
            },
            "validation_error": reason,
        },
        _seal=_PUBLICATION_SEAL,
    )


def attest_runtime_managed_publication(
    *,
    runtime_result: InvocationResult,
    request_digest: str,
    generation_intent: Mapping[str, Any],
    retrieved_outputs: Sequence[Mapping[str, Any]],
) -> _RuntimeManagedPublication:
    """Validate the raw H3 publication already committed by Runtime settlement.

    The accepted source is the concrete ``InvocationResult`` returned by the
    connected SDK after its Runtime task read and managed-output readback. The
    final receipt deliberately has no mapping/JSON publication-witness input.
    """

    if not isinstance(runtime_result, InvocationResult):
        raise ReceiptError(
            "raw managed publication evidence requires an SDK InvocationResult"
        )

    def fail(reason: str) -> _RuntimeManagedPublication:
        return _failure_publication(runtime_result, reason)

    if runtime_result.capability_id != "vibecomfy.run" or not runtime_result.ok:
        return fail("publication source is not a successful canonical vibecomfy.run result")
    run_id = _nonempty(runtime_result.kernel_run_id)
    task_id = _nonempty(runtime_result.kernel_task_id)
    attempt_id = _nonempty(runtime_result.kernel_attempt_id)
    if None in (run_id, task_id, attempt_id):
        return fail("publication source lacks canonical run/task/attempt identity")
    if not _nonempty(request_digest):
        return fail("publication request digest is missing")

    raw = runtime_result.raw_result
    if not isinstance(raw, Mapping):
        return fail("Runtime invocation result has no structured settlement")
    for field, expected in (
        ("kernel_run_id", run_id),
        ("kernel_task_id", task_id),
        ("kernel_attempt_id", attempt_id),
    ):
        if raw.get(field) != expected:
            return fail(f"Runtime settlement {field} disagrees with the invocation identity")
    if raw.get("state") not in {"completed", "succeeded"}:
        return fail("Runtime task did not return a completed settlement")

    task = raw.get("task")
    settled = raw.get("result")
    managed_outputs = raw.get("managed_outputs")
    if not isinstance(task, Mapping) or not isinstance(settled, Mapping):
        return fail("Runtime settlement is missing its task or result resource")
    if not isinstance(managed_outputs, list):
        return fail("Runtime settlement is missing managed-output readback")
    observed_task_id = task.get("task_id", task.get("id"))
    if observed_task_id != task_id or task.get("attempt_id") != attempt_id:
        return fail("Runtime task resource disagrees with task/attempt identity")
    if task.get("run_id") not in (None, run_id):
        return fail("Runtime task resource disagrees with run identity")
    task_result = task.get("result")
    if isinstance(task_result, Mapping) and dict(task_result) != dict(settled):
        return fail("Runtime task and invocation settlement results conflict")

    project_id = _nonempty(task.get("project_id"))
    expected_effect = task.get("expected_effect")
    if project_id is None:
        return fail("Runtime task is missing its project identity")
    if task.get("generation_intent") != dict(generation_intent):
        return fail("Runtime task generation intent disagrees with the sealed H3 compilation")
    if not isinstance(expected_effect, Mapping):
        return fail("Runtime task publication effect is missing or malformed")
    if expected_effect.get("effect_type") != "generation.publish_v1":
        return fail("Runtime task did not admit generation.publish_v1")
    if expected_effect.get("target_id") != project_id:
        return fail("publication effect target disagrees with the task project")
    payload = expected_effect.get("payload")
    if not isinstance(payload, Mapping) or payload.get("generation_type") != "vibecomfy.run":
        return fail("publication effect is not the canonical vibecomfy.run generation effect")
    metadata = payload.get("metadata")
    h3_metadata = metadata.get("h3_av") if isinstance(metadata, Mapping) else None
    if not isinstance(h3_metadata, Mapping) or h3_metadata.get("request_digest") != request_digest:
        return fail("publication effect is not bound to the H3 request digest")
    groups = payload.get("groups")
    if not isinstance(groups, list) or len(groups) != 1:
        return fail("H3 publication must contain exactly one output group")
    group = groups[0]
    selectors = group.get("selectors") if isinstance(group, Mapping) else None
    if (
        not isinstance(group, Mapping)
        or group.get("group_key") != "main"
        or not isinstance(selectors, list)
        or len(selectors) != 1
        or not isinstance(selectors[0], Mapping)
    ):
        return fail("H3 publication must contain the sealed main selector")
    selector = dict(selectors[0])
    if selector.get("output_port") != "vibecomfy_run":
        return fail("Runtime publication effect is missing the canonical H3 output port")
    if any(
        selector.get(field) != generation_intent["groups"][0]["selectors"][0].get(field)
        for field in ("selector", "ordinal", "variant_key")
    ):
        return fail("publication effect selector disagrees with the H3 generation intent")

    applied = settled.get("generation_publish_v1")
    publications = applied.get("publications") if isinstance(applied, Mapping) else None
    if (
        not isinstance(applied, Mapping)
        or applied.get("effect_type") != "generation.publish_v1"
        or not isinstance(publications, list)
        or len(publications) != 1
        or not isinstance(publications[0], Mapping)
    ):
        return fail("Runtime settlement has no canonical generation.publish_v1 result")
    publication = publications[0]
    variants = publication.get("variants")
    if (
        publication.get("group_key") != "main"
        or publication.get("missing_selectors") != []
        or not isinstance(variants, list)
        or len(variants) != 1
        or not isinstance(variants[0], Mapping)
    ):
        return fail("Runtime publication result does not contain the complete H3 main output")
    variant = variants[0]
    generation_id = _nonempty(publication.get("generation_id"))
    variant_id = _nonempty(variant.get("variant_id"))
    object_id = _digest(variant)
    if None in (generation_id, variant_id, object_id):
        return fail("Runtime publication result lacks generation/variant/object identity")
    for field in ("output_port", "ordinal", "variant_key"):
        if variant.get(field) != selector.get(field):
            return fail(f"published variant {field} disagrees with the admitted selector")
    if variant.get("generation_id") != generation_id:
        return fail("published variant disagrees with its generation identity")

    settled_outputs = settled.get("outputs")
    if not isinstance(settled_outputs, list):
        return fail("Runtime settlement has no typed output list")
    matching_settled = [
        row
        for row in settled_outputs
        if isinstance(row, Mapping)
        and _digest(row) == object_id
        and row.get("output_port") == selector.get("output_port")
        and row.get("group_key") == "main"
        and row.get("variant_key") == selector.get("variant_key")
        and row.get("ordinal") == selector.get("ordinal")
    ]
    if len(matching_settled) != 1:
        return fail("published variant does not match exactly one settled task output")

    matching_managed = [
        row
        for row in managed_outputs
        if isinstance(row, Mapping)
        and _digest(row) == object_id
        and row.get("task_id") == task_id
        and row.get("attempt_id") == attempt_id
        and row.get("project_id") == project_id
        and row.get("generation_id") == generation_id
        and row.get("variant_key") == selector.get("variant_key")
        and row.get("output_port") == selector.get("output_port")
        and row.get("group_key") == "main"
        and row.get("ordinal") == selector.get("ordinal")
    ]
    if len(matching_managed) != 1:
        return fail("published variant has no unique Runtime managed-output association")
    managed = matching_managed[0]
    association_id = _nonempty(managed.get("association_id"))
    if association_id is None:
        return fail("Runtime managed publication has no association identity")

    matching_retrieved = [
        row
        for row in retrieved_outputs
        if isinstance(row, Mapping) and _digest(row) == object_id
    ]
    if len(matching_retrieved) != 1:
        return fail("published raw object was not uniquely retrieved and digest-verified locally")

    return _RuntimeManagedPublication(
        status="passed",
        evidence={
            "effect_type": "generation.publish_v1",
            "phase": "vibecomfy.run_settlement",
            "published_scope": "raw_generation",
            "final_composition_publication": "deferred",
            "request_digest": request_digest,
            "task": {
                "run_id": run_id,
                "task_id": task_id,
                "attempt_id": attempt_id,
                "project_id": project_id,
            },
            "publication": {
                "generation_id": generation_id,
                "variant_id": variant_id,
                "association_id": association_id,
                "object_id": object_id,
                "output_port": selector.get("output_port"),
                "group_key": "main",
                "variant_key": selector.get("variant_key"),
                "ordinal": selector.get("ordinal"),
            },
        },
        _seal=_PUBLICATION_SEAL,
    )


def _cleanup_report(
    value: Mapping[str, Any] | None,
    *,
    expected_target: Mapping[str, Any] | None,
) -> tuple[dict[str, Any], str]:
    if value is None:
        return {"resources": [], "status": "not_claimed"}, "not_claimed"
    if expected_target is None:
        return {
            "resources": [],
            "status": "failed",
            "reason": "cleanup cannot be bound to an admitted target",
        }, "failed"

    target = value.get("target")
    if not isinstance(target, Mapping) or dict(target) != dict(expected_target):
        raise ReceiptError("cleanup target disagrees with the admitted H3 target")
    pod_id = _nonempty(expected_target.get("pod_id"))
    if expected_target.get("kind") != "runpod" or pod_id is None:
        return {
            "resources": [],
            "status": "failed",
            "target": dict(expected_target),
            "reason": "cleanup target lacks an exact RunPod pod identity",
        }, "failed"

    pending = (
        value.get("status") == "cleanup_pending"
        or value.get("state") == "cleanup_pending"
        or value.get("cleanup_pending") is True
    )
    raw_resources = value.get("resources", [])
    if not isinstance(raw_resources, list):
        raise ReceiptError("cleanup.resources must be an array")
    # A failed verification is unresolved cleanup, even if the caller omitted
    # the explicit lifecycle marker. Never turn uncertain teardown into a
    # terminal failure that can be mistaken for reconciled ownership.
    pending = pending or any(
        isinstance(resource, Mapping) and resource.get("verified") is False
        for resource in raw_resources
    )
    resources: list[dict[str, Any]] = []
    identities: set[tuple[str, str]] = set()
    resource_checks: list[bool] = []
    expected_volume_id = _expected_volume_id(expected_target)
    for index, raw in enumerate(raw_resources):
        if not isinstance(raw, Mapping):
            raise ReceiptError(f"cleanup.resources[{index}] must be an object")
        kind = raw.get("kind")
        resource_id = raw.get("id")
        owned = raw.get("owned")
        expected = raw.get("expected_postcondition")
        observed = raw.get("observed_postcondition")
        verified = raw.get("verified")
        owner_target = raw.get("owner_target")
        if (
            not isinstance(kind, str)
            or not isinstance(resource_id, str)
            or not isinstance(expected, str)
            or not isinstance(observed, str)
            or not all(item.strip() for item in (kind, resource_id, expected, observed))
        ):
            raise ReceiptError(
                f"cleanup.resources[{index}] requires kind, id, "
                "expected_postcondition, and observed_postcondition"
            )
        if type(owned) is not bool or type(verified) is not bool:
            raise ReceiptError(
                f"cleanup.resources[{index}] owned and verified must be booleans"
            )
        if not isinstance(owner_target, Mapping) or dict(owner_target) != dict(expected_target):
            raise ReceiptError(
                f"cleanup.resources[{index}] owner_target disagrees with the admitted H3 target"
            )
        identity = (kind.strip(), resource_id.strip())
        if identity in identities:
            raise ReceiptError(
                f"cleanup.resources contains duplicate resource {identity!r}"
            )
        identities.add(identity)
        postcondition = observed.strip().casefold().replace("_", " ")
        if identity == ("runpod.pod", pod_id):
            identity_verified = (
                owned is True
                and expected.strip().casefold().replace("_", " ")
                in {"terminated", "terminated and absent", "terminated and absent from provider list"}
                and observed.strip().casefold().replace("_", " ")
                in {"terminated", "terminated and absent", "terminated and absent from provider list"}
                and expected.strip() == observed.strip()
                and verified is True
            )
        elif expected_volume_id is not None and identity == ("runpod.volume", expected_volume_id):
            identity_verified = (
                owned is False
                and expected.strip().casefold() == "preserved"
                and postcondition == "preserved"
                and verified is True
            )
        else:
            identity_verified = False
        resource_checks.append(identity_verified)
        resources.append(
            {
                "kind": identity[0],
                "id": identity[1],
                "owned": owned,
                "owner_target": dict(expected_target),
                "expected_postcondition": expected.strip(),
                "observed_postcondition": observed.strip(),
                "verified": verified,
            }
        )
    required_count = 1 + (1 if expected_volume_id is not None else 0)
    required_identities = {("runpod.pod", pod_id)}
    if expected_volume_id is not None:
        required_identities.add(("runpod.volume", expected_volume_id))
    cleanup_status = (
        "passed"
        if len(resources) == required_count
        and identities == required_identities
        and all(resource_checks)
        else "failed"
    )
    if not resources:
        cleanup_status = "not_claimed"
    if pending:
        cleanup_status = "cleanup_pending"
    return {
        "resources": resources,
        "status": cleanup_status,
        "target": dict(expected_target),
    }, cleanup_status


def attest_runtime_managed_composition(
    *,
    runtime_result: InvocationResult,
    request_digest: str,
    candidate_verified: Mapping[str, Any],
    retrieved_outputs: Sequence[Mapping[str, Any]],
    raw_managed_publication: _RuntimeManagedPublication | None = None,
) -> _RuntimeManagedPublication:
    """Validate the Runtime finalizer's verified-candidate publication."""
    verification = candidate_verified.get("verification") if isinstance(candidate_verified, Mapping) else None
    candidate_digest = verification.get("candidate_sha256") if isinstance(verification, Mapping) else None
    def failed(reason: str) -> _RuntimeManagedPublication:
        return _RuntimeManagedPublication(
            status="failed",
            evidence={"validation_error": reason, "published_scope": "final_composition"},
            _seal=_PUBLICATION_SEAL,
        )
    if (
        not isinstance(verification, Mapping)
        or verification.get("status") != "verified"
        or verification.get("request_digest") != request_digest
        or not isinstance(candidate_digest, str)
        or not re.fullmatch(r"[0-9a-f]{64}", candidate_digest)
    ):
        return failed("verified composition has no candidate digest")
    if not isinstance(raw_managed_publication, _RuntimeManagedPublication) or raw_managed_publication.status != "passed":
        return failed("final composition has no verified raw Runtime lineage")
    raw_task = raw_managed_publication.evidence.get("task")
    raw_publication = raw_managed_publication.evidence.get("publication")
    if not isinstance(raw_task, Mapping) or not isinstance(raw_publication, Mapping):
        return failed("raw Runtime lineage is missing task or publication identity")
    raw_object_id = _digest(raw_publication)
    raw_identity = tuple(raw_task.get(field) for field in ("run_id", "task_id", "attempt_id"))
    raw_generation_id = _nonempty(raw_publication.get("generation_id"))
    raw_variant_id = _nonempty(raw_publication.get("variant_id"))
    raw_association_id = _nonempty(raw_publication.get("association_id"))
    raw_group_key = _nonempty(raw_publication.get("group_key"))
    raw_variant_key = _nonempty(raw_publication.get("variant_key"))
    raw_output_port = _nonempty(raw_publication.get("output_port"))
    if (
        raw_object_id is None
        or not all(isinstance(value, str) and value for value in raw_identity)
        or None in (raw_generation_id, raw_variant_id, raw_association_id, raw_group_key, raw_variant_key, raw_output_port)
    ):
        return failed("raw Runtime lineage lacks complete generation/variant/group identity")
    if not isinstance(runtime_result, InvocationResult) or runtime_result.capability_id != "h3_av.publication_finalizer" or not runtime_result.ok:
        return failed("publication source is not the canonical H3 finalizer")
    run_id, task_id, attempt_id = runtime_result.kernel_run_id, runtime_result.kernel_task_id, runtime_result.kernel_attempt_id
    final_identity = (run_id, task_id, attempt_id)
    if not all(isinstance(value, str) and value for value in final_identity):
        return failed("finalizer readback lacks task identity or settlement")
    if any(final == raw for final, raw in zip(final_identity, raw_identity)):
        return failed("finalizer task/run/attempt must be distinct from raw Runtime lineage")
    raw = runtime_result.raw_result
    if not isinstance(raw, Mapping):
        return failed("finalizer readback lacks task identity or settlement")
    if raw.get("state") not in {"completed", "succeeded"} or raw.get("kernel_task_id") != task_id or raw.get("kernel_attempt_id") != attempt_id:
        return failed("finalizer settlement identity is incomplete or contradictory")
    task, settled, managed_outputs = raw.get("task"), raw.get("result"), raw.get("managed_outputs")
    if not isinstance(task, Mapping) or not isinstance(settled, Mapping) or not isinstance(managed_outputs, list):
        return failed("finalizer settlement lacks task, result, or managed-output readback")
    if task.get("task_id", task.get("id")) != task_id or task.get("attempt_id") != attempt_id:
        return failed("finalizer task identity disagrees with invocation")
    project_id = _nonempty(task.get("project_id"))
    effect = task.get("expected_effect")
    if project_id is None or not isinstance(effect, Mapping) or effect.get("effect_type") != "generation.publish_v1" or effect.get("target_id") != project_id:
        return failed("finalizer did not admit the Runtime generation publication effect")
    payload = effect.get("payload")
    if not isinstance(payload, Mapping) or payload.get("generation_type") != "h3_av.publication_finalizer":
        return failed("finalizer publication is not the canonical H3 finalizer generation")
    metadata = payload.get("metadata") if isinstance(payload, Mapping) else None
    h3_metadata = metadata.get("h3_av") if isinstance(metadata, Mapping) else None
    expected_metadata = {
        "request_digest": request_digest,
        "publication_scope": "final_composition",
        "candidate_sha256": candidate_digest,
        "raw_sha256": raw_object_id.removeprefix("sha256:"),
        "raw_generation_id": raw_generation_id,
        "raw_variant_id": raw_variant_id,
        "raw_association_id": raw_association_id,
        "raw_group_key": raw_group_key,
        "raw_variant_key": raw_variant_key,
        "raw_output_port": raw_output_port,
    }
    if not isinstance(h3_metadata, Mapping) or any(
        h3_metadata.get(field) != expected for field, expected in expected_metadata.items()
    ):
        return failed("finalizer publication is not bound to request, candidate, and raw lineage")
    groups = payload.get("groups") if isinstance(payload, Mapping) else None
    group = groups[0] if isinstance(groups, list) and len(groups) == 1 and isinstance(groups[0], Mapping) else None
    selectors = group.get("selectors") if isinstance(group, Mapping) else None
    if (
        not isinstance(group, Mapping)
        or group.get("group_key") != "main"
        or not isinstance(selectors, list)
        or len(selectors) != 1
        or not isinstance(selectors[0], Mapping)
        or selectors[0].get("selector") != "final-composition"
        or selectors[0].get("output_port") != "verified_candidate"
        or selectors[0].get("variant_key") != "final-composition"
    ):
        return failed("finalizer publication does not select verified_candidate")
    selector = selectors[0]
    applied = settled.get("generation_publish_v1")
    publications = applied.get("publications") if isinstance(applied, Mapping) else None
    publication = publications[0] if isinstance(publications, list) and len(publications) == 1 and isinstance(publications[0], Mapping) else None
    variants = publication.get("variants") if isinstance(publication, Mapping) else None
    variant = variants[0] if isinstance(variants, list) and len(variants) == 1 and isinstance(variants[0], Mapping) else None
    object_id = _digest(variant) if isinstance(variant, Mapping) else None
    if (
        object_id != "sha256:" + candidate_digest
        or publication.get("group_key") != "main"
        or publication.get("missing_selectors") != []
        or not _nonempty(publication.get("generation_id"))
        or not _nonempty(variant.get("variant_id"))
        or variant.get("generation_id") != publication.get("generation_id")
        or variant.get("output_port") != "verified_candidate"
        or variant.get("ordinal") != selector.get("ordinal")
        or variant.get("variant_key") != selector.get("variant_key")
    ):
        return failed("published final object does not match verified composition")
    matches = [
        row for row in managed_outputs
        if isinstance(row, Mapping)
        and _digest(row) == object_id
        and row.get("task_id") == task_id
        and row.get("attempt_id") == attempt_id
        and row.get("project_id") == project_id
        and row.get("generation_id") == publication.get("generation_id")
        and row.get("output_port") == "verified_candidate"
        and row.get("group_key") == "main"
        and row.get("variant_key") == selector.get("variant_key")
        and row.get("ordinal") == selector.get("ordinal")
        and _nonempty(row.get("association_id"))
    ]
    expected_size = verification.get("candidate_size") if isinstance(verification, Mapping) else None
    retrieved = [
        row for row in retrieved_outputs
        if isinstance(row, Mapping)
        and _digest(row) == object_id
        and row.get("verified") is True
        and isinstance(row.get("size"), int)
        and (expected_size is None or row.get("size") == expected_size)
        and (row.get("sha256") in (None, candidate_digest))
    ]
    if len(matches) != 1 or len(retrieved) != 1 or object_id == raw_object_id:
        return failed("final composition was not uniquely associated and locally retrieved")
    return _RuntimeManagedPublication(
        status="passed",
        evidence={
            "effect_type": "generation.publish_v1",
            "phase": "h3_av.publication_finalizer",
            "published_scope": "final_composition",
            "request_digest": request_digest,
            "task": {"run_id": run_id, "task_id": task_id, "attempt_id": attempt_id, "project_id": project_id},
            "publication": {
                "object_id": object_id,
                "association_id": matches[0]["association_id"],
                "generation_id": publication.get("generation_id"),
                "variant_id": variant.get("variant_id"),
                "output_port": "verified_candidate",
                "group_key": "main",
                "ordinal": selector.get("ordinal"),
                "variant_key": selector.get("variant_key"),
            },
            "lineage": {
                "raw_object_id": raw_object_id,
                "raw_generation_id": raw_generation_id,
                "raw_variant_id": raw_variant_id,
                "raw_association_id": raw_association_id,
                "raw_group_key": raw_group_key,
                "raw_variant_key": raw_variant_key,
                "raw_output_port": raw_output_port,
            },
        },
        _seal=_PUBLICATION_SEAL,
    )


def _candidate_report(
    value: Mapping[str, Any] | None,
    *,
    request_digest: str,
    task_status: str,
) -> tuple[dict[str, Any], str]:
    verification = value.get("verification") if isinstance(value, Mapping) else None
    valid = (
        task_status == "passed"
        and isinstance(verification, Mapping)
        and verification.get("status") == "verified"
        and verification.get("request_digest") == request_digest
        and isinstance(verification.get("candidate_sha256"), str)
        and re.fullmatch(r"[0-9a-f]{64}", verification["candidate_sha256"])
    )
    status = "passed" if valid else "failed"
    return _stage(
        status,
        value,
        reason=None if valid else "verified local composition evidence is unavailable or inconsistent",
    ), status


def _raw_publication_report(
    value: _RuntimeManagedPublication | None,
    *,
    task_succeeded: Mapping[str, Any] | None,
    evidence_name: str,
) -> tuple[dict[str, Any], str]:
    if value is None:
        return {
            "status": "not_claimed",
            "evidence": {
                "effect_type": "generation.publish_v1",
                "phase": "vibecomfy.run_settlement",
                "published_scope": "raw_generation",
                "final_composition_publication": "deferred",
            },
        }, "not_claimed"
    if not isinstance(value, _RuntimeManagedPublication):
        if evidence_name == "raw_managed_publication":
            raise ReceiptError(
                "raw_managed_publication must come from attest_runtime_managed_publication"
            )
        raise ReceiptError(
            f"{evidence_name} must come from a Runtime attestation"
        )
    status = value.status
    evidence = dict(value.evidence)
    reason = evidence.get("validation_error") if status == "failed" else None
    publication_task = evidence.get("task")
    if status == "passed" and isinstance(publication_task, Mapping):
        for field in ("run_id", "task_id", "attempt_id"):
            if task_succeeded is None or task_succeeded.get(field) != publication_task.get(field):
                status = "failed"
                reason = f"task_succeeded {field} disagrees with Runtime publication evidence"
                break
    return _stage(status, evidence, reason=reason), status


def _final_publication_report(
    value: _RuntimeManagedPublication | None,
) -> tuple[dict[str, Any], str]:
    if value is None:
        return {
            "status": "not_claimed",
            "evidence": {
                "effect_type": "generation.publish_v1",
                "phase": "h3_av.publication_finalizer",
                "published_scope": "final_composition",
            },
        }, "not_claimed"
    if not isinstance(value, _RuntimeManagedPublication):
        raise ReceiptError("final_managed_publication must come from a Runtime attestation")
    status = value.status
    evidence = dict(value.evidence)
    reason = evidence.get("validation_error") if status == "failed" else None
    if status == "passed" and evidence.get("published_scope") != "final_composition":
        status = "failed"
        reason = "final publication evidence has the wrong publication scope"
    return _stage(status, evidence, reason=reason), status


def build_final_receipt(
    *,
    request_digest: str,
    task_succeeded: Mapping[str, Any] | None,
    candidate_verified: Mapping[str, Any] | None,
    editorially_approved: Mapping[str, Any] | None = None,
    cleanup: Mapping[str, Any] | None = None,
    raw_managed_publication: _RuntimeManagedPublication | None = None,
    final_managed_publication: _RuntimeManagedPublication | None = None,
) -> dict[str, Any]:
    """Build the raw-publication, local-composition, and cleanup receipt."""
    if not isinstance(request_digest, str) or not request_digest.strip():
        raise ReceiptError("request_digest is required")
    task_status = (
        "passed"
        if isinstance(task_succeeded, Mapping)
        and all(
            _nonempty(task_succeeded.get(field))
            for field in ("run_id", "task_id", "attempt_id")
        )
        else "failed"
    )
    publication_report, publication_status = _raw_publication_report(
        raw_managed_publication,
        task_succeeded=task_succeeded,
        evidence_name="raw_managed_publication",
    )
    candidate_report, candidate_status = _candidate_report(
        candidate_verified,
        request_digest=request_digest,
        task_status=task_status,
    )
    final_report, final_status = _final_publication_report(
        final_managed_publication,
    )
    editorial_status = "passed" if editorially_approved is not None else "not_claimed"
    cleanup_target = (
        task_succeeded.get("target")
        if isinstance(task_succeeded, Mapping)
        and isinstance(task_succeeded.get("target"), Mapping)
        else None
    )
    cleanup_report, cleanup_status = _cleanup_report(
        cleanup,
        expected_target=cleanup_target,
    )
    states = {
        "task_succeeded": _stage(
            task_status,
            task_succeeded,
            reason=(
                None
                if task_status == "passed"
                else "task did not settle with run/task/attempt identity"
            ),
        ),
        "raw_managed_publication": publication_report,
        "candidate_verified": candidate_report,
        "final_composition_publication": final_report,
        "editorially_approved": _stage(editorial_status, editorially_approved),
        "cleanup_verified": _stage(
            cleanup_status,
            {"resources": cleanup_report["resources"]},
        ),
    }
    overall = "candidate_verified"
    if task_status != "passed" or candidate_status != "passed":
        overall = "task_failed"
    elif (
        publication_status == "passed"
        and final_status == "passed"
        and cleanup_status == "passed"
        and editorial_status == "passed"
    ):
        overall = "complete"
    return {
        "schema_version": 1,
        "kind": "h3_av_final_receipt",
        "request_digest": request_digest,
        "publication_contract": {
            "published_scope": "final_composition" if final_status == "passed" else "raw_internal_lineage",
            "effect_type": "generation.publish_v1",
            "final_composition_publication": "verified" if final_status == "passed" else "required",
        },
        "states": states,
        "cleanup": cleanup_report,
        "overall_status": overall,
    }


def write_final_receipt(path: str | Path, receipt: Mapping[str, Any]) -> Path:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(dict(receipt), indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return destination


__all__ = [
    "ReceiptError",
    "attest_runtime_managed_publication",
    "attest_runtime_managed_composition",
    "build_final_receipt",
    "write_final_receipt",
]
