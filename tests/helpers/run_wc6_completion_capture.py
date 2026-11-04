"""Run the preserved WC6 test with the reviewed, opt-in completion observer.

This is an attachment, not a test replacement.  The caller supplies the one
original node id, while the source root and all production imports remain the
owner-controlled pinned checkout.  The helper is authenticated independently
and is the only extra import path exposed to pytest.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import BinaryIO

ORIGINAL = (
    "tests/timeline/test_authoring_media.py::"
    "test_real_runtime_document_script_same_alias_rebind_reload_undo_retry_and_aba"
)
ORIGINAL_BLOB = "e55be12f88d23fddba3dd36614eb91745c1f8adf"
AGENT_COMMIT = "0c2c28da4f9733a1becc134f95219ae3079fc50e"
AGENT_TREE = "4ce9de35768c75d38db16882423313fb5047eb5f"
RUNTIME_COMMIT = "becfe6db88b2cc034dba3fafa6c050117cdf2b06"
RUNTIME_TREE = "7edc08e3fe94edcbc8ef695c479f4de301789b89"
RUNTIME_AUTH_BLOB = "979225ab8fdf7f6de1fcea4e7576b60b49b8b5f1"
HELPER_NAME = "wc6_completion_capture.py"
HELPER_SHA256 = "f17c605ab7ad991d9f45713a8b1de0830289239506b62069691333b17e2a8c01"
MAX_PACKET_BYTES = 4 * 1024 * 1024
MISSING_PACKET_EXIT = 74


class AttachmentRefused(RuntimeError):
    """The owner-controlled source or packet boundary is not safe to use."""


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _git(root: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


def verify_pinned_sources(source_root: Path, runtime_root: Path) -> None:
    """Repeat the original row's source, blob and clean-tree guards."""
    source_checks = (
        ("HEAD", AGENT_COMMIT),
        ("HEAD^{tree}", AGENT_TREE),
        (f"HEAD:{ORIGINAL.split('::')[0]}", ORIGINAL_BLOB),
    )
    runtime_checks = (
        ("HEAD", RUNTIME_COMMIT),
        ("HEAD^{tree}", RUNTIME_TREE),
        ("HEAD:runtime_protocol/timeline_commit_auth.py", RUNTIME_AUTH_BLOB),
    )
    for root, checks, label in (
        (source_root, source_checks, "Agent"),
        (runtime_root, runtime_checks, "Runtime"),
    ):
        for ref, expected in checks:
            if _git(root, "rev-parse", ref) != expected:
                raise AttachmentRefused(f"{label} source guard mismatch: {ref}")
        if _git(root, "status", "--porcelain", "--untracked-files=all"):
            raise AttachmentRefused(f"{label} source checkout is dirty")


def _real_file(path: Path, label: str) -> Path:
    if not path.is_file() or path.is_symlink():
        raise AttachmentRefused(f"{label} is not a regular file")
    resolved = path.resolve()
    if resolved != path.absolute():
        raise AttachmentRefused(f"{label} resolves through a link")
    return path


def verify_helper(helper: Path, runner_dir: Path) -> Path:
    """Accept only the reviewed helper beside this runner, byte-for-byte."""
    helper = _real_file(helper, "capture helper")
    expected = (runner_dir.resolve() / HELPER_NAME)
    if helper != expected:
        raise AttachmentRefused("capture helper origin mismatch")
    if _sha256(helper) != HELPER_SHA256:
        raise AttachmentRefused("capture helper hash mismatch")
    # A helper directory is allowed as a support path, but it must not be a
    # production module overlay.  The reviewed helper has no such siblings.
    for name in ("astrid", "astrid_runtime", "astrid_runtime_client", "runtime_protocol"):
        if (helper.parent / name).exists():
            raise AttachmentRefused("capture helper directory overlays production modules")
    return helper


def verify_sink(sink: Path, tmpdir: Path) -> Path:
    """Require the plugin's exclusive sink to remain below the real TMPDIR."""
    sink = sink.absolute()
    temporary = tmpdir.resolve()
    if sink.exists() or sink.is_symlink():
        raise AttachmentRefused("capture sink must be a new file")
    if sink.parent.resolve() != sink.parent or not sink.parent.resolve().is_relative_to(temporary):
        raise AttachmentRefused("capture sink is outside TMPDIR")
    if not sink.parent.is_dir():
        raise AttachmentRefused("capture sink parent is missing")
    return sink


