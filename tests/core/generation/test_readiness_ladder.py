"""CD-1: observations, not a lack of issues, establish readiness."""
import copy
import json

import pytest

from astrid.core.generation.preflight import (
    LADDER_ISSUE_CODES,
    LADDER_OWNERS,
    LADDER_RUNGS,
    LADDER_THROUGH,
    GenerationReadinessError,
    ladder_stop,
    make_next_action,
    new_ladder,
    readiness_envelope,
    with_rung,
)
from astrid.core.generation.setup_contract import uses_setup_contract


RUNGS = ("installed", "reachable", "identified", "compatible", "computes", "workflow_ready", "delivered")
OWNERS = ("existing-install", "existing-install", "existing-install", "existing-install",
          "environment", "workflow", "delivery")
CODES = ("target_not_installed", "target_unreachable", "target_not_identified",
         "target_incompatible", "target_not_computing", "workflow_not_ready")
COMMAND = ["astrid", "generate", "targets", "--placement", "auto"]


def observed_prefix(through, *, before=None):
    ladder = new_ladder(through)
    for rung in RUNGS:
        if rung == before:
            break
        ladder = with_rung(ladder, rung, "observed", observation={})
        if rung == through:
            break
    return ladder


def check_envelope(envelope):
    assert not uses_setup_contract(envelope)
    assert json.loads(json.dumps(envelope)) == envelope
    if envelope["status"] == "ready":
        ladder = envelope["ladder"]
        assert ladder["stopped_at"] is None
        for rung in ladder["rungs"][:RUNGS.index(ladder["through"]) + 1]:
            assert rung["state"] == "observed"


def test_legacy_envelopes_are_unchanged():
    assert readiness_envelope() == {
        "entry": None, "target": None, "status": "ready", "issues": [], "fixes": [],
    }
    assert readiness_envelope(entry={"id": "entry"}, target={"endpoint": "url"},
                              issues=[{"code": "version_drift", "severity": "warning", "message": "drift"}]) == {
        "entry": {"id": "entry"}, "target": {"endpoint": "url"}, "status": "ready_with_warnings",
        "issues": [{"code": "version_drift", "severity": "warning", "message": "drift", "details": {}}],
        "fixes": [],
    }
    assert readiness_envelope(issues=[{"code": "missing_dependency", "severity": "error", "message": "missing"}],
                              fixes=[{"argv": ("astrid", "engines"), "scope": "read_only"}]) == {
        "entry": None, "target": None, "status": "blocked",
        "issues": [{"code": "missing_dependency", "severity": "error", "message": "missing", "details": {}}],
        "fixes": [{"argv": ["astrid", "engines"], "scope": "read_only", "details": {}}],
    }
    with pytest.raises(ValueError):
        readiness_envelope(next_action=make_next_action("person", "Open ComfyUI."))


def test_new_ladder_order_owners_and_validation():
    assert LADDER_RUNGS == RUNGS
    assert LADDER_OWNERS == dict(zip(RUNGS, OWNERS))
    assert LADDER_THROUGH == RUNGS[:-1]
    assert LADDER_ISSUE_CODES == dict(zip(RUNGS[:-1], CODES))
    assert new_ladder("compatible") == {
        "through": "compatible", "stopped_at": None,
        "rungs": [{"rung": rung, "owner": owner, "state": "unchecked",
                   "observation": None, "reason": None, "message": None}
                  for rung, owner in zip(RUNGS, OWNERS)],
    }
    for through in ("delivered", "missing", None):
        with pytest.raises(ValueError):
            new_ladder(through)


def test_with_rung_is_a_deep_copy_and_validates():
    original = new_ladder("compatible")
    evidence = {"paths": ["root"]}
    updated = with_rung(original, "installed", "observed", observation=evidence)
    assert original == new_ladder("compatible")
    assert updated["rungs"][0]["observation"] == {"paths": ["root"]}
    evidence["paths"].append("changed")
    updated["rungs"][1]["reason"] = "later"
    assert updated["rungs"][0]["observation"] == {"paths": ["root"]}
    assert original["rungs"][1]["reason"] is None
    assert with_rung(original, "installed", "unchecked", reason="not_reached")["rungs"][0]["reason"] == "not_reached"
    for rung, state, kwargs in [
        ("missing", "observed", {"observation": {}}),
        ("installed", "observed", {}),
        ("installed", "observed", {"observation": []}),
        ("installed", "failed", {}),
        ("installed", "failed", {"reason": ""}),
        ("installed", "failed", {"reason": 12}),
        ("installed", "skipped", {}),
    ]:
        with pytest.raises(ValueError):
            with_rung(original, rung, state, **kwargs)


