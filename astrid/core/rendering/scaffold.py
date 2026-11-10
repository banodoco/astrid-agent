"""Four-file renderer scaffold for pluggable timeline renderers.

``create_renderer_scaffold`` writes a complete, self-contained renderer pack
into a destination directory: ``pack.yaml``, ``rendering/renderer.yaml``,
``render.py``, and ``test_renderer.py`` — never more.  The generated
``render.py`` is a thin pure-stdlib raw-command backend (the Batch-2 raw
fixture pattern) that validates the v1 request statically and writes a
deterministic ``RenderResult``-shaped result JSON without ffmpeg, remotion,
or a GPU.

``pack.yaml`` is a schema v3 manifest (discovery quarantines v1/v2) that
declares this Astrid's pack API version and the renderer under
``rendering``; v3 requires the descriptor beneath ``rendering/``.  The
renderer command runs with the pack root as its working directory, so
``render.py`` stays at the root.

Files are created with the caller's uid/gid via plain ``Path.write_text``
(no sudo, no chown).  Collisions refuse to overwrite unless ``force=True``.
The manifest command is exactly ``[python3, render.py]`` — no absolute host
paths, no shell.
"""

from __future__ import annotations

import re
from pathlib import Path

from astrid.core.pack._common import _PACK_ID_RE
from astrid.pack_api import VERSION as PACK_API_VERSION

SCAFFOLD_FILES: tuple[str, ...] = (
    "pack.yaml",
    "rendering/renderer.yaml",
    "render.py",
    "test_renderer.py",
)

# The same qualified-id pattern the rendering wire contract enforces.
_QUALIFIED_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*(?:\.[a-z0-9][a-z0-9_-]*)+$")
# A v3 rendering declaration key (the renderer id's local part).
_V3_LOCAL_ID_RE = re.compile(r"^[a-z][a-z0-9_-]*$")
_DEFAULT_PACK_ID = "rendering"

_PACK_YAML = """\
schema_version: 3
id: __PACK_ID__
name: __DISPLAY_NAME__ Renderer Pack
version: 1.0.0
pack_api: "__PACK_API__"
description: >-
  Scaffolded pack for the __RENDERER_ID__ renderer.  render.py is a
  pure-stdlib raw-command backend: it parses argv, reads --request JSON,
  and writes --result JSON without importing the Astrid SDK.
permissions:
  - id: subprocess
    reason: Run the pack-owned render.py command as a subprocess.
  - id: project_files
    reason: Read the invocation request JSON and write the generated output into the request workspace.
aliases: []
rendering:
  __RENDERER_KEY__:
    type: renderer
    path: rendering/renderer.yaml
"""

_RENDERER_YAML = """\
schema_version: 1
id: __RENDERER_ID__
name: __DISPLAY_NAME__ Renderer
version: 1.0.0
protocol_version: 1
command: [python3, render.py]
operations: [support, render]
description: >-
  Scaffolded raw-command renderer implementing the frozen v1 wire protocol.
  render.py validates requests statically and writes deterministic
  RenderResult / SupportReport JSON without ffmpeg, remotion, or a GPU.
capabilities:
  clip_types: [media]
  track_types: [visual]
  features:
    media: true
    deterministic: true
  supports_full_timeline: true
  supports_windows: true
  output_profiles: [video/mp4]
  audio_ownership: [rendered]
required_permissions: [project_files, subprocess]
"""

