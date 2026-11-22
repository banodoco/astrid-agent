"""Validation helpers for immutable admitted task requests."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Mapping

from astrid.core.execution.host_errors import HostError
from astrid.sdk.execution_request import normalize_execution_request


def _canonical_digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()

def _task_selector(task_data: Mapping[str, Any], *, admitted_task_spec=None) -> str | None:
    """The renderer selector the admitted task names, or None when it omits one."""
    spec = (_admitted_task_spec if admitted_task_spec is None else admitted_task_spec)(task_data)
    for section in ("inputs", "params"):
        values = spec.get(section)
        if isinstance(values, Mapping) and values.get("selector"):
            return str(values["selector"])
    return None

def _admitted_spec_envelope(raw_spec: Any) -> Mapping[str, Any]:
    """Return the immutable capability request from a runtime task envelope.

    Runtime task snapshots have one stable wrapper around the immutable
    capability request::

        task.spec         = admission envelope
        task.spec.spec    = capability request
        task.spec.spec.inputs

    A few older in-process callers provide the capability request directly.
    Accept that compatibility shape, but never recursively unwrap arbitrary
    mappings or silently accept conflicting duplicate fields.
    """
    if not isinstance(raw_spec, Mapping):
        raise HostError("runtime task is missing its immutable spec envelope")
    nested = raw_spec.get("spec")
    if nested is None:
        return raw_spec
    if not isinstance(nested, Mapping):
        raise HostError("runtime task immutable spec envelope has a non-object spec")
    for key in (
        "capability_id",
        "kind",
        "inputs",
        "params",
        "outputs",
        "runtime_dependencies",
        "authority_context",
    ):
        if key in raw_spec and key in nested and raw_spec[key] != nested[key]:
            raise HostError(f"runtime task spec has conflicting {key!r} values")
    return nested

def _admitted_task_spec(task_data: Mapping[str, Any], *, admitted_spec_envelope=None) -> Mapping[str, Any]:
    """Decode the canonical immutable request from a task snapshot."""
    return (_admitted_spec_envelope if admitted_spec_envelope is None else admitted_spec_envelope)(task_data.get("spec"))

def _assert_verified_placement_binding(
    binding: Mapping[str, Any], target: Mapping[str, Any]
) -> None:
    """Require Runtime's exact credential-backed placement projection.

    The host consumes this evidence; it does not mint or upgrade selector,
    config, environment, or self-reported target data into evidence.
    """

    actual = binding.get("actual_target")
    if not isinstance(actual, Mapping):
        raise HostError("task target binding has no credential-verified actual target")
    verification = binding.get("verification")
    if not isinstance(verification, Mapping) or set(verification) != {
        "method", "evidence_digest", "verified",
    }:
        raise HostError("task target binding has no credential-verified placement verification")
    if verification.get("method") != "credential_claim" or verification.get("verified") is not True:
        raise HostError("task target binding placement verification is not a credential claim")
    digest = verification.get("evidence_digest")
    if not isinstance(digest, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise HostError("task target binding placement verification digest is invalid")
    incarnation = binding.get("executor_incarnation")
    if not isinstance(incarnation, str) or not incarnation.strip() or len(incarnation) > 256:
        raise HostError("task target binding has no valid executor incarnation")

    selected_kind = target.get("kind")
    actual_kind = actual.get("kind")
    resolved = binding.get("resolved_target")
    if not isinstance(resolved, Mapping) or resolved != target:
        raise HostError("credential-verified resolved target disagrees with execution_request")
    if selected_kind != "default" and actual_kind != selected_kind:
        raise HostError("credential-verified actual target kind disagrees with execution_request")
    for key, expected in target.items():
        if key != "kind" and actual.get(key) != expected:
            raise HostError(f"credential-verified actual target {key} disagrees with execution_request")

def _execution_contract(
    task_data: Mapping[str, Any],
    *,
    runtime_session_id: str | None = None,
    allow_runtime_empty_inputs: bool = False,
    admitted_spec_envelope=None,
    assert_verified_placement_binding=None,
    canonical_digest=None,
    normalize_request=None,
) -> dict[str, Any] | None:
    """Validate the carried request before the worker spends or opens a session."""
    admitted_spec_envelope = _admitted_spec_envelope if admitted_spec_envelope is None else admitted_spec_envelope
    assert_verified_placement_binding = _assert_verified_placement_binding if assert_verified_placement_binding is None else assert_verified_placement_binding
    canonical_digest = _canonical_digest if canonical_digest is None else canonical_digest
    normalize_request = normalize_execution_request if normalize_request is None else normalize_request
    envelope = task_data.get("spec")
    # A small set of legacy in-process callers intentionally provide only the
    # task identity while exercising capability-level admission guards.  With
    # no carried contract there is nothing to validate here; preserve that
    # compatibility path and let the ordinary capability checks continue.
    if envelope is None:
        if task_data.get("execution_request") is not None:
            raise HostError("execution_request requires its immutable spec envelope")
        return None
    if not isinstance(envelope, Mapping):
        raise HostError("runtime task is missing its immutable spec envelope")
    request = task_data.get("execution_request")
    nested = admitted_spec_envelope(envelope)
    for carried in (envelope.get("execution_request"), nested.get("execution_request")):
        if request is None:
            request = carried
        elif carried is not None and canonical_digest(request) != canonical_digest(carried):
            raise HostError("runtime task has conflicting execution_request values")
    if request is None:
        return None
    if not envelope or ("inputs" in nested and not isinstance(nested["inputs"], Mapping)):
        raise HostError("execution_request requires a usable immutable spec envelope")
    try:
        normalized = normalize_request(request)
    except ValueError as exc:
        raise HostError(f"invalid execution_request: {exc}") from exc
    if normalized is None or not isinstance(request, Mapping):
        raise HostError("invalid execution_request")
    carried_request = dict(request)
    # Exact delegated claims receive Runtime's canonical empty input manifest.
    # The SDK omits that empty member; permit this one representation only on
    # the authenticated exact handoff, preserving ordinary claim validation.
    if allow_runtime_empty_inputs and type(carried_request.get("inputs")) is list and carried_request["inputs"] == []:
        carried_request.pop("inputs")
    if canonical_digest(carried_request) != canonical_digest(normalized):
        raise HostError("runtime task execution_request is not normalized")

    declared = normalized.get("inputs", [])
    authorized = task_data.get("input_object_ids")
    if not isinstance(authorized, (list, tuple)):
        raise HostError("execution_request requires task input_object_ids")
    def object_id(value: Any) -> str:
        if not isinstance(value, str):
            raise HostError("execution_request input object IDs must be strings")
        return value.removeprefix("sha256:")
    expected_ids = [object_id(item["object_id"]) for item in declared]
    actual_ids = [object_id(item) for item in authorized]
    if len(actual_ids) != len(expected_ids) or set(actual_ids) != set(expected_ids):
        raise HostError("task input_object_ids do not match execution_request inputs")
    inputs = nested.get("inputs")
    if not isinstance(inputs, Mapping):
        inputs = {}
    declared_names = {item["name"] for item in declared}
    for name, descriptor in inputs.items():
        if isinstance(descriptor, Mapping) and (
            "digest" in descriptor or "object_id" in descriptor
        ) and name not in declared_names:
            raise HostError(f"task spec managed input {name!r} is absent from execution_request")
    for item in declared:
        name = item["name"]
        descriptor = inputs.get(name)
        if not isinstance(descriptor, Mapping):
            raise HostError(f"task spec input {name!r} is missing its execution_request descriptor")
        # ``object_id`` is the canonical managed-object digest.  ``digest``
        # is an optional repeated witness for callers that want the wire
        # envelope to spell it out, so its absence must not make an otherwise
        # valid frozen input unrunnable.
        digest = descriptor.get("digest") or descriptor.get("object_id")
        if digest is None:
            raise HostError(f"task spec input {name!r} is missing its materialization digest")
        if object_id(digest) != object_id(item["object_id"]):
            raise HostError(f"task spec input {name!r} disagrees with execution_request object_id")
        if descriptor.get("object_id") is not None and object_id(descriptor["object_id"]) != object_id(item["object_id"]):
            raise HostError(f"task spec input {name!r} disagrees with execution_request object_id")
        if descriptor.get("filename") != item["filename"]:
            raise HostError(f"task spec input {name!r} disagrees with execution_request filename")

    workflow = normalized.get("workflow")
    if isinstance(workflow, Mapping):
        workflow_spec = nested.get("workflow")
        workflow_id = nested.get("workflow_id")
        if workflow_id is None and isinstance(workflow_spec, Mapping):
            workflow_id = workflow_spec.get("id")
        if workflow_id is None:
            workflow_id = task_data.get("capability")
        if workflow_id != workflow["id"]:
            raise HostError("task spec workflow id disagrees with execution_request")
        identity = nested.get("workflow_contract_digest")
        if identity is None and isinstance(workflow_spec, Mapping):
            identity = workflow_spec.get("contract_digest")
        if identity != workflow["contract_digest"]:
            raise HostError("task spec workflow contract digest disagrees with execution_request")

    target = normalized["target"]
    binding = task_data.get("execution_binding") or task_data.get("placement_binding") or task_data.get("binding")
    if not isinstance(binding, Mapping):
        raise HostError("execution_request target requires an observed task binding")
    if binding.get("status") not in (None, "claimed"):
        raise HostError("task target binding is not claimed")
    required_identity = {
        "task_id": task_data.get("id") or task_data.get("task_id"),
        "run_id": task_data.get("run_id"),
        "attempt_id": task_data.get("attempt_id"),
        "lease_id": task_data.get("lease_id"),
        "fence": task_data.get("fence"),
        "executor_id": task_data.get("executor_id"),
        "runtime_epoch": task_data.get("runtime_epoch"),
        "capability_id": task_data.get("capability") or task_data.get("capability_id"),
    }
    for field, expected in required_identity.items():
        if expected is not None and binding.get(field) != expected:
            raise HostError(f"task target binding {field} disagrees with the claimed identity")
    if not isinstance(binding.get("binding_id"), str) or not binding["binding_id"]:
        raise HostError("task target binding has no Runtime-issued binding_id")
    if not isinstance(binding.get("session_id"), str) or not binding["session_id"]:
        raise HostError("task target binding has no Runtime session identity")
    expected_session = task_data.get("runtime_session_id") or runtime_session_id
    if expected_session is not None and binding["session_id"] != expected_session:
        raise HostError("task target binding session_id disagrees with the Runtime session")
    resolved_target = binding.get("resolved_target")
    if not isinstance(resolved_target, Mapping):
        raise HostError("task target binding has no resolved target identity")
    kind = target["kind"]
    if binding.get("target_kind") not in (None, kind) and binding.get("kind") not in (None, kind):
        raise HostError("task target binding kind disagrees with execution_request")
    identity_fields = {
        "default": (),
        "profile": (("id", "target_id"), ("profile_alias", "target_id")),
        "machine": (("id", "target_id"), ("machine_id", "target_id")),
        "runpod": (("pod_id", "pod_id"), ("provider_account_ref", "provider_account_ref")),
    }[kind]
    for requested, observed in identity_fields:
        if requested in target and target[requested] is not None and binding.get(observed) != target[requested]:
            raise HostError(f"task target binding {observed} disagrees with execution_request")
    for key in ("profile_revision", "profile_digest", "release_digest"):
        if key in target and binding.get(key) != target[key]:
            raise HostError(f"task target binding {key} disagrees with execution_request")
    for key in ("storage", "mounts"):
        if key in target and resolved_target.get(key) != target[key]:
            raise HostError(f"task target binding {key} disagrees with execution_request")
    assert_verified_placement_binding(binding, target)
    return normalized
