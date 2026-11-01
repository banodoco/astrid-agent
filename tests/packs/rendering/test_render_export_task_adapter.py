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
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from astrid.core.foundation.hash import sha256_file
from astrid.core.rendering import remotion_runtime
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
    (runtime / "node_modules" / "@banodoco").mkdir(parents=True)
    for package in ("timeline-composition", "timeline-schema", "timeline-theme-2rp"):
        package_dir = runtime / "node_modules" / "@banodoco" / package
        package_dir.mkdir()
        package_dir.joinpath("package.json").write_text(
            json.dumps({"name": f"@banodoco/{package}", "version": "0.0.0"}),
            encoding="utf-8",
        )
    generated_src = runtime / "node_modules" / "@banodoco" / "timeline-composition" / "typescript" / "src"
    generated_src.mkdir(parents=True)
    generated_runtime_src = runtime / "src"
    generated_runtime_src.mkdir()
    for kind in ("effects", "animations", "transitions"):
        provisioned = Path(remotion_runtime.REPO_ROOT) / 'remotion/node_modules/@banodoco/timeline-composition/typescript/src' / f'{kind}.generated.ts'
        assert provisioned.is_file(), 'the pinned provisioned registry is required'
        (generated_src / f"{kind}.generated.ts").write_bytes(provisioned.read_bytes())
        for extension in (".ts", ".js", ".d.ts", ".js.map", ".d.ts.map"):
            (generated_runtime_src / f"{kind}.generated{extension}").write_text(
                "export {};\n", encoding="utf-8"
            )
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
    schema_spec = importlib.util.find_spec("banodoco_timeline_schema")
    if schema_spec is None or schema_spec.origin is None:
        pytest.fail("the pinned timeline-schema dependency is required for this integration test")
    schema_install = tmp_path / "installed-python"
    schema_package = schema_install / "banodoco_timeline_schema"
    shutil.copytree(Path(schema_spec.origin).parent, schema_package)
    # The clean readiness probe intentionally ignores ambient/user site
    # packages. Provision the small jsonschema dependency closure beside the
    # trusted schema so this test models an installed runtime rather than an
    # editable checkout.
    for dependency in (
        "jsonschema",
        "jsonschema_specifications",
        "referencing",
        "rpds",
        "attrs",
        "attr",
    ):
        dependency_spec = importlib.util.find_spec(dependency)
        if dependency_spec is None or dependency_spec.submodule_search_locations is None:
            pytest.fail(f"the pinned {dependency} dependency is required for this integration test")
        shutil.copytree(
            Path(next(iter(dependency_spec.submodule_search_locations))),
            schema_install / dependency,
        )
    monkeypatch.setenv(TIMELINE_SCHEMA_PYTHONPATH_ENV, str(schema_install))
    monkeypatch.setenv(
        "PYTHONPATH",
        os.pathsep.join(
            path for path in (str(schema_install), os.environ.get("PYTHONPATH")) if path
        ),
    )
    probe_env = os.environ.copy()
    probe_env.pop("PYTHONPATH", None)
    schema_origin = subprocess.check_output(
        [
            sys.executable,
            "-c",
            "import astrid.core.timeline.banodoco_schema; "
            "import banodoco_timeline_schema; "
            "print(banodoco_timeline_schema.__file__)",
        ],
        text=True,
        env=probe_env,
    ).strip()
    assert Path(schema_origin).resolve().is_relative_to(schema_package.resolve())
    import banodoco_timeline_schema as ambient_schema

    ambient_origin = Path(ambient_schema.__file__).resolve()
    assert not ambient_origin.is_relative_to(schema_package.resolve())
    (runtime / "package-lock.json").write_text('{"lockfileVersion":3}\n')
    monkeypatch.setattr(remotion_run, "gen_effect_registry", None)
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
    ("__init__.py", "generated.py", "materialize.py", "theme.py", "timeline.schema.json", "validate.py"),
)
def test_trusted_schema_readiness_requires_complete_install(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    missing_file: str,
) -> None:
    import banodoco_timeline_schema

    schema_install = tmp_path / "installed-python"
    schema_package = schema_install / "banodoco_timeline_schema"
    schema_package.mkdir(parents=True)
    source_package = Path(banodoco_timeline_schema.__file__).resolve().parent
    for source in source_package.iterdir():
        if source.name != missing_file and source.is_file():
            shutil.copy2(source, schema_package / source.name)
    runtime = tmp_path / "installed-remotion"
    (runtime / "node_modules" / "@banodoco").mkdir(parents=True)
    for package in ("timeline-composition", "timeline-schema", "timeline-theme-2rp"):
        package_dir = runtime / "node_modules" / "@banodoco" / package
        package_dir.mkdir()
        package_dir.joinpath("package.json").write_text("{}", encoding="utf-8")
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
    monkeypatch.setenv(TIMELINE_SCHEMA_PYTHONPATH_ENV, str(schema_install))
    monkeypatch.setenv("PATH", f"{fake_bin}:{os.environ['PATH']}")
    status = remotion_runtime_status()
    assert not status.available
    assert missing_file in (status.reason or "")


def _runtime_with_locked_packages(tmp_path: Path) -> Path:
    runtime = tmp_path / "runtime"
    packages_root = runtime / "node_modules" / "@banodoco"
    for package in ("timeline-composition", "timeline-schema", "timeline-theme-2rp"):
        package_dir = packages_root / package
        package_dir.mkdir(parents=True)
        (package_dir / "package.json").write_text("{}", encoding="utf-8")
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
    runtime = _runtime_with_locked_packages(tmp_path)
    cli = runtime / REMOTION_CLI_RELATIVE_PATH
    cli.parent.mkdir(parents=True)
    cli.write_text("// local CLI")
    trusted_node = tmp_path / "trusted-node"
    trusted_node.write_text("server-owned Node")
    trusted_node.chmod(0o700)
    monkeypatch.setenv(NODE_EXECUTABLE_ENV, str(trusted_node))
    monkeypatch.setenv("PATH", str(tmp_path / "hostile"))
    calls = []
    def owned(argv, **kwargs):
        calls.append(argv)
        assert kwargs["timeout"] == 5
        return subprocess.CompletedProcess(argv, 0, "v20.19.4\n", ""), {}
    monkeypatch.setattr(remotion_runtime, "_owned_probe", owned)
    tools, error = resolve_remotion_runtime_tools(runtime)
    assert error is None
    assert tools is not None
    assert tools.node_executable == trusted_node.resolve()
    assert tools.remotion_cli == cli.resolve()
    assert calls == [[str(trusted_node), "--version"]]


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
    assert original.process is process
    assert isinstance(original.cleanup_error, RuntimeError)

def test_owned_probe_empty_env_refuses_before_launch(monkeypatch, tmp_path):
    from astrid.core.execution import process_group
    monkeypatch.setattr(process_group, 'popen_owned_group', lambda *a, **kw: pytest.fail('ambient fallback'))
    with pytest.raises(ValueError, match='nonempty'):
        remotion_runtime._owned_probe(['/exact/node'], cwd=tmp_path, env={}, timeout=5)
