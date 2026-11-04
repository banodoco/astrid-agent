"""Pure definition checks for the future Windows R0/R1 task adapter.

No scheduler calls or mutation authority live here. A matching definition does
not prove that a task is stopped, absent, or safe to mutate by name. The caller
must independently admit and retain the expected manager export, bind it to
the selected installation/journal, and solve task replacement before mutation.
The Windows boundary remains refused until those pieces are implemented.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import PureWindowsPath

_NS = "{http://schemas.microsoft.com/windows/2004/02/mit/task}"
_MAX_XML = 128 * 1024
_TASK_NAMES = {"Astrid Runtime", "Astrid Runtime R1"}

# Deliberately a subset, not a general Task Scheduler schema validator. Each
# permitted child is singular, including each of the two supported triggers.
# Unknown fields/attributes are refused even in the expected snapshot.
_CHILDREN = {
    "Task": "RegistrationInfo Principals Settings Triggers Actions",
    "RegistrationInfo": "Description URI Source Date Author Version Documentation",
    "Principals": "Principal",
    "Principal": "UserId LogonType RunLevel",
    "Settings": (
        "MultipleInstancesPolicy DisallowStartIfOnBatteries StopIfGoingOnBatteries "
        "AllowHardTerminate StartWhenAvailable RunOnlyIfNetworkAvailable IdleSettings "
        "AllowStartOnDemand Enabled RunOnlyIfIdle WakeToRun ExecutionTimeLimit "
        "RestartOnFailure UseUnifiedSchedulingEngine Hidden Priority"
    ),
    "IdleSettings": "Duration WaitTimeout StopOnIdleEnd RestartOnIdle",
    "RestartOnFailure": "Interval Count",
    "Triggers": "LogonTrigger TimeTrigger",
    "LogonTrigger": "Enabled StartBoundary EndBoundary Repetition ExecutionTimeLimit UserId Delay",
    "TimeTrigger": "Enabled StartBoundary EndBoundary Repetition ExecutionTimeLimit RandomDelay",
    "Repetition": "Interval Duration StopAtDurationEnd",
    "Actions": "Exec",
    "Exec": "Command Arguments WorkingDirectory",
}
_ATTRIBUTES = {"Task": {"version"}, "Principal": {"id"}, "Actions": {"Context"}}


class TaskIdentityError(ValueError):
    """Unknown or changed task definition; retain evidence and refuse mutation."""


@dataclass(frozen=True)
class TaskIdentity:
    """Independently trusted installation bindings and frozen manager XML.

    ``definition_xml`` is an admitted exported definition, not an unverified
    live task copied into an expectation or the pre-registration local XML.
    R1's journal publication token must be part of that frozen definition.
    """

    name: str
    sid: str
    command: str
    arguments: str
    working_directory: str
    definition_xml: str


class _TreeBuilder(ET.TreeBuilder):
    def doctype(self, name, pubid, system):
        raise TaskIdentityError("Task XML declarations are unsupported.")

    def pi(self, target, text=None):
        raise TaskIdentityError("Task XML processing instructions are unsupported.")

    def comment(self, text):
        raise TaskIdentityError("Task XML comments are unsupported.")


def _shape(node):
    if not isinstance(node.tag, str) or not node.tag.startswith(_NS):
        raise TaskIdentityError("Task XML has an unexpected namespace.")
    name = node.tag[len(_NS):]
    if set(node.attrib) - _ATTRIBUTES.get(name, set()):
        raise TaskIdentityError("Task XML has unsupported attributes.")
    allowed = _CHILDREN.get(name, "").split()
    seen = set()
    for child in node:
        if child.tag not in {_NS + key for key in allowed} or child.tag in seen:
            raise TaskIdentityError("Task XML has unknown or duplicate fields.")
        seen.add(child.tag)
        _shape(child)
    if (name in _CHILDREN and (node.text or "").strip()) or (node.tail or "").strip():
        raise TaskIdentityError("Task XML has unexpected mixed content.")


def _parse(xml):
    # Accept already decoded manager output only; do not guess its encoding or
    # treat empty/error output as absence. Reject DTDs before entity expansion.
    if not isinstance(xml, str) or not xml or len(xml) > _MAX_XML:
        raise TaskIdentityError("Task XML is missing or exceeds the size limit.")
    try:
        root = ET.fromstring(xml, parser=ET.XMLParser(target=_TreeBuilder()))
    except ET.ParseError as exc:
        raise TaskIdentityError("Task XML is malformed.") from exc
    if root.tag != _NS + "Task" or root.attrib != {"version": "1.4"}:
        raise TaskIdentityError("Unsupported task XML root or version.")
    _shape(root)
    return root


def _element(root, path):
    node = root.find("/".join(_NS + part for part in path.split("/")))
    if node is None:
        raise TaskIdentityError(f"Task XML is missing {path}.")
    return node


def _text(root, path):
    return _element(root, path).text or ""


def _check_bindings(root, expected):
    principal = _element(root, "Principals/Principal")
    actions = _element(root, "Actions")
    principal_id = principal.get("id")
    if not principal_id or actions.get("Context") != principal_id:
        raise TaskIdentityError("Task action is not bound to its sole principal.")
    for path, value in (
        ("RegistrationInfo/URI", "\\" + expected.name),
        ("Principals/Principal/UserId", expected.sid),
        ("Principals/Principal/LogonType", "InteractiveToken"),
        ("Actions/Exec/Command", expected.command),
        ("Actions/Exec/Arguments", expected.arguments),
        ("Actions/Exec/WorkingDirectory", expected.working_directory),
    ):
        if _text(root, path) != value:
            raise TaskIdentityError(f"Task identity does not match {path}.")
    # The omitted RunLevel is LUA. Do not infer actual process privileges from
    # this setting (e.g. UAC-disabled hosts); this checks definition identity.
    level = principal.find(_NS + "RunLevel")
    if level is not None and level.text != "LeastPrivilege":
        raise TaskIdentityError("Task RunLevel is not LeastPrivilege.")
    _element(root, "Triggers")
    settings = _element(root, "Settings")
    enabled = settings.find(_NS + "Enabled")
    # Microsoft's settingsType defaults omitted Enabled to true. Only the
    # task-level bit is mutable; trigger Enabled fields remain identity-bound.
    if enabled is None:
        return True
    if enabled.text not in {"true", "false"}:
        raise TaskIdentityError("Task Enabled must be true or false.")
    settings.remove(enabled)
    return enabled.text == "true"


def _definition(node):
    # Normalize only namespace spelling, attribute order and indentation
    # between elements. Never strip leaf text (especially command arguments),
    # reorder children, expand paths, or discard metadata/settings/triggers.
    text = "" if node.tag[len(_NS):] in _CHILDREN else (node.text or "")
    return (node.tag, tuple(sorted(node.attrib.items())), text,
            tuple(_definition(child) for child in node))


def verify_task_definition(xml: str, *, task_path: str, task_name: str,
                           expected: TaskIdentity, enabled: bool) -> None:
    """Require a root-folder observation matching a frozen definition.

    The only permitted semantic change is Settings/Enabled, and the observed
    value must equal the explicitly requested boolean. This function neither
    obtains a live observation nor authorizes a later task-name mutation.
    """
    if (expected.name not in _TASK_NAMES or task_path != "\\"
            or task_name != expected.name or type(enabled) is not bool):
        raise TaskIdentityError("Unexpected task name, folder or enabled expectation.")
    if not isinstance(expected.sid, str) or not re.fullmatch(r"S-1-\d+(?:-\d+)+", expected.sid):
        raise TaskIdentityError("Expected task owner must be a SID.")
    for path in (expected.command, expected.working_directory):
        if not isinstance(path, str) or not PureWindowsPath(path).is_absolute():
            raise TaskIdentityError("Expected task paths must be absolute Windows paths.")
    if not isinstance(expected.arguments, str):
        raise TaskIdentityError("Expected task arguments must be literal text.")
    frozen, observed = _parse(expected.definition_xml), _parse(xml)
    _check_bindings(frozen, expected)
    actual_enabled = _check_bindings(observed, expected)
    if _definition(frozen) != _definition(observed):
        raise TaskIdentityError("Task definition changed; retain recovery evidence.")
    if actual_enabled != enabled:
        raise TaskIdentityError("Task enabled state does not match the requested state.")
