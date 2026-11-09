"""Installed-package proof for consistent generation (Plan 02 Decision 10; task D3, run in V3).

A fresh, non-editable install of the committed candidate through the supported
installer entry ``Backend.stage(manifest, root, data, copy_from=...)``
(astrid/installer/backend.py:origin/pre-main:995), then, with no checkout and no
user site visible to the installed program (``python -I``, empty cwd, fresh HOME):

* import origins of ``astrid`` and ``vibecomfy`` are inside the installed environment;
* ``vibecomfy.utils.find_repo_root()`` raises ``CheckoutRequiredError``
  (VibeComfy donor 1b2e242c vibecomfy/utils.py:18-31, vibecomfy/errors.py:81);
* the ``generate list`` inventory contains the six defaults;
* a dry run per default (``--placement runpod --dry-run`` as D1b leaves it; the Foley
  RunPod refusal for sound effects, plus an auto dry run of sound effects against
  the CLI-GOLD stand-in ComfyUI so that slot also has a resolving dry run);
* fail-closed guidance (``needs a local GPU or --placement runpod``) with no engine and
  no RunPod.

No weights, GPU, provider call or model download. Every installed command runs with
HF_HUB_OFFLINE=1. If any stage would need a model download the test fails with
``model download needed: <stage>: <first error line>``. This module never skips.

What is stubbed, exactly as tests/test_installer_real_product_root.py:origin/pre-main
does (lines 118-138 and 167-169): ``install_tools`` (Node, Bun, OMP and ffmpeg
downloads) is replaced with placeholder executables, and Remotion's npm install is
not run. Neither is part of the generation path this proves. Everything else in
``stage`` is real: ``git archive`` of the pinned commits, ``uv venv``, the agent's
hash-locked ``requirements/runtime.lock``, and non-editable installs of Runtime,
Agent and VibeComfy into ``<version>/py`` (backend.py:origin/pre-main:1027-1033).

Citations: ``file:rev:line``; revs as in tests/core/generation/test_cli_gold_075b.py.
Values marked RE-VERIFY must be checked on the merged candidate (drafts/NOTES.md).
"""

from __future__ import annotations

import inspect
import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from tests.core.generation.test_cli_gold_075b import (
    DEFAULT_ENTRY_IDS,
    EDGE_TTS,
    FOLEY_REFUSAL_CODE,
    FOLEY_REFUSAL_MESSAGE,
    GPU_GUIDANCE,
    SLOTS,
    SLOT_IDS,
    StandInComfy,
    _assert_entry_named,
    _assert_nothing_banned_selected,
    _inputs,
    _parse_json,
    _payload,
    _require_no_discovered_comfy,
    _stand_in_object_info,
    _task_ids,
    _workflow_named,
    slot_argv,
)

# The retained INSTALLED-TEST card gives a disk peak of about 12 GB (tasks.md D3).
# Network or a warm uv cache is needed for the hash-locked runtime.lock wheels.
# The integration lane (default_off; it needs the Runtime checkout, VibeComfy at
# its pin and the network) keeps this out of the per-push run, so V3 selects it:
# `bash "$ENV/agent-pytest.sh" -q -m integration tests/installed`.
pytestmark = [pytest.mark.timeout(5400), pytest.mark.integration]

REPO = Path(__file__).resolve().parents[2]

# RE-VERIFY after C2/A1: include the lifecycle and runpod source blocks only when the
# candidate's App install manifest carries them. App pre-main 878c28df
# public/install-manifest.json has neither (nor vibecomfy), so the default is None.
# When used, each block is {"repo", "sha", "version"} with the reviewed repositories
# (astrid/installer/tools.py:origin/pre-main:26-29, 67-81; backend.py:origin/pre-main:1017-1023).
RUNPOD_SOURCES: dict[str, dict[str, str]] | None = None

# The App install manifest pins the installer's Python (public/install-manifest.json
# tools.python.version: 3.11.13 on App pre-main 878c28df and on the Plan 01 App
# composition 94f78ad1). requirements/runtime.lock is resolved for it; a newer
# interpreter (3.13 under agent-setup.sh) has no numpy 1.26.4 wheel and would
# build from source. uv provisions this CPython for the staged version.
INSTALLER_PYTHON = "3.11.13"

