"""The evidence gate rejects incomplete or dishonest qualification results."""
import copy
import importlib.util
import json
from pathlib import Path

spec = importlib.util.spec_from_file_location('journey_evidence', Path(__file__).parents[1] / 'scripts/check_journey_evidence.py')
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)


HOSTED = {"route": "hosted-button", "request": {"method": "POST", "path": "/api/runtime/v1/update",
          "origin": "https://astrid.haus", "content_length": 0, "authorization": False, "cookie": False,
          "status": 202, "operation_id": "a" * 32},
          "served": {"/install-manifest.json": 1, "/install": 1, "/install.ps1": 0},
          "final": {"state": "terminal", "outcome": "updated", "target_release": 2}, "intercept_reverted": True}
# The fresh Linux row's update is the hosted App build's own Update now, clicked in a browser.
BUTTON = {**HOSTED, "driver": "app-button",
          "request": {**HOSTED["request"], "headers": ["accept", "content-length", "host", "origin", "user-agent"]},
          "page": {"entry": "/tools/video-editor?settings=updates", "offered": True, "clicked": True,
                   "progress_shown": True, "build_sha256": "b" * 64, "served_manifest": 1}}


def handoff_upgrade_fields():
    """The r0-handoff-then-update receipt fields the upgrade rows must carry."""
    return {
        "release_transition": {"from": 9, "to": 12, "start_state": "r0-handoff-complete",
                               "source": "candidate-install-update"},
        "release0_fixture": {"fixture_sums_sha256": gate.RELEASE0_SUMS_SHA256,
                             "bootstrap_sha256": gate.RELEASE0_BOOTSTRAP_SHA256,
                             "manifest_sha256": "4" * 64, "provenance_sha256": "5" * 64,
                             "sources": {"agent": "3204bb00135bec8ee2d2d3fb21eed5abfceca649",
                                         "runtime": "6d53a19070faf63fc15ae1f81417722edc2c8a5f"}},
        "handoff_source": {
            "app_commit": gate.RELEASE9_APP_COMMIT, "manifest_sha256": "f" * 64,
            "bootstrap_sha256": {"public/install": "1" * 64, "public/install.sh": "2" * 64,
                                 "public/install.ps1": "3" * 64},
            "identity": gate.RELEASE9_IDENTITY,
        },
        "handoff": {"phase": "complete", "release": 9, "r0_data_root": "/run/data",
                    "r1_data_root": "/run/data-r1"},
        "update": {"command": "install --update", "exit_code": 0},
        "health_transition": {"candidate_live_release": 12},
    }


def release9_fields():
    """The release9-then-install row's own fields: the plain install from the pinned Release 9."""
    bootstrap = {"public/install": "1" * 64, "public/install.sh": "2" * 64, "public/install.ps1": "3" * 64}
    return {
        "release9_source": {"app_commit": gate.RELEASE9_APP_COMMIT, "manifest_sha256": "f" * 64,
                            "bootstrap_sha256": bootstrap, "identity": gate.RELEASE9_IDENTITY},
        "release_transition": {"from": 9, "to": 12, "source": "pinned-release9-plain-install"},
        "health_transition": {"release9_runtime_instance_id": "old", "candidate_runtime_instance_id": "new"},
        "service_transition": {"release9": {"manager": "native"}, "candidate": {"manager": "native"}},
    }


def release11_fields():
    """The release11-then-update row's own evidence for its four assertions."""
    bootstrap = {"public/install": "1" * 64, "public/install.sh": "2" * 64, "public/install.ps1": "3" * 64}
    return {
        "release11_source": {"app_commit": gate.RELEASE11_APP_COMMIT, "manifest_sha256": "e" * 64,
                             "bootstrap_sha256": bootstrap, "identity": gate.RELEASE11_IDENTITY},
        "release_transition": {"from": 11, "to": 12, "source": "candidate-install-update"},
        "health_transition": {"candidate_live_release": 12},
        "update": {"command": "install --update", "exit_code": 0,
                   "receipt": {"kind": "updated", "exit_code": 0, "start": 11, "still": 12, "target": 12}},
        "live_runtimes": [{"data_root": "/run/data", "pid": 1, "runtime_instance_id": "new"}],
    }


def autostart_fields():
    """The fresh-install-autostart-off row's own evidence: the selection, the status, the refused POST and the capture."""
    return {
        "selection": {"file": "astrid-install.json", "choices_autostart": False, "sha256": "a" * 64},
        "update_status": {"method": "GET", "path": "/update/status", "status": 200, "reason": "autostart-off"},
        "post_update": {"method": "POST", "path": "/update", "status": 409, "reason": "autostart-off",
                        "refused": True, "unchanged": True},
        "capture": {"path": "autostart-off-card.png", "sha256": "c" * 64, "device_scale_factor": 2,
                    "surface": "autostart-off card"},
        # Autostart off runs one birth-verified detached Runtime and no sign-in registration.
        "service_manager": {"manager": "detached", "autostart": False, "foreground_fallback": False,
                            "detached": {"pid": 4242, "birth_verified": True},
                            "sign_in_registration": {"manager": "systemd-user", "load_state": "not-found"}},
    }


