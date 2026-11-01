"""The evidence gate rejects incomplete or dishonest qualification results."""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path

from scripts import failure_mode_executor, journey_matrix
from tests.failure_mode_fixtures import PROVISIONAL_CANDIDATE

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


def _m4_6_local_observation(slot_id):
    contract = journey_matrix.validate_m4_6_slot(slot_id)

    def identity():
        return {
            "loaded_sources": {
                "agent_module": "/tmp/agent.py",
                "agent_module_sha256": "a" * 64,
                "runtime_module": "/tmp/runtime.py",
                "runtime_module_sha256": "b" * 64,
                "runtime_discovery": {
                    "pid": 123,
                    "process_birth_id": "birth",
                    "runtime_instance_id": "instance",
                },
            },
        }

    cleanup = {"owned_processes_after": 0, "process_absent": True}
    pair = {"before": identity(), "after": identity()}
    stop_cases = {
        outcome: {
            "executable_identity": copy.deepcopy(pair),
            "cleanup": copy.deepcopy(cleanup),
            "mutation_inventory": {"unchanged": True, "before": "same", "after": "same"},
        }
        for outcome in failure_mode_executor.failure.STOP_OUTCOMES
    }
    return {
        "slot_id": slot_id,
        "runner_image": contract["runner"],
        "shell": contract["shell"],
        "execution_scope": "local-disposable",
        "runner_provenance": {"ImageVersion": "local-disposable"},
        "candidate_checkouts": {
            name: {"sha": PROVISIONAL_CANDIDATE[name]}
            for name in ("app", "agent", "runtime", "pinokio")
        },
        "manifest_sha256": PROVISIONAL_CANDIDATE["manifest_sha256"],
        "evidence_files": [],
        "scenarios": {
            "interruption_recovery": {"terminal_identity": identity(), "cleanup": copy.deepcopy(cleanup)},
            "failed_candidate_rollback": {
                "executable_identity": copy.deepcopy(pair),
                "candidate_process_boundary": {
                    "started": True,
                    "exit_code": 73,
                    "process_identity_sha256": "c" * 64,
                    "loaded_sources": {"agent_module_sha256": "d" * 64},
                },
                "cleanup": copy.deepcopy(cleanup),
            },
            "keep_data_uninstall": {"executable_identity_before": identity(), "cleanup": copy.deepcopy(cleanup)},
            "stop_safety": {"cases": stop_cases},
        },
    }


def _write_m4_6_local_receipts(root):
    paths = {}
    for contract in journey_matrix.M4_6_MATRIX:
        slot_id = contract["job_id"]
        observation = _m4_6_local_observation(slot_id)
        evidence = []
        for scenario in failure_mode_executor.failure.SCENARIOS:
            path = root / slot_id / "evidence" / f"{scenario}.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            contents = json.dumps({"slot_id": slot_id, "scenario": scenario}).encode("utf-8")
            path.write_bytes(contents)
            evidence.append({
                "path": f"evidence/{scenario}.json",
                "sha256": hashlib.sha256(contents).hexdigest(),
                "kind": "machine-observation",
                "scenario": scenario,
            })
        observation["evidence_files"] = copy.deepcopy(evidence)
        receipt = {
            "schema_version": 1,
            "receipt_kind": "m4-6-local-disposable-execution",
            "status": "exercised",
            "slot_id": slot_id,
            "execution_scope": "local-disposable",
            "source_override_test_mode": False,
            "candidate": copy.deepcopy(PROVISIONAL_CANDIDATE),
            "evidence_files": copy.deepcopy(evidence),
            "observation": observation,
        }
        path = root / slot_id / "m4-6-local-result.json"
        path.write_text(json.dumps(receipt), encoding="utf-8")
        paths[slot_id] = path
    return paths


def test_m4_6_canonical_evaluator_rejects_missing_duplicate_stale_failed_and_prepared_evidence(tmp_path):
    valid = tmp_path / "valid"
    paths = _write_m4_6_local_receipts(valid)
    assert gate.evaluate_m4_6_local_matrix(valid, PROVISIONAL_CANDIDATE) == []

    missing = tmp_path / "missing"
    paths = _write_m4_6_local_receipts(missing)
    paths["direct-linux"].unlink()
    assert any("direct-linux: missing M4-6 local execution" in error
               for error in gate.evaluate_m4_6_local_matrix(missing, PROVISIONAL_CANDIDATE))

    duplicates = tmp_path / "duplicates"
    paths = _write_m4_6_local_receipts(duplicates)
    duplicate = duplicates / "duplicate" / "m4-6-local-result.json"
    duplicate.parent.mkdir()
    duplicate.write_bytes(paths["direct-linux"].read_bytes())
    assert any("direct-linux: duplicate M4-6 local receipt" in error
               for error in gate.evaluate_m4_6_local_matrix(duplicates, PROVISIONAL_CANDIDATE))

    stale_root = tmp_path / "stale"
    paths = _write_m4_6_local_receipts(stale_root)
    stale = json.loads(paths["direct-linux"].read_text())
    stale["candidate"]["agent"] = "f" * 40
    paths["direct-linux"].write_text(json.dumps(stale), encoding="utf-8")
    assert any("direct-linux: local receipt candidate tuple differs" in error
               for error in gate.evaluate_m4_6_local_matrix(stale_root, PROVISIONAL_CANDIDATE))

    failed_root = tmp_path / "failed"
    paths = _write_m4_6_local_receipts(failed_root)
    failed = json.loads(paths["direct-linux"].read_text())
    failed["status"] = "failed"
    paths["direct-linux"].write_text(json.dumps(failed), encoding="utf-8")
    assert any("direct-linux: local receipt status is not 'exercised'" in error
               for error in gate.evaluate_m4_6_local_matrix(failed_root, PROVISIONAL_CANDIDATE))

    prepared_root = tmp_path / "prepared"
    paths = _write_m4_6_local_receipts(prepared_root)
    prepared = json.loads(paths["direct-linux"].read_text())
    prepared["status"] = "prepared"
    paths["direct-linux"].write_text(json.dumps(prepared), encoding="utf-8")
    assert any("direct-linux: local receipt status is not 'exercised'" in error
               for error in gate.evaluate_m4_6_local_matrix(prepared_root, PROVISIONAL_CANDIDATE))

    missing_sidecar = tmp_path / "missing-sidecar"
    paths = _write_m4_6_local_receipts(missing_sidecar)
    evidence = missing_sidecar / "direct-linux" / "evidence" / "interruption_recovery.json"
    evidence.unlink()
    assert any("direct-linux: evidence_files[0]: missing evidence file" in error
               for error in gate.evaluate_m4_6_local_matrix(missing_sidecar, PROVISIONAL_CANDIDATE))


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
