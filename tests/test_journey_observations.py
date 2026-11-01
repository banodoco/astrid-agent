"""Focused parser coverage for the M-CI journey-evidence index boundary."""
from __future__ import annotations

import copy
import hashlib
import json

import pytest

from scripts import journey_failure_modes as failure
from scripts import journey_matrix as matrix
from scripts import journey_observations as observations
from scripts import journey_release2 as release2
from scripts import native_service_adapter as native
from tests.failure_mode_fixtures import PROVISIONAL_CANDIDATE, valid_observation


def _entry(path: str, payload: dict) -> dict:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return {"path": path, "sha256": hashlib.sha256(raw).hexdigest(), "json": payload}


def _producer_record(payload: dict) -> dict:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    return {"sha256": hashlib.sha256(raw).hexdigest(), "json": payload}


def producer_envelope(owner_kind: str = "native") -> dict:
    """A shape-only R-30 export/seal pair, never authenticated evidence."""
    binding = {
        "qualification_id": "qualification-r37",
        "tuple_sha256": "a" * 64,
        "inventory_sha256": "b" * 64,
        "obligation_id": "journey-slot-1",
        "attempt_id": "attempt-1",
        "nonce": "c" * 64,
        "origin_sha256": "d" * 64,
        "execution_sha256": "e" * 64,
    }
    files = [
        {
            "path": "observations/terminal.json",
            "role": "machine-observation",
            "sha256": "f" * 64,
            "size": 2,
        }
    ]
    export = {
        "kind": "r23-export",
        "version": 1,
        "binding": binding,
        "contract_sha256": "1" * 64,
        "owner_kind": owner_kind,
        "session_id": "session-1",
        "status": "pass",
        "checks": [{"id": "terminal", "result": "pass", "observations": ["terminal"]}],
        "observations": [
            {
                "id": "terminal",
                "kind": "terminal-observation",
                "file": "observations/terminal.json",
                "sha256": "f" * 64,
                "sequence": 1,
            }
        ],
        "continuity": {
            "source_profile_sha256": "2" * 64,
            "first_sequence": 1,
            "last_sequence": 1,
            "event_count": 1,
            "gaps": 0,
            "dropped": 0,
            "terminal": True,
        },
        "files": files,
    }
    export_entry = _producer_record(export)
    seal = {
        "kind": "collector-seal",
        "version": 1,
        "binding": copy.deepcopy(binding),
        "grant_sha256": "3" * 64,
        "collector_profile_sha256": "4" * 64,
        "provider_contract_sha256": "5" * 64,
        "export_sha256": export_entry["sha256"],
        "claims_sha256": "6" * 64,
        "files_sha256": observations.canonical_json_sha256(files),
        "result": "pass",
        "context_state": "finalized",
    }
    return {"export": export_entry, "seal": _producer_record(seal)}


def release1_index(slot: str = "direct-linux") -> dict:
    contract = next(row for row in failure.SLOT_CONTRACTS.values() if row["job_id"] == slot)
    _os_name, runner_os, image_os = failure.RUNNER_PROVENANCE[contract["runner"]]
    candidate = copy.deepcopy(PROVISIONAL_CANDIDATE)
    direct = contract["mode"] == "direct"
    receipt = {
        "schema_version": 2,
        "receipt_kind": "release1-installed-lifecycle",
        "job_id": slot,
        "mode": contract["mode"],
        "scenario": contract.get("leg", "fresh-install"),
        "status": "pass",
        "runner_image": contract["runner"],
        "shell": contract["shell"],
        "source_override_test_mode": False,
        "final_cleanup": "pass",
        "os_build": runner_os + "-test",
        "runner_provenance": {"RUNNER_OS": runner_os, "ImageOS": image_os, "ImageVersion": "test"},
        "candidate_checkouts": {name: {"sha": candidate[name]} for name in ("app", "agent", "runtime", "pinokio")},
        "sources": {name: {"sha": candidate[name]} for name in ("app", "agent", "runtime")},
        "installed_tuple": {name: candidate[name] for name in ("app", "agent", "runtime")},
        "import_origins": {"runtime": "/owned/runtime"},
        "service_manager_prerequisite": (
            {
                "required": True,
                "outcome": "pass",
                "manager": native.MANAGERS[contract["os"]],
                "version": "1",
                "foreground_fallback": False,
            }
            if direct else
            {"required": False, "outcome": "explicit-owner", "managed_by": "pinokio", "foreground_fallback": False}
        ),
        "service_manager": {
            "manager": native.MANAGERS[contract["os"]],
            "version": "1",
            "foreground_fallback": False,
        },
        "release_transition": {"from": 0, "to": 1, "harness_override": "frozen-release0-direct-to-release1"},
        "release0_fixture": {
            "fixture_sums_sha256": failure.RELEASE0_SUMS_SHA256,
            "manifest_sha256": "f" * 64,
            "provenance_sha256": "a" * 64,
            "bootstrap_sha256": failure.RELEASE0_BOOTSTRAP_SHA256,
            "sources": dict(failure.RELEASE0_REFS),
        },
        "semantic_data": {"identical": True, "before_sha256": "b" * 64, "after_sha256": "b" * 64},
        "checks": {name: "pass" for name in ("release", "service", "health", "semantic_data", "task_settled", "one_runtime", "unrelated_sentinel")},
    }
    return {"job_id": slot, "candidate": candidate, "files": [_entry("journey-result.json", receipt)]}


