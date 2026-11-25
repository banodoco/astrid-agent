"""LE-3 real producer cases: recorded git replies, not synthetic envelopes."""
from __future__ import annotations

import subprocess
from functools import partial
from unittest.mock import patch

from astrid.core.generation.preflight import GenerationReadinessError
from astrid.core.generation.workflow_additions import resolve_workflow_additions

URL = "https://github.com/example/node-pack"
PIN_A = "a" * 40
PIN_B = "b" * 40


def resolution_case(declarations, *, reply="", exit_code=0):
    argv = ["astrid", "generate", "workflow", "source.json", "--as-entry", "example",
            "--placement", "runpod", "--replace", "--no-run", "--dry-run", "--json"]
    for declaration in declarations:
        argv.extend(["--add", declaration])

    def ls_remote(command, **kwargs):
        if exit_code:
            raise subprocess.CalledProcessError(exit_code, command)
        return subprocess.CompletedProcess(command, 0, reply, "")

    with patch("vibecomfy.custom_node_refs.subprocess.run", side_effect=ls_remote):
        try:
            resolve_workflow_additions(declarations, retry_argv=argv)
        except GenerationReadinessError as exc:
            return exc.to_envelope()
    raise AssertionError("expected a typed generation readiness refusal")


CASES = [
    ("unreachable_repository", "source_unreachable",
     partial(resolution_case, [URL], exit_code=128)),
    ("missing_ref", "commit_unavailable",
     partial(resolution_case, [URL + "@main"], exit_code=2)),
    ("ambiguous_branch_or_tag", "commit_unavailable",
     partial(resolution_case, [URL + "@main"],
             reply=f"{PIN_A}\trefs/heads/main\n{PIN_B}\trefs/tags/main\n")),
    ("conflicting_new_pins", "pin_conflict",
     partial(resolution_case, [URL + "@" + PIN_A, URL + "@" + PIN_B])),
]
