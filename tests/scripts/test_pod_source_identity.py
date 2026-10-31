"""Offline source-bundle custody and Runtime identity regressions."""
import importlib.util
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile

import pytest

from tests.helpers.runtime import use_runtime


SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "pod_session.py"
spec = importlib.util.spec_from_file_location("pod_source_identity_session", SCRIPT)
ps = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = ps
spec.loader.exec_module(ps)


def git(root, *args):
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
    return subprocess.run(
        ["git", "-C", str(root), *args], check=True, capture_output=True, text=True, env=env
    ).stdout.strip()


@pytest.fixture
def repos(tmp_path):
    sources = tmp_path / "sources"
    sources.mkdir()
    heads = {}
    for name in ps.BUNDLE_TREES:
        root = sources / name
        root.mkdir()
        git(root, "init", "-q")
        git(root, "config", "user.name", "Fixture")
        git(root, "config", "user.email", "fixture@example.invalid")
        git(root, "config", "fixture.sensitive", "do-not-copy-local-config")
        (root / ".gitignore").write_text("ignored-local\n")
        (root / "tracked.txt").write_text(f"tracked {name}\n")
        (root / "tests").mkdir()
        (root / "tests" / "tracked_test.py").write_text("# tracked and shipped\n")
        git(root, "add", ".")
        git(root, "commit", "-qm", "fixture")
        heads[name] = git(root, "rev-parse", "HEAD")
        (root / "ignored-local").write_text("ignored, not shipped")
        (root / ".git" / "hooks" / "private-hook").write_text("do-not-copy-local-hooks")
    # Commit the producer first so the consumer lock pins its exact final HEAD.
    producer = sources / "astrid-runtime"
    artifact = b"fixture producer artifact\n"
    (producer / "contract").mkdir()
    (producer / "contract" / "artifact.txt").write_bytes(artifact)
    artifact_hash = "sha256:" + hashlib.sha256(artifact).hexdigest()
    manifest = {"producer": "astrid-runtime", "version": "fixture-v1",
                "artifacts": {"contract/artifact.txt": artifact_hash}}
    (producer / "contract" / "artifacts.json").write_text(json.dumps(manifest))
    git(producer, "add", "contract")
    git(producer, "commit", "-qm", "producer manifest")
    heads["astrid-runtime"] = git(producer, "rev-parse", "HEAD")
    agent = sources / "astrid-agent"
    (agent / "vendored.txt").write_bytes(artifact)
    lock = {"producer": "astrid-runtime", "manifest": "contract/artifacts.json",
            "producer_version": "fixture-v1", "source_commit": heads["astrid-runtime"],
            "files": {"vendored.txt": {"from": "contract/artifact.txt", "sha256": artifact_hash}}}
    (agent / "runtime.lock.json").write_text(json.dumps(lock))
    git(agent, "add", "vendored.txt", "runtime.lock.json")
    git(agent, "commit", "-qm", "vendor lock")
    heads["astrid-agent"] = git(agent, "rev-parse", "HEAD")
    return sources, heads


def profile(root):
    from astrid_runtime.bootstrap import SourceProfile
    return SourceProfile(profile="astrid", runtime_checkout=str(root / "astrid-runtime"),
                         source_checkout=str(root / "astrid-agent"))


def unpack(bundle, dest):
    dest.mkdir()
    with tarfile.open(bundle, "r:gz") as tar:
        tar.extractall(dest, filter="data")


@pytest.fixture
def runtime(monkeypatch):
    monkeypatch.delenv("ASTRID_INSTALL_RELEASE", raising=False)
    monkeypatch.delenv("ASTRID_INSTALL_MANAGED_BY", raising=False)
    use_runtime()
    return importlib.import_module("astrid_runtime.bootstrap")


def test_original_gitless_bundle_reproduces_retained_failure(repos, tmp_path, runtime):
    """The previous tar filter dropped .git, making the real capture fail."""
    from astrid.sdk.autobootstrap import AutoBootstrapError, _lifecycle_result

    sources, _ = repos
    old = tmp_path / "old"
    old.mkdir()
    for name in ("astrid-runtime", "astrid-agent"):
        shutil.copytree(sources / name, old / name, ignore=shutil.ignore_patterns(".git", "ignored-local"))
    with pytest.raises(runtime.BootstrapError, match="Requested Runtime identity could not be verified") as exc:
        runtime._capture_requested_identity(profile(old))
    with pytest.raises(AutoBootstrapError) as translated:
        _lifecycle_result({"ok": False, "error": {"message": str(exc.value)}}, action="bootstrap",
                          started=0, allowed_statuses=frozenset({"ready"}))
    assert str(translated.value) == (
        "neutral runtime bootstrap was not ready: Requested Runtime identity could not be verified.; "
        "run the Astrid install line again"
    )