_DOWNLOAD = re.compile(
    r"(?i)huggingface\.co|hf_hub_download|snapshot_download|OfflineModeIsEnabled|LocalEntryNotFoundError"
    r"|model download|download(?:ing)?\s+(?:the\s+)?(?:model|weights)"
)


def _first_error_line(text: str) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    for line in lines:
        if re.search(r"(?i)error|failed|refus|denied|missing|not found", line):
            return line[:400]
    return (lines[0] if lines else "<no output>")[:400]


def _guard_download(stage: str, text: str) -> None:
    """tasks.md D3/V3: a stage that needs a model download stops the proof; never fetch weights.

    Called only on an unexpected outcome, so guidance text that merely mentions a
    download (for example an install fix) never turns a pass into a failure."""
    if _DOWNLOAD.search(text or ""):
        matched = next((line for line in text.splitlines() if _DOWNLOAD.search(line)), text)
        pytest.fail(f"model download needed: {stage}: {_first_error_line(matched)}", pytrace=False)


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull})
    return subprocess.run(["git", "-C", str(repo), *args], env=env, capture_output=True,
                          text=True, check=check, timeout=120)


def _has_commit(repo: str | None, sha: str) -> bool:
    if not repo or not Path(repo).is_dir():
        return False
    result = _git(Path(repo), "cat-file", "-t", sha, check=False)
    return result.returncode == 0 and result.stdout.strip() == "commit"


def _tool_stand_ins(root: Path) -> dict[str, str]:
    """Copy of tests/test_installer_real_product_root.py:origin/pre-main:118-138
    (placeholders for install_tools' Node, npm and Bun downloads)."""
    node = root / "node" / "bin" / "node"
    npm = root / "node" / "lib" / "node_modules" / "npm" / "bin" / "npm-cli.js"
    bun = root / "bun" / "bun"
    for path in (node, npm, bun):
        path.parent.mkdir(parents=True, exist_ok=True)
    for path in (node, bun):
        path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        path.chmod(0o755)
    npm.write_text("// placeholder npm entry point\n", encoding="utf-8")
    return {"PATH": os.defpath, "ASTRID_NODE_EXECUTABLE": str(node), "ASTRID_BUN_EXECUTABLE": str(bun)}


def _manifest(uv: str) -> dict[str, Any]:
    """The installer manifest for the committed candidate.

    Shape: schema_version/release, agent/runtime/vibecomfy {sha[, repo]}, tools.uv.version,
    tools.python.version (backend.py:origin/pre-main:996-1013, 1027; tools.py:origin/pre-main:62-99).
    A local ``repo`` is archived with ``git archive`` of the commit, never the worktree
    (tools.py:origin/pre-main:219-246); without ``repo`` the source is the GitHub archive
    (tools.py:origin/pre-main:292-316).
    """
    from astrid.core.generation.vibecomfy_dependency import VIBECOMFY_ENGINE_REVISION

    if not (REPO / ".git").exists():
        pytest.fail(f"{REPO} is not a Git checkout; the proof installs the committed candidate head")
    dirty = _git(REPO, "status", "--porcelain", "--untracked-files=no")
    if dirty.stdout.strip():
        pytest.fail("Tracked files differ from HEAD; V3 must prove the committed candidate head:\n"
                    + dirty.stdout[:2000])
    agent_sha = _git(REPO, "rev-parse", "HEAD").stdout.strip()
    # runtime.lock.json source_commit (the same key agent-setup.sh reads; tasks.md C2 step 5).
    runtime_sha = json.loads((REPO / "runtime.lock.json").read_text(encoding="utf-8"))["source_commit"]
    runtime: dict[str, str] = {"sha": runtime_sha}
    if _has_commit(os.environ.get("ASTRID_RUNTIME_CHECKOUT"), runtime_sha):
        runtime["repo"] = str(Path(os.environ["ASTRID_RUNTIME_CHECKOUT"]).resolve())
    vibecomfy: dict[str, str] = {"sha": VIBECOMFY_ENGINE_REVISION}
    if _has_commit(os.environ.get("VIBECOMFY_SOURCE"), VIBECOMFY_ENGINE_REVISION):
        vibecomfy["repo"] = str(Path(os.environ["VIBECOMFY_SOURCE"]).resolve())
    uv_version = subprocess.run([uv, "--version"], check=True, capture_output=True,
                                text=True, timeout=60).stdout.split()[1]
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "release": 1,
        "agent": {"sha": agent_sha, "repo": str(REPO)},
        "runtime": runtime,
        "vibecomfy": vibecomfy,
        "tools": {
            "uv": {"version": uv_version},
            # The supported installer Python: the App install manifest's pin.
            "python": {"version": INSTALLER_PYTHON},
        },
    }
    if RUNPOD_SOURCES is not None:
        manifest.update(RUNPOD_SOURCES)
    return manifest


