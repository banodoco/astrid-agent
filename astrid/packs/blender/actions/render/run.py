#!/usr/bin/env python3
"""Blender render executor.

Renders a Blender scene to a still image or animation, either *locally* (runs
``blender`` as a subprocess on this machine) or on the *cloud* (HTTP POST to a
Blender render API host such as an SSH-deployed host or a RunPod GPU pod).

Inputs
------
A scene can come from either:
  * ``--scene <path.json>`` — a declarative scene spec (see
    ``render_core.DEFAULT_SCENE``); a pleasant default scene is used if omitted.
  * ``--blend <path.blend>`` — an existing Blender file (overrides --scene).

Render settings (``--engine``/``--samples``/``--resolution``/``--frames``/...) map
1:1 to ``render_core.DEFAULT_SETTINGS``. ``--frames 1`` → still PNG; ``--frames
N>1`` → mp4 animation.

Modes
-----
``--execution local``  run blender on this host (``--blender`` overrides path).
``--execution cloud``  POST to ``--cloud-url`` (a Blender render API host).
                      ``--cloud-token`` sets the Bearer token if the host auths.

Outputs (written under ``--out``)
---------------------------------
``render.png`` / ``render.mp4`` and a ``manifest.json`` describing the run.
"""

from __future__ import annotations

# ruff: noqa: E402
from astrid.pack_api import AstridError
from astrid.pack_api import guard_canonical_entrypoint, run_pack_main

guard_canonical_entrypoint("blender.render")
import argparse
import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from astrid.pack_api import StaticChoices

from astrid.packs.blender.render_core import (
    DEFAULT_SCENE,
    build_blend_render_script,
    build_blender_script,
    default_blender_executable,
    ffmpeg_encode_command,
    frame_pattern,
    https_only_opener,
    normalize_scene,
    normalize_settings,
    parse_output_path,
    require_https_mesh_url,
    safe_extract_zip,
)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Render a Blender scene locally or on a cloud render host.")
    p.add_argument("--out", type=Path, required=True, help="Output directory.")
    scene_group = p.add_mutually_exclusive_group()
    scene_group.add_argument("--scene", type=Path, help="Path to a declarative scene spec JSON file.")
    scene_group.add_argument("--blend", type=Path, help="Path to an existing .blend file (overrides --scene).")
    p.add_argument(
        "--execution",
        required=True,
        choices=StaticChoices(("local", "cloud")),
        help="local = run blender here; cloud = POST to a Blender render API host.",
    )
    p.add_argument("--engine", default="cycles", help="cycles (default), eevee, or workbench.")
    p.add_argument("--device", default="cpu", help="cpu (default) or gpu (Cycles only).")
    p.add_argument("--samples", type=int, default=64, help="Cycles samples.")
    p.add_argument("--resolution", default="1280x720", help="WxH, e.g. 1920x1080.")
    p.add_argument("--frames", type=int, default=1, help="1 = still; >1 = animation (mp4).")
    p.add_argument("--fps", type=int, default=24, help="Frames per second for animation.")
    p.add_argument("--denoise", action="store_true", help="Enable Cycles denoising (needs an OIDN-enabled build).")
    p.add_argument(
        "--blender",
        default=None,
        help="Path to blender executable (local mode). Defaults to 'blender' on PATH.",
    )
    p.add_argument("--cloud-url", dest="cloud_url", help="Base URL of the Blender render API host (cloud mode).")
    p.add_argument("--cloud-token", dest="cloud_token", help="Bearer token if the host requires auth.")
    p.add_argument("--cloud-timeout", dest="cloud_timeout", type=int, default=1800, help="Cloud request timeout (s).")
    p.add_argument(
        "--preset",
        default=None,
        help="Named render preset from astrid.packs.blender.renders (e.g. wink_turn). Overrides --scene/--blend.",
    )
    p.add_argument("--mesh-url", dest="mesh_url", default=None, help="Direct mesh URL (.glb/.gltf/.fbx/.zip) used by presets.")
    p.add_argument("--sketchfab-uid", dest="sketchfab_uid", default=None, help="Sketchfab model uid; resolved to a mesh_url via token.")
    p.add_argument("--head-yaw-deg", dest="head_yaw_deg", type=float, default=None, help="wink_turn: head turn angle (deg).")
    p.add_argument("--body-yaw-deg", dest="body_yaw_deg", type=float, default=None, help="wink_turn: body facing flip (deg).")
    return p


