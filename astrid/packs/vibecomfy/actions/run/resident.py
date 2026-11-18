"""Admitted explicit-context entry for the VibeComfy resident worker."""
from __future__ import annotations

from contextlib import nullcontext

from astrid.sdk import current_pack_job


def run(**inputs):
    from astrid.core.generation.managed_preparation import preparation_scope
    from astrid.core.generation.target_inventory import POD_ALIGN
    from astrid.core.pack.entrypoint import canonical_runtime_entrypoint
    from astrid.packs.vibecomfy.resident import _eligible
    from astrid.packs.vibecomfy.shared.bundle_inputs import staged_workflow_path

    job = current_pack_job()
    if not job.session_identity or not _eligible(inputs) or not inputs.get("comfy_url"):
        raise ValueError("workflow is not eligible for this resident context")
    token = POD_ALIGN.set(job.settings.get("align", "check"))
    try:
        report = job.settings.get("managed_engine_preparation")
        with canonical_runtime_entrypoint("vibecomfy.run"), (preparation_scope(report) if report is not None else nullcontext()):
            from astrid.packs.vibecomfy.actions.run.run import _run_and_settle
            with staged_workflow_path(workflow=inputs.get("workflow"), python=inputs.get("python"),
                    companion=inputs.get("companion"), source=inputs.get("source"), scratch=job.attempt_root) as (path, _):
                return _run_and_settle(path, job.output_root, task_identity=job.task_id,
                    attempt_identity=job.attempt_id, execution_identity=job.settings["execution_identity"],
                    align=job.settings.get("align"), entry=job.settings.get("entry"),
                    **{key: inputs[key] for key in ("workflow_inputs", "managed_assets", "comfy_url", "comfy_output_dir")
                       if key in inputs and inputs[key] is not None})
    finally:
        POD_ALIGN.reset(token)
