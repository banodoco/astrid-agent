"""A ComfyUI workflow file runs on each placement, on CPU fakes (Plan 32 S1 proof).

Same harness as the default-slot tests (test_runpod_slot_path_cpu._drive): the real SDK,
Runtime and host, with doubles only at HTTP I/O. ``local`` is the managed target
(``placement="managed"``, which the CLI spells ``--placement managed``), ``attached`` is a
stand-in ComfyUI at a loopback address, and ``runpod`` is the pod fake. Each cell must
reach the target's /prompt and deliver the result.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from astrid.core.generation.delivery import deliver_generation
from tests.integrations.test_runpod_host_endpoint_r5 import tuple_sources  # noqa: F401  (fixture)
from tests.integrations.test_runpod_slot_path_cpu import TARGET_KINDS, _assert_ran_on_the_machine, _drive

pytestmark = pytest.mark.integration

# cause f96644329; ruling: machines 11:35 Sat
# _drive's managed branch uses the shared _pinned_source.managed_cpu_engine.

# The built-in image t2i default (priority 30): its ready template is a workflow file, and the
# CPU fakes already carry its classes, models and object_info.
DEFAULT_ENTRY = "qwen-image-2.1-turbo"
INPUTS = {"prompt": "A red cube on a white tabletop."}


def _workflow_file() -> Path:
    from astrid.core.generation import entries

    rows = [row for row in entries.ready_entries() if DEFAULT_ENTRY in (row["id"], row.get("registry_id"))]
    assert rows and rows[0].get("bundle"), f"no ready template for {DEFAULT_ENTRY}"
    return Path(rows[0]["bundle"])


# Expected-to-flip characterizations (T1 style, not xfail). Each asserts today's refusal and names
# the task row that flips it. When that row lands, the cell's real assertion is the same one the
# attached cell makes: the workflow file runs, reaches /prompt on the target, and delivers a result.
#   local-managed -> flipped by T4f (local run path): the cell asserts result.ok, a /prompt on the
#                    managed ComfyUI, and delivery.
#   runpod        -> T5g, now merged: a workflow-file job runs on the pod with no named entry, so
#                    this cell runs for real (see test_remote_machine_cpu::
#                    test_a_workflow_file_job_runs_on_the_machine_without_a_named_entry).
_RED = {}  # local-managed flipped in T4f: the registered managed install (see _managed_local_engine) runs the file


# A full managed-preparation run through a real Runtime: about 100 s per case on a box running three such proofs at once, so the
# 120 s default timeout cuts it off (local-managed timed out at 120 s while its sibling cases passed).
@pytest.mark.timeout(600)
@pytest.mark.parametrize("target_kind", TARGET_KINDS)
def test_workflow_file_runs_on_each_placement(target_kind, tmp_path, monkeypatch, real_runtime, tuple_sources):
    workflow = _workflow_file()

    def call(facade, controls, tmp):
        return facade.workflow(workflow=workflow, inputs=dict(INPUTS), engine="comfyui", **controls)

    if target_kind in _RED:
        kind, message, _task = _RED[target_kind]
        with pytest.raises(Exception) as caught:
            with _drive(("image", "t2i"), tmp_path, monkeypatch, real_runtime, tuple_sources,
                        call=call, target_kind=target_kind):
                pass
        assert type(caught.value).__name__ == kind and message in str(caught.value), repr(caught.value)[:2000]
        return
    with _drive(("image", "t2i"), tmp_path, monkeypatch, real_runtime, tuple_sources,
                call=call, target_kind=target_kind) as (client, project_id, provider, result):
        assert result.ok, json.dumps(result.to_dict(), default=str)[:4000]
        delivered = deliver_generation(result, client=client, out=tmp_path / "delivered", project=project_id)
        assert delivered, "nothing was delivered"
        if target_kind == "runpod":
            # The pod runs the job and its own ComfyUI takes /prompt; the controller's stand-in sees none.
            _assert_ran_on_the_machine(provider)
        else:
            assert provider.comfy.prompts and not provider.comfy.rejections
