"""Real render-export task execution through the kernel completion seam."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shlex
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from astrid.core.foundation.hash import sha256_file
from astrid.core.rendering import _local_schema, remotion_runtime
from astrid.core.rendering.remotion_runtime import (
    NODE_EXECUTABLE_ENV,
    REMOTION_CLI_RELATIVE_PATH,
    TIMELINE_SCHEMA_PYTHONPATH_ENV,
    remotion_runtime_status,
    resolve_remotion_runtime_tools,
)
from astrid.packs.rendering.actions.render import task_adapter as task_adapter_module
from astrid.packs.rendering.actions.render.task_adapter import (
    RenderExportExecutionContext,
    RenderExportRefused,
    RenderExportTaskAdapter,
    execute_render_export_task,
)
from astrid.packs.rendering.rendering.renderers.remotion import run as remotion_run
from astrid.sdk import workspace_client
from astrid.sdk.workspace_client import WorkspaceClient

FIXTURE_VIDEO = Path(__file__).resolve().parents[2] / "fixtures" / "reshape" / "hype_regression" / "broll.mp4"
TS = "2026-08-25T00:00:00.000000+00:00"
REMOTION_ROOT = Path(remotion_runtime.REPO_ROOT) / "remotion"
COMPOSITION_ARTIFACT = REMOTION_ROOT / json.loads(
    (REMOTION_ROOT / "package.json").read_text(encoding="utf-8")
)["dependencies"]["@banodoco/timeline-composition"].removeprefix("file:")


@pytest.fixture(autouse=True)
def _reset_process_local_remotion_custody(monkeypatch):
    # Fatal retained-child custody is process-wide. Keep independent test cases
    # isolated while allowing each case to assert that it blocks later work.
    monkeypatch.setattr(remotion_runtime, "_UNRESOLVED_PROBE_CUSTODY", [])
    monkeypatch.setattr(remotion_runtime, "_UNRESOLVED_VALIDATION_PROBES", [])


def _stage_schema(source: Path, *, missing: str | None = None) -> Path:
    for relative in (_local_schema.SCHEMA_RELATIVE_PATH, Path('runtime.lock.json'),
                     *_local_schema.READER_INPUTS):
        target = source / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if relative.as_posix() == missing:
            continue
        target.write_bytes((REMOTION_ROOT.parent / relative).read_bytes())
    if sys.platform == "win32":
        from runtime_protocol import platform

        # Admission checks every selected source ancestor's native DACL.
        for path in (source, *source.rglob("*")):
            platform.owner_only(path)
    return source / _local_schema.SCHEMA_RELATIVE_PATH


def _install_composition_package(runtime: Path) -> Path:
    package = runtime / "node_modules/@banodoco/timeline-composition"
    package.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(COMPOSITION_ARTIFACT) as archive:
        archive.extractall(runtime)
    (runtime / "package").rename(package)
    return package


def _timeline_config() -> dict:
    return {
        "theme": "banodoco-default",
        "theme_overrides": {
            "visual": {"canvas": {"width": 1920, "height": 1080, "fps": 30}}
        },
        "tracks": [{"id": "v", "kind": "visual", "label": "Video"}],
        "clips": [
            {
                "id": "source-clip",
                "at": 0,
                "track": "v",
                "clipType": "media",
                "asset": "source",
                "from": 0,
                "to": 2,
                "speed": 1,
                "volume": 0,
            }
        ],
    }


def _task(*, root: Path, project_slug: str = "render-project") -> SimpleNamespace:
    digest = sha256_file(FIXTURE_VIDEO)
    # The worker receives an explicit host handoff.  This is deliberately
    # staged under the invocation's materialized root; no product locator or
    # local managed-media authority is consulted by the adapter.
    materialized = root / "staging" / "managed-objects" / "media-source.mp4"
    materialized.parent.mkdir(parents=True, exist_ok=True)
    materialized.write_bytes(FIXTURE_VIDEO.read_bytes())
    return SimpleNamespace(
        id="render-task-1",
        created_at=TS,
        spec={
            "family": "render_export",
            "project_slug": project_slug,
            "params": {
                "timeline_ref": "timeline-1",
                "expected_version": 1,
                "output_filename": "requested-render.mp4",
                "materialized_objects": {"media-source": str(materialized)},
            },
            "timeline_snapshot": {
                "timeline_id": "timeline-id",
                "timeline_ulid": "timeline-1",
                "config": _timeline_config(),
                "registry": {
                    "assets": {
                        "source": {
                            "media_id": "media-source",
                            "content_sha256": f"sha256:{digest}",
                            "type": "video/mp4",
                        }
                    }
                },
                "config_version": 1,
            },
        },
    )


def test_render_export_adapter_writes_real_mp4_and_is_callable(tmp_path: Path) -> None:
    (tmp_path / "render-project").mkdir(parents=True, exist_ok=True)
    task = _task(root=tmp_path)
    manifest = execute_render_export_task(
        task=task,
        staging_dir=tmp_path / "staging",
    )
    output = tmp_path / "staging" / manifest["outputs"][0]["path"]
    assert manifest["outputs"][0]["path"] == "requested-render.mp4"
    staged_registry = json.loads(
        (tmp_path / "staging" / "render-inputs" / "assets.json").read_text()
    )
    staged_asset = staged_registry["assets"]["source"]
    assert staged_asset["media_id"] == "media-source"
    assert f"sha256:{staged_asset['content_sha256']}" == task.spec["timeline_snapshot"]["registry"]["assets"]["source"]["content_sha256"]
    assert Path(staged_asset["file"]).is_relative_to(
        (tmp_path / "staging" / "render-inputs" / "assets").resolve()
    )
    raw = output.read_bytes()
    assert raw[4:8] == b"ftyp"
    assert manifest["outputs"][0]["role"] == "result"
    assert manifest["outputs"][0]["content_hash"] == f"sha256:{hashlib.sha256(raw).hexdigest()}"
    assert manifest["inputs"]["selector"] == "rendering.ffmpeg"


def test_media_timeline_selects_ffmpeg_without_remotion_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An ordinary media timeline must never synthesize a Remotion project."""

    (tmp_path / "render-project").mkdir(parents=True, exist_ok=True)
    task = _task(root=tmp_path)
    captured: dict[str, Any] = {}

    def fail_remotion_probe(**_kwargs: Any) -> None:
        raise AssertionError("FFmpeg-only timeline probed the Remotion runtime")

    def fake_run_executor(request: Any, _registry: Any) -> Any:
        captured.update(request.inputs)
        assert "ASTRID_REMOTION_PROJECT_DIR" not in os.environ
        output = Path(request.out) / request.inputs["output_name"]
        output.write_bytes(b"\x00\x00\x00\x18ftyp")
        return SimpleNamespace(
            ok=True,
            error=None,
            payload={},
            outputs={"video": output},
        )

    monkeypatch.delenv("ASTRID_REMOTION_PROJECT_DIR", raising=False)
    monkeypatch.setattr(remotion_runtime, "remotion_runtime_status", fail_remotion_probe)
    monkeypatch.setattr(task_adapter_module, "run_executor", fake_run_executor)
    monkeypatch.setattr(task_adapter_module, "load_default_registry", lambda: object())

    manifest = execute_render_export_task(
        task=task,
        staging_dir=tmp_path / "staging",
    )

    assert captured["selector"] == "rendering.ffmpeg"
    assert "backend_config" not in captured
    assert manifest["inputs"] == {
        "family": "render_export",
        "project_slug": "render-project",
        "timeline_ref": "timeline-1",
        "expected_version": 1,
        "semantic_role": "render",
        "selector": "rendering.ffmpeg",
    }


def test_admitted_theme_reaches_executor_without_changing_materialized_handoff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "render-project").mkdir(parents=True, exist_ok=True)
    task = _task(root=tmp_path)
    theme_path = tmp_path / "staging" / "managed-objects" / "theme.json"
    theme_path.write_text('{"id":"admitted-theme"}\n', encoding="utf-8")
    materialized_objects = dict(task.spec["params"]["materialized_objects"])
    materialized_objects["theme"] = str(theme_path)
    task.spec["params"] = {
        **task.spec["params"],
        "materialized_objects": materialized_objects,
    }
    captured: dict[str, Any] = {}

    def fake_run_executor(request: Any, _registry: Any) -> Any:
        captured.update(request.inputs)
        output = Path(request.out) / request.inputs["output_name"]
        output.write_bytes(b"\x00\x00\x00\x18ftyp")
        return SimpleNamespace(ok=True, error=None, payload={}, outputs={"video": output})

    monkeypatch.setattr(task_adapter_module, "run_executor", fake_run_executor)
    monkeypatch.setattr(task_adapter_module, "load_default_registry", lambda: object())

    execute_render_export_task(task=task, staging_dir=tmp_path / "staging")

    assert captured["theme"] == str(theme_path)
    assert captured["materialized_objects"] == materialized_objects


def test_unbound_theme_path_does_not_override_builtin_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "render-project").mkdir(parents=True, exist_ok=True)
    task = _task(root=tmp_path)
    forged_theme = tmp_path / "forged-theme.json"
    forged_theme.write_text('{"id":"forged-theme"}\n', encoding="utf-8")
    task.spec["params"] = {
        **task.spec["params"],
        "theme": str(forged_theme),
    }
    captured: dict[str, Any] = {}

    def fake_run_executor(request: Any, _registry: Any) -> Any:
        captured.update(request.inputs)
        output = Path(request.out) / request.inputs["output_name"]
        output.write_bytes(b"\x00\x00\x00\x18ftyp")
        return SimpleNamespace(ok=True, error=None, payload={}, outputs={"video": output})

    monkeypatch.setattr(task_adapter_module, "run_executor", fake_run_executor)
    monkeypatch.setattr(task_adapter_module, "load_default_registry", lambda: object())

    execute_render_export_task(task=task, staging_dir=tmp_path / "staging")

    assert "theme" not in captured


def test_theme_outside_materialization_root_is_refused(tmp_path: Path) -> None:
    (tmp_path / "render-project").mkdir(parents=True, exist_ok=True)
    task = _task(root=tmp_path)
    forged_theme = tmp_path / "outside-theme.json"
    forged_theme.write_text('{"id":"forged-theme"}\n', encoding="utf-8")
    materialized_objects = dict(task.spec["params"]["materialized_objects"])
    materialized_objects["theme"] = str(forged_theme)
    task.spec["params"] = {
        **task.spec["params"],
        "materialized_objects": materialized_objects,
    }

    with pytest.raises(RenderExportRefused, match="beneath the host materialization root"):
        RenderExportTaskAdapter().execute(task=task, staging_dir=tmp_path / "staging")


