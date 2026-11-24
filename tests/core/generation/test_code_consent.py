"""LE-2b consent from the warning to entry replay and target admission."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from astrid.core.generation import node_approvals as approvals
from astrid.core.generation.preflight import GenerationReadinessError

NODE = {"repo": "https://example.test/consent-pack.git", "commit": "a" * 40,
        "requirements_txt": "deps/requirements.txt", "pip_no_deps": ["first==1", "last==2"]}
RETRY = ["astrid", "generate", "workflow", "workflow.py", "--placement", "managed"]


def test_agent_uses_the_target_approval_contract():
    from vibecomfy import node_approvals as target

    for name in ("approval_record", "validate_approval_record", "require_approved_nodes",
                 "consent_token", "consent_matches", "approval_matches"):
        assert getattr(approvals, name) is getattr(target, name)
    assert approvals.target_approvals is target.require_approved_nodes


@pytest.mark.parametrize("consent", [(), ("user_env",), ("code",), ("code=stale",), ("code=" + "0" * 12,)])
def test_no_bare_or_stale_consent_shows_informed_warning(consent):
    with pytest.raises(GenerationReadinessError) as caught:
        approvals.authorize_nodes([NODE], placement_class="local", consent=consent, retry_argv=RETRY)
    payload = caught.value.to_envelope()
    assert payload["status"] == "blocked"
    assert payload["issues"][0]["code"] == "needs_consent"
    fix = payload["fixes"][0]
    assert fix["who"] == "human" and "argv" not in fix
    warning = fix["instruction"]["step"]
    assert all(text in warning for text in (NODE["repo"], NODE["commit"], "files", "credentials", "network",
                                           "deps/requirements.txt", "first==1", "install.py is never run"))
    then = fix["instruction"]["then_argv"]
    assert then[-2:] == ["--consent", "code=" + approvals.consent_token(
        approvals.approval_record(NODE, placement_class="local"))]
    assert "--dry-run" not in then


def test_consent_retry_pins_add_to_the_shown_full_commit():
    retry = [*RETRY, "--add", NODE["repo"] + "@main", "--dry-run", "--consent", "code"]
    with pytest.raises(GenerationReadinessError) as caught:
        approvals.authorize_nodes([NODE], placement_class="local", retry_argv=retry)
    then = caught.value.readiness["fixes"][0]["instruction"]["then_argv"]
    assert then[then.index("--add") + 1] == NODE["repo"] + "@" + NODE["commit"]
    assert "--dry-run" not in then and "code" not in then


def test_saved_entry_reuses_only_matching_approval(tmp_path, monkeypatch):
    from vibecomfy.entries import load_entry
    monkeypatch.setenv("VIBECOMFY_ENTRIES_DIR", str(tmp_path))
    directory = tmp_path / "mine"
    directory.mkdir()
    (directory / "entry.json").write_text(json.dumps({"id": "mine", "kind": "image", "modes": ["t2i"],
        "bundle": "workflow.py", "binds": {}, "defaults": {}, "needs": []}))
    entry = dict(load_entry(directory), source="user-entry")
    token = approvals.consent_token(approvals.approval_record(NODE, placement_class="runpod"))
    accepted = approvals.authorize_nodes([NODE], placement_class="runpod", entry=entry,
        consent=("code=" + token,), retry_argv=RETRY, persist=True)
    assert approvals.read_entry_approvals(directory) == accepted
    assert approvals.authorize_nodes([NODE], placement_class="runpod", entry=entry, retry_argv=RETRY) == accepted
    for changed, placement in [({**NODE, "commit": "b" * 40}, "runpod"),
                               ({**NODE, "pip_no_deps": ["different==1"]}, "runpod"), (NODE, "local")]:
        with pytest.raises(GenerationReadinessError):
            approvals.authorize_nodes([changed], placement_class=placement, entry=entry,
                consent=("code=" + token,), retry_argv=RETRY)
    assert approvals.read_entry_approvals(directory) == accepted


def test_valid_dry_run_does_not_write_an_approval(tmp_path, monkeypatch):
    from vibecomfy.entries import load_entry
    monkeypatch.setenv("VIBECOMFY_ENTRIES_DIR", str(tmp_path))
    directory = tmp_path / "mine"
    directory.mkdir()
    (directory / "entry.json").write_text(json.dumps({"id": "mine", "kind": "image", "modes": [],
        "bundle": "workflow.py", "binds": {}, "defaults": {}, "needs": []}))
    entry = dict(load_entry(directory), source="user-entry")
    token = approvals.consent_token(approvals.approval_record(NODE, placement_class="local"))
    approvals.authorize_nodes([NODE], placement_class="local", entry=entry,
        consent=("code=" + token,), retry_argv=RETRY, persist=False)
    assert not (directory / approvals.APPROVALS_FILENAME).exists()


def test_target_refuses_source_authored_approvals_before_native_preparation(tmp_path, monkeypatch):
    from astrid.core.generation import managed_preparation as managed
    canonical = SimpleNamespace(metadata={"code_approvals": [approvals.approval_record(NODE, placement_class="local")]})
    prep = object.__new__(managed.ManagedEnginePreparation)
    prep.code_approvals = []
    prep.placement_class = "local"
    prep.check = lambda: None
    prep._begin_model_verification = lambda: pytest.fail("unapproved preparation reached native work")
    engine = SimpleNamespace(workflow_requirements=lambda workflows: {"extra": {"nodes": [NODE]}})
    monkeypatch.setattr(managed, "_preparation_engine", lambda name: engine)
    monkeypatch.setattr("subprocess.run", lambda *a, **kw: pytest.fail("unapproved code invoked a subprocess"))
    with pytest.raises(ValueError, match="needs_consent"):
        prep._prepare(tmp_path / "workflow.py", execution_identity="test", inputs={},
                      canonical_workflow=canonical, input_digests={})


def test_recipe_and_approval_reach_the_actual_target_lock(tmp_path):
    from astrid.core.generation.engine_recipe import load_recipe
    from astrid.core.generation.managed_preparation import _write_managed_nodes_lock
    from vibecomfy.node_packs import read_lockfile
    from vibecomfy.node_packs._install import validate_restore_approvals
    recipe = load_recipe("comfyui", "linux-x86_64-cu130")
    lock = tmp_path / "nodes.lock"
    _write_managed_nodes_lock(lock, [NODE], recipe)
    [entry] = read_lockfile(lock)
    record = approvals.approval_record(NODE, placement_class="runpod")
    assert entry.requirements_txt == NODE["requirements_txt"]
    assert list(entry.pip_no_deps) == NODE["pip_no_deps"]
    assert validate_restore_approvals([entry], [record], placement_class="runpod") == [record]
    with pytest.raises(ValueError, match="needs_consent"):
        validate_restore_approvals([entry], [record], placement_class="local")


def test_job_keeps_code_records_out_of_workflow_inputs(tmp_path, monkeypatch):
    from astrid.core.generation.local_job import read_job, _run_job
    from astrid.packs.vibecomfy import local_job
    record = approvals.approval_record(NODE, placement_class="runpod")
    path = tmp_path / "job.json"
    body = {"schema": "astrid.machine-job.v1", "entry": "workflow", "align": "align",
            "inputs": {}, "bindings": {}, "execution": {"python": "workflow.py"}, "code_approvals": [record]}
    path.write_text(json.dumps(body))
    captured = []
    monkeypatch.setattr(local_job, "run_prepared_job", lambda *a, **kw: captured.append(kw) or {})
    _run_job("workflow", read_job(path), tmp_path / "out")
    assert captured[0]["code_approvals"] == [record]
    assert captured[0]["placement_class"] == "runpod"
    assert read_job(path)["inputs"] == {}
    body["code_approvals"][0]["identity"]["commit"] = "b" * 40
    path.write_text(json.dumps(body))
    with pytest.raises(ValueError, match="code approvals"):
        read_job(path)


def test_sdk_and_cli_accept_code_choice_without_accepting_paid_consent():
    from astrid.core.generation.cli import _parser
    from astrid.sdk.generation import _generation_controls, GenerationInputError
    token = approvals.consent_token(approvals.approval_record(NODE, placement_class="local"))
    assert _parser().parse_args(["workflow", "workflow.py", "--consent", "code=" + token]).consent == ["code=" + token]
    assert _generation_controls(engine=None, consent=["user_env", "code=" + token])["consent"] == ("user_env", "code=" + token)
    with pytest.raises(GenerationInputError):
        _generation_controls(engine=None, consent=["paid"])


def _alignment_manifest():
    return {"id": "mine", "provider_only": False, "modes": [{"mode": "workflow", "workflow": None,
        "models": [], "unlocked": [], "node_packs": [{"name": "consent-pack", "repository": NODE["repo"],
        "revision": NODE["commit"], "requirements_txt": NODE["requirements_txt"],
        "pip_no_deps": NODE["pip_no_deps"], "requires_code_consent": True}]}]}


def test_alignment_inspection_reports_unapproved_pack_without_requesting_consent(tmp_path, monkeypatch):
    import sys
    from astrid.core.generation import asset_alignment as alignment, entries

    root = tmp_path / "engine"
    target = alignment.ManagedAssetTarget(root, root / "models", root / "nodes", sys.executable,
        "http://127.0.0.1:8188", "a" * 64, root / "cache.json")
    monkeypatch.setattr(entries, "find_entry", lambda *args: pytest.fail("inspection asked to authorize code"))
    report = alignment.align(entry_id="mine", target=target, manifests=[_alignment_manifest()])
    assert report["status"] == "not_ready" and report["applied"] is False
    assert "no matching shipped pin or local code approval" in report["next_action"]
    assert not root.exists()


def test_alignment_cli_keeps_human_warning_and_token_before_target_selection(monkeypatch, capsys):
    from astrid.core.generation import asset_alignment as alignment, cli, entries

    monkeypatch.setattr(alignment, "catalog_manifests", lambda *args: [_alignment_manifest()])
    monkeypatch.setattr(entries, "find_entry", lambda *args: None)
    monkeypatch.setattr(alignment, "selected_target", lambda: pytest.fail("unapproved code selected target"))
    assert cli.main(["align", "--entry", "mine", "--apply", "--consent", "user_env", "--json"]) == 2
    warning = json.loads(capsys.readouterr().out)
    assert warning["issues"][0]["code"] == "needs_consent"
    instruction = warning["fixes"][0]["instruction"]
    assert all(text in instruction["step"] for text in (NODE["repo"], NODE["commit"], "credentials"))
    assert "code=" + approvals.consent_token(approvals.approval_record(NODE, placement_class="local")) in instruction["then_argv"]


def test_cli_save_requires_warning_then_persists_approval(tmp_path, monkeypatch, capsys):
    from vibecomfy.templates import ReadyMetadata, new_workflow, OutputSpec
    from vibecomfy.nodes.core import EmptyImage, SaveImage
    from vibecomfy.security.provenance import Provenance
    from vibecomfy.workflow_bundle import emit_bundle_with_candidate
    from astrid.core.generation import cli
    from vibecomfy.entries import entries_dir

    monkeypatch.setenv("VIBECOMFY_ENTRIES_DIR", str(tmp_path / "entries"))
    monkeypatch.setattr(cli, "_open_client", lambda: pytest.fail("save-only opened Runtime"))
    with new_workflow(ReadyMetadata.build(template_id="consent-fixture", capability="image")) as workflow:
        image = EmptyImage(width=16, height=16, batch_size=1, color=0)
        output = SaveImage(images=image.out("IMAGE"), filename_prefix="consent")
        workflow.finalize({}, outputs=[OutputSpec(node=output)])
    workflow.requirements.custom_node_refs.append({"name": "consent-pack", "slug": "consent-pack",
        "source": "git", "url": NODE["repo"], "commit": NODE["commit"],
        "requirements_txt": NODE["requirements_txt"], "pip_no_deps": NODE["pip_no_deps"]})
    emitted = emit_bundle_with_candidate(workflow, tmp_path / "workflow.py", Provenance.USER_CONFIRMED, None,
                                         source_format="scratchpad")
    argv = ["workflow", str(emitted.python_path), "--as-entry", "mine", "--no-run", "--placement", "managed", "--json"]
    assert cli.main(argv) == 2
    warning = json.loads(capsys.readouterr().out)
    assert warning["issues"][0]["code"] == "needs_consent"
    assert not (entries_dir() / "mine").exists()
    retry = warning["fixes"][0]["instruction"]["then_argv"]
    assert retry[:2] == ["astrid", "generate"]
    assert cli.main(retry[2:]) == 0
    assert json.loads(capsys.readouterr().out)["saved"] is True
    assert approvals.read_entry_approvals(entries_dir() / "mine")
    # Replacing the same declaration can use its saved record without a flag.
    assert cli.main([*argv, "--replace"]) == 0
    assert json.loads(capsys.readouterr().out)["saved"] is True
