"""H3 build and finalize for the vibecomfy.workflows point.

build runs prepare and compile once on CPU, picks the trio, and measures the source with ffprobe.
finalize composes the candidate and verifies it, so preservation is still proven before copy-back.
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any, Mapping


def _path(inputs: Mapping[str, Any], files: Mapping[str, Any], name: str) -> Path:
    value = (files or {}).get(name) or (inputs or {}).get(name)
    if value is None:
        raise ValueError(f"h3 needs the {name!r} input")
    return Path(str(value))


def _stages(inputs, files, work: Path):
    from astrid.packs.h3_av.actions.transform.run import _compile_stages, _authoritative_source_asset_id
    from astrid.packs.h3_av.shared.input_bundle import build_input_bundle
    from astrid.packs.h3_av.shared.request import load_request_document, normalize_request

    request = normalize_request(load_request_document(_path(inputs, files, "request")))
    asset_map = json.loads(_path(inputs, files, "asset_map").read_text(encoding="utf-8"))
    bundle = build_input_bundle(request, asset_map, work / "inputs.zip")
    paths, preparation, compilation = _compile_stages(request, bundle, work / "plan")
    source_id = _authoritative_source_asset_id(request)
    return request, preparation, compilation, (paths[source_id] if source_id else None)


def build(inputs, files, out):
    """{workflow, bindings, plan}: the chosen trio's graph, its bindings and the sealed plan."""
    from astrid.core.generation import entries
    from astrid.packs.h3_av.actions.transform.run import _sealed_plan
    from astrid.packs.vibecomfy.invocation_preflight import _assert_no_unresolved_placeholders, measure_h3_source_av

    work = Path(out) / "h3" if out else Path(tempfile.mkdtemp(prefix="astrid-h3-build-"))
    work.mkdir(parents=True, exist_ok=True)
    request, _preparation, compilation, source = _stages(inputs, files, work)
    bindings = compilation["workflow_inputs"]
    _assert_no_unresolved_placeholders(bindings)
    plan = {**_sealed_plan(compilation), "request_digest": request.digest}
    if source is not None and "source_video" in bindings:
        plan["h3_source_av"] = measure_h3_source_av(source).to_dict()
    trio = Path(compilation["workflow"]["workflow.py"]["path"]).parent
    return {"workflow": entries._load_entry_workflow({"bundle": str(trio)}), "bindings": bindings, "plan": plan}


def finalize(job_dir, inputs, files, outputs):
    """Compose the generated candidate and verify it; a failure raises and fails the job."""
    from astrid.packs.h3_av.actions.verify.verify import verify_candidate
    from astrid.packs.h3_av.shared.compose import compose_candidate

    outputs = list(outputs or [])
    if not outputs:
        raise ValueError("h3 finalize received no generated output")
    work = Path(job_dir) / "h3-finalize" if job_dir else Path(tempfile.mkdtemp(prefix="astrid-h3-finalize-"))
    work.mkdir(parents=True, exist_ok=True)
    _request, preparation, _compilation, source = _stages(inputs, files, work)
    composition = compose_candidate(preparation=preparation, generated=outputs[0], source=source,
                                    out_dir=work / "compose")
    verify_candidate(preparation=preparation, composition=composition, source=source,
                     candidate=composition["candidate"]["path"])
    return [composition["candidate"]["path"], *outputs[1:]]
