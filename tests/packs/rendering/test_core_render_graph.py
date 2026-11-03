from pathlib import Path

import yaml


PACK = Path(__file__).resolve().parents[3] / "astrid" / "packs" / "rendering"


def test_render_action_has_no_optional_pipeline_prerequisites_or_builder() -> None:
    pack = yaml.safe_load((PACK / "pack.yaml").read_text(encoding="utf-8"))
    render = pack["actions"]["render"]

    assert not render["graph"].get("depends_on")
    assert render["graph"]["provides"] == ["rendered_video"]
    assert "pipeline_requirements" not in render
    assert not {
        "command_builder",
        "pipeline_step",
        "pipeline_step_order",
    } & render["metadata"].keys()
    assert render["invocation"]["kind"] == "command"


def test_core_only_real_timeline_renders_through_action(tmp_path: Path) -> None:
    """The rendering pack can render a real timeline with no optional packs installed."""
    import json
    import os
    import shutil
    import subprocess
    import sys

    import runtime_protocol

    root = PACK.parents[2]
    install = tmp_path / "install"
    install.mkdir()
    shutil.copy2(root / "pyproject.toml", install / "pyproject.toml")
    # Stage real package files, omitting every other shipped capability pack.
    shutil.copytree(root / "astrid", install / "astrid", ignore=shutil.ignore_patterns("packs", "__pycache__"))
    (install / "astrid" / "packs").mkdir()
    shutil.copy2(root / "astrid" / "packs" / "__init__.py", install / "astrid" / "packs")
    shutil.copytree(PACK, install / "astrid" / "packs" / "rendering", ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copytree(root / "astrid_runtime_client", install / "astrid_runtime_client", ignore=shutil.ignore_patterns("__pycache__"))
    # The render transport sanitizes PYTHONPATH for its child, so stage the
    # pinned runtime protocol alongside the package as an installed dependency.
    shutil.copytree(Path(runtime_protocol.__file__).resolve().parent, install / "runtime_protocol", ignore=shutil.ignore_patterns("__pycache__"))
    assert sorted(p.name for p in (install / "astrid" / "packs").iterdir()) == ["__init__.py", "rendering"]

    source = root / "tests" / "fixtures" / "reshape" / "hype_regression" / "broll.mp4"
    timeline = tmp_path / "timeline.json"
    assets = tmp_path / "assets.json"
    output = tmp_path / "out" / "result.mp4"
    output.parent.mkdir()
    timeline.write_text(json.dumps({
        "theme": "banodoco-default",
        "theme_overrides": {"visual": {"canvas": {"width": 320, "height": 180, "fps": 30}}},
        "tracks": [{"id": "v", "kind": "visual", "label": "Video"}],
        "clips": [{"id": "clip", "at": 0, "track": "v", "clipType": "media",
                   "asset": "source", "from": 0, "to": 1, "speed": 1, "volume": 0}],
    }), encoding="utf-8")
    assets.write_text(json.dumps({"assets": {"source": {"file": str(source), "type": "video/mp4"}}}), encoding="utf-8")
    runtime_root = Path(runtime_protocol.__file__).resolve().parent.parent
    env = os.environ.copy()
    env.update({"PYTHONPATH": os.pathsep.join((str(install), str(runtime_root))),
                "PATH": str(Path(sys.executable).parent) + os.pathsep + env.get("PATH", ""),
                "HOME": str(tmp_path), "ASTRID_PACKS_PATH": "",
                # The rendering pack verifies bundled element identities through
                # the real installed Composition package, even for FFmpeg.
                "ASTRID_REMOTION_PROJECT_DIR": str(root / "remotion"),
                "ASTRID_INTERNAL_INVOCATION": "1", "PYTHONDONTWRITEBYTECODE": "1"})
    result = subprocess.run(
        [sys.executable, "-m", "astrid.packs.rendering.actions.render.run", "--timeline", str(timeline),
         "--assets", str(assets), "--out", str(output), "--selector", "rendering.ffmpeg"],
        cwd=tmp_path, env=env, text=True, capture_output=True, timeout=120,
    )
    replay = list(output.parent.glob(".*.replay/*/bundle.json"))
    diagnostics = replay[0].read_text(encoding="utf-8") if replay else ""
    assert result.returncode == 0, f"render action failed:\n{result.stdout}\n{result.stderr}\n{diagnostics}"
    assert output.read_bytes()[4:8] == b"ftyp"
    probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                            "-of", "json", str(output)], text=True, capture_output=True, check=True)
    assert float(json.loads(probe.stdout)["format"]["duration"]) > 0
    manifest = json.loads((output.parent / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["kind"] == "rendering.render"
    assert manifest["outputs"][0]["path"] == output.name
    assert Path(f"{output}.provenance.json").is_file()
