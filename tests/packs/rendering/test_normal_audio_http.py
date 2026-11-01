"""Normal-route PCM proof with the invocation's real loopback asset server.

Admission is doubled at ``_validate_project_dir``; registry generation,
validation, and state are doubled at ``_regenerate_element_registries``,
``_validate_renderer_effect_registry``, and ``_effective_registry_state``;
profile, theme, effect staging, and port selection are doubled at
``_canonical_profile``, ``_resolved_theme_for_render``,
``_stage_effect_assets_for_timeline``, and ``_available_remotion_port``; and
only the final encoder subprocess is doubled.
Those doubles do not replace the runtime, materializer, server, or native WAV
preparation.  The encoder double consumes the prepared props and performs real
HTTP GETs for every URL.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import struct
import subprocess
import urllib.parse
import urllib.request
import wave
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from astrid.core.rendering.assets import InvocationAssetServer
from astrid.core.rendering.contracts import (
    SCHEMA_VERSION,
    FrameWindow,
    RenderProfile,
    RenderRequest,
)
from astrid.core.rendering.service import RenderService
from astrid.packs.rendering.rendering.renderers.remotion import run as remotion

RATE = 30_000
FRAMES = RATE * 2
STEREO = {"sound": (8192, -4096), "other": (-16384, 2048)}
WINDOW = FrameWindow(15, 45, (30, 1))


def _project_dir() -> Path:
    value = os.environ.get("ASTRID_REMOTION_PROJECT_DIR")
    project = Path(value) if value else remotion.REPO_ROOT / "remotion"
    return project.resolve(strict=True)


def test_project_dir_defaults_to_imported_remotion_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkout = tmp_path / "checkout"
    expected = checkout / "remotion"
    expected.mkdir(parents=True)
    monkeypatch.setattr(remotion, "REPO_ROOT", checkout)
    monkeypatch.delenv("ASTRID_REMOTION_PROJECT_DIR", raising=False)

    assert _project_dir() == expected.resolve()


def test_project_dir_empty_override_uses_imported_remotion_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkout = tmp_path / "checkout"
    expected = checkout / "remotion"
    expected.mkdir(parents=True)
    monkeypatch.setattr(remotion, "REPO_ROOT", checkout)
    monkeypatch.setenv("ASTRID_REMOTION_PROJECT_DIR", "")

    assert _project_dir() == expected.resolve()


def test_project_dir_uses_explicit_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    configured = tmp_path / "configured-remotion"
    configured.mkdir()
    monkeypatch.setenv("ASTRID_REMOTION_PROJECT_DIR", str(configured))

    assert _project_dir() == configured.resolve()


def _profile() -> RenderProfile:
    return RenderProfile(
        width=320,
        height=180,
        fps_rational=(30, 1),
        time_base=(1, 90_000),
        video_codec="h264",
        pixel_format="yuv420p",
    )


def _timeline() -> dict:
    fade = {"fadeIn": 0.5, "fadeOut": 0.5, "fadeInCurve": "linear", "fadeOutCurve": "linear"}
    return {
        "theme": "banodoco-default",
        "tracks": [
            {"id": "A1", "kind": "audio", "label": "Audio", "volume": 0.5},
            {"id": "A2", "kind": "audio", "label": "Boosted", "volume": 2},
            {"id": "V1", "kind": "visual", "label": "Visual"},
        ],
        "clips": [
            {
                "id": "sound",
                "track": "A1",
                "asset": "sound",
                "at": 0,
                "from": 0,
                "to": 2,
                "speed": 1,
                "volume": 3,
                "params": fade,
            },
            {
                "id": "other",
                "track": "A2",
                "asset": "other",
                "at": 0,
                "from": 0,
                "to": 2,
                "speed": 1,
                "volume": 0,
                "params": fade,
            },
            {"id": "sprite", "track": "V1", "clipType": "am-sprite", "at": 0, "hold": 2},
        ],
        "metadata": {"opaque": {"original": [1, 2]}},
    }


def _wav_payload(stereo: tuple[int, int]) -> bytes:
    stream = io.BytesIO()
    with wave.open(stream, "wb") as writer:
        writer.setparams((2, 2, RATE, 0, "NONE", "not compressed"))
        writer.writeframes(struct.pack("<hh", *stereo) * FRAMES)
    return stream.getvalue()


def _get(url: str, *, server: InvocationAssetServer) -> bytes:
    parsed = urllib.parse.urlsplit(url)
    assert parsed.scheme == "http"
    assert parsed.hostname == "127.0.0.1"
    assert parsed.port == server.port
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(url, timeout=3) as response:
        assert response.status == 200
        body = response.read()
        assert int(response.headers["Content-Length"]) == len(body)
        return body


def _url_path(url: str, *, server: InvocationAssetServer) -> Path:
    relative = urllib.parse.unquote(urllib.parse.urlsplit(url).path).lstrip("/")
    path = (server.staging_dir / relative).resolve(strict=True)
    path.relative_to(server.staging_dir)
    return path


def _assert_native_wav(body: bytes, asset: str) -> None:
    assert body[:4] == b"RIFF" and body[8:12] == b"WAVE"
    assert struct.unpack_from("<I", body, 4)[0] == len(body) - 8
    offset = 12
    chunks: dict[bytes, bytes] = {}
    while offset + 8 <= len(body):
        name, length = struct.unpack_from("<4sI", body, offset)
        chunks[name] = body[offset + 8 : offset + 8 + length]
        offset += 8 + length + length % 2
    assert struct.unpack("<HHIIHH", chunks[b"fmt "][:16]) == (3, 2, RATE, RATE * 8, 8, 32)
    data = chunks[b"data"]
    assert len(data) == FRAMES * 8
    left, right = (value / 32768 for value in STEREO[asset])
    for index, (actual_left, actual_right) in enumerate(struct.iter_unpack("<ff", data)):
        elapsed = index / RATE
        gain = max(0.0, min(1.0, elapsed / 0.5)) * max(0.0, min(1.0, (2 - elapsed) / 0.5))
        assert actual_left == pytest.approx(left * gain, rel=0, abs=2**-24), index
        assert actual_right == pytest.approx(right * gain, rel=0, abs=2**-24), index
    assert struct.unpack_from("<ff", data, 15_000 * 8) == (left, right)


def _layer_request(tmp_path: Path) -> tuple[RenderRequest, RenderRequest]:
    timeline_path = tmp_path / "timeline.json"
    timeline_path.write_text(json.dumps(_timeline()))
    request = RenderRequest(SCHEMA_VERSION, str(timeline_path), "out.mp4", window=WINDOW)
    segment = SimpleNamespace(window=WINDOW, layer=SimpleNamespace(tracks=("A1", "A2", "V1"), z=0))
    candidate = SimpleNamespace(id="fixture", manifest=SimpleNamespace(capabilities={"supports_windows": True}))
    adapted, _ = RenderService._segment_request(
        object.__new__(RenderService), request, candidate=candidate, segment=segment,
        index=0, workspace=tmp_path,
    )
    return request, adapted


def _mode_request(tmp_path: Path, mode: str) -> tuple[RenderRequest, dict]:
    request, layered = _layer_request(tmp_path)
    if mode == "full":
        return replace(request, window=None), _timeline()
    if mode == "window":
        return request, _timeline()
    assert mode == "layer"
    return layered, json.loads(Path(layered.timeline_path).read_text())


def _build_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    *,
    reused_root: bool = False,
):
    """Run the actual normal backend with only the final encoder mocked."""

    for name in (
        "PYTHONUSERBASE", "PYTHONNOUSERSITE", "PYTHONSAFEPATH", "PYTHONPLATLIBDIR",
        "PYTHONCASEOK", "PYTHONUTF8", "PYTHONCOERCECLOCALE", "PYTHONSTARTUP",
        "PYTHONEXECUTABLE", "__PYVENV_LAUNCHER__", "PYTHONHOME", "NODE_OPTIONS",
        "NODE_PATH", "NODE_ICU_DATA", "OPENSSL_CONF",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("PYTHONPYCACHEPREFIX", raising=False)
    monkeypatch.delenv("ASTRID_PROGRESS_PATH", raising=False)
    monkeypatch.setenv("ASTRID_REMOTION_STREAM_MEMORY_BUDGET_BYTES", "0")
    original_request, expected_timeline = _mode_request(tmp_path, mode)
    timeline_path = Path(original_request.timeline_path)
    timeline_before = timeline_path.read_bytes()
    payloads = {key: _wav_payload(stereo) for key, stereo in STEREO.items()}
    registry = tmp_path / "assets.json"
    registry.write_text(
        json.dumps(
            {
                "assets": {
                    key: {
                        "object_id": f"object-{key}",
                        "digest": hashlib.sha256(payload).hexdigest(),
                        "type": "audio/wav",
                        "opaque": {"asset": key, "keep": None},
                    }
                    for key, payload in payloads.items()
                }
            }
        )
    )
    registry_before = registry.read_bytes()
    project = _project_dir()
    node = Path(shutil.which("node") or "").resolve(strict=True)
    runtime = SimpleNamespace(
        node_executable=node,
        remotion_cli=project / "unused-render-cli",
        node_version="v20.19.4",
    )
    for name in ("_regenerate_element_registries", "_validate_renderer_effect_registry"):
        monkeypatch.setattr(remotion, name, lambda *args, **kwargs: None)
    monkeypatch.setattr(remotion, "_validate_project_dir", lambda *args: runtime)
    monkeypatch.setattr(remotion, "_effective_registry_state", lambda *args: {"hash": "http-fixture"})
    monkeypatch.setattr(remotion, "_canonical_profile", lambda *args: _profile())
    monkeypatch.setattr(remotion, "_resolved_theme_for_render", lambda *args: {"id": "banodoco-default"})
    monkeypatch.setattr(remotion, "_stage_effect_assets_for_timeline", lambda *args, **kwargs: {"effects": []})
    monkeypatch.setattr(remotion, "_available_remotion_port", lambda: 32101)

    source_root = tmp_path / "managed"
    source_root.mkdir()
    for key, payload in payloads.items():
        (source_root / f"{key}.wav").write_bytes(payload)
    materialized_objects: dict[str, Path | bytes]
    if reused_root:
        materialized_objects = {
            f"object-{key}": source_root / f"{key}.wav" for key in payloads
        }
    else:
        materialized_objects = {f"object-{key}": payload for key, payload in payloads.items()}
    state = SimpleNamespace(
        events=[],
        props_runs=[],
        consumed_urls=[],
        prepared_paths=[],
        servers=[],
        consume=lambda: None,
        materialized_objects=materialized_objects,
        payloads=payloads,
        expected_timeline=expected_timeline,
        mode=mode,
        timeline_path=timeline_path,
        registry=registry,
        timeline_before=timeline_before,
        registry_before=registry_before,
        source_root=source_root,
    )

    real_enter = InvocationAssetServer.__enter__
    real_exit = InvocationAssetServer.__exit__
    real_local_url = InvocationAssetServer.local_url

    def enter(server: InvocationAssetServer):
        value = real_enter(server)
        state.servers.append(server)
        state.events.append("server-enter")
        return value

    def exit(server: InvocationAssetServer, *args):
        state.events.append("server-exit")
        assert all(not path.exists() for path in state.prepared_paths)
        return real_exit(server, *args)

    def local_url(server: InvocationAssetServer, path: str | Path):
        resolved = Path(path).resolve(strict=True)
        if resolved.name == "source.wav":
            state.prepared_paths.append(resolved)
        return real_local_url(server, path)

    monkeypatch.setattr(InvocationAssetServer, "__enter__", enter)
    monkeypatch.setattr(InvocationAssetServer, "__exit__", exit)
    monkeypatch.setattr(InvocationAssetServer, "local_url", local_url)
    real_subprocess_run = subprocess.run

    def mock_encoder(argv, **kwargs):
        if str(runtime.remotion_cli) not in argv:
            return real_subprocess_run(argv, **kwargs)
        state.events.append("consume")
        server = state.servers[-1]
        assert server.thread is not None and server.thread.is_alive()
        props_path = Path(argv[argv.index("--props") + 1])
        props = json.loads(props_path.read_text())
        state.props_runs.append(props)
        assert props["timeline"]["tracks"] == expected_timeline["tracks"]
        for actual, expected in zip(props["timeline"]["clips"], expected_timeline["clips"]):
            assert all(actual[key] == value for key, value in expected.items())
            assert set(actual) - set(expected) <= {"audio"}
        assert ("--frames=15-44" in argv) == (mode != "full")
        for key, entry in props["assets"]["assets"].items():
            url = entry["file"]
            state.consumed_urls.append(url)
            path = _url_path(url, server=server)
            assert path.is_file()
            assert _get(url, server=server) == payloads[key]
        for clip in props["timeline"]["clips"]:
            source = clip.get("audio", {}).get("render_source")
            if not source:
                continue
            url = source["file"]
            state.consumed_urls.append(url)
            path = _url_path(url, server=server)
            assert path.is_file()
            body = _get(url, server=server)
            _assert_native_wav(body, clip["asset"])
            assert source["source_from"] == 0
            assert source["playback_rate"] == 1
        assert props["timeline"]["clips"][0]["volume"] == 3
        assert props["timeline"]["clips"][1]["volume"] == 0
        assert props["timeline"]["tracks"][0]["volume"] == 0.5
        assert props["timeline"]["tracks"][1]["volume"] == 2
        assert timeline_path.read_bytes() == timeline_before
        state.consume()
        Path(argv[argv.index("--output") + 1]).write_bytes(b"mocked encoder output")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(remotion.subprocess, "run", mock_encoder)

    def execute(*, reused: bool = False):
        return remotion._execute_remotion_locked(
            timeline_path,
            registry,
            tmp_path / "out.mp4",
            provenance_out_path=tmp_path / "out.mp4",
            project_dir=project,
            composition_id="TimelineComposition",
            theme_path=None,
            min_free_gb=None,
            materialized_objects=state.materialized_objects,
            materialized_root=source_root if reused else None,
            staging_parent=tmp_path,
            window=original_request.window,
        )

    state.execute = execute
    return state


@pytest.fixture(params=["full", "window", "layer"])
def normal_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest):
    state = _build_run(tmp_path, monkeypatch, str(request.param))
    yield state
    assert state.timeline_path.read_bytes() == state.timeline_before
    assert state.registry.read_bytes() == state.registry_before
    assert not list(tmp_path.glob("astrid-render-assets-*"))
    assert not list(tmp_path.glob(".remotion-runtime-*"))
    assert not (tmp_path / ".remotion-props.json").exists()


def test_actual_normal_http_wav_is_consumed_with_live_server(normal_run):
    state = normal_run
    state.execute()
    assert state.events == ["server-enter", "consume", "server-exit"]
    assert len(state.consumed_urls) == 4
    server = state.servers[0]
    assert server.port == 0
    assert server.thread is None or not server.thread.is_alive()
    assert all(not path.exists() for path in state.prepared_paths)
    assert state.timeline_path.read_bytes() == state.timeline_before
    assert state.registry.read_bytes() == state.registry_before


@pytest.mark.parametrize(
    "error",
    [RuntimeError("consumer failed"), KeyboardInterrupt(), subprocess.TimeoutExpired("encoder", 1)],
    ids=["error", "cancel", "timeout"],
)
def test_actual_normal_http_cleanup_after_encoder_failure(normal_run, error):
    normal_run.consume = lambda: (_ for _ in ()).throw(error)
    with pytest.raises(type(error)):
        normal_run.execute()
    assert normal_run.events == ["server-enter", "consume", "server-exit"]
    assert all(not path.exists() for path in normal_run.prepared_paths)
    assert normal_run.servers[0].port == 0
    assert normal_run.timeline_path.read_bytes() == normal_run.timeline_before
    assert normal_run.registry.read_bytes() == normal_run.registry_before


def test_actual_normal_http_retry_reopens_server_and_cleans_each_attempt(normal_run):
    normal_run.execute()
    first_server = normal_run.servers[0]
    normal_run.consumed_urls.clear()
    normal_run.execute()
    assert len(normal_run.servers) == 2
    assert first_server.port == 0 and normal_run.servers[1].port == 0
    assert first_server.staging_dir != normal_run.servers[1].staging_dir
    assert normal_run.events == [
        "server-enter", "consume", "server-exit", "server-enter", "consume", "server-exit",
    ]


def test_actual_normal_http_reused_materialized_root_survives_cleanup(tmp_path, monkeypatch):
    # This invokes the same real route with path-backed, reused runtime
    # materialization; the source files must outlive the server and attempt.
    state = _build_run(tmp_path, monkeypatch, "full", reused_root=True)
    state.execute(reused=True)
    assert state.servers[0].port == 0
    assert all(path.is_file() for path in state.source_root.glob("*.wav"))
    assert [path.read_bytes() for path in state.source_root.glob("*.wav")] == list(state.payloads.values())
    assert state.timeline_path.read_bytes() == state.timeline_before
    assert state.registry.read_bytes() == state.registry_before
    assert not list(tmp_path.glob("astrid-render-assets-*"))
    assert not list(tmp_path.glob(".remotion-runtime-*"))


def test_actual_normal_http_native_error_never_consumes_or_leaks(normal_run):
    raw = json.loads(normal_run.timeline_path.read_text())
    raw["clips"][0]["params"]["fadeInCurve"] = "unknown-curve"
    try:
        normal_run.timeline_path.write_text(json.dumps(raw))
        with pytest.raises(RuntimeError, match="ENVELOPE_UNRENDERED|unknown"):
            normal_run.execute()
    finally:
        normal_run.timeline_path.write_bytes(normal_run.timeline_before)
    assert normal_run.events == ["server-enter", "server-exit"]
    assert normal_run.servers[0].port == 0
    assert all(not path.exists() for path in normal_run.prepared_paths)