@pytest.mark.parametrize("through", RUNGS[:-1])
def test_observed_prefix_ready_without_later_rungs(through):
    ladder = observed_prefix(through)
    envelope = readiness_envelope(ladder=ladder)
    assert envelope == {
        "entry": None, "target": None, "status": "ready", "issues": [], "fixes": [],
        "ladder": ladder, "next_action": None,
    }
    assert ladder_stop(ladder) is None
    assert envelope["ladder"]["rungs"][-1]["state"] == "unchecked"
    check_envelope(envelope)


def test_empty_issues_do_not_make_an_unchecked_rung_ready():
    ladder = observed_prefix("compatible", before="reachable")
    envelope = readiness_envelope(ladder=ladder, issues=[])
    assert envelope["status"] == "blocked"
    assert ladder_stop(ladder)["rung"] == "reachable"
    assert envelope["ladder"]["stopped_at"] == "reachable"
    assert envelope["issues"] == [{
        "code": "rung_unchecked", "severity": "error", "message": "reachable was not observed",
        "details": {"rung": "reachable", "reason": None, "field": "target"},
    }]
    assert envelope["next_action"] == {
        "who": "person", "step": "Check the reachable step (not checked), then ask me to check again.",
        "argv": None, "then_argv": COMMAND, "alternative": None, "choices": None,
    }
    assert envelope["fixes"] == [{
        "scope": "attached_target", "details": {
            "guidance": envelope["next_action"]["step"], "who": "person", "then_argv": COMMAND,
        },
    }]
    assert "argv" not in envelope["fixes"][0]
    assert ladder["stopped_at"] is None
    check_envelope(envelope)


@pytest.mark.parametrize(("rung", "code"), zip(RUNGS[:-1], CODES))
def test_failed_rung_retains_reason_message_and_one_action(rung, code):
    ladder = with_rung(observed_prefix("workflow_ready", before=rung), rung, "failed",
                       reason="missing", message="Check this observation.")
    action = make_next_action("agent", "Check again.", argv=COMMAND)
    envelope = readiness_envelope(ladder=ladder, next_action=action)
    assert envelope["status"] == "blocked"
    assert envelope["ladder"]["stopped_at"] == rung
    assert envelope["issues"] == [{
        "code": code, "severity": "error", "message": "Check this observation.",
        "details": {"rung": rung, "reason": "missing", "field": "target"},
    }]
    assert envelope["next_action"] == action
    assert envelope["fixes"] == [{
        "argv": COMMAND, "scope": "read_only", "details": {"guidance": "Check again.", "who": "agent"},
    }]
    check_envelope(envelope)


def test_first_stop_and_default_endpoint():
    ladder = observed_prefix("compatible")
    ladder = with_rung(ladder, "computes", "failed", reason="no_gpu")
    assert readiness_envelope(ladder=ladder)["status"] == "ready"
    ladder = with_rung(observed_prefix("workflow_ready", before="computes"), "workflow_ready", "failed", reason="bad")
    envelope = readiness_envelope(ladder=ladder, target={"endpoint": "http://127.0.0.1:8000"})
    assert envelope["ladder"]["stopped_at"] == "computes"
    assert envelope["issues"][0]["code"] == "rung_unchecked"
    assert envelope["next_action"]["then_argv"] == COMMAND[:-1] + ["http://127.0.0.1:8000"]
    check_envelope(envelope)


def test_person_action_choices_and_alternative_are_copied():
    alternative = {"kind": "separate_copy", "available": False}
    choices = [{"label": "Desktop", "endpoint": "http://127.0.0.1:8000"}]
    action = make_next_action("person", "Choose an install.", alternative=alternative, choices=choices)
    assert action == {"who": "person", "step": "Choose an install.", "argv": None,
                      "then_argv": None, "alternative": alternative, "choices": choices}
    ladder = new_ladder("compatible")
    envelope = readiness_envelope(ladder=ladder, next_action=action)
    assert envelope["fixes"] == [{"scope": "attached_target", "details": {
        "guidance": "Choose an install.", "who": "person", "then_argv": None,
    }}]
    alternative["available"] = True
    choices[0]["label"] = "changed"
    action["choices"][0]["label"] = "changed again"
    ladder["rungs"][0]["message"] = "changed"
    assert envelope["next_action"]["alternative"]["available"] is False
    assert envelope["next_action"]["choices"][0]["label"] == "Desktop"
    assert envelope["ladder"]["rungs"][0]["message"] is None
    check_envelope(envelope)


