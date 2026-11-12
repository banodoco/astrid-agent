"""NON-PROOF helper for the release1-then-update diagnostic job (macOS, default paths).

Subcommands:
  wait-health  wait for the selected Runtime's /v1/health and print it (no credentials)
  seed         create a project with a timeline through the live Runtime API
  redact       scrub credential-shaped values from collected evidence in place
  pack-snapshot OUT  record the managed Hivemind registration and checkout identity
  verdict E    PASS only for a clean Release 1 -> 12 update that left Hivemind untouched

The credential file is read only to authenticate the seed request; it is never
printed, copied or written to evidence.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from urllib.request import ProxyHandler, build_opener

sys.path.insert(0, str(Path(__file__).resolve().parent))
import journey_live_workspace as live  # noqa: E402

DATA0 = Path.home() / "Library" / "Application Support" / "Astrid Runtime"
DATA1 = Path(str(DATA0) + "-r1")
SENSITIVE_KEY = re.compile(r"(?i)(token|secret|password|authorization|api[_-]?key|cookie|bearer)")
SENSITIVE_TEXT = re.compile(
    r"(?i)(bearer\s+)[^\s\"',;]+"
    r"|((?:\"?(?:token|authorization|password|secret|api[_-]?key|cookie)\"?)\s*[:=]\s*\"?)(?!/)[^\s\"',;]{8,}"
    r"|\b(?:gh[pousr]_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9_-]{20,})\b")


def active_data() -> Path:
    for data in (DATA1, DATA0):
        if (data / "runtime" / "discovery.json").is_file():
            return data
    raise SystemExit("no runtime/discovery.json under either default data root")


def wait_health(timeout: float) -> int:
    opener = build_opener(ProxyHandler({}))
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            data = active_data()
            endpoint = json.loads((data / "runtime" / "discovery.json").read_text(encoding="utf-8"))["endpoint"]
            with opener.open(endpoint.rstrip("/") + "/v1/health", timeout=10) as response:
                health = json.loads(response.read())
            if health.get("status") == "ok":
                print(json.dumps({"data_root": str(data), "endpoint": endpoint, "health": health}, sort_keys=True))
                return 0
            last = health
        except Exception as exc:  # diagnostic loop: keep the last reason
            last = f"{type(exc).__name__}: {exc}"
        time.sleep(3)
    print(json.dumps({"health": "not-ready", "last": str(last)}), file=sys.stderr)
    return 1


def seed(generation: str) -> int:
    data = active_data()
    discovery = json.loads((data / "runtime" / "discovery.json").read_text(encoding="utf-8"))
    token = live.credential_token(Path(discovery["credential_file"]))
    client = live.LiveWorkspace(discovery["endpoint"], token)
    result = live.seed(client, "tst-r1-update-" + generation, generation, create_project=True)
    print(json.dumps({"data_root": str(data), **result}, sort_keys=True))
    return 0


def _scrub(value):
    if isinstance(value, dict):
        return {key: ("<redacted>" if SENSITIVE_KEY.search(str(key)) and not str(key).endswith(("_file", "_path", "_dir"))
                      and isinstance(item, str) else _scrub(item)) for key, item in value.items()}
    if isinstance(value, list):
        return [_scrub(item) for item in value]
    return value


def redact(root: Path) -> int:
    for path in sorted(root.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        name = path.name.lower()
        if any(word in name for word in ("credential", "token", "secret")) or name.endswith((".key", ".pem")):
            path.unlink()
            print(f"removed credential-named file: {path.relative_to(root)}")
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if path.suffix == ".json":
            try:
                text = json.dumps(_scrub(json.loads(text)), indent=2, sort_keys=True) + "\n"
            except ValueError:
                pass
        text = SENSITIVE_TEXT.sub(lambda m: (m.group(1) or m.group(2) or "") + "<redacted>", text)
        path.write_text(text, encoding="utf-8")
    return 0


PACK_STATE = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state") / "astrid" / "default" / "pack-sources.json"
PACK_DATA = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "astrid" / "packs"
UPDATED_LINE = "Astrid updated: Release 1 \u2192 Release 12."


def _tree_digest(root: Path) -> str | None:
    if not root.is_dir():
        return None
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if relative.parts[:1] == (".git",):
            continue
        kind = "l" if path.is_symlink() else "d" if path.is_dir() else "f"
        digest.update(f"{kind} {relative.as_posix()}\n".encode())
        if kind == "f":
            digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def _git(root: Path, *argv: str) -> str | None:
    if not (root / ".git").exists():
        return None
    result = subprocess.run(["git", "-C", str(root), *argv], capture_output=True, text=True, check=False)
    return result.stdout.strip() if result.returncode == 0 else f"git exit {result.returncode}: {result.stderr.strip()}"


def pack_snapshot(out: Path) -> int:
    state = json.loads(PACK_STATE.read_text(encoding="utf-8")) if PACK_STATE.is_file() else None
    active = (state or {}).get("active", {}).get("hivemind") if isinstance(state, dict) else None
    pack_root = Path(active["pack_root"]) if isinstance(active, dict) and active.get("pack_root") else None
    snapshot = {
        "pack_sources_path": str(PACK_STATE),
        "pack_sources_sha256": hashlib.sha256(PACK_STATE.read_bytes()).hexdigest() if PACK_STATE.is_file() else None,
        "pack_sources": state,
        "active_hivemind": active,
        "pack_root": str(pack_root) if pack_root else None,
        "pack_root_exists": bool(pack_root and pack_root.is_dir()),
        "checkout_tree_sha256": _tree_digest(pack_root) if pack_root else None,
        "checkout_head": _git(pack_root, "rev-parse", "HEAD") if pack_root else None,
        "checkout_status": _git(pack_root, "status", "--porcelain", "--untracked-files=all") if pack_root else None,
        "packs_dir_listing": sorted(str(p.relative_to(PACK_DATA)) for p in PACK_DATA.glob("*/*")) if PACK_DATA.is_dir() else None,
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(snapshot, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({k: v for k, v in snapshot.items() if k != "pack_sources"}, indent=2, sort_keys=True))
    return 0


def _last_service_lines(install_log: Path) -> str:
    if not install_log.is_file():
        return f"(no install.log at {install_log})"
    lines = install_log.read_text(encoding="utf-8", errors="replace").splitlines()
    starts = [i for i, line in enumerate(lines) if "Last service log lines" in line]
    if not starts:
        return "(install.log has no 'Last service log lines' section; its last 80 lines:)\n" + "\n".join(lines[-80:])
    start = starts[-1]
    return "\n".join(lines[max(0, start - 3):start + 80])


def verdict(evidence: Path) -> int:
    failures: list[str] = []
    codes = dict(line.split("=", 1) for line in (evidence / "exit-codes.txt").read_text().split() if "=" in line) \
        if (evidence / "exit-codes.txt").is_file() else {}
    if codes.get("r12_update_exit") != "0":
        failures.append(f"install --update exit code is {codes.get('r12_update_exit')!r}, not 0")
    output = (evidence / "r12-update.out").read_text(encoding="utf-8", errors="replace") \
        if (evidence / "r12-update.out").is_file() else ""
    if UPDATED_LINE not in output:
        failures.append(f"update output lacks {UPDATED_LINE!r}")
    pack_host, deadline = None, time.monotonic() + 180
    opener = build_opener(ProxyHandler({}))
    while time.monotonic() < deadline:
        try:
            endpoint = json.loads((active_data() / "runtime" / "discovery.json").read_text(encoding="utf-8"))["endpoint"]
            with opener.open(endpoint.rstrip("/") + "/v1/health", timeout=10) as response:
                health = json.loads(response.read())
            pack_host = next((s for s in health.get("services", []) if s.get("name") == "pack-host"), None)
            if pack_host and pack_host.get("state") in {"running", "ready"}:
                break
        except Exception as exc:
            pack_host = {"health_error": f"{type(exc).__name__}: {exc}"}
        time.sleep(5)
    print("pack-host:", json.dumps(pack_host, sort_keys=True))
    if (not pack_host or pack_host.get("state") not in {"running", "ready"} or pack_host.get("restart_count") != 0
            or pack_host.get("last_exit") is not None):
        failures.append(f"pack host is not ready with restart_count 0: {pack_host}")
    snapshots = {}
    for phase in ("before", "after"):
        path = evidence / f"packs-{phase}-update.json"
        snapshots[phase] = json.loads(path.read_text()) if path.is_file() else {"missing": str(path)}
        if not path.is_file():
            failures.append(f"no managed Hivemind snapshot {path.name} (an earlier step failed)")
    before, after = snapshots["before"], snapshots["after"]
    for key in ("pack_sources_sha256", "pack_root_exists", "checkout_tree_sha256", "checkout_head", "checkout_status"):
        if before.get(key) != after.get(key):
            failures.append(f"managed Hivemind {key} changed: {before.get(key)!r} -> {after.get(key)!r}")
    if failures:
        print("FAIL")
        for failure in failures:
            print(" - " + failure)
        print("===== install.log: Last service log lines")
        print(_last_service_lines(Path.home() / "Library" / "Logs" / "Astrid Runtime" / "install.log"))
        return 1
    print("PASS: Release 1 -> 12 update exited 0, pack host running with restart_count 0, Hivemind unchanged")
    return 0


def main(argv: list[str]) -> int:
    if argv[:1] == ["pack-snapshot"] and len(argv) == 2:
        return pack_snapshot(Path(argv[1]))
    if argv[:1] == ["verdict"] and len(argv) == 2:
        return verdict(Path(argv[1]))
    if argv[:1] == ["wait-health"]:
        return wait_health(float(argv[1]) if len(argv) > 1 else 240)
    if argv[:1] == ["seed"] and len(argv) == 2:
        return seed(argv[1])
    if argv[:1] == ["redact"] and len(argv) == 2:
        return redact(Path(argv[1]))
    raise SystemExit(__doc__)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
