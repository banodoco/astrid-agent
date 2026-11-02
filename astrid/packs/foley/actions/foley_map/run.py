#!/usr/bin/env python3
"""Foley Map action: tile → VLM → Foley → review."""


from __future__ import annotations

from astrid.pack_api import guard_canonical_entrypoint

guard_canonical_entrypoint('foley.foley_map')
import argparse
import hashlib  # noqa: E402
import json
import os  # noqa: E402
import re
import sys
import tempfile  # noqa: E402
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone  # noqa: E402
from pathlib import Path
from typing import Any

from astrid import sdk  # noqa: E402
from astrid.pack_api import build_manifest, write_manifest  # noqa: E402
from astrid.pack_api import add_choice_arg
from astrid.pack_api import AstridError
from astrid.pack_api import run_subprocess

GLOBAL_QUERY = (
    "You are designing AMBIENT atmosphere audio for a video — drones, hums, "
    "textures, room tone, environmental beds. NOT discrete sound effects, NOT "
    "music, NOT speech. In 2-3 sentences, describe the scene's ambient "
    "soundscape: setting, materials, mood, the textural sound of the "
    "environment itself. Plain prose, no lists. Do not mention 'video' or "
    "'frame'."
)


def _tile_query(global_context: str, row: int, col: int, rows: int, cols: int) -> str:
    return (
        f"Global ambient scene: {global_context}\n\n"
        f"This image shows the (row {row}, col {col}) region of a {rows}x{cols} "
        f"grid covering that scene. Write a single audio prompt — about 20 "
        f"words — that combines TWO things:\n"
        f"  (1) an AMBIENT BED suited to this region's materials and motion "
        f"(drone, hum, room tone, environmental texture). Keep this dominant.\n"
        f"  (2) ONE distinct, quirky, or playful detail specific to what's in "
        f"this region — a small character noise, an unusual material gesture, "
        f"a single tiny flourish that gives this region its own personality.\n"
        f"No music, no speech. Do not mention 'tile', 'crop', 'region', or "
        f"'image'. Output only the prompt."
    )



def step_tile(args: argparse.Namespace, out: Path) -> Path:
    cmd = [
        sys.executable, "-X", "utf8", "-m", "astrid.packs.foley.actions.tile_video.run",
        "--video", str(args.video),
        "--out", str(out),
        "--grid", f"{args.grid[0]}x{args.grid[1]}",
        "--overlap", str(args.overlap),
    ]
    if args.trim is not None:
        cmd += ["--trim", str(args.trim)]
    if args.dry_run:
        cmd += ["--dry-run"]
    run_subprocess(cmd, label="tile_video", orchestrator="foley_map")
    return out / "tiles.json"


def _contained_file(root: Path, path: Path) -> Path:
    """Accept only an existing file beneath this parent output root."""
    resolved = path.resolve(strict=True)
    try:
        resolved.relative_to(root.resolve())
    except ValueError as exc:
        raise AstridError("Foley child file escapes the parent output root") from exc
    if not resolved.is_file():
        raise AstridError("Foley child output is not a file")
    return resolved


def _producer_file(path: Path, root: Path, output_port: str, media_type: str) -> dict[str, str]:
    source = _contained_file(root, path)
    return {"filename": source.relative_to(root.resolve()).as_posix(),
            "output_port": output_port, "media_type": media_type}


def _env_input(env_file: Path | None) -> dict[str, Any]:
    if env_file is None:
        return {}
    # This is the original admitted parent file, not a producer-derived object.
    # The bridge verifies that the digest is in the persisted parent policy.
    # Do not parse or extract provider secrets in this pack.
    object_id = "sha256:" + hashlib.sha256(env_file.read_bytes()).hexdigest()
    return {"env_file": {"object_id": object_id, "filename": "foley-env.env"}}