def release1_receipts(tmp_path):
    candidate = {key: letter * 40 for key, letter in zip(("app", "agent", "runtime", "pinokio"), "abcd")}
    for contract in gate.RELEASE1_MATRIX:
        runner_os, image_os = gate.RUNNER_PROVENANCE[contract["runner"]]
        service = {"required": True, "outcome": "pass", "manager": "native", "version": "1",
                   "foreground_fallback": False}
        row = {"receipt_kind": gate.release1_receipt_kind(contract), "job_id": contract["job_id"],
               "mode": contract["mode"], "scenario": contract.get("leg", "fresh-install"),
               "status": "pass", "runner_image": contract["runner"],
               "shell": contract["shell"], "source_override_test_mode": False, "final_cleanup": "pass",
               "os_build": runner_os + "-test", "runner_provenance": {"RUNNER_OS": runner_os,
                                                                            "ImageOS": image_os,
                                                                            "ImageVersion": "test"},
               "candidate_checkouts": {name: {"sha": sha} for name, sha in candidate.items()},
               "run_roots": {"program": "/run/program", "data": "/run/data"},
               "sources": {name: {"sha": candidate[name]} for name in ("app", "agent", "runtime")},
               "installed_tuple": {name: candidate[name] for name in ("app", "agent", "runtime")},
               "import_origins": {"runtime": "/owned/runtime"},
               "service_manager_prerequisite": service,
               "checks": {"semantic_data": "pass", "release": "pass", "service": "pass", "health": "pass",
                          "task_settled": "pass", "unrelated_sentinel": "pass",
                          "handed_off_start": "pass", "update_exit": "pass"}}
        if contract["mode"] == "direct":
            row["service_manager"] = {"manager": "native", "foreground_fallback": False}
            if contract.get("leg") == "release9-then-install":
                row.update(release9_fields())
            elif contract.get("leg") == "release11-then-update":
                row.update(release11_fields())
                row["checks"].update({name: "pass" for name in gate.RELEASE11_CHECKS})
            elif contract.get("leg") == "fresh-install-autostart-off":
                row.update(autostart_fields())
                row["checks"].update({name: "pass" for name in gate.AUTOSTART_CHECKS})
            else:
                row.update(handoff_upgrade_fields())
            row["semantic_data"] = {"identical": True, "before_sha256": "b" * 64,
                                    "after_sha256": "b" * 64}
        else:
            row["service_manager_prerequisite"] = {"required": False, "outcome": "explicit-owner",
                                                    "managed_by": "pinokio", "foreground_fallback": False}
        if contract["leg"] == "fresh-install":
            row["checks"]["workspace_content"] = "pass"
        if contract["job_id"] in {"direct-linux-fresh", "direct-macos-fresh"}:
            name = "astrid.service" if contract["job_id"] == "direct-linux-fresh" else "haus.astrid.runtime.plist"
            row["update"] = {"status": "pass", "release_from": 1, "release_to": 2,
            "hosted": BUTTON if contract["job_id"] == "direct-linux-fresh" else HOSTED,
                             "old_version": "old", "new_version": "new", "manifest_sha256": "c" * 64,
                             "service": {"service_path": "/service/" + name, "foreground_fallback": False},
                             "service_names": [name],
                             "checks": {name: "pass" for name in ("new_version", "old_version_retained", "same_service", "no_boundary_handoff", "health", "workspace_content", "unrelated_sentinel", "verify_update", "task_settled")}}
        if contract["job_id"] == "direct-windows":
            row["update"] = {
                "outcome": "updated", "from_release": 1, "to_release": 2, "hosted": HOSTED,
                "marker": "M-RT UPDATE TEST STAND-IN", "manifest_sha256": "d" * 64,
                "old_version": "old", "current_version": "new", "exit_code": 0,
                "refusal": None, "task_id": "task-1", "task_labels": [{"name": "Astrid Runtime", "path": "\\"}], "service_names": [],
                "checks": {key: "pass" for key in ("running_before", "owned_task", "healthy", "same_labels",
                           "no_r0_handoff", "workspace", "sentinel", "prior_version", "verify_update", "task_settled")},
            }
            row["windows_upgrade_refusal"] = {
                "receipt_kind": "windows-upgrade-refusal", "job_id": "direct-windows", "scenario": "windows-upgrade-refusal", "mode": "direct", "status": "pass", "final_cleanup": "pass", "source_override_test_mode": False,
                "runner_image": row["runner_image"], "runner_provenance": row["runner_provenance"], "os_build": row["os_build"], "shell": row["shell"], "candidate_checkouts": row["candidate_checkouts"],
                "release0_fixture": {"fixture_sums_sha256": gate.RELEASE0_SUMS_SHA256, "manifest_sha256": "f" * 64, "provenance_sha256": "a" * 64, "bootstrap_sha256": gate.RELEASE0_BOOTSTRAP_SHA256, "sources": {"agent": "3204bb00135bec8ee2d2d3fb21eed5abfceca649", "runtime": "6d53a19070faf63fc15ae1f81417722edc2c8a5f"}}, "data_marker": {"sha256": "b" * 64},
                "scheduled_task": {"before_sha256": "c" * 64, "after_sha256": "c" * 64},
                "refusal": {"exit_code": 1, "message": "Upgrading from an earlier Windows version isn't supported yet. Your data was kept; reinstall to get the new version."},
                "checks": {key: "pass" for key in ("refusal", "data_marker", "r0_selected", "scheduled_task")},
            }
        receipt = tmp_path / contract["job_id"] / "journey-result.json"
        receipt.parent.mkdir(exist_ok=True)
        receipt.write_text(json.dumps(row), encoding="utf-8")
    # The candidate manifest's release, which the aggregate reads from the manifest in main().
    return {**candidate, "release": 12}


