"""WS9 Agent contract tests and real-producer receipt replay helper.

The envelope tests deliberately exercise serialization only, not a replacement
requirements producer. The golden local/SAM cases will call replay_preview with
the actual WS2/RS1 fixture and candidate pin; no synthetic plan is returned by
the public CLI on the accepted pre-producer pin.
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
    # Draft-only assertion: replace with real positive/blocked producer cases
    # before READY; never monkeypatch requirements.plan into existence.
    source = tmp_path / "workflow.json"
    source.write_text("{}")
    monkeypatch.setenv("VIBECOMFY_ENTRIES_DIR", str(tmp_path / "entries"))
    before = snapshot([tmp_path])
    rc = cli.main(["requirements", str(source), "--placement", "managed", "--dry-run", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 2 and payload["status"] == "blocked"
    assert payload["issues"][0]["code"] in ("missing_dependency", "requirements_contract_pending")
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
