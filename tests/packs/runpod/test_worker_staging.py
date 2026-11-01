from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping

import pytest

from astrid.core.generation.model_root import canonical_model_inventory_digest
from astrid.core.generation.vibecomfy_dependency import VIBECOMFY_CHECKOUT_ENV
from astrid.packs.runpod.worker_staging import (
    _CHILD_CHECK_PROGRAM,
    _REMOTE_PROBE_PROGRAM,
    _TREE_EXCLUDES,
    PreparationJournalError,
    StagingEvidenceStaleError,
    WorkerPreparationError,
    _journal_path,
    prepare_worker,
)
from tests.helpers.runtime import use_runtime
from tests.lanes import unavailable


def test_child_readiness_contract_does_not_probe_cuda_or_gpu() -> None:
    # Preparation is intentionally CPU/control-plane-only; static release
    # metadata may pin a CUDA build, but child readiness does not inspect it.
    assert "torch.version.cuda" not in _CHILD_CHECK_PROGRAM
    assert "torch.cuda" not in _CHILD_CHECK_PROGRAM
    assert "nvidia-smi" not in _CHILD_CHECK_PROGRAM.casefold()

ROOT = Path(__file__).resolve().parents[3]
# The checkout comes from the environment, else the sibling of this checkout; a missing one is the
# second-checkout case of tests/lanes.py (fail in the integration lane, skip elsewhere).
VIBECOMFY = Path(os.environ.get(VIBECOMFY_CHECKOUT_ENV, "").strip() or ROOT.parent / "vibecomfy").expanduser().resolve()
ACCOUNT_REF = "runpod-test-account"


def _ignore(directory: str, names: list[str]) -> set[str]:
    ignored = set()
    for name in names:
        path = Path(directory, name)
        if name in _TREE_EXCLUDES or path.suffix in {".pyc", ".pyo"}:
            ignored.add(name)
    return ignored


def _ignore_vibe_fixture(directory: str, names: list[str]) -> set[str]:
    ignored = _ignore(directory, names)
    current = Path(directory)
    if current.name == "comfy_nodes":
        ignored.add("web_dist")
    if current.name == "porting":
        ignored.add("cache")
    return ignored


def _copy_link_or_copy(source: str, destination: str) -> str:
    try:
        return os.link(source, destination)
    except OSError:
        return shutil.copy2(source, destination)


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


@pytest.fixture(scope="module")
def source_fixture(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    base = tmp_path_factory.mktemp("worker-staging-sources")
    astrid = base / "astrid-source"
    astrid.mkdir()
    shutil.copytree(ROOT / "astrid", astrid / "astrid", copy_function=_copy_link_or_copy, ignore=_ignore)
    shutil.copy2(ROOT / "pyproject.toml", astrid / "pyproject.toml")

    if not (VIBECOMFY / "vibecomfy").is_dir():
        unavailable(f"no VibeComfy checkout at {VIBECOMFY}: set {VIBECOMFY_CHECKOUT_ENV} to one")

    vibe = base / "vibecomfy-source"
    vibe.mkdir()
    shutil.copytree(VIBECOMFY / "vibecomfy", vibe / "vibecomfy", copy_function=_copy_link_or_copy, ignore=_ignore_vibe_fixture)
    shutil.copy2(VIBECOMFY / "pyproject.toml", vibe / "pyproject.toml")
    shutil.copy2(VIBECOMFY / "README.md", vibe / "README.md")
    workflow_dir = vibe / "ready_templates" / "smoke"
    workflow_dir.mkdir(parents=True)
    if str(VIBECOMFY) not in sys.path:
        sys.path.insert(0, str(VIBECOMFY))
    from vibecomfy.nodes.core import EmptyImage, SaveImage
    from vibecomfy.templates import ReadyMetadata, new_workflow
    from vibecomfy.workflow_bundle import emit_bundle

    workflow_path = workflow_dir / "empty_image_red.py"
    metadata = ReadyMetadata.build(
        capability="runtime_smoke",
        requirements={"custom_nodes": ["ComfyUI-H3-Motion-Context-MultiRef"]},
    )
    workflow = new_workflow(metadata, source_path=str(workflow_path))
    image_node = EmptyImage(
        _id="1", width=64, height=64, batch_size=1, color=16711680
    )
    save_node = SaveImage(
        _id="2", filename_prefix="vibecomfy_worker_staging_smoke", images=image_node
    )
    workflow.finalize(
        {},
        output_node=save_node,
        output_type="SaveImage",
        name="image",
        artifact_kind="image",
        mime_type="image/png",
        expected_cardinality="one",
        filename_prefix="vibecomfy_worker_staging_smoke",
    )
    emit_bundle(workflow, workflow_path, {"operation": "authored"})
    workflow_path.with_name("source.json").write_text("{}\n", encoding="utf-8")
    (vibe / ".gitignore").write_text("__pycache__/\n*.py[cod]\n", encoding="utf-8")
    _git(vibe, "init", "-q")
    _git(vibe, "config", "user.email", "worker-staging-test@example.invalid")
    _git(vibe, "config", "user.name", "Worker staging tests")
    _git(vibe, "add", "-A")
    _git(vibe, "commit", "-qm", "fixture release")
    with (vibe / "pyproject.toml").open("rb") as stream:
        version = tomllib.load(stream)["project"]["version"]
    return {
        "astrid": astrid,
        "vibecomfy": vibe,
        "vibe_commit": _git(vibe, "rev-parse", "HEAD"),
        "vibe_version": version,
        "workflow": workflow_path,
    }


class LocalPreparationTransport:
    """Execute the production probe and child programs against local paths."""

    def __init__(self) -> None:
        self.uploads: dict[str, int] = {}
        self.child_calls = 0
        self.fail_after_upload: str | None = None
        self.empty_probe = False

    @staticmethod
    def _run_program(program: str, argv: list[str], python: str | None = None) -> dict[str, Any]:
        selected_python = python or sys.executable
        wrapper = (
            "import json,os,sys; "
            "sys.executable=os.environ.get('ASTRID_TEST_SELECTED_PYTHON',sys.executable); "
            "program=sys.argv[1]; args=json.loads(sys.argv[2]); "
            "sys.argv=['<local-child>',*args]; exec(compile(program,'<local-child>','exec'))"
        )
        result = subprocess.run(
            [selected_python, "-c", wrapper, program, json.dumps(argv)],
            # This fake remote boundary executes with the test interpreter but
            # reports the selected deployment path as sys.executable. Preserve
            # the test venv's dependencies while probing the staged source.
            env={
                **os.environ,
                "PYTHONPATH": os.pathsep.join([*sys.path, str(use_runtime())]),
                "ASTRID_TEST_SELECTED_PYTHON": str(Path(selected_python).resolve()),
            },
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode:
            raise WorkerPreparationError((result.stderr or result.stdout).strip())
        try:
            return json.loads([line for line in result.stdout.splitlines() if line.strip()][-1])
        except (IndexError, json.JSONDecodeError) as exc:
            raise WorkerPreparationError("local child returned no JSON facts") from exc

    async def probe_tree(self, remote_path: str) -> Mapping[str, Any]:
        if self.empty_probe:
            return {}
        return self._run_program(
            _REMOTE_PROBE_PROGRAM,
            [remote_path, json.dumps(sorted(_TREE_EXCLUDES))],
        )

    async def upload_path(self, local_path: Path, remote_path: str) -> None:
        key = str(local_path)
        self.uploads[key] = self.uploads.get(key, 0) + 1
        target = Path(remote_path)
        shutil.copytree(local_path, target, copy_function=_copy_link_or_copy, ignore=_ignore)
        if self.fail_after_upload == key:
            self.fail_after_upload = None
            raise RuntimeError("injected lost upload response")

    async def remove_owned_tree(self, remote_path: str, owned_root: str) -> None:
        target = Path(remote_path)
        owner = Path(owned_root)
        if not target.is_relative_to(owner) or target == owner:
            raise AssertionError("transport was asked to remove an unowned path")
        if target.exists():
            shutil.rmtree(target)

    async def child_check(self, request: Mapping[str, Any]) -> Mapping[str, Any]:
        self.child_calls += 1
        payload = base64.b64encode(
            json.dumps(request, sort_keys=True, separators=(",", ":")).encode()
        ).decode()
        return self._run_program(
            _CHILD_CHECK_PROGRAM,
            [payload],
            python=str(request["effective_config"]["python_executable"]),
        )


def _git_repo(root: Path, files: Mapping[str, str]) -> str:
    root.mkdir(parents=True)
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "worker-staging-test@example.invalid")
    _git(root, "config", "user.name", "Worker staging tests")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "fixture pinned tree")
    return _git(root, "rev-parse", "HEAD")


