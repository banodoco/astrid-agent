import importlib.util
import json
import subprocess
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "dispatch-candidate.py"
SPEC = importlib.util.spec_from_file_location("dispatch_candidate", SCRIPT)
dispatch_candidate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(dispatch_candidate)


def git(*args, cwd=None):
    return subprocess.check_output(["git", *args], cwd=cwd, text=True, stderr=subprocess.STDOUT).strip()


def make_repo(tmp_path, name, files=None):
    bare = tmp_path / f"{name}.git"
    work = tmp_path / f"{name}-work"
    git("init", "--bare", "--quiet", str(bare))
    git("init", "--quiet", str(work))
    git("-C", str(work), "config", "user.name", "Test")
    git("-C", str(work), "config", "user.email", "test@example.invalid")
    fixture_files = files or {"source.txt": "source\n"}
    for rel, content in fixture_files.items():
        path = work / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    git("-C", str(work), "add", ".")
    git("-C", str(work), "commit", "--quiet", "-m", "fixture")
    git("-C", str(work), "branch", "-M", "int/r1")
    git("-C", str(work), "remote", "add", "origin", str(bare))
    git("-C", str(work), "push", "--quiet", "-u", "origin", "int/r1")
    return bare, work, git("-C", str(work), "rev-parse", "HEAD")


def test_candidate_keeps_manifest_and_relay_pins_paired(tmp_path):
    agent_remote, _, agent_sha = make_repo(tmp_path, "agent")
    runtime_remote, _, runtime_sha = make_repo(tmp_path, "runtime")
    manifest = {
        "agent": {"sha": "a" * 40},
        "runtime": {"sha": "b" * 40, "schema_digest": "sha256:test"},
    }
    app_remote, app_work, _ = make_repo(
        tmp_path,
        "app",
        {
            "public/install-manifest.json": json.dumps(manifest) + "\n",
            "public/install": "install\n",
            "public/install.sh": "#!/bin/sh\n",
            "public/install.ps1": "Write-Output install\n",
        },
    )
    output_dir = tmp_path / "inputs"
    dispatch_candidate.main(
        [
            agent_sha,
            runtime_sha,
            "direct-linux",
            "candidate.json",
            "--agent-remote",
            str(agent_remote),
            "--runtime-remote",
            str(runtime_remote),
            "--app-repo",
            str(app_work),
            "--output-dir",
            str(output_dir),
        ]
    )

    envelope = json.loads((output_dir / "candidate.json").read_text(encoding="utf-8"))
    candidate = json.loads(envelope["candidate"])
    pinned_manifest = json.loads(
        git("--git-dir", str(app_remote), "show", f"{candidate['app']}:public/install-manifest.json")
    )
    assert candidate["agent"] == pinned_manifest["agent"]["sha"] == envelope["stopped_path_agent_ref"] == agent_sha
    assert candidate["runtime"] == pinned_manifest["runtime"]["sha"] == runtime_sha
    assert envelope["row"] == "direct-linux"
    assert git("--git-dir", str(app_remote), "cat-file", "-t", candidate["app"]) == "commit"


def test_unfetchable_agent_sha_fails_before_app_push_or_input(tmp_path, capsys):
    agent_remote, _, _ = make_repo(tmp_path, "agent")
    runtime_remote, _, runtime_sha = make_repo(tmp_path, "runtime")
    app_remote, app_work, _ = make_repo(
        tmp_path,
        "app",
        {
            "public/install-manifest.json": '{"agent":{"sha":"a"},"runtime":{"sha":"b"}}\n',
            "public/install": "install\n",
            "public/install.sh": "#!/bin/sh\n",
            "public/install.ps1": "Write-Output install\n",
        },
    )
    missing_sha = "0" * 40
    before = git("--git-dir", str(app_remote), "rev-parse", "refs/heads/int/r1")
    output_dir = tmp_path / "inputs"

    try:
        dispatch_candidate.main(
            [
                missing_sha,
                runtime_sha,
                "direct-linux",
                "candidate.json",
                "--agent-remote",
                str(agent_remote),
                "--runtime-remote",
                str(runtime_remote),
                "--app-repo",
                str(app_work),
                "--output-dir",
                str(output_dir),
            ]
        )
    except SystemExit as exc:
        assert exc.code == 2
    else:
        raise AssertionError("unfetchable Agent SHA should fail closed")

    assert "cannot verify remote fetchability for Agent SHA" in capsys.readouterr().err
    assert git("--git-dir", str(app_remote), "rev-parse", "refs/heads/int/r1") == before
    assert not (output_dir / "candidate.json").exists()


def test_local_only_object_does_not_count_as_remote_fetchability(tmp_path):
    remote, work, _ = make_repo(tmp_path, "source")
    (work / "local-only.txt").write_text("not pushed\n", encoding="utf-8")
    git("-C", str(work), "add", "local-only.txt")
    git("-C", str(work), "commit", "--quiet", "-m", "local only")
    local_sha = git("-C", str(work), "rev-parse", "HEAD")

    try:
        dispatch_candidate.verify_remote_commit(str(remote), local_sha, "Agent")
    except ValueError as exc:
        assert f"cannot verify remote fetchability for Agent SHA {local_sha}" in str(exc)
    else:
        raise AssertionError("unpublished local object must not count as remotely fetchable")
