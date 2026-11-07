"""Offline wheel composition through real discovery, CLI, transport and backend.

Only the external Node renderer is a deterministic harness. FFmpeg/ffprobe,
registry generation/admission, Root preparation and publication remain real.
"""
from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import venv
import zipfile

import pytest

ROOT = Path(__file__).resolve().parents[3]
PROBE = Path(__file__).with_name("installed_render_probe.py")


@pytest.fixture(scope="module")
def installed_render(tmp_path_factory):
    yield from build_installed_render(tmp_path_factory.mktemp("installed-render-composition"))


def build_installed_render(root, *, packs=None, clip_type="am-snap-plate"):
    """Build the release wheel (optionally with only ``packs`` under astrid/packs) into a disposable interpreter.

    ``packs=None`` ships every pack of the checkout (the original fixture).
    """
    build = root / "build-source"
    build.mkdir()
    for name in ("pyproject.toml", "LICENSE", "NOTICE", "README.md"):
        shutil.copy2(ROOT / name, build / name)
    for name in ("astrid", "astrid_runtime_client"):
        ignore = shutil.ignore_patterns("__pycache__")
        if name == "astrid" and packs is not None:
            ignore = shutil.ignore_patterns("__pycache__", "packs")
        shutil.copytree(ROOT / name, build / name, ignore=ignore)
    if packs is not None:
        (build / "astrid" / "packs").mkdir()
        for entry in (ROOT / "astrid" / "packs").iterdir():
            if entry.is_file():
                shutil.copy2(entry, build / "astrid" / "packs" / entry.name)
        for owner in packs:
            shutil.copytree(ROOT / "astrid" / "packs" / owner, build / "astrid" / "packs" / owner,
                            ignore=shutil.ignore_patterns("__pycache__"))
    environment = {name: os.environ[name] for name in ("PATH", "TMPDIR", "LANG") if name in os.environ}
    environment.update(HOME=str(root / "home"), PYTHONDONTWRITEBYTECODE="1")
    Path(environment["HOME"]).mkdir()
    built = subprocess.run(
        [sys.executable, "-m", "build", "--wheel", "--no-isolation", "--outdir", str(root / "wheel")],
        cwd=build, env=environment, capture_output=True, text=True, timeout=120,
    )
    (root / "build.log").write_text(built.stdout + built.stderr)
    assert built.returncode == 0, built.stdout[-2000:] + built.stderr[-2000:]
    wheels = list((root / "wheel").glob("*.whl"))
    assert len(wheels) == 1
    (root / "wheel.log").write_text(hashlib.sha256(wheels[0].read_bytes()).hexdigest())
    shutil.rmtree(build)
    # No installer or dependency acquisition: extract the real release wheel
    # into a disposable interpreter's site-packages.
    interpreter = root / "interpreter"
    venv.EnvBuilder(with_pip=False).create(interpreter)
    python = interpreter / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    site = Path(subprocess.check_output(
        [str(python), "-I", "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"], text=True,
    ).strip())
    with zipfile.ZipFile(wheels[0]) as archive:
        for name in (
            "astrid/packs/rendering/actions/timeline_visualize/composed_frame.py",
            "astrid/packs/rendering/rendering/renderers/remotion/run.py",
        ):
            assert archive.read(name) == (ROOT / name).read_bytes()
        archive.extractall(site)
    shutil.rmtree(root / "wheel")
    dependencies = Path(__import__("pytest").__file__).resolve().parent.parent
    (site / "qualified-dependencies.pth").write_text(str(dependencies) + "\n")
    runtime = Path(os.environ["ASTRID_RUNTIME_CHECKOUT"])
    for name in ("runtime_protocol", "astrid_runtime"):
        shutil.copytree(runtime / name, site / name, ignore=shutil.ignore_patterns("__pycache__"))
    # The deployment's Runtime pin accompanies the installed reader; wheels
    # intentionally exclude repository-root provisioning receipts.
    shutil.copy2(ROOT / "runtime.lock.json", site / "runtime.lock.json")
    project = root / "remotion"
    project.mkdir()
    manifest = json.loads((ROOT / "remotion/package.json").read_text())
    artifact = ROOT / "remotion" / manifest["dependencies"]["@banodoco/timeline-composition"].removeprefix("file:")
    with tarfile.open(artifact) as archive:
        archive.extractall(project, filter="data")
    package = project / "node_modules/@banodoco/timeline-composition"
    package.parent.mkdir(parents=True)
    (project / "package").rename(package)
    shutil.copy2(ROOT / "remotion/package.json", project / "package.json")
    shutil.copy2(ROOT / "remotion/package-lock.json", project / "package-lock.json")
    cli = project / "node_modules/@remotion/cli/remotion-cli.js"
    cli.parent.mkdir(parents=True)
    cli.write_text("// external renderer edge supplied by the test harness\n")
    (project / "src").mkdir()
    ffmpeg = shutil.which("ffmpeg")
    assert ffmpeg and shutil.which("ffprobe"), "the qualified media tools are required"
    node = root / "native-render-edge"
    node.write_text(f"#!{python}\n" + '''import json, pathlib, subprocess, sys
args = sys.argv[1:]
root = pathlib.Path(__file__).parent
if args == ['--version']:
    print('v20.19.4')
elif '--input-type=module' in args:
    print((root / 'public-membership.json').read_text())
elif len(args) >= 2 and args[1] == 'render':
    props = json.loads(pathlib.Path(args[args.index('--props') + 1]).read_text())
    assert props['timeline']['clips'][0]['clipType'] == ''' + repr(clip_type) + '''
    (root / 'rendered-props.json').write_text(json.dumps(props))
    output = args[args.index('--output') + 1]
    subprocess.run([''' + repr(ffmpeg) + ''', '-loglevel', 'error', '-y',
        '-f', 'lavfi', '-i', 'color=c=red:s=1280x720:r=24',
        '-f', 'lavfi', '-i', 'anullsrc=r=48000:cl=stereo', '-t', '1',
        '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-c:a', 'aac',
        '-video_track_timescale', '90000', output], check=True)
else:
    raise AssertionError(args)
''')
    node.chmod(0o755)
    environment.update(
        ASTRID_HOST_EXECUTION_PROFILE="installed", ASTRID_NODE_EXECUTABLE=str(node),
        ASTRID_REMOTION_PROJECT_DIR=str(project), ASTRID_RUNTIME_CHECKOUT=str(runtime),
        ASTRID_TIMELINE_SCHEMA_PYTHONPATH=str(site / "astrid/core/timeline/schema/timeline-config.schema.json"),
    )
    assert not (site / "scripts").exists() and not (site / "tests").exists()
    yield root, python, site, environment
    shutil.rmtree(interpreter)


@pytest.mark.parametrize("mode", ["positive", "missing-element", "missing-renderer"])
def test_real_installed_loader_catalog_cli_and_render(installed_render, mode):
    root, python, site, environment = installed_render
    result = subprocess.run(
        [str(python), "-I", str(PROBE), str(root), str(site), mode],
        cwd=root, env=environment, text=True, capture_output=True, timeout=120,
    )
    (root / f"{mode}.log").write_text(result.stdout + result.stderr)
    assert result.returncode == 0, result.stdout[-4000:] + result.stderr[-4000:]
    proof = json.loads((root / f"{mode}.proof.json").read_text())
    assert proof["isolated"] is True
    assert proof["origin"].startswith(str(site) + os.sep)
    assert proof["selected_backend"] == "rendering.remotion"
    assert "am-snap-plate" in proof["catalog_ids"]
    assert proof["published"] is (mode == "positive")