def _atomic_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".foley-", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _child_outputs(capability_id: str, inputs: dict[str, Any], root: Path,
                   receipt: Path, key: str, ports: tuple[str, ...]) -> dict[str, bytes]:
    child = sdk.invoke(capability_id, kind="action", inputs=inputs, child_key=key,
                       wait=True, timeout_seconds=3600, poll_seconds=0.1)
    rows = child.outputs.get("managed_outputs", [])
    # Persist public child lineage even on a failed/uncertain wait. A timeout
    # is not a terminal or cancellation receipt.
    provenance = {"capability_id": capability_id, "child_key": key,
                  "run_id": child.kernel_run_id, "task_id": child.kernel_task_id,
                  "attempt_id": child.kernel_attempt_id,
                  "state": child.raw_result.get("state", "unknown"),
                  "ok": child.ok, "outputs": rows, "error": child.error}
    _atomic_bytes(receipt, (json.dumps(provenance, indent=2) + "\n").encode())
    if not child.ok or child.raw_result.get("state") not in {"completed", "succeeded"}:
        raise AstridError(f"Foley child {capability_id} did not complete: "
                          f"task={child.kernel_task_id}, state={provenance['state']}")
    selected = {}
    # Check all required port identities before requesting any materialization.
    for port in ports:
        matches = [row for row in rows if row.get("output_port") == port]
        if len(matches) != 1:
            raise AstridError(f"Foley child {capability_id} must return exactly one {port} output")
        selected[port] = matches[0]
    data = {}
    for port, row in selected.items():
        local = child.materialize_output(row["association_id"])
        if dict(local.output) != row or local.output["output_port"] != port:
            raise AstridError("Foley materialized child output identity changed")
        source = _contained_file(root, root / local.filename)
        payload = source.read_bytes()
        if len(payload) != row["size"] or "sha256:" + hashlib.sha256(payload).hexdigest() != row["digest"]:
            raise AstridError("Foley materialized child output bytes disagree with receipt")
        data[port] = payload
    return data


def _visual_understand_query(image: Path, query: str, env_file: Path | None,
                              out_json: Path, dry_run: bool) -> str:
    if dry_run:
        return f"[dry-run prompt for {image.name}]"
    root = out_json.parent
    inputs = {"image": _producer_file(image, root, "frames", "image/png"),
              "query": query, "mode": "fast", "max_output_tokens": 300,
              **_env_input(env_file)}
    key = "foley-vlm-" + hashlib.sha256((str(root.resolve()) + "\0" + out_json.name + "\0" + query).encode()).hexdigest()
    outputs = _child_outputs("understanding.visual_understand", inputs, root,
                             out_json.with_suffix(".json.child.json"), key, ("result",))
    data = json.loads(outputs["result"])
    results = data.get("results") or []
    if not results or results[0].get("status") != "ok":
        raise AstridError(f"VLM call failed: {results}")
    answer = results[0].get("answer") or ""
    if not isinstance(answer, str):
        raise AstridError("VLM answer must be text")
    _atomic_bytes(out_json, outputs["result"])
    return answer.strip()


