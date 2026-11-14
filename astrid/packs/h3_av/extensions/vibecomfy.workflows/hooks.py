"""H3 build and after_outputs for the vibecomfy.workflows point.

``build`` runs prepare and compile once on CPU from the ``request`` file and the ``asset_map``
file map, picks the trio, measures the source, and saves what it learned in the job directory.
It is pure and deterministic: it runs on the controller through ``requirements()``.

``after_outputs`` composes the candidate and verifies it, so preservation is still proven before
copy-back. It shares ``build``'s job directory, reads the saved state and never re-runs prepare
or compile.

This module imports nothing from Astrid. It loads the pack's own modules by path (they use
relative imports) and takes engine-side facts from the ``engine`` argument the engine passes.
"""
from __future__ import annotations

import importlib.util
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any, Mapping

_PACK = "_h3_av_pack"


def _pack():
    """The h3_av package, loaded once from this file's pack root under a private name."""
    module = sys.modules.get(_PACK)
    if module is None:
        root = Path(__file__).resolve().parents[2]
        spec = importlib.util.spec_from_file_location(
            _PACK, root / "__init__.py", submodule_search_locations=[str(root)])
        module = importlib.util.module_from_spec(spec)
        sys.modules[_PACK] = module
        spec.loader.exec_module(module)
    return module


def _shared(name: str):
    _pack()
    return importlib.import_module(f"{_PACK}.shared.{name}")


def _path(inputs: Mapping[str, Any], files: Mapping[str, Any], name: str) -> Path:
    value = (files or {}).get(name) or (inputs or {}).get(name)
    if value is None:
        raise ValueError(f"h3 needs the {name!r} input")
    return Path(str(value))


def _file_map(inputs: Mapping[str, Any], files: Mapping[str, Any]) -> dict[str, str]:
    value = (files or {}).get("asset_map") or (inputs or {}).get("asset_map")
    if not isinstance(value, Mapping) or not value:
        raise ValueError("h3 needs the 'asset_map' file map ({asset id: file path})")
    return {str(key): str(path) for key, path in value.items()}


def _require_media_tools() -> None:
    """Check output tools on the execution machine before the GPU run."""
    missing = [tool for tool in ("ffmpeg", "ffprobe") if shutil.which(tool) is None]
    if missing:
        raise ValueError("h3 needs ffmpeg and ffprobe on PATH on the machine that runs it; missing: "
                           + ", ".join(missing))


def _given(inputs: Mapping[str, Any], files: Mapping[str, Any], name: str) -> Any:
    return (files or {}).get(name) or (inputs or {}).get(name)


def _plain_request(inputs: Mapping[str, Any], files: Mapping[str, Any], facts: Any) -> tuple[dict[str, Any], dict[str, str]]:
    """The request.v1 document and asset map for the kind's plain inputs (a prompt and one media file).

    An image is the one reference of a source-free generation. A video is regenerated over its whole
    picture and length, keeping its audio. Anything finer (masks, continuation, more references) is a
    request document.
    """
    prompt, image, video = (_given(inputs, files, name) for name in ("prompt", "image_ref", "video_ref"))
    if not prompt or bool(image) == bool(video):
        raise ValueError("h3 needs a 'request' document with its 'asset_map', or a prompt with exactly one of "
                         "image_ref (i2v) or video_ref (v2v)")
    overrides = {name: int(inputs[name]) for name in ("seed", "steps") if (inputs or {}).get(name) is not None}
    base = {"version": 1, "content": {"prompt": str(prompt)}, "references": [], "overrides": overrides}
    if image:
        raw_frames = (inputs or {}).get("frames")
        frames = 120 if raw_frames is None else int(raw_frames)
        return ({**base, "operation": "generate", "output": {"duration": frames / 24},
                 "changes": {"video": [], "audio": []},
                 "references": [{"asset": "image_ref", "purpose": "appearance"}]}, {"image_ref": str(image)})
    if (inputs or {}).get("frames") is not None:
        raise ValueError("h3 frames is only supported with image_ref; video_ref uses its full source length")
    duration = int(facts.measure_source_av(video)["video_frames"]) / 24
    return ({**base, "operation": "edit", "source": {"asset": "video_ref", "range": [0.0, duration]},
             "output": {"duration": duration},
             "changes": {"video": [{"during": [0.0, duration], "area": {"full_frame": True}, "action": "generate"}],
                         "audio": []}}, {"video_ref": str(video)})


