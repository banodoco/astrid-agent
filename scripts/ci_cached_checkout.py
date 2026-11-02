#!/usr/bin/env python3
"""Use a verified runner-owned bare cache for exact smoke checkouts, or miss."""
import os
import re
import stat
import subprocess
import sys
from pathlib import Path

UID = 503
REPOS = {name: f"https://github.com/banodoco/astrid-{name}.git" for name in ("agent", "runtime", "pinokio")}
DESTINATIONS = {"agent": "candidate-agent", "runtime": "runtime", "pinokio": "pinokio"}


def run(args, env, *, allow_failure=False):
    command = ["git", "-c", "credential.helper=", "-c", "http.extraHeader=",
               "-c", "gc.auto=0", "-c", "maintenance.auto=false", *args]
    result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=180, check=False)
    if result.returncode and not allow_failure:
        raise RuntimeError("Local Git cache checkout failed")
    return result


def valid_config(name, raw):
    allowed = {"core.repositoryformatversion": {"0"}, "core.bare": {"true"},
               "core.filemode": {"true", "false"}, "core.ignorecase": {"true", "false"},
               "core.precomposeunicode": {"true", "false"}, "remote.origin.url": {REPOS[name]},
               "remote.origin.fetch": {"+refs/heads/*:refs/remotes/origin/*"}}
    seen = set()
    for item in raw.split("\0"):
        if not item: continue
        key, sep, value = item.partition("\n")
        key = key.lower()
        if not sep or key in seen or key not in allowed or value not in allowed[key]:
            raise RuntimeError("Cache configuration is unsafe")
        seen.add(key)
    if not {"core.repositoryformatversion", "core.bare", "remote.origin.url", "remote.origin.fetch"} <= seen:
        raise RuntimeError("Cache configuration is incomplete")


def safe_cache(path):
    for part in (path.parent.parent, path.parent, path):
        st = part.lstat()
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != UID or st.st_mode & 0o077:
            raise RuntimeError("Cache directory is not private and runner-owned")
    for directory, dirs, files in os.walk(path, followlinks=False, onerror=lambda e: (_ for _ in ()).throw(e)):
        for name in dirs + files:
            st = (Path(directory) / name).lstat()
            if stat.S_ISLNK(st.st_mode) or st.st_uid != UID:
                raise RuntimeError("Cache contains a symlink or foreign-owned entry")


def checkout(name, sha, root, workspace):
    if name not in REPOS or not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise RuntimeError("Invalid checkout identity")
    repo = root / f"{name}.git"
    if not (repo.exists() or repo.is_symlink()):
        return False
    safe_cache(repo)
    destination = workspace / DESTINATIONS[name]
    if destination.is_symlink():
        raise RuntimeError("Checkout destination is a symlink")
    if destination.exists():
        if not destination.is_dir():
            raise RuntimeError("Checkout destination is not a directory")
        # Let checkout's established cleanup handle an existing worktree.
        return False
    # No inherited token, helper, user config, SSH settings or XDG config.
    env = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "HOME": str(root / "git-home"),
           "XDG_CONFIG_HOME": str(root / "git-home"), "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_CONFIG_SYSTEM": "/dev/null", "GIT_CONFIG_GLOBAL": "/dev/null",
           "GIT_TERMINAL_PROMPT": "0"}
    config = run(["-C", str(repo), "config", "--local", "--no-includes", "--null", "--list"], env).stdout
    valid_config(name, config)
    if run(["-C", str(repo), "rev-parse", "--is-bare-repository"], env).stdout.strip() != "true":
        raise RuntimeError("Cache is not bare")
    present = run(["-C", str(repo), "cat-file", "-e", f"{sha}^{{commit}}"], env, allow_failure=True)
    if present.returncode:
        return False
    # Copy objects instead of leaving a mutable alternates dependency.
    run(["clone", "--local", "--no-hardlinks", "--no-checkout", str(repo), str(destination)], env)
    run(["-C", str(destination), "checkout", "--detach", sha], env)
    got = run(["-C", str(destination), "rev-parse", "HEAD"], env).stdout.strip()
    if got != sha:
        raise RuntimeError("Cache checkout did not resolve the frozen commit")
    run(["-C", str(destination), "remote", "set-url", "origin", REPOS[name]], env)
    return True


def main():
    if os.getuid() != UID or os.environ.get("RUNNER_NAME") != "hannah-mac":
        raise RuntimeError("Cached checkout requires the dedicated runner")
    if len(sys.argv) != 3:
        raise RuntimeError("Invalid cache checkout arguments")
    root = Path.home() / ".astrid-mac-bundle-cache" / "bundle-a-v1"
    workspace = Path(os.environ["GITHUB_WORKSPACE"])
    if not workspace.is_absolute() or workspace != workspace.resolve():
        raise RuntimeError("Checkout workspace path is invalid")
    hit = checkout(sys.argv[1], sys.argv[2], root, workspace)
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
        print(f"hit={str(hit).lower()}", file=output)
    print("Frozen checkout: cache hit" if hit else "Frozen checkout: cache miss; use network checkout")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        print("Cached checkout refused; no unsafe fallback after a partial checkout", file=sys.stderr)
        raise SystemExit(1)
