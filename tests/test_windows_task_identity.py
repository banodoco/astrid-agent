"""Read-only checks against a retained native Windows task export."""
import xml.etree.ElementTree as ET
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.helpers.bounded_ids import bounded_id
from astrid.installer.windows_task_identity import (
    TaskIdentity,
    TaskIdentityError,
    verify_task_definition,
)

NS = "{http://schemas.microsoft.com/windows/2004/02/mit/task}"
XML = (Path(__file__).parent / "fixtures/windows-task-boundary-41906110096.xml").read_text(
    encoding="utf-8")
VERSION = r"D:\a\_temp\astrid-r1-direct-windows\program\versions\3204bb00-6d53a190-46310e10"
EXPECTED = TaskIdentity(
    name="Astrid Runtime",
    sid="S-1-5-21-3708201392-4242277230-667464164-500",
    command=VERSION + r"\py\Scripts\pythonw.exe",
    # Freeze the entire captured action, including its inline Python and env.
    arguments=ET.fromstring(XML).find(f"{NS}Actions/{NS}Exec/{NS}Arguments").text,
    working_directory=VERSION,
    definition_xml=XML,
)


def verify(xml=XML, *, expected=EXPECTED, enabled=True, **observation):
    return verify_task_definition(
        xml, expected=expected, enabled=enabled,
        task_path=observation.get("task_path", "\\"),
        task_name=observation.get("task_name", expected.name),
    )


def edit(path, value, *, xml=XML, duplicate=False, attribute=None):
    root = ET.fromstring(xml)
    parts = path.split("/")
    parent = root
    for part in parts[:-1]:
        parent = parent.find(NS + part)
        assert parent is not None
    node = parent.find(NS + parts[-1])
    if node is None or duplicate:
        node = ET.SubElement(parent, NS + parts[-1])
    if attribute:
        node.set(attribute, value)
    else:
        node.text = value
    return ET.tostring(root, encoding="unicode")


def test_native_export_matches_frozen_r0_definition():
    assert verify() is None


def test_namespace_prefix_and_interelement_indentation_are_not_identity():
    root = ET.fromstring(XML)
    ET.indent(root, space="\t")
    assert verify(ET.tostring(root, encoding="unicode")) is None


@pytest.mark.parametrize("enabled", [True, False])
def test_only_task_enabled_may_change(enabled):
    observed = edit("Settings/Enabled", str(enabled).lower())
    assert verify(observed, enabled=enabled) is None
    # Also permit rollback against a snapshot captured while disabled.
    assert verify(expected=replace(EXPECTED, definition_xml=observed)) is None


@pytest.mark.parametrize("observed,requested", [(XML, False), (edit("Settings/Enabled", "false"), True)],
                         ids=bounded_id)
def test_enabled_state_must_match_even_when_identity_matches(observed, requested):
    with pytest.raises(TaskIdentityError, match="enabled state"):
        verify(observed, enabled=requested)


@pytest.mark.parametrize("observation", [
    {"task_path": "\\Other\\"}, {"task_path": ""},
    {"task_name": "Astrid Runtime R1"}, {"task_name": "astrid runtime"},
    {"task_name": "Astrid Runtime extra"},
])
def test_exact_root_folder_and_name_are_separate_from_xml(observation):
    with pytest.raises(TaskIdentityError):
        verify(**observation)


@pytest.mark.parametrize("path,value", [
    ("RegistrationInfo/URI", r"\Other\Astrid Runtime"),
    ("RegistrationInfo/Description", "foreign publication"),
    ("Principals/Principal/UserId", "S-1-5-18"),
    ("Principals/Principal/LogonType", "Password"),
    ("Principals/Principal/RunLevel", "HighestAvailable"),
    ("Actions/Exec/Command", VERSION + r"\foreign.exe"),
    ("Actions/Exec/Arguments", EXPECTED.arguments + " "),
    ("Actions/Exec/WorkingDirectory", VERSION + "-other"),
    ("Settings/MultipleInstancesPolicy", "Parallel"),
    ("Settings/ExecutionTimeLimit", "PT1M"),
    ("Settings/RestartOnFailure/Count", "1"),
    ("Triggers/LogonTrigger/UserId", "other-user"),
    ("Triggers/LogonTrigger/Repetition/Interval", "PT2M"),
    ("Triggers/LogonTrigger/Enabled", "false"),
    ("Triggers/TimeTrigger/StartBoundary", "2027-01-01T00:00:00Z"),
], ids=bounded_id)
def test_identity_changes_are_refused(path, value):
    with pytest.raises(TaskIdentityError):
        verify(edit(path, value))


@pytest.mark.parametrize("path,attribute", [
    ("Principals/Principal", "id"), ("Actions", "Context"),
])
def test_action_principal_binding_cannot_change(path, attribute):
    with pytest.raises(TaskIdentityError):
        verify(edit(path, "Foreign", attribute=attribute))


@pytest.mark.parametrize("path", [
    "Actions/Exec", "Principals/Principal", "Settings/Enabled",
    "Actions/Exec/Command", "Settings", "Triggers/TimeTrigger",
])
def test_duplicate_identity_and_state_fields_are_refused(path):
    xml = edit("Settings/Enabled", "true")
    with pytest.raises(TaskIdentityError, match="duplicate"):
        verify(edit(path, "", xml=xml, duplicate=True))