@pytest.mark.parametrize("name", ["project_dir", "backend_config"])
def test_task_cannot_select_remotion_project_or_backend(
    tmp_path: Path, name: str
) -> None:
    task = _task(root=tmp_path)
    params = dict(task.spec["params"])
    params[name] = "/caller/selected/remotion"
    task.spec = {**task.spec, "params": params}

    with pytest.raises(RenderExportRefused, match=f"server-owned parameter.*{name}"):
        execute_render_export_task(task=task, staging_dir=tmp_path / "staging")


def test_renderer_inputs_are_inode_isolated_and_cleaned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Renderer writes cannot mutate managed media or durable staging evidence."""

    (tmp_path / "render-project").mkdir(parents=True, exist_ok=True)
    task = _task(root=tmp_path)
    digest = task.spec["timeline_snapshot"]["registry"]["assets"]["source"][
        "content_sha256"
    ].removeprefix("sha256:")
    managed = tmp_path / "staging" / "managed-objects" / "media-source.mp4"
    captured: dict[str, Any] = {}

    def fake_run_executor(request, _registry):
        owned_registry_path = Path(request.inputs["assets_registry"])
        owned_registry = json.loads(owned_registry_path.read_text(encoding="utf-8"))
        owned_asset = Path(owned_registry["assets"]["source"]["file"])
        staged_registry = json.loads(
            (tmp_path / "staging" / "render-inputs" / "assets.json").read_text(
                encoding="utf-8"
            )
        )
        staged_asset = Path(staged_registry["assets"]["source"]["file"])
        input_inodes = {
            managed.stat().st_ino,
            staged_asset.stat().st_ino,
            owned_asset.stat().st_ino,
        }
        assert len(input_inodes) == 3

        captured["owned_root"] = owned_registry_path.parent
        captured["staged_asset"] = staged_asset
        owned_asset.write_bytes(b"renderer mutated its private input")
        output = Path(request.out) / request.inputs["output_name"]
        output.write_bytes(b"\x00\x00\x00\x18ftyp")
        return SimpleNamespace(
            ok=True,
            error=None,
            payload={},
            outputs={"video": output},
        )

    monkeypatch.setattr(task_adapter_module, "run_executor", fake_run_executor)
    monkeypatch.setattr(task_adapter_module, "load_default_registry", lambda: object())

    RenderExportTaskAdapter().execute(
        task=task,
        staging_dir=tmp_path / "staging",
    )

    assert sha256_file(managed) == digest
    assert sha256_file(captured["staged_asset"]) == digest
    assert not captured["owned_root"].exists()


def test_owned_input_setup_failure_is_cleaned(tmp_path: Path) -> None:
    (tmp_path / "render-project").mkdir(parents=True, exist_ok=True)
    task = _task(root=tmp_path)
    task.spec["params"] = {
        **task.spec["params"],
        "output_filename": "../escape.mp4",
    }

    with pytest.raises(RenderExportRefused, match="plain .mp4 filename"):
        RenderExportTaskAdapter().execute(
            task=task,
            staging_dir=tmp_path / "staging",
        )

    assert list((tmp_path / "render-project").glob(".render-inputs-*")) == []


@pytest.mark.integration
def test_forced_caption_uses_server_owned_executable_routing_and_isolation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify installed executable routing/isolation; the fake Node FFmpeg-copies fixture video, so this is not a text-render proof."""

    (tmp_path / "render-project").mkdir(parents=True, exist_ok=True)
    runtime = tmp_path / "installed-remotion"
    _install_composition_package(runtime)
    generated_src = runtime / "src"
    generated_src.mkdir(parents=True)
    for kind in ("effects", "animations", "transitions"):
        provisioned = Path(remotion_runtime.REPO_ROOT) / 'remotion/src' / f'{kind}.generated.ts'
        assert provisioned.is_file(), 'the pinned provisioned registry is required'
        (generated_src / f"{kind}.generated.ts").write_bytes(provisioned.read_bytes())
    (runtime / "package.json").write_text('{"private":true}\n', encoding="utf-8")
    cli = runtime / REMOTION_CLI_RELATIVE_PATH
    cli.parent.mkdir(parents=True)
    cli.write_text("// pinned local Remotion CLI\n", encoding="utf-8")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    (fake_bin / "python3").symlink_to(Path(sys.executable).resolve())
    real_ffprobe = shutil.which("ffprobe")
    if real_ffprobe is None:
        pytest.fail("the installed ffprobe binary is required for this integration test")
    real_ffmpeg = shutil.which("ffmpeg")
    if real_ffmpeg is None:
        pytest.fail("the installed ffmpeg binary is required for this integration test")
    source_video = tmp_path / "source.mp4"
    source_video.write_bytes(FIXTURE_VIDEO.read_bytes())
    trusted_node = tmp_path / "trusted-node"
    trusted_node.write_text(
        "#!/usr/bin/env python3\n"
        "import pathlib, subprocess, sys\n"
        "if sys.argv[1:] == ['--version']:\n"
        "    print('v20.19.4')\n"
        "    raise SystemExit(0)\n"
        "args = sys.argv[1:]\n"
        "out = pathlib.Path(args[args.index('--output') + 1])\n"
        f"subprocess.run([{str(real_ffmpeg)!r}, '-loglevel', 'error', '-y', '-i', {str(source_video)!r}, '-c', 'copy', '-video_track_timescale', '90000', str(out)], check=True)\n",
        encoding="utf-8",
    )
    (fake_bin / "npx").write_text(
        "#!/usr/bin/env python3\n"
        "import pathlib\n"
        f"pathlib.Path({str(tmp_path / 'npx-used')!r}).write_text('unexpected npx')\n"
        "raise SystemExit(97)\n",
        encoding="utf-8",
    )
    # Keep the deployment-shaped PATH free of node while still exposing the
    # real media probe through an explicit, trusted binary path.
    (fake_bin / "ffprobe").write_text(
        f"#!/bin/sh\nexec {shlex.quote(real_ffprobe)} \"$@\"\n", encoding="utf-8"
    )
    # The executor re-checks the configured VibeComfy checkout's revision with
    # ``git`` from PATH (vibecomfy_dependency.configured_vibecomfy_checkout), so a
    # deployment-shaped PATH must still expose git.
    real_git = shutil.which("git")
    if real_git is None:
        pytest.fail("the installed git binary is required for this integration test")
    (fake_bin / "git").write_text(
        f"#!/bin/sh\nexec {shlex.quote(real_git)} \"$@\"\n", encoding="utf-8"
    )
    for executable in (trusted_node, fake_bin / "npx", fake_bin / "ffprobe", fake_bin / "git"):
        executable.chmod(0o755)
    monkeypatch.setenv("ASTRID_REMOTION_PROJECT_DIR", str(runtime))
    monkeypatch.setenv(NODE_EXECUTABLE_ENV, str(trusted_node))
    monkeypatch.setenv("PATH", str(fake_bin))
    assert shutil.which("python3") is not None
    assert shutil.which("ffprobe") is not None
    assert shutil.which("node") is None
    # The source-profile probe selects the actual canonical reader and the
    # already installed standards validator, without an external model tree.
    schema = REMOTION_ROOT.parent / _local_schema.SCHEMA_RELATIVE_PATH
    monkeypatch.setenv(TIMELINE_SCHEMA_PYTHONPATH_ENV, str(schema))
    probe_env = os.environ.copy()
    probe_env.pop("PYTHONPATH", None)
    schema_origin = subprocess.check_output(
        [
            sys.executable,
            "-I", "-c", remotion_runtime._SCHEMA_PROBE,
            str(schema), str(REMOTION_ROOT.parent), 'source',
        ],
        text=True,
        env=probe_env,
    ).strip()
    assert Path(schema_origin).resolve() == schema.resolve()
    import banodoco_timeline_schema as ambient_schema

    ambient_origin = Path(ambient_schema.__file__).resolve()
    assert not ambient_origin.is_relative_to(schema.parent.resolve())
    (runtime / "package-lock.json").write_text('{"lockfileVersion":3}\n')
    monkeypatch.setattr(remotion_run, "gen_effect_registry", None)
    reference = remotion_runtime.prepare_remotion_validation(
        artifact_directory=tmp_path / "owned-readiness", host_environment=dict(os.environ),
        python_executable=sys.executable, launch_id="b" * 32,
        contexts={"standalone": dict(os.environ)})
    for name, value in reference.items():
        monkeypatch.setenv(name, value)
    assert remotion_runtime_status().available
    # This simulates the installed wheel, where authoring-only
    # scripts/gen_effect_registry.py is deliberately not packaged.  The
    # release-installed Remotion bundle must provide generated registries.
    monkeypatch.setattr(remotion_run, "gen_effect_registry", None)

    task = _task(root=tmp_path)
    snapshot = dict(task.spec["timeline_snapshot"])
    config = dict(snapshot["config"])
    # The fixture MP4 is 1280x720; keep the declared render profile aligned so
    # ffprobe validates the artifact produced by the trusted Node harness.
    config["theme_overrides"] = {
        **config["theme_overrides"],
        "visual": {
            **config["theme_overrides"]["visual"],
            "canvas": {"width": 1280, "height": 720, "fps": 24},
        },
    }
    config["clips"] = [
        {
            "id": "caption",
            "at": 0,
            "track": "v",
            "clipType": "text-card",
            "hold": 1,
            "params": {"content": "Installed runtime"},
        }
    ]
    snapshot["config"] = config
    task.spec = {**task.spec, "timeline_snapshot": snapshot}

    manifest = execute_render_export_task(
        task=task,
        staging_dir=tmp_path / "staging",
    )
    output = tmp_path / "staging" / manifest["outputs"][0]["path"]
    assert output.read_bytes()[4:8] == b"ftyp"
    assert manifest["inputs"]["selector"] == "rendering.remotion"
    assert not (tmp_path / "npx-used").exists()


def test_forced_caption_fails_before_renderer_without_server_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "render-project").mkdir(parents=True, exist_ok=True)
    # Make the absence explicit: the source checkout may itself contain a
    # valid Remotion bundle during the pinned CI run.
    monkeypatch.setenv("ASTRID_REMOTION_PROJECT_DIR", str(tmp_path / "missing-remotion"))
    task = _task(root=tmp_path)
    snapshot = dict(task.spec["timeline_snapshot"])
    config = dict(snapshot["config"])
    config["clips"] = [{"id": "caption", "clipType": "text-card", "at": 0, "hold": 1}]
    snapshot["config"] = config
    task.spec = {**task.spec, "timeline_snapshot": snapshot}
    with pytest.raises(RenderExportRefused, match="server-owned Remotion runtime unavailable"):
        RenderExportTaskAdapter().execute(
            task=task, staging_dir=tmp_path / "staging"
        )


