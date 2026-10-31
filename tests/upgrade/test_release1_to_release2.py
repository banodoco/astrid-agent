"""Focused admission and receipt controls for the provisional M4-5 module."""

import hashlib
import json
from copy import deepcopy

import pytest

from scripts import journey_release2 as release2


def admission(row):
    return release2.validate_admission(
        scenario_id=row["scenario_id"],
        candidate=release2.RELEASE1_CANDIDATE,
        stand_in_marker=release2.STAND_IN_MARKER,
        channel=row["channel"],
        mode=row["mode"],
        os_name=row["os"],
        job_id=row["job_id"],
        runner=row["runner"],
        shell=row["shell"],
    )


def valid_receipt(row):
    admitted = admission(row)
    runner = release2.RUNNERS[row["os"]]
    receipt = {
        "schema_version": 1,
        "receipt_kind": "release2-installed-lifecycle",
        "scenario_id": row["scenario_id"],
        "scenario": row["scenario"],
        "job_id": row["job_id"],
        "channel": row["channel"],
        "mode": row["mode"],
        "os": row["os"],
        "runner": row["runner"],
        "shell": row["shell"],
        "status": "pass",
        "evidence_kind": "hosted-lifecycle",
        "source_override_test_mode": False,
        "candidate": deepcopy(release2.RELEASE1_CANDIDATE),
        "candidate_checkouts": {
            name: {"sha": sha} for name, sha in release2.RELEASE1_CANDIDATE.items()
        },
        "stand_in": admitted["stand_in"],
        "release_transition": {"from": 1, "to": 2, "stand_in": True},
        "installed": {
            "release": 2,
            "qualification_marker": release2.STAND_IN_MARKER,
            "manifest_sha256": release2.STAND_IN_MANIFEST_SHA256,
            "agent": release2.STAND_IN_AGENT,
            "runtime": release2.STAND_IN_RUNTIME,
            "schema_digest": release2.STAND_IN_SCHEMA,
        },
        "runner_provenance": {
            "RUNNER_OS": runner["runner_os"],
            "ImageOS": runner["image_os"],
            "ImageVersion": "synthetic-shape-only",
        },
        "os_build": runner["runner_os"] + "-synthetic-shape-only",
        "service_manager_prerequisite": {
            "required": True,
            "outcome": "pass",
            "manager": runner["manager"],
            "version": "shape-only",
            "foreground_fallback": False,
        },
        "semantic_data": {
            "identical": True,
            "before_sha256": "a" * 64,
            "after_sha256": "a" * 64,
        },
        "checks": {
            name: "pass"
            for name in (
                "release",
                "marker",
                "service",
                "health",
                "semantic_data",
                "task_settled",
                "one_runtime",
                "unrelated_sentinel",
            )
        },
        "controls": [],
        "final_cleanup": "pass",
    }
    if row["channel"] == "button":
        receipt["trigger"] = {
            "channel": "button",
            "entrypoint": "post-v1-update",
            "request": "explicit-update",
            "draft_flush": "pass",
            "native_job": {
                "format": "astrid-update-platform/v1",
                "system": runner["native_system"],
                "one_shot": True,
                "owned": True,
                "acknowledged": True,
                "operation_id": "operation-main",
            },
        }
        receipt["controls"] = [
            {
                "control": "lost-acknowledgement",
                "polarity": "negative",
                "operation_id": "operation-lost-ack",
                "states": ["active", "recovery-needed"],
                "automatic_update_requests": 0,
                "terminal_inferred": False,
            },
            {
                "control": "lost-acknowledgement",
                "polarity": "positive",
                "operation_id": "operation-lost-ack",
                "action": "explicit-update",
                "states": ["recovery-needed", "active", "terminal"],
                "reconciled_under_lock": True,
                "logical_acceptances": 1,
                "new_operation_id": False,
                "result": "updated",
            },
        ]
        receipt["checks"].update(native_one_shot="pass", lost_ack_controls="pass")
        if row["os"] == "linux":
            receipt["controls"].extend(
                [
                    {
                        "control": "unsaved-draft",
                        "polarity": "negative",
                        "surface": "browser",
                        "save": "failed",
                        "update_requests": 0,
                        "native_jobs": 0,
                        "installed_release": 1,
                    },
                    {
                        "control": "unsaved-draft",
                        "polarity": "positive",
                        "surface": "browser",
                        "save": "pass",
                        "update_requests": 1,
                        "native_jobs": 1,
                        "installed_release": 2,
                    },
                ]
            )
            receipt["checks"]["draft_controls"] = "pass"
    elif row["channel"] == "install-line":
        receipt["trigger"] = {
            "channel": "install-line",
            "entrypoint": "public-bootstrap",
            "update_flag": True,
            "manifest_sha256": release2.STAND_IN_MANIFEST_SHA256,
            "root_bound": True,
            "data_root_bound": True,
        }
        receipt["checks"]["install_line"] = "pass"
    else:
        receipt["service_manager_prerequisite"] = {
            "required": False,
            "outcome": "explicit-owner",
            "managed_by": "pinokio",
            "foreground_fallback": False,
        }
        route = "backend-adapter" if row["mode"] == "pinokio-backend" else "existing-fully-local"
        receipt["trigger"] = {
            "channel": "pinokio-update",
            "entrypoint": "pinokio-update",
            "managed_by": "pinokio",
            "mode": row["mode"],
            "route": route,
            "foreground_fallback": False,
            "action_provenance": {
                "pinokio_sha": release2.RELEASE1_CANDIDATE["pinokio"],
                "action_sha256": "b" * 64,
            },
        }
        receipt["checks"]["pinokio_update"] = "pass"
    return receipt


