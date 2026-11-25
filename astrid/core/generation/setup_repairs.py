"""Construct explicit, named repairs for the single closed setup contract.

Producers choose their own issue links. The envelope validator rejects
unlinked issues or malformed fixes; no compatibility projection supplies them.
No proposed action grants installation authority.
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from .setup_contract import repair_step


def project_target_record(target: Any) -> Any:
    """One projection for target records at the readiness/repair boundary."""
    return target.as_dict() if hasattr(target, "as_dict") else target


def _target_probe(target: Mapping[str, Any] | None) -> tuple[list[str], dict[str, str]]:
    target = project_target_record(target)
    target = target if isinstance(target, Mapping) else {}
    selector = target.get("endpoint") or target.get("placement") or target.get("kind")
    if (not isinstance(selector, str)
            or (selector not in {"auto", "managed", "local", "runpod"}
                and not selector.startswith(("http://", "https://")))):
        selector = "<PLACEMENT>"
    placeholders = {"PLACEMENT": "The originally selected target or its explicit ComfyUI URL"} if selector == "<PLACEMENT>" else {}
    return ["astrid", "generate", "targets", "--placement", selector, "--json"], placeholders


def _inspect(entry, target, *, attached=False, code=None):
    if attached or code in {"target_unreachable", "engine_not_found", "placement_invalid",
                            "unsupported_placement", "endpoint_ownership", "endpoint_unhealthy"}:
        return _target_probe(target)
    if code == "entry_missing":
        return ["astrid", "generate", "list", "--json"], {}
    if isinstance(entry, Mapping) and isinstance(entry.get("id"), str) and entry["id"]:
        identifier = entry["id"]
        if re.search(r"<[^<>]+>", identifier):
            return ["astrid", "generate", "describe", "--entry", "<ENTRY>", "--json"], {
                "ENTRY": "The originally selected generation entry",
            }
        return ["astrid", "generate", "describe", "--entry", identifier, "--json"], {}
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


def native_setup_fix(issue, *, entry=None, target=None, task_id=None, fix_id="inspect-and-repair"):
    """Use the named code's documented repair, not an invented write command."""
    code = issue["code"]
    step = repair_step(code)
    if step is None:
        raise ValueError(f"setup issue {code!r} needs an explicit blocking fix")
    fix = manual_setup_fix(fix_id, step, entry=entry, target=target, code=code)
    if isinstance(task_id, str) and task_id and not re.search(r"<[^<>]+>", task_id):
        if code in {"output_delivery_failed", "generation_failed", "comfy_execution_error",
                    "executor_failed", "host_restarted_during_job", "deadline_exceeded"}:
            fix["instruction"]["then_argv"] = ["astrid", "tasks", "logs", task_id]
            fix["placeholders"] = {}
    return fix


def declare_redacted_action_values(envelope):
    """A redacted command argument is a human placeholder, never runnable.

    The ordinary error redactor has already removed the credential. Keep that
    removal visible and retain validation of every placeholder afterwards.
    """
    for fix in envelope["fixes"]:
        actions = fix.get("instruction")
        changed = False
        if actions is not None:
            if "<redacted>" in actions["step"]:
                actions["step"] = actions["step"].replace("<redacted>", "<REDACTED_VALUE>")
                changed = True
            argv = actions["then_argv"]
        else:
            argv = fix.get("argv", [])
        for index, item in enumerate(argv):
            if "<redacted>" in item:
                argv[index] = item.replace("<redacted>", "<REDACTED_VALUE>")
                changed = True
        if changed:
            fix["who"] = "human"
            fix.setdefault("placeholders", {})["REDACTED_VALUE"] = (
                "Supply the required secret privately through its supported credential mechanism; "
                "do not reveal it in the conversation"
            )
    return envelope
