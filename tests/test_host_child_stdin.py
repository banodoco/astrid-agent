from __future__ import annotations

import ast
import os
import subprocess
import sys
import threading
from contextlib import contextmanager
from pathlib import Path

import pytest

from astrid.core.execution import process_group, task_scope


_READ_STDIN = "import sys;sys.stdout.write('eof' if sys.stdin.read() == '' else 'input')"


@contextmanager
def _blocked_parent_stdin():
    original = os.dup(0)
    read_fd, write_fd = os.pipe()
    os.dup2(read_fd, 0)
    os.close(read_fd)
    reader = threading.Thread(target=lambda: os.read(0, 1), daemon=True)
    reader.start()
    try:
        yield
    finally:
        os.close(write_fd)
        reader.join(timeout=5)
        reader_stopped = not reader.is_alive()
        os.dup2(original, 0)
        os.close(original)
        assert reader_stopped, "blocked host stdin reader did not stop after pipe close"


def _assert_child_stdin_is_closed(process: subprocess.Popen) -> None:
    try:
        stdout, _stderr = process.communicate(timeout=10)
        assert process.returncode == 0
        assert stdout == b"eof"
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=5)


def test_task_scope_spawn_does_not_inherit_host_stdin():
    with _blocked_parent_stdin():
        process = task_scope.spawn(
            [sys.executable, "-c", _READ_STDIN],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        _assert_child_stdin_is_closed(process)


def test_task_scope_spawn_explicit_none_does_not_inherit_host_stdin():
    with _blocked_parent_stdin():
        process = task_scope.spawn(
            [sys.executable, "-c", _READ_STDIN],
            stdin=None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        _assert_child_stdin_is_closed(process)


def test_unprotected_spawn_stalls_with_a_blocked_host_stdin():
    with _blocked_parent_stdin():
        process = subprocess.Popen(
            [sys.executable, "-c", _READ_STDIN],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            with pytest.raises(subprocess.TimeoutExpired):
                process.wait(timeout=0.25)
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=5)


def test_owned_process_spawn_does_not_inherit_host_stdin(monkeypatch):
    class Custody:
        def __init__(self):
            self._handles = {}

        def prepare_launch(self, argv, **_kwargs):
            return argv, None

        def bind_launch(self, process):
            self._handles[process.pid] = ("test-birth", None)

        def start_observer(self):
            pass

        def start_inventory_owner(self):
            pass

        def release_launch(self):
            pass

    monkeypatch.setattr(process_group, "ProcessCustody", Custody)
    monkeypatch.setattr(process_group, "observe_tree", lambda _process: {})
    with _blocked_parent_stdin():
        process = process_group.popen_owned_process(
            [sys.executable, "-c", _READ_STDIN],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            _delegate_inventory=False,
        )
        _assert_child_stdin_is_closed(process)


def test_explicit_input_pipe_is_preserved():
    process = task_scope.spawn(
        [sys.executable, "-c", "import sys;sys.stdout.write(sys.stdin.read())"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        stdout, _stderr = process.communicate(b"explicit input", timeout=10)
        assert process.returncode == 0
        assert stdout == b"explicit input"
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=5)


def test_host_probe_subprocess_calls_explicitly_close_stdin():
    root = Path(__file__).resolve().parents[1]
    sources = (
        root / "astrid/core/execution/generic_host.py",
        root / "astrid/core/execution/process_group.py",
        root / "astrid/core/execution/task_scope.py",
        root / "astrid/core/execution/custody_broker.py",
        root / "astrid/core/execution/executor/catalog_source.py",
        root / "astrid/core/execution/executor/folder.py",
        root / "astrid/core/execution/executor/runner.py",
        root / "astrid/core/execution/orchestrator/runner.py",
        root / "astrid/core/execution/orchestrator/folder.py",
        root / "astrid/core/execution/reconciler.py",
        root / "astrid/core/execution/thumbnails.py",
        root / "astrid/core/element/project_packs.py",
        root / "astrid/core/generation/vibecomfy_dependency.py",
        root / "astrid/core/media.py",
        root / "astrid/core/pack/source_setup.py",
        root / "astrid/sdk/host_bootstrap.py",
        root / "astrid/core/runtime/log_capture.py",
        root / "astrid/core/rendering/node_pin.py",
        root / "astrid/core/foundation/renderer_toolchain.py",
    )
    for source in sources:
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if not isinstance(node.func.value, ast.Name) or node.func.value.id != "subprocess":
                continue
            if node.func.attr not in {"run", "Popen"}:
                continue
            stdin = next((keyword.value for keyword in node.keywords if keyword.arg == "stdin"), None)
            assert stdin is not None and not (
                isinstance(stdin, ast.Constant) and stdin.value is None
            ), f"{source}:{node.lineno} launches a host probe without effective stdin isolation"

    gpu_source = root / "astrid/core/generation/gpu_environment.py"
    gpu_tree = ast.parse(gpu_source.read_text(encoding="utf-8"), filename=str(gpu_source))
    detect_nvidia = next(
        node for node in gpu_tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "detect_nvidia"
    )
    probe = next(
        node for node in ast.walk(detect_nvidia)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "subprocess"
        and node.func.attr == "run"
    )
    stdin = next((keyword.value for keyword in probe.keywords if keyword.arg == "stdin"), None)
    assert stdin is not None and not (
        isinstance(stdin, ast.Constant) and stdin.value is None
    ), f"{gpu_source}:{probe.lineno} launches the host GPU probe without effective stdin isolation"