def scenario(channel, mode, os_name="linux"):
    return next(
        row
        for row in release2.RELEASE2_SCENARIOS
        if row["channel"] == channel and row["mode"] == mode and row["os"] == os_name
    )


def test_release2_fixture_is_exact_and_visible_marker_is_distinct_from_descriptor():
    receipt = release2.verified_stand_in()
    assert receipt["fixture_sums_sha256"] == release2.STAND_IN_SUMS_SHA256
    assert receipt["manifest_sha256"] == release2.STAND_IN_MANIFEST_SHA256
    assert receipt["qualification_marker"] == release2.STAND_IN_MARKER
    assert release2.STAND_IN_MARKER != release2.STAND_IN_DESCRIPTOR_MARKER
    assert receipt["target"] == {
        "agent": release2.STAND_IN_AGENT,
        "runtime": release2.STAND_IN_RUNTIME,
        "schema_digest": release2.STAND_IN_SCHEMA,
    }


def test_r1_installer_canonical_rewrite_preserves_exact_stand_in_bytes(tmp_path, monkeypatch):
    from astrid.installer import backend as installer
    from astrid.core.generation.vibecomfy_dependency import VIBECOMFY_ENGINE_REVISION

    source = release2.STAND_IN_MANIFEST.read_bytes()
    manifest = json.loads(source)
    assert VIBECOMFY_ENGINE_REVISION == release2.STAND_IN_VIBECOMFY
    assert manifest["vibecomfy"] == {"sha": VIBECOMFY_ENGINE_REVISION}
    uv = tmp_path / "uv"
    uv.touch()
    backend = installer.Backend(tmp_path / "root", tmp_path / "log", uv)
    monkeypatch.setattr(backend, "run", lambda *_args, **_kwargs: "uv " + manifest["tools"]["uv"]["version"])

    class Staged(Exception):
        pass

    def stop_after_manifest(*_args, **_kwargs):
        raise Staged

    monkeypatch.setattr(installer, "source", stop_after_manifest)
    with pytest.raises(Staged):
        backend.stage(manifest, tmp_path / "root", tmp_path / "data")
    installed = next((tmp_path / "root" / "versions").glob("*/manifest.json"))
    assert installed.read_bytes() == source
    assert hashlib.sha256(installed.read_bytes()).hexdigest() == release2.STAND_IN_MANIFEST_SHA256


@pytest.mark.parametrize("row", release2.RELEASE2_SCENARIOS,
                         ids=[row["scenario_id"] for row in release2.RELEASE2_SCENARIOS])
@pytest.mark.parametrize("field", ("release", "qualification_marker", "manifest_sha256",
                                   "agent", "runtime", "schema_digest"))
@pytest.mark.parametrize("missing", (False, True), ids=("wrong", "missing"))
def test_receipt_rejects_wrong_or_missing_installed_r2_identity_on_every_path(row, field, missing):
    receipt = valid_receipt(row)
    assert release2.receipt_errors(receipt) == []
    if missing:
        del receipt["installed"][field]
    else:
        receipt["installed"][field] = "wrong"
    assert any("installed stand-in identity" in error for error in release2.receipt_errors(receipt))


def test_release2_matrix_covers_every_os_and_owned_channel_mode():
    assert len(release2.RELEASE2_SCENARIOS) == 12
    observed = {(row["os"], row["channel"], row["mode"]) for row in release2.RELEASE2_SCENARIOS}
    expected = {
        (os_name, channel, mode)
        for os_name in release2.RUNNERS
        for channel, modes in release2.CHANNEL_MODES.items()
        for mode in modes
    }
    assert observed == expected
    assert scenario("button", "direct", "linux")["controls"] == (
        "lost-acknowledgement",
        "unsaved-draft",
    )


@pytest.mark.parametrize(
    "row",
    release2.RELEASE2_SCENARIOS,
    ids=[row["scenario_id"] for row in release2.RELEASE2_SCENARIOS],
)
def test_release2_admission_accepts_only_the_exact_scenario_inputs(row):
    admitted = admission(row)
    assert admitted["candidate"] == release2.RELEASE1_CANDIDATE
    assert admitted["stand_in"]["qualification_marker"] == release2.STAND_IN_MARKER
    assert admitted["channel"] == row["channel"]
    assert admitted["mode"] == row["mode"]


