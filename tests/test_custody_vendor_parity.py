"""The Agent custody consumer must not carry a second platform implementation.

Three guards (see docs/merge-plan notes in the R8 REPORT):
(a) each module-level custody function the Runtime also defines is a thin adapter that calls the Runtime's function;
(b) each qualified Agent method mapped to a Runtime method covers the Runtime's platform set, or is a documented
    exception with a reason and a tracked follow-up;
(c) the installer's reviewed inventory for the installed site-packages admits the Runtime modules the adapters import.
"""
import ast
from pathlib import Path

import pytest

AGENT = Path(__file__).resolve().parents[1] / "astrid" / "core" / "execution" / "custody_broker.py"
PLATFORM_MARKERS = ("darwin", "win32", "linux")

# Module-level functions delegated to the Runtime (same name on both sides).
DELEGATED = (
    "_peer_token", "audit_token_details", "current_process_audit_token", "signal_audit_token",
    "_append_owner_jsonl", "_open_authority_scope_lock", "child_exec_from_environment", "default_process_identity",
)

# Agent qualified method -> Runtime qualified method (the counterpart).
METHOD_MAP = {
    "RoleCustodyAuthority.signal": "RoleCustodyAuthority.signal",
    "RoleBoundCustodyBroker.signal": "RoleBoundCustodyBroker.signal",
    "RoleCustodyAuthority._guard": "RoleCustodyAuthority._guard",
    "RoleBoundCustodyBroker._serve": "RoleBoundCustodyBroker._serve",
}

# Documented exceptions: (agent name) -> (reason, tracked follow-up). Applies only to rule (b).
EXCEPTIONS = {
    "_CheckedPidTarget.signal": ("Agent-only checked-pid helper; no Runtime counterpart", "post-R1 class unification"),
    "ProcessCustody.signal": ("Linux and Darwin signal through the Runtime's pidfd primitive (runtime_protocol.platform.pidfd_send_signal) "
                              "reached via _signal_retained; the literal platform branch is the else-path, which AST literal matching cannot see; "
                              "win32 via platform.signal_tree", "post-R1 class unification"),
    "RoleCustodyAuthority._guard": ("Windows host custody is qualified separately; method-layer unification is post-R1 (class unification)",
                                    "post-R1 class unification"),
    "RoleBoundCustodyBroker._serve": ("Windows host custody is qualified separately; method-layer unification is post-R1 (class unification)",
                                      "post-R1 class unification"),
}
# Runtime methods with no Agent method at all: listed, not silently skipped.
RUNTIME_ONLY_EXCEPTIONS = {
    "NativeProcessCustody.publish": ("Agent has no publish method; publication is covered by ProcessCustody.signal and the Runtime's own tests", "post-R1 class unification"),
    "AuthenticatedCleanupActor.retained_child": ("Agent uses RetainedChildActor; retained_child has no Agent method", "post-R1 class unification"),
}


def _runtime_module():
    import astrid_runtime.custody_broker as module
    return module


def _module_functions(source: str) -> dict[str, ast.FunctionDef]:
    return {n.name: n for n in ast.parse(source).body if isinstance(n, ast.FunctionDef)}


def _methods(source: str) -> dict[str, ast.FunctionDef]:
    out = {}
    for cls in [n for n in ast.parse(source).body if isinstance(n, ast.ClassDef)]:
        for item in cls.body:
            if isinstance(item, ast.FunctionDef):
                out[f"{cls.name}.{item.name}"] = item
    return out


def _platform_keys(node: ast.AST) -> set[str]:
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


def _calls_runtime(node: ast.FunctionDef, name: str) -> bool:
    return any(
        isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute) and c.func.attr == name
        and isinstance(c.func.value, ast.Name) and c.func.value.id == "_rt"
        for c in ast.walk(node)
    )


def test_a_delegated_module_functions_are_thin_runtime_adapters():
    runtime = _runtime_module()
    agent = _module_functions(AGENT.read_text(encoding="utf-8"))
    for name in DELEGATED:
        assert hasattr(runtime, name), f"Runtime lacks {name}"
        assert name in agent, f"Agent lacks adapter {name}"
        assert _calls_runtime(agent[name], name), f"Agent {name} is not a thin adapter over the Runtime's {name}"


def test_b_method_platform_coverage_or_documented_exception():
    runtime = _methods(_runtime_module_source())
    agent = _methods(AGENT.read_text(encoding="utf-8"))
    failures = {}
    for agent_name, runtime_name in METHOD_MAP.items():
        if agent_name in EXCEPTIONS:
            reason, follow_up = EXCEPTIONS[agent_name]
            assert reason and follow_up
            continue
        assert agent_name in agent, f"Agent lacks {agent_name}"
        wanted = _platform_keys(runtime[runtime_name])
        have = _platform_keys(agent[agent_name])
        if not wanted <= have:
            failures[agent_name] = sorted(wanted - have)
    assert not failures, f"Agent method platform branches missing without a documented exception: {failures}"
    for runtime_name, (reason, follow_up) in RUNTIME_ONLY_EXCEPTIONS.items():
        assert runtime_name in runtime and reason and follow_up


def test_c_installer_inventory_admits_the_runtime_modules_the_adapters_import():
    from runtime_protocol.verified_host_launch import installed_inventory

    runtime = _runtime_module()
    root = Path(runtime.__file__).resolve().parents[1]
    files = installed_inventory(root)["files"]
    assert "astrid_runtime/custody_broker.py" in files
    assert "runtime_protocol/linux_process_binding.py" in files


def _runtime_module_source() -> str:
    return Path(_runtime_module().__file__).read_text(encoding="utf-8")