@pytest.mark.parametrize("linked_agent", [False, True], ids=["checkout", "linked-worktree"])
def test_bundle_preserves_exact_clean_source_identity_and_owner(repos, tmp_path, runtime, linked_agent):
    from astrid_runtime.identity import validate_live_owner_identity

    sources, heads = repos
    agent_root = sources / "astrid-agent"
    if linked_agent:
        linked = tmp_path / "linked-agent"
        git(agent_root, "worktree", "add", "--detach", str(linked), heads["astrid-agent"])
        assert (linked / ".git").is_file()
        agent_root = linked
    from astrid_runtime.bootstrap import SourceProfile
    requested = runtime._capture_requested_identity(SourceProfile(
        profile="astrid", runtime_checkout=str(sources / "astrid-runtime"), source_checkout=str(agent_root)))
    assert requested["dirty"] is False
    bundle = tmp_path / "bundle.tgz"
    ps.build_bundle(agent_root, sources, bundle)
    dest = tmp_path / "unpacked"
    unpack(bundle, dest)
    for name in ps.BUNDLE_TREES:
        root = dest / name
        assert (root / ".git").is_dir() and not (root / ".git").is_symlink()
        assert git(root, "rev-parse", "HEAD") == heads[name]
        git(root, "fsck", "--full", "--strict")
        assert git(root, "rev-list", "--count", "HEAD") == "1"
        assert git(root, "status", "--porcelain", "--untracked-files=all") == ""
        assert not (root / "ignored-local").exists()
        assert not (root / ".git" / "hooks" / "private-hook").exists()
        assert "do-not-copy-local-config" not in (root / ".git" / "config").read_text()
        for tracked in git(sources / name, "ls-files").splitlines():
            assert (root / tracked).read_bytes() == (sources / name / tracked).read_bytes()
    captured = runtime._capture_requested_identity(profile(dest))
    assert captured == requested
    owner = {"runtime_instance_id": "fixture-owner", "identity": captured}
    assert validate_live_owner_identity(owner, instance_id="fixture-owner") == requested
    assert runtime._validated_matching_owner_identity(owner, instance_id="fixture-owner", requested_identity=requested) == requested
    for invalid in (None, {"runtime_instance_id": "other", "identity": captured},
                    {"runtime_instance_id": "fixture-owner", "identity": {**captured, "runtime_head": "0" * 40}},
                    {"runtime_instance_id": "fixture-owner", "identity": {**captured, "dirty_hash": "0" * 64}}):
        with pytest.raises(runtime.BootstrapError):
            runtime._validated_matching_owner_identity(invalid, instance_id="fixture-owner", requested_identity=requested)
    shutil.rmtree(dest / "astrid-runtime" / ".git")
    with pytest.raises(runtime.BootstrapError, match="Requested Runtime identity could not be verified"):
        runtime._capture_requested_identity(profile(dest))


@pytest.mark.parametrize("name", ps.BUNDLE_TREES)
@pytest.mark.parametrize("dirt", ["tracked", "staged", "untracked"])
def test_bundle_rejects_dirty_source_repositories(repos, tmp_path, name, dirt):
    sources, _ = repos
    root = sources / name
    if dirt == "untracked":
        (root / "untracked").write_text("untracked")
    else:
        (root / "tracked.txt").write_text("changed")
        if dirt == "staged":
            git(root, "add", "tracked.txt")
    with pytest.raises(ps.SessionError):
        ps.build_bundle(sources / "astrid-agent", sources, tmp_path / "bundle.tgz")


@pytest.mark.parametrize("name", ps.BUNDLE_TREES)
def test_bundle_rejects_missing_git(repos, tmp_path, name):
    sources, _ = repos
    shutil.rmtree(sources / name / ".git")
    with pytest.raises(ps.SessionError):
        ps.build_bundle(sources / "astrid-agent", sources, tmp_path / "bundle.tgz")


def test_bundle_rejects_nested_agent_path(repos, tmp_path):
    sources, _ = repos
    with pytest.raises(ps.SessionError):
        ps.build_bundle(sources / "astrid-agent" / "tests", sources, tmp_path / "bundle.tgz")


