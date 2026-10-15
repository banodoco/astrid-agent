"""Explicit fixture bindings test policy only, not browser authentication."""
from __future__ import annotations

import pytest

from astrid.core.execution.capability_ledger import (
    load_capability_ledger,
    project_capability_execution_admission,
)
from astrid.core.execution.provider_route_grant import (
    SELECTED_PROJECT_OPERATIONS,
    ProjectGrantAuthority,
    ProjectGrantError,
    ProviderRouteGrantAuthority,
)


@pytest.fixture
def bound():
    now = [100.0]
    authority = ProjectGrantAuthority(
        realm="fixture-realm", browser_session="fixture-session", actor_id="fixture-actor",
        selected_project="fixture-project", clock=lambda: now[0],
    )
    policy = {"realm": "fixture-realm", "browser_session": "fixture-session",
              "projects": ["fixture-project"], "operations": sorted(SELECTED_PROJECT_OPERATIONS),
              "expiry": 110.0}
    request = dict(realm="fixture-realm", browser_session="fixture-session",
                   actor_id="fixture-actor", project_id="fixture-project", operation="rendering.render")
    return authority, policy, request, now


@pytest.mark.parametrize("operation", sorted(SELECTED_PROJECT_OPERATIONS))
def test_selected_project_policy_allows_current_operation(bound, operation):
    authority, policy, request, _ = bound
    request["operation"] = operation
    handle = authority.issue(policy)
    assert "fixture" not in handle
    assert authority.decide(handle, **request) == {
        "status": "allow", "code": "project_grant_allowed", "allowed": True,
        "operation": operation, "project_id": "fixture-project",
    }
    assert authority.decide(handle, **request)["allowed"] is True


@pytest.mark.parametrize("field,value,code", [
    ("realm", "other-realm", "project_grant_realm_mismatch"),
    ("browser_session", "forged-session", "project_grant_session_mismatch"),
    ("actor_id", "forged-actor", "project_grant_actor_mismatch"),
    ("project_id", "sibling-project", "project_grant_project_denied"),
    ("operation", "publish-document", "project_grant_operation_denied"),
    ("operation", "rendering.sprite_sheet", "project_grant_operation_denied"),
    ("operation", "Edit selected project (locally approved)", "project_grant_operation_denied"),
])
def test_claims_and_misleading_titles_cannot_widen_policy(bound, field, value, code):
    authority, policy, request, _ = bound
    handle = authority.issue(policy)
    request[field] = value
    assert authority.decide(handle, **request)["code"] == code
    assert authority.decide(handle, **request)["allowed"] is False


def test_expiry_boundary_and_caller_mutation(bound):
    authority, policy, request, now = bound
    handle = authority.issue(policy)
    policy["projects"].append("sibling-project")
    policy["operations"].append("publish-document")
    policy["expiry"] = 1000
    assert authority.decide(handle, **{**request, "project_id": "sibling-project"})["allowed"] is False
    assert authority.decide(handle, **{**request, "operation": "publish-document"})["allowed"] is False
    now[0] = 110.0
    assert authority.decide(handle, **request)["code"] == "project_grant_expired"


@pytest.mark.parametrize("handle", [None, "", {}, "project-grant-v1.forged", "provider-route-grant-v1.fake"])
def test_missing_forged_and_wrong_kind_handles_fail_closed(bound, handle):
    authority, _, request, _ = bound
    assert authority.decide(handle, **request)["allowed"] is False


def test_another_host_and_real_egress_grant_do_not_authorize_project(bound):
    authority, policy, request, _ = bound
    other = ProjectGrantAuthority(realm="fixture-realm", browser_session="fixture-session",
                                  actor_id="fixture-actor", selected_project="fixture-project")
    assert other.decide(authority.issue(policy), **request)["code"] == "project_grant_unknown"
    egress = ProviderRouteGrantAuthority(clock=lambda: 100).issue(
        task_id="fixture-task", capability_id="rendering.render", capability_digest="fixture-digest",
        routes=[], broker_binding="fixture-broker")
    assert authority.decide(egress, **request)["code"] == "project_grant_unknown"


@pytest.mark.parametrize("patch,code", [
    ({"realm": "wrong"}, "project_grant_binding_mismatch"),
    ({"browser_session": "wrong"}, "project_grant_binding_mismatch"),
    ({"projects": ["sibling-project"]}, "project_grant_local_approval_required"),
    ({"projects": ["fixture-project", "sibling-project"]}, "project_grant_local_approval_required"),
    ({"operations": ["publish-document"]}, "project_grant_local_approval_required"),
    ({"expiry": 100.0}, "project_grant_expired"),
    ({"expiry": True}, "project_grant_policy_invalid"),
    ({"expiry": float("nan")}, "project_grant_policy_invalid"),
    ({"expiry": float("inf")}, "project_grant_policy_invalid"),
    ({"expiry": 10 ** 1000}, "project_grant_policy_invalid"),
    ({"expiry": "110"}, "project_grant_policy_invalid"),
    ({"projects": "fixture-project"}, "project_grant_policy_invalid"),
    ({"operations": []}, "project_grant_policy_invalid"),
    ({"operations": ["rendering.render", "rendering.render"]}, "project_grant_policy_invalid"),
    ({"operations": [{}]}, "project_grant_policy_invalid"),
    ({"title": "Approved by owner"}, "project_grant_policy_invalid"),
])
def test_host_policy_validation(bound, patch, code):
    authority, policy, _, _ = bound
    with pytest.raises(ProjectGrantError) as error:
        authority.issue({**policy, **patch})
    assert error.value.code == code


