"""The Windows task host must not let OpenBLAS create threads at DLL load (astrid-dev#651)."""
import ast
from pathlib import Path

from astrid.core import subprocess_env

HOST = Path(__file__).resolve().parents[3] / "astrid/core/execution/generic_host.py"


def test_host_limits_openblas_threads_on_windows_at_import():
    tree = ast.parse(HOST.read_text(encoding="utf-8"))
    guards = [node for node in tree.body if isinstance(node, ast.If)
              and ast.unparse(node.test) == "sys.platform == 'win32'"
              and "OPENBLAS_NUM_THREADS" in ast.unparse(node)]
    assert len(guards) == 1
    assert "__import__('os').environ.setdefault('OPENBLAS_NUM_THREADS', '1')" in ast.unparse(guards[0])


def test_windows_children_inherit_the_setting():
    assert "OPENBLAS_NUM_THREADS" in subprocess_env._WINDOWS_BASE_ENV


def test_host_preloads_numpy_on_the_main_thread_on_windows():
    tree = ast.parse(HOST.read_text(encoding="utf-8"))
    guard = next(node for node in tree.body if isinstance(node, ast.If)
                 and ast.unparse(node.test) == "sys.platform == 'win32'"
                 and "OPENBLAS_NUM_THREADS" in ast.unparse(node))
    text = ast.unparse(guard)
    assert text.index("OPENBLAS_NUM_THREADS") < text.index("__import__('numpy')")
    assert "except ImportError" in text
