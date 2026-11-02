"""The Agent's vendored custody consumer must cover every platform the Runtime copy covers.

Regression for the r9-r11 activation failure: the Agent copy had no Linux branch in
_peer_token, so Linux host-control peers always failed. This test compares each
platform-dispatched function in the Runtime copy with the Agent copy by AST, so a
missing branch fails here instead of in a canary.
"""
import ast
from pathlib import Path

import pytest

AGENT = Path(__file__).resolve().parents[1] / "astrid" / "core" / "execution" / "custody_broker.py"


def _runtime_source():
    try:
        import astrid_runtime.custody_broker as module
    except ImportError:  # pragma: no cover - the Runtime package is part of the installed host
        pytest.skip("Runtime custody_broker is not importable in this environment")
    return Path(module.__file__).read_text(encoding="utf-8")


PLATFORM_MARKERS = ("darwin", "win32", "linux")


def _platform_keys(node: ast.AST) -> set[str]:
    """Platform identities a function dispatches on: sys.platform literals, startswith('linux'), LINUX flags."""
    keys: set[str] = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Constant) and isinstance(child.value, str) and child.value in PLATFORM_MARKERS:
            keys.add(child.value)
        if isinstance(child, ast.Attribute) and child.attr == "LINUX":
            keys.add("linux")
        if isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute) and child.func.attr == "startswith":
            if child.args and isinstance(child.args[0], ast.Constant) and child.args[0].value in PLATFORM_MARKERS:
                keys.add(child.args[0].value)
    return keys


def _functions(source: str) -> dict[str, ast.AST]:
    tree = ast.parse(source)
    return {node.name: node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}


def test_every_runtime_platform_dispatch_is_covered_in_the_agent_copy():
    runtime = _functions(_runtime_source())
    agent = _functions(AGENT.read_text(encoding="utf-8"))
    missing = {}
    for name, node in runtime.items():
        wanted = _platform_keys(node)
        if not wanted:
            continue
        if name not in agent:
            missing[name] = sorted(wanted)
            continue
        have = _platform_keys(agent[name])
        if not wanted <= have:
            missing[name] = sorted(wanted - have)
    assert not missing, f"Agent custody copy lacks platform branches the Runtime has: {missing}"


def test_linux_peer_token_branch_present_in_agent_copy():
    agent = _functions(AGENT.read_text(encoding="utf-8"))
    assert "linux" in _platform_keys(agent["_peer_token"])
    assert "linux" in _platform_keys(agent["default_process_identity"])