def _expect(result: subprocess.CompletedProcess[str], stage: str, *, ok: bool,
            contains: tuple[str, ...] = ()) -> str:
    text = result.stdout + "\n" + result.stderr
    if (result.returncode == 0) != ok or not all(item in text for item in contains):
        _guard_download(stage, text)
        pytest.fail(f"{stage}: exit {result.returncode}, expected {'0' if ok else 'non-zero'}"
                    + (f" with {list(contains)}" if contains else "") + f"\n{text[-6000:]}", pytrace=False)
    return text


@dataclass(frozen=True)
class Installed:
    version: Any
    python: Path
    base_env: dict[str, str]
    manifest: dict[str, Any]

    def env(self, root: Path) -> dict[str, str]:
        """A fresh HOME, TMPDIR and XDG roots per test, so no saved target leaks between cases."""
        env = dict(self.base_env)
        for name in ("home", "tmp", "cache", "config", "data", "state"):
            (root / name).mkdir(parents=True, exist_ok=True)
        env.update(HOME=str(root / "home"), TMPDIR=str(root / "tmp"), XDG_CACHE_HOME=str(root / "cache"),
                   XDG_CONFIG_HOME=str(root / "config"), XDG_DATA_HOME=str(root / "data"),
                   XDG_STATE_HOME=str(root / "state"))
        return env

    def run(self, *args: str, root: Path, stage: str) -> subprocess.CompletedProcess[str]:
        cwd = root / "cwd"
        cwd.mkdir(parents=True, exist_ok=True)
        result = subprocess.run([str(self.python), "-I", *args], cwd=cwd, env=self.env(root),
                                capture_output=True, text=True, timeout=600, check=False)
        assert not any(cwd.iterdir()), f"{stage}: the empty working directory was written"
        return result

    def generate(self, *args: str, root: Path, stage: str) -> subprocess.CompletedProcess[str]:
        return self.run("-m", "astrid", "generate", *args, root=root, stage=stage)


def _installed_env(version: Any) -> dict[str, str]:
    """The staged version's environment with nothing pointing at a checkout and no RunPod key.
    HOME/TMPDIR/XDG roots are set per test by Installed.env."""
    drop = {"PYTHONPATH", "PYTHONHOME", "VIBECOMFY_SOURCE", "ASTRID_VIBECOMFY_CHECKOUT",
            "ASTRID_RUNTIME_CHECKOUT", "ASTRID_WORKSPACE_ENDPOINT", "ASTRID_WORKSPACE_CREDENTIAL",
            "VIBECOMFY_COMFYUI_URL"}
    env = {key: value for key, value in version.env.items()
           if key not in drop and not key.upper().startswith("RUNPOD")}
    env.update(PYTHONNOUSERSITE="1", HF_HUB_OFFLINE="1")
    return env


