"""Pure Windows path witnesses for the installer and generation runtime."""

from __future__ import annotations

import json
import os
import subprocess
import threading
from pathlib import Path, PurePosixPath, PureWindowsPath
from types import SimpleNamespace

from astrid.core.execution.generic_host import GenericPackHost
from astrid.core.generation.vibecomfy_dependency import VIBECOMFY_ENGINE_REVISION
from astrid.core.generation.vibecomfy_runtime_root import vibecomfy_runtime_root
from astrid.installer import tools
from astrid.installer.core import new_version_id
from astrid.installer.service import default_program_root
from runtime_protocol import platform as runtime_platform
from runtime_protocol.util import new_id


ROOT = Path(__file__).resolve().parents[1]
RUNTIME_PIN = "3f9c0ca34170cbd3f39d61077ecfe497fc8bbf6b"
VIBECOMFY_PIN = "799d90f619a6723d0b376b7e77aef09c244a0821"
RUNTIME_LONGEST_FILE = "diagnostics/readiness-auth/readiness-native21-66e943bd/authenticated-state.json"
RUNTIME_LONGEST_DIRECTORY = "diagnostics/readiness-auth/readiness-native21-66e943bd"
VIBECOMFY_LONGEST_INCLUDED_FILE = (
    "tests/fixtures/live_agentic_corpus/corpus/"
    "1472123703247769_ComfyUI_2026-02-14-014741z-image-base__00001_.extracted.json"
)
VIBECOMFY_LONGEST_INCLUDED_DIRECTORY = (
    "ready_templates/sources/custom_nodes/ComfyUI-H3-Motion-Context-MultiRef/seitanism"
)
VIBECOMFY_LONGEST_EXCLUDED_FILE = (
    "docs/handover/unified-workflow-integrity-20260909/evidence/"
    "workflow-review-set-20260911/exploratory-failures/"
    "06_ltx2_3_talking_avatar_qwen_tts.convert.json"
)
VIBECOMFY_REQUIRED_JSON = "docs/templates/strict_ready_exceptions.json"


def _windows_roots(monkeypatch):
    user = "u" * 40
    home = Path(f"C:/Users/{user}")
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    program = PureWindowsPath(str(default_program_root(home, "win32")))
    with monkeypatch.context() as windows:
        windows.setattr(runtime_platform, "sys", SimpleNamespace(platform="win32"))
        support = PureWindowsPath(str(runtime_platform.data_root(home)))
    return program, support


def _tree_entries(paths):
    return [(PurePosixPath(path).as_posix(), False) for path in paths]


def _assert_windows_archive_fits(entries, destination, *, strip_root=False):
    return tools.archive_destinations(
        entries,
        destination,
        platform_name="win32",
        strip_root=strip_root,
    )


def _git_files(root: Path) -> list[str]:
    result = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z"],
        check=True,
        stdout=subprocess.PIPE,
    )
    return [os.fsdecode(path) for path in result.stdout.split(b"\0") if path]


def _git(repo: Path, *args: str) -> str:
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull})
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        env=env,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ).stdout.decode().strip()


def _assert_budget(paths, *, file_limit=260, directory_limit=248):
    def units(path):
        return len(str(path).encode("utf-16-le")) // 2

    files = list(paths)
    directories = [path.parent for path in files]
    longest_file = max(files, key=units)
    longest_directory = max(directories, key=units)
    assert units(longest_file) < file_limit, f"file path too long ({units(longest_file)}): {longest_file}"
    assert units(longest_directory) < directory_limit, (
        f"directory path too long ({units(longest_directory)}): {longest_directory}"
    )


