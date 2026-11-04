#!/usr/bin/env python3
"""Publish an App commit pinned to current or explicitly selected commits."""
import argparse
import hashlib
import json
import re
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ARTIFACTS = ("public/install", "public/install.sh", "public/install.ps1")
PINOKIO = "3a6a98ae251ca126505245286e2d3e760f4348e9"
OLD = "bbba620e3d89eeff8021417f365e6ea584203105"
AGENT_REMOTE = "https://github.com/banodoco/astrid-agent"
RUNTIME_REMOTE = "https://github.com/banodoco/astrid-runtime"
APP_REMOTE = "https://github.com/banodoco/astrid-app"


def run(*args, cwd=None):
    return subprocess.check_output(args, cwd=cwd, text=True, stderr=subprocess.STDOUT).strip()


def verify_remote_commit(remote, sha, label, run_command=run):
    """Fetch the exact commit in an isolated repository; local caches do not count."""
    with tempfile.TemporaryDirectory(prefix="qual-remote-probe-") as tmp:
        probe = Path(tmp) / "probe.git"
        try:
            run_command("git", "init", "--bare", "--quiet", str(probe))
            run_command("git", "-C", str(probe), "fetch", "--quiet", "--no-tags", remote, sha)
            fetched = run_command("git", "-C", str(probe), "rev-parse", "FETCH_HEAD^{commit}")
        except (OSError, subprocess.CalledProcessError) as exc:
            detail = getattr(exc, "output", "") or str(exc)
            raise ValueError(
                f"cannot verify remote fetchability for {label} SHA {sha} from {remote}: {detail.strip()}"
            ) from exc
        if fetched != sha:
            raise ValueError(
                f"remote fetchability check for {label} SHA {sha} resolved to {fetched}, not the exact requested commit"
            )


def resolve_remote_int_r1(remote, label, run_command=run):
    """Resolve the current int/r1 head from the remote, not a local cache."""
    with tempfile.TemporaryDirectory(prefix="qual-remote-head-") as tmp:
        probe = Path(tmp) / "probe.git"
        try:
            run_command("git", "init", "--bare", "--quiet", str(probe))
            run_command(
                "git", "-C", str(probe), "fetch", "--quiet", "--no-tags",
                remote, "refs/heads/int/r1",
            )
            resolved = run_command(
                "git", "-C", str(probe), "rev-parse", "FETCH_HEAD^{commit}"
            )
        except (OSError, subprocess.CalledProcessError) as exc:
            detail = getattr(exc, "output", "") or str(exc)
            raise ValueError(
                f"cannot resolve current int/r1 head for {label} from {remote}: {detail.strip()}"
            ) from exc
    if not re.fullmatch(r"[0-9a-f]{40}", resolved):
        raise ValueError(
            f"remote int/r1 head for {label} did not resolve to a full commit SHA: {resolved}"
        )
    return resolved


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "agent", nargs="?", help="explicit Agent commit; default is the current int/r1 head"
    )
    parser.add_argument(
        "runtime", nargs="?", help="explicit Runtime commit; default is the current int/r1 head"
    )
    parser.add_argument("row", choices=("direct-linux", "direct-macos", "direct-windows", "direct"))
    parser.add_argument("name", help="unique filename without manager prefix")
    parser.add_argument("--mac-runner", choices=("hosted", "astrid-mac"), default="hosted")
    parser.add_argument("--app-repo", type=Path, default=ROOT.parent / "astrid-app")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "outbox" / "inputs")
    parser.add_argument("--agent-remote", default=AGENT_REMOTE)
    parser.add_argument("--runtime-remote", default=RUNTIME_REMOTE)
    args = parser.parse_args(argv)
    if (args.agent is None) != (args.runtime is None):
        parser.error(
            "provide both explicit Agent and Runtime SHAs, or omit both to use current int/r1 heads"
        )
    if args.mac_runner == "astrid-mac" and args.row != "direct-macos":
        parser.error("astrid-mac requires the direct-macos diagnostic row")
    if args.agent is not None and not all(
        re.fullmatch(r"[0-9a-f]{40}", sha) for sha in (args.agent, args.runtime)
    ):
        parser.error("agent and runtime must be full lowercase SHAs")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+\.json", args.name) or args.name.startswith("M-QUAL-"):
        parser.error("name must be a JSON filename without the manager prefix")
    output = args.output_dir / args.name
    if output.exists():
        parser.error(f"input already exists: {output}")
    if not args.app_repo.is_dir():
        parser.error(f"App repository directory does not exist: {args.app_repo}; pass --app-repo")

    # Every int/r1 workflow candidate must follow today's int/r1 heads.
    # Explicit SHAs are accepted for caller compatibility, but cannot pin a
    # stale commit. Verify exact fetchability before touching App or creating
    # the relay input.
    try:
        current_agent = resolve_remote_int_r1(args.agent_remote, "Agent")
        current_runtime = resolve_remote_int_r1(args.runtime_remote, "Runtime")
        if args.agent is None:
            args.agent = current_agent
            args.runtime = current_runtime
        verify_remote_commit(args.agent_remote, args.agent, "Agent")
        verify_remote_commit(args.runtime_remote, args.runtime, "Runtime")
        if args.agent != current_agent:
            raise ValueError(
                f"Agent SHA {args.agent} is not current int/r1 head {current_agent}; "
                "int/r1 workflow candidates must install the current head"
            )
        if args.runtime != current_runtime:
            raise ValueError(
                f"Runtime SHA {args.runtime} is not current int/r1 head {current_runtime}; "
                "int/r1 workflow candidates must install the current head"
            )
    except ValueError as exc:
        parser.error(str(exc))

    remote = run("git", "-C", str(args.app_repo), "remote", "get-url", "origin")
    with tempfile.TemporaryDirectory(prefix="qual-app-") as tmp:
        run("git", "clone", "--quiet", "--shared", "--no-checkout", str(args.app_repo), tmp)
        run("git", "fetch", "--quiet", remote, "refs/heads/int/r1", cwd=tmp)
        run("git", "checkout", "--quiet", "--detach", "FETCH_HEAD", cwd=tmp)
        path = Path(tmp) / "public/install-manifest.json"
        manifest = json.loads(path.read_text(encoding="utf-8"))
        manifest["agent"]["sha"] = args.agent
        manifest["runtime"]["sha"] = args.runtime
        path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        run("git", "add", "public/install-manifest.json", cwd=tmp)
        run("git", "-c", "user.name=Astrid merge (M-QUAL)", "-c", "user.email=noreply@anthropic.com", "commit", "--quiet", "--allow-empty", "-m", "Pin diagnostic candidate", cwd=tmp)
        sha = run("git", "rev-parse", "HEAD", cwd=tmp)
        branch = f"lane/M-QUAL/candidate-{sha}"
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
    args.output_dir.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8") as f:
        json.dump(inputs, f, separators=(",", ":"))
        f.write("\n")
    print(f"Published App {sha} ({branch})")
    print(f"ACTIONS: astrid-agent journey.yml int/r1 @inputs/M-QUAL-{args.name}")


if __name__ == "__main__":
    main()
