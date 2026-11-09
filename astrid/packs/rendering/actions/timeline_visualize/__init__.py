"""Contracts shared by the rendered ``timeline_visualize`` executor.

Consumers can call :func:`validate_structural` before deriving a filmstrip to
collect duplicate-ID, dangling-track, and compositor timing errors. The former
structural diagram is no longer a public visualization route.
"""

from astrid.core.timeline.validate import validate_structural

__all__ = ["validate_structural"]