@pytest.fixture(scope="module")
def installed(tmp_path_factory) -> Installed:
    from astrid.installer.backend import Backend
    from astrid.installer.core import write_json
    from astrid.installer.service import LABEL

    # stage(manifest, root, data, *, copy_from=None): backend.py:origin/pre-main:995.
    # PINS and the DEFAULTS donor lack copy_from (backend.py:1608dbbf:907, 9d3bb248:907);
    # the merged candidate must keep pre-main's signature.
    parameters = inspect.signature(Backend.stage).parameters
    assert "copy_from" in parameters, f"Backend.stage lost copy_from: {list(parameters)}"
    uv = shutil.which("uv")
    if uv is None:
        pytest.fail("uv must be on PATH: Backend.stage needs the pinned user-local uv")
    if shutil.which("git") is None:
        pytest.fail("git must be on PATH: Backend.stage archives the pinned commits")

    root = tmp_path_factory.mktemp("installed")
    program, data = root / "program", root / "data"
    program.mkdir()
    # As tests/test_installer_real_product_root.py:origin/pre-main:151.
    write_json(program / "install.json", {"owner": LABEL, "data_root": str(data)})
    manifest = _manifest(uv)
    (root / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    log = root / "install.log"
    backend = Backend(program, log, Path(uv), say=lambda _line: None)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr("astrid.installer.backend.install_tools",
                      lambda _manifest, folder, _target, _run: _tool_stand_ins(folder))
        patch.setattr(backend, "install_remotion_dependencies", lambda *_args: False)
        # The installed timeline-schema check refuses group/other-writable inputs
        # (astrid/core/rendering/_local_schema.py, forbidden=0o022), and uv writes
        # site-packages files with the process umask. Stage with the conventional
        # 022 so a group-writable default umask (0002) does not decide the result;
        # restored right after staging.
        previous_umask = os.umask(0o022)
        try:
            version = backend.stage(manifest, program, data, copy_from=None)
        except subprocess.CalledProcessError as exc:
            text = str(exc.output or "") + "\n" + (log.read_text(encoding="utf-8", errors="replace")
                                                   if log.is_file() else "")
            _guard_download("installer stage", text)
            pytest.fail("Backend.stage failed (it needs network or a warm uv cache for the hash-locked "
                        f"runtime.lock wheels): {_first_error_line(text)}\n{text[-3000:]}", pytrace=False)
        except (ValueError, RuntimeError, OSError) as exc:
            _guard_download("installer stage", str(exc))
            pytest.fail(f"Backend.stage refused the candidate: {exc}", pytrace=False)
        finally:
            os.umask(previous_umask)

    return Installed(version, Path(version.python), _installed_env(version), manifest)


_ORIGINS = r"""
import json, pathlib, sys, sysconfig
import astrid, vibecomfy
print(json.dumps({
    "astrid": str(pathlib.Path(astrid.__file__).resolve()),
    "vibecomfy": str(pathlib.Path(vibecomfy.__file__).resolve()),
    "purelib": str(pathlib.Path(sysconfig.get_path("purelib")).resolve()),
    "prefix": str(pathlib.Path(sys.prefix).resolve()),
    "executable": str(pathlib.Path(sys.executable)),
    "path": sys.path,
}))
"""

_REPO_ROOT = r"""
import json
from vibecomfy.errors import CheckoutRequiredError
from vibecomfy.utils import find_repo_root
try:
    root = find_repo_root()
except CheckoutRequiredError as exc:
    print(json.dumps({"raised": type(exc).__name__, "message": str(exc)}))
else:
    print(json.dumps({"raised": None, "root": str(root)}))
"""


def test_import_origins_are_inside_the_installed_environment(installed, tmp_path):
    result = installed.run("-c", _ORIGINS, root=tmp_path, stage="import origins")
    _expect(result, "import origins", ok=True)
    origins = json.loads(result.stdout)
    purelib, version_root = Path(origins["purelib"]), Path(installed.version.root).resolve()
    assert purelib.is_relative_to(version_root), origins
    source = os.environ.get("VIBECOMFY_SOURCE")
    for name in ("astrid", "vibecomfy"):
        origin = Path(origins[name])
        assert origin.is_relative_to(purelib), f"{name} imported from {origin}, not {purelib}"
        assert not origin.is_relative_to(REPO), f"{name} imported from the source checkout {REPO}"
        if source:
            assert not origin.is_relative_to(Path(source).resolve()), f"{name} imported from {source}"
    leaked = [entry for entry in origins["path"]
              if entry and Path(entry).resolve().is_relative_to(REPO)]
    assert not leaked, f"the source checkout is on the installed sys.path: {leaked}"


def test_vibecomfy_find_repo_root_requires_a_checkout(installed, tmp_path):
    result = installed.run("-c", _REPO_ROOT, root=tmp_path, stage="find_repo_root")
    _expect(result, "find_repo_root", ok=True)
    outcome = json.loads(result.stdout)
    assert outcome["raised"] == "CheckoutRequiredError", outcome


def test_installed_inventory_lists_the_six_defaults(installed, tmp_path):
    result = installed.generate("list", "--json", root=tmp_path, stage="generate list")
    _expect(result, "generate list", ok=True)
    rows = _parse_json(result.stdout)
    ids = {row.get("id") for row in rows if isinstance(row, dict)}
    assert DEFAULT_ENTRY_IDS <= ids, f"missing from the installed inventory: {sorted(DEFAULT_ENTRY_IDS - ids)}"


@pytest.mark.parametrize("slot", SLOTS, ids=SLOT_IDS)
def test_installed_runpod_dry_run_per_default(slot, installed, tmp_path):
    stage = f"dry run {slot.name} runpod"
    result = installed.generate(*slot_argv(slot, _inputs(tmp_path), placement="runpod"),
                                root=tmp_path, stage=stage)
    if slot.name == "sound-effects":
        _expect(result, stage, ok=False, contains=(FOLEY_REFUSAL_CODE, FOLEY_REFUSAL_MESSAGE))
        assert not _task_ids(_payload(result)), _task_ids(_payload(result))
        return
    _expect(result, stage, ok=True)
    payload = _payload(result)
    _assert_entry_named(payload, slot)
    assert _workflow_named(payload, slot.workflow), json.dumps(payload)[:4000]
    _assert_nothing_banned_selected(payload, context=f"installed {slot.name} runpod")
    assert not _task_ids(payload), _task_ids(payload)


def test_installed_sound_effects_auto_dry_run_resolves_foley(installed, tmp_path):
    """RunPod refuses Foley, so sound effects resolves through `--placement auto` against the
    CLI-GOLD stand-in ComfyUI (reads only; no /prompt)."""
    slot = next(item for item in SLOTS if item.name == "sound-effects")
    files = _inputs(tmp_path)
    with StandInComfy(_stand_in_object_info()) as comfy:
        saved = installed.generate("targets", "--placement", comfy.url, "--save", "--json",
                                   root=tmp_path, stage="save stand-in target")
        _expect(saved, "save stand-in target", ok=True)
        stage = "dry run sound-effects auto"
        result = installed.generate(*slot_argv(slot, files, placement="auto"), root=tmp_path, stage=stage)
        _expect(result, stage, ok=True)
        payload = _payload(result)
        assert isinstance(payload, dict) and payload["entry"]["id"] == slot.entry, payload
        _assert_nothing_banned_selected(payload, context="installed sound-effects auto")
        assert not _task_ids(payload), _task_ids(payload)
        assert not comfy.writes(), comfy.writes()


@pytest.mark.parametrize("case", ["image", "tts"])
def test_installed_no_engine_fails_closed_with_guidance(case, installed, tmp_path):
    _require_no_discovered_comfy()
    argv = (["image", "A tiny blue lighthouse.", "--placement", "auto", "--json"] if case == "image" else
            ["audio", "--mode", "tts", "--input", "text=The exact authored words.", "--placement", "auto", "--json"])
    stage = f"fail-closed {case}"
    result = installed.generate(*argv, root=tmp_path, stage=stage)
    _expect(result, stage, ok=False, contains=(GPU_GUIDANCE, EDGE_TTS) if case == "tts" else (GPU_GUIDANCE,))
    assert not _task_ids(_payload(result)), result.stdout + result.stderr
