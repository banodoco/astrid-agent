"""Map producer repair facts into the single closed setup contract.

This is a projection, never a validation exemption. Older callers still
supply scope/action/argv facts while their owning functions land. Explicit
links and modern fixes are not guessed or repaired; the validator must
reject malformed ones. No proposed action grants installation authority.
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from .setup_contract import canonical_issue_code, repair_step


def _target_probe(target: Mapping[str, Any] | None) -> tuple[list[str], dict[str, str]]:
    target = target or {}
    selector = target.get("endpoint") or target.get("placement") or target.get("kind")
    if not isinstance(selector, str) or selector not in {"auto", "managed", "local", "runpod"} and not selector.startswith(("http://", "https://")):
        selector = "<PLACEMENT>"
    placeholders = {"PLACEMENT": "The originally selected target or its explicit ComfyUI URL"} if selector == "<PLACEMENT>" else {}
    return ["astrid", "generate", "targets", "--placement", selector, "--json"], placeholders


def _inspect(entry, target, *, attached=False, code=None):
    if attached or code in {"target_unreachable", "engine_not_found", "placement_invalid",
                            "unsupported_placement", "endpoint_ownership", "endpoint_unhealthy"}:
        return _target_probe(target)
    if isinstance(entry, Mapping) and isinstance(entry.get("id"), str) and entry["id"]:
        return ["astrid", "generate", "describe", "--entry", entry["id"], "--json"], {}
    if code == "entry_missing":
        return ["astrid", "generate", "list", "--json"], {}
    return ["astrid", "generate", "list", "--json"], {}


def _literal_step(step: str) -> tuple[str, dict[str, str]]:
    # A legacy manual instruction used this human-readable folder placeholder.
    # Keep the original action/commands in details, but make the contract's
    # instruction's placeholder explicit. Other angle-bracket text is literal.
    folder = "<your ComfyUI folder>"
    placeholders = {}
    if folder in step:
        step = step.replace(folder, "<COMFYUI_FOLDER>")
        placeholders["COMFYUI_FOLDER"] = "The person's actual ComfyUI installation folder"
    step = re.sub(r"<([^<>]+)>",
                  lambda match: match.group(0) if match.group(1) in placeholders
                  else "«" + match.group(1) + "»", step)
    return step, placeholders


def manual_setup_fix(fix_id: str, step: str, *, entry=None, target=None,
                     scope="read_only", details=None, code=None) -> dict[str, Any]:
    argv, placeholders = _inspect(entry, target, attached=scope == "attached_target", code=code)
    step, step_placeholders = _literal_step(step)
    placeholders.update(step_placeholders)
    return {"id": fix_id, "who": "human", "scope": scope, "details": details or {},
            "instruction": {"step": step, "then_argv": argv},
            "placeholders": placeholders}


def native_setup_fix(issue, *, entry=None, target=None, fix_id="inspect-and-repair"):
    """Use the named code's documented repair, not an invented write command."""
    code = issue["code"]
    step = repair_step(code)
    if step is None:
        raise ValueError(f"setup issue {code!r} needs an explicit blocking fix")
    details = issue.get("details", {})
    # A producer's explicit human guidance is useful, but never interpreted as
    # a command or a grant of consent.
    guidance = details.get("fix")
    if isinstance(guidance, str) and guidance.strip():
        step = guidance
    return manual_setup_fix(fix_id, step, entry=entry, target=target, code=code)


def _project_legacy_fix(fix, fix_id, *, entry, target):
    """Convert only the previous closed scope/details/argv shape."""
    if set(fix) - {"scope", "details", "argv"} or "scope" not in fix:
        raise ValueError("setup fix must use the declared contract")
    details, scope = fix.get("details", {}), fix["scope"]
    if not isinstance(details, dict):
        raise ValueError("setup fix details must be an object")
    if "argv" in fix and scope != "attached_target":
        # Preserve old concrete commands and scope. A user-environment or
        # paid action is a person's decision, never an automatic recovery.
        argv = fix["argv"]
        if not isinstance(argv, list):
            raise ValueError("setup fix argv must be an array")
        return {"id": fix_id, "who": "agent" if scope == "read_only" else "human",
                "scope": scope, "details": details, "argv": argv}
    action = details.get("action") or details.get("guidance")
    if not isinstance(action, str) or not action.strip():
        raise ValueError("legacy manual setup fix requires explicit guidance")
    return manual_setup_fix(fix_id, action, entry=entry, target=target,
                            scope=scope, details=details)


def project_setup_facts(issues, fixes, *, entry=None, target=None):
    """Normalize old producer facts, then let the one validator check all links.

    Already linked issues and declared modern fixes are kept verbatim apart
    from native code aliases. The remaining old fixes may serve one issue,
    a shared repair, or a one-to-one ordered set. Ambiguous relationships are
    refused rather than guessed. Modern producers must supply explicit links.
    """
    for issue in issues:
        native = issue.get("code")
        if isinstance(native, str):
            normalized = canonical_issue_code(native)
            if normalized != native:
                issue["code"] = normalized
                if native != "version_drift":
                    issue.setdefault("details", {}).setdefault("engine_code", native)
    used = {fix.get("id") for fix in fixes if isinstance(fix.get("id"), str)}

    def fresh_id(index):
        key = f"setup-{index}"
        while key in used:
            key += "-next"
        used.add(key)
        return key

    legacy_ids = []
    for index, fix in enumerate(fixes):
        if not set(fix) & {"id", "who", "instruction", "placeholders"}:
            fix_id = fresh_id(index + 1)
            fixes[index] = _project_legacy_fix(fix, fix_id, entry=entry, target=target)
            legacy_ids.append(fix_id)

    unlinked = [issue for issue in issues if issue.get("severity") == "error" and "fix_ids" not in issue]
    if legacy_ids:
        if len(unlinked) == 1 or len(legacy_ids) == 1:
            for issue in unlinked:
                issue["fix_ids"] = list(legacy_ids)
        elif len(unlinked) == len(legacy_ids):
            for issue, fix_id in zip(unlinked, legacy_ids):
                issue["fix_ids"] = [fix_id]
        else:
            raise ValueError("legacy setup fixes have ambiguous issue ownership")
    elif unlinked:
        # Only an old issue with no supplied modern fixes may use its named
        # native repair. An explicit but dangling link must fail validation.
        if fixes:
            raise ValueError("setup issues must link to their declared fixes")
        for issue in unlinked:
            fix_id = fresh_id(len(fixes) + 1)
            fixes.append(native_setup_fix(issue, entry=entry, target=target, fix_id=fix_id))
            issue["fix_ids"] = [fix_id]
    return issues, fixes
