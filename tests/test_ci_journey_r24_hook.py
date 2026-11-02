"""Focused synthetic tests; they do not establish Actions custody or qualification."""
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import ci_journey_r24_hook as hook
from tests.test_ci_journey_provider import success_fixture


def env():
    return {"GITHUB_ACTIONS": "true", "RUNNER_OS": "Linux", "RUNNER_NAME": "runner-1",
            "GITHUB_REPOSITORY": "banodoco/astrid-agent", "GITHUB_RUN_ID": "123",
            "GITHUB_RUN_ATTEMPT": "2", "GITHUB_JOB": "journey", "GITHUB_WORKFLOW_REF": "owner/repo/.github/workflows/journey.yml@refs/heads/main",
            "GITHUB_WORKFLOW_SHA": "a" * 40}


@pytest.mark.parametrize("mode", ["success", "nonzero", "malformed", "foreign", "missing", "changed"])
def test_invocation_bundle(tmp_path, monkeypatch, mode):
    provider, root, config, _, _ = success_fixture(tmp_path, monkeypatch)
    assert provider is hook.provider
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    for kind in ("busy", "unknown", "restoration"):
        (evidence / (kind + ".bin")).write_bytes(kind.encode())
    def run(argv, **kwargs):
        request = provider.ProviderRequest.parse(Path(argv[4]).read_bytes())
        source = next(iter(provider._SUCCESS_PRODUCER_FILES))
        result = {"schema": provider.RESULT_SCHEMA, "request_sha256": request.sha256,
                  "observations": [{"kind": k, "source": source, "evidence_sha256": hook.digest(k.encode()), "safe": True}
                                   for k in ("busy", "unknown", "restoration")]}
        if mode == "foreign": result["request_sha256"] = "f" * 64
        raw = json.dumps(result).encode() + b"\n"
        if mode == "malformed": raw = b"{"
        return SimpleNamespace(returncode=7 if mode == "nonzero" else 0, stdout=raw, stderr=b"diagnostic\n")
    monkeypatch.setattr(subprocess, "run", run)
    if mode == "missing": (evidence / "unknown.bin").unlink()
    if mode == "changed": (evidence / "unknown.bin").write_bytes(b"wrong")
    out = tmp_path / "bundle"
    result = hook.invoke(json.dumps(config).encode(), str(root), out, evidence, env())
    assert result == json.loads((out / "bundle.json").read_bytes())
    assert result["session"] == {k: v for k, v in env().items() if k != "GITHUB_ACTIONS"}
    assert result["run_id"] == json.loads((out / "request.json").read_bytes())["run_id"]
    assert result["request_sha256"] == hook.digest((out / "request.json").read_bytes())
    assert result["invoked"] and result["exit_code"] == (7 if mode == "nonzero" else 0)
    for name, sha in result["artifacts"].items(): assert hook.digest((out / name).read_bytes()) == sha
    assert (out / "stderr.bin").read_bytes() == b"diagnostic\n"
    assert result["status"] == ("structural-result-only" if mode == "success" else "rejected")
    assert result["qualification"] == "not-qualified"
    if mode == "success": assert result["result_sha256"] == hook.digest((out / "stdout.bin").read_bytes())


@pytest.mark.parametrize("key", ["GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT", "GITHUB_JOB", "GITHUB_WORKFLOW_SHA", "RUNNER_NAME", "GITHUB_REPOSITORY"])
def test_session_required(tmp_path, key):
    identity = env()
    identity.pop(key)
    with pytest.raises(ValueError): hook.invoke(b"{}", "/provider", tmp_path / "bundle", None, identity)
    assert not (tmp_path / "bundle").exists()


def test_existing_bundle_rejected(tmp_path):
    out = tmp_path / "bundle"
    out.mkdir()
    with pytest.raises(FileExistsError): hook.invoke(b"{}", "/provider", out, None, env())


def test_timeout_retains_partial_streams_and_no_exit(tmp_path, monkeypatch):
    _, root, config, _, _ = success_fixture(tmp_path, monkeypatch)
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], 150, output=b"partial-out", stderr=b"partial-err")
    monkeypatch.setattr(subprocess, "run", timeout)
    out = tmp_path / "bundle"
    result = hook.invoke(json.dumps(config).encode(), str(root), out, None, env())
    assert result["status"] == "rejected" and result["timed_out"]
    assert result["invoked"] and result["exit_code"] is None
    assert (out / "stdout.bin").read_bytes() == b"partial-out"
    assert (out / "stderr.bin").read_bytes() == b"partial-err"