@pytest.mark.parametrize(
    "missing_file",
    (_local_schema.SCHEMA_RELATIVE_PATH.as_posix(), 'runtime.lock.json',
     *(p.as_posix() for p in _local_schema.READER_INPUTS)),
)
def test_trusted_schema_readiness_requires_complete_install(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    missing_file: str,
) -> None:
    source = tmp_path / "agent"
    schema = _stage_schema(source, missing=missing_file)
    monkeypatch.setattr(remotion_runtime, 'REPO_ROOT', source)
    runtime = tmp_path / "installed-remotion"
    _install_composition_package(runtime)
    (runtime / "package.json").write_text("{}", encoding="utf-8")
    cli = runtime / REMOTION_CLI_RELATIVE_PATH
    cli.parent.mkdir(parents=True)
    cli.write_text("// pinned local Remotion CLI\n", encoding="utf-8")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    trusted_node = fake_bin / "node"
    trusted_node.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = \"--version\" ]; then printf 'v20.19.4\\n'; fi\n"
        "exit 0\n",
        encoding="utf-8",
    )
    trusted_node.chmod(0o755)
    monkeypatch.setenv("ASTRID_REMOTION_PROJECT_DIR", str(runtime))
    monkeypatch.setenv(NODE_EXECUTABLE_ENV, str(trusted_node))
    monkeypatch.setenv(TIMELINE_SCHEMA_PYTHONPATH_ENV, str(schema))
    monkeypatch.setenv("PATH", f"{fake_bin}:{os.environ['PATH']}")
    status = remotion_runtime_status()
    assert not status.available
    assert missing_file in (status.reason or "")


def _runtime_with_locked_packages(tmp_path: Path) -> Path:
    runtime = tmp_path / "runtime"
    _install_composition_package(runtime)
    (runtime / "package.json").write_text("{}", encoding="utf-8")
    return runtime


def _version_node(path: Path, *, marker: Path | None = None) -> None:
    marker_line = (
        f"pathlib.Path({str(marker)!r}).write_text('used')\n" if marker else ""
    )
    path.write_text(
        "#!/usr/bin/env python3\n"
        "import pathlib, sys\n"
        "if sys.argv[1:] == ['--version']:\n"
        "    print('v20.19.4')\n"
        "    raise SystemExit(0)\n"
        + marker_line
        + "raise SystemExit(97)\n",
        encoding="utf-8",
    )
    path.chmod(0o755)


def test_remotion_readiness_requires_exact_locked_local_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = _runtime_with_locked_packages(tmp_path)
    node = tmp_path / "trusted-node"
    _version_node(node)
    monkeypatch.setenv("ASTRID_REMOTION_PROJECT_DIR", str(runtime))
    monkeypatch.setenv(NODE_EXECUTABLE_ENV, str(node))

    status = remotion_runtime_status()
    assert not status.available
    assert "locked Remotion CLI is missing" in (status.reason or "")

    wrong_cli = runtime / "node_modules" / "@remotion" / "cli" / "wrong.js"
    wrong_cli.parent.mkdir(parents=True)
    wrong_cli.write_text("// wrong entrypoint\n", encoding="utf-8")
    status = remotion_runtime_status()
    assert not status.available
    assert "locked Remotion CLI is missing" in (status.reason or "")


def test_remotion_runtime_uses_absolute_node_not_hostile_path(tmp_path, monkeypatch):
    project, _, trusted_node, environment, reference, calls = _owned_validation_fixture(
        tmp_path, monkeypatch, environment_overrides={"PATH": str(tmp_path / "hostile")}
    )
    tools, error = resolve_remotion_runtime_tools(
        project, environment={**environment, **reference}
    )
    assert error is None
    assert tools is not None
    assert tools.node_executable == trusted_node.resolve()
    assert tools.remotion_cli == (project / REMOTION_CLI_RELATIVE_PATH).resolve()
    assert calls[0][0] == [str(trusted_node), "--version"]


def test_remotion_node_version_probe_is_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], 5)

    monkeypatch.setattr(remotion_runtime, "_owned_probe", timeout)
    version, error = remotion_runtime._probe_node(
        tmp_path / "trusted-node", cwd=tmp_path
    )
    assert version is None
    assert error is not None
    assert "version probe failed" in error


def test_render_export_adapter_fails_closed_without_snapshot_or_project(tmp_path: Path) -> None:
    task = _task(root=tmp_path)
    task.spec = dict(task.spec)
    task.spec.pop("timeline_snapshot")
    with pytest.raises(RenderExportRefused, match="timeline snapshot"):
        RenderExportTaskAdapter().execute(
            task=task, staging_dir=tmp_path / "staging"
        )


def test_render_export_adapter_has_cooperative_cancel_and_progress_seam(
    tmp_path: Path,
) -> None:
    (tmp_path / "render-project").mkdir(parents=True, exist_ok=True)
    task = _task(root=tmp_path)
    seen: list[dict] = []
    context = RenderExportExecutionContext.bounded(
        cancelled=lambda: True,
        progress=seen.append,
    )
    with pytest.raises(RenderExportRefused, match="cancelled"):
        RenderExportTaskAdapter().execute(
            task=task,
            staging_dir=tmp_path / "staging",
            context=context,
        )
    assert seen == []
    # Project slugs are runtime identity, not local checkout selectors.  A
    # different slug therefore does not alter the attempt-local boundary.
    task = _task(root=tmp_path, project_slug="missing")
    assert task.spec["project_slug"] == "missing"


@pytest.mark.parametrize(
    "change, expected",
    [
        ("missing_asset", "runtime object .* no host materialization"),
        ("missing_renderer", "server-owned"),
    ],
)
def test_render_export_adapter_fails_closed_on_missing_asset_or_renderer(
    tmp_path: Path, change: str, expected: str
) -> None:
    (tmp_path / "render-project").mkdir(parents=True, exist_ok=True)
    task = _task(root=tmp_path)
    task.spec = dict(task.spec)
    snapshot = dict(task.spec["timeline_snapshot"])
    snapshot["registry"] = {
        "assets": {
            "source": {
                "media_id": "missing-media",
                "content_sha256": "sha256:" + "0" * 64,
                "type": "video/mp4",
            }
        }
    }
    task.spec["timeline_snapshot"] = snapshot
    if change == "missing_renderer":
        task.spec["params"] = {**task.spec["params"], "engine": "rendering.not-installed"}
    with pytest.raises(RenderExportRefused, match=expected):
        RenderExportTaskAdapter().execute(
            task=task, staging_dir=tmp_path / "staging"
        )

def test_generated_runtime_attempt_settlement_round_trip(monkeypatch: pytest.MonkeyPatch) -> None:
    """Attempt completion crosses only the generated neutral-runtime client.

    The former local SQLite task/media round trip was retired with local
    authority. This fake generated client preserves the wire/argument contract
    while keeping the test independent of product stores and repositories.
    """
    calls: list[tuple[str, dict[str, Any], str]] = []

    class Generated:
        def __init__(self, _endpoint: str, _token: str) -> None:
            pass

        def settle_attempt(self, attempt_id: str, settlement: dict[str, Any], *, idempotency_key: str) -> dict[str, Any]:
            calls.append((attempt_id, settlement, idempotency_key))
            return {"attempt_id": attempt_id, "status": settlement["status"], "outputs": settlement["output_objects"]}

    monkeypatch.setattr(workspace_client, "GeneratedWorkspaceClient", Generated)
    client = WorkspaceClient("https://127.0.0.1:8443", "runtime-token")
    settlement = {
        "status": "succeeded",
        "output_objects": [{"name": "rendered", "object_id": "sha256:" + "a" * 64}],
    }
    response = client.settle_attempt("attempt-1", settlement, idempotency_key="settle-1")
    assert response == {"attempt_id": "attempt-1", "status": "succeeded", "outputs": settlement["output_objects"]}
    assert calls == [("attempt-1", settlement, "settle-1")]


def _owned_validation_fixture(tmp_path, monkeypatch, *, environment_overrides=None, schema_result_transform=None, worker_contexts=None):
    """Production-shaped inputs and successful original wait receipts; no launch."""
    runtime = tmp_path / 'runtime'
    _install_composition_package(runtime)
    from astrid.packs.rendering.rendering.renderers.remotion import run as registry_run
    monkeypatch.setattr(registry_run, 'gen_effect_registry', None)
    for output in registry_run._registry_output_paths(runtime):
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text('// provisioned registry')
    cli = runtime / REMOTION_CLI_RELATIVE_PATH
    cli.parent.mkdir(parents=True)
    cli.write_text('// exact CLI')
    (runtime / 'package.json').write_text('{}')
    (runtime / 'package-lock.json').write_text('{}')
    source = tmp_path / 'agent'
    schema = _stage_schema(source)
    monkeypatch.setattr(remotion_runtime, 'REPO_ROOT', source)
    node = tmp_path / 'node'
    node.write_text('server-owned Node')
    node.chmod(0o700)
    env = {'HOME': str(tmp_path), 'LANG': 'C.UTF-8', 'PATH': '/usr/bin', 'TMPDIR': str(tmp_path),
           'ASTRID_REMOTION_PROJECT_DIR': str(runtime), 'ASTRID_NODE_EXECUTABLE': str(node),
           'ASTRID_TIMELINE_SCHEMA_PYTHONPATH': str(schema)}
    env.update(environment_overrides or {})
    site = tmp_path / 'site-packages'
    site.mkdir()
    (site / 'editable.pth').write_text('import safe_finder')
    (site / 'safe_finder.py').write_text('# installed finder')
    (site / 'safe_finder.pyc').write_bytes(b'loaded bytecode bytes')
    dep = site / 'native_dep'
    dep.mkdir()
    (dep / 'extension.so').write_text('native dependency bytes')
    (dep / 'resources.json').write_text('{}')
    calls = []
    def owned(argv, *, cwd, env, timeout):
        if argv[-1] != '--version':
            assert not list((tmp_path / 'artifacts').glob('remotion-validation-*.json'))
        calls.append((argv, dict(env)))
        receipt = {'original_wait_exit_code': 0, 'retained_set_final_census_complete': True,
                   'custody_admitted': True, 'original_child_pid': 500 + len(calls)}
        if argv[-1] == '--version':
            return subprocess.CompletedProcess(argv, 0, 'v20.19.4\n', ''), receipt
        closure = {'origin': str(schema),
                   'package_roots': [str(dep)], 'module_files': [str(site / 'safe_finder.py'), str(site / 'safe_finder.pyc')], 'search_paths': [str(site)],
                   'startup': {'executable': sys.executable, 'prefix': sys.prefix, 'exec_prefix': sys.exec_prefix,
                               'base_prefix': sys.base_prefix, 'base_exec_prefix': sys.base_exec_prefix,
                               'version': sys.version, 'cache_tag': sys.implementation.cache_tag,
                               'platform': sys.platform, 'abi_flags': getattr(sys, 'abiflags', ''),
                               'locale_env': dict(env)}}
        if schema_result_transform is not None:
            schema_result_transform(closure, len(calls))
        return subprocess.CompletedProcess(argv, 0, json.dumps(closure), ''), receipt
    monkeypatch.setattr(remotion_runtime, '_owned_probe', owned)
    reference = remotion_runtime.prepare_remotion_validation(
        artifact_directory=tmp_path / 'artifacts', host_environment=env,
        python_executable=sys.executable, launch_id='a' * 32,
        contexts={'host': env, 'worker': (worker_contexts(env) if worker_contexts else remotion_runtime_context_worker(env))})
    return runtime, schema, node, env, reference, calls