def _setup(tmp_path: Path, sources: Mapping[str, Any]) -> dict[str, Any]:
    volume = tmp_path / "volume"
    volume.mkdir(parents=True)
    candidate = volume / "releases" / "selected-v1"
    runtime = candidate / "runtime"
    python_link = runtime / "venv" / "bin" / "python"
    python_link.parent.mkdir(parents=True)
    python_link.symlink_to(Path(sys.executable).resolve())
    launcher = runtime / "launch-comfy.sh"
    launcher.parent.mkdir(parents=True, exist_ok=True)
    launcher.write_text("#!/bin/sh\nexec python ComfyUI/main.py\n", encoding="utf-8")
    launcher.chmod(0o755)

    comfy = runtime / "ComfyUI"
    comfy.mkdir(parents=True)
    (comfy / ".gitignore").write_text("custom_nodes\n", encoding="utf-8")
    (comfy / "main.py").write_text("# fixture Comfy entrypoint\n", encoding="utf-8")
    # Initialize Comfy after writing source files, with its custom-node roots ignored.
    _git(comfy, "init", "-q")
    _git(comfy, "config", "user.email", "worker-staging-test@example.invalid")
    _git(comfy, "config", "user.name", "Worker staging tests")
    _git(comfy, "add", ".gitignore", "main.py")
    _git(comfy, "commit", "-qm", "fixture Comfy release")
    comfy_commit = _git(comfy, "rev-parse", "HEAD")

    node_commits: dict[str, str] = {}
    node_specs = {
        "h3_custom_node_commit": "ComfyUI-H3-Motion-Context-MultiRef",
        "videohelpersuite_commit": "ComfyUI-VideoHelperSuite",
    }
    node_roots: dict[str, str] = {}
    for field, name in node_specs.items():
        root = comfy / "custom_nodes" / name
        node_commits[field] = _git_repo(root, {"README.md": name + " fixture\n"})
        node_roots[field] = str(root)

    lock = runtime / "vibecomfy" / "custom_nodes.lock"
    lock.parent.mkdir(parents=True)
    lock.write_text(
        "# fixture pinned node closure\n"
        "[nodepacks.ComfyUI-H3-Motion-Context-MultiRef]\nname = \"ComfyUI-H3-Motion-Context-MultiRef\"\n"
        f"commit = \"{node_commits['h3_custom_node_commit']}\"\n\n"
        "[nodepacks.ComfyUI-VideoHelperSuite]\nname = \"ComfyUI-VideoHelperSuite\"\n"
        f"git_commit_sha = \"{node_commits['videohelpersuite_commit']}\"\n",
        encoding="utf-8",
    )

    model_root = volume / "models"
    model_root.mkdir()
    model = model_root / "fixture-model.safetensors"
    model.write_bytes(b"small deterministic CPU fixture model\n")
    model_rows = [
        {
            "subdir": "",
            "name": model.name,
            "size": model.stat().st_size,
            "sha256": "sha256:" + hashlib.sha256(model.read_bytes()).hexdigest(),
        }
    ]
    model_binding = {
        "schema_version": 1,
        "path": str(model_root),
        "inventory": model_rows,
        "inventory_digest": canonical_model_inventory_digest(model_rows),
        "qualification_identity": None,
    }
    output_root = volume / "outputs"
    readiness = {
        "launch": {
            "model_root": model_binding,
            "output_root": str(output_root),
            "python_executable": str(python_link),
            "comfy_root": str(comfy),
            "launcher_path": str(launcher),
        }
    }
    lock_sha = hashlib.sha256(lock.read_bytes()).hexdigest()
    manifest = {
        "schema": "astrid.worker-staging.test-release.v1",
        "state": "ready_for_gpu_qualification",
        "release_id": "selected-v1",
        "candidate_namespace": str(candidate),
        "volume": {
            "id": "volume-test",
            "size_gb": 64,
            "datacenter": "EU-RO-1",
            "mount": str(volume),
        },
        "platform": {"python": sys.version.split()[0]},
        "models": [
            {
                "path": model.name,
                "bytes": model.stat().st_size,
                "sha256": hashlib.sha256(model.read_bytes()).hexdigest(),
            }
        ],
        "runtime": {
            "comfyui_commit": comfy_commit,
            "vibecomfy_source_commit": sources["vibe_commit"],
            "vibecomfy": sources["vibe_version"],
            "venv": "runtime/venv",
            "launcher": "runtime/launch-comfy.sh",
            "custom_nodes_lock_sha256": lock_sha,
            **node_commits,
        },
    }
    manifest_path = tmp_path / "release-manifest.json"
    manifest_path.write_text(json.dumps(manifest, sort_keys=True, separators=(",", ":")), encoding="utf-8")
    handle_path = tmp_path / "claim-handle.json"
    handle_path.write_text(
        json.dumps(
            {
                "state": "claimed",
                "operation_id": "operation-test",
                "provider_account_ref": ACCOUNT_REF,
                "api_key_ref": "RUNPOD_API_KEY",
                "pod_id": "pod-test",
                "network_volume_id": "volume-test",
                "network_volume_name": "test-volume",
                "network_volume_size_gb": 64,
                "network_volume_datacenter_id": "EU-RO-1",
                "volume_mount_path": str(volume),
            }
        ),
        encoding="utf-8",
    )
    return {
        "volume": volume,
        "candidate": candidate,
        "comfy": comfy,
        "comfy_root": str(comfy),
        "lock": str(lock),
        "node_roots": node_roots,
        "python": str(python_link),
        "launcher": str(launcher),
        "model_root": model_root,
        "output_root": output_root,
        "readiness": readiness,
        "manifest_path": manifest_path,
        "manifest_sha": "sha256:" + hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        "handle_path": handle_path,
        "sources": sources,
    }