def evidence(tmp_path):
    candidate = {k: ch * 40 for k, ch in zip(('app', 'agent', 'runtime', 'pinokio'), 'abcd')}
    manifest = {'agent': {'sha': candidate['agent']}, 'runtime': {'sha': candidate['runtime'], 'schema_digest': 'schema'}, 'tools': {'uv': {'version': '1'}}}
    row = {'scenario': 'fresh-install', 'status': 'pass', 'os_build': 'Linux-6.8.0', 'shell': 'sh', 'final_cleanup': 'pass',
           'runner_image': 'ubuntu-24.04', 'runner_provenance': {'RUNNER_OS': 'Linux', 'ImageOS': 'ubuntu24', 'ImageVersion': '20261028.1'},
           'source_override_test_mode': False,
           'sources': {'app': {'sha': candidate['app']}, 'agent': manifest['agent'], 'runtime': manifest['runtime']},
           'installed_tuple': {**{k: candidate[k] for k in ('app', 'agent', 'runtime')}, 'schema_digest': 'schema', 'tools': {'uv': '1'}},
           'import_origins': {'runtime': '/owned/runtime'},
           'checks': {k: 'pass' for k in ('task_settled', 'workspace_content', 'unrelated_sentinel')}}
    def write(value):
        (tmp_path / 'journey-result.json').write_text(json.dumps(value))
    return candidate, manifest, row, write


def test_four_required_negative_controls(tmp_path):
    candidate, manifest, row, write = evidence(tmp_path)
    assert gate.evaluate(candidate, manifest, tmp_path, ['fresh-install'], 'ubuntu-24.04', 'sh')
    write(row)
    assert not gate.evaluate(candidate, manifest, tmp_path, ['fresh-install'], 'ubuntu-24.04', 'sh')
    for mutate in ('skipped', 'wrong-sha', 'unlabelled'):
        bad = copy.deepcopy(row)
        if mutate == 'skipped':
            bad['status'] = 'skipped'
        elif mutate == 'wrong-sha':
            bad['installed_tuple']['runtime'] = 'e' * 40
        else:
            del bad['source_override_test_mode']
        write(bad)
        assert gate.evaluate(candidate, manifest, tmp_path, ['fresh-install'], 'ubuntu-24.04', 'sh'), mutate
    row['source_override_test_mode'] = True
    write(row)
    assert gate.evaluate(candidate, manifest, tmp_path, ['fresh-install'], 'ubuntu-24.04', 'sh')
    assert not gate.evaluate(candidate, manifest, tmp_path, ['fresh-install'], 'ubuntu-24.04', 'sh', allow_override=True)


def test_runner_shell_and_cleanup_must_match(tmp_path):
    candidate, manifest, row, write = evidence(tmp_path)
    write(row)
    assert gate.evaluate(candidate, manifest, tmp_path, ['fresh-install'], 'windows-2022', 'powershell.exe')
    assert gate.evaluate(candidate, manifest, tmp_path, ['fresh-install'], 'macos-15', 'sh')
    assert gate.evaluate(candidate, manifest, tmp_path, ['fresh-install'], 'ubuntu-24.04', 'powershell.exe')
    row['final_cleanup'] = 'retained-for-next-leg'
    write(row)
    assert gate.evaluate(candidate, manifest, tmp_path, ['fresh-install'], 'ubuntu-24.04', 'sh')
    del row['final_cleanup']
    write(row)
    assert gate.evaluate(candidate, manifest, tmp_path, ['fresh-install'], 'ubuntu-24.04', 'sh')


def test_runner_provenance_required(tmp_path):
    candidate, manifest, row, write = evidence(tmp_path)
    write(row)
    assert not gate.evaluate(candidate, manifest, tmp_path, ['fresh-install'], 'ubuntu-24.04', 'sh')
    assert gate.evaluate(candidate, manifest, tmp_path, ['fresh-install'], 'ubuntu-22.04', 'sh')
    bad = copy.deepcopy(row)
    bad['runner_image'] = 'ubuntu-22.04'
    write(bad)
    assert any('ImageOS' in e for e in gate.evaluate(candidate, manifest, tmp_path, ['fresh-install'], 'ubuntu-22.04', 'sh'))
    for field in ('RUNNER_OS', 'ImageOS', 'ImageVersion'):
        bad = copy.deepcopy(row)
        del bad['runner_provenance'][field]
        write(bad)
        assert gate.evaluate(candidate, manifest, tmp_path, ['fresh-install'], 'ubuntu-24.04', 'sh'), field
    bad = copy.deepcopy(row)
    del bad['runner_image']
    write(bad)
    assert gate.evaluate(candidate, manifest, tmp_path, ['fresh-install'], 'ubuntu-24.04', 'sh')


def test_hosted_image_mappings(tmp_path):
    candidate, manifest, row, write = evidence(tmp_path)
    for runner, runner_os, image_os, build, shell in (
        ('ubuntu-24.04', 'Linux', 'ubuntu24', 'Linux-6.8', 'sh'),
        ('ubuntu-22.04', 'Linux', 'ubuntu22', 'Linux-6.8', 'sh'),
        ('windows-2022', 'Windows', 'win22', 'Windows-Server', 'powershell.exe'),
        ('macos-15', 'macOS', 'macos15', 'macOS-15.7-arm64', 'sh'),
    ):
        item = copy.deepcopy(row)
        item.update(runner_image=runner, os_build=build, shell=shell)
        item['runner_provenance'].update(RUNNER_OS=runner_os, ImageOS=image_os)
        write(item)
        assert not gate.evaluate(candidate, manifest, tmp_path, ['fresh-install'], runner, shell)
        item['runner_provenance']['ImageOS'] = 'unexpected'
        write(item)
        assert any('ImageOS' in e for e in gate.evaluate(candidate, manifest, tmp_path, ['fresh-install'], runner, shell))


