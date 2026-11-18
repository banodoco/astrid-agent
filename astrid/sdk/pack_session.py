"""Public, job-scoped context for declared resident Python actions.

No Runtime client, publication operation or process-lifetime credential is
exposed. Retained contexts expire when their job finishes or is cancelled.
"""
from astrid.core.execution.resident_sessions import (
    ResidentJobContext as PackJobContext,
)
from astrid.core.execution.resident_sessions import (
    ResidentProtocolError as PackSessionError,
)
from astrid.core.execution.resident_sessions import (
    current_job as current_pack_job,
)

__all__ = ["PackJobContext", "PackSessionError", "current_pack_job"]
