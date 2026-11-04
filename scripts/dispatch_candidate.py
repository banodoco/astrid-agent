#!/usr/bin/env python3
"""Publish an App pinned to Agent and Runtime SHAs and emit a relay input."""
import argparse
import hashlib
import json
import re
import subprocess
import tempfile
from pathlib import Path

ARTIFACTS = ("public/install", "public/install.sh", "public/install.ps1")
PINOKIO = "3a6a98ae251ca126505245286e2d3e760f4348e9"
OLD = "bbba620e3d89eeff8021417f365e6ea584203105"


def run(*args, cwd=None):
    return subprocess.check_output(args, cwd=cwd, text=True).strip()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("agent")
    parser.add_argument("runtime")
    parser.add_argument("row", choices=("direct-linux", "direct-macos", "direct-windows", "direct"))
    parser.add_argument("name", help="unique filename without manager prefix")
    parser.add_argument("--mac-runner", choices=("hosted", "astrid-mac"), default="hosted")
    parser.add_argument("--workspace", type=Path, required=True, help="manager workspace containing repos/astrid-app and outbox/inputs")
    parser.add_argument("--lane", required=True, help="manager lane, e.g. M-QUAL")
    args = parser.parse_args()
    if args.mac_runner == "astrid-mac" and args.row != "direct-macos":
        parser.error("astrid-mac requires the direct-macos diagnostic row")
    if not all(re.fullmatch(r"[0-9a-f]{40}", s) for s in (args.agent, args.runtime)):
        parser.error("agent and runtime must be full lowercase SHAs")
    if not re.fullmatch(r"M-[A-Z0-9]+", args.lane):
        parser.error("lane must match M- followed by uppercase letters or digits")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+\.json", args.name) or args.name.startswith(args.lane + "-"):
        parser.error("name must be a JSON filename without the manager prefix")
    output = args.workspace / "outbox/inputs" / args.name
    app = args.workspace / "repos/astrid-app"
    if output.exists():
        parser.error(f"input already exists: {output}")
    remote = run("git", "-C", str(app), "remote", "get-url", "origin")
    with tempfile.TemporaryDirectory(prefix="qual-app-") as tmp:
        run("git", "clone", "--quiet", "--shared", "--no-checkout", str(app), tmp)
        run("git", "fetch", "--quiet", remote, "refs/heads/int/r1", cwd=tmp)
        run("git", "checkout", "--quiet", "--detach", "FETCH_HEAD", cwd=tmp)
        path = Path(tmp) / "public/install-manifest.json"
        manifest = json.loads(path.read_text(encoding="utf-8"))
        manifest["agent"]["sha"] = args.agent
        manifest["runtime"]["sha"] = args.runtime
        path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        run("git", "add", "public/install-manifest.json", cwd=tmp)
        run("git", "-c", "user.name=Astrid merge", "-c", "user.email=noreply@anthropic.com", "commit", "--quiet", "--allow-empty", "-m", "Pin diagnostic candidate", cwd=tmp)
        sha = run("git", "rev-parse", "HEAD", cwd=tmp)
        branch = f"lane/{args.lane}/candidate-{sha}"
        run("git", "push", "--quiet", remote, f"HEAD:refs/heads/{branch}", cwd=tmp)
        manifest_bytes = path.read_bytes()
        artifacts = {p: hashlib.sha256((Path(tmp) / p).read_bytes()).hexdigest() for p in ARTIFACTS}
    candidate = {"app": sha, "agent": args.agent, "runtime": args.runtime,
                 "pinokio": PINOKIO, "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
                 "artifacts": artifacts}
    inputs = {"candidate": json.dumps(candidate, separators=(",", ":")), "old_launcher_ref": OLD,
              "stopped_path_agent_ref": args.agent, "direct_only": True, "row": args.row}
    if args.mac_runner != "hosted":
        inputs["mac_runner"] = args.mac_runner
    with output.open("x", encoding="utf-8") as f:
        json.dump(inputs, f, separators=(",", ":"))
        f.write("\n")
    print(f"Published App {sha} ({branch})")
    print(f"ACTIONS: astrid-agent journey.yml int/r1 @inputs/{args.lane}-{args.name}")


if __name__ == "__main__":
    main()