def test_macos15_receipt_key_rejects_the_retired_macos14_label(tmp_path):
    candidate, manifest, row, write = evidence(tmp_path)
    row.update(runner_image='macos-15', os_build='macOS-15.7-arm64', shell='sh')
    row['runner_provenance'].update(RUNNER_OS='macOS', ImageOS='macos15')
    write(row)
    assert not gate.evaluate(candidate, manifest, tmp_path, ['fresh-install'], 'macos-15', 'sh')

    row['runner_image'] = 'macos-14'
    write(row)
    assert any('runner image label' in error
               for error in gate.evaluate(candidate, manifest, tmp_path, ['fresh-install'], 'macos-15', 'sh'))


def test_detached_runtime_is_admitted_only_on_the_autostart_off_rows(tmp_path):
    candidate = release1_receipts(tmp_path)
    assert not gate.evaluate_release1_matrix(candidate, tmp_path)

    def rewrite(job_id, change):
        path = tmp_path / job_id / "journey-result.json"
        data = json.loads(path.read_text())
        change(data)
        path.write_text(json.dumps(data), encoding="utf-8")

    # An autostart-off row still needs its detached Runtime assertion, not a generic manager record.
    rewrite("direct-linux-autostart-off", lambda data: data.update(
        service_manager={"manager": "native", "foreground_fallback": False}))
    assert any("direct-linux-autostart-off: autostart-off detached Runtime assertion is absent" in error
               for error in gate.evaluate_release1_matrix(candidate, tmp_path))
    candidate = release1_receipts(tmp_path)
    rewrite("direct-macos-autostart-off",
            lambda data: data["service_manager"]["detached"].update(birth_verified=False))
    assert any("direct-macos-autostart-off: autostart-off detached Runtime assertion is absent" in error
               for error in gate.evaluate_release1_matrix(candidate, tmp_path))
    # Every other direct row keeps the foreground ban.
    candidate = release1_receipts(tmp_path)
    rewrite("direct-linux-fresh", lambda data: data.update(service_manager={
        "manager": "detached", "autostart": False, "foreground_fallback": False,
        "detached": {"birth_verified": True}, "sign_in_registration": {}}))
    assert any("direct-linux-fresh: a detached Runtime is admitted only on the autostart-off leg" in error
               for error in gate.evaluate_release1_matrix(candidate, tmp_path))


def test_release1_aggregate_rejects_missing_skipped_and_ambiguous_receipts(tmp_path):
    candidate = release1_receipts(tmp_path)
    assert not gate.evaluate_release1_matrix(candidate, tmp_path)
    (tmp_path / "direct-linux" / "journey-result.json").unlink()
    assert any("direct-linux: missing receipt" in error
               for error in gate.evaluate_release1_matrix(candidate, tmp_path))
    candidate = release1_receipts(tmp_path)
    skipped = tmp_path / "direct-linux-fresh" / "journey-result.json"
    data = json.loads(skipped.read_text())
    data["status"] = "skipped"
    skipped.write_text(json.dumps(data), encoding="utf-8")
    assert any("direct-linux-fresh: status 'skipped'" in error
               for error in gate.evaluate_release1_matrix(candidate, tmp_path))
    candidate = release1_receipts(tmp_path)
    direct = tmp_path / "direct-macos" / "journey-result.json"
    data = json.loads(direct.read_text())
    data["service_manager"]["foreground_fallback"] = True
    direct.write_text(json.dumps(data), encoding="utf-8")
    assert any("direct-macos: direct service ownership" in error
               for error in gate.evaluate_release1_matrix(candidate, tmp_path))
    candidate = release1_receipts(tmp_path)
    direct = tmp_path / "direct-windows" / "journey-result.json"
    data = json.loads(direct.read_text())
    data["windows_upgrade_refusal"]["scheduled_task"]["after_sha256"] = "d" * 64
    direct.write_text(json.dumps(data), encoding="utf-8")
    assert any("direct-windows: refusal scheduled task definition" in error
               for error in gate.evaluate_release1_matrix(candidate, tmp_path))
    candidate = release1_receipts(tmp_path)
    direct = tmp_path / "direct-linux" / "journey-result.json"
    data = json.loads(direct.read_text())
    data["checks"]["task_settled"] = "not-run"
    direct.write_text(json.dumps(data), encoding="utf-8")
    assert any("direct-linux: task_settled assertion did not pass" in error
               for error in gate.evaluate_release1_matrix(candidate, tmp_path))
    candidate = release1_receipts(tmp_path)
    direct = tmp_path / "direct-macos" / "journey-result.json"
    data = json.loads(direct.read_text())
    data["installed_tuple"]["runtime"] = "e" * 40
    direct.write_text(json.dumps(data), encoding="utf-8")
    assert any("direct-macos: runtime installed/source identity differs" in error
               for error in gate.evaluate_release1_matrix(candidate, tmp_path))
    candidate = release1_receipts(tmp_path)
    direct = tmp_path / "direct-linux" / "journey-result.json"
    data = json.loads(direct.read_text())
    data["handoff_source"]["app_commit"] = "e" * 40
    direct.write_text(json.dumps(data), encoding="utf-8")
    assert any("direct-linux: pinned release-9 handoff installer provenance is absent or wrong" in error
               for error in gate.evaluate_release1_matrix(candidate, tmp_path))


