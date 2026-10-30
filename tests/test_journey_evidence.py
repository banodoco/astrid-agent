"""The evidence gate rejects incomplete or dishonest qualification results."""
import copy
import importlib.util
import json
from pathlib import Path

spec = importlib.util.spec_from_file_location('journey_evidence', Path(__file__).parents[1] / 'scripts/check_journey_evidence.py')
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)


def release1_receipts(tmp_path):
    candidate = {key: letter * 40 for key, letter in zip(("app", "agent", "runtime", "pinokio"), "abcd")}
    for contract in gate.RELEASE1_MATRIX:
        service = {"required": True, "outcome": "pass", "manager": "native", "version": "1",
                   "foreground_fallback": False}
        row = {"receipt_kind": "release1-installed-lifecycle", "job_id": contract["job_id"],
               "mode": contract["mode"], "scenario": contract.get("leg", "fresh-install"),
               "status": "pass", "runner_image": contract["runner"],
               "shell": contract["shell"], "source_override_test_mode": False, "final_cleanup": "pass",
               "candidate_checkouts": {name: {"sha": sha} for name, sha in candidate.items()},
               "service_manager_prerequisite": service,
               "checks": {"semantic_data": "pass", "release": "pass", "service": "pass", "health": "pass"}}
        if contract["mode"] == "direct":
            row["service_manager"] = {"manager": "native", "foreground_fallback": False}
            row["release_transition"] = {"from": 0, "to": 1,
                                         "harness_override": "frozen-release0-direct-to-release1"}
            row["release0_fixture"] = {
                "fixture_sums_sha256": "e" * 64, "manifest_sha256": "f" * 64,
                "provenance_sha256": "a" * 64,
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