_RENDER_PY = """\
#!/usr/bin/env python3
\"\"\"Raw v1 command renderer scaffold (pure stdlib, no Astrid SDK).
Usage: python3 render.py render|support --request <abs.json> --result <abs.json>\"\"\"

from __future__ import annotations

import argparse, hashlib, json, re, sys
from pathlib import Path

BACKEND_ID = "__RENDERER_ID__"
_BACKEND_VERSION = "1.0.0"
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_PROFILE = {"width": 64, "height": 64, "fps_rational": [24, 1], "time_base": [1, 12288], "container": "mp4", "video_codec": "h264", "video_profile": None, "video_level": None, "pixel_format": "yuv420p", "audio_codec": "pcm_s16le", "audio_sample_rate": 48000, "audio_channel_layout": "stereo", "duration_tolerance": 1}


def _write(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, indent=2) + "\\n", encoding="utf-8")


def _error(result_path: Path, message: str) -> None:
    _write(result_path, {"schema_version": 1, "kind": "protocol", "backend": BACKEND_ID, "message": message, "recovery_command": None, "details": {}})


def _run(verb: str, request: dict, request_path: Path, result_path: Path) -> int:
    if request.get("schema_version") != 1:
        _error(result_path, "unsupported request schema_version; expected 1")
        return 0
    output_name = request.get("output_name")
    if not isinstance(output_name, str) or not _NAME_RE.fullmatch(output_name):
        _error(result_path, "output_name must match [A-Za-z0-9][A-Za-z0-9._-]*")
        return 0
    if verb == "support":
        _write(result_path, {"schema_version": 1, "supported": True, "reasons": [], "features": {"media": True, "audio_mode": "rendered"}, "alternatives": [], "backend": BACKEND_ID, "backend_version": _BACKEND_VERSION})
        return 0
    workspace = request_path.resolve().parent
    out_dir = workspace / "outputs"
    out_dir.mkdir(parents=True, exist_ok=True)
    media = b"astrid-scaffold-" + output_name.encode("ascii", "ignore")
    (out_dir / output_name).write_bytes(media)
    _write(result_path, {"schema_version": 1, "video": {"path": "outputs/" + output_name, "profile": dict(_PROFILE), "sha256": hashlib.sha256(media).hexdigest(), "duration_frames": 2, "audio": "rendered", "attachments": {}}, "backend_fragments": {BACKEND_ID: {"renderer": "scaffold"}}, "audio_ownership": "rendered", "normalization": [], "logs": [], "metadata": {}})
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="render.py")
    parser.add_argument("verb", choices=("render", "support"))
    parser.add_argument("--request", required=True)
    parser.add_argument("--result", required=True)
    args = parser.parse_args(argv)
    request_path = Path(args.request)
    result_path = Path(args.result)
    try:
        request = json.loads(request_path.read_text(encoding="utf-8"))
        if not isinstance(request, dict):
            raise TypeError("request must be a JSON object")
    except Exception as exc:
        _error(result_path, f"cannot read request JSON: {exc}")
        return 0
    return _run(args.verb, request, request_path, result_path)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
"""

_TEST_RENDERER_PY = """\
\"\"\"Deterministic smoke test for the __RENDERER_ID__ renderer scaffold.\"\"\"

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

PACK_ROOT = Path(__file__).resolve().parent


def test_render_writes_valid_result(tmp_path: Path) -> None:
    request = {
        "schema_version": 1,
        "timeline_path": "timeline.json",
        "output_name": "out.mp4",
        "audio": "rendered",
    }
    request_path = tmp_path / "request.json"
    request_path.write_text(json.dumps(request), encoding="utf-8")
    result_path = tmp_path / "result.json"
    completed = subprocess.run(
        [
            sys.executable,
            str(PACK_ROOT / "render.py"),
            "render",
            "--request",
            str(request_path),
            "--result",
            str(result_path),
        ],
        cwd=PACK_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert completed.returncode == 0, completed.stderr
    result = json.loads(result_path.read_text(encoding="utf-8"))
    assert result["schema_version"] == 1
    assert result["audio_ownership"] == "rendered"
    output = tmp_path / "outputs" / "out.mp4"
    assert output.is_file()
    assert result["video"]["sha256"] == hashlib.sha256(output.read_bytes()).hexdigest()
"""


def _display_name(name: str) -> str:
    return name.replace("-", " ").replace("_", " ").strip().title()


def _pack_id_from_dest(dest: Path) -> str:
    """Derive the scaffold pack id from the destination folder name.

    The pack manifest requires ``root.name == pack_id`` with a
    CASE-SENSITIVE comparison (see ``astrid/core/pack/loader.py``), so a
    scaffold is only valid when the folder it is written into is named exactly
    like the pack id.  The name is used VERBATIM (no case-folding): a
    destination whose name is not already a valid lowercase pack id is
    rejected.  The default ``rendering`` pack id is also rejected: the
    first-party ``rendering`` pack owns it and a trusted install would
    collide.
    """
    pack_id = dest.name
    if not _PACK_ID_RE.fullmatch(pack_id):
        raise ValueError(
            f"destination directory name {pack_id!r} is not a valid pack id; "
            "pack ids must match [a-z][a-z0-9_]* and the scaffold folder "
            "must be named exactly like the desired pack id (e.g. "
            "'python3 -m astrid.core.rendering.cli create wave acme_wave' writes "
            "acme_wave/pack.yaml with id: acme_wave)"
        )
    if pack_id == _DEFAULT_PACK_ID:
        raise ValueError(
            f"pack id {pack_id!r} collides with the first-party rendering pack; "
            "scaffold into a differently named directory"
        )
    return pack_id