@pytest.mark.parametrize("path", [
    "RegistrationInfo", "RegistrationInfo/URI", "Principals", "Actions/Exec/Arguments",
    "Actions/Exec/WorkingDirectory", "Settings", "Triggers",
])
def test_missing_fields_are_not_defaulted_to_owned_or_absent(path):
    root = ET.fromstring(XML)
    parts = path.split("/")
    parent = root.find("/".join(NS + p for p in parts[:-1])) if len(parts) > 1 else root
    parent.remove(parent.find(NS + parts[-1]))
    with pytest.raises(TaskIdentityError):
        verify(ET.tostring(root, encoding="unicode"))


@pytest.mark.parametrize("xml", [
    "", "access denied", "<Task>", XML.replace('version="1.4"', 'version="1.3"'),
    XML.replace("http://schemas.microsoft.com/windows/2004/02/mit/task", "urn:foreign"),
    XML.replace("<Command>", '<Command xmlns="urn:foreign">'),
    XML.replace("<Task version=", '<Task extra="untrusted" version='),
    XML.replace("<Actions ", '<Actions xmlns:other="urn:foreign" other:Context="Owner" '),
    edit("Settings/UnknownSetting", "true"),
    edit("Principals/Principal/GroupId", "S-1-5-32-544"),
    edit("Actions/ComHandler", "untrusted"),
    edit("Settings/Enabled", "maybe"),
    edit("Settings/Enabled", ""),
    edit("Settings/Enabled", "true", attribute="unknown"),
    XML.replace("<Settings>", "<Settings>unexpected text"),
    XML.replace("</Settings>", "</Settings>unexpected tail"),
    XML.replace("<Settings>", "<!-- comment --><Settings>"),
    XML.replace("<Settings>", "<?unexpected instruction?><Settings>"),
    '<!DOCTYPE Task [<!ENTITY owner "Owner">]>' + XML[XML.index("<Task "):],
    '<!DOCTYPE Task SYSTEM "file:///must-not-read">' + XML[XML.index("<Task "):],
    " " * (128 * 1024 + 1), None, b"not decoded XML",
], ids=bounded_id)
def test_unknown_xml_is_refused_even_if_frozen_snapshot_also_contains_it(xml):
    with pytest.raises(TaskIdentityError):
        verify(xml)
    with pytest.raises(TaskIdentityError):
        verify(xml, expected=replace(EXPECTED, definition_xml=xml))


@pytest.mark.parametrize("change", [
    {"name": "Other"}, {"sid": "runneradmin"}, {"command": "pythonw.exe"},
    {"working_directory": r"C:relative"}, {"arguments": None},
])
def test_invalid_trusted_bindings_fail_closed(change):
    with pytest.raises(TaskIdentityError):
        verify(expected=replace(EXPECTED, **change))


@pytest.mark.parametrize("value", [None, 0, 1, "false"])
def test_enabled_expectation_must_be_a_boolean(value):
    with pytest.raises(TaskIdentityError):
        verify(enabled=value)


def test_r1_publication_identity_and_literal_paths_are_bound():
    command = 'C:\\Astrid & 100% 雪\\python.exe'
    directory = 'C:\\Astrid & 100% 雪'
    arguments = '-I "C:\\Astrid & 100% 雪\\runner.py" --data-root "D:\\kept data-r1"'
    xml = XML
    for path, value in [
        ("RegistrationInfo/URI", r"\Astrid Runtime R1"),
        ("RegistrationInfo/Description", "Astrid R1 publication: 8e91d1"),
        ("Actions/Exec/Command", command), ("Actions/Exec/Arguments", arguments),
        ("Actions/Exec/WorkingDirectory", directory), ("Settings/Enabled", "false"),
    ]:
        xml = edit(path, value, xml=xml)
    expected = replace(EXPECTED, name="Astrid Runtime R1", command=command,
                       arguments=arguments, working_directory=directory, definition_xml=xml)
    assert verify(xml, expected=expected, enabled=False) is None
    with pytest.raises(TaskIdentityError, match="definition changed"):
        verify(edit("RegistrationInfo/Description", "Astrid R1 publication: foreign", xml=xml),
               expected=expected, enabled=False)
    with pytest.raises(TaskIdentityError):
        verify(xml, expected=EXPECTED, enabled=False)


def test_windows_safety_refusals_still_precede_any_manager_command(tmp_path):
    from astrid.installer import boundary_registration, r0_stop

    def unexpected(*args, **kwargs):
        pytest.fail("The unqualified Windows boundary issued a manager command")

    service = SimpleNamespace(root=tmp_path, system="win32", run=unexpected, powershell=unexpected)
    with pytest.raises(ValueError, match="not qualified"):
        boundary_registration.BoundaryRegistration(service)
    with pytest.raises(r0_stop.R0StopError, match="refusing to stop"):
        r0_stop._validate_manager(service, EXPECTED.sid)
    with pytest.raises(r0_stop.R0StopError, match="not restarted"):
        r0_stop._require_no_r1(service)
