"""Attempt-local sibling staging for canonical VibeComfy bundle consumers."""

from __future__ import annotations

import shutil
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


class CanonicalBundleInputError(ValueError):
    """A canonical bundle input set is incomplete or conflicts with a UI input."""


@contextmanager
def staged_workflow_path(
    *,
    workflow: str | Path | None,
    python: str | Path | None,
    companion: str | Path | None,
    source: str | Path | None,
    scratch: str | Path | None = None,
    preserve_on_error: tuple[type[BaseException], ...] = (),
) -> Iterator[tuple[Path, str]]:
    """Yield one workflow path and authority label, staging canonical siblings.

    Existing single-file UI JSON tasks remain supported. Canonical tasks pass
    the Python/companion/source objects independently; this adapter gives the
    package loader their canonical sibling basenames without mutating inputs.
    """
    workflow_path = Path(workflow) if workflow else None
    member_values = (python, companion, source)
    present = tuple(bool(value) for value in member_values)
    if workflow_path is not None and any(present):
        raise CanonicalBundleInputError(
            "provide either one workflow JSON input or all canonical bundle members"
        )
    if not any(present):
        if workflow_path is None:
            raise CanonicalBundleInputError(
                "provide a workflow JSON input or Python, companion, and source members"
            )
        if not workflow_path.is_file():
            raise CanonicalBundleInputError(f"workflow input is not a readable file: {workflow_path}")
        yield workflow_path, "input_ui_graph"
        return
    if not all(present):
        raise CanonicalBundleInputError(
            "canonical bundle input requires python, companion, and source members together"
        )

    paths = tuple(Path(str(value)) for value in member_values)
    if any(not path.is_file() for path in paths):
        missing = [str(path) for path in paths if not path.is_file()]
        raise CanonicalBundleInputError(
            "canonical bundle member is missing or unreadable: " + ", ".join(missing)
        )

    scratch_root = None
    if scratch is not None:
        scratch_root = Path(scratch).expanduser().resolve()
        scratch_root.mkdir(parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(
            prefix="astrid-vibecomfy-bundle-",
            dir=scratch_root,
        )
    )
    retained = False
    try:
        staged_python = staging / "workflow.py"
        staged_companion = staging / "workflow.vibe.json"
        staged_source = staging / "source.json"
        shutil.copyfile(paths[0], staged_python)
        shutil.copyfile(paths[1], staged_companion)
        shutil.copyfile(paths[2], staged_source)
        yield staged_python, "canonical_bundle"
    except preserve_on_error:
        # An owned writer may still reference these exact paths. The host
        # retains/fences their attempt until custody has been recovered.
        retained = True
        raise
    finally:
        if not retained:
            shutil.rmtree(staging, ignore_errors=True)