def _rewrite_manifest(setup: dict[str, Any], manifest: Mapping[str, Any]) -> None:
    path = Path(setup["manifest_path"])
    path.write_text(json.dumps(manifest, sort_keys=True, separators=(",", ":")), encoding="utf-8")
    setup["manifest_sha"] = "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _prepare_args(setup: Mapping[str, Any], transport: LocalPreparationTransport) -> dict[str, Any]:
    return {
        "claim_handle_path": setup["handle_path"],
        "journal_dir": Path(setup["handle_path"]).parent / "journals",
        "provider_account_ref": ACCOUNT_REF,
        "release_manifest_path": setup["manifest_path"],
        "release_manifest_sha256": setup["manifest_sha"],
        "release_id": "selected-v1",
        "resolved_compute_profile": {"profile_id": "cpu-fixture"},
        "readiness_profile": setup["readiness"],
        "astrid_source": setup["sources"]["astrid"],
        "vibecomfy_source": setup["sources"]["vibecomfy"],
        "comfy_root": setup["comfy_root"],
        "custom_nodes_lock_path": setup["lock"],
        "custom_node_roots": setup["node_roots"],
        "python_executable": setup["python"],
        "engine_mode": "checkout_server",
        "workflow_path": setup["sources"]["workflow"],
        "workflow_inputs": {},
        "capability_id": "runtime_smoke",
        "transport": transport,
    }


def _run(setup: Mapping[str, Any], transport: LocalPreparationTransport, **extra: Any) -> dict[str, Any]:
    args = _prepare_args(setup, transport)
    args.update(extra)
    return asyncio.run(prepare_worker(**args))