# ---------------------------------------------------------------------------
# Scene / settings resolution
# ---------------------------------------------------------------------------


def _load_scene(args: argparse.Namespace) -> tuple[dict[str, Any] | None, str | None]:
    """Return (scene_spec, blend_path). Exactly one may be non-None (scene default used if neither)."""
    if args.blend is not None:
        blend = args.blend.expanduser().resolve()
        if not blend.is_file():
            raise AstridError(
                f"blend file not found: {blend}",
                recovery_command="rerun with an existing --blend file",
            )
        return None, str(blend)
    if args.scene is not None:
        scene_path = args.scene.expanduser().resolve()
        if not scene_path.is_file():
            raise AstridError(
                f"scene spec not found: {scene_path}",
                recovery_command="rerun with an existing --scene JSON file",
            )
        try:
            scene = json.loads(scene_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise AstridError(f"invalid scene JSON: {exc}", recovery_command="fix the scene JSON and retry") from exc
        return scene, None
    return DEFAULT_SCENE, None


def _settings_from_args(args: argparse.Namespace) -> dict[str, Any]:
    s = normalize_settings(
        {
            "engine": args.engine,
            "device": args.device,
            "samples": args.samples,
            "resolution": args.resolution,
            "frames": args.frames,
            "fps": args.fps,
            "denoise": args.denoise,
        }
    )
    if getattr(args, "head_yaw_deg", None) is not None:
        s["head_yaw_deg"] = args.head_yaw_deg
    if getattr(args, "body_yaw_deg", None) is not None:
        s["body_yaw_deg"] = args.body_yaw_deg
    return s


def _resolve_mesh_url(args: argparse.Namespace) -> str | None:
    if getattr(args, "sketchfab_uid", None):
        from astrid.packs.blender import mesh_fetch

        url: str | None = mesh_fetch.resolve_sketchfab_mesh_url(args.sketchfab_uid)
    else:
        url = getattr(args, "mesh_url", None)
    if not url:
        return None
    try:
        return require_https_mesh_url(url)
    except ValueError as exc:
        raise AstridError(
            str(exc),
            recovery_command="pass an https:// --mesh-url (or use --sketchfab-uid)",
        ) from exc


def _fetch(url: str, dest: str) -> None:
    """Stream ``url`` to ``dest``, refusing redirects off ``https``."""
    with https_only_opener().open(url, timeout=300) as resp, open(dest, "wb") as fh:
        shutil.copyfileobj(resp, fh)


def _download_mesh_to_dir(url: str, work_dir: Path) -> str:
    """Download a mesh URL into work_dir (extract archives); return model path."""
    import zipfile

    low = url.split("?")[0].lower()
    ext = ""
    for e in (".glb", ".gltf", ".fbx", ".zip"):
        if low.endswith(e):
            ext = e
            break
    if ext in ("", ".zip"):
        archive = str(work_dir / ("mesh.zip" if ext == ".zip" else "mesh.bin"))
        _fetch(url, archive)
        if zipfile.is_zipfile(archive):
            with zipfile.ZipFile(archive) as zf:
                safe_extract_zip(zf, str(work_dir))
            for e in (".glb", ".gltf", ".fbx"):
                for p in work_dir.rglob(f"*{e}"):
                    return str(p)
        os.rename(archive, str(work_dir / "mesh.glb"))
        return str(work_dir / "mesh.glb")
    dest = str(work_dir / f"mesh{ext}")
    _fetch(url, dest)
    return dest


# ---------------------------------------------------------------------------
# Local render
# ---------------------------------------------------------------------------


def _run_local(
    args: argparse.Namespace,
    scene: dict[str, Any] | None,
    blend: str | None,
    settings: dict[str, Any],
    out_dir: Path,
) -> dict[str, Any]:
    blender = args.blender or default_blender_executable()
    if not shutil.which(blender) and not os.path.exists(blender):
        raise AstridError(
            f"blender executable not found: {blender}",
            recovery_command="install blender or pass --blender /path/to/blender",
        )
    animation = int(settings.get("frames", 1)) > 1

    with tempfile.TemporaryDirectory(prefix="astrid-blender-") as work:
        work_dir = Path(work)
        if getattr(args, "preset", None):
            from astrid.packs.blender import renders

            mesh_file = ""
            mesh_url = _resolve_mesh_url(args)
            if mesh_url:
                mesh_file = _download_mesh_to_dir(mesh_url, work_dir)
            body = renders.get_builder(args.preset)(settings)
            full = body.replace("__MESH_FILE__", repr(mesh_file)).replace("__OUTPUT__", repr(str(work_dir)))
            script_path = work_dir / "render_preset.py"
            script_path.write_text(full, encoding="utf-8")
            argv = [blender, "-b", "--factory-startup", "-P", str(script_path)]
            animation = True
        elif blend:
            script_path = work_dir / "render_settings.py"
            script_path.write_text(
                build_blend_render_script(settings, str(work_dir), animation=animation),
                encoding="utf-8",
            )
            argv = [blender, "-b", "--factory-startup", blend, "-P", str(script_path)]
        else:
            script_path = work_dir / "render_scene.py"
            out_target = str(work_dir) if animation else str(work_dir / "out.png")
            script_path.write_text(
                build_blender_script(scene, settings, out_target, animation=animation),
                encoding="utf-8",
            )
            argv = [blender, "-b", "--factory-startup", "-P", str(script_path)]

        t0 = time.time()
        proc = subprocess.run(argv, capture_output=True, text=True)
        render_ms = int((time.time() - t0) * 1000)
        if proc.returncode != 0:
            tail = (proc.stderr or proc.stdout)[-3000:]
            raise AstridError(
                f"blender exited {proc.returncode}: {tail}",
                recovery_command="check the scene spec / settings and retry, or try --device cpu",
            )

        if animation:
            mp4 = out_dir / "render.mp4"
            enc = subprocess.run(
                ffmpeg_encode_command(frame_pattern(str(work_dir)), str(mp4), fps=int(settings.get("fps", 24))),
                capture_output=True,
                text=True,
            )
            if enc.returncode != 0:
                raise AstridError(f"ffmpeg encode failed: {enc.stderr[-2000:]}", recovery_command="install ffmpeg")
            out_file = mp4
            out_type = "video/mp4"
        else:
            produced = None
            for line in (proc.stdout or "").splitlines():
                produced = parse_output_path(line) or produced
            produced = produced or str(work_dir / "out.png")
            if not os.path.exists(produced):
                raise AstridError(
                    f"render produced no output: {(proc.stderr or '')[-2000:]}",
                    recovery_command="check blender stderr and scene spec",
                )
            out_file = out_dir / "render.png"
            shutil.copy2(produced, out_file)
            out_type = "image/png"

    return {
        "out_file": str(out_file),
        "output_type": out_type,
        "render_ms": render_ms,
        "engine": settings.get("engine", "cycles"),
        "frames": int(settings.get("frames", 1)),
    }


# ---------------------------------------------------------------------------
# Cloud render
# ---------------------------------------------------------------------------


def _run_cloud(
    args: argparse.Namespace,
    scene: dict[str, Any] | None,
    blend: str | None,
    settings: dict[str, Any],
    out_dir: Path,
) -> dict[str, Any]:
    if not args.cloud_url:
        raise AstridError(
            "cloud execution requires --cloud-url (the Blender render API host)",
            recovery_command="deploy a render host and pass --cloud-url http://<host>:8778",
        )
    base = args.cloud_url.rstrip("/")
    payload: dict[str, Any] = {"settings": settings}
    if getattr(args, "preset", None):
        from astrid.packs.blender import renders

        payload["script"] = renders.get_builder(args.preset)(settings)
        mesh_url = _resolve_mesh_url(args)
        if mesh_url:
            payload["mesh_url"] = mesh_url
    elif blend:
        payload["blend_b64"] = base64.b64encode(Path(blend).read_bytes()).decode("ascii")
        payload["blend_name"] = os.path.basename(blend)
    else:
        payload["scene"] = normalize_scene(scene)

    headers = {"Content-Type": "application/json"}
    if args.cloud_token:
        headers["Authorization"] = f"Bearer {args.cloud_token}"
    body = json.dumps(payload).encode("utf-8")
    url = base + "/render"
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")

    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=args.cloud_timeout) as resp:
            if resp.status != 200:
                raise AstridError(
                    f"cloud render host returned HTTP {resp.status}: {resp.read().decode('utf-8', 'replace')[:1000]}",
                    recovery_command="check the render host logs and retry",
                )
            data = resp.read()
            transfer_ms = int((time.time() - t0) * 1000)
            render_ms = int(resp.headers.get("X-Render-Ms", 0) or 0)
            engine = resp.headers.get("X-Blender-Engine", settings.get("engine", "cycles"))
            blender_version = resp.headers.get("X-Blender-Version", "")
            is_animation = resp.headers.get("X-Output-Type", "still") == "animation"
            content_type = resp.headers.get("Content-Type", "application/octet-stream")
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:1000]
        except Exception:
            pass
        raise AstridError(
            f"cloud render failed (HTTP {exc.code}): {detail}",
            recovery_command="check the render host (/health) and scene spec, then retry",
        ) from exc
    except urllib.error.URLError as exc:
        raise AstridError(
            f"could not reach cloud render host {url}: {exc.reason}",
            recovery_command="verify the host is up and --cloud-url is correct",
        ) from exc

    if is_animation or "video" in content_type:
        out_file = out_dir / "render.mp4"
        out_type = "video/mp4"
    else:
        out_file = out_dir / "render.png"
        out_type = "image/png"
    out_file.write_bytes(data)

    return {
        "out_file": str(out_file),
        "output_type": out_type,
        "render_ms": render_ms,
        "transfer_ms": transfer_ms,
        "engine": engine,
        "blender_version": blender_version,
        "frames": int(settings.get("frames", 1)),
        "cloud_url": base,
    }


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------


