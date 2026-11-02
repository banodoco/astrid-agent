"""The evidence gate rejects incomplete or dishonest qualification results."""
import copy
import hashlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

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


def self_hosted_macos_identity(candidate, receipt_sha256="f" * 64):
    context = {
        "schema_version": 1,
        "source": "github-actions-api",
        "repository": "banodoco/astrid-agent",
        "workflow_path": ".github/workflows/journey.yml",
        "ref": "refs/heads/lane/M-QUAL/self-hosted-macos",
        "head_sha": "e" * 40,
        "run_id": 123,
        "run_attempt": 2,
        "job_id": 456,
        "matrix_job_id": "direct-macos",
        "runner_name": "hannah-mac",
        "runner_labels": ["self-hosted", "macOS", "astrid-mac"],
        "conclusion": "success",
        "candidate": copy.deepcopy(candidate),
        "receipt_sha256": receipt_sha256,
    }
    receipt = {
        "venue": "self-hosted",
        # Current driver receipt emission may retain local here; it is never
        # authentication and the API-originated context below is authoritative.
        "runner_image": "local",
        "execution_identity": {
            key: copy.deepcopy(context[key])
            for key in gate.SELF_HOSTED_MACOS_RECEIPT_IDENTITY_FIELDS
        },
    }
    return receipt, context


def self_hosted_macos_files(tmp_path, candidate, *, receipt=None, context=None):
    receipt = copy.deepcopy(receipt) if receipt is not None else {}
    context = copy.deepcopy(context) if context is not None else {}
    receipt_path = tmp_path / "self-hosted-macos" / "journey-result.json"
    context_path = tmp_path / "trusted-actions-context.json"
    receipt_path.parent.mkdir(exist_ok=True)
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    context["receipt_sha256"] = gate.digest(receipt_path)
    context_path.write_text(json.dumps(context), encoding="utf-8")
    return receipt_path, context_path


def self_hosted_macos_release1_receipt(tmp_path):
    candidate = release1_receipts(tmp_path)
    receipt_path = tmp_path / "direct-macos" / "journey-result.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    identity, context = self_hosted_macos_identity(candidate)
    receipt.update(identity)
    return candidate, receipt, context


def test_self_hosted_macos_identity_requires_matching_external_context(tmp_path):
    candidate = {key: letter * 40 for key, letter in zip(("app", "agent", "runtime", "pinokio"), "abcd")}
    receipt, context = self_hosted_macos_identity(candidate)
    assert not gate.self_hosted_macos_identity_errors(candidate, receipt, context, "f" * 64)

    # The receipt's own runner/repository claims have no authority; changing
    # either after the API context hashed the receipt is rejected.
    bad = copy.deepcopy(receipt)
    bad["execution_identity"]["runner_name"] = "not-hannah-mac"
    assert any("runner_name differs" in error for error in gate.self_hosted_macos_identity_errors(
        candidate, bad, context, "f" * 64
    ))
    bad = copy.deepcopy(receipt)
    bad["execution_identity"]["repository"] = "attacker/example"
    assert any("repository differs" in error for error in gate.self_hosted_macos_identity_errors(
        candidate, bad, context, "f" * 64
    ))


def test_self_hosted_macos_positive_exact_authenticated_record(tmp_path):
    candidate, receipt, context = self_hosted_macos_release1_receipt(tmp_path)
    receipt_path, context_path = self_hosted_macos_files(tmp_path, candidate, receipt=receipt, context=context)
    assert not gate.evaluate_self_hosted_macos_receipt(candidate, receipt_path, context_path)