@pytest.mark.integration  # needs a VibeComfy checkout (ASTRID_VIBECOMFY_CHECKOUT)
def test_preparation_runs_real_child_probe_and_reuses_verified_content(
    tmp_path: Path, source_fixture: Mapping[str, Any]
) -> None:
    setup = _setup(tmp_path, source_fixture)
    transport = LocalPreparationTransport()

    result = _run(setup, transport)
    assert result["status"] == "cpu_ready"
    assert result["gpu_qualified"] is False
    assert result["worker_qualified"] is False
    assert result["release_state"] == "ready_for_gpu_qualification"
    assert result["qualification_scope"] == "cpu_only_ready_for_gpu_qualification"
    assert result["cpu_readiness"]["status"] == "cpu_ready"
    assert result["cpu_readiness"]["workflow_preflight"]["phase"] == "preparation"
    assert result["cpu_readiness"]["release_facts"]["custom_nodes_lock_sha256"] == "sha256:" + hashlib.sha256(Path(setup["lock"]).read_bytes()).hexdigest()
    assert result["cpu_readiness"]["release_facts"]["required_custom_nodes"] == [
        "ComfyUI-H3-Motion-Context-MultiRef"
    ]
    h3_facts = result["cpu_readiness"]["release_facts"]["custom_node_commits"][
        "h3_custom_node_commit"
    ]
    assert h3_facts["lock_entry"] == "ComfyUI-H3-Motion-Context-MultiRef"
    assert h3_facts["commit"] == _git(
        Path(setup["node_roots"]["h3_custom_node_commit"]), "rev-parse", "HEAD"
    )
    assert result["cpu_readiness"]["imports"]["astrid"].startswith(str(setup["volume"]))
    assert len(transport.uploads) == 2
    first = result["cpu_readiness"]

    repeated = _run(setup, transport)
    assert repeated["cpu_readiness"] == first
    assert len(transport.uploads) == 2
    assert transport.child_calls == 2
    assert Path(result["journal_path"]).stat().st_mode & 0o777 == 0o600


@pytest.mark.integration  # needs a VibeComfy checkout (ASTRID_VIBECOMFY_CHECKOUT)
def test_materialized_h3_bundle_stages_as_an_owned_workflow_root(
    tmp_path: Path, source_fixture: Mapping[str, Any]
) -> None:
    setup = _setup(tmp_path, source_fixture)
    bundle = tmp_path / "h3-operation" / "prepared-workflow-bundle"
    bundle.mkdir(parents=True)
    source_bundle = Path(source_fixture["workflow"]).parent
    sources = {
        "workflow.py": Path(source_fixture["workflow"]),
        "workflow.vibe.json": Path(source_fixture["workflow"]).with_suffix(".vibe.json"),
        "source.json": source_bundle / "source.json",
    }
    for name, source in sources.items():
        shutil.copyfile(source, bundle / name)

    transport = LocalPreparationTransport()
    result = _run(
        setup,
        transport,
        workflow_path=bundle / "workflow.py",
        workflow_bundle_path=bundle,
    )

    assert result["status"] == "cpu_ready"
    assert result["gpu_qualified"] is False
    assert result["cpu_readiness"]["workflow_preflight"]["phase"] == "preparation"
    remote_bundle = Path(setup["volume"]) / ".astrid" / "prepared-workers" / (
        __import__("hashlib").sha256(b"operation-test").hexdigest()[:24]
    ) / "workflow"
    assert {path.name for path in remote_bundle.iterdir()} == {
        "workflow.py", "workflow.vibe.json", "source.json",
    }
    for name in ("workflow.py", "workflow.vibe.json", "source.json"):
        assert (remote_bundle / name).read_bytes() == (bundle / name).read_bytes()

    (remote_bundle / "workflow.py").write_text("# changed after staging\n", encoding="utf-8")
    with pytest.raises(PreparationJournalError, match="selected release/profile/config changed"):
        _run(
            setup,
            transport,
            workflow_path=bundle / "workflow.py",
            workflow_bundle_path=bundle,
        )


@pytest.mark.integration  # needs a VibeComfy checkout (ASTRID_VIBECOMFY_CHECKOUT)
def test_interrupted_upload_reconciles_remote_bytes_without_reupload(
    tmp_path: Path, source_fixture: Mapping[str, Any]
) -> None:
    setup = _setup(tmp_path, source_fixture)
    transport = LocalPreparationTransport()
    transport.fail_after_upload = str(source_fixture["astrid"])

    with pytest.raises(WorkerPreparationError, match="injected lost upload response"):
        _run(setup, transport)
    journal = _journal_path(Path(setup["handle_path"]).parent / "journals", "operation-test")
    saved = json.loads(journal.read_text(encoding="utf-8"))
    assert saved["status"] == "preparing"
    assert saved["steps"]["astrid"]["status"] == "intent"
    assert "cpu_readiness" not in saved

    result = _run(setup, transport)
    assert result["status"] == "cpu_ready"
    assert transport.uploads[str(source_fixture["astrid"])] == 1
    assert transport.uploads[str(source_fixture["vibecomfy"])] == 1


@pytest.mark.integration  # needs a VibeComfy checkout (ASTRID_VIBECOMFY_CHECKOUT)
def test_remote_drift_fails_closed_then_explicit_refresh_repairs_owned_source(
    tmp_path: Path, source_fixture: Mapping[str, Any]
) -> None:
    setup = _setup(tmp_path, source_fixture)
    transport = LocalPreparationTransport()
    first = _run(setup, transport)
    astrid_remote = Path(first["cpu_readiness"]["effective_paths"]["astrid_root"])
    target = astrid_remote / "astrid" / "__init__.py"
    target.unlink()
    target.write_text("# injected byte drift\n", encoding="utf-8")

    with pytest.raises(StagingEvidenceStaleError, match="previously verified remote astrid bytes changed"):
        _run(setup, transport)
    refreshed = _run(setup, transport, refresh=True)
    assert refreshed["status"] == "cpu_ready"
    assert transport.uploads[str(source_fixture["astrid"])] == 2
    assert "refresh_started_at" in json.loads(Path(first["journal_path"]).read_text(encoding="utf-8"))
    model = setup["model_root"] / "fixture-model.safetensors"
    model.write_bytes(b"model bytes drifted after preparation\n")
    with pytest.raises(WorkerPreparationError, match="model-root inventory"):
        _run(setup, transport)


