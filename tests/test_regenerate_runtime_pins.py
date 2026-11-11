from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "regenerate_runtime_pins.py"


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


def commit_all(repo: Path) -> str:
    git(repo, "add", ".")
    git(repo, "-c", "user.name=Pin Test", "-c", "user.email=pin@example.invalid",
        "commit", "-m", "fixture")
    return git(repo, "rev-parse", "HEAD")


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


class RegenerateRuntimePinsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.root = Path(self.temp.name)
        self.agent = self.root / "agent"
        self.runtime = self.root / "runtime"
        self.app = self.root / "app"
        for repo in (self.agent, self.runtime):
            repo.mkdir()
            git(repo, "init", "-q")

        self.runtime_files = {
            "packages/python/astrid_runtime_client/generated.py": b"new client bytes\n",
            "runtime_protocol/contract_metadata.py": (
                b"SCHEMA_DIGEST = 'sha256:" + b"a" * 64 + b"'\n"
            ),
        }
        artifacts = {}
        for source, payload in self.runtime_files.items():
            path = self.runtime / source
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(payload)
            artifacts[source] = "sha256:" + hashlib.sha256(payload).hexdigest()
        write_json(self.runtime / "contract/artifacts.json", {
            "producer": "astrid-runtime", "version": "sha256:" + "b" * 64,
            "artifacts": artifacts,
        })
        self.runtime_sha = commit_all(self.runtime)
        (self.agent / "tracked.txt").write_text("tracked\n", encoding="utf-8")
        self.agent_sha = commit_all(self.agent)

        for root, target in ((self.agent, "astrid_runtime_client/generated.py"),
                             (self.app, "src/integrations/runtime/generated.py")):
            write_json(root / "runtime.lock.json", {
                "files": {target: {"from": "packages/python/astrid_runtime_client/generated.py",
                                   "sha256": "sha256:" + "0" * 64}},
                "manifest": "contract/artifacts.json", "producer": "astrid-runtime",
                "producer_version": "sha256:" + "0" * 64, "source_commit": "0" * 40,
            })
        write_json(self.app / "public/install-manifest.json", {
            "schema_version": 1, "release": 7,
            "agent": {"sha": "0" * 40},
            "runtime": {"sha": "0" * 40, "schema_digest": "sha256:" + "0" * 64},
            "tools": {"python": {"version": "3.11"}},
        })

    def tearDown(self) -> None:
        self.temp.cleanup()

    def command(self, *extra: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run([
            sys.executable, str(SCRIPT), "--runtime-sha", self.runtime_sha,
            "--agent-sha", self.agent_sha, "--runtime-checkout", str(self.runtime),
            "--agent-checkout", str(self.agent), "--app-checkout", str(self.app), *extra,
        ], capture_output=True, text=True, env={**os.environ, "TMPDIR": str(self.root)})

    def test_default_dry_run_shows_proposal_without_mutating_checkouts(self) -> None:
        agent_lock = (self.agent / "runtime.lock.json").read_bytes()
        app_lock = (self.app / "runtime.lock.json").read_bytes()
        manifest = (self.app / "public/install-manifest.json").read_bytes()
        result = self.command()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--- a/", result.stdout)
        self.assertIn("Dry run only", result.stdout)
        self.assertFalse((self.agent / "astrid_runtime_client/generated.py").exists())
        self.assertFalse((self.app / "src/integrations/runtime/generated.py").exists())
        self.assertEqual((self.agent / "runtime.lock.json").read_bytes(), agent_lock)
        self.assertEqual((self.app / "runtime.lock.json").read_bytes(), app_lock)
        self.assertEqual((self.app / "public/install-manifest.json").read_bytes(), manifest)

    def test_apply_vendors_both_consumers_and_updates_metadata(self) -> None:
        result = self.command("--apply")
        self.assertEqual(result.returncode, 0, result.stderr)
        expected = self.runtime_files["packages/python/astrid_runtime_client/generated.py"]
        self.assertEqual((self.agent / "astrid_runtime_client/generated.py").read_bytes(), expected)
        self.assertEqual((self.app / "src/integrations/runtime/generated.py").read_bytes(), expected)
        for lock_path in (self.agent / "runtime.lock.json", self.app / "runtime.lock.json"):
            lock = json.loads(lock_path.read_text())
            self.assertEqual(lock["source_commit"], self.runtime_sha)
            self.assertEqual(lock["producer_version"], "sha256:" + "b" * 64)
            self.assertEqual(lock["files"][next(iter(lock["files"]))]["sha256"],
                             "sha256:" + hashlib.sha256(expected).hexdigest())
        manifest = json.loads((self.app / "public/install-manifest.json").read_text())
        self.assertEqual(manifest["agent"]["sha"], self.agent_sha)
        self.assertEqual(manifest["runtime"]["sha"], self.runtime_sha)
        self.assertEqual(manifest["runtime"]["schema_digest"], "sha256:" + "a" * 64)
        self.assertEqual(manifest["release"], 7)
        self.assertEqual(manifest["tools"], {"python": {"version": "3.11"}})

    def test_apply_writes_the_packaged_agent_lock_identical_to_the_root_lock(self) -> None:
        result = self.command("--apply")
        self.assertEqual(result.returncode, 0, result.stderr)
        packaged = self.agent / "astrid/resources/runtime.lock.json"
        self.assertEqual(packaged.read_bytes(), (self.agent / "runtime.lock.json").read_bytes())
        self.assertEqual(json.loads(packaged.read_text())["source_commit"], self.runtime_sha)

    def test_invalid_sha_fails_before_any_write(self) -> None:
        before = (self.agent / "runtime.lock.json").read_bytes()
        result = self.command("--runtime-sha", "not-a-sha")
        self.assertEqual(result.returncode, 2)
        self.assertIn("full lowercase 40-character", result.stderr)
        self.assertEqual((self.agent / "runtime.lock.json").read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