@pytest.mark.parametrize(("who", "step", "kwargs"), [
    ("human", "Open", {}),
    ("person", "", {}),
    ("person", "Open", {"argv": COMMAND}),
    ("person", "Open", {"then_argv": []}),
    ("agent", "Check", {}),
    ("agent", "Check", {"argv": []}),
    ("agent", "Check", {"argv": COMMAND, "then_argv": COMMAND}),
    ("agent", "Check", {"argv": "astrid generate targets"}),
])
def test_invalid_action_shapes(who, step, kwargs):
    with pytest.raises(ValueError):
        make_next_action(who, step, **kwargs)


def test_no_stop_can_have_an_attach_action_but_cannot_hide_other_issues():
    ladder = observed_prefix("compatible")
    action = make_next_action("agent", "Attach after consent.", argv=["astrid", "generate", "settings", "set", "placement", "url"])
    envelope = readiness_envelope(ladder=ladder, next_action=action)
    assert envelope["status"] == "ready"
    assert envelope["next_action"] == action
    assert envelope["fixes"] == [{"argv": action["argv"], "scope": "read_only",
                                  "details": {"guidance": action["step"], "who": "agent"}}]
    check_envelope(envelope)
    for severity, status in [("warning", "ready_with_warnings"), ("error", "blocked")]:
        other = readiness_envelope(ladder=ladder, issues=[
            {"code": "other", "severity": severity, "message": "Other observation."},
        ])
        assert other["status"] == status
        assert other["ladder"]["stopped_at"] is None
        check_envelope(other)


def test_envelope_rejects_malformed_ladders_and_unrelated_fixes():
    ladder = new_ladder("compatible")
    action = make_next_action("person", "Open.")
    for broken in [
        {**ladder, "through": "delivered"},
        {**ladder, "rungs": ladder["rungs"][:-1]},
        {**ladder, "rungs": list(reversed(ladder["rungs"]))},
    ]:
        with pytest.raises(ValueError):
            readiness_envelope(ladder=broken)
    broken = copy.deepcopy(ladder)
    broken["rungs"][0]["state"] = "observed"
    with pytest.raises(ValueError):
        readiness_envelope(ladder=broken)
    with pytest.raises(ValueError):
        readiness_envelope(ladder=ladder, next_action={"who": "agent", "step": "Check."})
    fix = {"scope": "read_only", "argv": COMMAND}
    with pytest.raises(ValueError):
        readiness_envelope(ladder=ladder, next_action=action, fixes=[fix])
    with pytest.raises(ValueError):
        readiness_envelope(ladder=ladder, attempt_count=0)
    envelope = readiness_envelope(ladder=ladder, next_action=action)
    with pytest.raises(ValueError):
        readiness_envelope(ladder=ladder, next_action=action, fixes=envelope["fixes"] * 2)


@pytest.mark.parametrize("who", ["person", "agent"])
def test_error_round_trip_and_reprojection_are_idempotent(who):
    ladder = with_rung(new_ladder("compatible"), "installed", "failed",
                       reason="not_installed", message="Installation missing.")
    action = (make_next_action("person", "Open.", then_argv=COMMAND) if who == "person"
              else make_next_action("agent", "Check.", argv=COMMAND))
    envelope = readiness_envelope(ladder=ladder, next_action=action)
    assert envelope == readiness_envelope(ladder=ladder, next_action=action)
    assert readiness_envelope(ladder=envelope["ladder"], next_action=envelope["next_action"],
                              issues=envelope["issues"], fixes=envelope["fixes"]) == envelope
    assert GenerationReadinessError(envelope).to_envelope() == {**envelope, "task_id": None}
    assert GenerationReadinessError(envelope, task_id="task-1").to_envelope() == {**envelope, "task_id": "task-1"}
    check_envelope(envelope)
