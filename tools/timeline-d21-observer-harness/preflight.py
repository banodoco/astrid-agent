"""Report-all D21 qualification checks and immutable before/after custody."""

from __future__ import annotations

import gzip
import hashlib
import importlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tarfile


HARNESS = Path(__file__).resolve().parent
ROOT = Path.cwd().resolve()
RUNTIME = Path(os.environ["ASTRID_RUNTIME_CHECKOUT"]).resolve()
PROFILE = Path(os.environ["ASTRID_REMOTION_PROJECT_DIR"]).resolve()
EXPECTED_PYTHON = Path("/home/user/work/venv-agent/bin/python")
NODE = Path(os.environ["ASTRID_NODE_EXECUTABLE"]).resolve()
EVIDENCE = Path(os.environ["D21_EVIDENCE_DIR"]).resolve()
RUN_ID = os.environ.get("D21_RUN_ID", "d21")
EXPECTED_HEAD = "f88cc5242d4b6c87b97a83878b86ec3168daa3db"
EXPECTED_PARENT = "19646f04d815900853a8892dd24f1ddefffe9c21"
EXPECTED_TREE = "c8ce72197087f4ef7dc86c979ca0c8fb43ff1581"
EXPECTED_FILES = {
    "tests/test_skip_guard.py": ("b36889aa641d07f796d26802690adba16c0b0de5", "d921aa62009a00f482bfb6a245954bde63e9dc3654f449e08743fa4d726abb78"),
    "tests/test_lanes.py": ("32c326aef17979840889fb119aaae991bf55df3f", "cb312ee03ed80f5b676494382c07e3418c9ce09935ca560f39a6bb0b7b8e52c5"),
    "tests/conftest.py": ("76b9e1567ffcb0a3f1995a656e740afdae8a598d", "ab682e260ac750b575173f6899075df33e9636bbca97b9383ac56378d94208b5"),
    "tests/lanes.py": ("48b6a6663891bf99c96d134d2c0d297f97deab93", "f086adc13fb43c8bde0c484f6af822d9bfcf315e128d0a1cd0a307579c89a487"),
    "tests/test_lane_registry.py": ("cc7eda0725dc604fcbca7c64adec61c591ad7e9e", "5ab797bc3527a484deef231b87dc119a20bb865867247263da9e91645ea7bfbb"),
}
REGISTRY_HASHES = {
    ".astrid-registry-state.json": "d609b44b37d724358a33405e9d8539008fd1ac30bc2f0d9746575c3fbadaac96",
    "src/animations.generated.ts": "02326f12177879eb5905f9d9b6d476b2de185928ddeeeeb2321a68ebffb47ad6",
    "src/effects.generated.ts": "127e5653e0c66993726f8334b9f4ac122d24cdfbaa9e70cdaa142189afcf2bbe",
    "src/transitions.generated.ts": "c46d6bbd88712e775812867ec1cbe6de67473664775266c5a0b9317344ceb989",
}
STATE_HASH = "32b312f5ea7b2a67717a79ae71b539510590e9010e2ec7f77c8bce1edbabbfc6"
ARCHIVE_HASH = "819acf6b3815d6eeabe772a9569581fedbe4f9c9cce35946efca9454e2040234"
LOCK_HASH = "f4fc0a32a8822e956dfa21ff35d19c0c83d147c754cdb3546f91c84e330be18d"
ARCHIVE_NAME = "artifacts/banodoco-timeline-composition-635f4de06fda22a85fc2e4cfc764d0153a4c9f40.tgz"
ORIGINAL_FULL_BODY_SHA256 = "5ebb51b8d7100a60658595176fac538d8248e9e2a85e082dc7c7feffd16bec71"

failures: list[str] = []
lines: list[str] = []


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def git(*args: str, cwd: Path = ROOT) -> str:
    return subprocess.check_output(["git", *args], cwd=cwd, text=True).strip()


def command(args: list[str]) -> str:
    result = subprocess.run(args, capture_output=True, text=True, timeout=120, check=False)
    assert result.returncode == 0, f"exit={result.returncode}\n{result.stdout}\n{result.stderr}"
    return result.stdout.strip()


def report(label: str, fn) -> None:
    try:
        value = fn()
        assert value is not False, "comparison failed"
        line = f"PASS {label}: {value}"
    except Exception as exc:  # report all checks before returning
        failures.append(label)
        line = f"FAIL {label}: {type(exc).__name__}: {exc}"
    print(line, flush=True)
    lines.append(line)