@pytest.mark.parametrize("field", tuple(release2.RELEASE1_CANDIDATE))
def test_release2_admission_rejects_missing_or_wrong_candidate_refs(field):
    row = scenario("button", "direct")
    candidate = deepcopy(release2.RELEASE1_CANDIDATE)
    candidate[field] = "f" * 40
    values = dict(
        scenario_id=row["scenario_id"],
        candidate=candidate,
        stand_in_marker=release2.STAND_IN_MARKER,
        channel=row["channel"],
        mode=row["mode"],
        os_name=row["os"],
        job_id=row["job_id"],
        runner=row["runner"],
        shell=row["shell"],
    )
    with pytest.raises(ValueError, match=f"candidate {field}"):
        release2.validate_admission(**values)
    del candidate[field]
    with pytest.raises(ValueError, match=f"candidate {field}"):
        release2.validate_admission(**{**values, "candidate": candidate})


@pytest.mark.parametrize(
    ("change", "message"),
    (
        ({"stand_in_marker": None}, "qualification marker"),
        ({"channel": None}, "inputs are required"),
        ({"mode": "pinokio-backend"}, "mode input differs"),
        ({"source_override": True}, "source override"),
    ),
)
def test_release2_admission_rejects_missing_marker_channel_mode_and_override(change, message):
    row = scenario("button", "direct")
    values = dict(
        scenario_id=row["scenario_id"],
        candidate=release2.RELEASE1_CANDIDATE,
        stand_in_marker=release2.STAND_IN_MARKER,
        channel=row["channel"],
        mode=row["mode"],
        os_name=row["os"],
        job_id=row["job_id"],
        runner=row["runner"],
        shell=row["shell"],
        source_override=False,
    )
    with pytest.raises(ValueError, match=message):
        release2.validate_admission(**{**values, **change})


@pytest.mark.parametrize("os_name", tuple(release2.RUNNERS))
def test_button_receipt_requires_owned_native_one_shot_and_lost_ack_controls(os_name):
    row = scenario("button", "direct", os_name)
    receipt = valid_receipt(row)
    assert release2.receipt_errors(receipt) == []
    bad = deepcopy(receipt)
    bad["trigger"]["native_job"]["one_shot"] = False
    assert any("one-shot" in error for error in release2.receipt_errors(bad))
    bad = deepcopy(receipt)
    bad["controls"][0]["automatic_update_requests"] = 1
    assert any("without retry" in error for error in release2.receipt_errors(bad))
    bad = deepcopy(receipt)
    bad["controls"][1]["operation_id"] = "new-operation"
    assert any("explicit-Update reconciliation" in error for error in release2.receipt_errors(bad))


def test_linux_unsaved_draft_is_an_executable_negative_positive_control():
    receipt = valid_receipt(scenario("button", "direct", "linux"))
    assert release2.receipt_errors(receipt) == []
    bad = deepcopy(receipt)
    negative = next(
        item
        for item in bad["controls"]
        if item["control"] == "unsaved-draft" and item["polarity"] == "negative"
    )
    negative["update_requests"] = 1
    assert any("refuse before" in error for error in release2.receipt_errors(bad))
    bad = deepcopy(receipt)
    positive = next(
        item
        for item in bad["controls"]
        if item["control"] == "unsaved-draft" and item["polarity"] == "positive"
    )
    positive["save"] = "failed"
    assert any("guarded save" in error for error in release2.receipt_errors(bad))


def test_install_line_receipt_requires_explicit_update_manifest_and_roots():
    receipt = valid_receipt(scenario("install-line", "direct"))
    assert release2.receipt_errors(receipt) == []
    for field in ("update_flag", "root_bound", "data_root_bound"):
        bad = deepcopy(receipt)
        bad["trigger"][field] = False
        assert any("install-line" in error for error in release2.receipt_errors(bad)), field


@pytest.mark.parametrize("mode", ("pinokio-backend", "pinokio-fully-local"))
def test_pinokio_update_receipts_require_exact_owner_mode_route_and_action(mode):
    receipt = valid_receipt(scenario("pinokio-update", mode))
    assert release2.receipt_errors(receipt) == []
    bad = deepcopy(receipt)
    bad["trigger"]["foreground_fallback"] = True
    assert any("Pinokio Update" in error for error in release2.receipt_errors(bad))
    bad = deepcopy(receipt)
    bad["trigger"]["action_provenance"].pop("action_sha256")
    assert any("action provenance" in error for error in release2.receipt_errors(bad))


def test_receipt_rejects_source_evidence_candidate_marker_and_semantic_drift():
    receipt = valid_receipt(scenario("install-line", "direct", "windows"))
    for mutate, message in (
        (lambda row: row.update(evidence_kind="source-test"), "hosted lifecycle"),
        (lambda row: row["candidate"].update(agent="f" * 40), "candidate agent"),
        (lambda row: row["installed"].update(qualification_marker="changed"), "visible marker"),
        (lambda row: row["semantic_data"].update(after_sha256="b" * 64), "semantic data"),
    ):
        bad = deepcopy(receipt)
        mutate(bad)
        assert any(message in error for error in release2.receipt_errors(bad)), message