def m4_5_index(scenario_id: str = "release2-install-line-direct-linux") -> dict:
    row = release2.scenario_definition(scenario_id)
    runner = release2.RUNNERS[row["os"]]
    receipt = {
        "schema_version": 1,
        "receipt_kind": "release2-installed-lifecycle",
        **row,
        "status": "pass",
        "evidence_kind": "hosted-lifecycle",
        "source_override_test_mode": False,
        "candidate": copy.deepcopy(release2.RELEASE1_CANDIDATE),
        "candidate_checkouts": {name: {"sha": sha} for name, sha in release2.RELEASE1_CANDIDATE.items()},
        "stand_in": release2.verified_stand_in(),
        "release_transition": {"from": 1, "to": 2, "stand_in": True},
        "installed": {"release": 2, "qualification_marker": release2.STAND_IN_MARKER, "manifest_sha256": release2.STAND_IN_MANIFEST_SHA256, "agent": release2.STAND_IN_AGENT, "runtime": release2.STAND_IN_RUNTIME, "schema_digest": release2.STAND_IN_SCHEMA},
        "runner_provenance": {"RUNNER_OS": runner["runner_os"], "ImageOS": runner["image_os"], "ImageVersion": "shape-only"},
        "os_build": runner["runner_os"] + "-shape-only",
        "service_manager_prerequisite": {"required": True, "outcome": "pass", "manager": runner["manager"], "version": "shape-only", "foreground_fallback": False},
        "semantic_data": {"identical": True, "before_sha256": "a" * 64, "after_sha256": "a" * 64},
        "checks": {name: "pass" for name in ("release", "marker", "service", "health", "semantic_data", "task_settled", "one_runtime", "unrelated_sentinel")},
        "controls": [],
        "trigger": {"channel": "install-line", "entrypoint": "public-bootstrap", "update_flag": True, "manifest_sha256": release2.STAND_IN_MANIFEST_SHA256, "root_bound": True, "data_root_bound": True},
        "actions": [],
        "execution_boundary": {},
        "final_cleanup": "pass",
    }
    if row["channel"] == "pinokio-update":
        receipt["service_manager_prerequisite"] = {
            "required": False,
            "outcome": "explicit-owner",
            "managed_by": "pinokio",
            "foreground_fallback": False,
        }
        receipt["trigger"] = {
            "channel": "pinokio-update",
            "entrypoint": "pinokio-update",
            "managed_by": "pinokio",
            "mode": row["mode"],
            "route": "backend-adapter" if row["mode"] == "pinokio-backend" else "existing-fully-local",
            "foreground_fallback": False,
            "action_provenance": {
                "pinokio_sha": release2.RELEASE1_CANDIDATE["pinokio"],
                "action_sha256": "b" * 64,
            },
        }
        receipt["checks"]["pinokio_update"] = "pass"
    else:
        receipt["checks"]["install_line"] = "pass"
    return {"scenario_id": scenario_id, "files": [_entry("journey-result.json", receipt)]}


