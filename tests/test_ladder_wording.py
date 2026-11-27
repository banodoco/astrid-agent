"""R93-D: wording follows C's observed payload, not a separate state machine."""
import pytest

from astrid.core.generation import preflight
from astrid.core.generation.targets import TargetRecord
from tests.core.generation.test_desktop_rungs import installation_for


URL = "http://127.0.0.1:8000"


@pytest.mark.parametrize("case,reason,message,step", [
    ("closed", "closed", f"I found your ComfyUI Desktop. It is closed at {URL}.",
     "Open ComfyUI Desktop yourself, then I will check the same address again."),
    ("stale", "stale_registration",
     "Your ComfyUI Desktop registration points to missing files.",
     "Reinstall ComfyUI Desktop without deleting your existing files, then I will check again."),
    ("denied", "access_denied", f"Access to {URL} was denied.",
     "I'll retry the same check through the normal permission request."),
])
def test_desktop_copy_is_the_single_public_action(case, reason, message, step, monkeypatch):
    monkeypatch.setattr(preflight, "_platform", lambda: "win32")
    installation = installation_for(case)
    record = TargetRecord(endpoint=URL)
    def forbidden_probe(*args):
        pytest.fail("a stopped Desktop must not be probed for compatibility")
    ladder = preflight.observe_target_rungs(record, installation=installation, get_json=forbidden_probe)
    action = preflight.ladder_next_action(ladder, url=URL, installation=installation)
    payload = preflight.readiness_envelope(target=record, ladder=ladder, next_action=action)
    assert len(payload["issues"]) == len(payload["fixes"]) == 1
    assert payload["issues"][0]["details"]["reason"] == reason
    assert payload["issues"][0]["message"] == message
    assert action["step"] == step
    assert payload["fixes"][0]["details"]["guidance"] == step
    if case == "denied":
        assert action["who"] == "agent" and action["argv"][-1] == URL
        assert action["alternative"] is None
    else:
        assert action["who"] == "person" and action["argv"] is None
        assert action["then_argv"][-1] == (URL if case == "closed" else "auto")
        if case == "stale":
            assert action["alternative"]["available"] is False
        else:
            assert action["alternative"] is None


def test_running_desktop_copy_does_not_claim_compute_or_delivery(monkeypatch):
    monkeypatch.setattr(preflight, "_platform", lambda: "win32")
    installation = installation_for("running")
    record = TargetRecord(endpoint=URL)
    ladder = preflight.observe_target_rungs(
        record, installation=installation, get_json=lambda *_: {"ExampleNode": {}},
    )
    assert preflight.ladder_summary(ladder, installation=installation) == (
        "I found your running ComfyUI Desktop and its API is compatible. "
        "Compute, workflow and delivery have not been checked."
    )
    assert preflight.ladder_next_action(ladder, url=URL, bound=True) is None
    action = preflight.ladder_next_action(ladder, url=URL, bound=False)
    assert action["requires_consent"] == "attach_existing"
    assert action["argv"] == ["astrid", "generate", "settings", "set", "placement", URL]
    assert "only after the person says yes" in action["step"]


def test_choices_and_unsupported_copy():
    assert preflight.LADDER_TEXT["several_installs"]["message"] == "I found more than one ComfyUI."
    assert preflight.LADDER_TEXT["several_installs"]["step"] == "Which one should I use?"
    unsupported = preflight.LADDER_TEXT["not_supported_here"]
    assert unsupported["message"] == "Astrid can only discover ComfyUI Desktop installations on Windows."
    assert unsupported["step"] == "Choose the address of a running ComfyUI Portable, Git or Pinokio copy."
    for reason in preflight.LADDER_REASONS:
        wording = preflight.LADDER_TEXT[reason]
        assert wording["message"].strip() and wording["step"].strip()
        for word in ("rung", "loopback", "envelope", "ladder"):
            assert word not in (wording["message"] + wording["step"]).lower()