@pytest.mark.integration  # needs a VibeComfy checkout (ASTRID_VIBECOMFY_CHECKOUT)
def test_workflow_pack_requires_its_unique_lock_pin_and_exact_checkout(
    tmp_path: Path, source_fixture: Mapping[str, Any]
) -> None:
    setup = _setup(tmp_path / "wrong-lock", source_fixture)
    manifest = json.loads(Path(setup["manifest_path"]).read_text(encoding="utf-8"))
    expected_commit = manifest["runtime"]["h3_custom_node_commit"]
    lock = Path(setup["lock"])
    lock.write_text(
        "[nodepacks.ComfyUI-H3-Motion-Context-MultiRef]\n"
        'name = "ComfyUI-H3-Motion-Context-MultiRef"\n'
        f'commit = "{"b" * 40}"\n\n'
        "[nodepacks.WrongPack]\nname = \"WrongPack\"\n"
        f'commit = "{expected_commit}"\n\n'
        "[nodepacks.ComfyUI-VideoHelperSuite]\n"
        'name = "ComfyUI-VideoHelperSuite"\n'
        f'git_commit_sha = "{manifest["runtime"]["videohelpersuite_commit"]}"\n',
        encoding="utf-8",
    )
    manifest["runtime"]["custom_nodes_lock_sha256"] = hashlib.sha256(
        lock.read_bytes()
    ).hexdigest()
    _rewrite_manifest(setup, manifest)
    transport = LocalPreparationTransport()
    with pytest.raises(WorkerPreparationError, match="lock commit differs from the manifest pin"):
        _run(setup, transport)

    setup = _setup(tmp_path / "wrong-checkout", source_fixture)
    selected_root = Path(setup["node_roots"]["h3_custom_node_commit"])
    alternate_root = selected_root.parent / "alternate" / selected_root.name
    wrong_commit = _git_repo(alternate_root, {"README.md": "different pinned content\n"})
    assert wrong_commit != json.loads(Path(setup["manifest_path"]).read_text(encoding="utf-8"))["runtime"]["h3_custom_node_commit"]
    setup["node_roots"]["h3_custom_node_commit"] = str(alternate_root)
    with pytest.raises(WorkerPreparationError, match="h3_custom_node_commit checkout differs"):
        _run(setup, LocalPreparationTransport())


@pytest.mark.integration  # needs a VibeComfy checkout (ASTRID_VIBECOMFY_CHECKOUT)
def test_manifest_pinned_workflow_pack_cannot_omit_its_checkout_root(
    tmp_path: Path, source_fixture: Mapping[str, Any]
) -> None:
    setup = _setup(tmp_path, source_fixture)
    transport = LocalPreparationTransport()
    args = _prepare_args(setup, transport)
    args["custom_node_roots"] = {
        "videohelpersuite_commit": setup["node_roots"]["videohelpersuite_commit"]
    }
    with pytest.raises(WorkerPreparationError, match="checkout path is required for runtime.h3_custom_node_commit"):
        asyncio.run(prepare_worker(**args))
    assert transport.uploads == {}


@pytest.mark.integration  # needs a VibeComfy checkout (ASTRID_VIBECOMFY_CHECKOUT)
@pytest.mark.parametrize("alias_kind", ["lock_leaf", "lock_parent", "node_leaf", "node_parent"])
def test_same_byte_custom_node_release_aliases_fail_before_resolved_reuse(
    tmp_path: Path, source_fixture: Mapping[str, Any], alias_kind: str
) -> None:
    setup = _setup(tmp_path, source_fixture)
    transport = LocalPreparationTransport()
    first = _run(setup, transport)

    if alias_kind == "lock_leaf":
        lock_path = Path(setup["lock"])
        copy = lock_path.with_name("custom_nodes.lock.copy")
        shutil.copyfile(lock_path, copy)
        lock_path.unlink()
        lock_path.symlink_to(copy)
        assert lock_path.read_bytes() == copy.read_bytes()
    elif alias_kind == "lock_parent":
        lock_path = Path(setup["lock"])
        original_parent = lock_path.parent
        copied_parent = original_parent.with_name(original_parent.name + "-copy")
        shutil.copytree(original_parent, copied_parent)
        original_lock = original_parent / lock_path.name
        copied_lock = copied_parent / lock_path.name
        assert original_lock.read_bytes() == copied_lock.read_bytes()
        shutil.rmtree(original_parent)
        original_parent.symlink_to(copied_parent, target_is_directory=True)
    elif alias_kind == "node_leaf":
        node_root = Path(setup["node_roots"]["h3_custom_node_commit"])
        copied_root = node_root.with_name(node_root.name + "-copy")
        shutil.copytree(node_root, copied_root)
        assert _git(node_root, "rev-parse", "HEAD") == _git(copied_root, "rev-parse", "HEAD")
        shutil.rmtree(node_root)
        node_root.symlink_to(copied_root, target_is_directory=True)
    else:
        node_parent = Path(setup["comfy"]) / "custom_nodes"
        copied_parent = Path(setup["candidate"]) / "custom_nodes-copy"
        shutil.copytree(node_parent, copied_parent)
        node_name = Path(setup["node_roots"]["h3_custom_node_commit"]).name
        assert _git(node_parent / node_name, "rev-parse", "HEAD") == _git(
            copied_parent / node_name, "rev-parse", "HEAD"
        )
        shutil.rmtree(node_parent)
        node_parent.symlink_to(copied_parent, target_is_directory=True)

    assert first["cpu_readiness"]["status"] == "cpu_ready"
    with pytest.raises(WorkerPreparationError, match="traverses a symlink or shadow path"):
        _run(setup, transport)


