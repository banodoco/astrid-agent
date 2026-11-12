"""NON-PROOF helper for the release1-then-update diagnostic job (macOS, default paths).

Subcommands:
  wait-health  wait for the selected Runtime's /v1/health and print it (no credentials)
  seed         create a project with a timeline through the live Runtime API
  redact       scrub credential-shaped values from collected evidence in place

The credential file is read only to authenticate the seed request; it is never
printed, copied or written to evidence.
"""
from __future__ import annotations

import json
import re
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


def main(argv: list[str]) -> int:
    if argv[:1] == ["wait-health"]:
        return wait_health(float(argv[1]) if len(argv) > 1 else 240)
    if argv[:1] == ["seed"] and len(argv) == 2:
        return seed(argv[1])
    if argv[:1] == ["redact"] and len(argv) == 2:
        return redact(Path(argv[1]))
    raise SystemExit(__doc__)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
