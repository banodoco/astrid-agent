"""Public, disposable acceptance for the saved-composition visualizer path.

The test deliberately creates its own project, canonical timeline, media, and
saved parent revision. It then creates a successful matching MP4 and verifies
that both automatic and explicit-run visualization still produce frames from
the saved composition. No user project or pre-existing identity is required.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import time
import uuid
import zlib

import pytest
from tests.lanes import unavailable


ROOT = Path(__file__).resolve().parents[2]


def _digest(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _png(path: Path, width: int = 96, height: int = 54) -> None:
    """Write a small valid RGB PNG without an image-library dependency."""
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    rows = b"".join(
        b"\x00" + bytes((35, 126 + (y % 70), 205)) * width
        for y in range(height)
    )
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(rows, level=6))
        + chunk(b"IEND", b"")
    )


def _remotion_runtime_environment(node: Path) -> tuple[dict[str, str], dict[str, object]]:
    """Validate and expose the repository's exact Node/npm engine pair."""
    node = node.resolve(strict=True)
    assert node.is_absolute() and node.is_file() and os.access(node, os.X_OK), node
    npm = node.parent / "npm"
    assert npm.is_file() and os.access(npm, os.X_OK), npm
    project = (ROOT / "remotion").resolve(strict=True)
    package_path = project / "package.json"
    lock_path = project / "package-lock.json"
    package = json.loads(package_path.read_text(encoding="utf-8"))
    engines = package.get("engines", {})
    assert engines.get("node") == "=20.19.4", engines
    assert engines.get("npm") == "=10.8.2", engines

    child_env = dict(os.environ)
    child_env["PATH"] = str(node.parent) + os.pathsep + child_env.get("PATH", "")
    child_env["ASTRID_NODE_EXECUTABLE"] = str(node)
    child_env["ASTRID_REMOTION_PROJECT_DIR"] = str(project)
    child_env["ASTRID_TIMELINE_SCHEMA_PYTHONPATH"] = str(
        project / "node_modules" / "@banodoco" / "timeline-schema" / "python"
    )
    child_env["NPM_CONFIG_USERCONFIG"] = "/dev/null"

    def version(command: list[str]) -> str:
        result = subprocess.run(
            command,
            cwd=project,
            env=child_env,
            capture_output=True,
            check=False,
            text=True,
            timeout=15,
        )
        assert result.returncode == 0, result.stderr
        return result.stdout.strip()

    node_version = version([str(node), "--version"])
    npm_version = version([str(npm), "--version"])
    assert node_version == "v20.19.4", node_version
    assert npm_version == "10.8.2", npm_version
    return child_env, {
        "node": {
            "path": str(node),
            "sha256": hashlib.sha256(node.read_bytes()).hexdigest(),
            "version": node_version,
        },
        "npm": {
            "path": str(npm),
            "resolved_path": str(npm.resolve(strict=True)),
            "sha256": hashlib.sha256(npm.resolve(strict=True).read_bytes()).hexdigest(),
            "version": npm_version,
        },
        "remotion_project": str(project),
        "package_json_sha256": hashlib.sha256(package_path.read_bytes()).hexdigest(),
        "package_lock_sha256": hashlib.sha256(lock_path.read_bytes()).hexdigest(),
        "declared_engines": engines,
        "timeline_schema_pythonpath": child_env["ASTRID_TIMELINE_SCHEMA_PYTHONPATH"],
    }