def m4_6_index(slot: str = "direct-linux") -> dict:
    observation = valid_observation(slot)
    observation.update(
        execution_scope=native.HOSTED_SCOPE,
        collector_provenance={"kind": "synthetic"},
        collector_structural_sha256="a" * 64,
        native_service={"instances": [], "raw_observations": []},
    )
    declarations, payloads = native._evidence_bundle(observation)
    observation["evidence_files"] = declarations
    receipt = failure.create_receipt(slot, PROVISIONAL_CANDIDATE, observation)
    files = [_entry(failure.RECEIPT_NAME, receipt)]
    files.extend(
        {"path": path, "sha256": hashlib.sha256(raw).hexdigest(), "json": json.loads(raw)}
        for path, raw in payloads.items()
    )
    return {"slot_id": slot, "candidate": copy.deepcopy(PROVISIONAL_CANDIDATE), "files": files}


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode("ascii")).hexdigest()


def _binding() -> dict:
    return {
        "qualification_id": "synthetic-rehearsal-r37",
        "tuple_sha256": _digest("tuple"),
        "inventory_sha256": _digest("inventory"),
        "obligation_id": "synthetic-obligation-1",
        "attempt_id": "synthetic-attempt-1",
        "nonce": _digest("nonce"),
        "origin_sha256": _digest("origin"),
        "execution_sha256": _digest("execution"),
    }


def _custody(principal_id: str) -> dict:
    identities = ["controller-1", "child-1"]
    return {
        "principal_id": principal_id,
        "controller": {
            "incarnation": identities[0],
            "executable_sha256": _digest("controller"),
            "birth_sequence": 1,
            "reaped": True,
        },
        "descendants": [
            {
                "incarnation": identities[1],
                "parent_incarnation": identities[0],
                "executable_sha256": _digest("child"),
                "birth_sequence": 2,
                "reaped": True,
            }
        ],
        "listeners": [{"owner_incarnation": identities[0], "identity_sha256": _digest("listener")}],
        "roots": [{"owner_incarnation": identities[0], "identity_sha256": _digest("root")}],
        "locks": [{"owner_incarnation": identities[1], "identity_sha256": _digest("lock")}],
        "authorized_removal": identities,
        "removed": identities,
        "final_census": {
            "absent_incarnations": identities,
            "manager_absent": True,
            "no_owned_listeners": True,
            "no_owned_roots": True,
            "no_owned_locks": True,
            "observer_loss": 0,
            "fork_or_reparent_loss": 0,
        },
    }


def _direct_observation(selector_name: str, selector: str, candidate: dict, os_name: str) -> dict:
    principal_id = "native-principal-1"
    return {
        "kind": "direct-custody/v1",
        "version": 1,
        "owner_kind": "native",
        "selector_name": selector_name,
        "selector_value": selector,
        "candidate_sha256": observations.canonical_json_sha256(candidate),
        "manager": {
            "name": native.MANAGERS[os_name],
            "version": "synthetic-version-1",
            "definition_sha256": _digest("manager-definition"),
            "action_sha256": _digest("manager-action"),
            "principal_id": principal_id,
            "foreground_fallback": False,
        },
        "custody": _custody(principal_id),
        "lifecycle": {
            "operation_id": "synthetic-operation-1",
            "journal_sha256": _digest("native-journal"),
            "terminal": "pass",
            "health": "pass",
            "cleanup": "pass",
        },
        "semantic_data": {
            "before_sha256": _digest("native-data"),
            "after_sha256": _digest("native-data"),
            "entities": {
                name: {"before_sha256": _digest(name), "after_sha256": _digest(name)}
                for name in ("project", "document", "preferences", "cas")
            },
            "unrelated_sentinel_sha256": _digest("native-sentinel"),
        },
        "m4_6": _m4_6_groups(),
    }


def _stop_case(outcome: str) -> dict:
    return {
        "precondition_sha256": _digest(outcome + "-precondition"),
        "injection_profile_sha256": _digest(outcome + "-injection"),
        "controller_incarnation": "controller-1",
        "outcome": outcome,
        "no_write": True,
        "no_download": True,
        "no_admission": True,
        "no_retry": True,
        "protected_state_sha256": _digest(outcome + "-state"),
    }


def _m4_6_groups(stop_safety: dict | None = None) -> dict:
    return {
        "interruption_recovery": {
            "operation_id": "same-operation-1",
            "terminal": "recovered",
            "journal_sha256": _digest("interruption-journal"),
            "same_operation": True,
        },
        "failed_candidate_rollback": {
            "release0_fixture_sha256": _digest("release0-fixture"),
            "rollback": "pass",
            "data_sha256": _digest("rollback-data"),
        },
        "keep_data_uninstall": {
            "cleanup": "pass",
            "data_sha256": _digest("keep-data"),
            "no_owned_residue": True,
        },
        "stop_safety": copy.deepcopy(stop_safety or {
            "refused": _stop_case("refused"),
            "unknown": _stop_case("unknown"),
        }),
    }