def test_self_hosted_macos_rejects_missing_wrong_runner_name_label_run_attempt_ref_head_tuple_and_cross_run(tmp_path):
    candidate, original_receipt, original_context = self_hosted_macos_release1_receipt(tmp_path)

    cases = []
    cases.append((original_receipt, {}, "trusted context is missing"))
    missing_name = copy.deepcopy(original_receipt)
    del missing_name["execution_identity"]["runner_name"]
    cases.append((missing_name, original_context, "identity is missing runner_name"))

    hosted_relabel = copy.deepcopy(original_receipt)
    hosted_relabel["runner_image"] = "macos-15"
    cases.append((hosted_relabel, original_context, "relabelled hosted evidence"))

    wrong_name = copy.deepcopy(original_receipt)
    wrong_name["execution_identity"]["runner_name"] = "other-mac"
    cases.append((wrong_name, original_context, "runner_name differs"))

    wrong_label = copy.deepcopy(original_receipt)
    wrong_label["execution_identity"]["runner_labels"] = ["self-hosted", "macOS", "other-mac"]
    cases.append((wrong_label, original_context, "runner labels are missing astrid-mac"))

    wrong_attempt = copy.deepcopy(original_receipt)
    wrong_attempt["execution_identity"]["run_attempt"] = 3
    cases.append((wrong_attempt, original_context, "run_attempt differs"))

    wrong_ref = copy.deepcopy(original_receipt)
    wrong_ref["execution_identity"]["ref"] = "refs/heads/other"
    cases.append((wrong_ref, original_context, "ref differs"))

    wrong_head = copy.deepcopy(original_receipt)
    wrong_head["execution_identity"]["head_sha"] = "9" * 40
    cases.append((wrong_head, original_context, "head_sha differs"))

    wrong_tuple = copy.deepcopy(original_context)
    wrong_tuple["candidate"]["runtime"] = "9" * 40
    cases.append((original_receipt, wrong_tuple, "trusted tuple differs"))

    cross_run = copy.deepcopy(original_context)
    cross_run["run_id"] = 999
    cases.append((original_receipt, cross_run, "run_id differs"))

    forged_source = copy.deepcopy(original_context)
    forged_source["source"] = "runner-environment"
    cases.append((original_receipt, forged_source, "must be GitHub Actions API-originated"))

    for receipt, context, expected in cases:
        receipt_path, context_path = self_hosted_macos_files(
            tmp_path, candidate, receipt=receipt, context=context
        )
        assert any(expected in error for error in gate.evaluate_self_hosted_macos_receipt(
            candidate, receipt_path, context_path
        )), expected

    receipt_path, _ = self_hosted_macos_files(
        tmp_path, candidate, receipt=original_receipt, context=original_context
    )
    assert any("trusted context cannot be supplied by the receipt artifact" in error
               for error in gate.evaluate_self_hosted_macos_receipt(candidate, receipt_path, receipt_path))


def test_self_hosted_macos_rejects_a_receipt_forged_after_trusted_api_binding(tmp_path):
    candidate, receipt, context = self_hosted_macos_release1_receipt(tmp_path)
    receipt_path, context_path = self_hosted_macos_files(tmp_path, candidate, receipt=receipt, context=context)
    receipt["execution_identity"]["runner_name"] = "forged-mac"
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    assert any("trusted receipt digest differs" in error
               for error in gate.evaluate_self_hosted_macos_receipt(candidate, receipt_path, context_path))


