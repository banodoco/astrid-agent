"""Exercise the real managed Qwen child using non-editable installed packages."""
from __future__ import annotations

import json
import os
import struct
import subprocess
import sysconfig
import textwrap
import venv
from pathlib import Path

from astrid.core.generation.vibecomfy_dependency import VIBECOMFY_ENGINE_REVISION
from tests.fixtures.comfyui_protocol.server import ComfyUIProtocolServer

ROOT = Path(__file__).resolve().parents[3]
ENTRY = "qwen-image-2.1-turbo"
TEMPLATE = "image/qwen_image_2_1_turbo"

CHILD = textwrap.dedent("""\
    import importlib
    import json
    import sys
    from pathlib import Path

    import astrid
    import vibecomfy
    from astrid.core.generation import entries
    from astrid.core.model_catalog.registry import ModelRegistry

    url, work, entry_id, expected_template = sys.argv[1:]
    work = Path(work)
    prefix = Path(sys.prefix).resolve()
    assert Path(vibecomfy.__file__).resolve().is_relative_to(prefix), vibecomfy.__file__
    assert Path(astrid.__file__).resolve().is_relative_to(prefix), astrid.__file__
    model = ModelRegistry.load_default().get(entry_id)
    template = model.modes['t2i'].backends['local'].template
    assert template == expected_template, template
    # These rows come from the shipped ready inventory, independently of the
    # catalog row that already worked when installed entry discovery was broken.
    ready = entries.ready_entries()
    row = next((row for row in ready if row.get('registry_id') == entry_id
                and row['id'] == template), None)
    assert row is not None, f'{entry_id} absent from installed ready entries'
    assert 't2i' in row['modes'], row
    workflow = work / 'qwen.json'
    workflow.write_text(json.dumps({'template_id': template, 'bindings': {}}), encoding='utf-8')
    action = importlib.import_module('astrid.packs.vibecomfy.actions.run.run')
    code = action.main([
        'run', str(workflow), '--comfy-url', url, '--out', str(work / 'outputs'),
        '--task-identity', 'installed-task', '--attempt-identity', 'installed-attempt',
        '--workflow-inputs', json.dumps({'prompt': 'a copper teapot on a blue table',
                                        'width': 512, 'height': 512, 'seed': 42}),
    ])
    if code == 0:
        (work / 'installed-probe.json').write_text(json.dumps({
            'vibecomfy_file': str(Path(vibecomfy.__file__).resolve()),
            'astrid_file': str(Path(astrid.__file__).resolve()),
            'ready_entry': row,
        }), encoding='utf-8')
    raise SystemExit(code)
""")