def _pinokio_observation(selector_name: str, selector: str, candidate: dict) -> dict:
    terminal_source = _digest("engine-terminal")
    readiness = _digest("pinokio-readiness")
    principal_id = "pinokio-principal-1"
    return {
        "kind": "pinokio-settlement/v1",
        "version": 1,
        "owner_kind": "pinokio",
        "selector_name": selector_name,
        "selector_value": selector,
        "candidate_sha256": observations.canonical_json_sha256(candidate),
        "engine": {
            "daemon_sha256": _digest("daemon"),
            "pterm_sha256": _digest("pterm"),
            "instance_id": "engine-instance-1",
            "boot_id": "engine-boot-1",
            "app_id": "registered-app-1",
            "resource_id": "registered-resource-1",
            "root_sha256": _digest("pinokio-root"),
            "launcher_sha256": _digest("launcher"),
            "action_sha256": _digest("action"),
            "helper_graph_sha256": _digest("helpers"),
            "principal_id": principal_id,
            "pinokio_sha": candidate["pinokio"],
        },
        "settlement": {
            "request_id": "request-1",
            "execution_id": "execution-1",
            "operation_id": "operation-1",
            "journal_sha256": _digest("pinokio-journal"),
            "events": [
                {"sequence": 1, "kind": "accepted", "execution_id": "execution-1", "outcome": None, "source_sha256": None},
                {"sequence": 2, "kind": "running", "execution_id": "execution-1", "outcome": None, "source_sha256": None},
                {"sequence": 3, "kind": "terminal", "execution_id": "execution-1", "outcome": "success", "source_sha256": terminal_source},
            ],
            "terminal": {"execution_id": "execution-1", "outcome": "success", "source_sha256": terminal_source, "sequence": 3},
            "readiness_sha256": readiness,
            "process_census_sha256": _digest("pinokio-process-census"),
        },
        "stop_safety": {"refused": _stop_case("refused"), "unknown": _stop_case("unknown")},
        "restoration": {
            "seam_removed": True,
            "callbacks_fenced": True,
            "controller_incarnation": "controller-1",
            "transport_sha256": _digest("transport"),
            "checkout_sha": candidate["pinokio"],
            "runtime_sha": candidate["runtime"],
            "readiness_sha256": readiness,
            "data_sha256": _digest("pinokio-data"),
            "sentinel_sha256": _digest("pinokio-sentinel"),
            "cleanup": "pass",
        },
        "custody": _custody(principal_id),
        "m4_6": _m4_6_groups({"refused": _stop_case("refused"), "unknown": _stop_case("unknown")}),
    }