def test_self_hosted_macos_cli_accepts_only_the_exact_authenticated_record(tmp_path):
    app = tmp_path / "app"
    public = app / "public"
    public.mkdir(parents=True)
    fixture_agent = "b" * 40
    fixture_runtime = "c" * 40
    manifest = {
        "agent": {"sha": fixture_agent},
        "runtime": {"sha": fixture_runtime, "schema_digest": "fixture-schema"},
        "tools": {},
    }
    (public / "install-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    for relative in ("install", "install.sh", "install.ps1"):
        (public / relative).write_text(relative + "\n", encoding="utf-8")
    for command in (
        ("git", "init", "-q", str(app)),
        ("git", "-C", str(app), "add", "public"),
        ("git", "-C", str(app), "-c", "user.name=checker-test", "-c", "user.email=checker@example.invalid",
         "commit", "-qm", "fixture"),
    ):
        subprocess.run(command, check=True)
    app_sha = subprocess.check_output(("git", "-C", str(app), "rev-parse", "HEAD"), text=True).strip()
    candidate = {
        "app": app_sha,
        "agent": fixture_agent,
        "runtime": fixture_runtime,
        "pinokio": "d" * 40,
        "manifest_sha256": hashlib.sha256((public / "install-manifest.json").read_bytes()).hexdigest(),
        "artifacts": {
            "public/" + relative: hashlib.sha256((public / relative).read_bytes()).hexdigest()
            for relative in ("install", "install.sh", "install.ps1")
        },
    }
    fixture_root = tmp_path / "release1-fixture"
    fixture_root.mkdir()
    fixture_candidate = release1_receipts(fixture_root)
    receipt = json.loads((fixture_root / "direct-macos" / "journey-result.json").read_text(encoding="utf-8"))
    replacements = {fixture_candidate[key]: candidate[key] for key in fixture_candidate}

    def replace_fixture_tuple(value):
        if isinstance(value, dict):
            return {key: replace_fixture_tuple(item) for key, item in value.items()}
        if isinstance(value, list):
            return [replace_fixture_tuple(item) for item in value]
        return replacements.get(value, value)

    receipt = replace_fixture_tuple(receipt)
    identity, context = self_hosted_macos_identity(candidate)
    receipt.update(identity)
    receipt_path, context_path = self_hosted_macos_files(
        tmp_path, candidate, receipt=receipt, context=context
    )
    candidate_path = tmp_path / "candidate.json"
    candidate_path.write_text(json.dumps(candidate), encoding="utf-8")
    completed = subprocess.run(
        (
            sys.executable, str(Path(__file__).parents[1] / "scripts" / "check_journey_evidence.py"),
            "--aggregate-self-hosted-macos", "--candidate", str(candidate_path),
            "--app-checkout", str(app), "--self-hosted-macos-receipt", str(receipt_path),
            "--trusted-context", str(context_path),
        ),
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "## Journey evidence: PASS" in completed.stdout


def test_self_hosted_macos_route_leaves_hosted_release1_acceptance_and_rejection_unchanged(tmp_path):
    candidate = release1_receipts(tmp_path)
    assert not gate.evaluate_release1_matrix(candidate, tmp_path)

    receipt_path = tmp_path / "direct-macos" / "journey-result.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    identity, context = self_hosted_macos_identity(candidate)
    receipt.update(identity)
    trusted_receipt, trusted_context = self_hosted_macos_files(
        tmp_path, candidate, receipt=receipt, context=context
    )
    assert not gate.evaluate_self_hosted_macos_receipt(candidate, trusted_receipt, trusted_context)
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    assert any("direct-macos: runner does not match" in error
               for error in gate.evaluate_release1_matrix(candidate, tmp_path))


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


def test_release1_macos_rows_require_macos15_and_hosted_provenance(tmp_path):
    candidate = release1_receipts(tmp_path)
    assert not gate.evaluate_release1_matrix(candidate, tmp_path)
    for job_id in ('direct-macos', 'pinokio-backend-macos', 'pinokio-fully-local-macos'):
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
    matrix_text = workflow.split('          matrix = [\n', 1)[1].split('          ]', 1)[0]
    workflow_rows = [json.loads(line.strip().rstrip(',')) for line in matrix_text.splitlines()]
    contract_rows = [{**row, 'leg': row.get('leg', 'fresh-install')} for row in gate.RELEASE1_MATRIX]
    assert len(contract_rows) == len({row['job_id'] for row in contract_rows}) == 9
    assert workflow_rows == contract_rows
    assert "--aggregate-release1" in workflow
    assert "journey-receipt-${{ matrix.job_id }}" in workflow
    assert "candidate-agent" in workflow and "pinokio" in workflow
    assert '"leg":"release0-to-release1"' in workflow
    assert "--release0-fixture" in workflow and "--allow-release0-to-release1" in workflow