def _qualified_renderer_id(
    name: str,
    renderer_id: str | None,
    pack_id: str,
) -> str:
    if renderer_id is not None:
        candidate = renderer_id.strip()
    else:
        candidate = f"{pack_id}.{name.strip().lower()}"
    if not _QUALIFIED_ID_RE.fullmatch(candidate):
        raise ValueError(
            f"invalid renderer id {candidate!r}; expected the qualified form "
            "'<pack>.<name>' with segments matching [a-z0-9][a-z0-9_-]* "
            f"(e.g. '{pack_id}.wave')"
        )
    return candidate


def create_renderer_scaffold(
    name: str,
    dest: str | Path,
    *,
    force: bool = False,
    renderer_id: str | None = None,
) -> Path:
    """Write the four-file renderer scaffold into *dest*.

    Args:
        name: Renderer name; the qualified id becomes ``rendering.<name>``
            unless *renderer_id* overrides it.
        dest: Destination directory for the scaffold.
        force: Overwrite any of the four scaffold files that already exist.
        renderer_id: Optional qualified id override (``<pack>.<name>``).

    Returns:
        The resolved destination directory.

    Raises:
        ValueError: If *name* / *renderer_id* cannot form a qualified id.
        FileExistsError: If a scaffold file already exists and *force* is
            False.
    """
    if not isinstance(name, str) or not name.strip():
        raise ValueError("renderer name must be a non-empty string")

    target = Path(dest).expanduser().resolve()
    pack_id = _pack_id_from_dest(target)
    if renderer_id is not None:
        explicit_pack_id = renderer_id.strip().partition(".")[0]
        if explicit_pack_id != pack_id:
            raise ValueError(
                f"--id {renderer_id!r} declares pack {explicit_pack_id!r} but the "
                f"destination directory is named {pack_id!r}; installability "
                "requires root.name == pack id — use a matching directory"
            )
    renderer_id = _qualified_renderer_id(name, renderer_id, pack_id)
    renderer_key = renderer_id.partition(".")[2]
    if not _V3_LOCAL_ID_RE.fullmatch(renderer_key):
        raise ValueError(
            f"invalid renderer id {renderer_id!r}; a schema v3 pack declares the "
            "renderer as '<pack>.<name>' with one name segment matching "
            f"[a-z][a-z0-9_-]* (e.g. '{pack_id}.wave')"
        )
    display = _display_name(name)

    if target.exists() and not target.is_dir():
        if not force:
            raise FileExistsError(f"refusing to overwrite existing file {target}")
        target.unlink()
    target.mkdir(parents=True, exist_ok=True)

    collisions = [
        target / filename
        for filename in SCAFFOLD_FILES
        if (target / filename).exists()
    ]
    if collisions:
        if not force:
            names = ", ".join(path.name for path in collisions)
            raise FileExistsError(
                f"refusing to overwrite existing scaffold file(s): {names}"
            )
        for path in collisions:
            path.unlink()

    substitutions = {
        "__PACK_ID__": pack_id,
        "__RENDERER_ID__": renderer_id,
        "__RENDERER_KEY__": renderer_key,
        "__PACK_API__": "%d.%d" % PACK_API_VERSION,
        "__DISPLAY_NAME__": display,
    }
    templates = {
        "pack.yaml": _PACK_YAML,
        "rendering/renderer.yaml": _RENDERER_YAML,
        "render.py": _RENDER_PY,
        "test_renderer.py": _TEST_RENDERER_PY,
    }
    for filename, template in templates.items():
        content = template
        for token, value in substitutions.items():
            content = content.replace(token, value)
        path = target / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        # Plain write_text creates the file with the caller's uid/gid.
        path.write_text(content, encoding="utf-8")
    return target
