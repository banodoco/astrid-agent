#!/usr/bin/env python3
"""Host deployment adapter for the sealed C-06 coordinator/finalizer seam.

This adapter deliberately has no producer launcher, shell command, publisher
credential or network client.  M-INT's supervised host owns those outer seams;
this code makes their handoff durable and fail closed.
"""
from __future__ import annotations

import json
from pathlib import Path

import regression_coordinator as coordinator
import regression_finalizer as finalizer


class DeploymentError(RuntimeError):
    pass


def _atomic_input(path, value):
    """Use finalizer's fsyncing writer so restart sees all-or-nothing bytes."""
    finalizer._atomic_json(path, value)  # Deliberately shared, private durability primitive.


class DeploymentAdapter:
    """Drive a sealed invocation once; recovery never replays a started body."""

    def __init__(self, database, invocation_root):
        self.coordinator = coordinator.Coordinator(database)
        self.invocation_root = Path(invocation_root)
        self.invocation_root.mkdir(parents=True, exist_ok=True)

    def close(self):
        self.coordinator.close()

    def invocation_directory(self, sealed):
        # Both fields came from SQLite, never an artifact or user-provided path.
        return self.invocation_root / sealed["batch_id"] / sealed["invocation_id"]

    def finalise(self, batch_id, evaluator=None):
        """Unconditionally attempt the one sealed finalizer invocation.

        Caller-provided ``evaluator`` is an in-process reviewed function for
        synthetic tests or the trusted aggregate implementation.  It is never
        a producer command and cannot cause producer replay.
        """
        sealed = self.coordinator.begin_finalization(batch_id)
        directory = self.invocation_directory(sealed)
        input_path, status_path = directory / "sealed-input.json", directory / "status.json"
        _atomic_input(input_path, sealed)
        try:
            status = finalizer.run(sealed, directory, evaluator=evaluator, status_path=status_path)
        except finalizer.AlreadyStarted:
            # A prior body may have completed before a coordinator crash.  If
            # not, the durable marker proves it was accepted and died: red,
            # with no second evaluator body.
            try:
                status = finalizer.read_complete_status(status_path, sealed)
            except finalizer.FinalizerError:
                return self.coordinator.fail_finalization(batch_id, "finalizer worker died after accepted start")
        except Exception as exc:
            return self.coordinator.fail_finalization(
                batch_id, "finalizer unstartable or interrupted: %s: %s" % (type(exc).__name__, exc)
            )
        try:
            return self.coordinator.record_verdict(batch_id, status)
        except Exception as exc:
            # A malformed, stale or wrong-tuple status must never remain
            # eligible for publication.  This transition is itself durable.
            try:
                return self.coordinator.fail_finalization(
                    batch_id, "finalizer emitted invalid status: %s: %s" % (type(exc).__name__, exc)
                )
            except Exception:
                raise DeploymentError("coordinator could not store finalizer failure") from exc

    def recover(self, batch_id, evaluator=None):
        """Recover a restart without evaluating an invocation body twice."""
        batch = self.coordinator.batch(batch_id)
        if batch["state"] == coordinator.FINALIZE_READY:
            return self.finalise(batch_id, evaluator=evaluator)
        if batch["state"] != coordinator.FINALIZING:
            return batch
        sealed = self.coordinator.begin_finalization(batch_id)
        directory = self.invocation_directory(sealed)
        status_path = directory / "status.json"
        marker = directory / "started.json"
        if marker.exists():
            try:
                status = finalizer.read_complete_status(status_path, sealed)
            except finalizer.FinalizerError:
                return self.coordinator.fail_finalization(batch_id, "finalizer worker died after accepted start")
            return self.coordinator.record_verdict(batch_id, status)
        # No marker means an exec/permission crash before an accepted body.
        # Starting it now is safe and remains the sole evaluator execution.
        return self.finalise(batch_id, evaluator=evaluator)


def main(argv=None):
    """Intentionally small CLI: host supervision uses explicit Python wiring.

    No command-line producer/finalizer command is accepted.  This avoids a
    deployment configuration accidentally reintroducing the old replay path.
    """
    raise SystemExit("regression_deployment is a host adapter module, not a producer launcher")


if __name__ == "__main__":
    main()
