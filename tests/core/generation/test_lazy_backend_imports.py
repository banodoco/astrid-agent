"""Lazy imports keep the selected backend and external pack APIs intact."""
import os
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[3]


def _fresh(code):
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(
        p for p in (str(ROOT), env.get("PYTHONPATH")) if p
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, (result.stdout, result.stderr)


def test_provider_exports_load_only_when_requested():
    _fresh("""
import sys
import astrid.core.generation.backends as backends

codex = backends.__name__ + ".codex"
fal = backends.__name__ + ".fal"
assert codex not in sys.modules
assert fal not in sys.modules
assert {"CodexBackend", "FalBackend"} <= set(dir(backends))
assert codex not in sys.modules
assert fal not in sys.modules
selected = backends.CodexBackend
from astrid.core.generation.backends.codex import CodexBackend
assert selected is CodexBackend is backends.CodexBackend
assert fal not in sys.modules
selected = backends.FalBackend
from astrid.core.generation.backends.fal import FalBackend
assert selected is FalBackend is backends.FalBackend
""")


def test_gateway_pack_route_import_is_deferred():
    _fresh("""
import sys
from astrid.core.gateway import dispatch

name = "astrid.core.gateway.hivemind"
assert name not in sys.modules
dispatch._installed_pack_ids = lambda: frozenset({"hivemind"})
dispatch._register_installed_pack_routes()
assert name in sys.modules
assert "hivemind" in dispatch._TOP_LEVEL_HANDLERS
assert "hivemind" in dispatch._DYNAMIC_PACK_ROUTE_NAMES
dispatch._clear_dynamic_pack_routes()
assert "hivemind" not in dispatch._TOP_LEVEL_HANDLERS
""")
