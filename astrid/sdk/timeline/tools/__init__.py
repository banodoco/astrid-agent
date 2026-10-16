"""Public command-macro facade over canonical pack-v3 admission.

Other extension kinds keep their own consumer contracts. This facade neither
exports the compatibility registry nor bypasses the existing command runner.
"""
from astrid.core.pack.operations import (
    UnsupportedOperation,
    discover_operations,
    invoke_operation,
    operation_call,
    operation_discovery_scope,
    plan_operation,
    resolve_operation,
)

__all__ = [
    "UnsupportedOperation", "discover_operations", "invoke_operation",
    "plan_operation", "resolve_operation", "operation_call", "operation_discovery_scope",
]
