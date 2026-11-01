"""The evidence gate rejects incomplete or dishonest qualification results."""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path

import yaml

spec = importlib.util.spec_from_file_location('journey_evidence', Path(__file__).parents[1] / 'scripts/check_journey_evidence.py')
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)


def release1_receipts(tmp_path):
    candidate = {key: letter * 40 for key, letter in zip(("app", "agent", "runtime", "pinokio"), "abcd")}
    for contract in gate.RELEASE1_MATRIX:
        runner_os, image_os = gate.RUNNER_PROVENANCE[contract["runner"]]
        service = {"required": True, "outcome": "pass", "manager": "native", "version": "1",
                   "foreground_fallback": False}
        row = {"receipt_kind": "release1-installed-lifecycle", "job_id": contract["job_id"],
               "mode": contract["mode"], "scenario": contract.get("leg", "fresh-install"),
               "status": "pass", "runner_image": contract["runner"],
               "shell": contract["shell"], "source_override_test_mode": False, "final_cleanup": "pass",
               "os_build": runner_os + "-test", "runner_provenance": {"RUNNER_OS": runner_os,
                                                                            "ImageOS": image_os,
                                                                            "ImageVersion": "test"},
               "candidate_checkouts": {name: {"sha": sha} for name, sha in candidate.items()},
               "sources": {name: {"sha": candidate[name]} for name in ("app", "agent", "runtime")},
               "installed_tuple": {name: candidate[name] for name in ("app", "agent", "runtime")},
               "import_origins": {"runtime": "/owned/runtime"},
               "service_manager_prerequisite": service,
               "checks": {"semantic_data": "pass", "release": "pass", "service": "pass", "health": "pass",
                          "task_settled": "pass", "one_runtime": "pass", "unrelated_sentinel": "pass"}}
        if contract["mode"] == "direct":
            row["service_manager"] = {"manager": "native", "foreground_fallback": False}
            row["release_transition"] = {"from": 0, "to": 1,
                                         "harness_override": "frozen-release0-direct-to-release1"}
            row["release0_fixture"] = {
                "fixture_sums_sha256": gate.RELEASE0_SUMS_SHA256, "manifest_sha256": "f" * 64,
                "provenance_sha256": "a" * 64,
                "bootstrap_sha256": gate.RELEASE0_BOOTSTRAP_SHA256,
                "sources": {"agent": "3204bb00135bec8ee2d2d3fb21eed5abfceca649",
                            "runtime": "6d53a19070faf63fc15ae1f81417722edc2c8a5f"},
            }
            row["semantic_data"] = {"identical": True, "before_sha256": "b" * 64,
                                    "after_sha256": "b" * 64}
        else:
            row["service_manager_prerequisite"] = {"required": False, "outcome": "explicit-owner",
                                                    "managed_by": "pinokio", "foreground_fallback": False}
        receipt = tmp_path / contract["job_id"] / "journey-result.json"
        receipt.parent.mkdir(exist_ok=True)
        receipt.write_text(json.dumps(row), encoding="utf-8")
    return candidate


def evidence(tmp_path):
    candidate = {k: ch * 40 for k, ch in zip(('app', 'agent', 'runtime', 'pinokio'), 'abcd')}
    manifest = {'agent': {'sha': candidate['agent']}, 'runtime': {'sha': candidate['runtime'], 'schema_digest': 'schema'}, 'tools': {'uv': {'version': '1'}}}
    row = {'scenario': 'fresh-install', 'status': 'pass', 'os_build': 'Linux-6.8.0', 'shell': 'sh', 'final_cleanup': 'pass',
           'runner_image': 'ubuntu-24.04', 'runner_provenance': {'RUNNER_OS': 'Linux', 'ImageOS': 'ubuntu24', 'ImageVersion': '20261028.1'},
           'source_override_test_mode': False,
           'sources': {'app': {'sha': candidate['app']}, 'agent': manifest['agent'], 'runtime': manifest['runtime']},
           'installed_tuple': {**{k: candidate[k] for k in ('app', 'agent', 'runtime')}, 'schema_digest': 'schema', 'tools': {'uv': '1'}},
           'import_origins': {'runtime': '/owned/runtime'},
           'checks': {k: 'pass' for k in ('task_settled', 'workspace_content', 'one_runtime', 'unrelated_sentinel')}}
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
    assert gate.evaluate(candidate, manifest, tmp_path, ['fresh-install'], 'macos-14', 'sh')
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
        ('macos-14', 'macOS', 'macos14', 'macOS-14.7-arm64', 'sh'),
    ):
        item = copy.deepcopy(row)
        item.update(runner_image=runner, os_build=build, shell=shell)
        item['runner_provenance'].update(RUNNER_OS=runner_os, ImageOS=image_os)
        write(item)
        assert not gate.evaluate(candidate, manifest, tmp_path, ['fresh-install'], runner, shell)
        item['runner_provenance']['ImageOS'] = 'unexpected'
        write(item)
        assert any('ImageOS' in e for e in gate.evaluate(candidate, manifest, tmp_path, ['fresh-install'], runner, shell))