@pytest.mark.integration  # needs a VibeComfy checkout (ASTRID_VIBECOMFY_CHECKOUT)
def test_changed_config_corrupt_journal_and_account_or_volume_mismatch_refuse_reuse(
    tmp_path: Path, source_fixture: Mapping[str, Any]
) -> None:
    setup = _setup(tmp_path / "base", source_fixture)
    transport = LocalPreparationTransport()
    result = _run(setup, transport)
    uploads = dict(transport.uploads)

    changed = dict(setup["readiness"])
    changed["launch"] = dict(changed["launch"], output_root=str(setup["volume"] / "alternate"))
    with pytest.raises(PreparationJournalError, match="profile/config changed"):
        asyncio.run(prepare_worker(**{**_prepare_args(setup, transport), "readiness_profile": changed}))
    assert transport.uploads == uploads

    alternate_model = setup["volume"] / "models-alt"
    shutil.copytree(setup["model_root"], alternate_model)
    changed_model = json.loads(json.dumps(setup["readiness"]))
    changed_model["launch"]["model_root"]["path"] = str(alternate_model)
    with pytest.raises(PreparationJournalError, match="profile/config changed"):
        asyncio.run(prepare_worker(**{**_prepare_args(setup, transport), "readiness_profile": changed_model}))
    assert transport.uploads == uploads

    manifest = json.loads(Path(setup["manifest_path"]).read_text(encoding="utf-8"))
    manifest["runtime"]["comfyui_commit"] = "a" * 40
    Path(setup["manifest_path"]).write_text(json.dumps(manifest), encoding="utf-8")
    setup["manifest_sha"] = "sha256:" + hashlib.sha256(Path(setup["manifest_path"]).read_bytes()).hexdigest()
    with pytest.raises(PreparationJournalError, match="release/profile/config changed"):
        _run(setup, transport)
    assert transport.uploads == uploads

    journal = Path(result["journal_path"])
    journal.write_text("{broken\n", encoding="utf-8")
    with pytest.raises(PreparationJournalError, match="corrupt"):
        _run(setup, transport)

    handle = json.loads(Path(setup["handle_path"]).read_text(encoding="utf-8"))
    handle["provider_account_ref"] = "other-profile"
    Path(setup["handle_path"]).write_text(json.dumps(handle), encoding="utf-8")
    with pytest.raises(WorkerPreparationError, match="differs from exact P1 custody"):
        _run(setup, transport)


@pytest.mark.integration  # needs a VibeComfy checkout (ASTRID_VIBECOMFY_CHECKOUT)
def test_empty_remote_observation_and_model_shadow_remain_unresolved(
    tmp_path: Path, source_fixture: Mapping[str, Any]
) -> None:
    setup = _setup(tmp_path / "empty", source_fixture)
    transport = LocalPreparationTransport()
    transport.empty_probe = True
    with pytest.raises(WorkerPreparationError, match="observation was empty or malformed"):
        _run(setup, transport)
    assert transport.uploads == {}

    setup = _setup(tmp_path / "model", source_fixture)
    shadow = setup["model_root"] / "unlisted.safetensors"
    shadow.write_bytes(b"not in manifest")
    with pytest.raises(WorkerPreparationError, match="model-root inventory is incomplete"):
        _run(setup, LocalPreparationTransport())

    setup = _setup(tmp_path / "model-symlink", source_fixture)
    model = setup["model_root"] / "fixture-model.safetensors"
    (setup["model_root"] / "shadow.safetensors").symlink_to(model)
    with pytest.raises(WorkerPreparationError, match="model-root contains a symlink"):
        _run(setup, LocalPreparationTransport())


@pytest.mark.integration  # needs a VibeComfy checkout (ASTRID_VIBECOMFY_CHECKOUT)
def test_release_manifest_datacenter_and_account_reference_are_mandatory(
    tmp_path: Path, source_fixture: Mapping[str, Any]
) -> None:
    setup = _setup(tmp_path, source_fixture)
    handle = json.loads(Path(setup["handle_path"]).read_text(encoding="utf-8"))
    handle.pop("provider_account_ref")
    Path(setup["handle_path"]).write_text(json.dumps(handle), encoding="utf-8")
    with pytest.raises(WorkerPreparationError, match="no stable provider_account_ref"):
        _run(setup, LocalPreparationTransport())


@pytest.mark.integration  # needs a VibeComfy checkout (ASTRID_VIBECOMFY_CHECKOUT)
def test_manifest_volume_alias_is_optional_but_mismatch_and_effective_path_drift_fail_closed(
    tmp_path: Path, source_fixture: Mapping[str, Any]
) -> None:
    setup = _setup(tmp_path / "alias", source_fixture)
    manifest = json.loads(Path(setup["manifest_path"]).read_text(encoding="utf-8"))
    manifest["volume"]["datacenter"] = "EU-RO-2"
    Path(setup["manifest_path"]).write_text(json.dumps(manifest), encoding="utf-8")
    setup["manifest_sha"] = "sha256:" + hashlib.sha256(Path(setup["manifest_path"]).read_bytes()).hexdigest()
    transport = LocalPreparationTransport()
    with pytest.raises(WorkerPreparationError, match="selected release volume does not match"):
        _run(setup, transport)
    assert transport.uploads == {}

    manifest["volume"]["datacenter"] = "EU-RO-1"
    manifest["volume"]["name"] = "wrong-alias"
    Path(setup["manifest_path"]).write_text(json.dumps(manifest), encoding="utf-8")
    setup["manifest_sha"] = "sha256:" + hashlib.sha256(Path(setup["manifest_path"]).read_bytes()).hexdigest()
    transport = LocalPreparationTransport()
    with pytest.raises(WorkerPreparationError, match="selected release volume does not match"):
        _run(setup, transport)
    assert transport.uploads == {}

    setup = _setup(tmp_path / "path", source_fixture)
    readiness = dict(setup["readiness"])
    readiness["launch"] = dict(readiness["launch"], python_executable="/tmp/other/python")
    transport = LocalPreparationTransport()
    with pytest.raises(WorkerPreparationError, match="python_executable differs"):
        asyncio.run(prepare_worker(**{**_prepare_args(setup, transport), "readiness_profile": readiness}))
    assert transport.uploads == {}


