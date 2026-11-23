"""Schema fixtures only; each mapping card adds cases invoking its real producer.

This explicit list is deliberately independent of SETUP_ISSUE_CODES: extending
the registry without adding a case must fail the rule test. The safe inspection
command here demonstrates the shape; it is not a claimed repair for these states.
"""
from functools import partial

from astrid.core.generation.preflight import generation_error


def _contract_case(code):
    return generation_error(ValueError(f"Contract fixture for {code}"), code=code, fixes=[{
        "id": "inspect", "who": "human", "scope": "read_only", "details": {},
        "instruction": {"step": "Inspect the reported state with the person before continuing.",
                        "then_argv": ["astrid", "generate", "describe", "--entry", "<ENTRY>", "--json"]},
        "placeholders": {"ENTRY": "The saved entry being diagnosed"},
    }]).to_envelope() | {"task_id": None}


CASES = [(code, code, partial(_contract_case, code)) for code in (
    "needs_consent", "entry_exists", "pin_conflict", "pack_version_differs", "unknown_input",
    "source_unreachable", "commit_unavailable", "pip_failed", "protected_change", "conflict",
    "partial_install", "classes_not_loaded", "fetch_404",
)]
