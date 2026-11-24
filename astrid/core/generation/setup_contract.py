"""P56's single setup issue vocabulary and strict envelope validator.

Fix list order is execution order; each error's fix_ids is an ordered subset.
Warnings have no repair links. Native spellings normalize before validation.
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

# This is the one public issue vocabulary. Values are explicit human repairs
# for a native failure without a more specific producer-owned fix. Warning-only
# codes have no default repair. Native aliases below are input spellings, not
# additional public codes.
_ISSUE_REPAIR_STEPS = {
    "needs_consent": "Review the exact repository, commit, recipe and target with the person. Obtain the specific approval token before attempting installation.",
    "entry_exists": "Choose whether to retain the existing entry or explicitly replace it; do not remove the existing entry to retry.",
    "pin_conflict": "Choose one consistent source and immutable commit for the conflicting declaration.",
    "pack_version_differs": None,
    "unknown_input": "Review the changed input against the selected node schema; correct the saved workflow before retrying.",
    "source_unreachable": "Check that the exact HTTPS repository is reachable and its source is correct.",
    "commit_unavailable": "Choose an available full commit or exact branch or tag for the declared repository.",
    "pip_failed": "Review the failing dependency and declare a compatible recipe. Do not run a pack install.py.",
    "protected_change": "Choose a recipe compatible with the protected runtime packages; do not change those packages to force the install.",
    "node_pack_conflict": "Resolve the conflicting pack source, pin or checkout with the person before making changes.",
    "partial_install": "Inspect the incomplete installation and its owner; let active work finish or repair the stale installation through the managed setup path. Do not delete an active sentinel.",
    "classes_not_loaded": "Restart the selected ComfyUI after the approved installation and verify its loaded classes.",
    "fetch_404": "Check the model source and choose a verified available URL, SHA-256 and exact byte size.",
    "missing_node_pack": "Identify the exact providing pack or select a target that provides the missing node before installing anything.",
    "missing_model": "Obtain the missing model at its verified destination; do not install on an unrelated target.",
    "model_download_notice": None,
    "model_lock_incomplete": "Complete the saved model declaration with the verified HTTPS source, SHA-256 and exact byte size.",
    "invalid_model_lock": "Correct the invalid model declaration and verify its source, fingerprint and destination before installation.",
    "target_unreachable": "Start or repair the selected target and verify its endpoint before retrying.",
    "input_rejected": "Correct the rejected input or option using the selected entry's declared inputs and retry.",
    "missing_dependency": "Install or repair the required dependency through the supported setup path, or select a target that already provides it.",
    "engine_not_found": "Install or repair the selected managed ComfyUI engine, or explicitly select an existing ComfyUI.",
    "generation_failed": "Inspect the reported failure and correct its cause before retrying the same request.",
    "integrity_failed": "Keep the conflicting file or installation untouched; verify its identity and restore the expected locked bytes before retrying.",
    "insufficient_storage": "Free space on the selected target or select a larger volume; verify free space before retrying.",
    "download_failed": "Check access to the verified source and target folder; repair the transfer before confirming a new download.",
    "model_license_required": "Review and accept the model license at its source if appropriate, then authenticate through the supported credential mechanism.",
    "conflict": "Inspect the conflicting service state and resolve it with the person before retrying the write.",
    "entry_missing": "Choose an existing saved entry, or recreate the removed entry from its original workflow before retrying.",
    "GENERATION_KEY_MISSING": "Configure the required provider credential through its supported secret mechanism; never put it in a workflow or command.",
    "placement_invalid": "Correct the placement selector and verify the intended target.",
    "generation_route_unqualified": "Select a qualified generation route or wait for this route to be qualified.",
    "unsupported_output_cardinality": "Choose a workflow with a supported output count before requesting generation.",
    "output_delivery_failed": "Generation already completed. Inspect the admitted task and recover its existing output; do not rerun generation just to deliver it.",
    "tls_verification_failed": "Repair the selected endpoint's certificate trust; do not disable TLS verification.",
    "asset_alignment_failed": "Inspect the selected managed entry's locked assets and repair the reported alignment failure.",
    "not_found": "Verify the requested resource still exists and select its current identity before retrying.",
    "stale_version": "Read the current resource revision and resolve the stale selection before retrying.",
    "idempotency_mismatch": "Inspect the original admitted request; resolve the mismatched idempotency identity before retrying.",
    "terminal_state": "Inspect the existing terminal operation; do not repeat a completed or cancelled mutation.",
    "validation_error": "Correct the invalid service request using its reported constraints before retrying.",
    "integrity_error": "Resolve the reported service integrity failure before retrying.",
    "unavailable": "Check the selected service's availability before retrying.",
    "internal_error": "Inspect the reported service failure before retrying.",
    "deadline_exceeded": "Inspect the timed-out operation and its custody before retrying; do not assume it stopped.",
    "transport_failed": "Verify the selected transport and existing operation state before retrying.",
    "command_failed": "Inspect the failing command's diagnostic and correct its cause before retrying.",
    "comfy_execution_error": "Inspect the reported ComfyUI execution failure and repair the workflow or selected runtime.",
    "host_restarted_during_job": "Inspect the existing task after the host restart before attempting another admission.",
    "executor_failed": "Inspect the existing task and its executor failure before retrying.",
    "engine_busy": "Wait for the selected engine's existing operation to finish or inspect its custody.",
    "custody_unresolved": "Resolve the existing engine or task custody before starting another operation.",
    "endpoint_ownership": "Verify endpoint ownership; do not adopt or restart an unrelated service.",
    "endpoint_unhealthy": "Repair or start the selected engine and verify its endpoint.",
    "unsupported_placement": "Select a supported target without silently switching placements.",
    "unknown_engine": "Select a known engine and verify its installation.",
    "unsupported_platform": "Choose a supported platform for the selected engine.",
    "invalid_recipe": "Correct the invalid engine recipe before installing.",
    "unsafe_path": "Correct the unsafe destination; do not follow or overwrite an unverified path.",
    "attestation_invalid": "Verify the selected installation's attestation before retrying.",
    "placement_refused": "Inspect the placement gate and resolve the reported refusal before retrying.",
}
SETUP_ISSUE_CODES = frozenset(_ISSUE_REPAIR_STEPS)
_NATIVE_ALIASES = {
    "COMFY_TARGET_UNAVAILABLE": "target_unreachable",
    "COMFY_TARGET_INVALID": "input_rejected",
    "COMFY_TARGET_UNSUPPORTED": "missing_dependency",
    "COMFY_TARGET_NOT_READY": "engine_not_found",
    "COMFY_TARGET_NO_MODEL": "missing_dependency",
    "engine_not_installed": "engine_not_found",
    "comfyui.missing_node": "missing_node_pack",
    "comfyui.models_missing": "missing_model",
    "COMFY_MODEL_MISSING": "missing_model",
    "version_drift": "pack_version_differs",
    "insufficient_space": "insufficient_storage",
    "present_file_mismatch": "integrity_failed",
    "license_gated": "model_license_required",
    "gated": "model_license_required",
    "hash_mismatch": "integrity_failed",
    "incomplete_install": "partial_install",
}


def canonical_issue_code(code: str) -> str:
    """Translate only established native spellings; reject undeclared issues later."""
    return _NATIVE_ALIASES.get(code, code)


def native_issue_code(code: str) -> str:
    """An unhandled native exception retains its code as evidence, not a public synonym."""
    normalized = canonical_issue_code(code)
    return normalized if normalized in SETUP_ISSUE_CODES else "generation_failed"


def repair_step(code: str) -> str | None:
    return _ISSUE_REPAIR_STEPS.get(code)

_SCOPES = frozenset({"read_only", "managed_write", "user_env", "paid", "attached_target"})
_FIX_FIELDS = frozenset({"id", "who", "scope", "details", "argv", "instruction", "placeholders"})
_PLACEHOLDER = re.compile(r"<([^<>]+)>")
_NAME = re.compile(r"[A-Z][A-Z0-9_]*")


def _words(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip()) and "\0" not in value


def _argv(value: Any) -> list[str]:
    if not isinstance(value, list) or not value or not all(_words(item) for item in value):
        raise ValueError("setup fix argv must be a nonempty array of nonempty strings")
    if any(item == "--dry-run" or item.startswith("--dry-run=") for item in value):
        raise ValueError("a setup fix must not contain --dry-run")
    return value


def validate_setup_envelope(envelope: Mapping[str, Any]) -> None:
    """Reject incomplete/ambiguous setup guidance without inventing a repair."""
    attempt = envelope.get("attempt_count")
    if type(attempt) is not int or attempt < 0:
        raise ValueError("setup attempt_count must be a nonnegative integer")
    issues, fixes = envelope.get("issues"), envelope.get("fixes")
    if not isinstance(issues, list) or not isinstance(fixes, list):
        raise ValueError("setup issues and fixes must be arrays")
    order = {}
    for index, fix in enumerate(fixes):
        if not isinstance(fix, dict) or set(fix) - _FIX_FIELDS:
            raise ValueError("setup fix has unrecognized fields")
        fix_id = fix.get("id")
        if not _words(fix_id) or fix_id in order:
            raise ValueError("setup fixes need unique nonempty ids")
        order[fix_id] = index
        if (fix.get("who") not in ("agent", "human") or not isinstance(fix.get("scope"), str)
                or fix["scope"] not in _SCOPES):
            raise ValueError("setup fix must name who acts and its scope")
        if not isinstance(fix.get("details", {}), dict):
            raise ValueError("setup fix details must be an object")
        if ("argv" in fix) == ("instruction" in fix):
            raise ValueError("setup fix needs exactly one of argv or instruction")
        texts = []
        if "argv" in fix:
            texts.extend(_argv(fix["argv"]))
        else:
            instruction = fix["instruction"]
            if (not isinstance(instruction, dict) or set(instruction) != {"step", "then_argv"}
                    or not _words(instruction["step"])):
                raise ValueError("setup instruction needs step and then_argv")
            texts.append(instruction["step"])
            texts.extend(_argv(instruction["then_argv"]))
        placeholders = fix.get("placeholders", {})
        if (not isinstance(placeholders, dict)
                or not all(isinstance(key, str) and _NAME.fullmatch(key) and _words(value)
                           for key, value in placeholders.items())):
            raise ValueError("setup placeholders must map uppercase names to explanations")
        used = {name for text in texts for name in _PLACEHOLDER.findall(text)}
        if used != set(placeholders):
            raise ValueError("setup fix must declare every used placeholder and no unused placeholders")
    linked = set()
    for issue in issues:
        if (not isinstance(issue, dict) or not isinstance(issue.get("code"), str)
                or issue["code"] not in SETUP_ISSUE_CODES):
            raise ValueError("undeclared setup issue code")
        if issue.get("severity") not in ("error", "warning") or not _words(issue.get("message")):
            raise ValueError("setup issue needs severity and message")
        if not isinstance(issue.get("details", {}), dict):
            raise ValueError("setup issue details must be an object")
        ids = issue.get("fix_ids", [])
        if (not isinstance(ids, list) or not all(isinstance(item, str) and item in order for item in ids)
                or len(ids) != len(set(ids)) or ids != sorted(ids, key=order.__getitem__)):
            raise ValueError("setup issue fix_ids must reference fixes once in their declared order")
        if issue["severity"] == "error" and not ids:
            raise ValueError("every setup error needs a linked next step")
        if issue["severity"] == "warning" and ids:
            raise ValueError("a setup warning must not invent a repair")
        linked.update(ids)
    if linked != set(order):
        raise ValueError("setup fixes must be linked to an error")
    expected = "blocked" if any(issue["severity"] == "error" for issue in issues) else "ready_with_warnings" if issues else "ready"
    if envelope.get("status") != expected:
        raise ValueError("setup status must match its issues")