def _state_dir(job_dir) -> Path:
    return (Path(job_dir) if job_dir else Path(tempfile.mkdtemp(prefix="astrid-h3-"))) / "h3"


def build(inputs, files, out, *, engine=None, check_runtime=False):
    """{workflow, bindings, plan, assets, bundle}: the chosen trio's graph, bindings, sealed plan, media, folder."""
    # Source inspection checks its own tools during planning. Output composition
    # tools belong to execution, on the machine that will run after_outputs.
    if check_runtime:
        _require_media_tools()
    if engine is not None:
        _shared("engine_facts").install(engine)
    facts = _shared("engine_facts").facts()
    stages, request_module = _shared("stages"), _shared("request")

    work = _state_dir(out)
    work.mkdir(parents=True, exist_ok=True)
    run_logs = facts.run_logs
    for name, relative in {
        "h3.preparation": "preparation.json",
        "h3.plan": "build-state.json",
        "h3.compilation": "plan/compile/compilation.json",
        "h3.composition": "compose/composition-manifest.json",
        "h3.verification": "verify.json",
    }.items():
        run_logs.declare(work.parent, run_logs.LogDecl(name, "run", str(Path("h3") / relative)))
    if _given(inputs, files, "request"):
        document = request_module.load_request_document(_path(inputs, files, "request"))
        asset_map = _file_map(inputs, files)
    else:
        document, asset_map = _plain_request(inputs, files, facts)
    request = request_module.normalize_request(document)
    paths, preparation, compilation = stages.compile_stages(request, asset_map, work / "plan")
    _shared("prepare").write_preparation(work / "preparation.json", preparation)
    bindings = compilation["workflow_inputs"]
    facts.assert_no_unresolved_placeholders(bindings)
    plan = {**stages._sealed_plan(compilation), "request_digest": request.digest}
    source_id = stages._authoritative_source_asset_id(request)
    source = paths[source_id] if source_id else None
    if source is not None and "source_video" in bindings:
        plan["source_av"] = dict(facts.measure_source_av(source))
    stages.save_build_state(work, request=request, document=document, preparation=preparation, source=source, plan=plan)
    trio = Path(compilation["workflow"]["workflow.py"]["path"]).parent
    # The media the graph binds, unpacked from compile's managed-assets archive: {binding: path}.
    resolved = facts.read_archive(compilation["managed_assets"]["path"])
    assets = {}
    for record in resolved.manifest["assets"]:
        target = work / "managed" / record["member"]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(resolved.members[record["member"]])
        assets[record["binding"]] = target
    return {"workflow": _load_graph(trio), "bindings": bindings, "plan": plan, "assets": assets,
            "bundle": str(trio)}


def _load_graph(trio: Path):
    """The trio's workflow graph, through VibeComfy's own bundle loader."""
    from vibecomfy.schema import get_authoring_schema_provider
    from vibecomfy.security.provenance import Provenance
    from vibecomfy.workflow_bundle import load_bundle
    return load_bundle(trio / "workflow.py", trust=Provenance.USER_CONFIRMED,
                       schema_provider=get_authoring_schema_provider(on_demand_schemas=False)).workflow


def after_outputs(job_dir, inputs, files, outputs, *, engine=None):
    """Compose the generated candidate and verify it; a failure raises and fails the job."""
    if engine is not None:
        _shared("engine_facts").install(engine)
    facts = _shared("engine_facts").facts()
    stages = _shared("stages")
    compose_candidate = importlib.import_module(f"{_PACK}.shared.compose").compose_candidate
    verify_candidate = importlib.import_module(f"{_PACK}.shared.verify").verify_candidate

    outputs = list(outputs or [])
    if not outputs:
        raise ValueError("h3 after_outputs received no generated output")
    work = _state_dir(job_dir)
    state = stages.load_build_state(work)
    preparation, source = state["preparation"], state["source"]
    composition = compose_candidate(preparation=preparation, generated=outputs[0], source=source,
                                    out_dir=work / "compose")
    verification = verify_candidate(preparation=preparation, composition=composition, source=source,
                                    candidate=composition["candidate"]["path"])
    facts.write_json_atomic(work / "verify.json", verification)
    return [composition["candidate"]["path"], *outputs[1:]]