def authenticated_index(family: str, selector: str) -> dict:
    """A complete synthetic/rehearsal M-CI predicate input, never acceptance evidence."""
    if family == "release1":
        index = release1_index(selector)
        selector_name = "job_id"
        row = matrix.release1_job(selector)
        candidate = copy.deepcopy(index["candidate"])
    elif family == "m4_5":
        index = m4_5_index(selector)
        selector_name = "scenario_id"
        row = release2.scenario_definition(selector)
        candidate = {**release2.RELEASE1_CANDIDATE, "manifest_sha256": release2.STAND_IN_MANIFEST_SHA256}
        index["candidate"] = copy.deepcopy(candidate)
    elif family == "m4_6":
        index = m4_6_index(selector)
        selector_name = "slot_id"
        row = failure.SLOT_CONTRACTS[selector]
        candidate = copy.deepcopy(index["candidate"])
    else:  # pragma: no cover - test helper guard
        raise ValueError(f"unknown synthetic family {family!r}")
    owner_kind = "native" if row["mode"] == "direct" else "pinokio"
    observation_id = "direct-custody" if owner_kind == "native" else "pinokio-settlement"
    observation_path = (
        "observations/direct-custody.json"
        if owner_kind == "native" else "observations/pinokio-settlement.json"
    )
    observation_kind = "direct-custody-v1" if owner_kind == "native" else "pinokio-settlement-v1"
    payload = (
        _direct_observation(selector_name, selector, candidate, row["os"])
        if owner_kind == "native"
        else _pinokio_observation(selector_name, selector, candidate)
    )
    indexed_payload = _entry(observation_path, payload)
    raw_size = len(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    binding = _binding()
    export = {
        "kind": "r23-export",
        "version": 1,
        "binding": binding,
        "contract_sha256": _digest("producer-contract"),
        "owner_kind": owner_kind,
        "session_id": "synthetic-session-1",
        "status": "pass",
        "checks": [{"id": observation_id, "result": "pass", "observations": [observation_id]}],
        "observations": [{"id": observation_id, "kind": observation_kind, "file": observation_path, "sha256": indexed_payload["sha256"], "sequence": 1}],
        "continuity": {
            "source_profile_sha256": _digest("loss-detecting-source"),
            "first_sequence": 1,
            "last_sequence": 1,
            "event_count": 1,
            "gaps": 0,
            "dropped": 0,
            "terminal": True,
        },
        "files": [{"path": observation_path, "role": "semantic-observation", "sha256": indexed_payload["sha256"], "size": raw_size}],
    }
    export_entry = _producer_record(export)
    seal = {
        "kind": "collector-seal",
        "version": 1,
        "binding": copy.deepcopy(binding),
        "grant_sha256": _digest("grant"),
        "collector_profile_sha256": _digest("collector-profile"),
        "provider_contract_sha256": _digest("provider-contract"),
        "export_sha256": export_entry["sha256"],
        "claims_sha256": _digest("claims"),
        "files_sha256": observations.canonical_json_sha256(export["files"]),
        "result": "pass",
        "context_state": "finalized",
    }
    producer = {"export": export_entry, "seal": _producer_record(seal)}
    index["producer"] = producer
    index["producer_files"] = [indexed_payload]
    index["authenticated_context"] = {
        "kind": "mci-authenticated-context",
        "version": 1,
        "family": family,
        "selector_name": selector_name,
        "selector_value": selector,
        "candidate": copy.deepcopy(candidate),
        "binding": copy.deepcopy(binding),
        "producer_export_sha256": export_entry["sha256"],
        "collector_seal_sha256": producer["seal"]["sha256"],
        "observation_ids": [observation_id],
        "state": "finalized",
    }
    return index


@pytest.mark.parametrize(
    "raw",
    [b"{", b'{"same": 1, "same": 2}', b'{"float": 1.5}', b'{"large": 9007199254740992}', b'{"bad": "\\ud800"}'],
)
def test_raw_parser_rejects_noncanonical_or_unsafe_json(raw: bytes) -> None:
    with pytest.raises(observations.ObservationParseError):
        observations.parse_json_object(raw, hashlib.sha256(raw).hexdigest())


def test_raw_parser_binds_the_exact_raw_digest() -> None:
    raw = b'{"valid":true}'
    with pytest.raises(observations.ObservationParseError, match="SHA-256"):
        observations.parse_json_object(raw, "0" * 64)
    assert observations.parse_json_object(raw, hashlib.sha256(raw).hexdigest()) == {"valid": True}


def test_r30_producer_parser_accepts_only_a_complete_linked_export_and_seal() -> None:
    parsed, errors = observations.parse_producer_envelope(producer_envelope())
    assert parsed is not None and errors == []

    duplicate = producer_envelope()
    duplicate["export"]["json"]["observations"].append(
        copy.deepcopy(duplicate["export"]["json"]["observations"][0])
    )
    _parsed, errors = observations.parse_producer_envelope(duplicate)
    assert any("duplicate observation ID" in error for error in errors)
    assert any("duplicate sequence" in error for error in errors)

    wrong_hash = producer_envelope()
    wrong_hash["seal"]["json"]["files_sha256"] = "0" * 64
    _parsed, errors = observations.parse_producer_envelope(wrong_hash)
    assert any("R30-CJSON-1" in error for error in errors)


def test_r30_producer_parser_rejects_malformed_undeclared_and_unsettled_fields() -> None:
    malformed = producer_envelope()
    malformed["export"]["json"]["unexpected"] = True
    malformed["export"]["json"]["checks"][0]["result"] = "not-run"
    malformed["export"]["json"]["continuity"]["dropped"] = 1
    malformed["seal"]["json"]["context_state"] = "aborted"
    _parsed, errors = observations.parse_producer_envelope(malformed)
    assert any("unknown field" in error for error in errors)
    assert any("pass required" in error for error in errors)
    assert any("dropped: zero required" in error for error in errors)
    assert any("finalized required" in error for error in errors)


@pytest.mark.parametrize("path", ["../escape.json", "/absolute.json", "evidence\\bad.json", "NUL.json", "evidence/%2e.json", "dot./x.json"])
def test_portable_path_parser_rejects_unsafe_paths(path: str) -> None:
    assert observations.safe_relative_path(path) is False


def test_release1_and_m4_5_parsers_accept_their_checked_in_structural_contracts() -> None:
    release1, release1_errors = observations.parse_release1_evidence(release1_index())
    m4_5, m4_5_errors = observations.parse_m4_5_evidence(m4_5_index())
    assert release1 is not None and release1_errors == []
    assert m4_5 is not None and m4_5_errors == []


@pytest.mark.parametrize(
    ("factory", "parser", "selector", "owner_kind"),
    [
        (release1_index, observations.parse_release1_evidence, "direct-linux", "native"),
        (release1_index, observations.parse_release1_evidence, "pinokio-backend-linux", "pinokio"),
        (release1_index, observations.parse_release1_evidence, "pinokio-fully-local-linux", "pinokio"),
        (m4_5_index, observations.parse_m4_5_evidence, "release2-install-line-direct-linux", "native"),
        (m4_5_index, observations.parse_m4_5_evidence, "release2-pinokio-update-pinokio-backend-linux", "pinokio"),
        (m4_5_index, observations.parse_m4_5_evidence, "release2-pinokio-update-pinokio-fully-local-linux", "pinokio"),
    ],
)
def test_direct_and_pinokio_legacy_shapes_require_the_matching_r30_owner_kind(
    factory, parser, selector: str, owner_kind: str
) -> None:
    index = factory(selector)
    index["producer"] = producer_envelope(owner_kind)
    parsed, errors = parser(index)
    assert parsed is not None and errors == []

    index["producer"] = producer_envelope("pinokio" if owner_kind == "native" else "native")
    _parsed, errors = parser(index)
    assert any("requires" in error and "ownership" in error for error in errors)


@pytest.mark.parametrize(
    ("factory", "parser"),
    [
        (release1_index, observations.parse_release1_evidence),
        (m4_5_index, observations.parse_m4_5_evidence),
        (m4_6_index, observations.parse_m4_6_evidence),
    ],
)
def test_parsers_reject_undeclared_fields(factory, parser) -> None:
    index = factory()
    index["files"][0]["json"]["not_declared_by_contract"] = True
    _parsed, errors = parser(index)
    assert any("unknown field" in error for error in errors)


def test_release1_parser_requires_schema_kind_full_r0_binding_and_native_manager_identity() -> None:
    index = release1_index()
    receipt = index["files"][0]["json"]
    del receipt["schema_version"]
    receipt["release0_fixture"]["bootstrap_sha256"] = {}
    receipt["service_manager"]["manager"] = "foreground-process"
    _parsed, errors = observations.parse_release1_evidence(index)
    assert any("receipt.schema_version" in error for error in errors)
    assert any("release-0 provenance" in error for error in errors)
    assert any("direct service ownership" in error for error in errors)


def test_m4_5_parser_rejects_nested_unknown_or_duplicate_check_shapes() -> None:
    index = m4_5_index()
    receipt = index["files"][0]["json"]
    receipt["trigger"]["forged"] = True
    receipt["checks"]["duplicate-claim"] = "pass"
    _parsed, errors = observations.parse_m4_5_evidence(index)
    assert any("receipt.trigger: unknown field" in error for error in errors)
    assert any("receipt.checks: unknown field" in error for error in errors)


def test_m4_6_parser_rejects_sidecar_mismatch_and_malformed_observation() -> None:
    index = m4_6_index()
    sidecar = next(item for item in index["files"] if item["path"] == "evidence/stop_safety.json")
    sidecar["json"]["observation"] = {"status": "forged"}
    sidecar["json"]["not_declared_by_contract"] = True
    _parsed, errors = observations.parse_m4_6_evidence(index)
    assert any("unknown field" in error for error in errors)
    assert any("parsed observation differs" in error for error in errors)


def test_m4_6_parser_rejects_duplicate_missing_wrong_slot_and_wrong_tuple() -> None:
    index = m4_6_index()
    index["files"].append(copy.deepcopy(index["files"][1]))
    index["files"].pop(2)
    index["slot_id"] = "direct-macos"
    index["candidate"]["agent"] = "f" * 40
    _parsed, errors = observations.parse_m4_6_evidence(index)
    assert any("duplicate evidence path" in error for error in errors)
    assert any("missing evidence" in error for error in errors)
    assert any("differs from selected tuple" in error or "candidate_checkouts.agent" in error for error in errors)
