from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

LAUNCHER = Path(__file__).resolve().parents[2] / "tools" / "p4-d73-pinned-launch.sh"
HEX = {
    "agent": "a" * 40,
    "agent_tree": "b" * 40,
    "runtime": "c" * 40,
    "runtime_tree": "d" * 40,
    "app": "e" * 40,
    "app_tree": "f" * 40,
    "composition": "1" * 40,
    "composition_tree": "2" * 40,
}
AGENT_PARENTS = f"{HEX['agent']} {'3' * 40} {'4' * 40}"
COMPOSITION_VERSION = json.loads(
    (Path(__file__).resolve().parents[2] / "remotion" / "package-lock.json").read_text(
        encoding="utf-8"
    )
)["packages"]["node_modules/@banodoco/timeline-composition"]["version"]


def _write_executable(path: Path, contents: str) -> None:
    path.write_text(contents, encoding="utf-8")
    path.chmod(0o700)


def _make_fixture(tmp_path: Path) -> tuple[Path, Path, Path, Path, dict[str, str], Path]:
    agent = Path(__file__).resolve().parents[2]
    runtime = tmp_path / "runtime"
    app = tmp_path / "app"
    composition = tmp_path / "composition"
    for checkout in (runtime, app, composition):
        checkout.mkdir()

    test_paths = [
        agent / "tests/editing_conformance/test_p4_cli_sheet_v2_runtime.py",
        agent / "tests/editing_conformance/test_p4_ten_note_v2_runtime.py",
        agent / "tests/editing_conformance/test_p4_note_rerun_v2_runtime.py",
    ]

    profile = tmp_path / "profile"
    (profile / "node_modules").mkdir(parents=True)
    (profile / "package-lock.json").write_text("{}\n", encoding="utf-8")
    (profile / ".astrid-registry-state.json").write_text("{}\n", encoding="utf-8")
    archive = tmp_path / "composition.tgz"
    archive.write_bytes(b"composition archive fixture\n")
    (profile / "package.json").write_text(
        json.dumps({
            "dependencies": {"@banodoco/timeline-composition": "file:../composition.tgz"},
        }),
        encoding="utf-8",
    )
    schema = tmp_path / "schema"
    schema.mkdir()
    preflight_marker = tmp_path / "preflight-env"

    fake_git = tmp_path / "git"
    git_mapping = {
        str(agent): (HEX["agent"], HEX["agent_tree"], AGENT_PARENTS),
        str(runtime): (HEX["runtime"], HEX["runtime_tree"], ""),
        str(app): (HEX["app"], HEX["app_tree"], ""),
        str(composition): (HEX["composition"], HEX["composition_tree"], ""),
    }
    _write_executable(
        fake_git,
        "#!/usr/bin/env python3\n"
        "import sys\n"
        f"mapping = {git_mapping!r}\n"
        "if sys.argv[1] != '-C': raise SystemExit(2)\n"
        "path, command = sys.argv[2], sys.argv[3:]\n"
        "sha, tree, parents = mapping[path]\n"
        "if command == ['rev-parse', 'HEAD']: print(sha)\n"
        "elif command == ['rev-parse', 'HEAD^{tree}']: print(tree)\n"
        "elif command == ['rev-list', '--parents', '-n', '1', 'HEAD']: print(parents)\n"
        "elif command == ['status', '--porcelain', '--untracked-files=all']: pass\n"
        "else: raise SystemExit(3)\n",
    )
    fake_python = tmp_path / "python"
    _write_executable(
        fake_python,
        "#!/usr/bin/env python3\n"
        "import sys\n"
        f"with open({str(preflight_marker)!r}, 'a', encoding='utf-8') as marker:\n"
        "    marker.write(f'PYTHONNOUSERSITE={__import__(\"os\").environ.get(\"PYTHONNOUSERSITE\")!r}\\n')\n"
        "if len(sys.argv) == 3 and sys.argv[1:2] == ['-P'] and sys.argv[2] == '-':\n"
        "    raise SystemExit(0)\n"
        "if len(sys.argv) == 4 and sys.argv[1:3] == ['-P', '-']:\n"
        f"    print({str(archive)!r})\n"
        "    raise SystemExit(0)\n"
        "raise SystemExit(0)\n",
    )
    fake_node = tmp_path / "node"
    _write_executable(fake_node, "#!/bin/sh\nprintf '%s\\n' v20.19.4\n")

    original_hash = hashlib.sha256(
        subprocess.check_output(["sha256sum", *(str(path) for path in test_paths)])
    ).hexdigest()
    values = {
        "format": "p4-d73-v1",
        "agent_sha": HEX["agent"],
        "agent_tree": HEX["agent_tree"],
        "agent_parents": AGENT_PARENTS,
        "agent_path": str(agent),
        "runtime_sha": HEX["runtime"],
        "runtime_tree": HEX["runtime_tree"],
        "runtime_path": str(runtime),
        "app_sha": HEX["app"],
        "app_tree": HEX["app_tree"],
        "app_path": str(app),
        "composition_sha": HEX["composition"],
        "composition_tree": HEX["composition_tree"],
        "composition_path": str(composition),
        "python": str(fake_python),
        "node": str(fake_node),
        "profile": str(profile),
        "schema": str(schema),
        "node_version": "v20.19.4",
        "profile_lock_sha": hashlib.sha256((profile / "package-lock.json").read_bytes()).hexdigest(),
        "profile_archive_sha": hashlib.sha256(archive.read_bytes()).hexdigest(),
        "profile_registry_sha": hashlib.sha256((profile / ".astrid-registry-state.json").read_bytes()).hexdigest(),
        "composition_version": COMPOSITION_VERSION,
        "original_p4_test_hash": original_hash,
        "service_versions": "launcher-control",
        "junit_paths": str(tmp_path / "junit.xml"),
    }
    manifest = tmp_path / "manifest"
    manifest.write_text("".join(f"{key}={value}\n" for key, value in values.items()), encoding="utf-8")
    return (
        agent,
        runtime,
        profile,
        manifest,
        {"PATH": f"{tmp_path}:{os.environ['PATH']}"},
        preflight_marker,
    )