def test_installed_qwen_managed_child_path(tmp_path: Path) -> None:
    source_value = os.environ.get("VIBECOMFY_SOURCE")
    assert source_value, "VIBECOMFY_SOURCE must name the pinned VibeComfy checkout"
    source = Path(source_value).resolve()
    revision = subprocess.run(
        ["git", "-C", str(source), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=False,
    )
    assert revision.returncode == 0, revision.stdout + revision.stderr
    assert revision.stdout.strip() == VIBECOMFY_ENGINE_REVISION, (
        f"VIBECOMFY_SOURCE is {revision.stdout.strip()}, expected {VIBECOMFY_ENGINE_REVISION}"
    )

    environment = tmp_path / "installed"
    venv.EnvBuilder(with_pip=True).create(environment)
    scripts = Path(sysconfig.get_path("scripts", vars={"base": str(environment), "platbase": str(environment)}))
    python = scripts / ("python.exe" if os.name == "nt" else "python")
    assert python.is_file(), python
    empty = tmp_path / "empty"
    empty.mkdir()
    home = tmp_path / "home"
    temporary = tmp_path / "tmp"
    home.mkdir(exist_ok=True)
    temporary.mkdir(exist_ok=True)
    clean_env = {key: os.environ[key] for key in (
        "PATH", "SYSTEMROOT", "WINDIR", "SSL_CERT_FILE", "SSL_CERT_DIR",
    ) if key in os.environ}
    clean_env.update(
        HOME=str(home), USERPROFILE=str(home), TMPDIR=str(temporary),
        TEMP=str(temporary), TMP=str(temporary),
        XDG_CONFIG_HOME=str(tmp_path / "config"), XDG_DATA_HOME=str(tmp_path / "data"),
        XDG_STATE_HOME=str(tmp_path / "state"), XDG_CACHE_HOME=str(tmp_path / "cache"),
        PIP_DISABLE_PIP_VERSION_CHECK="1", PIP_NO_CACHE_DIR="1", PIP_CONFIG_FILE=os.devnull,
        ASTRID_INTERNAL_INVOCATION="1", VIBECOMFY_HEADLESS="1", HF_HUB_OFFLINE="1",
    )
    assert "PYTHONPATH" not in clean_env

    def run(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(python), "-I", *args], cwd=empty, env=clean_env,
            capture_output=True, text=True, timeout=600, check=False,
        )

    # No editable installs or --no-deps: this is the supported installed tuple,
    # with Runtime supplied by the checkout's real lock/install script.
    for project in (source, ROOT):
        result = run("-m", "pip", "install", "--no-input", str(project))
        assert result.returncode == 0, result.stdout + result.stderr
    runtime = run(str(ROOT / "scripts" / "ci_install_runtime.py"))
    assert runtime.returncode == 0, runtime.stdout + runtime.stderr

    with ComfyUIProtocolServer() as comfy:
        work = tmp_path / "success"
        work.mkdir()
        result = run("-c", CHILD, comfy.url, str(work), ENTRY, TEMPLATE)
        assert result.returncode == 0, result.stdout + result.stderr
        probe = json.loads((work / "installed-probe.json").read_text(encoding="utf-8"))
        assert Path(probe["vibecomfy_file"]).is_relative_to(environment)
        assert Path(probe["astrid_file"]).is_relative_to(environment)
        assert probe["ready_entry"]["registry_id"] == ENTRY
        prompts = [request for request in comfy.requests
                   if request.method == "POST" and request.path == "/prompt"]
        assert len(prompts) == 1, comfy.requests
        graph = prompts[0].body["prompt"]
        classes = {node["class_type"] for node in graph.values()}
        assert classes == set(comfy.object_info)
        assert {"SaveImage", "UNETLoader", "CLIPLoader", "VAELoader",
                "TextEncodeQwenImage21", "QwenImage21Cache"} <= classes
        latent = next(node for node in graph.values() if node["class_type"] == "EmptyLatentImage")
        assert latent["inputs"]["width"] == latent["inputs"]["height"] == 512
        for class_type, field, expected in (
            ("UNETLoader", "unet_name", "qwen_image_2.1_turbo_int8_convrot.safetensors"),
            ("CLIPLoader", "clip_name", "qwen3vl_8b_int8_convrot.safetensors"),
            ("VAELoader", "vae_name", "qwen_image_2.1_vae_bf16.safetensors"),
        ):
            loader = next(node for node in graph.values() if node["class_type"] == class_type)
            assert loader["inputs"][field] == expected
        output = work / "outputs"
        # The private canonical rewrite and settled bytes prove preparation and
        # /history + /view ran, beyond merely reaching the submit endpoint.
        assert (output / "local-server-workflow" / "workflow.py").is_file()
        manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
        assert manifest["managed_generation_result"]["outcomes"]["execution"]["status"] == "succeeded"
        image = next(row for row in manifest["outputs"] if row["role"] == "result")
        assert (output / image["path"]).read_bytes() == comfy.png
        assert comfy.png[:8] == b"\x89PNG\r\n\x1a\n"
        assert struct.unpack(">II", comfy.png[16:24]) == (512, 512)
        assert any(request.path.startswith("/history/") for request in comfy.requests)
        assert any(request.path.startswith("/view?") for request in comfy.requests)

        comfy.fail_object_info = True
        failure_start = len(comfy.requests)
        failed_work = tmp_path / "failure"
        failed_work.mkdir()
        failed = run("-c", CHILD, comfy.url, str(failed_work), ENTRY, TEMPLATE)
        assert failed.returncode == 1, failed.stdout + failed.stderr
        assert "vibecomfy.run:" in failed.stderr
        assert "Traceback (most recent call last)" in failed.stderr
        assert "ValueError" in failed.stderr
        assert "HTTPError" in failed.stderr  # original HTTP 500 remains chained
        assert "not ready" in failed.stderr  # the engine probe refuses before any submit
        failure_requests = comfy.requests[failure_start:]
        assert any(request.path == "/object_info" for request in failure_requests)
        assert not any(request.method == "POST" for request in failure_requests)
        assert sum(request.method == "POST" and request.path == "/prompt"
                   for request in comfy.requests) == 1