def step_prompts(args: argparse.Namespace, out: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    prompts_path = out / "prompts.json"
    if prompts_path.exists() and not args.force_prompts:
        return json.loads(prompts_path.read_text(encoding="utf-8"))

    global_frame = (out / manifest["global_first_frame"]).resolve()
    rows = manifest["grid"]["rows"]
    cols = manifest["grid"]["cols"]

    global_context = _visual_understand_query(
        global_frame, GLOBAL_QUERY,
        args.env_file, out / "_vlm_global.json",
        args.dry_run,
    )

    tile_prompts: dict[str, str] = {}
    work = []
    for tile in manifest["tiles"]:
        frame_abs = (out / tile["first_frame"]).resolve()
        out_json = out / f"_vlm_{tile['id']}.json"
        query = _tile_query(global_context, tile["row"], tile["col"], rows, cols)
        work.append((tile["id"], frame_abs, query, out_json))

    # VLM calls are network-bound but cheap; run a few in parallel.
    with ThreadPoolExecutor(max_workers=args.vlm_concurrency) as pool:
        futures = {
            pool.submit(_visual_understand_query, fp, q, args.env_file, oj, args.dry_run): tid
            for (tid, fp, q, oj) in work
        }
        for fut in as_completed(futures):
            tid = futures[fut]
            tile_prompts[tid] = fut.result()

    prompts_payload = {
        "global_context": global_context,
        "tile_prompts": tile_prompts,
    }
    prompts_path.write_text(json.dumps(prompts_payload, indent=2) + "\n", encoding="utf-8")
    print(f"[foley_map] wrote prompts: {prompts_path}")
    return prompts_payload


def _foley_one(clip: Path, prompt: str, out_audio: Path, env_file: Path | None,
                dry_run: bool) -> None:
    if dry_run:
        return
    root = out_audio.parent.parent
    inputs = {"clip": _producer_file(clip, root, "tiles", "video/mp4"),
              "prompt": prompt, **_env_input(env_file)}
    key = "foley-audio-" + hashlib.sha256((str(root.resolve()) + "\0" + out_audio.name + "\0" + prompt).encode()).hexdigest()
    outputs = _child_outputs("fal.fal_foley", inputs, root,
                             out_audio.with_suffix(".wav.child.json"), key,
                             ("audio", "audio_provenance"))
    # Both files must be authenticated and read before replacing a cached pair.
    _atomic_bytes(out_audio.with_suffix(".wav.fal.json"), outputs["audio_provenance"])
    _atomic_bytes(out_audio, outputs["audio"])


def step_foley(args: argparse.Namespace, out: Path, manifest: dict[str, Any],
                prompts: dict[str, Any], retry_ids: set[str] | None) -> dict[str, Any]:
    audio_dir = out / "audio"
    audio_dir.mkdir(parents=True, exist_ok=True)
    tile_prompts = prompts["tile_prompts"]

    work = []
    for tile in manifest["tiles"]:
        tid = tile["id"]
        out_audio = audio_dir / f"{tile['row']}_{tile['col']}.wav"
        existing = out_audio.exists() and (retry_ids is None or tid not in retry_ids)
        if retry_ids is not None and tid not in retry_ids and existing:
            continue
        if existing and not args.force_foley:
            continue
        clip_abs = (out / tile["tile_clip"]).resolve()
        work.append((tid, clip_abs, tile_prompts[tid], out_audio))

    print(f"[foley_map] foley work: {len(work)} tiles "
          f"(skipped {len(manifest['tiles']) - len(work)} cached)")

    with ThreadPoolExecutor(max_workers=args.foley_concurrency) as pool:
        futures = [
            pool.submit(_foley_one, clip, prompt, out_audio, args.env_file, args.dry_run)
            for (_tid, clip, prompt, out_audio) in work
        ]
        for fut in as_completed(futures):
            fut.result()

    # Augment manifest with prompt + audio paths for downstream tools.
    enriched = dict(manifest)
    enriched_tiles = []
    for tile in manifest["tiles"]:
        tid = tile["id"]
        audio_rel = f"audio/{tile['row']}_{tile['col']}.wav"
        enriched_tiles.append({
            **tile,
            "prompt": tile_prompts.get(tid, ""),
            "foley_audio": audio_rel,
        })
    enriched["tiles"] = enriched_tiles
    enriched["global_context"] = prompts["global_context"]
    return enriched


def step_review(out: Path, enriched: dict[str, Any]) -> Path:
    manifest_path = out / "tiles.json"
    manifest_path.write_text(json.dumps(enriched, indent=2) + "\n", encoding="utf-8")
    review_path = out / "review.html"
    cmd = [
        sys.executable, "-X", "utf8", "-m", "astrid.packs.foley.actions.foley_review.run",
        "--manifest", str(manifest_path),
        "--out", str(review_path),
    ]
    run_subprocess(cmd, label="foley_review", orchestrator="foley_map")
    return review_path


def _parse_grid(value: str) -> tuple[int, int]:
    match = re.fullmatch(r"\s*([1-9][0-9]*)\s*[xX]\s*([1-9][0-9]*)\s*", value)
    if not match:
        raise argparse.ArgumentTypeError("--grid must be COLSxROWS, e.g. 4x4")
    return int(match.group(1)), int(match.group(2))


def _parse_concurrency(value: str) -> int:
    try:
        count = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("concurrency must be an integer from 1 through 64") from exc
    if not 1 <= count <= 64:
        raise argparse.ArgumentTypeError("concurrency must be an integer from 1 through 64")
    return count


def _load_flagged(path: Path) -> set[str]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    flags = raw.get("flags") or raw  # allow {flags: {...}} or {...} directly
    return {tile_id for tile_id, status in flags.items() if status == "bad"}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Spatial Foley pipeline: tile → VLM → Foley → review.")
    p.add_argument("--video", type=Path, required=True, help="Source video.")
    p.add_argument("--out", type=Path, required=True, help="Output directory.")
    p.add_argument("--grid", type=_parse_grid, default=(4, 4), help="COLSxROWS, default 4x4.")
    p.add_argument("--overlap", type=float, default=0.25, help="Tile overlap fraction (default 0.25).")
    p.add_argument("--trim", type=float, default=None, help="Trim each tile clip to this many seconds.")
    p.add_argument("--env-file", type=Path, help="Env file with FAL_KEY and OPENAI_API_KEY.")
    p.add_argument("--vlm-concurrency", type=_parse_concurrency, default=4, help="Parallel VLM calls.")
    p.add_argument("--foley-concurrency", type=_parse_concurrency, default=4, help="Parallel fal Foley calls.")
    p.add_argument("--retry-flagged", type=Path, default=None,
                   help="Path to flagged.json (downloaded from review.html); only re-run tiles flagged 'bad'.")
    p.add_argument("--force-prompts", action="store_true", help="Re-run VLM prompts even if prompts.json exists.")
    p.add_argument("--force-foley", action="store_true", help="Re-run Foley calls even when audio file exists.")
    add_choice_arg(p, "--stop-after", values=("tile", "prompts", "foley", "review"), default="review",
                   help="Stop after the named stage.")
    p.add_argument("--dry-run", action="store_true",
                   help="Plan everything; tile_video runs (cheap), VLM and Foley are stubbed.")
    return p


def _write_result(out: Path, args: argparse.Namespace) -> None:
    """Receipt only the existing declared artifacts at a successful stop stage."""
    outputs = []
    for name, relative in (
        ("tiles_manifest", "tiles.json"), ("prompts", "prompts.json"),
        ("tiles", "tiles"), ("frames", "frames"),
        ("audio", "audio"), ("review_html", "review.html"),
    ):
        path = out / relative
        if path.exists():
            outputs.append({"name": name, "path": relative,
                            "type": "directory" if path.is_dir() else "file",
                            "role": "result" if name == "tiles_manifest" else "auxiliary",
                            "is_primary": name == "tiles_manifest"})
    write_manifest(out / "manifest.json", build_manifest(
        kind="foley.foley_map", inputs={"video": str(args.video), "stop_after": args.stop_after,
                                      "dry_run": args.dry_run}, outputs=outputs,
        created=datetime.now(timezone.utc).isoformat()))


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    out = args.out.expanduser().resolve()
    out.mkdir(parents=True, exist_ok=True)

    retry_ids = _load_flagged(args.retry_flagged) if args.retry_flagged else None

    print("[foley_map] step 1/5: tile_video")
    tiles_manifest_path = step_tile(args, out)
    if args.stop_after == "tile":
        _write_result(out, args)
        return 0
    manifest = json.loads(tiles_manifest_path.read_text(encoding="utf-8"))

    print("[foley_map] step 2/5: visual_understand (global + per-tile)")
    prompts = step_prompts(args, out, manifest)
    if args.stop_after == "prompts":
        _write_result(out, args)
        return 0

    print(f"[foley_map] step 3/5: fal_foley × {len(manifest['tiles'])}")
    enriched = step_foley(args, out, manifest, prompts, retry_ids)
    if args.stop_after == "foley":
        # Still write the enriched manifest for resumption.
        (out / "tiles.json").write_text(json.dumps(enriched, indent=2) + "\n", encoding="utf-8")
        _write_result(out, args)
        return 0

    print("[foley_map] step 4/5: foley_review")
    review_path = step_review(out, enriched)
    print(f"open file://{review_path}")
    if args.stop_after == "review":
        _write_result(out, args)
        return 0

    print(f"[foley_map] review complete: {review_path}")
    _write_result(out, args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