def source_records(root: Path) -> bytes:
    records = []
    for name in git("ls-files", "-z", cwd=root).split("\0"):
        if not name:
            continue
        path = root / name
        record = {"path": name, "symlink": os.readlink(path) if path.is_symlink() else None}
        if not path.is_symlink():
            record["sha256"] = digest(path)
        records.append(record)
    payload = (json.dumps({"root": str(root), "head": git("rev-parse", "HEAD", cwd=root)}, sort_keys=True) + "\n").encode()
    payload += b"".join(json.dumps(record, sort_keys=True).encode() + b"\n" for record in records)
    return payload


def profile_records() -> bytes:
    records = []
    for path in [PROFILE, *sorted(PROFILE.rglob("*"))]:
        info = path.lstat()
        record = {"path": str(path.relative_to(PROFILE)), "mode": stat.S_IMODE(info.st_mode), "type": stat.S_IFMT(info.st_mode)}
        if path.is_symlink():
            record.update(link=os.readlink(path), resolved=str(path.resolve()))
        elif path.is_file():
            record.update(size=info.st_size, sha256=digest(path))
        records.append(record)
    return b"".join(json.dumps(record, sort_keys=True).encode() + b"\n" for record in records)


def save_gzip(name: str, payload: bytes) -> None:
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    (EVIDENCE / name).write_bytes(gzip.compress(payload, mtime=0))


def capture(phase: str) -> bytes:
    source = source_records(ROOT)
    profile = profile_records()
    save_gzip(f"{RUN_ID}-source-{phase}.jsonl.gz", source)
    save_gzip(f"{RUN_ID}-profile-{phase}.jsonl.gz", profile)
    return source + b"\0PROFILE\0" + profile


def source_lock() -> dict[str, dict[str, str]]:
    actual = {
        path: {"git_blob": git("rev-parse", f"HEAD:{path}"), "sha256": digest(ROOT / path)}
        for path in EXPECTED_FILES
    }
    expected = {path: {"git_blob": blob, "sha256": sha} for path, (blob, sha) in EXPECTED_FILES.items()}
    assert actual == expected, {"actual": actual, "expected": expected}
    return actual


def qualify_profile() -> dict[str, object]:
    installed = PROFILE / "node_modules/@banodoco/timeline-composition"
    with tarfile.open(PROFILE / ARCHIVE_NAME) as archive:
        members = [member for member in archive.getmembers() if member.isfile()]
        assert len(members) == 80
        for member in members:
            actual = digest(installed / member.name.removeprefix("package/"))
            expected = hashlib.sha256(archive.extractfile(member).read()).hexdigest()
            assert actual == expected, member.name
    regular, links = [], []
    for path in PROFILE.rglob("*"):
        if path.is_symlink():
            links.append(path)
        elif path.is_file():
            regular.append(path)
        else:
            assert path.is_dir(), path
    for name, expected in REGISTRY_HASHES.items():
        assert digest(PROFILE / name) == expected, name
    state = json.loads((PROFILE / ".astrid-registry-state.json").read_text())
    assert state == {"version": 1, "hash": STATE_HASH, "content_hashes": {kind: REGISTRY_HASHES[f"src/{kind}.generated.ts"] for kind in ("animations", "effects", "transitions")}}
    baseline = [path for path in regular if path.relative_to(PROFILE).as_posix() not in REGISTRY_HASHES]
    assert len(baseline) == 21501
    assert len(links) == 33
    assert all(path.resolve().is_relative_to(PROFILE) for path in links)
    assert all(not path.stat().st_mode & 0o222 for path in baseline)
    return {"nonregistry_readonly": len(baseline), "internal_links": len(links), "archive_installed_files": 80, "registry_hashes": REGISTRY_HASHES, "state_hash": STATE_HASH}


