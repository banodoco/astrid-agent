"""Observe LS-6 before/after counts without changing product or strict tests.

Run this same file with PYTHONPATH and the checkout environment pointing at
either three isolated bases or three candidate checkouts. The Comfy response is
an attributed byte fixture; collection, custody, upload, Runtime settlement,
Runtime serving, SDK verification and independent caller delivery are real.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import ExitStack
from dataclasses import asdict, is_dataclass
import hashlib
import importlib
import inspect
import io
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import threading
import tomllib
import traceback
from types import SimpleNamespace
from unittest.mock import patch


SIZES = (37, 9 * 1024 * 1024 + 13)
PREFIXES = {
    "agent": ("astrid", "tests._local_speed_hashes"),
    "runtime": ("runtime_protocol",),
    "vibe": ("vibecomfy",),
}


def _git(root: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


def _file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _repo_identity(root: Path) -> dict:
    project_file = root / "pyproject.toml"
    project = tomllib.loads(project_file.read_text()).get("project", {}) if project_file.is_file() else {}
    diff = subprocess.check_output(["git", "-C", str(root), "diff", "--binary", "HEAD"])
    untracked = _git(root, "ls-files", "--others", "--exclude-standard").splitlines()
    return {
        "root": str(root),
        "head": _git(root, "rev-parse", "HEAD"),
        "tree": _git(root, "rev-parse", "HEAD^{tree}"),
        "status": _git(root, "status", "--porcelain=v1", "--untracked-files=normal").splitlines(),
        "tracked_diff_sha256": hashlib.sha256(diff).hexdigest(),
        "untracked_files": {name: _file_digest(root / name) for name in untracked if (root / name).is_file()},
        "project_name": project.get("name"),
        "project_version": project.get("version"),
        "pyproject_sha256": _file_digest(project_file) if project_file.is_file() else None,
    }


def _selected_modules(roots: dict[str, Path]) -> dict:
    selected = {}
    for name, module in sorted(sys.modules.items()):
        owner = next((key for key, prefixes in PREFIXES.items()
                      if any(name == prefix or name.startswith(prefix + ".") for prefix in prefixes)), None)
        filename = getattr(module, "__file__", None)
        if owner is None or filename is None:
            continue
        path = Path(filename).resolve()
        assert path.is_relative_to(roots[owner]), f"{name} loaded outside selected {owner} checkout: {path}"
        selected[name] = {"path": str(path), "sha256": _file_digest(path), "checkout": owner}
    return selected


def _produce(work_dir: Path, data: bytes, *, task_id: str, attempt_id: str, checkpoint):
    from astrid.core.generation.backends.comfy_target import ComfyTarget, ComfyTargetClient
    from astrid.core.generation.backends.vibecomfy_local_server import AttachedArtifacts
    from astrid.core.generation.producer_result import _read_correlated_result
    from vibecomfy.runtime.session import _build_managed_generation_result

    materializer = importlib.import_module("astrid.packs.vibecomfy.actions.run.run")
    vibe_run = importlib.import_module("vibecomfy.runtime.run")
    run_dir = work_dir / "producer"
    run_dir.mkdir(parents=True)
    descriptor = {"filename": "final.png", "subfolder": "", "type": "output", "animated": False}
    artifact = {"filename": "final.png", "reported_path": "final.png", "descriptor": descriptor}
    journal = {"prompt_id": "prompt-1", "queue_acceptance": {"status": "accepted", "prompt_id": "prompt-1"},
               "artifacts": [artifact]}
    (run_dir / "attempt.json").write_text(json.dumps(journal))
    fetches = []

    class Response(io.BytesIO):
        status = 200
        headers = {"Content-Length": str(len(data))}

    def opener(request, **_kwargs):
        fetches.append(request.full_url)
        return Response(data)

    client = ComfyTargetClient(ComfyTarget("http://127.0.0.1:8188", "attached"), opener=opener)
    client.witness_prompt("prompt-1")
    retrieval = asyncio.run(AttachedArtifacts(client).download_artifacts(local_root=run_dir / "downloaded-artifacts"))
    assert retrieval["status"] == "retrieved"
    assert len(fetches) == 1 and "/view?" in fetches[0]
    checkpoint("download")

    # Both versions execute their ordinary binder. Only the candidate exposes
    # the receipt handoff; the baseline retains all its existing hash checks.
    binder = vibe_run._bind_downloaded_lifecycle_artifacts
    parameters = inspect.signature(binder).parameters
    receipt_kwargs = {}
    if "output_receipts" in parameters and "output_receipts" in retrieval:
        receipt_kwargs["output_receipts"] = retrieval["output_receipts"]
    if "prompt_id" in parameters:
        receipt_kwargs["prompt_id"] = "prompt-1"
    assert binder([artifact], Path(retrieval["local_root"]), **receipt_kwargs) == 1
    metadata = {
        "run_id": "producer-1", "task_id": task_id, "attempt_id": attempt_id,
        "created": "2026-11-18T00:00:00Z", "prompt_id": "prompt-1", "queued": {"prompt_id": "prompt-1"},
        "inputs": {}, "artifacts": [artifact], "media_validation": {"status": "verified"},
        "declared_outputs": [{"name": "image", "mime_type": "image/png", "declaration_ordinal": 0}],
        "artifact_manifest": {"attribution": [{"path": "final.png", "output": "image"}]},
    }
    payload, unavailable = _build_managed_generation_result(
        run_dir=run_dir, metadata=metadata, attempt_bundle=journal,
        evidence={"queue_acceptance": journal["queue_acceptance"]},
    )
    assert unavailable == {} and payload is not None
    checkpoint("vibe_result")
    envelope = run_dir / "managed-generation-result.json"
    envelope.write_text(json.dumps(payload))
    _read_correlated_result(envelope, task_id=task_id, attempt_id=attempt_id)
    checkpoint("correlated_result")
    spool = work_dir / "host" / "outputs"
    manifest = materializer._materialize_managed_generation_result(
        spool, SimpleNamespace(managed_generation_result_path=envelope),
        task_identity=task_id, attempt_identity=attempt_id, workflow_path=work_dir / "unused.json",
    )
    checkpoint("materialized_result")
    return spool, manifest, fetches, {"binder_receipt_kwargs": sorted(receipt_kwargs)}


def _measure(work_dir: Path, size: int) -> dict:
    from astrid.core._shared.result_manifest import harvest_staged_outputs
    from astrid.core.execution.generic_host import GenericPackHost, RuntimeProtocolClient
    from astrid.core.generation.delivery import deliver_generation
    from astrid.sdk.remote import RemoteMedia
    from astrid.sdk.workspace_client import WorkspaceClient
    from runtime_protocol.daemon import RuntimeDaemon
    from runtime_protocol.store import RealmStore
    from tests._local_speed_hashes import OutputHashObserver

    data = (b"attributed-output-" * ((size + 17) // 18))[:size]
    assert len(data) == size
    digest = hashlib.sha256(data).hexdigest()
    definition_digest = "sha256:" + hashlib.sha256(b"vibecomfy.run").hexdigest()
    realm = work_dir / "realm"
    RealmStore.initialize(realm).close()
    daemon = RuntimeDaemon(realm, support_root=work_dir / "support").start()
    try:
        service = daemon.service
        project = service.create_project({"slug": "receipt", "name": "Receipt"}, idempotency_key="project")
        source = service.ingest(project["id"], b"admitted workflow", media_type="application/json",
                                idempotency_key="input")["data"]["digest"]
        token, _ = daemon.credentials.provision("receipt-worker", list(RuntimeProtocolClient.WORKER_SCOPES))
        worker = RuntimeProtocolClient(daemon.endpoint, token)
        worker.register_executor(
            "receipt-worker",
            capabilities=[{"capability_id": "vibecomfy.run", "definition_digest": definition_digest,
                "status": "ready", "required_resource_keys": [], "estimated_scratch_bytes": 0,
                "estimated_output_bytes": size * 2 + 1024 * 1024}],
            max_concurrency=1, resource_keys=[],
            source_digest=hashlib.sha256(b"ls6 fixture source").hexdigest(),
            dependency_digest=hashlib.sha256(b"ls6 fixture dependencies").hexdigest(),
            source_epoch=hashlib.sha256(b"ls6 fixture epoch").hexdigest(),
            runtime_epoch=service.health()["runtime_epoch"],
        )
        intent = {"version": 1, "modality": "image", "metadata": {}, "partial_success_policy": "reject",
            "groups": [{"group_key": "main", "selectors": [
                {"selector": "image", "ordinal": 0, "variant_key": "original"}]}]}
        effect = {"effect_type": "generation.publish_v1", "target_id": project["id"], "payload": {
            **intent, "generation_type": "vibecomfy.run", "groups": [{"group_key": "main", "selectors": [
                {"selector": "image", "ordinal": 0, "variant_key": "original", "output_port": "vibecomfy_run"}]}]}}
        service.create_task({"capability_id": "vibecomfy.run", "capability_digest": definition_digest,
            "project": project["id"], "input_object_ids": [source], "settlement_effect": effect,
            "generation_intent": intent, "idempotency_key": "primary"})
        claim = worker.claim_next(executor_id="receipt-worker", capability_ids=["vibecomfy.run"], idempotency_key="claim")
        claim = asdict(claim) if is_dataclass(claim) else dict(claim)
        observers = {name: OutputHashObserver({digest: size})
                     for name in ("producer", "runtime_pre_delivery", "runtime_transmit", "caller")}
        phase = ["producer"]
        main_thread = threading.get_ident()
        checkpoints = {}

        def selected():
            if phase[0] == "producer":
                return observers["producer"]
            if phase[0] == "runtime_query":
                return observers["runtime_pre_delivery"]
            if phase[0] == "upload":
                return observers["producer" if threading.get_ident() == main_thread else "runtime_pre_delivery"]
            assert phase[0] == "delivery"
            return observers["caller" if threading.get_ident() == main_thread else "runtime_transmit"]

        def checkpoint(name):
            checkpoints[name] = asdict(observers["producer"].snapshot())

        with ExitStack() as patches:
            for name in ("sha256", "new", "file_digest"):
                patches.enter_context(patch.object(hashlib, name,
                    lambda *args, _name=name, **kwargs: getattr(selected(), _name)(*args, **kwargs)))
            spool, manifest, fetches, features = _produce(
                work_dir, data, task_id=claim["task_id"], attempt_id=claim["attempt_id"], checkpoint=checkpoint,
            )
            declared = (
                SimpleNamespace(name="vibecomfy_run", type="file", path_template="outputs/final.png", artifact_type="image/png"),
                SimpleNamespace(name="result_manifest", type="file", path_template="outputs/managed-generation-result.json", artifact_type="application/json"),
            )
            harvest_kwargs = {"declared_outputs": declared}
            if "use_output_receipts" in inspect.signature(harvest_staged_outputs).parameters:
                harvest_kwargs["use_output_receipts"] = True
            features["harvest_receipt_option"] = "use_output_receipts" in harvest_kwargs
            harvested = harvest_staged_outputs(spool, **harvest_kwargs)
            checkpoint("harvest")
            host = object.__new__(GenericPackHost)
            host.client = worker
            host._validate_producer_evidence(harvested, output_root=spool, task_id=claim["task_id"],
                                             attempt_id=claim["attempt_id"], successful=True)
            checkpoint("host_validation")
            record = SimpleNamespace(definition=SimpleNamespace(outputs=declared, metadata={}))
            typed = host._typed_outputs(record, harvested, spool.parent, generation_intent=intent,
                                        defer_content_check_to_runtime=True)
            checkpoint("typed_outputs")
            phase[0] = "upload"
            outputs = host._upload_outputs(typed, project_id=project["id"], run_id=claim["run_id"],
                task_id=claim["task_id"], attempt_id=claim["attempt_id"], lease_id=claim["lease_id"],
                fence=claim["fence"], runtime_epoch=claim["runtime_epoch"])
            checkpoint("uploaded_outputs")
            result = worker.generated.settle_attempt(claim["attempt_id"], {"effect": effect, "outputs": outputs,
                **{key: claim[key] for key in ("lease_id", "fence", "runtime_epoch")}}, idempotency_key="settle")
            assert result["state"] == "succeeded"
            checkpoint("settled")
            phase[0] = "runtime_query"
            task = service.store.get_task(claim["task_id"])["task"]
            assert task["status"] == "completed"
            rows = service.managed_outputs(claim["task_id"])
            primary = next(row for row in rows if row["role"] == "result")
            envelope = next(row for row in rows if row["role"] == "manifest")
            assert primary["object_id"] == "sha256:" + digest
            thumbnail_intents = task["result"].get("thumbnail_intents", [])
            if thumbnail_intents:
                assert thumbnail_intents[0]["source_object_id"] == "sha256:" + digest
            runtime_before = asdict(observers["runtime_pre_delivery"].snapshot())
            opens = []
            original_open = service.open_object

            def opened(object_id, **kwargs):
                if object_id.removeprefix("sha256:") == digest:
                    opens.append(object_id)
                return original_open(object_id, **kwargs)

            patches.enter_context(patch.object(service, "open_object", opened))
            phase[0] = "delivery"
            client = SimpleNamespace(media=RemoteMedia(WorkspaceClient(daemon.endpoint, daemon.token)))
            delivered = deliver_generation({"task_id": claim["task_id"], "project_id": project["id"],
                "manifest_ref": envelope["object_id"], "manifest": {"outputs": [primary]}},
                client=client, out=work_dir / "delivered.png", project=project["id"])
            caller_file = Path(delivered["image_paths"][0])
            assert caller_file.read_bytes() == data
            assert len(opens) == 1
            stored = service.cas.root / digest[:2] / digest[2:]
            assert not os.path.samefile(caller_file, stored)
            caller_file.write_bytes(b"caller mutation")
            assert caller_file.read_bytes() == b"caller mutation"
            assert stored.read_bytes() == data
            snapshots = {name: asdict(observer.snapshot()) for name, observer in observers.items()}
            assert all(not value["coverage_gaps"] for value in snapshots.values())
            assert snapshots["producer"]["completed_streams"] >= 1
            assert snapshots["runtime_pre_delivery"]["completed_streams"] >= 1
            assert snapshots["caller"]["completed_streams"] >= 1
            assert snapshots["runtime_pre_delivery"] == runtime_before
            observation = {
                "bytes": size, "sha256": digest, "fetch_count": len(fetches), "fetch_urls": fetches,
                "runtime_primary_open_count": len(opens), "settlement_state": result["state"],
                "task_status": task["status"], "thumbnail_intent_count": len(thumbnail_intents),
                "cas_unchanged_after_caller_mutation": True, "independent_caller_file": True,
                "features": features, "producer_checkpoints": checkpoints, **snapshots,
            }
        return observation
    finally:
        daemon.stop()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("agent", "runtime", "vibe"):
        parser.add_argument(f"--{name}-root", required=True, type=Path)
    parser.add_argument("--label", required=True)
    parser.add_argument("--work-dir", required=True, type=Path)
    parser.add_argument("--output-json", required=True, type=Path)
    args = parser.parse_args()
    roots = {name: getattr(args, f"{name}_root").resolve() for name in PREFIXES}
    assert Path(os.environ["ASTRID_RUNTIME_CHECKOUT"]).resolve() == roots["runtime"]
    assert Path(os.environ["ASTRID_VIBECOMFY_CHECKOUT"]).resolve() == roots["vibe"]
    assert not args.output_json.exists(), "do not overwrite earlier counter evidence"
    args.work_dir.mkdir(parents=True, exist_ok=False)
    before = {name: _repo_identity(root) for name, root in roots.items()}
    report = {
        "schema_version": 1, "label": args.label,
        "harness": {"path": str(Path(__file__).resolve()), "sha256": _file_digest(Path(__file__))},
        "python": {"executable": sys.executable, "version": sys.version, "platform": platform.platform()},
        "checkouts_before": before, "measurements": [],
        "instrumentation": {
            "observer": "tests._local_speed_hashes.OutputHashObserver",
            "constructors": ["hashlib.sha256", "hashlib.new", "hashlib.file_digest"],
            "match": "complete SHA-256 streams matched by digest and byte count",
            "attribution": "all collection threads are producer; during upload/delivery the main thread is client and other threads are Runtime",
            "limitations": [
                "Python in-process observation; native hashing, subprocess hashing and direct cached constructor aliases are not covered",
                "Comfy inference and its HTTP response are a fixed attributed byte fixture",
                "Real product collection/custody, Runtime HTTP transfer/settlement, SDK verification and caller file delivery",
                "Counts are not latency measurements or a live-model/remote-GPU qualification",
            ],
        },
    }
    print(json.dumps({"event": "start", "label": args.label, "harness": report["harness"],
        "checkouts": {name: {key: value[key] for key in ("head", "tree", "tracked_diff_sha256")}
                      for name, value in before.items()}}, sort_keys=True), flush=True)
    exit_code = 0
    try:
        with patch.dict(os.environ, {"ASTRID_INTERNAL_INVOCATION": "1"}):
            for size in SIZES:
                observation = _measure(args.work_dir / f"output-{size}", size)
                report["measurements"].append(observation)
                print(json.dumps({"event": "measurement", **observation}, sort_keys=True), flush=True)
        report["status"] = "completed"
    except Exception as exc:
        report["status"] = "failed"
        report["error"] = {"type": type(exc).__name__, "message": str(exc)}
        traceback.print_exc()
        exit_code = 1
    finally:
        report["selected_modules"] = _selected_modules(roots)
        report["checkouts_after"] = {name: _repo_identity(root) for name, root in roots.items()}
        report["code_unchanged"] = before == report["checkouts_after"]
        if not report["code_unchanged"]:
            report["status"] = "code_changed_during_observation"
            exit_code = 1
        args.output_json.parent.mkdir(parents=True, exist_ok=True)
        with args.output_json.open("x") as stream:
            json.dump(report, stream, indent=2, sort_keys=True)
            stream.write("\n")
        print(json.dumps({"event": "finish", "status": report["status"],
            "code_unchanged": report["code_unchanged"], "output_json": str(args.output_json)}, sort_keys=True), flush=True)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
