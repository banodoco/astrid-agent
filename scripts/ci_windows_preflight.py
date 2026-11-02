"""Entry point whose stderr retains import and preflight tracebacks in CI."""

import os
from pathlib import Path

from scripts import ci_windows_journey as windows

windows.EVIDENCE = Path(os.environ["RUNNER_TEMP"]) / "journey-manager-direct-windows"
windows.scheduled_task_preflight()