def test_release1_gate_requires_an_ordinary_final_uninstall_or_the_handoff_refusal(tmp_path):
    candidate = release1_receipts(tmp_path)
    assert not gate.evaluate_release1_matrix(candidate, tmp_path)
    path = tmp_path / "direct-macos-fresh" / "journey-result.json"
    original = json.loads(path.read_text())
    path.write_text(json.dumps(dict(original, final_cleanup="retained-after-upgrade-refusal")))
    assert any("direct-macos-fresh: final cleanup is not pass" in error
               for error in gate.evaluate_release1_matrix(candidate, tmp_path))
    path.write_text(json.dumps(original))
    refusal = {"exit_code": 1, "r0_data_root": "/run/data", "r1_data_root": "/run/data-r1",
               "message": ("Astrid was upgraded from an earlier version; automatic uninstall after an upgrade "
                           "isn't supported yet. Both data folders were kept.")}
    for job_id in ("direct-linux", "direct-macos"):
        path = tmp_path / job_id / "journey-result.json"
        original = json.loads(path.read_text())
        retained = dict(original, final_cleanup="retained-after-upgrade-refusal")
        path.write_text(json.dumps(retained))
        assert any(f"{job_id}: required preserved-state uninstall refusal is absent or invalid" in error
                   for error in gate.evaluate_release1_matrix(candidate, tmp_path))
        path.write_text(json.dumps(dict(retained, upgrade_uninstall_refusal=refusal)))
        assert not gate.evaluate_release1_matrix(candidate, tmp_path)
        path.write_text(json.dumps(dict(original, final_cleanup="fail")))
        assert any(f"{job_id}: final cleanup is not pass" in error
                   for error in gate.evaluate_release1_matrix(candidate, tmp_path))
        path.write_text(json.dumps(original))


def test_handoff_rows_require_the_completed_handoff_and_a_real_update(tmp_path):
    candidate = release1_receipts(tmp_path)
    path = tmp_path / "direct-linux" / "journey-result.json"
    original = json.loads(path.read_text())
    for change, message in (
        ({"update": {"command": "install --update", "exit_code": 3}}, "candidate install --update did not exit 0"),
        ({"handoff": dict(original["handoff"], phase="starting")}, "completed R0 handoff start state"),
        ({"handoff": dict(original["handoff"], r1_data_root="/run/data")}, "completed R0 handoff start state"),
        ({"release0_fixture": None}, "frozen release-0 fixture provenance"),
        ({"health_transition": {"candidate_live_release": 9}}, "the live Runtime does not report the candidate release"),
        ({"release_transition": dict(original["release_transition"], start_state="release9")},
         "handed-off release-9 to candidate transition"),
    ):
        path.write_text(json.dumps(dict(original, **change)))
        assert any(f"direct-linux: {message}" in error
                   for error in gate.evaluate_release1_matrix(candidate, tmp_path)), message
    path.write_text(json.dumps(original))
    assert not gate.evaluate_release1_matrix(candidate, tmp_path)


def test_release1_macos_rows_require_macos15_and_hosted_provenance(tmp_path):
    candidate = release1_receipts(tmp_path)
    assert not gate.evaluate_release1_matrix(candidate, tmp_path)
    for job_id in ('direct-macos', 'direct-macos-fresh'):
        receipt = tmp_path / job_id / 'journey-result.json'
        original = json.loads(receipt.read_text())
        for field, value, message in (
            ('runner_image', 'macos-14', 'runner does not match'),
            ('RUNNER_OS', 'Linux', 'hosted runner provenance'),
            ('ImageOS', 'macos14', 'hosted runner provenance'),
            ('ImageVersion', '', 'hosted runner provenance'),
            ('os_build', 'Linux-6.8', 'OS build does not match'),
        ):
            bad = copy.deepcopy(original)
            if field in ('RUNNER_OS', 'ImageOS', 'ImageVersion'):
                bad['runner_provenance'][field] = value
            else:
                bad[field] = value
            receipt.write_text(json.dumps(bad), encoding='utf-8')
            assert any(job_id in error and message in error
                       for error in gate.evaluate_release1_matrix(candidate, tmp_path)), (job_id, field)
        receipt.write_text(json.dumps(original), encoding='utf-8')


def test_workflow_declares_the_exact_release1_matrix_and_aggregate_gate():
    workflow = (Path(__file__).parents[1] / ".github" / "workflows" / "journey.yml").read_text()
    assert "matrix = [dict(row) for row in journey_matrix.RELEASE1_MATRIX]" in workflow
    contract_rows = {(row["job_id"], row["mode"], row["os"], row["leg"]) for row in gate.RELEASE1_MATRIX}
    assert len(gate.RELEASE1_MATRIX) == len({row["job_id"] for row in gate.RELEASE1_MATRIX}) == 11
    assert contract_rows == {
        ("direct-linux", "direct", "linux", "r0-handoff-then-update"),
        ("direct-macos", "direct", "macos", "r0-handoff-then-update"),
        ("direct-windows", "direct", "windows", "fresh-install"),
        ("direct-linux-fresh", "direct", "linux", "fresh-install"),
        ("direct-macos-fresh", "direct", "macos", "fresh-install"),
        ("direct-linux-r9-install", "direct", "linux", "release9-then-install"),
        ("direct-macos-r9-install", "direct", "macos", "release9-then-install"),
        ("direct-linux-r11-update", "direct", "linux", "release11-then-update"),
        ("direct-macos-r11-update", "direct", "macos", "release11-then-update"),
        ("direct-linux-autostart-off", "direct", "linux", "fresh-install-autostart-off"),
        ("direct-macos-autostart-off", "direct", "macos", "fresh-install-autostart-off"),
    }
    assert not any(row["mode"] != "direct" for row in gate.RELEASE1_MATRIX)
    assert {row["mode"] for row in gate.RELEASE1_1_DEFERRED_PINOKIO_MATRIX} == {"pinokio-backend", "pinokio-fully-local"}
    assert "--aggregate-release1" in workflow
    assert "journey-receipt-${{ matrix.job_id }}" in workflow
    assert 'run_root="$RUNNER_TEMP/astrid-r1-$JOB_ID"' in workflow
    assert "if: matrix.mode == 'direct' && matrix.os == 'linux'" in workflow
    assert "candidate-agent" in workflow and "pinokio" in workflow
    assert "--release9-app-checkout" in workflow and "--allow-release0-to-release1" in workflow
    assert "--release0-fixture" in workflow