def _run_launcher(
    *, agent: Path, runtime: Path, profile: Path, manifest: Path, evidence: Path, request_id: str,
    command: Path, env: dict[str, str], cwd: Path,
) -> subprocess.CompletedProcess[str]:
    manifest_values = dict(
        line.split("=", 1) for line in manifest.read_text(encoding="utf-8").splitlines() if "=" in line
    )
    launch_env = {
        **env,
        "HOME": "/private/home",
        "PYTHONNOUSERSITE": "1",
        "D73_UNKNOWN": "preserved",
        "P4_D73_EVIDENCE": str(evidence),
        "P4_D73_REQUEST_ID": request_id,
    }
    return subprocess.run(
        [str(agent / "tools" / LAUNCHER.name), str(manifest), "dry", "--", str(command)],
        cwd=cwd,
        env={
            **launch_env,
            "EXPECTED_AGENT": str(agent),
            "EXPECTED_RUNTIME": str(runtime),
            "EXPECTED_PROFILE": str(profile),
            "EXPECTED_NODE": manifest_values["node"],
        },
        capture_output=True,
        text=True,
        check=False,
    )


def test_launcher_clears_rejected_startup_key_and_preserves_private_origins(tmp_path: Path) -> None:
    agent, runtime, profile, manifest, env, preflight_marker = _make_fixture(tmp_path)
    evidence = tmp_path / "positive-evidence"
    marker = tmp_path / "positive.marker"
    command = tmp_path / "positive-command"
    _write_executable(
        command,
        "#!/bin/sh\n"
        "test -z \"${PYTHONNOUSERSITE+x}\"\n"
        "test \"$ASTRID_RUNTIME_CHECKOUT\" = \"$EXPECTED_RUNTIME\"\n"
        "test \"$ASTRID_RUNTIME_SOURCE_ROOT\" = \"$EXPECTED_RUNTIME\"\n"
        "test \"$ASTRID_REMOTION_PROJECT_DIR\" = \"$EXPECTED_PROFILE\"\n"
        "test \"$ASTRID_NODE_EXECUTABLE\" = \"$EXPECTED_NODE\"\n"
        "test \"$ASTRID_TEST_NODE_MODULES\" = \"$EXPECTED_PROFILE/node_modules\"\n"
        "test \"$PYTHONPATH\" = \"$EXPECTED_AGENT:$EXPECTED_RUNTIME\"\n"
        "test \"$D73_UNKNOWN\" = preserved\n"
        "test \"$PWD\" = \"$EXPECTED_AGENT\"\n"
        f"printf 'called\\n' >> {marker!s}\n"
        "exit 17\n",
    )
    result = _run_launcher(
        agent=agent, runtime=runtime, profile=profile, manifest=manifest,
        evidence=evidence, request_id="positive", command=command, env=env, cwd=agent,
    )
    assert result.returncode == 17, result.stderr
    assert marker.read_text(encoding="utf-8").splitlines() == ["called"]
    assert preflight_marker.read_text(encoding="utf-8").splitlines() == [
        "PYTHONNOUSERSITE=None",
        "PYTHONNOUSERSITE=None",
    ]
    assert (evidence / "positive.preflight.exit").read_text(encoding="utf-8").strip() == "0"
    assert (evidence / "positive.body.exit").read_text(encoding="utf-8").strip() == "17"


def test_launcher_rejects_poisoned_manifest_before_body(tmp_path: Path) -> None:
    agent, runtime, profile, manifest, env, _ = _make_fixture(tmp_path)
    poisoned = tmp_path / "poisoned-manifest"
    poisoned.write_text(
        manifest.read_text(encoding="utf-8").replace(f"agent_sha={HEX['agent']}", "agent_sha=" + "0" * 40),
        encoding="utf-8",
    )
    evidence = tmp_path / "poisoned-evidence"
    marker = tmp_path / "poisoned.marker"
    command = tmp_path / "poisoned-command"
    _write_executable(command, f"#!/bin/sh\nprintf 'called\\n' >> {marker!s}\nexit 17\n")
    result = _run_launcher(
        agent=agent, runtime=runtime, profile=profile, manifest=poisoned,
        evidence=evidence, request_id="poisoned", command=command, env=env, cwd=agent,
    )
    assert result.returncode != 0
    assert not marker.exists()
    assert not (evidence / "poisoned.body.log").exists()
    assert not (evidence / "poisoned.body.exit").exists()