def test_release1_aggregate_rejects_missing_skipped_and_ambiguous_receipts(tmp_path):
    candidate = release1_receipts(tmp_path)
    assert not gate.evaluate_release1_matrix(candidate, tmp_path)
    (tmp_path / "direct-linux" / "journey-result.json").unlink()
    assert any("direct-linux: missing receipt" in error
               for error in gate.evaluate_release1_matrix(candidate, tmp_path))
    candidate = release1_receipts(tmp_path)
    skipped = tmp_path / "pinokio-backend-linux" / "journey-result.json"
    data = json.loads(skipped.read_text())
    data["status"] = "skipped"
    skipped.write_text(json.dumps(data), encoding="utf-8")
    assert any("pinokio-backend-linux: status 'skipped'" in error
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
    data["semantic_data"]["after_sha256"] = "c" * 64
    direct.write_text(json.dumps(data), encoding="utf-8")
    assert any("direct-windows: semantic data preservation" in error
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


def test_workflow_declares_the_exact_release1_matrix_and_aggregate_gate():
    workflow = (Path(__file__).parents[1] / ".github" / "workflows" / "journey.yml").read_text()
    for contract in gate.RELEASE1_MATRIX:
        assert f'"job_id":"{contract["job_id"]}"' in workflow
        assert f'"mode":"{contract["mode"]}"' in workflow
        assert f'"runner":"{contract["runner"]}"' in workflow
    assert "--aggregate-release1" in workflow
    assert "journey-receipt-${{ matrix.job_id }}" in workflow
    assert "candidate-agent" in workflow and "pinokio" in workflow
    assert '"leg":"release0-to-release1"' in workflow
    assert "--release0-fixture" in workflow and "--allow-release0-to-release1" in workflow


def windows_task_host_receipt(root, shell="powershell.exe"):
    """Create one synthetic X5 artifact; it is never presented as hosted proof."""
    root.mkdir(parents=True, exist_ok=True)
    candidate = {key: letter * 40 for key, letter in zip(("app", "agent", "runtime", "pinokio"), "abcd")}
    manifest = {"agent": {"sha": candidate["agent"]}, "runtime": {"sha": candidate["runtime"]}}
    manifest_bytes = (json.dumps(manifest, sort_keys=True) + "\n").encode()
    candidate["manifest_sha256"] = hashlib.sha256(manifest_bytes).hexdigest()
    candidate["artifacts"] = {path: letter * 64 for path, letter in zip(gate.ARTIFACTS, "ef0")}
    task = {
        "ok": True,
        "data": {"state": "succeeded", "result": {"outputs": [
            {"name": "sfx", "digest": "sha256:" + "1" * 64, "size": 1},
            {"name": "sfx_meta", "digest": "sha256:" + "2" * 64, "size": 1},
        ]}},
    }
    contents = {
        "install-manifest.json": manifest_bytes,
        "installed-manifest.json": manifest_bytes,
        "sources.json": json.dumps({
            "app": {"sha": candidate["app"]}, "agent": manifest["agent"], "runtime": manifest["runtime"],
        }).encode(),
        "bootstrap.log": b"installed\n",
        "start.log": b"started\n",
        "health.json": json.dumps({"status": "ok", "runtime_instance_id": "instance-1"}).encode(),
        "pack-host-ready.json": json.dumps({"status": "ready", "runtime_instance_id": "instance-1"}).encode(),
        "processes-host-ready.json": json.dumps({"astrid_processes": [{"pid": 1}]}).encode(),
        "installed-profile-binding-summary.json": json.dumps({"profile_bound": True, "service_bound": True}).encode(),
        "sdk-invocation.json": json.dumps({"ok": True}).encode(),
        "task-settled.json": json.dumps(task).encode(),
        "disable-scheduled-task.log": b"disabled\n",
        "down.json": json.dumps({"ok": True}).encode(),
        "processes-after-down.json": json.dumps({"astrid_processes": []}).encode(),
        "collection.json": json.dumps({"errors": []}).encode(),
    }
    for name, content in contents.items():
        (root / name).write_bytes(content)
    evidence_files = [
        {"path": name, "sha256": hashlib.sha256(content).hexdigest(), "bytes": len(content)}
        for name, content in sorted(contents.items())
    ]
    commands = {
        name: {"command": f"ci_windows_journey.py {name}", "outcome": "success", "conclusion": "success"}
        for name in gate.WINDOWS_TASK_HOST_REQUIRED_STEPS
    }
    row = {
        "schema_version": 1,
        "receipt_kind": gate.WINDOWS_TASK_HOST_RECEIPT_KIND,
        "scenario": "windows-task-host",
        "status": "pass",
        "evidence_class": "hosted-installed-task-host",
        "source_override_test_mode": False,
        "shell": shell,
        "runner_selector": "windows-latest",
        "runner_provenance": {"RUNNER_OS": "Windows", "ImageOS": "win25", "ImageVersion": "20261030.1"},
        "os_build": "Windows-10.0.26100",
        "workflow": {
            "repository": "banodoco/astrid-agent", "commit": candidate["agent"],
            "workflow_sha": candidate["agent"], "workflow_ref": "banodoco/astrid-agent/.github/workflows/windows-journey.yml@abc",
            "run_id": "123", "run_attempt": "1",
        },
        "candidate": {key: candidate[key] for key in ("app", "agent", "runtime", "manifest_sha256", "artifacts")},
        "inner_steps": commands,
        "coverage": {
            "journey": "windows-task-host", "platform": "windows", "installer_shell": shell,
            "required_evidence": list(gate.WINDOWS_TASK_HOST_REQUIRED_EVIDENCE),
        },
        "limitations": sorted(gate.WINDOWS_TASK_HOST_LIMITATIONS),
        "evidence_files": evidence_files,
    }
    receipt = root / "journey-result.json"
    receipt.write_text(json.dumps(row), encoding="utf-8")
    return candidate, manifest, row, receipt


def test_windows_task_host_receipt_is_admitted_by_the_canonical_evaluator(tmp_path):
    candidate, manifest, _, _ = windows_task_host_receipt(tmp_path)
    assert not gate.evaluate(candidate, manifest, tmp_path, ["windows-task-host"], "windows-latest", "powershell.exe")


def test_windows_task_host_rejects_missing_duplicate_and_stale_tuple(tmp_path):
    candidate, manifest, _, _ = windows_task_host_receipt(tmp_path / "one")
    assert any("windows-task-host: missing" in error
               for error in gate.evaluate(candidate, manifest, tmp_path / "empty", ["windows-task-host"], "windows-latest", "powershell.exe"))
    windows_task_host_receipt(tmp_path / "two")
    assert any("duplicate scenario" in error
               for error in gate.evaluate(candidate, manifest, tmp_path, ["windows-task-host"], "windows-latest", "powershell.exe"))
    stale = copy.deepcopy(candidate)
    stale["runtime"] = "e" * 40
    assert any("candidate runtime differs" in error
               for error in gate.evaluate(stale, manifest, tmp_path / "one", ["windows-task-host"], "windows-latest", "powershell.exe"))


def test_windows_task_host_rejects_prepared_incomplete_and_failed_inner_receipts(tmp_path):
    candidate, manifest, row, receipt = windows_task_host_receipt(tmp_path)
    row["status"] = "prepared"
    receipt.write_text(json.dumps(row), encoding="utf-8")
    assert any("status 'prepared'" in error
               for error in gate.evaluate(candidate, manifest, tmp_path, ["windows-task-host"], "windows-latest", "powershell.exe"))
    row["status"] = "pass"
    del row["inner_steps"]["collect"]
    receipt.write_text(json.dumps(row), encoding="utf-8")
    assert any("inner command results are incomplete" in error
               for error in gate.evaluate(candidate, manifest, tmp_path, ["windows-task-host"], "windows-latest", "powershell.exe"))
    row["inner_steps"]["collect"] = {"command": "ci_windows_journey.py collect", "outcome": "success", "conclusion": "success"}
    row["inner_steps"]["task"]["outcome"] = "failure"
    receipt.write_text(json.dumps(row), encoding="utf-8")
    assert any("inner command task did not pass" in error
               for error in gate.evaluate(candidate, manifest, tmp_path, ["windows-task-host"], "windows-latest", "powershell.exe"))
    row["inner_steps"]["task"]["outcome"] = "success"
    evidence_files = row["evidence_files"]
    row["evidence_files"] = {"not": "a list"}
    receipt.write_text(json.dumps(row), encoding="utf-8")
    assert any("evidence file inventory is absent" in error
               for error in gate.evaluate(candidate, manifest, tmp_path, ["windows-task-host"], "windows-latest", "powershell.exe"))
    row["evidence_files"] = evidence_files
    receipt.write_text(json.dumps(row), encoding="utf-8")
    (tmp_path / "installed-manifest.json").unlink()
    assert any("required evidence is missing: installed-manifest.json" in error
               for error in gate.evaluate(candidate, manifest, tmp_path, ["windows-task-host"], "windows-latest", "powershell.exe"))


def test_windows_workflow_declares_int_r1_and_canonical_receipt_contract():
    workflow_path = Path(__file__).parents[1] / ".github" / "workflows" / "windows-journey.yml"
    document = yaml.load(workflow_path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    assert document["on"]["push"]["branches"] == ["main", "release-1", "int/r1", "windows/**"]
    assert "workflow_dispatch" in document["on"]
    job = document["jobs"]["windows-journey"]
    assert job["concurrency"] if "concurrency" in job else document["concurrency"]
    receipt = next(step for step in job["steps"] if step.get("id") == "receipt")
    assert receipt["if"] == "always()"
    assert "steps.task.outcome" in receipt["env"]["STEP_TASK_OUTCOME"]
    upload = next(step for step in job["steps"] if step["name"] == "Upload Windows task host journey evidence")
    assert upload["with"]["path"] == "${{ runner.temp }}/X5-windows-journey/"
    assert upload["with"]["name"] == "Windows-task-host-journey-${{ matrix.installer-shell }}"