def test_r1_1_pinokio_matrix_has_exact_three_rows_and_own_checker(tmp_path):
    from scripts import journey_matrix

    rows = journey_matrix.RELEASE1_1_PINOKIO_MATRIX
    assert [(row["job_id"], row["mode"], row["os"]) for row in rows] == [
        ("pinokio-linux", "pinokio-backend", "linux"),
        ("pinokio-macos", "pinokio-backend", "macos"),
        ("pinokio-windows", "pinokio-backend", "windows"),
    ]
    assert len({row["job_id"] for row in rows}) == 3
    assert all(row["leg"] == "fresh-install" for row in rows)
    assert journey_matrix.validate_job("pinokio-linux", "pinokio-backend")["os"] == "linux"
    assert {error.split(":", 1)[0] for error in gate.evaluate_release1_1_pinokio_matrix(None, tmp_path)} == {
        "pinokio-linux", "pinokio-macos", "pinokio-windows",
    }
    assert {error.split(":", 1)[0] for error in gate.evaluate_release1_matrix({}, tmp_path)} == {
        row["job_id"] for row in gate.RELEASE1_MATRIX
    }


PINOKIO_STEPS = ("install", "start_task", "stop_start", "busy_stop_data", "update_ready_task", "uninstall_data")
PINOKIO_OS = {"pinokio-linux": "linux", "pinokio-macos": "macos", "pinokio-windows": "windows"}


def _pinokio_row(job_id, sha="a" * 40):
    steps = [{"name": name, "status": "pass", "detail": "ok"} for name in PINOKIO_STEPS]
    return {"schema": "pinokio-row-v1", "job_id": job_id, "os": PINOKIO_OS[job_id],
            "pinokio_sha": sha, "steps": steps}


def _write_pinokio_rows(root, rows):
    root.mkdir(parents=True, exist_ok=True)
    for index, row in enumerate(rows):
        path = root / f"row-{index}" / "journey-result.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(row), encoding="utf-8")


def _all_pinokio_rows():
    return [_pinokio_row(job_id) for job_id in ("pinokio-linux", "pinokio-macos", "pinokio-windows")]


def test_pinokio_row_receipts_accept_exact_complete_green_set(tmp_path):
    _write_pinokio_rows(tmp_path, _all_pinokio_rows())
    assert gate.evaluate_release1_1_pinokio_matrix({"pinokio": "a" * 40}, tmp_path) == []


def test_pinokio_row_receipts_accept_only_the_selected_job(tmp_path):
    _write_pinokio_rows(tmp_path / "one", [_pinokio_row("pinokio-linux")])
    assert gate.evaluate_release1_1_pinokio_matrix(None, tmp_path / "one", ["pinokio-linux"]) == []
    _write_pinokio_rows(tmp_path / "extra", _all_pinokio_rows()[:2])
    errors = gate.evaluate_release1_1_pinokio_matrix(None, tmp_path / "extra", ["pinokio-linux"])
    assert any(error.endswith("Pinokio job pinokio-macos was not selected for this run") for error in errors)
    assert gate.evaluate_release1_1_pinokio_matrix(None, tmp_path / "one", ["pinokio-freebsd"]) != []


def test_pinokio_row_receipts_reject_missing_row_step_and_failed_step(tmp_path):
    _write_pinokio_rows(tmp_path / "missing-row", _all_pinokio_rows()[:2])
    errors = gate.evaluate_release1_1_pinokio_matrix(None, tmp_path / "missing-row")
    assert any("pinokio-windows: missing" in error for error in errors)

    rows = _all_pinokio_rows()
    rows[0]["steps"] = rows[0]["steps"][1:]
    rows[1]["steps"][1] = {"name": "start_task", "status": "fail", "detail": "task did not start"}
    _write_pinokio_rows(tmp_path / "bad-steps", rows)
    errors = gate.evaluate_release1_1_pinokio_matrix(None, tmp_path / "bad-steps")
    assert any("step names are missing, extra or duplicated" in error for error in errors)
    assert any("start_task status is not pass" in error for error in errors)


def test_pinokio_row_receipts_reject_duplicate_and_mismatched_jobs(tmp_path):
    rows = _all_pinokio_rows()
    _write_pinokio_rows(tmp_path / "duplicate", rows + [copy.deepcopy(rows[0])])
    errors = gate.evaluate_release1_1_pinokio_matrix(None, tmp_path / "duplicate")
    assert any("pinokio-linux: duplicate" in error for error in errors)

    mismatched = copy.deepcopy(rows)
    mismatched[0]["os"] = "windows"
    mismatched[1]["job_id"] = "pinokio-linux"
    mismatched[2]["pinokio_sha"] = "b" * 40
    _write_pinokio_rows(tmp_path / "mismatch", mismatched)
    errors = gate.evaluate_release1_1_pinokio_matrix({"pinokio": "a" * 40}, tmp_path / "mismatch")
    assert any("job id and OS do not match" in error for error in errors)
    assert any("duplicate Pinokio receipt" in error for error in errors)
    assert any("pinokio SHA differs from candidate" in error for error in errors)


