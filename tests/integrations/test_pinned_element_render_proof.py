"""End-to-end pinned project-element proof on a throwaway stand-in.

This is deliberately an Agent-owned integration proof rather than a generic
host smoke test.  The timeline is resolved through the managed snapshot path,
materialized, and rendered by the provisioned Remotion browser.  A tiny Vite
preview uses the same admitted component sources so the comparison has a real
local-editor clock and not a mocked renderer call.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import os
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image, ImageChops, ImageStat

pytest_plugins = ("tests.core.test_project_packs",)
pytestmark = pytest.mark.integration

from astrid.core.element import project_packs  # noqa: E402
from astrid.core.rendering.project_elements import CARRIER, selections  # noqa: E402
from astrid.packs.rendering.actions.render import managed_timeline  # noqa: E402
from astrid.packs.rendering.actions.render.managed_timeline import (  # noqa: E402
    resolve_managed_render_snapshot,
    validate_managed_render_snapshot,
)
from astrid.packs.rendering.rendering.renderers.remotion import run  # noqa: E402
from tests.integrations.test_generic_host_remotion_render import (  # noqa: E402
    _prepare_remotion_fixture,
)
from tests.packs.rendering.test_managed_timeline_render import _Runtime  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
FPS = 10
SAMPLES = {"very": 5, "next": 15, "day": 25}
COLOURS = {"very": "#d94f70", "next": "#377dff"}
PIXEL_TOLERANCE = {"max_channel_delta": 128, "mean_absolute_delta": 0.25}


def _result(data: object = None, *, ok: bool = True) -> SimpleNamespace:
    return SimpleNamespace(ok=ok, data=data, error=None if ok else {"message": "not found"})


def _component_source(colour: str) -> str:
    return (
        "import {AbsoluteFill} from 'remotion';\n"
        "export default function Reveal(){"
        f"return <AbsoluteFill style={{{{backgroundColor: '{colour}'}}}}/>;"
        "}\n"
    )


def _make_admitted_standin(configured) -> tuple[_Runtime, list[dict[str, str]], dict[str, str]]:
    """Create two retained revisions and an exact shot/occurrence closure."""

    runtime = _Runtime()
    runtime.documents = configured
    runtime.documents._client = runtime
    project = runtime.project["id"]
    source, element, manifest = project_packs_test_seed(project)
    sources: dict[str, str] = {}
    refs: list[dict[str, str]] = []
    admission_result = None
    for label, colour in (("very", COLOURS["very"]), ("next", COLOURS["next"])):
        component = _component_source(colour)
        (element / "component.tsx").write_text(component, encoding="utf-8")
        admission_result = project_packs.admit_project_pack("realm", project)
        record = admission_result["elements"][0]
        descriptor = record["descriptor"]
        refs.append({key: descriptor[key] for key in ("packId", "kind", "id", "revision")})
        sources[label] = component

    assert admission_result is not None
    row = copy.deepcopy(configured.rows[(project, admission_result["documentId"])])

    def verified_document(project_ref: str, pin: dict[str, object]) -> dict[str, object]:
        return {"pin": pin, "document": {**copy.deepcopy(row), "project_id": project_ref}}

    def verified_object(project_ref: str, _pin: dict[str, object], object_id: str) -> dict[str, object]:
        admission = next(
            admission
            for admission in row["content"]["admissions"]
            if admission["snapshot_object"]["object_id"] == object_id
        )
        data = configured.media.objects[(project_ref, object_id)]
        return {
            **admission["snapshot_object"],
            "bytes_base64": base64.b64encode(data).decode("ascii"),
        }

    runtime.get_project_verified_document = verified_document
    runtime.get_project_verified_document_object = verified_object

    occurrence_id = "occ-standin"
    shot_id = "shot-standin"
    shot_revision_id = "shot-revision-standin"
    internal_revision_id = "internal-revision-standin"
    custom_clips = [
        {
            "id": "c02-02-am-discord",
            "at": 0,
            "hold": 1,
            "track": "picture",
            "clipType": "am-discord",
            "elementRef": refs[0],
            "params": {},
        },
        {
            "id": "c02-02-am-discord-next",
            "at": 1,
            "hold": 1,
            "track": "picture",
            "clipType": "am-discord",
            "elementRef": refs[1],
            "params": {},
        },
        {
            "id": "built-in-am-discord",
            "at": 2,
            "hold": 1,
            "track": "picture",
            "clipType": "am-discord",
            "params": {
                "server": "STAND-IN",
                "channel": "# announcements",
                "sidebar": False,
                "badge": "",
                "messages": [{
                    "author": "Astrid",
                    "tag": "BOT",
                    "avatarColor": "#5865F2",
                    "time": "10:00",
                    "lines": ["The very next day"],
                    "appearAt": 0,
                }],
            },
        },
    ]
    runtime.standin_before_placement = {
        "id": "c02-02-am-discord",
        "track": "picture",
        "at": 0,
        "hold": 1,
        "clipType": "am-discord",
    }
    tracks = [{"id": "picture", "kind": "visual", "label": "Picture"}]
    parent_revision = "parent-revision-standin"
    parent = {
        "project_id": project,
        "timeline_id": runtime.timeline["timeline_id"],
        "revision_id": parent_revision,
        "content_digest": "sha256:" + "a" * 64,
        "payload": {
            "config": {
                "theme": "banodoco-default",
                "theme_overrides": {
                    "visual": {
                        "canvas": {"width": 1920, "height": 1080, "fps": FPS},
                        "color": {"fg": "#ffffff", "bg": "#000000", "accent": "#ffffff"},
                    }
                },
                "tracks": tracks,
                # Canonical parent compositions carry occurrences separately;
                # legacy ``clipType: shot`` placeholders are intentionally not
                # renderable and must never enter the prepared config.
                "clips": [],
            },
            "registry": {"assets": {}},
            "clips": [],
            "occurrences": [{
                "occurrence_id": occurrence_id,
                "shot_id": shot_id,
                "shot_revision_id": shot_revision_id,
                "placement": {"start_ms": 0, "track": "picture"},
                "duration_ms": 3000,
                "source_offset": 0,
                "speed": 1,
                "gain": 1,
                "mute": False,
                "track": "picture",
                "transform": {},
                "provenance": {},
            }],
        },
    }
    runtime.timeline["head_revision_id"] = parent_revision
    runtime.timeline["version"] = 1
    runtime.parent_revisions[parent_revision] = parent
    runtime.exact_shot_revisions[(shot_id, shot_revision_id)] = {
        "project_id": project,
        "shot_id": shot_id,
        "revision_id": shot_revision_id,
        "content_digest": "sha256:" + "b" * 64,
        "internal_timeline_revision_id": internal_revision_id,
        "payload": {"metadata": {"name": "Stand-in smooth zoom"}, "text_bindings": []},
    }
    runtime.internal_revisions[internal_revision_id] = {
        "project_id": project,
        "timeline_id": runtime.timeline["timeline_id"],
        "revision_id": internal_revision_id,
        "content_digest": "sha256:" + "c" * 64,
        "payload": {"tracks": tracks, "clips": custom_clips, "registry": {"assets": {}}},
    }
    return runtime, refs, sources


def project_packs_test_seed(project: str):
    from tests.core.test_project_packs import seed

    return seed(project)


@pytest.fixture
def remotion_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    from tests.helpers.remotion_runtime import remotion_install_problems

    problems = remotion_install_problems()
    if problems:
        pytest.fail("real pinned-element render prerequisites unavailable: " + "; ".join(problems), pytrace=False)
    node = os.environ.get("ASTRID_NODE_EXECUTABLE") or shutil.which("node")
    if not node:
        pytest.fail("real pinned-element render requires the provisioned Node executable", pytrace=False)
    monkeypatch.setenv("ASTRID_NODE_EXECUTABLE", str(Path(node).expanduser().resolve(strict=True)))
    _workspace, remotion, timeline_path, assets_path = _prepare_remotion_fixture(tmp_path, monkeypatch)
    timeline_path.write_text(json.dumps({"tracks": [], "clips": []}), encoding="utf-8")
    assets_path.write_text(json.dumps({"assets": {}}), encoding="utf-8")
    return remotion


@pytest.fixture
def preview_runtime(remotion_project: Path) -> tuple[Path, Path]:
    """Use the provisioned App checkout dependencies and Node executable."""
    app_checkout = os.environ.get("ASTRID_APP_CHECKOUT")
    if not app_checkout:
        pytest.fail("pinned-element preview requires a provisioned ASTRID_APP_CHECKOUT", pytrace=False)
    modules_path = (Path(app_checkout).expanduser() / "node_modules").resolve(strict=True)
    required = (".bin/vite", "@remotion/player", "playwright", "react", "react-dom", "remotion")
    missing = [name for name in required if not (modules_path / name).exists()]
    if missing:
        pytest.fail("pinned-element preview dependencies are missing: " + ", ".join(missing), pytrace=False)
    node = os.environ.get("ASTRID_NODE_EXECUTABLE") or shutil.which("node")
    if not node:
        pytest.fail("pinned-element preview requires the provisioned Node executable", pytrace=False)
    return modules_path, Path(node).expanduser().resolve(strict=True)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _write_preview_app(root: Path, sources: dict[str, str], node_modules: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "node_modules").symlink_to(node_modules, target_is_directory=True)
    source_a = root / "revision-a.tsx"
    source_b = root / "revision-b.tsx"
    source_a.write_text(sources["very"], encoding="utf-8")
    source_b.write_text(sources["next"], encoding="utf-8")
    built_in = (ROOT / "astrid/packs/astrid_motion/rendering/elements/effects/am-discord/component.tsx").as_posix()
    (root / "index.html").write_text(
        '<link rel="stylesheet" href="/style.css"><div id="root"></div>'
        '<script type="module" src="/preview.tsx"></script>',
        encoding="utf-8",
    )
    (root / "vite.config.mjs").write_text(
        "export default {esbuild:{jsxInject:\"import React from 'react';\"},server:{fs:{allow:"
        + json.dumps([str(ROOT), str(root)])
        + "}}};\n",
        encoding="utf-8",
    )
    (root / "preview.tsx").write_text(
        "import {useEffect} from 'react';\n"
        "import {createRoot} from 'react-dom/client';\n"
        "import {Player} from '@remotion/player';\n"
        "import {AbsoluteFill, Sequence} from 'remotion';\n"
        "import RevealVery from './revision-a.tsx';\n"
        "import RevealNext from './revision-b.tsx';\n"
        f"import AmDiscord from {json.dumps(built_in)};\n"
        "const params={server:'STAND-IN',channel:'# announcements',sidebar:false,badge:'',messages:[{author:'Astrid',tag:'BOT',avatarColor:'#5865F2',time:'10:00',lines:['The very next day'],appearAt:0}]};\n"
        "const Scene=()=>{useEffect(()=>{document.body.dataset.ready='1';},[]);return <AbsoluteFill>"
        "<Sequence from={0} durationInFrames={10}><RevealVery params={{}}/></Sequence>"
        "<Sequence from={10} durationInFrames={10}><RevealNext params={{}}/></Sequence>"
        "<Sequence from={20} durationInFrames={10}><AmDiscord params={params}/></Sequence>"
        "</AbsoluteFill>;};\n"
        "const frame=Number(new URLSearchParams(location.search).get('frame')||0);\n"
        "createRoot(document.getElementById('root')).render(<Player component={Scene} durationInFrames={30} fps={10} compositionWidth={1920} compositionHeight={1080} initialFrame={frame} controls={false} acknowledgeRemotionLicense style={{width:1920,height:1080}}/>);\n",
        encoding="utf-8",
    )
    (root / "style.css").write_text(
        "html,body,#root{margin:0;width:100%;height:100%;overflow:hidden;background:#000}\n",
        encoding="utf-8",
    )


def _capture_vite_preview(
    root: Path, output: Path, frames: list[int], *, node_modules: Path, node: Path
) -> dict[int, Path]:
    vite = node_modules / ".bin/vite"
    if not vite.is_file():
        pytest.fail(f"local Vite executable is unavailable: {vite}", pytrace=False)
    port = _free_port()
    server = subprocess.Popen(
        [str(vite), "--host", "127.0.0.1", "--port", str(port)],
        cwd=root,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=1).close()
                break
            except (OSError, urllib.error.URLError):
                if server.poll() is not None:
                    output_text = server.stdout.read() if server.stdout else ""
                    raise AssertionError(f"Vite exited before serving preview: {output_text}")
                time.sleep(0.1)
        else:
            raise AssertionError("Vite preview did not become ready within 30 seconds")
        capture = root / "capture.mjs"
        capture.write_text(
            "import {chromium} from 'playwright';\n"
            "const [base, frames, out] = process.argv.slice(2);\n"
            "const browser=await chromium.launch({headless:true,executablePath:process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE});\n"
            "const page=await browser.newPage({viewport:{width:1920,height:1080},deviceScaleFactor:1});\n"
            "page.on('pageerror', error=>console.error(`pageerror: ${error.stack||error}`));\n"
            "page.on('console', message=>{if(message.type()==='error')console.error(`console: ${message.text()}`);});\n"
            "for(const frame of JSON.parse(frames)){await page.goto(`${base}/?frame=${frame}`,{waitUntil:'networkidle'});await page.waitForSelector('body[data-ready=\\\"1\\\"]');await page.screenshot({path:`${out}/frame-${frame}.png`});}\n"
            "await browser.close();\n",
            encoding="utf-8",
        )
        output.mkdir(parents=True, exist_ok=True)
        result = subprocess.run(
            [str(node), str(capture), f"http://127.0.0.1:{port}", json.dumps(frames), str(output)],
            cwd=root,
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise AssertionError(f"Vite/Playwright capture failed:\n{result.stdout}\n{result.stderr}")
        return {frame: output / f"frame-{frame}.png" for frame in frames}
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()


def _metrics(left: Path, right: Path) -> dict[str, float | int | bool]:
    with Image.open(left) as left_image, Image.open(right) as right_image:
        left_rgb = left_image.convert("RGB")
        right_rgb = right_image.convert("RGB")
        assert left_rgb.size == right_rgb.size, (left_rgb.size, right_rgb.size)
        diff = ImageChops.difference(left_rgb, right_rgb)
        extrema = diff.getextrema()
        max_delta = max(high for _low, high in extrema)
        mean_delta = sum(ImageStat.Stat(diff).mean) / 3
        differing = sum(1 for pixel in diff.getdata() if pixel != (0, 0, 0))
        return {
            "width": left_rgb.width,
            "height": left_rgb.height,
            "max_channel_delta": max_delta,
            "mean_absolute_delta": mean_delta,
            "differing_pixels": differing,
            "pass": max_delta <= PIXEL_TOLERANCE["max_channel_delta"]
            and mean_delta <= PIXEL_TOLERANCE["mean_absolute_delta"],
        }


def test_pinned_element_render_proof_on_standin(
    configured,
    remotion_project: Path,
    preview_runtime: tuple[Path, Path],
    tmp_path: Path,
) -> None:
    node_modules, node = preview_runtime
    runtime, refs, sources = _make_admitted_standin(configured)
    snapshot = resolve_managed_render_snapshot(
        project_ref="demo",
        timeline_ref="main",
        client=runtime,
    )
    validate_managed_render_snapshot(snapshot)
    assert len(selections(snapshot.config)) == 2
    assert {row["reference"]["revision"] for row in selections(snapshot.config)} == {
        refs[0]["revision"], refs[1]["revision"]
    }
    clip_rows = snapshot.config["clips"]
    assert runtime.standin_before_placement["clipType"] == "am-discord"
    assert "elementRef" not in runtime.standin_before_placement
    assert clip_rows[0]["id"] == "occ-standin:c02-02-am-discord"
    assert clip_rows[0]["clipType"] == "reveal"
    assert clip_rows[0]["elementRef"] == refs[0]
    assert clip_rows[1]["elementRef"] == refs[1]

    timeline_path, assets_path, authority = managed_timeline.materialize_managed_render_snapshot(
        tmp_path / "managed", snapshot
    )
    export_frames = run.capture_remotion_frames(
        timeline_path,
        assets_path,
        tmp_path / "export-frames",
        list(SAMPLES.values()),
        project_dir=remotion_project,
        frame_resolution=(1920, 1080),
    )
    assert all(path.is_file() and path.stat().st_size > 0 for path in export_frames.values())

    preview_root = tmp_path / "vite-preview"
    _write_preview_app(preview_root, sources, node_modules)
    preview_frames = _capture_vite_preview(
        preview_root, tmp_path / "preview-frames", list(SAMPLES.values()),
        node_modules=node_modules, node=node,
    )
    parity = {
        label: _metrics(preview_frames[frame], export_frames[frame])
        for label, frame in SAMPLES.items()
    }
    assert all(row["pass"] for row in parity.values()), parity

    with Image.open(export_frames[SAMPLES["very"]]) as very_image:
        assert very_image.convert("RGB").getpixel((0, 0)) == (217, 79, 112)
    with Image.open(export_frames[SAMPLES["next"]]) as next_image:
        assert next_image.convert("RGB").getpixel((0, 0)) == (55, 125, 255)

    baseline_config = copy.deepcopy(snapshot.config)
    baseline_config["clips"] = [clip_rows[2]]
    baseline_config.setdefault("app", {}).pop(CARRIER, None)
    baseline_timeline = tmp_path / "builtin-only.timeline.json"
    baseline_assets = tmp_path / "builtin-only.assets.json"
    baseline_timeline.write_text(json.dumps(baseline_config), encoding="utf-8")
    baseline_assets.write_text(json.dumps({"assets": {}}), encoding="utf-8")
    builtin_baseline = run.capture_remotion_frames(
        baseline_timeline,
        baseline_assets,
        tmp_path / "builtin-baseline",
        [SAMPLES["day"]],
        project_dir=remotion_project,
        frame_resolution=(1920, 1080),
    )
    builtin_unchanged = _metrics(builtin_baseline[SAMPLES["day"]], export_frames[SAMPLES["day"]])
    assert builtin_unchanged["pass"], builtin_unchanged

    missing_rows = copy.deepcopy(runtime.documents.rows)
    runtime.documents.rows.clear()
    try:
        with pytest.raises(ValueError, match=r"snapshot missing.*\$\.clips\[0\].*c02-02-am-discord") as missing:
            resolve_managed_render_snapshot(project_ref="demo", timeline_ref="main", client=runtime)
        missing_error = str(missing.value)
    finally:
        runtime.documents.rows.update(missing_rows)

    corrupt_config = json.loads(timeline_path.read_text(encoding="utf-8"))
    corrupt_selection = selections(corrupt_config)[0]
    corrupt_selection["snapshot"]["bytes_base64"] = base64.b64encode(b"not-the-admitted-source").decode("ascii")
    corrupt_timeline = tmp_path / "corrupt.timeline.json"
    corrupt_timeline.write_text(json.dumps(corrupt_config), encoding="utf-8")
    with pytest.raises(ValueError, match=r"digest mismatch.*\$\.clips\[0\].*c02-02-am-discord") as corrupt:
        run.capture_remotion_frames(
            corrupt_timeline,
            assets_path,
            tmp_path / "corrupt-frames",
            [SAMPLES["very"]],
            project_dir=remotion_project,
            frame_resolution=(1920, 1080),
        )
    corrupt_error = str(corrupt.value)

    evidence_dir = Path(os.environ["ASTRID_T3_EVIDENCE_DIR"]).expanduser() if os.environ.get(
        "ASTRID_T3_EVIDENCE_DIR"
    ) else tmp_path / "evidence"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    source_digests = {label: "sha256:" + hashlib.sha256(source.encode()).hexdigest() for label, source in sources.items()}
    (evidence_dir / "standin-render-proof.json").write_text(
        json.dumps(
            {
                "result": "PASS: proof on a stand-in, not the owner's film",
                "placement": {
                    "shot_id": "shot-standin",
                    "occurrence_id": "occ-standin",
                    "clip_id": "c02-02-am-discord",
                    "target": "occurrence:occ-standin/clip:c02-02-am-discord",
                    "clip_type_before": "am-discord",
                    "clip_type_after_resolution": clip_rows[0]["clipType"],
                    "revisions": refs,
                    "source_sha256": source_digests,
                    "authority": authority,
                },
                "one_render": {
                    "revisions_used": [ref["revision"] for ref in refs],
                    "expected_colours": COLOURS,
                    "sample_frames": SAMPLES,
                    "observed": {"very": COLOURS["very"], "next": COLOURS["next"], "day": "built-in am-discord"},
                },
                "snapshot_errors": {"missing": missing_error, "corrupt": corrupt_error, "fallback": "none"},
                "builtin_am_discord_unchanged": builtin_unchanged,
                "parity": {
                    "tolerance": PIXEL_TOLERANCE,
                    "samples": parity,
                },
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
