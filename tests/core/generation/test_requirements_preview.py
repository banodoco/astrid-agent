"""WS9 command, exact producer and error transport regressions.

Full portable golden plans are transported unchanged. Public FILE commands
use the real producer, real selected-receipt paths and recorded source reads;
raw CLI flags are not silently upgraded to the richer structured golden inputs.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys

import pytest

from astrid.core.generation import cli, requirements
from astrid.core.generation.preflight import GenerationReadinessError, generation_error


def test_requirements_parser_retains_exact_repeated_inputs():
    args = cli._parser().parse_args([
        "requirements", "workflow.json", "--placement", "http://127.0.0.1:8188",
        "--add", "https://github.com/example/node@v1", "--add", "https://github.com/example/other",
        "--add-model", "checkpoints/model.safetensors=https://example.org/model#sha256",
        "--consent", "code=0123456789ab", "--dry-run", "--json",
    ])
    assert args.source == "workflow.json"
    assert args.placement == "http://127.0.0.1:8188"
    assert args.add == ["https://github.com/example/node@v1", "https://github.com/example/other"]
    assert args.add_model == ["checkpoints/model.safetensors=https://example.org/model#sha256"]
    assert args.consent == ["code=0123456789ab"]
    assert args.dry_run and args.json
    assert "requirements" in cli.subcommand_names()


def test_draft_preview_does_not_advertise_apply(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["requirements", "--help"])
    assert exc.value.code == 0
    help_text = capsys.readouterr().out
    assert "--add-model" in help_text and "--dry-run" in help_text
    assert "--apply" not in help_text


def test_resolve_file_without_publishing(tmp_path):
    source = tmp_path / "workflow.json"
    source.write_text("{}")
    before = snapshot([tmp_path])
    selected = requirements.resolve_source(str(source))
    assert selected.path == source.resolve()
    assert selected.entry is None and selected.approvals == []
    assert snapshot([tmp_path]) == before


def test_plan_and_who_survive_blocked_error_adapters():
    # Serialization sentinel only, not a claimed producer fixture/digest.
    plan = {"requirements": [], "gaps": [], "bytes": None, "digest": "a" * 64}
    entry = {"id": "saved"}
    target = {"root": "/actual/root", "python": "/actual/python"}
    fix = {"id": "inspect-target", "who": "agent", "scope": "read_only",
           "argv": ["astrid", "generate", "targets", "--placement", "managed"]}
    issue = {"code": "unknown_input", "severity": "error", "message": "Target facts unavailable.",
             "details": {"state": "unknown"}, "fix_ids": ["inspect-target"]}
    with pytest.raises(GenerationReadinessError) as exc:
        requirements.preview_envelope(plan, entry=entry, target=target, issues=[issue], fixes=[fix])
    result = exc.value.to_envelope()
    assert result["plan"] == plan
    assert result["entry"] == entry and result["target"] == target
    assert result["fixes"][0]["who"] == "agent"
    assert generation_error(result).to_envelope() == result
    result["plan"]["digest"] = "changed"
    assert exc.value.to_envelope()["plan"] == plan


def test_requirements_envelope_refuses_ambiguous_fixes():
    plan = {"requirements": [], "gaps": [], "bytes": None, "digest": "a" * 64}
    with pytest.raises(ValueError, match="who acts"):
        requirements.preview_envelope(plan, entry=None, target=None, fixes=[{"scope": "read_only"}])


def test_p35_read_observes_protected_document_without_write():
    # Unit contract only: native proof uses the real authenticated Runtime
    # transport and approved -> revoked -> apply refusal, not this store double.
    from astrid.core import user_state

    digest = "sha256:" + "a" * 64
    subject = "example-pack"
    expected_id = "pack-" + hashlib.sha256(
        json.dumps(["pack", subject, digest], separators=(",", ":")).encode()
    ).hexdigest()[:24]

    class ReadOnly:
        def __init__(self):
            self.content = None
            self.reads = []

        def get_user_app_state(self, key):
            self.reads.append(key)
            return {"content": self.content, "version": 1}

    store = ReadOnly()
    with user_state.using(store):
        assert requirements.read_grant_status("pack", subject, digest) == {
            "grant_id": expected_id, "state": "absent", "authorized": False, "generation": 0}
        row = {"subject": subject, "digest": digest, "state": "approved", "generation": 1}
        store.content = {"schema": "astrid.grants.v1", "kind": "pack",
                         "grants": {expected_id: row}}
        assert requirements.read_grant_status("pack", subject, digest)["authorized"] is True
        row["state"] = "revoked"
        assert requirements.read_grant_status("pack", subject, digest) == {
            "grant_id": expected_id, "state": "revoked", "authorized": False, "generation": 1}
        row["digest"] = "sha256:" + "b" * 64
        assert requirements.read_grant_status("pack", subject, digest)["state"] == "unknown"
        assert store.reads == ["grants.pack"] * 4
    with user_state.using(object()):
        # An invalid transport is not an authorized grant; programming errors
        # may surface instead of being silently recast as absence.
        with pytest.raises(AttributeError):
            requirements.read_grant_status("pack", subject, digest)


def snapshot(roots):
    """Snapshot small disposable fixture roots (not real model libraries)."""
    result = {}
    for root in map(Path, roots):
        paths = [root]
        if root.is_dir() and not root.is_symlink():
            paths.extend(sorted(root.rglob("*")))
        for path in paths:
            key = str(path)
            try:
                meta = path.lstat()
            except FileNotFoundError:
                result[key] = ("absent",)
                continue
            mode = meta.st_mode
            if stat.S_ISLNK(mode):
                value = ("link", os.readlink(path))
            elif stat.S_ISREG(mode):
                value = ("file", hashlib.sha256(path.read_bytes()).hexdigest())
            elif stat.S_ISDIR(mode):
                value = ("directory",)
            else:
                raise AssertionError(f"unexpected fixture path type: {path}")
            result[key] = (*value, mode, meta.st_mtime_ns)
    return result


def test_accepted_preproducer_pin_refuses_without_plan_or_writes(tmp_path, monkeypatch, capsys):
    # Missing-dependency negative control only; positive cases use the real producer.
    monkeypatch.setitem(sys.modules, 'vibecomfy.requirements', None)
    source = tmp_path / "workflow.json"
    source.write_text("{}")
    monkeypatch.setenv("VIBECOMFY_ENTRIES_DIR", str(tmp_path / "entries"))
    before = snapshot([tmp_path])
    rc = cli.main(["requirements", str(source), "--placement", "managed", "--dry-run", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 2 and payload["status"] == "blocked"
    assert payload["issues"][0]["code"] == "missing_dependency"
    assert "plan" not in payload
    assert snapshot([tmp_path]) == before


def replay_preview(*, args, cwd, env, roots, expected, agent_sha, vibecomfy_sha, receipt):
    """Run the actual installed CLI, pin-check actual imports and compare WS2 JSON.

    Native golden cases supply exact expected envelope/digest and isolated target,
    source, entry and settings roots. Receipt must be outside those roots.
    No plan mock, fake producer or acceptance of an unverified checkout.
    """
    assert "--json" in args
    assert not any(arg == "--apply" or arg.startswith("--apply=") for arg in args)
    argv = [sys.executable, "-m", "astrid", "generate", "requirements", *args]
    import_check = (
        "import astrid, vibecomfy, runtime_protocol, json, subprocess;"
        "from pathlib import Path;"
        "mods=(astrid,vibecomfy,runtime_protocol);"
        "print(json.dumps({m.__name__:{'origin':str(Path(m.__file__).resolve()),"
        "'sha':subprocess.check_output(['git','-C',str(Path(m.__file__).resolve().parent),"
        "'rev-parse','HEAD'],text=True).strip()} for m in mods}))"
    )
    observed = subprocess.run([sys.executable, "-c", import_check], cwd=cwd, env=env,
                              capture_output=True, text=True, timeout=30, check=True)
    imports = json.loads(observed.stdout)
    assert imports["astrid"]["sha"] == agent_sha
    assert imports["vibecomfy"]["sha"] == vibecomfy_sha
    before = snapshot(roots)
    result = subprocess.run(argv, cwd=cwd, env=env, capture_output=True,
                            text=True, timeout=120, check=False)
    after = snapshot(roots)
    payload = json.loads(result.stdout)
    receipt.write_text(json.dumps({
        "argv": argv, "exit": result.returncode, "json": payload, "stderr": result.stderr,
        "digest": payload.get("plan", {}).get("digest"), "imports": imports,
        "before": before, "after": after,
    }, indent=2, sort_keys=True) + "\n")
    assert before == after
    assert result.returncode == (2 if expected["status"] == "blocked" else 0)
    assert payload == expected
    assert payload["plan"]["digest"] == expected["plan"]["digest"]
    return payload


# Real WS2 fixture files are taken from the exact dependency checkout, not
# copied into Agent or regenerated from our output.
VC_SHA = "3ed74f2c3dbd782afdd8702d22b7c83b1c40b82a"
GOLDEN_DIGESTS = {
    "sam": "742426424e951083b122c769993afcd79c36425562b1c9cbe8115945183332bd",
    "local": "f6498495c5b33f6bf0bcb9d7548c63788c470b8a37074da2d9c17a8e4aa3c53d",
}
GOLDEN_BLOBS = {
    "sam": "24c7b4473aea095c628911e588941301063ae2d5",
    "local": "ace51a6c8b2c174ae8851c3b21997d628795ec3e",
}
SAM_REPO = "https://github.com/ethanfel/ComfyUI-SAM-Audio.git"
SAM_COMMIT = "3cfbd03da3019c7252ba13efd5694b1b2ff487e5"


@pytest.fixture
def vc_fixtures():
    import vibecomfy
    import vibecomfy.requirements as producer

    checkout = Path(vibecomfy.__file__).resolve().parent.parent
    assert Path(producer.__file__).resolve().is_relative_to(checkout)
    # Do not accept an installed fallback package or a partial fixture copy.
    actual = subprocess.check_output(["git", "-C", str(checkout), "rev-parse", "HEAD"], text=True).strip()
    assert actual == os.environ.get("WS9_EXPECTED_VC_SHA", VC_SHA), "use the exact manager-selected WS2 source"
    assert not subprocess.check_output(["git", "-C", str(checkout), "status", "--porcelain", "--untracked-files=no"])
    folder = checkout / "tests" / "fixtures" / "requirements"
    for case, blob in GOLDEN_BLOBS.items():
        path = folder / f"{case}.expected-plan.json"
        payload = path.read_bytes()
        assert hashlib.sha1(b"blob " + str(len(payload)).encode() + b"\0" + payload).hexdigest() == blob
    return folder


@pytest.mark.parametrize("case", ["local", "sam"])
def test_complete_accepted_fixture_normal_and_blocked_transport(vc_fixtures, case):
    import copy
    from vibecomfy.requirements import plan_digest
    from astrid.core.contracts.errors import redact_credential_data

    value = json.loads((vc_fixtures / f"{case}.expected-plan.json").read_text())
    assert value["digest"] == plan_digest(value) == GOLDEN_DIGESTS[case]
    assert redact_credential_data(value) == value
    normal = requirements.preview_envelope(value, entry=None, target=value["target"])
    assert json.loads(json.dumps(normal))["plan"] == value
    # Force a blocking error even for the local no-op. This is a transport
    # check, not a claim that the local producer reported a real gap.
    issue = {"code": "transport_regression", "severity": "error",
             "message": "Fixture transport check", "details": {}}
    with pytest.raises(GenerationReadinessError) as exc:
        requirements.preview_envelope(value, entry=None, target=value["target"], issues=[issue])
    blocked = json.loads(json.dumps(exc.value.to_envelope()))
    assert blocked["plan"] == value and blocked["status"] == "blocked"
    assert generation_error(blocked).to_envelope()["plan"] == value
    assert plan_digest(blocked["plan"]) == GOLDEN_DIGESTS[case]
    negative = copy.deepcopy(value)
    negative["credential"] = "fixture-only-not-a-real-credential"
    negative["digest"] = plan_digest(negative)
    assert redact_credential_data(negative)["credential"] == "<redacted>"
    with pytest.raises(ValueError, match="non-public plan data"):
        requirements.preview_envelope(negative, entry=None, target=value["target"])
    if case == "sam":
        old = copy.deepcopy(value)
        node, = [row for row in old["requirements"] if row["kind"] == "node"]
        assert node["approval"]["consent_code"] == "07a48e72110d"
        node["approval"]["token"] = node["approval"].pop("consent_code")
        old["digest"] = plan_digest(old)
        assert old["digest"] == "51dad55cfbc57570a8f22d89901cc54a461f416298a277a71bace6483c241a43"
        assert redact_credential_data(old) != old
        with pytest.raises(ValueError, match="non-public plan data"):
            requirements.preview_envelope(old, entry=None, target=old["target"])


def _read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def _recorded_install(root):
    # Same receipt-construction pattern as the existing test_targets metadata
    # fixture, using the real selected recipe/plan and canonical record writer.
    # This is a CPU path-selection fixture, not an installed GPU environment.
    from dataclasses import asdict
    from astrid.core.generation.engine_contracts import DownloadResult, HealthEvidence, InstallReceipt, StepReceipt
    from astrid.core.generation.engine_local_endpoint import record_engine_root
    from astrid.core.generation.engine_recipe import load_model_lock, load_recipe
    from astrid.core.generation.engine_runner import plan_install

    recipe = load_recipe("comfyui", "linux-x86_64-cu130")
    plan = plan_install(recipe, root=str(root), placement="local")
    models = tuple(DownloadResult(m.name, str(root / "models" / m.destination),
        "skipped", m.bytes, m.sha256, True, 0) for m in load_model_lock(recipe.models_lock).models)
    steps = tuple(StepReceipt(s.id, s.kind, "executed", "passed", {}) for s in plan.steps)
    health = HealthEvidence("healthy", "2026-10-31T12:00:01Z", "a" * 64, True)
    receipt = InstallReceipt("astrid.engine-install.v1", "owned-install", plan.engine,
        plan.platform, plan.placement, plan.root, "installed", False, plan.recipe_sha256,
        plan.models_lock_sha256, {e.name: "b" * 64 for e in recipe.environments},
        {s.name: s.pin.revision for s in recipe.sources}, models, steps, health,
        "2026-10-31T12:00:00Z", "2026-10-31T12:00:01Z", None)
    record_engine_root("comfyui", str(root))
    receipt_path = root / ".astrid-engine/install.json"
    receipt_path.parent.mkdir(parents=True)
    receipt_path.write_text(json.dumps(asdict(receipt)))
    env = next(row for row in recipe.environments if row.name == recipe.primary_environment)
    python = root / env.relative_path / "bin/python"
    python.parent.mkdir(parents=True)
    python.symlink_to(Path(sys.executable).resolve())
    (root / "models").mkdir()
    return python


@pytest.fixture
def command_case(tmp_path, monkeypatch, vc_fixtures):
    from types import SimpleNamespace
    from astrid.core import user_state
    from astrid.core.generation import asset_alignment
    from vibecomfy.node_packs import _install
    from vibecomfy import custom_node_refs
    from vibecomfy.requirements import _sources
    import httpx

    root = tmp_path.resolve()
    home = root / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("VIBECOMFY_ENTRIES_DIR", str(root / "entries"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(root / "cache"))
    monkeypatch.delenv("VIBECOMFY_COMFYUI_URL", raising=False)
    monkeypatch.chdir(root)
    managed = root / "selected-target"
    python = _recorded_install(managed)
    # Keep the custom_nodes directory absent: capacity is genuinely unknown
    # for the selected paths, not an arbitrary mocked disk size.
    target = asset_alignment.selected_target()
    assert target.root == managed and Path(target.python) == python
    assert target.models_root.is_dir() and not target.custom_nodes_root.exists()

    def no_network(*args, **kwargs):
        pytest.fail("golden command must not access HTTP or download anything")

    monkeypatch.setattr(httpx.Client, "send", no_network)
    monkeypatch.setattr(_install, "get_known_node_packs", lambda **kwargs: [])
    monkeypatch.setattr(_install, "resolve_node_packs", lambda *args, **kwargs: [])
    # Same recorded immutable upstream evidence as the producer golden test;
    # actual plan, source-ref normalization, file observation and digest run.
    git_calls = []
    run = subprocess.run
    import astrid
    import vibecomfy
    import vibecomfy.requirements as producer
    import runtime_protocol
    from astrid.core.generation import preflight
    imports = {}
    for module, package in ((requirements, astrid), (cli, astrid), (preflight, astrid),
                            (producer, vibecomfy), (runtime_protocol, runtime_protocol)):
        path = Path(module.__file__).resolve()
        checkout = Path(package.__file__).resolve().parent.parent
        sha = subprocess.check_output(["git", "-C", str(checkout), "rev-parse", "HEAD"], text=True).strip()
        tracked = subprocess.check_output(["git", "-C", str(checkout), "show",
                                           "HEAD:" + path.relative_to(checkout).as_posix()])
        assert tracked == path.read_bytes()
        imports[module.__name__] = {"sha": sha, "origin": str(path),
                                   "file_sha256": hashlib.sha256(tracked).hexdigest()}
    if "WS9_EXPECTED_AGENT_SHA" in os.environ:
        assert imports[requirements.__name__]["sha"] == os.environ["WS9_EXPECTED_AGENT_SHA"]
    if "WS9_EXPECTED_RUNTIME_SHA" in os.environ:
        assert imports[runtime_protocol.__name__]["sha"] == os.environ["WS9_EXPECTED_RUNTIME_SHA"]

    def recorded_run(argv, **kwargs):
        if argv and argv[0] == "git" and "ls-remote" in argv:
            assert argv == [
                "git", "-c", "protocol.allow=never", "-c", "protocol.https.allow=always",
                "ls-remote", "--exit-code", "--", SAM_REPO,
                "refs/heads/main", "refs/tags/main", "refs/tags/main^{}",
            ]
            assert kwargs["check"] is True and kwargs["timeout"] == 30
            git_calls.append(list(argv))
            return subprocess.CompletedProcess(argv, 0, (vc_fixtures / "sam.ref.txt").read_text(), "")
        return run(argv, **kwargs)

    def inspect_source(repo, commit, **kwargs):
        assert (repo, commit) == (SAM_REPO, SAM_COMMIT)
        assert kwargs.get("requirements_txt") in (None, "requirements.txt")
        return _read_json(vc_fixtures / "sam.source-inspection.json")

    monkeypatch.setattr(custom_node_refs.subprocess, "run", recorded_run)
    monkeypatch.setattr(_sources, "inspect_source", inspect_source)

    class ReadOnlyStore:
        def __init__(self):
            self.reads = []

        def get_user_app_state(self, key):
            self.reads.append(key)
            assert key == "generation"
            return {"content": None, "version": 0}

    store = ReadOnlyStore()
    with user_state.using(store):
        yield SimpleNamespace(root=root, managed=managed, python=python, target=target,
                              files=vc_fixtures, store=store, git_calls=git_calls, imports=imports)


def _command(case, capsys, *argv):
    from vibecomfy.requirements import plan_digest
    from astrid.core.generation.preflight import generation_error

    before = snapshot([case.root])
    rc = cli.main(list(argv))
    capture = capsys.readouterr()
    after = snapshot([case.root])
    assert after == before, "declined/read-only preview changed selected state"
    payload = json.loads(capture.out)
    assert "plan" in payload, capture.err
    value = payload["plan"]
    assert value["digest"] == plan_digest(value)
    assert payload["target"] == value["target"]
    assert rc == (2 if payload["status"] == "blocked" else 0)
    if rc == 2:
        assert generation_error(payload).to_envelope() == payload
        assert all(fix["who"] in ("human", "agent") for fix in payload["fixes"])
        # Every actual producer gap must be retained, in addition to any
        # consumer-specific stale-code or transport observation gap.
        assert all(any(issue["details"].get("gap") == gap for issue in payload["issues"])
                   for gap in value["gaps"])
    # Native proof can archive pytest -s output alongside JUnit. Do not put
    # receipts under observed source/target roots.
    with capsys.disabled():
        print("WS9_COMMAND_RECEIPT", json.dumps({
            "argv": ["astrid", "generate", *argv], "exit": rc, "json": payload,
            "digest": value["digest"], "unchanged": after == before,
            "before": before, "after": after, "imports": case.imports,
        }, sort_keys=True))
    return payload


def _local_inputs(case):
    model = _read_json(case.files / "local.model.json")
    payload = (case.files / "local-model-only-payload.bin").read_bytes()
    assert len(payload) == model["size_bytes"] == 36
    assert hashlib.sha256(payload).hexdigest() == model["sha256"]
    dest = case.target.models_root / model["subdir"] / model["name"]
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(payload)
    source = case.root / "local.api.json"
    source.write_bytes((case.files / "local.workflow.api.json").read_bytes())
    addition = f"{model['subdir']}/{model['name']}={model['url']}#{model['sha256']}"
    return source, dest, addition


def test_actual_local_cli_preview_and_decline(command_case, capsys):
    from vibecomfy.requirements import plan
    case = command_case
    source, dest, addition = _local_inputs(case)
    args = ["requirements", str(source), "--placement", "managed", "--add-model", addition, "--json"]
    first = _command(case, capsys, *args, "--dry-run")
    second = _command(case, capsys, *args)  # default is equally read-only
    assert first == second
    value = first["plan"]
    assert first["status"] == "ready" and not value["gaps"] and not value["actions"]
    assert value == plan(source, target=requirements.selected_target("managed"), add_models=[addition])
    item, = value["requirements"]
    assert item["destination"] == str(dest)
    assert item["observed"]["size_bytes"] == 36
    assert item["observed"]["sha256"] == _read_json(case.files / "local.model.json")["sha256"]
    assert value["bytes"] == {"download": 0, "known_download": 0, "unknown": [],
                              "additional_disk": 0, "capacity": None}
    assert value["target"]["root"] == str(case.managed)
    assert value["target"]["python"] == str(case.python)
    assert value["target"]["models_root"] == str(case.target.models_root)
    assert value["target"]["custom_nodes_root"] == str(case.target.custom_nodes_root)
    assert value["target"]["recipe"]["receipt_sha256"] == case.target.receipt_sha256
    # No approval, apply, launch or publish action follows the person's decline.
    assert not (case.root / "entries").exists()


def test_actual_local_cli_missing_capacity(command_case, capsys):
    case = command_case
    source, dest, addition = _local_inputs(case)
    dest.unlink()
    value = _command(case, capsys, "requirements", str(source), "--placement", "managed",
                     "--add-model", addition, "--dry-run", "--json")["plan"]
    assert value["bytes"]["capacity"] is None
    assert value["bytes"]["additional_disk"] is None
    assert value["bytes"]["download"] is None  # raw --add-model has no declared size
    assert {"target_capacity_unknown", "required_disk_unknown", "model_size_unknown"} <= {
        gap["code"] for gap in value["gaps"]}
    assert not dest.exists()


def test_actual_local_cli_root_and_interpreter_changes_invalidate_digest(command_case, capsys):
    import shutil
    case = command_case
    source, dest, addition = _local_inputs(case)
    args = ["requirements", str(source), "--placement", "managed", "--add-model", addition, "--dry-run", "--json"]
    first = _command(case, capsys, *args)["plan"]
    alternate = case.root / "second-python"
    shutil.copy2(Path(sys.executable).resolve(), alternate)
    case.python.unlink()
    case.python.symlink_to(alternate)
    second = _command(case, capsys, *args)["plan"]
    assert second["target"]["python"] == first["target"]["python"]
    assert second["target_observed"]["python"]["executable"] == str(alternate)
    assert second["digest"] != first["digest"]
    moved = case.root / "other-target"
    changed_python = _recorded_install(moved)
    from astrid.core.generation import asset_alignment
    actual = asset_alignment.selected_target()
    new_dest = actual.models_root / "checkpoints" / dest.name
    new_dest.parent.mkdir(parents=True)
    new_dest.write_bytes(dest.read_bytes())
    third = _command(case, capsys, *args)["plan"]
    assert third["target"]["root"] == str(moved)
    assert third["target"]["python"] == str(changed_python)
    assert third["digest"] not in (first["digest"], second["digest"])


def test_actual_sam_cli_unapproved_then_code_recheck(command_case, capsys):
    from vibecomfy.requirements import plan
    from vibecomfy.node_approvals import approval_record, consent_token
    case = command_case
    source = case.root / "sam.api.json"
    source.write_bytes((case.files / "sam.workflow.api.json").read_bytes())
    args = ["requirements", str(source), "--placement", "managed", "--add", SAM_REPO + "@main", "--dry-run", "--json"]
    first = _command(case, capsys, *args)
    assert first["status"] == "blocked"
    value = first["plan"]
    assert value == plan(source, target=requirements.selected_target("managed"), add=[SAM_REPO + "@main"])
    node, = [row for row in value["requirements"] if row["kind"] == "node"]
    assert node["source"] == {"repo": SAM_REPO, "commit": SAM_COMMIT}
    assert node["source_tree"] == _read_json(case.files / "sam.source-inspection.json")
    assert node["action"]["install_py"] == "never"
    assert node["approval"]["state"] == "needs_approval"
    record = approval_record(node["node"], placement_class="local")
    code = consent_token(record)
    assert node["approval"]["record"] == record
    assert node["approval"]["consent_code"] == code
    assert node["approval"]["runtime_grant"] == "not_observed"
    assert len([p for p in node["packages"] if p["origin"] == "requirements"]) == 9
    # The raw flag did not explicitly supply the golden recipe's three ordered
    # no-deps pins. Keep that difference visible, do not manufacture them.
    assert not node["recipe"]["pip_no_deps"]
    assert {"node_code_unapproved", "package_resolution_unknown", "target_capacity_unknown",
            "required_disk_unknown"} <= {gap["code"] for gap in value["gaps"]}
    fix = next(f for f in first["fixes"] if "code=" + code in f["instruction"]["then_argv"])
    assert fix["who"] == "human" and fix["scope"] == "read_only"
    assert SAM_REPO + "@" + SAM_COMMIT in fix["instruction"]["then_argv"]
    assert "files, credentials and network" in fix["instruction"]["step"]
    assert "code=" + code in fix["instruction"]["step"]
    for invalid in ("code", "code=000000000000"):
        refused = _command(case, capsys, *args, "--consent", invalid)
        assert refused["status"] == "blocked"
        assert any(i["details"]["gap"]["code"] == "stale_code_consent" for i in refused["issues"])
        assert refused["plan"] == value
    confirmed = _command(case, capsys, *args, "--consent", "code=" + code)
    assert confirmed["status"] == "blocked"  # unknown packages/bytes are not approval
    assert confirmed["plan"]["digest"] != value["digest"]
    approved, = [row for row in confirmed["plan"]["requirements"] if row["kind"] == "node"]
    assert approved["approval"]["state"] == "record_matches"
    assert approved["approval"]["runtime_grant"] == "not_observed"
    assert not (case.root / "entries").exists()
    assert case.git_calls


def test_actual_runpod_sam_no_machine_no_mutation(command_case, capsys):
    case = command_case
    source = case.root / "sam.api.json"
    source.write_bytes((case.files / "sam.workflow.api.json").read_bytes())
    result = _command(case, capsys, "requirements", str(source), "--placement", "runpod",
                      "--add", SAM_REPO + "@main", "--dry-run", "--json")
    value = result["plan"]
    assert result["status"] == "blocked"
    assert value["target"]["placement_class"] == "runpod" and value["target"]["root"] is None
    assert value["target"]["python"] is None
    assert {"remote_apply_out_of_scope", "node_code_unapproved"} <= {g["code"] for g in value["gaps"]}
    assert value["bytes"]["capacity"] is None


def test_entry_manifest_and_p56_read_without_executing_python(command_case, capsys):
    from vibecomfy.node_approvals import approval_record
    from astrid.core.generation.node_approvals import write_entry_approvals
    case = command_case
    directory = case.root / "entries" / "saved"
    bundle = directory / "bundle"
    bundle.mkdir(parents=True)
    marker = case.root / "must-not-exist"
    workflow = bundle / "workflow.py"
    workflow.write_text(f"from pathlib import Path\nPath({str(marker)!r}).write_text('executed')\n")
    manifest = directory / "entry.json"
    manifest.write_text(json.dumps({"id": "saved", "kind": "audio", "modes": [],
                                    "bundle": "bundle", "needs": [], "binds": {}, "defaults": {}}))
    record = approval_record({"repo": SAM_REPO, "commit": SAM_COMMIT}, placement_class="local")
    write_entry_approvals(directory, [record])
    selected = requirements.resolve_source("saved")
    assert selected.path == manifest
    assert selected.approvals == [record]
    result = _command(case, capsys, "requirements", "saved", "--placement", "managed", "--dry-run", "--json")
    assert result["status"] == "blocked" and result["entry"] == {"id": "saved"}
    assert result["plan"]["workflow"]["kind"] == "entry"
    assert result["plan"]["inputs"]["approvals"] == [record]
    assert "workflow_code_not_admitted" in {g["code"] for g in result["plan"]["gaps"]}
    assert not marker.exists()


@pytest.mark.parametrize("addition", [
    "https://name:fixture-only-password@example.org/pack.git",
    "https://example.org/pack.git?credential=fixture-only-password",
])
def test_public_cli_refuses_credential_sources_before_producer(command_case, capsys, addition):
    case = command_case
    source = case.root / "source.json"
    source.write_bytes((case.files / "local.workflow.api.json").read_bytes())
    before = snapshot([case.root])
    rc = cli.main(["requirements", str(source), "--placement", "managed", "--add", addition, "--json"])
    result = capsys.readouterr()
    assert rc == 2 and json.loads(result.out)["status"] == "blocked"
    assert "fixture-only-password" not in result.out + result.err
    assert not case.git_calls and snapshot([case.root]) == before


def test_auto_legacy_settings_selection_is_read_only(command_case, monkeypatch):
    from astrid.core.generation import settings, targets
    case = command_case
    config = case.root / "legacy.toml"
    config.write_text('[placement]\nkind = "local"\n')
    monkeypatch.setattr(targets, "config_path", lambda: config)
    before = snapshot([case.root])
    target = requirements.selected_target("auto")
    assert target["root"] == str(case.managed)
    assert case.store.reads == [settings.KEY]
    assert snapshot([case.root]) == before


@pytest.mark.parametrize("case_name", ["local", "sam"])
def test_complete_canonical_golden_producer_and_transport(command_case, case_name):
    """Replay the exact richer WS2 fixture inputs and compare the entire plan.

    The CLI form has a raw URL/hash or repo/ref, not a structured model size
    or an explicitly authored no-deps recipe. The separate actual CLI cases
    above retain that real difference; this checks the exact canonical contract
    with the reviewed complete fixtures, without adding hidden CLI options.
    """
    import copy
    from vibecomfy.requirements import plan, plan_digest

    case = command_case
    root = case.root / "golden-target"
    root.mkdir()
    python = str(Path(sys.executable).parent.resolve() / Path(sys.executable).name)
    target = {"root": str(root), "python": python, "comfyui_root": str(root / "ComfyUI"),
              "models_root": str(root / "ComfyUI" / "models"),
              "custom_nodes_root": str(root / "ComfyUI" / "custom_nodes"),
              "placement_class": "local", "location": "local", "owner": "astrid",
              "capacity_bytes": None}
    source = case.root / (case_name + ".api.json")
    source.write_bytes((case.files / (case_name + ".workflow.api.json")).read_bytes())
    if case_name == "local":
        model = _read_json(case.files / "local.model.json")
        dest = Path(target["models_root"]) / model["subdir"] / model["name"]
        dest.parent.mkdir(parents=True)
        dest.write_bytes((case.files / "local-model-only-payload.bin").read_bytes())
        options = {"add_models": [model]}
        flags = ["--add-model", f"{model['subdir']}/{model['name']}={model['url']}#{model['sha256']}"]
    else:
        options = {"add": [SAM_REPO + "@main"],
                   "added_node_refs": [_read_json(case.files / "sam.recipe.json")]}
        flags = ["--add", SAM_REPO + "@main"]
    before = snapshot([case.root])
    value = plan(source, target=target, **options)
    assert snapshot([case.root]) == before
    assert plan_digest(value) == value["digest"]
    executable = Path(python).resolve()
    executable_bytes = executable.read_bytes()
    assert value["target"]["python"] == python
    assert value["target_observed"]["python"] == {
        "state": "present", "size_bytes": len(executable_bytes),
        "sha256": hashlib.sha256(executable_bytes).hexdigest(), "executable": str(executable),
    }

    def portable(item):
        if isinstance(item, dict):
            return {key: portable(child) for key, child in item.items()}
        if isinstance(item, list):
            return [portable(child) for child in item]
        if isinstance(item, str):
            if item == python:
                return "${PYTHON}"
            if item == str(root) or item.startswith(str(root) + "/"):
                return "${TARGET}" + item[len(str(root)):]
        return item

    normalized = portable(copy.deepcopy(value))
    normalized["target_observed"]["python"].update(
        size_bytes="${PYTHON_SIZE}", sha256="${PYTHON_SHA256}", executable="${PYTHON_EXECUTABLE}",
    )
    normalized["digest"] = plan_digest(normalized)
    expected = _read_json(case.files / (case_name + ".expected-plan.json"))
    assert normalized == expected
    assert normalized["digest"] == GOLDEN_DIGESTS[case_name]
    args = cli._parser().parse_args(["requirements", str(source), "--placement", "managed", *flags, "--json"])
    issues, fixes = requirements._readiness(value, args)
    if case_name == "sam":
        with pytest.raises(GenerationReadinessError) as exc:
            requirements.preview_envelope(value, entry=None, target=value["target"], issues=issues, fixes=fixes)
        envelope = json.loads(json.dumps(exc.value.to_envelope()))
        assert envelope["status"] == "blocked"
        assert generation_error(envelope).to_envelope() == envelope
        assert all(f["who"] in ("agent", "human") for f in envelope["fixes"])
    else:
        envelope = requirements.preview_envelope(value, entry=None, target=value["target"], issues=issues, fixes=fixes)
        assert envelope["status"] == "ready"
    assert envelope["plan"] == value
    assert plan_digest(envelope["plan"]) == value["digest"]
    assert snapshot([case.root]) == before


def test_attached_timeout_has_typed_gap_and_never_falls_back(command_case, capsys, monkeypatch):
    from astrid.core.generation.backends.comfy_target import ComfyTargetClient
    case = command_case
    source = case.root / "timeout.api.json"
    source.write_bytes((case.files / "local.workflow.api.json").read_bytes())
    calls = []

    def timeout(self, path, **kwargs):
        calls.append(path)
        raise TimeoutError("fixture target did not answer")

    monkeypatch.setattr(ComfyTargetClient, "request_json", timeout)
    result = _command(case, capsys, "requirements", str(source),
                      "--placement", "http://127.0.0.1:8188", "--dry-run", "--json")
    assert calls == ["/system_stats"]
    assert result["status"] == "blocked"
    assert result["target"]["placement_class"] == "attached"
    assert result["target"]["root"] is None
    assert result["target"]["python"] is None
    assert any(i["details"]["gap"]["code"] == "selected_target_timeout"
               and i["details"]["state"] == "timeout" for i in result["issues"])
