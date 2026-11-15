from __future__ import annotations

import json
import os
import platform
import shlex
import subprocess
import sys
import time
from pathlib import Path

import pytest

from astrid.core.generation import engine_runner
from astrid.core.generation.engine_contracts import CommandFailed, CommandResult
from astrid.core.generation.engine_execution import LocalLinuxExecutionContext
from astrid.core.generation.engine_platforms import platform_adapter
from astrid.core.generation.engine_recipe import load_recipe


SENTINEL = "PINS_A6_CPU_CLOSURE_STOP"


class _EnvironmentComplete(CommandFailed):
    """Private test signal for the exact post-environment sentinel result."""


class _ProcessClient:
    """Run install subprocesses locally while the production context owns files and helpers."""

    def __init__(self, cache: Path, sentinel: Path, venv: str):
        self.cache = cache
        self.sentinel = sentinel
        self.venv = venv
        self.commands = []

    def run(self, request):
        env = dict(os.environ)
        env.update(request.env)
        env["HF_HUB_OFFLINE"] = "1"
        env["CUDA_VISIBLE_DEVICES"] = ""
        env["UV_CACHE_DIR"] = str(self.cache)
        try:
            result = subprocess.run(request.argv, cwd=request.cwd, env=env, text=True,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    timeout=max(1, request.deadline - time.monotonic()), check=False)
            self.commands.append((tuple(request.argv), result.returncode, result.stdout, result.stderr))
            if (len(request.argv) >= 2 and request.argv[1] == str(self.sentinel)
                    and request.argv[-2:] == ("--venv", self.venv)
                    and result.returncode == 73 and result.stderr.strip() == SENTINEL):
                raise _EnvironmentComplete("environment completed", step_id="sanity")
            return CommandResult(result.returncode, result.stdout, result.stderr, False,
                                 0.0, None, False, False)
        except subprocess.TimeoutExpired as exc:
            return CommandResult(None, exc.stdout or "", exc.stderr or "", True,
                                 0.0, None, False, False)


@pytest.mark.opt_in
@pytest.mark.timeout(3600)
def test_linux_x86_64_h3_environment_closure_before_model_stages(tmp_path):
    assert sys.platform == "linux" and platform.machine().lower() in {"x86_64", "amd64"}, (
        "the real H3 CPU closure requires a Linux x86_64 worker"
    )

    recipe = load_recipe("comfyui", "linux-x86_64-cu130")
    adapter = platform_adapter(recipe.platform)
    program_root = tmp_path / "program"
    data_root = tmp_path / "data"
    engine_root = tmp_path / "engine"
    program_root.mkdir()
    data_root.mkdir()
    engine_root.mkdir()
    # Keep each install/build's uv state inside the fresh run-local data root.
    uv_cache = data_root / "uv-cache"
    assert not uv_cache.exists(), "the closure install must begin with a fresh run-local uv cache"

    stop = program_root / "stop-after-environment.py"
    stop.write_text(
        "import sys\n"
        "assert '--venv' in sys.argv\n"
        f"print('{SENTINEL}', file=sys.stderr)\n"
        "raise SystemExit(73)\n",
        encoding="utf-8",
    )
    primary = next(e for e in recipe.environments if e.name == recipe.primary_environment)
    # The runner appends ``--venv <primary venv directory>`` to the sanity command
    # (engine_runner._Runner.sanity); the sentinel matcher must compare that exact value.
    venv_dir = adapter.join(str(engine_root), primary.relative_path)
    python = adapter.venv_python(venv_dir)
    client = _ProcessClient(uv_cache, stop, venv_dir)
    context = LocalLinuxExecutionContext(engine="comfyui", platform=recipe.platform,
                                         root=str(engine_root), adapter=adapter,
                                         service_client=client)
    plan = engine_runner.plan_install(recipe, root=str(engine_root), placement="local")
    model_calls = []
    def reject_model(*args, **kwargs):
        model_calls.append((args, kwargs))
        raise AssertionError("model download reached before environment closure")
    context.download_model = reject_model

    with pytest.raises(_EnvironmentComplete) as caught:
        engine_runner.install_engine(
            plan, context=context, adapter=adapter, deadline=time.monotonic() + 3600,
            sanity_cmd=shlex.join((sys.executable, str(stop))),
        )
    assert caught.value.step_id == "sanity"
    assert len([row for row in client.commands if row[1] == 73 and row[3].strip() == SENTINEL]) == 1
    assert not model_calls

    receipt = json.loads((engine_root / ".astrid-engine/install.json").read_text())
    completed = [step["id"] for step in receipt["steps"] if step["validation"] == "passed"]
    assert "environment:comfy" in completed
    assert not any(step.startswith("model:") for step in completed)
    assert not any(step["kind"] == "model" for step in receipt["steps"]), (
        "the installer must stop before starting any model stage"
    )
    assert receipt["status"] == "failed"

    source_paths = {source.name: str(engine_root / source.relative_path) for source in recipe.sources}
    probe = r'''
import importlib.metadata as md, json, pathlib, sys
engine, source_paths = pathlib.Path(sys.argv[1]), json.loads(sys.argv[2])
sys.argv = ["pins-closure", "--cpu"]
for path in source_paths.values():
    sys.path.insert(0, path)
comfy = engine / "runtime/ComfyUI"
sys.path.insert(0, str(comfy))
import comfy.cli_args
comfy.cli_args.args.cpu = True
required = {
    "comfyui-hunyuanvideo-foley": {"HunyuanFoleyModelLoader", "HunyuanFoleySampler",
                                  "DACHunyuanVAEDecode", "LoadDACHunyuanVAE"},
    "comfyui-qwentts": {"AILab_Qwen3TTSCustomVoice"},
}
import importlib.util
for index, (name, expected) in enumerate(required.items()):
    package = pathlib.Path(source_paths[name])
    init = package / "__init__.py"
    spec = importlib.util.spec_from_file_location(
        f"pins_cpu_node_{index}", init, submodule_search_locations=[str(package)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    registered = set(getattr(module, "NODE_CLASS_MAPPINGS", {}))
    entrypoint = getattr(module, "comfy_entrypoint", None)
    if entrypoint is not None:
        # ComfyUI V3 extension API: the package registers its nodes through comfy_entrypoint().
        import asyncio
        extension = asyncio.run(entrypoint())
        for node in asyncio.run(extension.get_node_list()):
            registered.add(node.define_schema().node_id)
    assert expected <= registered, f"{name} missing nodes: {sorted(expected - registered)}"
expected = {"openai-whisper": "20250625", "argbind": "0.3.9", "randomname": "0.2.1"}
for distribution, version in expected.items():
    metadata = md.distribution(distribution)
    assert metadata.version == version
    wheel_text = metadata.read_text("WHEEL")
    assert wheel_text is not None, f"{distribution} has no wheel metadata"
    assert "Root-Is-Purelib: true" in wheel_text, distribution
    assert "Tag: py3-none-any" in wheel_text, distribution
    assert "Generator: setuptools (84.0.0)" in wheel_text, distribution
import whisper, argbind, randomname
assert whisper.__file__ and argbind.__file__ and randomname.__file__
assert md.version("setuptools") == "84.0.0"
'''
    result = subprocess.run(
        [python, "-c", probe, str(engine_root), json.dumps(source_paths)],
        cwd=str(engine_root), env={**os.environ, "HF_HUB_OFFLINE": "1",
                                   "CUDA_VISIBLE_DEVICES": "",
                                   "UV_CACHE_DIR": str(uv_cache)},
        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=300, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