def remotion_runtime_context_worker(environment):
    from astrid.core.subprocess_env import build_child_subprocess_env
    return build_child_subprocess_env(base=environment, parent=environment)


def test_launch_validation_owned_waits_before_publish_and_reuse_without_probe(tmp_path, monkeypatch):
    runtime, _, _, env, ref, calls = _owned_validation_fixture(tmp_path, monkeypatch)
    assert len(calls) == 3  # Identical actual host/worker contexts share one result.
    actual = {**env, **ref}
    assert remotion_runtime._validated_runtime(runtime, environment=actual)['node_version'] == 'v20.19.4'
    worker = remotion_runtime_context_worker(actual)
    worker['ASTRID_REMOTION_VALIDATION_ROLE'] = 'worker'
    worker.update({'ASTRID_TASK_RUN_ID': 'new-task', 'ASTRID_RUNTIME_CREDENTIAL': 'SECRET'})
    remotion_runtime._validated_runtime(runtime, environment=worker)
    assert len(calls) == 3
    raw = Path(ref['ASTRID_REMOTION_VALIDATION_PATH']).read_text()
    assert 'SECRET' not in raw and 'new-task' not in raw


@pytest.mark.parametrize('key,value', [('HOME', '/different'), ('LANG', ''), ('PATH', '/other'),
                                     ('TMPDIR', '/other'), ('ASTRID_REMOTION_VALIDATION_LAUNCH', 'b' * 32),
                                     ('ASTRID_REMOTION_VALIDATION_ROLE', 'foreign')])
def test_launch_validation_rejects_context_or_foreign_launch_without_fallback(tmp_path, monkeypatch, key, value):
    runtime, _, _, env, ref, calls = _owned_validation_fixture(tmp_path, monkeypatch)
    actual = {**env, **ref, key: value}
    with pytest.raises(remotion_runtime.RemotionValidationError):
        remotion_runtime._validated_runtime(runtime, environment=actual)
    assert len(calls) == 3


@pytest.mark.parametrize('which', ['node', 'cli', 'schema', 'lock', 'package', 'added_package_file'])
def test_launch_validation_rejects_changed_installed_content(tmp_path, monkeypatch, which):
    runtime, schema, node, env, ref, calls = _owned_validation_fixture(tmp_path, monkeypatch)
    paths = {'node': node, 'cli': runtime / REMOTION_CLI_RELATIVE_PATH,
             'schema': schema,
             'lock': runtime / 'package-lock.json', 'package': runtime / 'package.json',
             'added_package_file': runtime / 'node_modules/new-runtime-code.js'}
    paths[which].write_text('changed')
    with pytest.raises(remotion_runtime.RemotionValidationError, match='inputs changed'):
        remotion_runtime._validated_runtime(runtime, environment={**env, **ref})
    assert len(calls) == 3


@pytest.mark.parametrize('origin', ['/foreign/timeline-config.schema.json', '/foreign', 'relative'])
def test_launch_validation_refuses_foreign_schema_origin(tmp_path, monkeypatch, origin):
    def foreign(closure, _count):
        closure['origin'] = origin
    with pytest.raises(remotion_runtime.RemotionValidationError, match='invalid identity'):
        _owned_validation_fixture(tmp_path, monkeypatch, schema_result_transform=foreign)
    assert not list((tmp_path / 'artifacts').glob('remotion-validation-*.json'))


@pytest.mark.parametrize('change', ['lock', 'schema_and_lock', 'reader_mode'])
def test_launch_validation_refuses_changed_canonical_authority(tmp_path, monkeypatch, change):
    runtime, schema, _, env, ref, calls = _owned_validation_fixture(tmp_path, monkeypatch)
    lock_path = remotion_runtime.REPO_ROOT / 'runtime.lock.json'
    lock = json.loads(lock_path.read_text())
    pin = lock['files'][_local_schema.SCHEMA_RELATIVE_PATH.as_posix()]
    if change == 'lock':
        pin['sha256'] = 'sha256:' + '0' * 64
    elif change == 'schema_and_lock':
        schema.write_text('{}')
        pin['sha256'] = 'sha256:' + hashlib.sha256(schema.read_bytes()).hexdigest()
    else:
        (remotion_runtime.REPO_ROOT / _local_schema.READER_INPUTS[0]).chmod(0o666)
    lock_path.write_text(json.dumps(lock))
    with pytest.raises(remotion_runtime.RemotionValidationError, match='inputs changed'):
        remotion_runtime._validated_runtime(runtime, environment={**env, **ref})
    assert len(calls) == 3


def test_launch_validation_rejects_changed_extracted_helper_source(tmp_path, monkeypatch):
    helper_source = Path(remotion_runtime._remotion_package.__file__).resolve()
    owned_source = tmp_path / 'owned-source' / helper_source.name
    owned_source.parent.mkdir()
    shutil.copy2(helper_source, owned_source)
    spec = importlib.util.spec_from_file_location('owned_remotion_package', owned_source)
    assert spec is not None and spec.loader is not None
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    monkeypatch.setattr(remotion_runtime, '_remotion_package', helper)
    monkeypatch.setattr(remotion_runtime, '_composition_package_problem', helper.composition_package_problem)

    runtime, _, _, env, ref, calls = _owned_validation_fixture(tmp_path, monkeypatch)
    actual = {**env, **ref}
    remotion_runtime._validated_runtime(runtime, environment=actual)
    owned_source.write_bytes(owned_source.read_bytes() + b'\n')
    with pytest.raises(remotion_runtime.RemotionValidationError, match='inputs changed'):
        remotion_runtime._validated_runtime(runtime, environment=actual)
    assert len(calls) == 3


@pytest.mark.parametrize('home', [None, '', 'relative'])
def test_clean_probe_home_refuses_before_launch(home):
    environment = {} if home is None else {'HOME': home}
    with pytest.raises(remotion_runtime.RemotionValidationError, match='absolute HOME'):
        remotion_runtime.probe_context(environment=environment, python_executable=sys.executable)


@pytest.mark.parametrize('name', ['DYLD_INSERT_LIBRARIES', 'LD_PRELOAD', 'PYTHONHOME',
                                 'PYTHONEXECUTABLE', '__PYVENV_LAUNCHER__', 'NODE_OPTIONS',
                                 'NODE_PATH', 'NODE_ICU_DATA', 'OPENSSL_CONF'])
def test_clean_probe_ignores_ambient_but_rejects_consuming_overrides(tmp_path, name):
    clean = {'HOME': str(tmp_path)}
    ambient = {**clean, name: 'override'}
    assert remotion_runtime.probe_context(environment=ambient, python_executable=sys.executable) == remotion_runtime.probe_context(
        environment=clean, python_executable=sys.executable)
    with pytest.raises(remotion_runtime.RemotionValidationError, match=name):
        remotion_runtime._validate_remotion_worker_environment(ambient)


def test_clean_probe_projection_preserves_values_absence_and_excludes_task_metadata(tmp_path):
    env = {'HOME': str(tmp_path), 'LANG': '', 'PATH': 'actual-path', 'TMPDIR': 'actual-tmp',
           'ASTRID_TASK_RUN_ID': 'task', 'COVERAGE_PROCESS_START': 'private-instrumentation',
           'ASTRID_RUNTIME_CREDENTIAL': 'SECRET', 'SETUPTOOLS_USE_DISTUTILS': 'stdlib'}
    ctx = remotion_runtime.probe_context(environment=env, python_executable=sys.executable)
    assert ctx['schema_env'] == {'HOME': str(tmp_path), 'LANG': ''}
    assert ctx['node_env'] == {'HOME': str(tmp_path), 'LANG': '', 'PATH': 'actual-path', 'TMPDIR': 'actual-tmp'}
    assert 'SECRET' not in json.dumps(ctx)