def test_pinokio_row_receipts_reject_malformed_schema_and_fields(tmp_path):
    rows = _all_pinokio_rows()
    rows[0]["schema"] = "pinokio-row-v2"
    rows[1]["steps"][0]["error"] = "unexpected"
    rows[2]["extra"] = True
    _write_pinokio_rows(tmp_path, rows)
    errors = gate.evaluate_release1_1_pinokio_matrix(None, tmp_path)
    assert any("schema is not" in error for error in errors)
    assert any("step is not a name/status/detail object" in error for error in errors)
    assert any("receipt fields do not match" in error for error in errors)


def test_pinokio_dispatch_selects_r1_1_pinokio_rows_and_skips_old_launcher_inputs():
    workflow = (Path(__file__).parents[1] / ".github" / "workflows" / "journey.yml").read_text()
    for row in ("pinokio-linux", "pinokio-macos", "pinokio-windows", "pinokio"):
        assert f"          - {row}\n" in workflow
    assert 'pinokio_only = journey_row == "pinokio" or journey_row in pinokio_job_ids' in workflow
    assert "matrix = [dict(row) for row in journey_matrix.RELEASE1_1_PINOKIO_MATRIX]" in workflow
    assert "journey_row not in {\"direct\", \"pinokio\"}" in workflow
    assert "if not pinokio_only:" in workflow
    assert "pinokio rows run only the R1.1 Pinokio journey rows" in workflow
    assert "--aggregate-release1-1-pinokio --pinokio-job-ids" in workflow
    assert "outputs.pinokio_only == 'true'" in workflow
    assert "required: true" not in workflow.split("workflow_dispatch:")[0].split("old_launcher_ref:")[1].split("\n      ")[1]


def test_pinokio_row_steps_run_driver_and_keep_the_evidence_boundary_path():
    workflow = (Path(__file__).parents[1] / ".github" / "workflows" / "journey.yml").read_text()
    assert "if: ${{ !startsWith(matrix.job_id, 'pinokio-') }}" in workflow
    assert "if: ${{ startsWith(matrix.job_id, 'pinokio-') }}" in workflow
    assert "scripts/pinokio_row_driver.py" in workflow or "agent/scripts/pinokio_row_driver.py" in workflow
    assert "evidence-upload/after-cleanup/journey-result.json" in workflow


def test_release1_aggregate_accepts_full_staged_receipts_with_capture_status(tmp_path):
    from scripts import ci_journey as journey
    import shutil

    source = tmp_path / "source"
    source.mkdir()
    candidate = release1_receipts(source)
    staged_root = tmp_path / "staged"
    aggregate_root = tmp_path / "artifacts"
    aggregate_root.mkdir()
    for contract in gate.RELEASE1_MATRIX:
        job = contract["job_id"]
        evidence_dir = staged_root / job / "evidence" / "fresh"
        evidence_dir.mkdir(parents=True)
        original = (source / job / "journey-result.json").read_bytes()
        (evidence_dir / "journey-result.json").write_bytes(original)
        journey.SDK_CAPTURE_RECEIPTS[evidence_dir.absolute()] = {
            "sdk-capture-status.json": {
                "schema": "astrid.sdk-capture-status.v1", "exit_condition": "success",
                "response_size": "within_limit", "json_object": True,
                "ok": True, "task_refs": "one",
            }
        }
        journey.retain_named_evidence(evidence_dir, "after-cleanup")
        stage = staged_root / job / "evidence-upload" / "after-cleanup"
        assert (stage / "journey-result.json").read_bytes() == original
        assert (stage / "sdk-capture-status.json").is_file()
        shutil.copytree(stage, aggregate_root / job)
    assert not gate.evaluate_release1_matrix(candidate, aggregate_root)
    direct = aggregate_root / "direct-linux" / "journey-result.json"
    failed = json.loads(direct.read_text())
    failed["checks"]["task_settled"] = "not-run"
    direct.write_text(json.dumps(failed))
    assert any("direct-linux: task_settled assertion did not pass" in error
               for error in gate.evaluate_release1_matrix(candidate, aggregate_root))


def test_direct_receipts_require_observed_checks_without_pid_count(tmp_path):
    candidate = release1_receipts(tmp_path)
    path = tmp_path / "direct-linux" / "journey-result.json"
    row = json.loads(path.read_text())
    assert "one_runtime" not in row["checks"]
    assert not gate.evaluate_release1_matrix(candidate, tmp_path)
    for check in ("handed_off_start", "update_exit", "release", "service", "health", "semantic_data",
                  "task_settled", "unrelated_sentinel"):
        bad = copy.deepcopy(row)
        bad["checks"][check] = "not-run"
        path.write_text(json.dumps(bad))
        assert any(f"direct-linux: {check} assertion did not pass" in error
                   for error in gate.evaluate_release1_matrix(candidate, tmp_path))