def _load_public_fixture():
    helper = Path(os.environ["ASTRID_C06_PUBLIC_WORKSPACE_HELPER"])
    spec = importlib.util.spec_from_file_location("c06_public_workspace_fixture", helper)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _copy_evidence(source: Path, destination: Path) -> dict[str, object]:
    destination.parent.mkdir(parents=True, exist_ok=True)
    data = source.read_bytes()
    destination.write_bytes(data)
    return {"path": str(destination), "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}


def _successful_render(client, project_id: str, timeline_id: str, out_root: Path, remotion: Path) -> dict:
    result = client.invoke_result(
        "rendering.render",
        kind="executor",
        project=project_id,
        inputs={
            "timeline_ref": timeline_id,
            "selector": "rendering.remotion",
            "output_name": "c06-matching-render.mp4",
            "backend_config": {
                "rendering.remotion": {
                    "project_dir": str(remotion),
                    "composition_id": "TimelineComposition",
                }
            },
        },
        out=out_root,
        wait=os.environ.get("ASTRID_C06_ABORT_AFTER_ADMISSION") != "1",
        timeout_seconds=240,
        poll_seconds=0.2,
    )
    if os.environ.get("ASTRID_C06_ABORT_AFTER_ADMISSION") == "1":
        assert result.ok and result.run_id and result.kernel_task_id, result.to_dict()
        helper = _load_public_fixture()
        helper.record_controller_admission(os.environ["ASTRID_C06_CONTROLLER_DESCRIPTOR"], result)
        # The driver discriminates real public admission, then hard-aborts this
        # workload while the creator survives. No fake/component proof counts.
        while True:
            time.sleep(0.2)
    if not result.ok:
        error = result.error
        if isinstance(error, dict):
            detail = error.get("message") or error
        else:
            detail = getattr(error, "message", None) or error or result.to_dict()
        raise AssertionError(f"render executor failed: {detail!r}; result={result.to_dict()!r}")
    assert result.run_id and result.kernel_task_id and result.kernel_attempt_id
    return result.to_dict()


def _saved_composed_capture(frame_index_path: Path, *, project_id: str,
                            timeline_id: str, saved_revision: str, frame: int) -> tuple[dict, dict, Path]:
    """Read the compact v2 receipt and its hash-bound authored snapshot."""
    frame_index = json.loads(frame_index_path.read_text(encoding="utf-8"))
    assert frame_index["schema"] == "astrid.filmstrip.v2", frame_index
    provenance = frame_index["provenance"]
    assert provenance["timeline_id"] == timeline_id, provenance
    sidecar = frame_index["snapshot_sidecar"]
    assert sidecar["verified"] is True and sidecar["path"] == "render-snapshot.json", sidecar
    snapshot_path = frame_index_path.parent / sidecar["path"]
    raw = snapshot_path.read_bytes()
    assert len(raw) == sidecar["bytes"], sidecar
    assert "sha256:" + hashlib.sha256(raw).hexdigest() == sidecar["digest"], sidecar
    snapshot = json.loads(raw)
    assert snapshot["video_digest"] == provenance["video_digest"] == sidecar["render_digest"]
    metadata = snapshot["metadata"]
    assert metadata["selection"] == "composed_frame_capture", metadata
    authority = metadata["capture_authority"]
    assert authority["project_id"] == project_id and authority["timeline_id"] == timeline_id, authority
    assert authority["parent_revision_id"] == saved_revision, authority
    assert metadata["requested_revision_id"] == saved_revision, metadata
    capture = provenance["frame_capture"]
    assert capture["evidence_source"] == "fresh_capture", capture
    assert capture["requested_frame_count"] == 1 and capture["requested_frame_bounds"] == [frame, frame], capture
    assert frame_index["sampling"]["requested_frame"] == frame, frame_index["sampling"]
    assert [card["frame"] for card in frame_index["cards"]] == [frame], frame_index["cards"]
    return frame_index, capture, snapshot_path


def _assert_composed_view(client, project_id: str, *, mode: str, run_id: str | None,
                          timeline_id: str, matching_render_id: str, frame: int, run_root: Path,
                          label: str, saved_revision: str) -> dict:
    options: dict[str, object] = {"frame": frame, "resolution": "160x90"}
    if run_id is not None:
        options["render_run"] = run_id
    result = client.timelines.visualize(
        project_id,
        timeline_id,
        mode=mode,
        options=options,
        formats=("png", "md"),
    )
    assert result.ok, result.to_dict()
    matching_render = client.runs.show(matching_render_id)
    assert matching_render.ok, matching_render
    render_status = matching_render.data.get("status")
    assert render_status in {"completed", "succeeded"}, matching_render.data
    frame_index_path = Path(str(result.outputs["frame_index"]))
    frame_index, capture, snapshot_path = _saved_composed_capture(
        frame_index_path, project_id=project_id, timeline_id=timeline_id,
        saved_revision=saved_revision, frame=frame,
    )
    assert "media" not in result.outputs

    durable = run_root / "c06-output" / label
    index_record = _copy_evidence(frame_index_path, durable / "frame-index.json")
    snapshot_record = _copy_evidence(snapshot_path, durable / "render-snapshot.json")
    manifest_record = _copy_evidence(
        Path(str(result.outputs["manifest_path"])), durable / "manifest.json"
    )
    pages = [Path(str(item)) for item in result.outputs.get("pages", [])]
    assert pages, result.outputs
    page_records = [_copy_evidence(page, durable / f"frame-{number:02d}.png")
                    for number, page in enumerate(pages)]
    return {
        "result": result.to_dict(),
        "frame_index": index_record,
        "snapshot": snapshot_record,
        "manifest": manifest_record,
        "pages": page_records,
        "capture": capture,
        "saved_parent_revision_id": saved_revision,
        "requested_frame": frame,
        "explicit_render_run": run_id,
        "matching_render_status": matching_render.data,
    }


@pytest.mark.workstation
def test_public_visualizer_always_composes_current_saved_revision_with_matching_render(tmp_path, monkeypatch):
    if os.environ.get("ASTRID_C06_PUBLIC_ACCEPTANCE") != "1":
        unavailable("set ASTRID_C06_PUBLIC_ACCEPTANCE=1 only in the bounded public acceptance run")

    run_root = Path(os.environ["ASTRID_STAGE1_ATTEMPT_ROOT"]).resolve()
    helper = _load_public_fixture()
    node_env, remotion_runtime = _remotion_runtime_environment(
        Path(os.environ["ASTRID_C06_NODE"])
    )
    for name in ("PATH", "ASTRID_NODE_EXECUTABLE", "ASTRID_REMOTION_PROJECT_DIR",
                 "ASTRID_TIMELINE_SCHEMA_PYTHONPATH", "NPM_CONFIG_USERCONFIG"):
        monkeypatch.setenv(name, node_env[name])

    descriptor_path = Path(os.environ["ASTRID_C06_CONTROLLER_DESCRIPTOR"])
    client, controller = helper.controller_connection(descriptor_path)
    operation_error: BaseException | None = None
    evidence: dict[str, object] = {
        "schema": "astrid-c06-public-visualizer/v1",
        "workspace_provenance": controller["workspace_provenance"],
        "project": None,
        "timeline": None,
        "render": None,
        "views": [],
        "remotion_runtime": remotion_runtime,
    }

    try:
        os.environ["ASTRID_RUNTIME_DATA_ROOT"] = controller["workspace_provenance"]["support_root"]
        os.environ["ASTRID_RUNTIME_HOME"] = controller["workspace_provenance"]["home"]
        os.environ["ASTRID_RUNTIME_SOURCE_MANIFEST"] = controller["workspace_provenance"]["source_manifest"]
        os.environ["ASTRID_PROJECTS_ROOT"] = str(Path(os.environ["ASTRID_STAGE1_SCRATCH_ROOT"]) / "projects")
        os.environ["ASTRID_REMOTION_PROJECT_DIR"] = str(ROOT / "remotion")

        suffix = controller["suffix"]
        project_id = controller["project"]["project_id"]
        evidence["project"] = dict(controller["project"])
        helper.require_controller_submission(descriptor_path)

        source = tmp_path / "c06-picture.png"
        _png(source)
        media = client.media.import_file(
            project=project_id, path=source, idempotency_key=f"c06-media-{suffix}"
        )
        assert media.ok and media.data.get("object_id"), media
        object_id = str(media.data["object_id"])

        transport = client._remote._transport
        requested_timeline_id = f"c06-main-{suffix}"
        timeline_create = transport.create_timeline(
            project_id, requested_timeline_id, idempotency_key=f"c06-timeline-{suffix}"
        )
        assert timeline_create
        timeline_data = timeline_create.get("data", timeline_create)
        timeline_id = str(timeline_data.get("timeline_id") or requested_timeline_id)
        assert timeline_id == requested_timeline_id, timeline_create

        # One real saved shot and pinned child timeline gives the renderer a
        # canonical immutable composition. The IDs are derived locally, not
        # supplied by the user or inferred from old workspace state.
        requested_shot_id = f"c06-shot-{suffix}"
        shot_revision_id = f"c06-shot-rev-{suffix}"
        internal_revision_id = f"c06-internal-rev-{suffix}"
        created_shot = client.shots.create(
            project=project_id,
            shot={"shot_id": requested_shot_id, "name": "C06 still"},
            idempotency_key=f"c06-shot-{suffix}",
        )
        assert created_shot.ok, created_shot
        shot_id = str(created_shot.data.get("shot_id") or requested_shot_id)
        assert shot_id == requested_shot_id, created_shot.data

        internal_payload = {
            "tracks": [{"id": "picture", "kind": "visual", "label": "Picture"}],
            "clips": [{
                "id": f"clip-{suffix}", "track": "picture", "clipType": "media",
                "asset": "c06-picture", "at": 0, "hold": 1,
                "x": 0, "y": 0, "width": 1, "height": 1,
            }],
            "effects": [], "audio": [], "layout": {"width": 160, "height": 90},
            "registry": {"assets": {"c06-picture": {
                # Runtime media identity and the timeline asset type are the
                # render contract; MIME type is read from the admitted object.
                "media_id": object_id, "type": "image",
            }}},
            "assets": [],
        }
        shot_payload = {
            "name": "C06 still", "metadata": {},
            "items": [{"item_id": f"item-{suffix}", "media_id": object_id,
                       "source_frame": None, "metadata": {"role": "primary_visual"}}],
            "pools": [], "selected_variants": {}, "provenance": {"source": "C06 public fixture"},
            "generation_inputs": {"visual": {"object_id": object_id}},
            "audio_bindings": [], "text_bindings": [],
            "internal_timeline_revision_id": internal_revision_id,
        }
        parent_revision_id = f"c06-parent-{suffix}"
        occurrence = {
            "occurrence_id": f"occ-{suffix}", "shot_id": shot_id,
            "shot_revision_id": shot_revision_id,
            "placement": {"start_ms": 0, "track": "picture"},
            "source_offset": {"start": 0, "end": 1000}, "duration_ms": 1000,
            "speed": {"numerator": 1, "denominator": 1}, "track": "picture",
            "transform": {"scale": 1}, "gain": 1, "muted": False, "provenance": {},
        }
        parent_payload = {
            "config": {"tracks": [{"id": "picture", "kind": "visual", "label": "Picture"}],
                       "clips": [], "theme": "banodoco-default"},
            "registry": {"assets": {}}, "clips": [], "occurrences": [occurrence],
        }
        publication = {
            "project_id": project_id, "timeline_id": timeline_id, "expected_head": None,
            "parent_revision_id": parent_revision_id, "parent_composition": parent_payload,
            "shot_revisions": [{"shot_id": shot_id, "revision_id": shot_revision_id,
                                 "internal_timeline_revision_id": internal_revision_id,
                                 "payload": shot_payload,
                                 "content_digest": _digest(shot_payload)}],
            "internal_timeline_revisions": [{"timeline_id": timeline_id,
                                              "revision_id": internal_revision_id,
                                              "payload": internal_payload,
                                              "content_digest": _digest(internal_payload)}],
        }
        publication_result = transport.publish_parent_composition(
            project_id, timeline_id, publication, idempotency_key=f"c06-publish-{suffix}"
        )
        assert publication_result, publication_result
        publication_data = publication_result.get("data", publication_result)
        saved_revision = str(publication_data.get("new_head") or publication_data.get("parent_revision_id") or parent_revision_id)
        assert saved_revision == parent_revision_id, publication_result
        saved_result = transport.get_project_parent_composition_revision(project_id, timeline_id, saved_revision)
        saved = saved_result.get("data", saved_result)
        assert saved.get("revision_id") == saved_revision
        evidence["timeline"] = {
            "timeline_id": timeline_id, "saved_parent_revision_id": saved_revision,
            "shot_id": shot_id, "shot_revision_id": shot_revision_id,
            "internal_timeline_revision_id": internal_revision_id,
            "managed_media_id": object_id,
            "saved_parent_sha256": hashlib.sha256(
                json.dumps(saved, sort_keys=True, separators=(",", ":"), default=str).encode()
            ).hexdigest(),
        }

        helper.require_controller_submission(descriptor_path)
        render = _successful_render(
            client, project_id, timeline_id,
            Path(os.environ["ASTRID_STAGE1_SCRATCH_ROOT"]) / "render-attempt",
            ROOT / "remotion",
        )
        helper.record_controller_admission(descriptor_path, render)
        evidence["render"] = render
        render_run_id = str(render["run_id"])
        exact_render = client.runs.show(render_run_id)
        assert exact_render.ok, exact_render
        evidence["matching_render_status"] = exact_render.data

        # The separately authorized native counterfactual uses this same public
        # admission and retained creator, and claims only render/encode evidence.
        if os.environ.get("ASTRID_C06_RENDER_ENCODE_ONLY") == "1":
            evidence["acceptance_scope"] = "render_encode_counterfactual_only"
            (run_root / "c06-render-encode-counterfactual.json").write_text(
                json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
            return

        helper.require_controller_submission(descriptor_path)
        automatic = _assert_composed_view(
            client, project_id, mode="auto", run_id=None, timeline_id=timeline_id,
            matching_render_id=render_run_id,
            frame=0, run_root=run_root,
            label="automatic-matching-render-present", saved_revision=saved_revision,
        )
        helper.require_controller_submission(descriptor_path)
        explicit = _assert_composed_view(
            client, project_id, mode="composed", run_id=render_run_id, timeline_id=timeline_id,
            matching_render_id=render_run_id, frame=1, run_root=run_root,
            label="explicit-matching-render", saved_revision=saved_revision,
        )
        evidence["views"] = [automatic, explicit]
        evidence["matching_render_present_during_both_views"] = True
        evidence["frame_source"] = "Remotion composed-frame capture from saved canonical parent revision"
        evidence["production_inference_claimed"] = False
        (run_root / "c06-public-acceptance.json").write_text(
            json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    except BaseException as exc:
        operation_error = exc
    finally:
        # The surviving creator writes workspace-stop-result.json after the
        # driver has positively reaped this workload. Pytest holds no owners.
        (run_root / "workload-result.json").write_text(
            json.dumps({"operation_succeeded": operation_error is None,
                        "controller_pid": controller["controller_pid"]}, sort_keys=True) + "\n"
        )
    if operation_error is not None:
        raise operation_error