def _write_manifest(out_dir: Path, result: dict[str, Any], args: argparse.Namespace) -> Path:
    manifest = {
        "executor": "blender.render",
        "execution": args.execution,
        "engine": result.get("engine"),
        "frames": result.get("frames"),
        "output_type": result.get("output_type"),
        "render_ms": result.get("render_ms"),
        "out_file": result.get("out_file"),
    }
    if args.execution == "cloud":
        manifest["cloud_url"] = result.get("cloud_url")
        manifest["blender_version"] = result.get("blender_version")
        manifest["transfer_ms"] = result.get("transfer_ms")
    path = out_dir / "manifest.json"
    path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return path


def main(argv: list[str] | None = None) -> int:
    def _run() -> int:
        args = build_parser().parse_args(argv)
        out_dir = args.out.expanduser().resolve()
        out_dir.mkdir(parents=True, exist_ok=True)
        render_dir = out_dir / "render"
        render_dir.mkdir(parents=True, exist_ok=True)
        if args.preset:
            scene, blend = None, None
        else:
            scene, blend = _load_scene(args)
        settings = _settings_from_args(args)
        if args.preset and int(settings.get("frames", 1)) <= 1:
            settings["frames"] = 60  # presets are animations; default to 60 frames

        if args.execution == "local":
            result = _run_local(args, scene, blend, settings, render_dir)
        else:
            result = _run_cloud(args, scene, blend, settings, render_dir)

        manifest = _write_manifest(out_dir, result, args)
        print(
            f"blender.render: execution={args.execution} engine={result.get('engine')} "
            f"frames={result.get('frames')} -> {result.get('out_file')} "
            f"({result.get('render_ms')}ms) manifest={manifest}"
        )
        return 0

    return run_pack_main("blender.render", _run, argv=argv)


if __name__ == "__main__":
    raise SystemExit(main())