def checks(phase: str) -> None:
    report("launcher argv", lambda: str(Path(sys.argv[1]).resolve()) == str(HARNESS / "launch.sh"))
    report("observer isolation env", lambda: os.environ.get("PYTEST_PLUGINS") is None and os.environ.get("PYTEST_ADDOPTS") is None)
    report("Agent HEAD", lambda: git("rev-parse", "HEAD") == EXPECTED_HEAD)
    report("Agent parent", lambda: git("rev-parse", "HEAD^") == EXPECTED_PARENT)
    report("Agent tree", lambda: git("rev-parse", "HEAD^{tree}") == EXPECTED_TREE)
    report("Agent tracked diff empty", lambda: git("diff", "--quiet", "HEAD", "--") == "")
    report("Runtime pin", lambda: git("rev-parse", "HEAD", cwd=RUNTIME) == "8ea0766f5e9fb23cea26c7398770a8aaac06b258")
    report("Runtime tree", lambda: git("rev-parse", "HEAD^{tree}", cwd=RUNTIME) == "6047ac3033c3594b8f83db3afd8bb2c47cafa6d6")
    report("pinned Python", lambda: Path(sys.executable) == EXPECTED_PYTHON)
    report("Python version", lambda: sys.version_info[:3] == (3, 13, 16))
    report("Node version", lambda: command([str(NODE), "--version"]) == "v20.19.4")
    report("pytest/xdist versions", lambda: (importlib.import_module("pytest").__version__, importlib.import_module("xdist").__version__) == ("9.0.2", "3.8.0"))
    report("source/test hash locks", lambda: source_lock())
    report("PYTHONNOUSERSITE unset", lambda: os.environ.get("PYTHONNOUSERSITE") is None)
    def import_origins():
        actual = {
            name: str(Path(importlib.import_module(name).__file__).resolve())
            for name in ("astrid", "astrid_runtime_client", "runtime_protocol", "banodoco_timeline_schema")
        }
        expected = {
            "astrid": str(ROOT / "astrid/__init__.py"),
            "astrid_runtime_client": str(ROOT / "astrid_runtime_client/__init__.py"),
            "runtime_protocol": str(RUNTIME / "runtime_protocol/__init__.py"),
            "banodoco_timeline_schema": "/home/user/work/venv-agent/lib/python3.13/site-packages/banodoco_timeline_schema/__init__.py",
        }
        assert actual == expected, {"actual": actual, "expected": expected}
        return actual

    report("import origins", import_origins)
    report("original full-body hash retained", lambda: ORIGINAL_FULL_BODY_SHA256 == "5ebb51b8d7100a60658595176fac538d8248e9e2a85e082dc7c7feffd16bec71")
    report("source/profile bindings", lambda: {"root": str(ROOT), "runtime": str(RUNTIME), "profile": str(PROFILE), "node": str(NODE)})
    report("profile qualification", qualify_profile)
    report("profile package-lock", lambda: digest(PROFILE / "package-lock.json") == LOCK_HASH)
    report("profile composition archive", lambda: digest(PROFILE / ARCHIVE_NAME) == ARCHIVE_HASH)
    capture_phase = "before" if phase == "preflight" else "after"
    report("source manifests", lambda: capture(capture_phase) is not None)
    if phase == "post":
        before = gzip.decompress((EVIDENCE / f"{RUN_ID}-source-before.jsonl.gz").read_bytes()) + b"\0PROFILE\0" + gzip.decompress((EVIDENCE / f"{RUN_ID}-profile-before.jsonl.gz").read_bytes())
        after = gzip.decompress((EVIDENCE / f"{RUN_ID}-source-after.jsonl.gz").read_bytes()) + b"\0PROFILE\0" + gzip.decompress((EVIDENCE / f"{RUN_ID}-profile-after.jsonl.gz").read_bytes())
        report("source/profile before-after custody", lambda: before == after)
        origin_files = sorted(EVIDENCE.glob(f"{RUN_ID}-origins-*.json"))
        expected_origins = {(worker, phase) for worker in ("controller", "gw0", "gw1", "gw2") for phase in ("before", "after")}
        observed_origins = {(record["worker"], record["phase"]) for record in (json.loads(path.read_text()) for path in origin_files)}
        report("controller/gw0-gw2 origin custody", lambda: observed_origins == expected_origins)
        exits = [json.loads(path.read_text()) for path in sorted(EVIDENCE.glob(f"{RUN_ID}-exit-*.json"))]
        report("25 collected/zero failed per observed process", lambda: len(exits) == 4 and all(row["exit"] == 0 and row["collected"] == 25 and row["failed"] == 0 for row in exits))
        report("nested pytest observer isolation", lambda: len(origin_files) == len(expected_origins))


def main() -> int:
    phase = sys.argv[2] if len(sys.argv) == 3 else "preflight"
    checks(phase)
    (EVIDENCE / f"{RUN_ID}-preflight-{phase}.txt").write_text("\n".join(lines) + "\n")
    print(f"D21_PREFLIGHT_RESULT={'FAIL' if failures else 'PASS'} failures={len(failures)}", flush=True)
    return int(bool(failures))


if __name__ == "__main__":
    raise SystemExit(main())
