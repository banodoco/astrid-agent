"""LE-10: genuine graph-verified doctor labels have a linked repair."""
import json

from astrid.core.generation.managed_preparation import (
    native_failure_inputs, native_unknown_input_readiness,
)


def _removed_input():
    stdout = json.dumps({"status": "error", "layer": "VibeWorkflow validation",
                         "validation_errors": [{"node_id": "1", "class_type": "FixtureNode",
                                                "errors": [{"code": "unknown_input", "input": "old"}]}]})
    labels = native_failure_inputs(stdout, "workflow_validation_failed",
                                   (("1", "FixtureNode", "old"),))
    return native_unknown_input_readiness(labels)


CASES = [("live-doctor-removed-input", "unknown_input", _removed_input)]