@pytest.mark.parametrize("field", ["realm", "browser_session", "projects", "operations", "expiry"])
def test_every_card_field_is_required(bound, field):
    authority, policy, _, _ = bound
    del policy[field]
    with pytest.raises(ProjectGrantError, match="project_grant_policy_invalid"):
        authority.issue(policy)


@pytest.mark.parametrize("policy", [None, {}, [], "approved"])
def test_missing_or_malformed_policy_cannot_mint(bound, policy):
    with pytest.raises(ProjectGrantError, match="project_grant_policy_invalid"):
        bound[0].issue(policy)


def test_subset_policy_does_not_inherit_default_operations(bound):
    authority, policy, request, _ = bound
    policy["operations"] = ["timeline.update"]
    handle = authority.issue(policy)
    assert authority.decide(handle, **request)["code"] == "project_grant_operation_denied"
    assert authority.decide(handle, **{**request, "operation": "timeline.update"})["allowed"] is True


def test_missing_host_session_binding_is_refused():
    with pytest.raises(ProjectGrantError, match="project_grant_host_binding_missing"):
        ProjectGrantAuthority(realm="fixture-realm", browser_session="", actor_id="fixture-actor",
                              selected_project="fixture-project")


@pytest.mark.parametrize("capability", ["rendering.render", "rendering.timeline_visualize"])
def test_project_decision_intersects_current_capability_admission(bound, capability):
    authority, policy, request, _ = bound
    handle = authority.issue(policy)
    del request["operation"]
    # Invented source projection exercises the existing ledger decision path,
    # without discovering packs, starting a renderer or claiming host readiness.
    ledger = {"capabilities": [{"id": capability, "disposition": "required"}],
              "sources": {"coverage": {"source_labels": {"complete": True}},
                          "executor_inventory": []}}
    def decide(value=ledger, grant=handle):
        return project_capability_execution_admission(value, capability, authority=authority,
                                                       grant=grant, **request)
    assert decide()["executable"] is True
    assert decide()["requires_runtime_readiness"] is True
    assert decide(grant=None)["executable"] is False
    ledger["capabilities"][0]["disposition"] = "unsupported"
    assert decide()["code"] == "project_capability_unavailable"
    ledger["capabilities"][0]["disposition"] = "required"
    ledger["sources"]["coverage"]["source_labels"]["complete"] = False
    assert decide()["executable"] is False
    assert decide({})["executable"] is False
    # A forged decision/policy dictionary cannot stand in for host authority.
    assert project_capability_execution_admission(ledger, capability, authority=policy,
                                                 grant=handle, **request)["code"] == "project_grant_authority_missing"


def test_fixture_grant_intersects_actual_shipped_source_ledger(bound):
    authority, policy, request, _ = bound
    handle = authority.issue(policy)
    del request["operation"]
    ledger = load_capability_ledger("config/astrid-beta-capabilities.json")
    for capability in ("rendering.render", "rendering.timeline_visualize"):
        decision = project_capability_execution_admission(
            ledger, capability, authority=authority, grant=handle, **request,
        )
        assert decision["executable"] is True
        assert decision["code"] == "project_capability_admitted"
        assert decision["requires_runtime_readiness"] is True
    # Edit is a Runtime operation, not an executable capability in this ledger.
    # A successful policy check cannot invent that missing consumer.
    decision = project_capability_execution_admission(
        ledger, "timeline.update", authority=authority, grant=handle, **request,
    )
    assert decision["project_decision"]["allowed"] is True
    assert decision["executable"] is False
    assert decision["code"] == "project_capability_unavailable"


@pytest.mark.parametrize("session,credential", [(120, 110), (110, 120)])
def test_authenticated_host_expiry_ceiling_cannot_be_extended_by_policy(session, credential):
    from astrid.core.execution.provider_route_grant import ProjectGrantAuthority
    clock = [100.0]
    authority = ProjectGrantAuthority(realm="r", browser_session="s", actor_id="a",
        selected_project="p", clock=lambda: clock[0], session_expiry=session, credential_expiry=credential)
    handle = authority.issue({"realm": "r", "browser_session": "s", "projects": ["p"],
        "operations": ["rendering.render"], "expiry": 9999})
    def decide():
        return authority.decide(handle, realm="r", browser_session="s", actor_id="a",
            project_id="p", operation="rendering.render")
    assert decide()["allowed"]
    clock[0] = 110
    assert decide()["code"] == "project_grant_expired"
