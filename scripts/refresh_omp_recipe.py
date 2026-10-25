#!/usr/bin/env python3
"""Refresh the reviewed fork's build closure; users run neither this script nor Git.

python scripts/refresh_omp_recipe.py --producer-repo /path/to/oh-my-pi \
    --candidate astrid/18.12.6 --version 18.12.6 --cache /tmp/omp-archives

The selected commit is authoritative: the installer never applies a patch.
Review the candidate separately; this recipe pins its lock, Bun and native leaves.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import subprocess
import tarfile
import urllib.request
from pathlib import Path

TARGETS = {"linux-x64": "linux-x64", "linux-arm64": "linux-arm64", "macos-x64": "darwin-x64",
           "macos-arm64": "darwin-arm64", "windows-x64": "win32-x64"}


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--producer-repo", required=True, type=Path)
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--bun-version", default="1.3.14")
    parser.add_argument("--cache", required=True, type=Path)
    parser.add_argument("--output", type=Path,
                        default=Path(__file__).resolve().parents[1] / "astrid/installer/omp")
    args = parser.parse_args()
    args.cache.mkdir(parents=True, exist_ok=True)
    args.output.mkdir(parents=True, exist_ok=True)

    def git(*argv):
        return subprocess.check_output(["git", "-C", str(args.producer_repo), *argv])

    candidate = git("rev-parse", "--verify", args.candidate + "^{commit}").decode().strip()
    package = json.loads(git("show", candidate + ":packages/coding-agent/package.json"))
    if package["version"] != args.version:
        raise ValueError("Candidate version does not match the requested native release.")
    recipe = {"schema_version": 1, "version": args.version, "bun_version": args.bun_version,
              "bun_lock_sha256": sha(git("show", candidate + ":bun.lock")), "native": {}}
    for target, tag in TARGETS.items():
        package = "@oh-my-pi/pi-natives-" + tag
        with urllib.request.urlopen(f"https://registry.npmjs.org/{package}/{args.version}", timeout=120) as response:
            metadata = json.load(response)
        dist = metadata["dist"]
        if metadata["name"] != package or metadata["version"] != args.version or not dist["tarball"].startswith("https://"):
            raise ValueError("Unexpected native package identity or transport.")
        archive = args.cache / (package.rsplit("/", 1)[1] + f"-{args.version}.tgz")
        if not archive.exists():
            with urllib.request.urlopen(dist["tarball"], timeout=120) as response, archive.open("wb") as output:
                while chunk := response.read(1024 * 1024):
                    output.write(chunk)
        content = archive.read_bytes()
        integrity = "sha512-" + base64.b64encode(hashlib.sha512(content).digest()).decode()
        if integrity != dist["integrity"]:
            raise ValueError(f"Native SRI mismatch: {package}")
        with tarfile.open(archive) as bundle:
            files = sorted(member.name.removeprefix("package/") for member in bundle
                           if member.isfile() and member.name.endswith(".node"))
        baseline = f"pi_natives.{tag}-baseline.node"
        modern = f"pi_natives.{tag}-modern.node"
        valid = (files in ([baseline], sorted([baseline, modern])) if tag.endswith("-x64")
                 else files == [f"pi_natives.{tag}.node"])
        if not valid:
            raise ValueError(f"Unexpected native variants: {package}: {files}")
        recipe["native"][target] = {"url": dist["tarball"], "sha256": sha(content),
                                    "integrity": integrity, "files": files}
    (args.output / "recipe.json").write_text(json.dumps(recipe, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"candidate": candidate, "output": str(args.output)}, indent=2))


if __name__ == "__main__":
    main()