def pytest_command(sink: Path) -> list[str]:
    """The unchanged original budget and exact original selector."""
    return [
        sys.executable,
        "-P",
        "-m",
        "pytest",
        "-q",
        "-oaddopts=",
        "-p",
        "no:cacheprovider",
        "--timeout=45",
        "--tb=short",
        "-m",
        "integration",
        "-p",
        "wc6_completion_capture",
        f"--wc6-completion-sink={sink}",
        ORIGINAL,
    ]


def _packet_state(packet: bytes) -> str:
    if not packet:
        return "empty"
    if not packet.endswith(b"\n"):
        return "partial"
    try:
        rows = [json.loads(line) for line in packet.splitlines()]
    except (UnicodeDecodeError, json.JSONDecodeError):
        return "partial"
    if rows and isinstance(rows[-1], dict) and rows[-1].get("phase") == "capture.end":
        return "complete"
    return "partial"


def retain_packet(sink: Path, child_exit: int, output: BinaryIO) -> tuple[int, str]:
    """Copy exact bounded sink bytes into the managed service's retained log."""
    if not sink.is_file() or sink.is_symlink():
        output.write(b"WC6_COMPLETION_PACKET_MISSING\n")
        output.flush()
        return (child_exit if child_exit else MISSING_PACKET_EXIT, "missing")
    packet = sink.read_bytes()
    if len(packet) > MAX_PACKET_BYTES:
        output.write(f"WC6_COMPLETION_PACKET_OVERSIZE bytes={len(packet)}\n".encode())
        output.flush()
        return (child_exit if child_exit else MISSING_PACKET_EXIT, "oversize")
    digest = hashlib.sha256(packet).hexdigest()
    state = _packet_state(packet)
    output.write(f"WC6_COMPLETION_PACKET_BEGIN bytes={len(packet)} sha256={digest} state={state}\n".encode())
    output.write(packet)
    if not packet.endswith(b"\n"):
        output.write(b"\n")
    output.write(f"WC6_COMPLETION_PACKET_END bytes={len(packet)} sha256={digest} state={state}\n".encode())
    output.flush()
    return child_exit, state


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("selector", nargs=1)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.selector != [ORIGINAL]:
        raise AttachmentRefused("exact WC6 selector is required")
    source_root = Path.cwd().resolve()
    runtime_value = os.environ.get("ASTRID_RUNTIME_CHECKOUT")
    if not runtime_value:
        raise AttachmentRefused("Runtime checkout is not declared")
    runtime_root = Path(runtime_value).resolve()
    verify_pinned_sources(source_root, runtime_root)
    runner_dir = Path(__file__).resolve().parent
    helper = verify_helper(runner_dir / HELPER_NAME, runner_dir)
    temporary = Path(os.environ.get("TMPDIR", "/tmp")).resolve()
    sink = verify_sink(temporary / f"wc6-completion-{os.getpid()}.jsonl", temporary)
    env = os.environ.copy()
    existing_pythonpath = env.get("PYTHONPATH", "")
    helper_path = str(helper.parent)
    env["PYTHONPATH"] = helper_path + (os.pathsep + existing_pythonpath if existing_pythonpath else "")
    # The helper path is deliberately narrower than the runner's source tree;
    # production modules therefore continue to resolve from the pinned source.
    env["TMPDIR"] = str(temporary)
    try:
        child_exit = subprocess.run(pytest_command(sink), cwd=source_root, env=env, check=False).returncode
    except BaseException:
        # Do not manufacture a pytest result when the existing invocation could
        # not start.  Retain any partial sink, if one was created.
        child_exit = 1
        raise
    finally:
        try:
            retain_packet(sink, child_exit, sys.stdout.buffer)
        except OSError as exc:
            # Retention is diagnostic.  It must never replace the original
            # pytest exit or exception with an observer filesystem error.
            print(f"WC6 completion packet retention unavailable: {type(exc).__name__}", file=sys.stderr)
    return child_exit


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AttachmentRefused as exc:
        print(f"WC6 completion attachment refused: {exc}", file=sys.stderr)
        raise SystemExit(2)
