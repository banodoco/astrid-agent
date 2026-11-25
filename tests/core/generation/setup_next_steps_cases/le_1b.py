"""LE-1b real asset, version and native adapter cases.

The explicit case list is deliberately independent of the registry. New
canonical codes without a witness still fail test_every_declared_code_has_a_case.
"""
from functools import partial

from astrid.core.generation.asset_gaps import asset_readiness, attached_asset_readiness, declared_model_specs
from astrid.core.generation.engine_contracts import ModelLockValidationError
from astrid.core.generation.preflight import compare_versions, generation_error, readiness_envelope


def _native_case(native, message, *, expected=None):
    result = generation_error({"code": native, "message": message}, entry={"id": "example"}).to_envelope()
    if expected is not None:
        assert result["issues"][0]["code"] == expected
        assert result["issues"][0]["details"]["engine_code"] == native
    return result


def _missing_node():
    return attached_asset_readiness("example", nodes=["CLIPLoader"])


def _missing_model():
    return asset_readiness("example", models=["model.safetensors"])


def _incomplete_model():
    try:
        declared_model_specs([{"name": "model.safetensors", "subdir": "checkpoints"}],
                             entry_id="example")
    except ModelLockValidationError as exc:
        return generation_error(exc, entry={"id": "example"}).to_envelope()
    raise AssertionError("incomplete declaration was admitted")


def _version_warning():
    issues = compare_versions({"comfyui": "1.0"}, {"comfyui": "2.0"})
    assert issues[0]["details"] == {"name": "comfyui", "recorded": "1.0", "observed": "2.0"}
    return readiness_envelope(issues=issues)


def _model_download_warning():
    # LE-8 owns the catalog observation and has its own real producer case
    # on #607. This tests the receiving contract independently of that PR.
    return readiness_envelope(issues=[{
        "code": "model_download_notice", "severity": "warning",
        "message": "A selected pack may download models; volume not declared.",
        "details": {"volume": "not_declared"},
    }])


CASES = [
    ("managed-missing-model", "missing_model", _missing_model),
    ("attached-core-class", "missing_node_pack", _missing_node),
    ("incomplete-model-declaration", "model_lock_incomplete", _incomplete_model),
    ("runtime-version-warning", "version_drift", _version_warning),
    ("model-download-advisory", "model_download_notice", _model_download_warning),
    ("old-engine-unavailable", "target_unreachable", partial(
        _native_case, "COMFY_TARGET_UNAVAILABLE", "Target unavailable", expected="target_unreachable")),
    ("old-engine-not-ready", "engine_not_found", partial(
        _native_case, "COMFY_TARGET_NOT_READY", "Engine not ready", expected="engine_not_found")),
    ("old-engine-invalid", "input_rejected", partial(
        _native_case, "COMFY_TARGET_INVALID", "Target invalid", expected="input_rejected")),
    ("old-engine-unsupported", "missing_dependency", partial(
        _native_case, "COMFY_TARGET_UNSUPPORTED", "Dependency unavailable", expected="missing_dependency")),
    ("native-sentinel", "partial_install", partial(
        _native_case, "incomplete_install", "Incomplete install sentinel", expected="partial_install")),
    ("native-gated-model", "model_license_required", partial(
        _native_case, "license_gated", "Model license not accepted", expected="model_license_required")),
    ("native-model-hash", "integrity_failed", partial(
        _native_case, "hash_mismatch", "Model hash mismatch", expected="integrity_failed")),
    ("native-target-disk", "insufficient_storage", partial(
        _native_case, "insufficient_space", "Insufficient target disk", expected="insufficient_storage")),
]

# Remaining pre-existing public native states, each exercising the actual
# exception/result adapter rather than constructing a synthetic fix.
CASES += [
    (code, code, partial(_native_case, code, message))
    for code, message in (
        ("target_schema_unavailable", "Attached target schema unavailable"),
        ("invalid_model_lock", "Invalid model declaration"),
        ("generation_failed", "Generation failed"),
        ("download_failed", "Model download failed"),
        ("conflict", "A service write conflicted"),
        ("entry_missing", "Saved entry removed"),
        ("GENERATION_KEY_MISSING", "A required provider key is missing"),
        ("placement_invalid", "Invalid placement selector"),
        ("generation_route_unqualified", "Route not qualified"),
        ("unsupported_output_cardinality", "Multiple unsupported outputs"),
        ("output_delivery_failed", "Output delivery failed after completion"),
        ("tls_verification_failed", "Certificate verification failed"),
        ("asset_alignment_failed", "Alignment failed"),
        ("not_found", "Resource not found"),
        ("stale_version", "Resource version is stale"),
        ("idempotency_mismatch", "Idempotency identity changed"),
        ("terminal_state", "Operation is already terminal"),
        ("validation_error", "Service request invalid"),
        ("integrity_error", "Service integrity refusal"),
        ("unavailable", "Service unavailable"),
        ("internal_error", "Service internal error"),
        ("deadline_exceeded", "Operation timed out"),
        ("transport_failed", "Transport failed"),
        ("command_failed", "Native command failed"),
        ("comfy_execution_error", "ComfyUI execution failed"),
        ("host_restarted_during_job", "Host restarted"),
        ("executor_failed", "Executor failed"),
        ("engine_busy", "Engine busy"),
        ("custody_unresolved", "Custody unresolved"),
        ("endpoint_ownership", "Endpoint ownership unproved"),
        ("endpoint_unhealthy", "Endpoint unhealthy"),
        ("unsupported_placement", "Unsupported placement"),
        ("unknown_engine", "Engine unknown"),
        ("unsupported_platform", "Platform unsupported"),
        ("invalid_recipe", "Recipe invalid"),
        ("unsafe_path", "Unsafe destination"),
        ("attestation_invalid", "Attestation invalid"),
        ("placement_refused", "Placement refused"),
    )
]