def test_installed_agent_source_inventory_fits_fresh_version_staging(monkeypatch):
    program, _support = _windows_roots(monkeypatch)
    version = new_version_id({"agent": {"sha": "a" * 40}, "runtime": {"sha": "b" * 40}})
    version_root = program / "versions" / version

    agent_sha = subprocess.run(
        ["git", "-C", str(ROOT), "rev-parse", "HEAD"],
        check=True,
        stdout=subprocess.PIPE,
    ).stdout.decode().strip()
    agent_files = _git_files(ROOT)
    agent_entries = [(f"astrid-agent-{agent_sha}/{name}", False) for name in agent_files]
    agent_stage = _assert_windows_archive_fits(
        agent_entries, version_root / "a.u", strip_root=True,
    )
    _assert_budget(agent_stage, file_limit=260, directory_limit=248)


def test_installed_runtime_pinned_source_witness_fits_fresh_version_staging(monkeypatch):
    program, _support = _windows_roots(monkeypatch)
    version_root = program / "versions" / new_version_id(
        {"agent": {"sha": "a" * 40}, "runtime": {"sha": "b" * 40}}
    )
    runtime_lock = json.loads((ROOT / "runtime.lock.json").read_text(encoding="utf-8"))
    assert runtime_lock["source_commit"] == RUNTIME_PIN, "refresh the pinned Runtime path witnesses"
    runtime_entries = [
        (f"astrid-runtime-{RUNTIME_PIN}/{RUNTIME_LONGEST_FILE}", False),
        (f"astrid-runtime-{RUNTIME_PIN}/{RUNTIME_LONGEST_DIRECTORY}/witness.json", False),
    ]
    runtime_stage = _assert_windows_archive_fits(
        runtime_entries, version_root / "r.u", strip_root=True,
    )
    _assert_budget(runtime_stage, file_limit=260, directory_limit=248)

    final_paths = [
        version_root / name
        for name in ("manifest.json", "version.json", "profile.json", "services.json")
    ]
    _assert_budget(final_paths)
    py_root = version_root / "py"
    from astrid.installer import service

    with monkeypatch.context() as windows:
        windows.setattr(service, "sys", SimpleNamespace(platform="win32"))
        interpreter = service.venv_python(py_root)
    _assert_budget([interpreter])


def test_vibecomfy_source_projection_fits_the_actual_windows_install_location(
        tmp_path, monkeypatch):
    program, _support = _windows_roots(monkeypatch)
    version_root = program / "versions" / new_version_id(
        {"agent": {"sha": "a" * 40}, "runtime": {"sha": "b" * 40}}
    )
    _assert_vibecomfy_witness_pin()
    repo = tmp_path / "pinned-source-witness"
    repo.mkdir()
    _git(repo, "init", "--quiet")
    files = {
        VIBECOMFY_LONGEST_INCLUDED_FILE: b"included pinned source witness",
        f"{VIBECOMFY_LONGEST_INCLUDED_DIRECTORY}/path-budget-witness.txt": b"included pinned directory witness",
        VIBECOMFY_REQUIRED_JSON: b"required wheel input",
        VIBECOMFY_LONGEST_EXCLUDED_FILE: b"excluded pinned documentation witness",
        "examples/readme-example.py": b"excluded example",
        "vibecomfy/__init__.py": b"runtime package",
    }
    for relative, data in files.items():
        path = repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    _git(repo, "add", ".")
    _git(repo, "-c", "user.name=Path budget", "-c", "user.email=path@example.invalid",
         "commit", "--quiet", "-m", "Pinned path witnesses")
    source_sha = _git(repo, "rev-parse", "HEAD")

    installed = tmp_path / "installed-vibecomfy"
    tools.source("vibecomfy", source_sha, installed, repo=str(repo), platform_name="win32")

    # Measure the actual projected tree at the same stage name used by Backend._stage/tools.source.
    entries = _tree_entries(
        path.relative_to(installed).as_posix() for path in installed.rglob("*") if path.is_file()
    )
    staged = _assert_windows_archive_fits(entries, version_root / "v.u")
    _assert_budget(staged, file_limit=260, directory_limit=248)
    assert (installed / VIBECOMFY_REQUIRED_JSON).read_bytes() == b"required wheel input"
    assert (installed / VIBECOMFY_LONGEST_INCLUDED_FILE).read_bytes() == b"included pinned source witness"
    assert (installed / VIBECOMFY_LONGEST_INCLUDED_DIRECTORY / "path-budget-witness.txt").is_file()
    assert not (installed / VIBECOMFY_LONGEST_EXCLUDED_FILE).exists()
    assert not (installed / "examples").exists()
    final = _assert_windows_archive_fits(entries, version_root / "v")
    _assert_budget(final, file_limit=260, directory_limit=248)