def test_release1_aggregate_requires_exactly_the_five_direct_rows(tmp_path):
    candidate = release1_receipts(tmp_path)
    assert {path.parent.name for path in tmp_path.rglob("journey-result.json")} == set(gate.RELEASE1_JOB_IDS)
    assert not gate.evaluate_release1_matrix(candidate, tmp_path)
    for job_id in gate.RELEASE1_JOB_IDS:
        receipt = tmp_path / job_id / "journey-result.json"
        original = receipt.read_bytes()
        receipt.unlink()
        assert f"{job_id}: missing receipt" in gate.evaluate_release1_matrix(candidate, tmp_path)
        receipt.write_bytes(original)
    duplicate = tmp_path / "copy" / "journey-result.json"
    duplicate.parent.mkdir()
    duplicate.write_bytes((tmp_path / "direct-linux-fresh" / "journey-result.json").read_bytes())
    assert any("duplicate receipt for direct-linux-fresh" in error
               for error in gate.evaluate_release1_matrix(candidate, tmp_path))
    duplicate.unlink()
    deferred = json.loads((tmp_path / "direct-linux-fresh" / "journey-result.json").read_text())
    deferred.update(job_id="pinokio-backend-linux", mode="pinokio-backend")
    duplicate.write_text(json.dumps(deferred), encoding="utf-8")
    assert any("unknown or absent Release-1 job id" in error
               for error in gate.evaluate_release1_matrix(candidate, tmp_path))


def test_release1_fresh_rows_use_fresh_expectations_and_upgrade_rows_stay_strict(tmp_path):
    candidate = release1_receipts(tmp_path)
    for job_id in ("direct-linux-fresh", "direct-macos-fresh"):
        path = tmp_path / job_id / "journey-result.json"
        row = json.loads(path.read_text())
        for key in ("release_transition", "handoff_source", "semantic_data"):
            row.pop(key)
        row["checks"] = {name: "pass" for name in ("task_settled", "workspace_content", "unrelated_sentinel")}
        path.write_text(json.dumps(row), encoding="utf-8")
        assert not gate.evaluate_release1_matrix(candidate, tmp_path)
        for check in ("task_settled", "workspace_content", "unrelated_sentinel"):
            bad = copy.deepcopy(row)
            bad["checks"][check] = "not-run"
            path.write_text(json.dumps(bad), encoding="utf-8")
            assert f"{job_id}: fresh {check} assertion did not pass" in gate.evaluate_release1_matrix(candidate, tmp_path)
        for field, value, message in (
            ("scenario", "release9-then-install", "scenario does not match the required matrix leg"),
            ("service_manager", {"foreground_fallback": True}, "direct service ownership"),
        ):
            bad = dict(row, **{field: value})
            path.write_text(json.dumps(bad), encoding="utf-8")
            assert any(message in error for error in gate.evaluate_release1_matrix(candidate, tmp_path)), field
        path.write_text(json.dumps(row), encoding="utf-8")
    upgrade = tmp_path / "direct-linux" / "journey-result.json"
    row = json.loads(upgrade.read_text())
    fresh_shaped = dict(row, scenario="fresh-install")
    for key in ("release_transition", "handoff_source", "semantic_data"):
        fresh_shaped.pop(key)
    upgrade.write_text(json.dumps(fresh_shaped), encoding="utf-8")
    errors = gate.evaluate_release1_matrix(candidate, tmp_path)
    assert "direct-linux: scenario does not match the required matrix leg" in errors
    assert "direct-linux: direct receipt did not run the r0-handoff-then-update leg" in errors
    assert "direct-linux: handed-off release-9 to candidate transition is absent or wrong" in errors


def test_both_fresh_rows_require_complete_update_receipt(tmp_path):
    candidate = release1_receipts(tmp_path)
    for job in ("direct-linux-fresh", "direct-macos-fresh"):
        path = tmp_path / job / "journey-result.json"
        row = json.loads(path.read_text())
        original = row["update"]
        del row["update"]
        path.write_text(json.dumps(row))
        assert any(job + ": fresh update receipt" in error
                   for error in gate.evaluate_release1_matrix(candidate, tmp_path))
        row["update"] = copy.deepcopy(original)
        row["update"]["checks"]["verify_update"] = "not-run"
        path.write_text(json.dumps(row))
        assert any(job + ": fresh update verify_update" in error
                   for error in gate.evaluate_release1_matrix(candidate, tmp_path))
        row["update"] = original
        path.write_text(json.dumps(row))
    assert not gate.evaluate_release1_matrix(candidate, tmp_path)


def test_each_release1_leg_needs_its_own_receipt_kind(tmp_path):
    """The R0 handoff rows carry their leg's receipt kind; the other rows the shared one."""
    candidate = release1_receipts(tmp_path)
    assert not gate.evaluate_release1_matrix(candidate, tmp_path)
    kinds = {row["job_id"]: gate.release1_receipt_kind(row) for row in gate.RELEASE1_MATRIX}
    assert kinds["direct-linux"] == kinds["direct-macos"] == "r0-handoff-then-update"
    assert {kinds[job] for job in ("direct-windows", "direct-linux-fresh", "direct-macos-fresh")} == {
        "release1-installed-lifecycle"}
    swapped = {"r0-handoff-then-update": "release1-installed-lifecycle",
               "release1-installed-lifecycle": "r0-handoff-then-update",
               "release11-then-update": "release1-installed-lifecycle",
               "fresh-install-autostart-off": "release1-installed-lifecycle"}
    for job_id, kind in kinds.items():
        path = tmp_path / job_id / "journey-result.json"
        original = json.loads(path.read_text())
        assert original["receipt_kind"] == kind
        path.write_text(json.dumps(dict(original, receipt_kind=swapped[kind])))
        assert f"{job_id}: not a Release-1 lifecycle receipt" in gate.evaluate_release1_matrix(candidate, tmp_path)
        path.write_text(json.dumps(original))
    assert not gate.evaluate_release1_matrix(candidate, tmp_path)