@pytest.fixture
def verifier_bundle(repos, tmp_path, runtime):
    sources, _ = repos
    archive = tmp_path / "verified-bundle.tgz"
    ps.build_bundle(sources / "astrid-agent", sources, archive)
    root = tmp_path / "verified-sources"
    unpack(archive, root)
    profile_path = tmp_path / "source-profile.json"
    profile_path.write_text(json.dumps({"profile": "astrid", "runtime_checkout": str(root / "astrid-runtime"),
                                        "source_checkout": str(root / "astrid-agent")}))
    spec = importlib.util.spec_from_file_location("pod_source_identity_verifier", SCRIPT.with_name("pod_source_identity.py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module, root, profile_path


def test_verifier_accepts_matching_bundle_and_profile(verifier_bundle, runtime):
    verifier, root, profile_path = verifier_bundle
    assert verifier.verify(root, profile_path) == runtime._capture_requested_identity(profile(root))


@pytest.mark.parametrize("mutation", ["missing_manifest", "stale_head", "missing_lock", "stale_lock",
                                       "changed_source", "profile_elsewhere", "install_release", "install_managed"])
def test_verifier_rejects_untrusted_identity_evidence(verifier_bundle, tmp_path, monkeypatch, mutation):
    verifier, root, profile_path = verifier_bundle
    manifest_path = root / ".pod-source.json"
    lock_path = root / "astrid-agent" / "runtime.lock.json"
    if mutation == "missing_manifest":
        manifest_path.unlink()
    elif mutation == "stale_head":
        manifest = json.loads(manifest_path.read_text())
        manifest["heads"]["astrid-runtime"] = "0" * 40
        manifest_path.write_text(json.dumps(manifest))
    elif mutation == "missing_lock":
        lock_path.unlink()
    elif mutation == "stale_lock":
        lock = json.loads(lock_path.read_text())
        lock["source_commit"] = "0" * 40
        lock_path.write_text(json.dumps(lock))
    elif mutation == "changed_source":
        (root / "astrid-runtime" / "tracked.txt").write_text("changed after packing")
    elif mutation == "profile_elsewhere":
        data = json.loads(profile_path.read_text())
        data["runtime_checkout"] = str(tmp_path / "other-runtime")
        profile_path.write_text(json.dumps(data))
    elif mutation == "install_release":
        monkeypatch.setenv("ASTRID_INSTALL_RELEASE", "1")
    elif mutation == "install_managed":
        monkeypatch.setenv("ASTRID_INSTALL_MANAGED_BY", "pinokio")
    with pytest.raises(ValueError):
        verifier.verify(root, profile_path)


def test_sdk_strict_handshake_with_captured_source_identity(verifier_bundle, monkeypatch):
    from astrid.sdk.client import AstridClient
    from astrid_runtime_client.contract_metadata import SCHEMA_DIGEST

    verifier, root, profile_path = verifier_bundle
    identity = verifier.verify(root, profile_path)
    calls = []

    class FakeWorkspace:
        def __init__(self, endpoint, token):
            assert (endpoint, token) == ("http://127.0.0.1:1", "token")

        def health(self):
            calls.append("health")
            return {"status": "ok", "protocol": "workspace.v1", "identity": identity,
                    "schema_digest": SCHEMA_DIGEST, "runtime_epoch": 1,
                    "runtime_instance_id": "runtime-1", "runtime_session_id": "session-1"}

        def handshake(self, *args):
            calls.append("handshake")
            return {"protocol": "workspace.v1", "schema_digest": SCHEMA_DIGEST,
                    "session_id": "session", "realm_id": "realm", "actor_id": "actor",
                    "scopes": ["projects:read", "projects:write", "objects:read", "objects:write",
                               "tasks:read", "tasks:write"],
                    "capabilities": ["execution_binding.targeted.v1"]}

    monkeypatch.setattr("astrid.sdk.workspace_client.resolve_runtime_connection", lambda endpoint, credential: (endpoint, credential))
    monkeypatch.setattr("astrid.sdk.workspace_client.WorkspaceClient", FakeWorkspace)
    monkeypatch.setattr("astrid.sdk.autobootstrap.ensure_runtime", lambda: pytest.fail("unexpected bootstrap"))
    AstridClient.open(endpoint="http://127.0.0.1:1", credential="token", realm_id="realm", actor_id="actor",
                      client_name="test", client_version="1", protocol_version="workspace.v1")
    assert calls == ["health", "handshake"]
