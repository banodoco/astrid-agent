import importlib.util
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from tests.lanes import unavailable

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("upgrade_release", ROOT / "scripts/upgrade_release.py")
upgrade_release = importlib.util.module_from_spec(spec)
assert spec.loader
spec.loader.exec_module(upgrade_release)


def local_repo(name):
    value = os.environ.get(name)
    if not value:
        unavailable(f"set {name} to the local source checkout to run this offline check")
    return Path(value)


@pytest.mark.app_checkout
def test_live_served_observation_resolves_exact_local_manifest_commit_offline(tmp_path):
    app = local_repo("ASTRID_APP_REPO")
    fixture = ROOT / "tests/upgrade/fixtures/served-observation.json"
    evidence = json.loads(fixture.read_text())
    result = upgrade_release.resolve("served", tmp_path / "served", app_repo=app, observation_fixture=fixture)
    assert evidence["source_url"] == "https://astrid.haus/install-manifest.json"
    assert evidence["manifest_sha256"] == "fe12781e4da60d441595c08f84cb8d0390d840ab7c71227b8c4eff791553795e"
    assert result["status"] == "verified"
    assert result["app_manifest_commit"] == "d174f667aa04a7c43e6b0f767abb168b6022a6ab"
    assert result["served_evidence"]["evidence_mode"] == "fixture"
    assert (tmp_path / "served" / "served-observation.json").is_file()
    assert result["local_candidate_used"] is False


@pytest.mark.app_checkout
def test_pre_updater_resolves_from_local_app_history_and_exports_bootstraps(tmp_path):
    app = local_repo("ASTRID_APP_REPO")
    result = upgrade_release.resolve("pre-updater", tmp_path / "pre-updater", app_repo=app)
    assert result["status"] == "verified"
    assert result["app_manifest_commit"] == "b96c41fca2da7993b41324aedf61eb297dc34f10"
    assert result["identity"]["agent"] == "710e9c0aae306adf3113cd239b1bb8883093b275"
    assert result["identity"]["runtime"] == "11443b0b5b34bd5b36725504fb083146192a759d"
    assert all((tmp_path / "pre-updater" / row["path"]).is_file() for row in result["bootstrap"])


def test_equal_release_tuples_collapse_to_one_leg():
    item = {"identity": {"agent": "a", "runtime": "r", "schema_digest": "s"}}
    assert upgrade_release.dedupe_legs([item, item]) == [item]


@pytest.mark.app_checkout
def test_unavailable_live_endpoint_stays_unverified_without_candidate_fallback(tmp_path, monkeypatch):
    app = local_repo("ASTRID_APP_REPO")

    def unavailable(*_args, **_kwargs):
        raise OSError("fixture simulates endpoint unavailable")

    monkeypatch.setattr(upgrade_release, "manifest_observation", unavailable)
    result = upgrade_release.resolve("served", tmp_path / "served-unavailable", app_repo=app)
    assert result["status"] == "environmental-unverified"
    assert result["local_candidate_used"] is False
    assert not (tmp_path / "served-unavailable" / "install-manifest.json").exists()


def _commit_repo(root):
    subprocess.run(["git", "-C", str(root), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)
    subprocess.run(["git", "-C", str(root), "-c", "user.name=TST", "-c", "user.email=tst@example.invalid", "commit", "-qm", "source fixture"], check=True)


def _source_fixtures(tmp_path, runtime_repo, agent_repo):
    runtime = tmp_path / "runtime"
    agent = tmp_path / "agent"
    for repo, files in ((runtime, ("runtime_protocol/store.py", "runtime_protocol/service.py")),
                        (agent, ("astrid/installer/core.py",))):
        for rel in files:
            target = repo / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            source = runtime_repo if "runtime_protocol" in rel else agent_repo
            target.write_bytes(upgrade_release.git(source, "show", f"HEAD:{rel}"))
        _commit_repo(repo)
    return runtime, agent


@pytest.mark.integration
def test_guard_accepts_authoritative_source_shapes():
    runtime = local_repo("ASTRID_RUNTIME_REPO")
    upgrade_release.check_format_review(runtime, ROOT)


@pytest.mark.parametrize("relative,old,new,repo_name", [
    ("runtime_protocol/store.py", "SCHEMA_VERSION = 31", "SCHEMA_VERSION = 32", "runtime"),
    ("runtime_protocol/service.py", 'journal.get("version") != 1', 'journal.get("version") != 2', "runtime"),
    ("astrid/installer/core.py", '"identity": {"release": self.release, "managed_by": self.managed_by}', '"identity": {"release": self.release, "managed_by": self.managed_by}, "format": 2', "agent"),
])
@pytest.mark.integration
def test_production_source_format_change_requires_release_review(tmp_path, relative, old, new, repo_name):
    runtime_source = local_repo("ASTRID_RUNTIME_REPO")
    runtime, agent = _source_fixtures(tmp_path, runtime_source, ROOT)
    target = (runtime if repo_name == "runtime" else agent) / relative
    source = target.read_text()
    assert old in source
    target.write_text(source.replace(old, new, 1))
    subprocess.run(["git", "-C", str(runtime if repo_name == "runtime" else agent), "add", relative], check=True)
    subprocess.run(["git", "-C", str(runtime if repo_name == "runtime" else agent), "-c", "user.name=TST", "-c", "user.email=tst@example.invalid", "commit", "-qm", "format bump"], check=True)
    with pytest.raises(ValueError, match="Production upgrade format changed without a release-list review entry"):
        upgrade_release.check_format_review(runtime, agent)
