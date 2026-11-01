"""Exercise product imports, readiness and runtime behavior from installed wheels."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from tests.helpers.runtime import locate_runtime

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path("/home/user/astrid/orch/tasks/GPU-SHAKEDOWN/out/live/comfy-contract")
UV = shutil.which("uv")


def _clean_env(tmp_path: Path) -> dict[str, str]:
    env = {
        key: value for key, value in os.environ.items()
        if key in {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "SSL_CERT_FILE", "SSL_CERT_DIR"}
        and not key.startswith(("ASTRID_", "VIBECOMFY_"))
    }
    home = tmp_path / "home"
    env.update(
        HOME=str(home), USERPROFILE=str(home),
        XDG_CONFIG_HOME=str(tmp_path / "config"),
        XDG_DATA_HOME=str(tmp_path / "data"),
        XDG_STATE_HOME=str(tmp_path / "state"),
        XDG_CACHE_HOME=str(tmp_path / "cache"),
        UV_CACHE_DIR=str(tmp_path / "uv-cache"),
        PIP_DISABLE_PIP_VERSION_CHECK="1", PIP_NO_CACHE_DIR="1",
        PYTHONDONTWRITEBYTECODE="1",
    )
    return env


def _run(args: list[str], *, cwd: Path, env: dict[str, str], timeout: int = 600) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout)


def _build_local_wheel(source: Path, dest: Path, *, cwd: Path, env: dict[str, str]) -> Path:
    built = _run([UV, "build", "--wheel", "--out-dir", str(dest), str(source)], cwd=cwd, env=env)
    assert built.returncode == 0, built.stdout + built.stderr
    wheels = list(dest.glob("*.whl"))
    assert len(wheels) == 1, f"expected one wheel from {source}, found {wheels}"
    return wheels[0]


@pytest.mark.installed_smoke
def test_fresh_wheel_cli_and_installed_production_imports(tmp_path: Path) -> None:
    assert importlib.util.find_spec("build") is not None
    assert importlib.util.find_spec("wheel") is not None
    assert UV is not None, "the installed smoke requires uv to create an unseeded product venv"
    runtime = locate_runtime()
    assert runtime is not None, "installed smoke needs the pinned Runtime checkout (ASTRID_RUNTIME_CHECKOUT)"
    lifecycle = Path(os.environ["ASTRID_LIFECYCLE_CHECKOUT"])
    sdk = Path(os.environ["ASTRID_SDK_CHECKOUT"])
    vibe = Path(os.environ["ASTRID_VIBECOMFY_CHECKOUT"])
    assert (lifecycle / "pyproject.toml").is_file()
    assert (sdk / "pyproject.toml").is_file()
    assert (vibe / "pyproject.toml").is_file()
    assert shutil.disk_usage(tmp_path).free >= 2 * 1024**3, "less than 2 GiB free"

    env = _clean_env(tmp_path)
    dist = tmp_path / "dist"
    dist.mkdir()

    # Match the existing installed wheel smoke's isolated Agent/Runtime build.
    agent_source = tmp_path / "agent"
    agent_source.mkdir()
    for name in ("pyproject.toml", "LICENSE", "NOTICE"):
        shutil.copy2(ROOT / name, agent_source / name)
    for name in ("astrid", "astrid_runtime_client"):
        shutil.copytree(ROOT / name, agent_source / name,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    runtime_source = tmp_path / "runtime"
    runtime_source.mkdir()
    shutil.copy2(runtime / "pyproject.toml", runtime_source / "pyproject.toml")
    for name in ("runtime_protocol", "astrid_runtime"):
        shutil.copytree(runtime / name, runtime_source / name,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    for source in (agent_source, runtime_source):
        built = _run([sys.executable, "-m", "build", "--wheel", "--no-isolation",
                      "--outdir", str(dist), str(source)], cwd=tmp_path, env=env)
        assert built.returncode == 0, built.stdout + built.stderr

    # Clone only the pinned source trees into pytest's scratch space so wheel
    # building cannot add files to the shared dependency checkouts.
    local_wheels = {}
    for name, source in (("lifecycle", lifecycle), ("sdk", sdk)):
        clone = tmp_path / f"{name}-source"
        cloned = _run(["git", "clone", "--shared", "--no-checkout", str(source), str(clone)],
                      cwd=tmp_path, env=env)
        assert cloned.returncode == 0, cloned.stdout + cloned.stderr
        checkout = _run(["git", "-C", str(clone), "checkout", "--detach", "HEAD"],
                        cwd=tmp_path, env=env)
        assert checkout.returncode == 0, checkout.stdout + checkout.stderr
        local_dist = tmp_path / f"{name}-dist"
        local_dist.mkdir()
        local_wheels[name] = _build_local_wheel(clone, local_dist, cwd=tmp_path, env=env)

    wheels = list(dist.glob("*.whl"))
    assert len(wheels) == 2
    wheels.extend(local_wheels.values())

    # uv creates this venv without pip; uv installs the built product wheels.
    product_env = tmp_path / "product-venv"
    created = _run([UV, "venv", "--no-project", "--python", sys.executable, str(product_env)],
                   cwd=tmp_path, env=env)
    assert created.returncode == 0, created.stdout + created.stderr
    scripts = product_env / ("Scripts" if os.name == "nt" else "bin")
    python = scripts / ("python.exe" if os.name == "nt" else "python")
    no_pip = _run([str(python), "-I", "-c",
                   "import importlib.util; assert importlib.util.find_spec('pip') is None"],
                  cwd=tmp_path, env=env)
    assert no_pip.returncode == 0, no_pip.stdout + no_pip.stderr
    installed = _run([UV, "pip", "install", "--python", str(python), *(str(w) for w in wheels)],
                     cwd=tmp_path, env=env)
    assert installed.returncode == 0, installed.stdout + installed.stderr

    imports = _run([str(python), "-I", "-c", textwrap.dedent("""
        import importlib.util
        import pathlib
        import sys
        import astrid, astrid_runtime_client, runpod, runpod_lifecycle
        prefix = pathlib.Path(sys.prefix).resolve()
        for module in (astrid, astrid_runtime_client, runpod, runpod_lifecycle):
            pathlib.Path(module.__file__).resolve().relative_to(prefix)
        assert importlib.util.find_spec('vibecomfy') is None
        assert importlib.util.find_spec('pip') is None
    """)], cwd=tmp_path, env=env)
    assert imports.returncode == 0, imports.stdout + imports.stderr

    # The no-pip environment can execute the installed product CLI operation.
    cli = scripts / ("astrid" + (".exe" if os.name == "nt" else ""))
    result = _run([str(cli), "generate", "image", "a red boat", "--entry", "z-image",
                   "--mode", "t2i", "--target", "managed", "--dry-run", "--json"],
                  cwd=tmp_path, env=env)
    assert result.returncode == 2, result.stdout + result.stderr
    envelope = json.loads(result.stdout)
    assert envelope["status"] == "blocked"
    assert envelope["issues"][0]["code"] == "engine_not_found"
    assert envelope["issues"][0]["details"]["engine_code"] == "COMFY_TARGET_NOT_READY"

    # Exercise the installed no-client readiness branch against a registered,
    # healthy managed engine witness. No VibeComfy module exists in this venv.
    readiness = _run([str(python), "-I", "-c", textwrap.dedent("""
        import importlib.util
        from types import SimpleNamespace
        assert importlib.util.find_spec('vibecomfy') is None
        from astrid.core.generation import engine_local_endpoint, preflight
        from astrid.core.model_catalog.registry import ModelRegistry
        engine_local_endpoint.recorded_engine_root = lambda engine: '/healthy/managed/comfyui'
        engine_local_endpoint.local_engine_endpoint = lambda engine, *, root: SimpleNamespace(
            endpoint=SimpleNamespace(
                url='http://127.0.0.1:8188',
                health=SimpleNamespace(object_info_sha256='a' * 64,
                                       checked_at='2026-11-01T00:00:00Z')))
        preflight._module_available = lambda name: (_ for _ in ()).throw(
            AssertionError('readiness consulted Agent-env optional module ' + name))
        preflight._comfyui_runtime_available = lambda: (_ for _ in ()).throw(
            AssertionError('readiness consulted the legacy local Comfy runtime'))
        entry = ModelRegistry.load_default().get('z-image')
        client = preflight.select_generation_target('managed')
        preflight.require_generation_target_readiness(entry, 't2i', client)
        assert client.target.kind == 'managed'
        managed_only = preflight.check_local_generation_readiness(
            entry, 't2i', require_managed_runtime=True, legacy_local=False)
        assert managed_only.ready, managed_only.reason
    """)], cwd=tmp_path, env=env)
    assert readiness.returncode == 0, readiness.stdout + readiness.stderr


@pytest.mark.installed_smoke
def test_noneditable_vibecomfy_run_sync_replays_live_contract(tmp_path: Path) -> None:
    assert UV is not None, "the installed smoke requires uv"
    vibe = Path(os.environ["ASTRID_VIBECOMFY_CHECKOUT"])
    assert subprocess.run(["git", "-C", str(vibe), "rev-parse", "--short=8", "HEAD"],
                          check=True, capture_output=True, text=True).stdout.strip() == "8cf4c758"
    assert FIXTURES.is_dir(), f"live contract fixtures are unavailable: {FIXTURES}"
    env = _clean_env(tmp_path)

    # Build from an isolated source copy. The live fixture directory is passed
    # by path and read in place; fixture contents and any media are not copied.
    source = tmp_path / "vibecomfy-source"
    shutil.copytree(vibe, source, ignore=shutil.ignore_patterns(
        ".git", "__pycache__", "*.pyc", ".venv", "node_modules", "build", "dist"))
    dist = tmp_path / "vibecomfy-dist"
    dist.mkdir()
    wheel = _build_local_wheel(source, dist, cwd=tmp_path, env=env)
    installed_env = tmp_path / "vibecomfy-wheel-venv"
    created = _run([UV, "venv", "--no-project", "--python", sys.executable, str(installed_env)],
                   cwd=tmp_path, env=env)
    assert created.returncode == 0, created.stdout + created.stderr
    python = installed_env / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    no_pip = _run([str(python), "-I", "-c",
                   "import importlib.util; assert importlib.util.find_spec('pip') is None"],
                  cwd=tmp_path, env=env)
    assert no_pip.returncode == 0, no_pip.stdout + no_pip.stderr
    install = _run([UV, "pip", "install", "--python", str(python), str(wheel)],
                   cwd=tmp_path, env=env)
    assert install.returncode == 0, install.stdout + install.stderr

    # Install the Agent client wheel too, without importing either source
    # checkout. This is the production parser used for Comfy output routing.
    agent_source = tmp_path / "agent-source"
    agent_source.mkdir()
    for name in ("pyproject.toml", "LICENSE", "NOTICE"):
        shutil.copy2(ROOT / name, agent_source / name)
    shutil.copytree(ROOT / "astrid", agent_source / "astrid",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    shutil.copytree(ROOT / "astrid_runtime_client", agent_source / "astrid_runtime_client",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    agent_dist = tmp_path / "agent-dist"
    agent_dist.mkdir()
    built_agent = _run([sys.executable, "-m", "build", "--wheel", "--no-isolation",
                        "--outdir", str(agent_dist), str(agent_source)], cwd=tmp_path, env=env)
    assert built_agent.returncode == 0, built_agent.stdout + built_agent.stderr
    agent_wheel, = agent_dist.glob("*.whl")
    runtime = Path(os.environ["ASTRID_RUNTIME_CHECKOUT"])
    runtime_source = tmp_path / "runtime-source"
    runtime_source.mkdir()
    shutil.copy2(runtime / "pyproject.toml", runtime_source / "pyproject.toml")
    for name in ("runtime_protocol", "astrid_runtime"):
        shutil.copytree(runtime / name, runtime_source / name,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    runtime_dist = tmp_path / "runtime-dist"
    runtime_dist.mkdir()
    built_runtime = _run([sys.executable, "-m", "build", "--wheel", "--no-isolation",
                          "--outdir", str(runtime_dist), str(runtime_source)], cwd=tmp_path, env=env)
    assert built_runtime.returncode == 0, built_runtime.stdout + built_runtime.stderr
    runtime_wheel, = runtime_dist.glob("*.whl")
    installed_agent = _run([UV, "pip", "install", "--python", str(python), "--no-deps",
                            str(agent_wheel), str(runtime_wheel)],
                           cwd=tmp_path, env=env)
    assert installed_agent.returncode == 0, installed_agent.stdout + installed_agent.stderr

    script = textwrap.dedent("""
        import hashlib
        import json
        from pathlib import Path
        from types import SimpleNamespace
        from types import ModuleType
        import sys
        import vibecomfy
        from vibecomfy.errors import CheckoutRequiredError
        from vibecomfy.utils import find_repo_root
        from vibecomfy.runtime import session
        from vibecomfy.runtime.run import run_sync
        from vibecomfy.schema import NodeSchema
        from vibecomfy.workflow import VibeNode, VibeOutput, VibeWorkflow, WorkflowSource
        from vibecomfy.workflow_bundle import load_bundle
        import astrid
        # Load the single installed Agent transport module under its package
        # without importing unrelated cloud backends and their optional SDKs.
        backends = ModuleType('astrid.core.generation.backends')
        backends.__path__ = [str(Path(astrid.__file__).parent / 'core/generation/backends')]
        sys.modules['astrid.core.generation.backends'] = backends
        from astrid.core.generation.backends.comfy_target import (
            extract_output_descriptors, validate_output_descriptor,
        )

        fixture_root = Path(sys.argv[1])
        source_root = Path(sys.argv[2]).resolve()
        agent_source_root = Path(sys.argv[3]).resolve()
        prefix = Path(sys.prefix).resolve()
        Path(vibecomfy.__file__).resolve().relative_to(prefix)
        Path(validate_output_descriptor.__code__.co_filename).resolve().relative_to(prefix)
        assert all(not Path(p).resolve().is_relative_to(root)
                   for p in sys.path if p for root in (source_root, agent_source_root))
        try:
            find_repo_root()
        except CheckoutRequiredError:
            pass
        else:
            raise AssertionError('installed wheel found a VibeComfy source checkout')

        manifest = json.loads((fixture_root / 'sha256-manifest.json').read_text())
        for name, digest in manifest.items():
            assert hashlib.sha256((fixture_root / name).read_bytes()).hexdigest() == digest
        history = json.loads((fixture_root / 'history.json').read_text())
        prompt_id, entry = next(iter(history.items()))
        descriptor = entry['outputs']['13']['images'][0]
        assert descriptor['metadata'] == {'kind': 'image', 'width': 512, 'height': 512}
        routed = extract_output_descriptors(entry['outputs'])
        assert routed == [descriptor]
        assert validate_output_descriptor(routed[0]) == (
            descriptor['filename'], descriptor['subfolder'], descriptor['type'])
        view = json.loads((fixture_root / 'view-headers.json').read_text())
        assert view['status'] == 200
        assert view['headers']['Content-Type'] == 'image/png'
        assert view['headers']['Content-Length'] == '265200'

        url = 'http://live-contract.invalid'
        calls = []
        class Client:
            server_url = url
            async def _post_prompt(self, prompt):
                assert prompt
                # run_sync must treat checkout identity as optional for this
                # non-editable wheel; source-only APIs cannot find a checkout.
                assert session.current_source_revision() is None
                assert session.current_source_content_digest() is None
                calls.append('prompt')
                return {'prompt_id': prompt_id}
            async def history(self, observed_prompt_id):
                assert observed_prompt_id == prompt_id
                calls.append('history')
                return history
        class Artifacts:
            server_url = url
            async def download_artifacts(self, *, local_root):
                # Replay only the recorded output descriptor and HTTP header
                # contract. The live fixture intentionally contains no media
                # body, so this transport writes an empty custody marker.
                assert descriptor['filename'] in view['headers']['Content-Disposition']
                assert descriptor['type'] == 'output'
                assert descriptor['subfolder'] == ''
                output = local_root / 'output' / descriptor['filename']
                output.parent.mkdir(parents=True, exist_ok=True)
                output.touch()
                calls.append('retrieved')
                return {'status': 'retrieved', 'local_root': str(local_root)}

        workflow = VibeWorkflow('installed-live-contract', WorkflowSource('installed-live-contract'))
        workflow.nodes['13'] = VibeNode('13', 'SaveImage', uid='live-save-image',
                                        inputs={'filename_prefix': 'live-contract'})
        workflow.outputs = [VibeOutput('13', 'SaveImage', name='image',
                                       artifact_kind='image', mime_type='image/png',
                                       expected_cardinality='one')]
        bundle = load_bundle(workflow)
        provider = SimpleNamespace(get_schema=lambda name: NodeSchema(name, None, {}, []),
                                   requires_fresh_target=True)
        record = bundle.compile(schema_provider=provider)
        result = run_sync(record, bundle, server_url=url, server_client=Client(),
                          artifact_client=Artifacts(), schema_provider=provider)
        assert calls == ['prompt', 'history', 'retrieved'], calls
        assert result.artifacts[0]['descriptor']['metadata'] == descriptor['metadata']
        assert Path(result.completion_path).is_file()
        assert Path(result.artifacts[0]['path']).is_file()
        attempt = Path(result.metadata_path).parent / 'attempt.json'
        attempt_data = json.loads(attempt.read_text())
        assert attempt_data['prompt_id'] == prompt_id
        assert attempt_data['output_verification']['status'] == 'final_output_produced'
        assert attempt_data['artifact_retrieval']['status'] == 'retrieved'
    """)
    checked = _run([str(python), "-I", "-c", script, str(FIXTURES), str(vibe), str(agent_source)],
                   cwd=tmp_path, env=env)
    assert checked.returncode == 0, checked.stdout + checked.stderr