@pytest.mark.integration  # needs VibeComfy (ASTRID_VIBECOMFY_CHECKOUT) and Runtime (ASTRID_RUNTIME_CHECKOUT) checkouts
def test_json_selection_uses_production_composition_and_real_cpu_staging(
    tmp_path: Path,
    source_fixture: Mapping[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise file decoding -> production owners -> real P2 with fake I/O."""
    import runpod_lifecycle

    use_runtime()
    from runtime_protocol.remote_worker_deployment import deployment_binding_from_task

    import astrid.packs.runpod.worker_session as worker_session
    import astrid.packs.runpod.worker_staging as worker_staging
    from astrid.core.util.credentials_scope import CredentialsScope
    from astrid.packs.runpod.prepared_task import (
        PreparedWorkerError,
        load_prepared_worker_selection,
    )
    from astrid.packs.runpod.worker_staging import _remote_owned_root, _tree_inventory

    setup = _setup(tmp_path, source_fixture)
    # The selected profile models the ordinary release layout, where the
    # interpreter is a venv entry point. Use a regular executable shim because
    # Runtime artifact references reject symlinks; the fake child boundary
    # forwards its arguments to the test interpreter without device checks.
    selected_python = Path(setup["python"])
    selected_python.unlink()
    selected_python.write_text(
        f"#!/bin/sh\nexec \"{sys.executable}\" \"$@\"\n",
        encoding="utf-8",
    )
    selected_python.chmod(0o755)
    claim = json.loads(Path(setup["handle_path"]).read_text(encoding="utf-8"))
    account = claim["provider_account_ref"]
    target = {
        "kind": "runpod",
        "pod_id": claim["pod_id"],
        "provider_account_ref": account,
        "storage": {
            "network_volume_id": claim["network_volume_id"],
            "name": claim["network_volume_name"],
        },
    }
    owned_root = Path(_remote_owned_root({
        "operation_id": claim["operation_id"],
        "volume_mount_path": claim["volume_mount_path"],
    }))
    digest = "sha256:" + hashlib.sha256(b"selected deployment artifact").hexdigest()
    executable = {"name": "python", "path": str(selected_python), "digest": digest}
    runtime_health = {
        "runtime_instance_id": "runtime-fixture",
        "runtime_session_id": "session-fixture",
        "runtime_epoch": 3,
        "schema_digest": "sha256:" + "a" * 64,
    }
    session_ref = "prepared-session-fixture"
    session_runtime_root = str(Path(setup["comfy_root"]).parent)
    session_values = {
        "runtime_root": session_runtime_root,
        "cwd": session_runtime_root,
        "port": 8188,
        "warm_policy": "never",
        "base_directory": str(setup["comfy_root"]),
        "models_root": str(setup["model_root"]),
        "models_root_normalized": str(setup["model_root"]),
        "output_directory": str(setup["output_root"]),
        "locality": "managed_local_server",
        "server_log_path": str(
            Path(session_runtime_root) / "out" / "sessions" / session_ref / "comfy.log"
        ),
    }
    session_config_digest = worker_session._digest(session_values)
    profile = {
        "schema": "astrid.runpod.deployment-profile.v1",
        "reference": {
            "deployment_id": "runpod-deployment-fixture",
            "revision": "fixture-r1",
            "source_closure_digest": digest,
            "data_root": str(owned_root),
            "support_root": str(owned_root / "runtime"),
            "runtime_schema_digest": runtime_health["schema_digest"],
            "model_root": str(setup["model_root"]),
            "capacity": 1,
            "session_ref": session_ref,
            "session_config_digest": session_config_digest,
            "output_root": str(setup["output_root"]),
            "credential_ref": str(owned_root / "runtime" / "credentials" / "executor.token"),
            "executor_id": "astrid-pack-host",
            "boot_manifest_path": str(owned_root / "runtime" / "boot-manifest.json"),
            "boot_manifest_hash": digest,
            "readiness_profile_path": str(owned_root / "runtime" / "readiness.json"),
            "readiness_profile_hash": digest,
            "source_checkout_digest": _tree_inventory(Path(source_fixture["astrid"]))[0].removeprefix("sha256:"),
            "executable": executable,
            "dependency_closure": [executable],
            "pack_roots": ["astrid/packs"],
        },
    }
    profile_path = tmp_path / "deployment-profile.json"
    profile_path.write_text(json.dumps(profile), encoding="utf-8")
    staging = {
        **_prepare_args(setup, LocalPreparationTransport()),
    }
    staging.pop("claim_handle_path")
    staging.pop("journal_dir")
    staging.pop("provider_account_ref")
    staging.pop("transport")
    staging.pop("workflow_path")
    staging.pop("workflow_inputs")
    staging.pop("capability_id")
    staging.pop("workflow_bundle_path", None)
    # The H3 caller contributes its exact compiled operation-owned bundle at
    # bind time. Keep P2's staging JSON limited to selected release inputs.
    staging_path = tmp_path / "staging-inputs.json"
    staging_path.write_text(json.dumps(staging, default=str), encoding="utf-8")
    selection_path = tmp_path / "selection.json"
    selection_path.write_text(json.dumps({
        "schema": "astrid.runpod.prepared-worker.v1",
        "claim_handle_path": str(setup["handle_path"]),
        "deployment_profile_path": str(profile_path),
        "staging_inputs_path": str(staging_path),
        "journal_dir": str(tmp_path / "journals"),
    }), encoding="utf-8")
    selection = load_prepared_worker_selection(selection_path)
    assert isinstance(selection.staging_inputs["release_manifest_path"], Path)
    assert isinstance(selection.staging_inputs["astrid_source"], Path)
    assert isinstance(selection.staging_inputs["vibecomfy_source"], Path)

    credential_calls: list[tuple[str, str | None]] = []
    def resolve_credential(provider: str, *, env_var: str | None = None, **_kwargs: Any):
        credential_calls.append((provider, env_var))
        return SimpleNamespace(value="fixture-secret-never-logged")

    monkeypatch.setattr(CredentialsScope, "resolve_local", staticmethod(resolve_credential))
    config_calls: list[dict[str, Any]] = []
    fake_config = SimpleNamespace(api_key="fixture-secret-never-logged")
    monkeypatch.setattr(
        runpod_lifecycle.RunPodConfig,
        "from_env",
        classmethod(lambda cls, **kwargs: (config_calls.append(dict(kwargs)) or fake_config)),
    )

    async def fake_get_pod(pod_id: str, config: Any, *, name: str):
        return SimpleNamespace(id=pod_id, config=config)

    monkeypatch.setattr(runpod_lifecycle, "get_pod", fake_get_pod)
    transport = LocalPreparationTransport()
    monkeypatch.setattr(worker_staging, "RunPodPreparationTransport", lambda _pod: transport)
    monkeypatch.setattr(worker_session, "RunPodVibeComfySessionTransport", lambda *_args, **_kwargs: object())
    monkeypatch.setenv("ASTRID_RUNPOD_ACCOUNT_REF", account)

    class Client:
        endpoint = "http://127.0.0.1:59683"

        def __init__(self) -> None:
            self._remote = SimpleNamespace(_transport=SimpleNamespace(
                health=lambda: dict(runtime_health),
                get_executor=lambda _executor_id: None,
            ))

        def health(self) -> dict[str, Any]:
            return dict(runtime_health)

    client = Client()
    source_workflow_path = Path(source_fixture["workflow"])
    workflow_bundle = tmp_path / "prepared-workflow-bundle"
    workflow_bundle.mkdir()
    for source_path, name in (
        (source_workflow_path, "workflow.py"),
        (source_workflow_path.with_suffix(".vibe.json"), "workflow.vibe.json"),
        (source_workflow_path.with_name("source.json"), "source.json"),
    ):
        shutil.copy2(source_path, workflow_bundle / name)
    workflow_path = workflow_bundle / "workflow.py"
    bound = selection.bind_client(
        client,
        workflow_path=workflow_path,
        workflow_bundle_path=workflow_bundle,
        workflow_inputs={},
    )
    # Match the operator's ID-only execution-request artifact. Normalize it
    # to P1's full canonical target (which also carries the selected label)
    # before the real DeploymentReference join checks exact dictionary identity.
    task_request = selection.execution_request({
        "target": {
            "kind": "runpod",
            "pod_id": claim["pod_id"],
            "provider_account_ref": account,
            "storage": {"network_volume_id": claim["network_volume_id"]},
        },
        "lifecycle": {"mode": "leave_running"},
    })
    assert task_request["target"] == target
    for changed in (
        {"provider_account_ref": "other-account"},
        {"pod_id": "other-pod"},
        {"storage": {"network_volume_id": "other-volume"}},
        {"storage": {"network_volume_id": claim["network_volume_id"], "name": "other-name"}},
    ):
        rejected_target = {
            "kind": "runpod",
            "pod_id": claim["pod_id"],
            "provider_account_ref": account,
            "storage": {"network_volume_id": claim["network_volume_id"]},
            **changed,
        }
        with pytest.raises(PreparedWorkerError):
            selection.execution_request({"target": rejected_target})
    capability_digest = "sha256:" + "b" * 64
    task = {
        "id": "task-fixture",
        "run_id": "run-fixture",
        "project_id": "project-fixture",
        "idempotency_key": "admission-fixture",
        "capability_id": "vibecomfy.run",
        "capability_digest": capability_digest,
        "input_object_ids": [],
        "spec": {
            "schema_version": 1,
            "input_object_ids": [],
            "capability_digest": capability_digest,
            "execution_request": task_request,
            "spec": {},
        },
        "execution_request": task_request,
        "execution_binding": {
            "original_target": target,
            "effective_target": target,
            "resolved_target": target,
            "placement_version": 0,
        },
    }
    assert deployment_binding_from_task(task).placement.effective_target == target
    reference, preparer, _runtime_owner, _launcher, _pod = bound._runtime._components(task)
    result = preparer._stage(reference)

    assert credential_calls == [("runpod", "RUNPOD_API_KEY")]
    assert config_calls == [{"api_key": "fixture-secret-never-logged"}]
    assert result["status"] == "cpu_ready"
    assert result["gpu_qualified"] is False
    assert result["worker_qualified"] is False
    assert result["cpu_readiness"]["effective_paths"]["astrid_root"] == str(reference.source_checkout)
    assert transport.child_calls == 1
    selected_session = worker_session.RunPodManagedVibeComfySessionOwner(
        object(),
    ).select(reference, result["cpu_readiness"], port=8188)
    assert selected_session.runtime_root == session_runtime_root
    assert selected_session.comfy_root == str(setup["comfy_root"])
    assert selected_session.config_digest == session_config_digest