def _allocate_default_windows_attempt(tmp_path, monkeypatch, support):
    local_support = tmp_path / "support"
    from astrid.sdk import storage_root

    monkeypatch.setattr(storage_root, "resolve_runtime_data_root", lambda **_kwargs: local_support)
    monkeypatch.setattr(storage_root, "resolve_xdg_runtime_root", lambda: None)
    host = GenericPackHost.__new__(GenericPackHost)
    host.executor_id = "windows-path-budget"
    host.attempt_base = None
    host.attempt_root = None
    host._private_attempt_base_identity = None
    host._process_lock = threading.RLock()
    attempt = host._allocate_attempt_root("task", "attempt")
    assert attempt.name.startswith("astrid-attempt-")
    win_attempt = support / PureWindowsPath(attempt.relative_to(local_support).as_posix())
    return attempt, win_attempt


def _assert_vibecomfy_witness_pin():
    assert VIBECOMFY_ENGINE_REVISION == VIBECOMFY_PIN, "refresh the pinned VibeComfy path witnesses"


def test_attempt_result_receipt_fits_40_character_profile(tmp_path, monkeypatch):
    _program, support = _windows_roots(monkeypatch)
    _attempt, win_attempt = _allocate_default_windows_attempt(tmp_path, monkeypatch, support)

    from tests.core.execution.test_producer_evidence_transport_f06 import _materialize

    spool = _materialize(tmp_path, monkeypatch)
    receipt_host = GenericPackHost.__new__(GenericPackHost)
    receipt_host.boot_manifest_provenance = lambda: None
    receipt_host._prepare_producer_evidence(
        spool, {"execution_request": {"target": {"kind": "local"}}},
    )
    receipt = json.loads((spool / "manifest.json").read_text(encoding="utf-8"))
    result_relative = PurePosixPath(receipt["managed_generation_result_path"])
    result_path = win_attempt / "outputs" / PureWindowsPath(result_relative.as_posix())
    _assert_budget([result_path])


def test_vibecomfy_runtime_writer_paths_fit_40_character_profile(tmp_path, monkeypatch):
    _assert_vibecomfy_witness_pin()
    _program, support = _windows_roots(monkeypatch)
    _attempt, win_attempt = _allocate_default_windows_attempt(tmp_path, monkeypatch, support)

    runtime_root = vibecomfy_runtime_root(win_attempt / "outputs", platform_name="win32")
    from vibecomfy.runtime.run import _allocate_run_dir

    local_runtime_root = tmp_path / "vibecomfy-runtime-writer"
    _actual_run_id, actual_run_dir = _allocate_run_dir("run", runtime_root=local_runtime_root)
    windows_run_parent = runtime_root / PureWindowsPath(
        actual_run_dir.parent.relative_to(local_runtime_root).as_posix()
    )
    # GenericHost's native Runtime run IDs are longer than VibeComfy's own generated ID.
    run_id = "run-" + new_id()
    run_dir = windows_run_parent / run_id
    transaction_dir = run_dir / "transactions" / ("e" * 64)
    # The filenames and transaction directory are the pinned VibeComfy writer layout.
    transaction_files = [
        transaction_dir / "lifecycle_events.jsonl",
        transaction_dir / "canvas_verified.json.tmp",
        transaction_dir / "prepared.json",
        transaction_dir / "finalized.json",
        transaction_dir / "rollback.json",
    ]
    run_result = run_dir / "managed-generation-result.json.tmp"
    runtime_paths = [*transaction_files, run_result]
    _assert_budget(runtime_paths)
