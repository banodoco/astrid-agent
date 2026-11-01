"""Alternative workflow file ports stay absent across staged child admission."""
from pathlib import Path

import pytest

import astrid
from astrid.core.contracts.binding import expand_command
from astrid.packs.vibecomfy.actions.run.run import build_parser as run_parser
from astrid.packs.vibecomfy.actions.validate.run import build_parser as validate_parser
from tests.sdk.test_child_bridge_d18 import channel as channel
from tests.sdk.test_child_bridge_d18 import isolated as isolated

PACK_ROOT = Path(astrid.__file__).parent / "packs" / "vibecomfy"
MEMBERS = {"python": "workflow.py", "companion": "workflow.vibe.json", "source": "source.json"}


def _canonical_inputs():
    return {
        port: {"object_id": "sha256:" + str(index) * 64, "filename": filename}
        for index, (port, filename) in enumerate(MEMBERS.items(), start=1)
    }


@pytest.mark.parametrize("action", ["validate", "run"])
@pytest.mark.parametrize("canonical", [True, False], ids=["canonical", "ui-json"])
def test_optional_workflow_files_stay_absent_on_serialized_child_request(
        action, canonical, isolated, channel):
    _, messages, _ = channel
    inputs = _canonical_inputs() if canonical else {
        "workflow": {"object_id": "sha256:" + "4" * 64, "filename": "workflow.json"},
    }
    if canonical and action == "validate":
        inputs["python_execution_consent"] = "confirmed"
    refs = [
        {"name": port, "producer_task_id": "compile-task", "association_id": "compile-" + port,
         "output_port": port}
        for port in inputs if port in {*MEMBERS, "workflow"}
    ]
    result = astrid.invoke(
        "vibecomfy." + action, kind="action", inputs=inputs,
        extra_pack_roots=(str(PACK_ROOT),), child_key=action, stage=action, input_refs=refs,
    )
    assert result.ok
    (request,) = messages  # Decoded by the host side of the actual socket bridge.
    file_inputs = {name: value for name, value in inputs.items() if name in {*MEMBERS, "workflow"}}
    assert request["input_descriptors"] == [
        {"name": name, "kind": "object", "object_id": value["object_id"]}
        for name, value in file_inputs.items()
    ]
    absent = ({"workflow", "source_video", "managed_assets"} if canonical else
              {*MEMBERS, "source_video", "managed_assets"})
    assert not absent.intersection(request["inputs"])
    assert all(request["inputs"][name] == value for name, value in inputs.items())
    assert request["stage"] == action and request["input_refs"] == refs

    # After custody, only supplied file descriptors become worker-local paths.
    # Exercise the declared command too: an omitted positional placeholder is
    # still an error even when transport correctly omits its file descriptor.
    capability = astrid.get_capability(
        "vibecomfy." + action, kind="action", extra_pack_roots=(str(PACK_ROOT),),
    )
    paths = {name: "/worker/" + value["filename"] for name, value in file_inputs.items()}
    values = {**request["inputs"], **paths, "out": "/worker/out", "python_exec": "python",
              "task_identity": "task", "attempt_identity": "attempt", "execution_identity": "execution",
              "readiness_profile_path": "/worker/profile.json", "readiness_profile_hash": "sha256:" + "5" * 64}
    command = expand_command(
        capability.definition["command"], capability.inputs, values, capability.definition["metadata"],
    ).argv
    parsed = (validate_parser() if action == "validate" else run_parser()).parse_args(command[3:])
    assert parsed.workflow == ("" if canonical else paths["workflow"])
    for name in MEMBERS:
        assert getattr(parsed, name) == (paths[name] if canonical else "")
    if action == "run":
        assert parsed.source_video == parsed.managed_assets == ""


@pytest.mark.parametrize("action", ["validate", "run"])
@pytest.mark.parametrize("workflow", [
    "", "/caller/workflow.json", {"object_id": "sha256:bad"},
    {"object_id": "sha256:" + "a" * 64, "digest": "sha256:" + "b" * 64},
], ids=["empty-string", "caller-path", "bad-digest", "mismatched-digest"])
def test_explicit_invalid_workflow_still_fails_managed_digest_validation(
        action, workflow, isolated, channel):
    _, messages, _ = channel
    with pytest.raises(astrid.CapabilityValidationError, match="valid SHA-256 digest"):
        astrid.invoke(
            "vibecomfy." + action, kind="action",
            inputs={**_canonical_inputs(), "workflow": workflow},
            extra_pack_roots=(str(PACK_ROOT),), child_key=action,
        )
    assert messages == []
