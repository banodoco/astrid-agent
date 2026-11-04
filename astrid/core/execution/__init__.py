"""Execution tier: executor + orchestrator runners (orchestrator drives executor, intra-package)."""

from astrid.core.source_identity import verify_installed_host_startup

# The selected installed launch must be checked before imports of Runtime code.
verify_installed_host_startup()

from .persistent_supervisor import (  # noqa: E402
    JsonlSupervisor,
    LeaseFence,
    PersistentJsonlSupervisor,
    PersistentSupervisor,
    SupervisorError,
)
from .reconciler import (  # noqa: E402
    ExecutionReconciler,
    ExecutionUncertain,
    OutputCustodyError,
    ReconcileResult,
    ReconcilerError,
    verify_output_custody,
)
from .target_adapter import (  # noqa: E402
    LocalMachineTargetAdapter,
    RunPodTargetAdapter,
    TargetAdapterError,
    TargetObservation,
    TargetReceipt,
)

__all__ = [
    "JsonlSupervisor",
    "LeaseFence",
    "PersistentJsonlSupervisor",
    "PersistentSupervisor",
    "SupervisorError",
    "ExecutionReconciler",
    "ExecutionUncertain",
    "OutputCustodyError",
    "ReconcileResult",
    "ReconcilerError",
    "verify_output_custody",
    "LocalMachineTargetAdapter",
    "RunPodTargetAdapter",
    "TargetAdapterError",
    "TargetObservation",
    "TargetReceipt",
]