def test_launch_validation_missing_reference_does_not_spawn(tmp_path, monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail('ordinary readiness verification spawned a probe')
    monkeypatch.setattr(remotion_runtime, '_owned_probe', unexpected)
    with pytest.raises(remotion_runtime.RemotionValidationError, match='explicit owned validation'):
        remotion_runtime._validated_runtime(tmp_path, environment={'HOME': str(tmp_path)})


def test_owned_probe_positive_original_wait_precedes_final_census(monkeypatch, tmp_path):
    from astrid.core.execution import process_group
    events = []
    class Process:
        pid = 812
        returncode = 0
        _astrid_custody = object()
        def communicate(self, *, timeout):
            events.append(('communicate', timeout))
            return 'v20.19.4\n', ''
        def wait(self, *, timeout):
            events.append(('original_wait', timeout))
            return self.returncode
    process = Process()
    monkeypatch.setattr(process_group, 'popen_owned_group', lambda *a, **kw: process)
    monkeypatch.setattr(process_group, 'verify_tree_absent', lambda p: events.append(('final_census', p.pid)))
    result, receipt = remotion_runtime._owned_probe(['/exact/node', '--version'], cwd=tmp_path,
                                                   env={'HOME': str(tmp_path)}, timeout=5)
    assert result.returncode == 0
    assert receipt['original_wait_exit_code'] == 0 and receipt['custody_admitted']
    assert events == [('communicate', 5), ('original_wait', 0), ('final_census', 812)]

def test_owned_probe_timeout_preserves_primary_when_cleanup_refuses(monkeypatch, tmp_path):
    from astrid.core.execution import process_group
    monkeypatch.setattr(remotion_runtime, "_UNRESOLVED_PROBE_CUSTODY", [])
    original = subprocess.TimeoutExpired(['/exact/node', '--version'], 5)
    class Process:
        pid = 813
        returncode = None
        def communicate(self, *, timeout):
            raise original
    process = Process()
    monkeypatch.setattr(process_group, 'popen_owned_group', lambda *a, **kw: process)
    def refuse(*a, **kw):
        raise RuntimeError('strict retained cleanup refusal')
    monkeypatch.setattr(process_group, 'terminate_group', refuse)
    with pytest.raises(remotion_runtime.RemotionProbeCleanupError, match="timed out") as error:
        remotion_runtime._owned_probe(['/exact/node', '--version'], cwd=tmp_path,
                                      env={'HOME': str(tmp_path)}, timeout=5)
    assert error.value.process is process
    assert str(error.value.cleanup_error) == 'strict retained cleanup refusal'
    assert error.value.original_error is original
    assert error.value.__cause__ is error.value.cleanup_error
    assert original.process is process
    assert isinstance(original.cleanup_error, RuntimeError)

def test_owned_probe_empty_env_refuses_before_launch(monkeypatch, tmp_path):
    from astrid.core.execution import process_group
    monkeypatch.setattr(process_group, 'popen_owned_group', lambda *a, **kw: pytest.fail('ambient fallback'))
    with pytest.raises(ValueError, match='nonempty'):
        remotion_runtime._owned_probe(['/exact/node'], cwd=tmp_path, env={}, timeout=5)


def test_validation_failure_never_publishes_ready_artifact_and_preserves_receipt(tmp_path, monkeypatch):
    runtime, _, _, env, _, _ = _owned_validation_fixture(tmp_path, monkeypatch)
    # Separate launch; previously successful history is immutable.
    def failure(argv, **kwargs):
        receipt = {'original_wait_exit_code': 2, 'retained_set_final_census_complete': True,
                   'custody_admitted': True, 'original_child_pid': 901}
        return subprocess.CompletedProcess(argv, 2, '', 'invalid runtime'), receipt
    monkeypatch.setattr(remotion_runtime, '_owned_probe', failure)
    with pytest.raises(remotion_runtime.RemotionValidationError) as caught:
        remotion_runtime.prepare_remotion_validation(
            artifact_directory=tmp_path / 'failed', host_environment=env,
            python_executable=sys.executable, launch_id='b' * 32, contexts={'host': env})
    assert not list((tmp_path / 'failed').glob('remotion-validation-*.json'))
    history = json.loads(Path(caught.value.validation_evidence).read_text())
    assert history['original_probe_receipt']['original_wait_exit_code'] == 2
    assert history['status'] == 'FAILED_NOT_READINESS'


def test_parent_owned_reference_survives_worker_and_renderer_filters(tmp_path, monkeypatch):
    from astrid.core.rendering.transport import _build_environment
    from astrid.core.subprocess_env import SubprocessEnvPolicyError, build_child_subprocess_env
    runtime, _, _, env, ref, calls = _owned_validation_fixture(tmp_path, monkeypatch)
    parent = {**env, **ref}
    child = build_child_subprocess_env(base=parent, parent=parent)
    child['ASTRID_REMOTION_VALIDATION_ROLE'] = 'worker'
    monkeypatch.setattr(os, 'environ', child)
    renderer = _build_environment(None)
    assert all(renderer[k] == child[k] for k in remotion_runtime._REF_ENV_KEYS)
    remotion_runtime._validated_runtime(runtime, environment=renderer)
    with pytest.raises(SubprocessEnvPolicyError, match='parent-owned'):
        build_child_subprocess_env(parent=parent, explicit_env={'ASTRID_REMOTION_VALIDATION_PATH': '/task/foreign'})
    assert len(calls) == 3


@pytest.mark.parametrize('which', ['editable.pth', 'safe_finder.py', 'native_dep/extension.so',
                                  'native_dep/resources.json', 'sitecustomize.py', 'idna.py'])
def test_launch_validation_startup_import_and_optional_availability_invalidation(tmp_path, monkeypatch, which):
    runtime, _, _, env, ref, calls = _owned_validation_fixture(tmp_path, monkeypatch)
    (tmp_path / 'site-packages' / which).write_text('changed input')
    with pytest.raises(remotion_runtime.RemotionValidationError, match='inputs changed'):
        remotion_runtime._validated_runtime(runtime, environment={**env, **ref})
    assert len(calls) == 3


def test_launch_validation_missing_role_and_tampered_artifact_refuse_without_probe(tmp_path, monkeypatch):
    runtime, _, _, env, ref, calls = _owned_validation_fixture(tmp_path, monkeypatch)
    p = Path(ref['ASTRID_REMOTION_VALIDATION_PATH'])
    p.write_bytes(p.read_bytes() + b' ')
    with pytest.raises(remotion_runtime.RemotionValidationError, match='digest differs'):
        remotion_runtime._validated_runtime(runtime, environment={**env, **ref})
    assert len(calls) == 3



def test_launch_validation_explicitly_validates_startup_locale_transition(tmp_path, monkeypatch):
    def transform(closure, _count):
        closure['startup']['locale_env']['LC_CTYPE'] = 'UTF-8'
    runtime, _, _, environment, reference, calls = _owned_validation_fixture(
        tmp_path, monkeypatch, schema_result_transform=transform)
    assert len(calls) == 6
    assert calls[3][1]['LC_CTYPE'] == 'UTF-8'
    assert calls[4][1]['LC_CTYPE'] == 'UTF-8'
    entry = remotion_runtime._validated_runtime(
        runtime, environment={**environment, **reference, 'LC_CTYPE': 'UTF-8'})
    assert entry['closure']['startup']['locale_env']['LC_CTYPE'] == 'UTF-8'
    assert len(calls) == 6


@pytest.mark.parametrize('defect', ['second_transition', 'foreign_interpreter', 'changed_home'])
def test_launch_validation_refuses_unsupported_startup_transition(tmp_path, monkeypatch, defect):
    def transform(closure, count):
        closure['startup']['locale_env']['LC_CTYPE'] = 'UTF-8'
        if defect == 'changed_home':
            closure['startup']['locale_env']['HOME'] = str(tmp_path / 'foreign')
        elif count == 5 and defect == 'second_transition':
            closure['startup']['locale_env']['LC_CTYPE'] = 'C'
        elif count == 5 and defect == 'foreign_interpreter':
            closure['startup']['executable'] = '/bin/sh'
    with pytest.raises(remotion_runtime.RemotionValidationError):
        _owned_validation_fixture(tmp_path, monkeypatch, schema_result_transform=transform)
    assert not list((tmp_path / 'artifacts').glob('remotion-validation-*.json'))
    assert list((tmp_path / 'artifacts').glob('failed-validation-*.json'))


def test_launch_validation_dependency_change_during_qualifying_import_refuses_publication(tmp_path, monkeypatch):
    def mutate_after_discovery(_closure, count):
        if count == 3:
            (tmp_path / 'site-packages/native_dep/extension.so').write_text('replaced during import')
    with pytest.raises(remotion_runtime.RemotionValidationError, match='dependency closure changed'):
        _owned_validation_fixture(tmp_path, monkeypatch, schema_result_transform=mutate_after_discovery)
    assert not list((tmp_path / 'artifacts').glob('remotion-validation-*.json'))
    receipt = json.loads(next((tmp_path / 'artifacts').glob('failed-validation-*.json')).read_text())
    assert receipt['original_probe_receipt']['original_wait_exit_code'] == 0
    assert receipt['status'] == 'FAILED_NOT_READINESS'


def test_validation_acquisition_failure_retains_handle_and_blocks_replacement(tmp_path, monkeypatch):
    from astrid.core.execution import process_group
    owned_probe = remotion_runtime._owned_probe
    _, _, _, environment, _, _ = _owned_validation_fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(remotion_runtime, '_owned_probe', owned_probe)
    monkeypatch.setattr(remotion_runtime, '_UNRESOLVED_VALIDATION_PROBES', [])
    process = SimpleNamespace(pid=9881, returncode=None)
    primary = RuntimeError('initial acquisition refused with retained child')
    primary.process = process
    cleanup = RuntimeError('native cleanup remains uncertain')
    acquisitions = []
    def acquire(*_args, **_kwargs):
        acquisitions.append(process)
        raise primary
    def refuse_cleanup(*_args, **_kwargs):
        raise cleanup
    monkeypatch.setattr(process_group, 'popen_owned_group', acquire)
    monkeypatch.setattr(process_group, 'terminate_group', refuse_cleanup)
    arguments = dict(artifact_directory=tmp_path / 'failed-acquisition',
                     host_environment=environment, python_executable=sys.executable,
                     launch_id='c' * 32, contexts={'host': environment})
    with pytest.raises(RuntimeError) as failure:
        remotion_runtime.prepare_remotion_validation(**arguments)
    assert isinstance(failure.value, remotion_runtime.RemotionProbeCleanupError)
    assert failure.value.original_error is primary
    assert failure.value.__cause__ is cleanup
    assert primary.process is process and primary.cleanup_error is cleanup
    with pytest.raises(remotion_runtime.RemotionProbeCleanupError) as blocked:
        remotion_runtime.prepare_remotion_validation(**{**arguments, 'launch_id': 'd' * 32})
    assert blocked.value is failure.value
    assert acquisitions == [process]
    assert not list((tmp_path / 'failed-acquisition').glob('remotion-validation-*.json'))



@pytest.mark.parametrize('change', ['delete', 'replace'])
def test_launch_validation_loaded_bytecode_change_refuses_without_fallback(tmp_path, monkeypatch, change):
    runtime, _, _, environment, reference, calls = _owned_validation_fixture(tmp_path, monkeypatch)
    cache = tmp_path / 'site-packages/safe_finder.pyc'
    if change == 'delete':
        cache.unlink()
    else:
        cache.write_bytes(b'different loaded bytecode')
    with pytest.raises(remotion_runtime.RemotionValidationError):
        remotion_runtime._validated_runtime(runtime, environment={**environment, **reference})
    assert len(calls) == 3


def _registry_settling_fixture(tmp_path, monkeypatch):
    from astrid.packs.rendering.rendering.renderers.remotion import run, lock
    project = tmp_path / 'remotion'
    src = project / 'src'
    src.mkdir(parents=True)
    corpus = tmp_path / 'packs'
    corpus.mkdir()
    (corpus / 'pack.yaml').write_text('ordinary local corpus')
    state_file = tmp_path / 'managed-state.json'
    content = {kind: '// ' + kind for kind in remotion_runtime._REGISTRY_KINDS}
    state = {'version': 1, 'hash': 'a' * 64,
             'content_hashes': {kind: hashlib.sha256(raw.encode()).hexdigest() for kind, raw in content.items()}}
    observation = {'state': state, 'module_files': [], 'package_roots': [], 'search_paths': [],
                   'roots': [str(corpus)], 'optional_files': [str(state_file)],
                   'lock_path': str(lock._resolved_lock_path())}
    calls = []
    def probe(environment, python, **_kwargs):
        calls.append(dict(environment))
        return json.loads(json.dumps(observation)), {'original_wait_exit_code': 0,
            'retained_set_final_census_complete': True, 'custody_admitted': True}
    generation = []
    def owned(argv, *, cwd, env, timeout):
        assert lock.remotion_render_lock_held()
        token = env[run.remotion_lock.REMOTION_LOCK_TOKEN_ENV]
        assert token
        assert timeout == 90
        generation.append(dict(env))
        for kind, raw in content.items():
            (src / (kind + '.generated.ts')).write_text(raw)
        receipt = run.remotion_lock.REMOTION_LOCK_RECEIPT_PREFIX + token + "\n"
        return subprocess.CompletedProcess(argv, 0, receipt, ''), {'original_wait_exit_code': 0,
            'retained_set_final_census_complete': True, 'custody_admitted': True}
    monkeypatch.setattr(run, 'gen_effect_registry', SimpleNamespace())
    monkeypatch.setattr(remotion_runtime, '_registry_probe', probe)
    monkeypatch.setattr(remotion_runtime, '_owned_probe', owned)
    environment = {'HOME': str(tmp_path), 'PATH': '/usr/bin', 'LANG': 'C.UTF-8',
                   'ASTRID_TIMELINE_SCHEMA_PYTHONPATH': str(tmp_path / 'schema'),
                   'ASTRID_PACKS_PATH': str(corpus), 'ASTRID_TASK_RUN_ID': 'must-not-enter',
                   'ASTRID_RUNTIME_CREDENTIAL': 'must-not-enter'}
    return project, environment, observation, content, calls, generation, state_file


def test_registry_settling_owned_generator_four_file_readback_and_unchanged_skip(tmp_path, monkeypatch):
    from astrid.packs.rendering.rendering.renderers.remotion import run, lock
    project, environment, observation, content, calls, generation, _ = _registry_settling_fixture(tmp_path, monkeypatch)
    with lock.remotion_render_lock():
        result = remotion_runtime._settle_registries(project, {'host': environment, 'worker': environment}, sys.executable)
        assert len(generation) == 1 and len(calls) == 1
        contract = next(iter(result.values()))
        assert len(contract['outputs']) == 4
        assert contract['expected_state'] == observation['state']
        assert run._read_registry_state(project) == observation['state']
        assert run._registry_output_content_hashes(project) == observation['state']['content_hashes']
        stat_before = [(Path(p['path']).stat().st_mtime_ns, p) for p in contract['outputs']]
        remotion_runtime._settle_registries(project, {'host': environment}, sys.executable)
        assert len(generation) == 1
        assert [(Path(p['path']).stat().st_mtime_ns, p) for p in contract['outputs']] == stat_before
    assert not any('ASTRID_TASK_RUN_ID' in env or 'ASTRID_RUNTIME_CREDENTIAL' in env for env in (*calls, *generation))
    assert generation[0]['ASTRID_PACKS_PATH'] == environment['ASTRID_PACKS_PATH']


@pytest.mark.parametrize('change', ['new_manifest', 'state_appears', 'component_changed'])
def test_registry_qualifying_discovery_refuses_corpus_change(tmp_path, monkeypatch, change):
    from astrid.packs.rendering.rendering.renderers.remotion import lock
    project, environment, observation, _, calls, generation, state_path = _registry_settling_fixture(tmp_path, monkeypatch)
    count = 0
    def probe(env, python, **_kwargs):
        nonlocal count
        count += 1
        if count == 1:  # Mutation during the single owned probe trips the start-time fence.
            root = Path(observation['roots'][0])
            if change == 'state_appears':
                state_path.write_text('{}')
            elif change == 'new_manifest':
                (root / 'new').mkdir()
                (root / 'new/pack.yaml').write_text('new local pack')
            else:
                (root / 'pack.yaml').write_text('replaced component')
        return json.loads(json.dumps(observation)), {'original_wait_exit_code': 0,
            'retained_set_final_census_complete': True, 'custody_admitted': True}
    monkeypatch.setattr(remotion_runtime, '_registry_probe', probe)
    with lock.remotion_render_lock(), pytest.raises(remotion_runtime.RemotionValidationError, match='corpus changed'):
        remotion_runtime._settle_registries(project, {'host': environment}, sys.executable)
    assert generation == []


def test_registry_settling_requires_all_three_hashes_not_legacy_subset(tmp_path, monkeypatch):
    from astrid.packs.rendering.rendering.renderers.remotion import run, lock
    project, environment, observation, content, calls, generation, _ = _registry_settling_fixture(tmp_path, monkeypatch)
    for path, raw in zip(run._registry_output_paths(project), content.values()):
        path.write_text(raw)
    run._write_registry_state(project, {**observation['state'], 'content_hashes': {'effects': observation['state']['content_hashes']['effects']}})
    with lock.remotion_render_lock():
        remotion_runtime._settle_registries(project, {'host': environment}, sys.executable)
    assert len(generation) == 1
    assert run._read_registry_state(project) == observation['state']


def test_registry_settling_wheel_binds_provisioned_outputs_without_generator(tmp_path, monkeypatch):
    from astrid.packs.rendering.rendering.renderers.remotion import run, lock
    project, environment, _, content, calls, generation, _ = _registry_settling_fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(run, 'gen_effect_registry', None)
    for path, raw in zip(run._registry_output_paths(project), content.values()):
        path.write_text(raw)
    with lock.remotion_render_lock():
        result = remotion_runtime._settle_registries(project, {'host': environment}, sys.executable)
    assert next(iter(result.values()))['mode'] == 'provisioned_only'
    assert not (project / '.astrid-registry-state.json').exists()
    assert calls == generation == []


def test_registry_generator_custody_refusal_preserves_primary_and_no_state_publication(tmp_path, monkeypatch):
    from astrid.packs.rendering.rendering.renderers.remotion import lock
    project, environment, _, _, _, _, _ = _registry_settling_fixture(tmp_path, monkeypatch)
    refusal = remotion_runtime.RemotionValidationError('managed Git descendant capture refused')
    refusal.process = SimpleNamespace(pid=99)
    refusal.cleanup_error = RuntimeError('retained descendant unresolved')
    def refuse(*args, **kwargs):
        raise refusal
    monkeypatch.setattr(remotion_runtime, '_owned_probe', refuse)
    with lock.remotion_render_lock(), pytest.raises(remotion_runtime.RemotionValidationError) as failure:
        remotion_runtime._settle_registries(project, {'host': environment}, sys.executable)
    assert failure.value is refusal and failure.value.process.pid == 99
    assert not (project / '.astrid-registry-state.json').exists()


@pytest.mark.parametrize('control', sorted(remotion_runtime._UNSUPPORTED_PYTHON_STARTUP))
def test_launch_validation_projects_out_ambient_python_startup(tmp_path, control):
    clean = {'HOME': str(tmp_path)}
    ambient = {**clean, control: ''}
    assert remotion_runtime.probe_context(environment=ambient, python_executable=sys.executable) == remotion_runtime.probe_context(
        environment=clean, python_executable=sys.executable)
    with pytest.raises(remotion_runtime.RemotionValidationError, match=control):
        remotion_runtime._validate_remotion_worker_environment(ambient)


@pytest.mark.parametrize('section', ['include', 'includeIf "gitdir:/any/"'])
def test_registry_git_configuration_indirection_refuses(tmp_path, section):
    config = tmp_path / '.gitconfig'
    config.write_text('[' + section + ']\n path = ../external-config\n')
    with pytest.raises(remotion_runtime.RemotionValidationError, match='Git include indirection'):
        remotion_runtime._refuse_git_config_indirection([config])


@pytest.mark.parametrize('change', ['appears', 'replace'])
def test_registry_import_archive_and_child_cwd_binding(tmp_path, monkeypatch, change):
    child = tmp_path / 'child'
    child.mkdir()
    archive = child / 'imports.zip'
    if change == 'replace':
        archive.write_bytes(b'original zip')
    observation = {'module_files': [], 'package_roots': [], 'roots': [], 'optional_files': [],
                   'search_paths': [str(child), str(archive)], 'search_path_spellings': ['', 'imports.zip'],
                   'probe_cwd': str(child)}
    initial = remotion_runtime._registry_inputs(observation)
    elsewhere = tmp_path / 'consumer'
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    assert remotion_runtime._registry_inputs(observation) == initial
    archive.write_bytes(b'changed import archive')
    assert remotion_runtime._registry_inputs(observation) != initial


def test_schema_import_archive_absence_to_presence_refuses_without_fallback(tmp_path, monkeypatch):
    runtime, _, _, environment, reference, calls = _owned_validation_fixture(tmp_path, monkeypatch)
    artifact = Path(reference['ASTRID_REMOTION_VALIDATION_PATH'])
    payload = json.loads(artifact.read_text())
    archive = tmp_path / 'optional-imports.zip'
    # Production closure input supports absent/file paths, not just directories.
    closure = payload['contexts']['host']['variants'][0]['closure']
    closure['search_paths'].append(str(archive))
    before = remotion_runtime._runtime_inputs(runtime, environment, sys.executable, closure)
    archive.write_bytes(b'new importable archive')
    assert remotion_runtime._runtime_inputs(runtime, environment, sys.executable, closure) != before
    assert len(calls) == 3


def test_launch_validation_oversized_artifact_refuses_before_publication(tmp_path, monkeypatch):
    # First fixture proves the ordinary complete contract, then a separate
    # transaction exercises the exact same publisher with a smaller test limit.
    runtime, _, _, environment, reference, calls = _owned_validation_fixture(tmp_path, monkeypatch)
    Path(reference['ASTRID_REMOTION_VALIDATION_PATH']).unlink()
    monkeypatch.setattr(remotion_runtime, '_VALIDATION_MAX_BYTES', 1)
    with pytest.raises(remotion_runtime.RemotionValidationError, match='bounded format'):
        remotion_runtime.prepare_remotion_validation(artifact_directory=tmp_path / 'too-large',
            host_environment=environment, python_executable=sys.executable, launch_id='b' * 32,
            contexts={'host': environment})
    assert not list((tmp_path / 'too-large').glob('remotion-validation-*.json'))


@pytest.mark.parametrize('changed', [False, True])
def test_source_settled_locale_transition_preserves_shared_complete_state(tmp_path, monkeypatch, changed):
    from astrid.packs.rendering.rendering.renderers.remotion import lock
    project, environment, observation, _, _, _, _ = _registry_settling_fixture(tmp_path, monkeypatch)
    with lock.remotion_render_lock():
        first = remotion_runtime._settle_registries(project, {'host': environment}, sys.executable)
        expected = next(iter(first.values()))['expected_state']
        environment['LC_CTYPE'] = 'UTF-8'
        if changed:
            observation['state'] = {**observation['state'], 'hash': 'b' * 64}
            with pytest.raises(remotion_runtime.RemotionValidationError, match='different registry contents'):
                remotion_runtime._settle_registries(project, {'transition': environment}, sys.executable, expected_state=expected)
        else:
            second = remotion_runtime._settle_registries(project, {'transition': environment}, sys.executable, expected_state=expected)
            assert next(iter(second.values()))['expected_state'] == expected


def test_registry_probe_preserves_distinct_renderer_and_generator_startup(tmp_path, monkeypatch):
    from astrid.packs.rendering.rendering.renderers.remotion import run
    captured = []
    payload = {'runtime_origin': str(Path(remotion_runtime.__file__).resolve()),
               'startup': {'executable': sys.executable},
               'state': {'hash': 'a' * 64, 'content_hashes': {kind: 'b' * 64 for kind in remotion_runtime._REGISTRY_KINDS}}}
    def owned(argv, *, cwd, env, timeout):
        assert timeout == 90
        captured.append((argv, cwd))
        return subprocess.CompletedProcess(argv, 0, json.dumps(payload), ''), {'original_wait_exit_code': 0,
            'retained_set_final_census_complete': True, 'custody_admitted': True}
    monkeypatch.setattr(remotion_runtime, '_owned_probe', owned)
    environment = {'HOME': str(tmp_path)}
    remotion_runtime._registry_probe(environment, sys.executable)
    remotion_runtime._registry_probe(environment, sys.executable, generator=True)
    assert captured[0][1] == Path(run.__file__).resolve().parents[2]
    assert captured[0][0][-1] == str(Path(run.__file__).resolve().parents[2])
    assert captured[1][1] == remotion_runtime.REPO_ROOT
    assert captured[1][0][-1] == str(remotion_runtime.REPO_ROOT / 'scripts')
    for argv, cwd in captured:
        command_index = argv.index('-c')
        prefix = argv[command_index + 1].split('from astrid.core.rendering', 1)[0]
        for already_present in (False, True):
            root, script_dir = argv[command_index + 2:]
            original = ['', 'other'] + ([root] if already_present else [])
            monkeypatch.setattr(sys, 'path', list(original))
            monkeypatch.setattr(sys, 'argv', ['-c', root, script_dir])
            exec(prefix, {})
            expected = [script_dir, 'other', root] if already_present else [root, script_dir, 'other']
            assert sys.path == expected


def test_registry_readiness_qualifies_actual_canonical_transport_projection(tmp_path, monkeypatch):
    runtime, _, _, environment, reference, calls = _owned_validation_fixture(tmp_path, monkeypatch,
        environment_overrides={'PYTHONPATH': str(tmp_path / 'known-source'), 'XDG_CONFIG_HOME': str(tmp_path / 'known-config')})
    from astrid.core.subprocess_env import build_child_subprocess_env
    renderer = build_child_subprocess_env(base={**environment, **reference}, parent={**environment, **reference})
    assert 'PYTHONPATH' not in renderer and 'XDG_CONFIG_HOME' not in renderer
    remotion_runtime._validated_runtime(runtime, environment=renderer)
    assert len(calls) == 3  # Distinct registries share only identical isolated probe contexts.


@pytest.mark.parametrize('defect', ['include', 'include_if', 'include_if_tab', 'worktree_include', 'pointer', 'common_directory', 'symlink'])
def test_managed_git_indirection_refuses_before_verified_discovery(tmp_path, monkeypatch, defect):
    from astrid.core.pack import source_setup
    from astrid.packs.rendering.rendering.renderers.remotion import run
    checkout = tmp_path / 'managed'
    checkout.mkdir()
    git = checkout / '.git'
    if defect == 'pointer':
        git.write_text('gitdir: ../foreign-git\n')
    elif defect == 'symlink':
        foreign = tmp_path / 'foreign-git'
        foreign.mkdir()
        git.symlink_to(foreign, target_is_directory=True)
    else:
        git.mkdir()
        if defect == 'common_directory':
            (git / 'commondir').write_text('../common\n')
        elif defect == 'worktree_include':
            (git / 'config').write_text('[extensions]\n worktreeConfig = true\n')
            (git / 'config.worktree').write_text('[include]\n path = ../foreign-config\n')
        else:
            section = 'include' if defect == 'include' else ('includeIf\t"gitdir:/any/"' if defect == 'include_if_tab' else 'includeIf "gitdir:/any/"')
            (git / 'config').write_text('[' + section + ']\n path = ../foreign-config\n')
    calls = []
    def inventory(*, verify=True):
        calls.append(verify)
        assert not verify, 'verified inventory/Git must never be reached'
        return SimpleNamespace(sources=[SimpleNamespace(pack_root=str(checkout / 'packs' / 'one'), pack_subpath='packs/one')])
    monkeypatch.setattr(source_setup, 'active_source_inventory', inventory)
    monkeypatch.setattr(remotion_runtime, '_registry_config_files', lambda _environment: [])
    class NoGitAllowed:
        @staticmethod
        def compute_registry_state(**kwargs):
            pytest.fail('registry discovery may invoke Git and must not be reached')
    monkeypatch.setattr(run, 'gen_effect_registry', NoGitAllowed())
    with pytest.raises(remotion_runtime.RemotionValidationError, match='Git'):
        remotion_runtime._registry_observation()
    assert calls == [False]


def _six_registry_contexts(environment):
    root = Path(environment['HOME'])
    return [dict(environment),
            {**environment, 'ASTRID_PACKS_PATH': str(root / 'packs-a'), 'PYTHONPATH': str(root / 'python-a')},
            {**environment, 'ASTRID_PACKS_PATH': str(root / 'packs-a')},
            {**environment, 'PYTHONPATH': str(root / 'python-a')},
            {**environment, 'ASTRID_PACKS_PATH': str(root / 'packs-a'), 'PYTHONPATH': str(root / 'python-b')},
            {**environment, 'PYTHONPATH': str(root / 'python-b')}]


def test_launch_validation_shares_only_isolated_proof_preserving_six_registries(tmp_path, monkeypatch):
    inputs = remotion_runtime._runtime_inputs
    scans = []
    def observed(*args, **kwargs):
        result = inputs(*args, **kwargs)
        scans.append(result)
        return result
    monkeypatch.setattr(remotion_runtime, '_runtime_inputs', observed)
    runtime, _, _, environment, reference, calls = _owned_validation_fixture(
        tmp_path, monkeypatch, worker_contexts=_six_registry_contexts)
    artifact = json.loads(Path(reference['ASTRID_REMOTION_VALIDATION_PATH']).read_text())
    variants = artifact['contexts']['worker']['variants']
    assert len(variants) == 6 and len(calls) == 3
    assert len(scans) == 5  # Four qualifying scans + final fence; repeat contexts reuse the proof by key.
    assert len({json.dumps(v['context']['registry_context'], sort_keys=True) for v in variants}) == 6
    assert all(v['registry']['mode'] == 'provisioned_only' for v in variants)
    assert all(v['original_waits'] == variants[0]['original_waits'] for v in variants)
    for variant in variants:
        consumer = {**environment, **variant['context']['registry_context'], **reference,
                    'ASTRID_REMOTION_VALIDATION_ROLE': 'worker'}
        remotion_runtime._validated_runtime(runtime, environment=consumer)
    assert len(calls) == 3


@pytest.mark.parametrize('mutation_scan', [5])  # The final fresh fence before publication.
def test_launch_validation_shared_proof_rejects_between_variants_and_before_publish(tmp_path, monkeypatch, mutation_scan):
    inputs = remotion_runtime._runtime_inputs
    count = 0
    def mutate(*args, **kwargs):
        nonlocal count
        count += 1
        if count == mutation_scan:
            (tmp_path / 'site-packages/native_dep/extension.so').write_text('changed after qualifying proof')
        return inputs(*args, **kwargs)
    monkeypatch.setattr(remotion_runtime, '_runtime_inputs', mutate)
    with pytest.raises(remotion_runtime.RemotionValidationError, match='closure changed'):
        _owned_validation_fixture(tmp_path, monkeypatch, worker_contexts=_six_registry_contexts)
    assert not list((tmp_path / 'artifacts').glob('remotion-validation-*.json'))
    failure = json.loads(next((tmp_path / 'artifacts').glob('failed-validation-*.json')).read_text())
    assert failure['status'] == 'FAILED_NOT_READINESS'
    assert failure['original_probe_receipt']['original_wait_exit_code'] == 0


@pytest.mark.parametrize('key,value', [('LANG', 'C'), ('PATH', '/different'), ('TMPDIR', '/different')])
def test_launch_validation_separate_effective_contexts_have_fresh_owned_proofs(tmp_path, monkeypatch, key, value):
    _, _, _, _, reference, calls = _owned_validation_fixture(tmp_path, monkeypatch,
        worker_contexts=lambda environment: [{**environment, key: value}])
    artifact = json.loads(Path(reference['ASTRID_REMOTION_VALIDATION_PATH']).read_text())
    assert len(calls) == 6
    host = artifact['contexts']['host']['variants'][0]
    worker = artifact['contexts']['worker']['variants'][0]
    assert host['original_waits']['schema']['original_child_pid'] != worker['original_waits']['schema']['original_child_pid']


def test_launch_validation_proofs_never_cross_launch_transactions(tmp_path, monkeypatch):
    first = _owned_validation_fixture(tmp_path / 'first', monkeypatch)
    second = _owned_validation_fixture(tmp_path / 'second', monkeypatch)
    assert len(first[-1]) == 3 and len(second[-1]) == 3
    assert first[-2]['ASTRID_REMOTION_VALIDATION_PATH'] != second[-2]['ASTRID_REMOTION_VALIDATION_PATH']


def test_launch_validation_timing_metadata_contains_no_arguments_or_error_messages(capsys):
    @remotion_runtime._validation_timing('complete_inputs')
    def refuse(secret):
        raise RuntimeError(secret)
    with pytest.raises(RuntimeError, match='PRIVATE_VALUE'):
        refuse('PRIVATE_VALUE')
    raw = capsys.readouterr().err
    assert 'PRIVATE_VALUE' not in raw
    record = json.loads(raw)
    assert set(record) == {'diagnostic', 'phase', 'outcome', 'process_id', 'elapsed_ms'}
    assert record['outcome'] == 'failed' and record['elapsed_ms'] >= 0


def test_launch_validation_shared_proof_keeps_registry_output_mutation_rejection(tmp_path, monkeypatch):
    inputs = remotion_runtime._runtime_inputs
    scans = 0
    def mutate(*args, **kwargs):
        nonlocal scans
        scans += 1
        if scans == 5:
            from astrid.packs.rendering.rendering.renderers.remotion import run
            run._registry_output_paths(args[0])[0].write_text('// changed registry')
        return inputs(*args, **kwargs)
    monkeypatch.setattr(remotion_runtime, '_runtime_inputs', mutate)
    with pytest.raises(remotion_runtime.RemotionValidationError):
        _owned_validation_fixture(tmp_path, monkeypatch, worker_contexts=_six_registry_contexts)
    assert not list((tmp_path / 'artifacts').glob('remotion-validation-*.json'))


def test_launch_validation_changed_node_configuration_has_separate_proof(tmp_path, monkeypatch):
    def workers(environment):
        alternative = tmp_path / 'other-node'
        alternative.write_text('other configured Node')
        alternative.chmod(0o700)
        return [{**environment, 'ASTRID_NODE_EXECUTABLE': str(alternative)}]
    _, _, _, _, _, calls = _owned_validation_fixture(tmp_path, monkeypatch, worker_contexts=workers)
    assert len(calls) == 6
    assert calls[0][0][0] != calls[3][0][0]


def test_launch_validation_changed_schema_configuration_is_not_reused(tmp_path, monkeypatch):
    def workers(environment):
        import shutil
        alternative = tmp_path / 'other-schema'
        shutil.copyfile(environment['ASTRID_TIMELINE_SCHEMA_PYTHONPATH'], alternative)
        return [{**environment, 'ASTRID_TIMELINE_SCHEMA_PYTHONPATH': str(alternative)}]
    # The fixture probe still reports the original origin: a cached proof must
    # not turn this foreign origin into readiness for the second configured root.
    with pytest.raises(remotion_runtime.RemotionValidationError, match='invalid identity'):
        _owned_validation_fixture(tmp_path, monkeypatch, worker_contexts=workers)
    assert not list((tmp_path / 'artifacts').glob('remotion-validation-*.json'))


def test_launch_digest_memo_reuses_bytes_but_keeps_fences_and_fresh_consumers(tmp_path, monkeypatch):
    import time
    clock = time.time_ns
    monkeypatch.setattr(remotion_runtime.time, 'time_ns', lambda: clock() + 10_000_000_000)
    original_open = Path.open
    reads = []
    def observed_open(path, mode='r', *args, **kwargs):
        if mode == 'rb' and path.name == 'extension.so':
            reads.append(str(path))
        return original_open(path, mode, *args, **kwargs)
    monkeypatch.setattr(Path, 'open', observed_open)
    inputs = remotion_runtime._runtime_inputs
    scans = []
    def observed_inputs(*args, **kwargs):
        scans.append(kwargs.get('digest_memo'))
        return inputs(*args, **kwargs)
    monkeypatch.setattr(remotion_runtime, '_runtime_inputs', observed_inputs)
    runtime, _, _, env, ref, calls = _owned_validation_fixture(
        tmp_path, monkeypatch, worker_contexts=_six_registry_contexts)
    assert len(scans) == 5 and len(calls) == 3
    assert len(reads) == 1
    assert scans[0] is not None and all(m is scans[0] for m in scans)
    remotion_runtime._validated_runtime(runtime, environment={**env, **ref})
    assert len(reads) == 2 and scans[-1] is None


@pytest.mark.parametrize('mutation_scan', [5])  # The final fresh fence before publication.
@pytest.mark.parametrize('change', ['same_size_restored_mtime', 'add', 'remove', 'replace', 'chmod'])
def test_launch_digest_memo_rejects_changes_at_every_fence(tmp_path, monkeypatch, mutation_scan, change):
    import time
    clock = time.time_ns
    monkeypatch.setattr(remotion_runtime.time, 'time_ns', lambda: clock() + 10_000_000_000)
    inputs = remotion_runtime._runtime_inputs
    count = 0
    def mutate(*args, **kwargs):
        nonlocal count
        count += 1
        if count == mutation_scan:
            path = tmp_path / 'site-packages/native_dep/extension.so'
            old = path.stat()
            if change == 'same_size_restored_mtime':
                path.write_bytes(b'X' * old.st_size)
                os.utime(path, ns=(old.st_atime_ns, old.st_mtime_ns))
            elif change == 'add':
                path.with_name('new.so').write_bytes(b'new dependency')
            elif change == 'remove':
                path.unlink()
            elif change == 'replace':
                other = path.with_name('replacement')
                other.write_bytes(b'Y' * old.st_size)
                os.utime(other, ns=(old.st_atime_ns, old.st_mtime_ns))
                other.replace(path)
            else:
                path.chmod(0o700)
        return inputs(*args, **kwargs)
    monkeypatch.setattr(remotion_runtime, '_runtime_inputs', mutate)
    with pytest.raises(remotion_runtime.RemotionValidationError, match='closure changed'):
        _owned_validation_fixture(tmp_path, monkeypatch, worker_contexts=_six_registry_contexts)
    assert not list((tmp_path / 'artifacts').glob('remotion-validation-*.json'))


@pytest.mark.parametrize('clock_mode', ['recent', 'future', 'backward'])
def test_launch_digest_memo_conservative_timestamps(tmp_path, monkeypatch, clock_mode):
    path = tmp_path / 'input'
    path.write_bytes(b'original')
    now = max(path.stat().st_mtime_ns, path.stat().st_ctime_ns)
    ticks = [now + 10_000_000_000] if clock_mode == 'backward' else [now]
    if clock_mode == 'future':
        os.utime(path, ns=(now + 20_000_000_000, now + 20_000_000_000))
    monkeypatch.setattr(remotion_runtime.time, 'time_ns', lambda: ticks[0])
    original_open = Path.open
    reads = []
    def observed_open(p, mode='r', *args, **kwargs):
        if p == path and mode == 'rb': reads.append(p)
        return original_open(p, mode, *args, **kwargs)
    monkeypatch.setattr(Path, 'open', observed_open)
    memo = {}
    first = remotion_runtime._file_binding(path, digest_memo=memo)
    if clock_mode == 'backward': ticks[0] -= 1
    assert remotion_runtime._file_binding(path, digest_memo=memo) == first
    assert len(reads) == 2


def test_launch_digest_memo_rejects_mutation_during_read(tmp_path, monkeypatch):
    import contextlib
    path = tmp_path / 'input'
    path.write_bytes(b'original')
    original_open = Path.open
    @contextlib.contextmanager
    def changing_open(p, mode='r', *args, **kwargs):
        with original_open(p, mode, *args, **kwargs) as stream:
            yield stream
        if p == path and mode == 'rb':
            with original_open(p, 'wb') as stream: stream.write(b'modified')
    monkeypatch.setattr(Path, 'open', changing_open)
    memo = {}
    with pytest.raises(remotion_runtime.RemotionValidationError, match='changed during content binding'):
        remotion_runtime._file_binding(path, digest_memo=memo)
    assert not memo


@pytest.mark.parametrize("phase", ["registry_discovery", "registry_generator"])
@pytest.mark.parametrize("cleanup_refuses", [False, True])
def test_registry_probe90_timeout_is_not_proven_and_preserves_original_cleanup(monkeypatch, tmp_path, phase, cleanup_refuses):
    from astrid.core.execution import process_group
    primary = subprocess.TimeoutExpired(["/fixture/python", "registry"], 90)
    cleanup = RuntimeError("fixture retained cleanup refusal")
    events = []
    class Process:
        pid = 913
        returncode = None
        _astrid_custody = object()
        def communicate(self, *, timeout):
            events.append(("communicate", timeout))
            raise primary
        def wait(self, *, timeout):
            events.append(("original_wait", timeout));self.returncode = -15;return -15
    process = Process()
    monkeypatch.setattr(process_group, "popen_owned_group", lambda *a, **k: process)
    def terminate(original, *, grace_seconds):
        assert original is process;events.append(("terminate_original", grace_seconds))
        if cleanup_refuses:raise cleanup
    monkeypatch.setattr(process_group, "terminate_group", terminate)
    monkeypatch.setattr(process_group, "verify_tree_absent", lambda original: events.append(("final_census", original.pid)))
    if cleanup_refuses:
        with pytest.raises(remotion_runtime.RemotionProbeCleanupError) as caught:
            remotion_runtime._owned_registry_probe(["/fixture/python", "registry"], cwd=tmp_path,
                env={"HOME":str(tmp_path)}, phase=phase)
        assert caught.value.original_error is primary
        assert caught.value.process is process and caught.value.cleanup_error is cleanup
        assert caught.value.__cause__ is cleanup
        assert events == [("communicate", 90), ("terminate_original", 0.2)]
    else:
        with pytest.raises(remotion_runtime.RemotionValidationError, match="registry validation not proven") as caught:
            remotion_runtime._owned_registry_probe(["/fixture/python", "registry"], cwd=tmp_path,
                env={"HOME":str(tmp_path)}, phase=phase)
        assert caught.value.__cause__ is primary
        receipt = caught.value.probe_receipt
        assert receipt["outcome"] == "REGISTRY_TIMEOUT_NOT_PROVEN" and receipt["timeout_seconds"] == 90
        assert receipt["phase"] == phase and events[0] == ("communicate", 90)
        assert receipt["original_wait_exit_code"] == -15
        assert receipt["retained_set_final_census_complete"] is True
        assert events == [("communicate", 90), ("terminate_original", 0.2), ("original_wait", 1), ("final_census", 913)]


def test_registry_generator90_timeout_never_publishes_state(tmp_path, monkeypatch):
    from astrid.packs.rendering.rendering.renderers.remotion import lock
    project, environment, _, _, _, _, _ = _registry_settling_fixture(tmp_path, monkeypatch)
    primary = subprocess.TimeoutExpired(["/fixture/python", "generator"], 90)
    primary.probe_receipt = {"original_wait_exit_code": -15,
        "retained_set_final_census_complete": True, "custody_admitted": True}
    def timed_out(*args, **kwargs):
        assert kwargs["timeout"] == 90
        raise primary
    monkeypatch.setattr(remotion_runtime, "_owned_probe", timed_out)
    with lock.remotion_render_lock(), pytest.raises(remotion_runtime.RemotionValidationError, match="not proven") as caught:
        remotion_runtime._settle_registries(project, {"host": environment}, sys.executable)
    assert caught.value.__cause__ is primary
    assert caught.value.probe_receipt["outcome"] == "REGISTRY_TIMEOUT_NOT_PROVEN"
    assert not (project / ".astrid-registry-state.json").exists()
