"""CPU RunPod-as-a-machine journeys: real Runtime, host, lease owner and HTTPS transport.

Plan 32 T5. Only provider HTTP is doubled: RunPod GraphQL/REST and the pod agent.
These provider journeys answer the pod-side generation command with a fake:
it writes the job's output tree,
``result.json``, a real ``wrapper-manifest.json`` with its sha256 line, and
``progress.json`` once per poll. No ``/engines/`` request is ever made.

Journeys: a cold run then a warm second run; a custody change that refuses
reuse; cancel mid-poll; a bad mount mapping that fails before install.
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import re
import shlex
import socket
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
import requests
from PIL import Image

from astrid.core.execution.generic_host import GenericPackHost, RuntimeProtocolClient
from astrid.core.generation import engine_recipe, engine_runner
from astrid.core.generation.delivery import deliver_generation
from astrid.packs.runpod import remote_placement, target_selection
from astrid.packs.runpod.cuda_versions import DEFAULT_ALLOWED_CUDA_VERSIONS
from astrid.sdk import AstridClient
from astrid.sdk.generation import GenerationFacade
from astrid.sdk.remote import RemoteAstridClient
from astrid.sdk.workspace_client import WorkspaceClient
from tests.integrations.test_runpod_host_endpoint_r5 import tuple_sources  # noqa: F401  (fixture)

pytestmark = pytest.mark.integration

POD = "cpupod"
VOLUME = {"id": "volcpu1", "name": "cpu-models", "size_gb": 100, "mount_path": "/workspace"}
DC = "DC-unpinned"
#: Pod polls before the fake generation job exits (each poll writes one phase).
PHASES = ({"phase": "align"}, {"phase": "submit"}, {"phase": "executing", "current": 1, "total": 1},
          {"phase": "fetch"})


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def tuple_pins(source_root):
    """The (repository, commit) pins of the shared source tuple; no installed release manifest."""
    return {directory: ("https://example.invalid/" + Path(directory).name, subprocess.check_output(
        ["git", "-C", str(Path(source_root) / directory), "rev-parse", "HEAD"], text=True).strip())
        for directory in remote_placement.SOURCE_DIRS}


class MachineProviderHTTP:
    """RunPod GraphQL/REST and pod-agent contracts for the machine route."""

    def __init__(self, claim_path, attempts, *, volume=None):
        self.claim_path, self.attempts, self.volume = claim_path, attempts, volume
        self.events, self.commands, self.pods, self.files = [], [], [], {}
        self.agent_jobs, self.detached = {}, {}
        self.deploys = 0
        self.pod_id = POD  # the current (latest) pod; each deploy gets a new id
        self.killed = []
        self.mount_id, self.mount_path = 7, (volume or {}).get("mount_path")
        self.expect_entry = "cpu-image"  # None: any entry, or a shipped workflow
        self.generations = []  # job.json of every generation job the pod ran
        self.selections = []  # selection.json of every shipped selection the pod read
        self.seen_controller_progress = []
        # The pod's one Runtime (T5f): container-disk state of the current pod.
        self.fail_reset = False
        self.job_failure = None  # (exit code, stdout bytes): the generation job fails instead of finishing
        self.corrupt_manifest = False  # the job prints a manifest digest the uploaded manifest does not match
        self.no_stock = False  # the data-centre stock query reports nothing available
        self.resets = []  # each detached pod_reset: when, what it removed, the lease then
        self._new_pod_state()
        self.job_hold = threading.Event()
        self.job_hold.set()
        image = io.BytesIO()
        Image.new("RGB", (16, 16), (12, 34, 56)).save(image, format="PNG")
        self.image = image.getvalue()
        self.receipts = {}
        self.receipt = self._receipt_for("comfyui")

    # -- evidence ------------------------------------------------------------

    def _receipt_for(self, engine):
        """The pod's install receipt for a RunPod engine, from that engine's own recipe and lock."""
        if engine not in self.receipts:
            self.receipts[engine] = self._install_receipt(engine)
        return self.receipts[engine]

    def _install_receipt(self, engine, root=None):
        from dataclasses import asdict
        from astrid.core.generation.engine_contracts import (
            DownloadResult, HealthEvidence, InstallReceipt, StepReceipt,
        )
        recipe = engine_recipe.load_recipe(engine, "linux-x86_64-cu130")
        root = root or f"/root/astrid/engines/{engine}"
        plan = engine_runner.plan_install(recipe, root=root, placement="local")
        lock = engine_recipe.load_model_lock(recipe.models_lock)
        stamp = datetime.now(timezone.utc).isoformat()
        receipt = InstallReceipt(
            "astrid.engine-install.v1", f"cpu-http-install-{engine}", plan.engine, plan.platform, plan.placement,
            root, "installed", False, plan.recipe_sha256, plan.models_lock_sha256,
            {env.name: (env.requirements.packaged_lock.sha256 if env.requirements.packaged_lock
                        else _sha(env.name.encode())) for env in recipe.environments},
            {source.name: source.pin.revision for source in recipe.sources},
            tuple(DownloadResult(model.name, root + "/models/" + model.destination, "downloaded",
                                 model.bytes, model.sha256, True, 0) for model in lock.models),
            tuple(StepReceipt(step.id, step.kind, "executed", "passed", {}) for step in plan.steps),
            HealthEvidence("healthy", stamp, _sha(b"{}"), True), stamp, stamp, None,
        )
        engine_runner._parse_receipt(asdict(receipt), plan)
        return asdict(receipt)

    def _new_pod_state(self):
        self.pod_state, self.active_job, self.template = "absent", None, False

    def kinds(self, *names):
        return [c for c in self.commands if any(name in c for name in names)]

    # -- provider ------------------------------------------------------------

    def graphql(self, body):
        query = body["query"]
        variables = body.get("variables", {})
        self.events.append(("graphql", query.split("{")[0].strip(), variables))
        if "GpuCatalogue" in query:
            return {"gpuTypes": [{"id": "gpu-exact", "displayName": "CPU fixture GPU"}]}
        if "DataCenterStock" in query:
            assert variables["availability"]["allowedCudaVersions"] == list(DEFAULT_ALLOWED_CUDA_VERSIONS)
            return {"dataCenters": [{"id": DC, "storageSupport": self.volume is not None,
                    "gpuAvailability": [{"gpuTypeId": "gpu-exact", "available": not self.no_stock,
                                         "stockStatus": "None" if self.no_stock else "High"}]}]}
        if "DataCenterPrices" in query:
            return {"gpuTypes": [{"id": "gpu-exact", "dc0": {"stockStatus": "High", "uninterruptablePrice": 1.0}}]}
        if "gpuTypes(input:" in query:
            requested = re.search(r'id: "([^"]+)"', query)[1]
            assert requested in {"gpu-exact", "CPU fixture GPU"}
            return {"gpuTypes": [{"id": requested, "displayName": "CPU fixture GPU"}]}
        if "query GpuTypes" in query:
            assert 'id: "gpu-exact"' in query
            return {"gpuTypes": [{"id": "gpu-exact", "displayName": "CPU fixture GPU"}]}
        if "podFindAndDeployOnDemand" in query:
            record = json.loads(self.claim_path.read_text())
            assert record["state"] == "allocation_pending" and record["lease"]["state"] == "preparing"
            assert json.loads(re.search(r"allowedCudaVersions: (\[[^\]]+\])", query)[1]) == list(
                DEFAULT_ALLOWED_CUDA_VERSIONS)
            if self.volume is None:
                assert "networkVolumeId" not in query
            else:
                assert f'networkVolumeId: "{self.volume["id"]}"' in query
            assert 'gpuTypeId: "gpu-exact"' in query and record["request_name"] in query
            assert not self.pods, "a second pod was deployed while one exists"
            self.deploys += 1
            self.pod_id = POD if self.deploys == 1 else f"{POD}{self.deploys}"
            self._new_pod_state()
            self.pods.append({"id": self.pod_id, "name": record["request_name"],
                "desiredStatus": "RUNNING", "actualStatus": "RUNNING", "costPerHr": 1.0, "gpuCount": 1,
                "imageName": record["worker_image"],
                "networkVolumeId": self.volume["id"] if self.volume else None,
                **({"volumeMountPath": self.volume["mount_path"]} if self.volume else {}),
                "dataCenterId": DC, "machineType": "CPU fixture GPU",
                "machine": {"gpuTypeId": "gpu-exact", "gpuDisplayName": "CPU fixture GPU", "dataCenterId": DC}})
            return {"podFindAndDeployOnDemand": self.pods[0]}
        if "podMounts" in query:
            # Typed mounts, as runpod_lifecycle.discovery.observe_pod_mounts reads them (f2948817).
            assert f'podId: "{self.pod_id}"' in query
            mounts = [] if self.volume is None else [{"volumeId": self.volume["id"], "volumeType": "NETWORK_VOLUME",
                                                      "mountPath": self.mount_path, "readOnly": False}]
            return {"pod": {"id": self.pod_id, "volumeMounts": mounts}}
        if "networkVolumes" in query:
            return {"myself": {"networkVolumes": self._volumes()}}
        if "myself" in query:
            return {"myself": {"pods": self.pods}}
        if "pod(" in query:
            return {"pod": self.pods[0] if self.pods else None}
        raise AssertionError("unexpected provider GraphQL operation")

    def _volumes(self):
        assert self.volume is not None, "ephemeral route queried volumes"
        return [{"id": self.volume["id"], "name": self.volume["name"], "size": self.volume["size_gb"],
                 "dataCenterId": DC}]

    # -- pod agent commands --------------------------------------------------

    def command(self, command):
        self.commands.append(command)
        if command.startswith("test -f "):
            return (0 if shlex.split(command)[2] in self.files else 1), ""
        if "base64 -d" in command and '"$d/command.sh"' in command:
            directory = shlex.split(command.splitlines()[1])[0].removeprefix("d=")
            detached = base64.b64decode(re.search(r"printf %s (\S+) \| base64 -d", command)[1]).decode()
            self.commands.append(detached)
            self.files.update({directory + "/pid": b"123", directory + "/stderr": b""})
            if "pod_reset reset" in detached:
                self._reset(directory, detached)
            elif "pod_bootstrap.sh" in detached:
                self.files[self._marker(detached)] = b"ok"
                self._exit(directory, 0, b"")
            elif re.search(r"engines install (\S+) --platform linux-x86_64-cu130", detached):
                engine = re.search(r"engines install (\S+) --platform", detached)[1]
                if self.volume is not None:
                    assert f"--model-cache-root /workspace/astrid/engines/{engine}/models" in detached
                self.files[f"/root/astrid/engines/{engine}/.astrid-engine/install.json"] = json.dumps(
                    self._receipt_for(engine)).encode()
                self._exit(directory, 0, b"")
            else:
                self._start_generation(directory, detached)
            return 0, ""
        if ': > "$d/cancel"' in command:
            # detached_cancel: signal the recorded group; a finished job keeps its exit.
            directory = shlex.split(command.splitlines()[0])[0].removeprefix("d=")
            job = self.detached[directory]
            if directory + "/exit" not in self.files:
                job["state"] = "cancelled"
                self.files[directory + "/status"] = b"cancelled"
            self.events.append(("detached_cancel", directory))
            return 0, ""
        if "pod-state:" in command:
            return 0, "pod-state:" + ("pending" if self.active_job is not None else self.pod_state) + "\n"
        if command.startswith("rm -rf "):
            gone = shlex.split(command)[2]
            for path in [p for p in self.files if p.startswith(gone + "/")]:
                del self.files[path]
            return 0, ""
        if "engines status" in command:
            engine = re.search(r"engines status (\S+)", command)[1]
            installed = f"/root/astrid/engines/{engine}/.astrid-engine/install.json" in self.files
            return 0, json.dumps(self._receipt_for(engine) if installed else None)
        if command.startswith("python3 -c ") and "ASTRID_STORAGE_DIAGNOSTIC" in command:
            return self._storage_probe(command)
        if command.startswith(("cat ", "tail ")):
            return 0, self.files.get(shlex.split(command)[-1], b"").decode()
        if command.startswith("sha256sum "):
            path = shlex.split(command)[1]
            return 0, _sha(self.files[path]) + "  " + path
        assert command.startswith("mkdir -p "), command
        return 0, ""

    def _marker(self, detached):
        work = re.search(r"cd (\S+);", detached)[1].strip("'")
        digest = work.rsplit("upload-", 1)[1]
        return f"{remote_placement.BOOTSTRAP_ROOT}/{digest}/ok"

    def _exit(self, directory, code, stdout):
        self.files.update({directory + "/status": b"exited", directory + "/exit": str(code).encode(),
                           directory + "/stdout": stdout})

    def _storage_probe(self, command):
        request = json.loads(base64.b64decode(shlex.split(command)[-1]))
        mapping = request.pop("provider_mapping")
        assert mapping["schema"] == "runpod.provider-mounts.v1" and mapping["pod_id"] == self.pod_id
        if not [e for e in self.events if e[0] == "storage_probe"]:
            # The first probe of a pod precedes every install write (a warm reuse re-probes later).
            assert not any(path.endswith("/.astrid-engine/install.json") for path in self.files), "probe after install"
        self.events.append(("storage_probe", request["mount"]))
        return 0, json.dumps({**request, "cache_device": 64768, "cache_mount_id": self.mount_id,
                              "free_bytes": 10**12, "total_bytes": 2 * 10**12, "local_free_bytes": 10**12})

    def _reset(self, directory, detached):
        """``pod_reset reset``: template on a fresh pod, else restore + replace; sweep; ready.json."""
        argv = shlex.split(next(line for line in detached.splitlines() if "pod_reset reset" in line))
        dirs = [argv[argv.index(flag) + 1] for flag in ("--run-dir", "--job-dir") if flag in argv]
        record = json.loads(self.claim_path.read_text())
        self.resets.append({"at": len(self.events), "dirs": dirs, "active": self.active_job,
                            "lease": record["lease"]["state"], "job": dict(record["lease"].get("job") or {})})
        self.events.append(("reset", dirs[0] if dirs else None))
        if self.fail_reset and dirs:
            self.pod_state = "absent"
            self._exit(directory, 1, json.dumps({"reset": "failed", "error": "ResetError: replace refused"}).encode())
            return
        for gone in dirs:
            for path in [p for p in self.files if p.startswith(gone + "/")]:
                del self.files[path]
        taken = not self.template
        self.template, self.pod_state, self.active_job = True, "ready", None
        self._exit(directory, 0, json.dumps({"reset": "ok", "removed": len(dirs),
                                             "template": "taken" if taken else "restored"}).encode())

    def _start_generation(self, directory, detached):
        # Provider-only journeys answer the pod command here; the shipped-job seam
        # is exercised through real run_job in test_remote_machine_job_seam_cpu.py.
        # T5f: the job's one command opens the prepared connection under the reset lock.
        assert "ensure_runtime" not in detached and "astrid-runtime" not in detached, "a job ran up/connect"
        assert "export ASTRID_PREPARED_CONNECTION=/root/astrid/prepared-connection.json" in detached
        assert "flock -s 9" in detached and "ASTRID_RUNTIME_DOOR_PORT=0" in detached
        assert self.pod_state == "ready" and self.active_job is None, "a job started on a pod with no ready reset"
        active = next(row for row in detached.splitlines()
                      if row.startswith("/opt/astrid/bin/python -c ") and "active-job.json" in row)
        self.active_job = {**json.loads(shlex.split(active)[3]), "job_dir": directory}
        assert detached.index("generate list --json") < detached.index("--placement local")
        argv = shlex.split(next(line for line in detached.splitlines() if "--placement local" in line).split(" > ")[0])
        assert argv[1] == "generate" and argv[3:5] == ["--placement", "local"], argv
        assert self.expect_entry is None or argv[2] == self.expect_entry, argv
        assert "17340" not in detached and "--port" not in detached
        root = argv[argv.index("--job") + 1].rsplit("/", 1)[0]
        job = json.loads(self.files[root + "/job.json"])
        assert f"export ASTRID_PACKS_PATH={root}/selection/packs" in detached
        # The command's own listing reads the shipped selection (every job, a workflow job too).
        shipped = self._shipped(root)
        if argv[2] != "workflow":
            # The entry resolves from the pod's built-ins or the shipped selection only.
            assert argv[2] in [*self.listed, *shipped], (argv[2], self.listed)
        assert argv[2] == (job["entry"] or "workflow")
        execution = job["execution"]
        assert set(execution) >= {"python", "companion", "source"}
        for name in ("python", "companion", "source", "managed_assets", "source_video"):
            if name in execution:
                # Input files ship inside the job directory, digest-checked after upload.
                assert execution[name].startswith(root + "/in/") and execution[name] in self.files
        self.generations.append(job)
        self.detached[directory] = {"state": "running", "root": root, "polls": 0, "job": job}
        self.files[directory + "/status"] = b"running"

    def _advance(self, directory):
        """One detached poll of the generation job: write its next phase, exit after the last."""
        job = self.detached[directory]
        if job["state"] != "running":
            return
        if not self.job_hold.is_set():
            return  # held running (cancel journey)
        root, index = job["root"], job["polls"]
        job["polls"] += 1
        self.files[root + "/progress.json"] = json.dumps(PHASES[min(index, len(PHASES) - 1)]).encode()
        if index == 2:
            # A phase written at an earlier poll is already in the controller's attempt file.
            for path in Path(self.attempts).rglob(".astrid-progress.json"):
                self.seen_controller_progress.append(json.loads(path.read_text()))
        if index < len(PHASES) - 1:
            return
        if self.job_failure is not None:
            job["state"] = "exited"
            self._exit(directory, self.job_failure[0], self.job_failure[1])
            return
        out = root + "/out"
        # The producer's own result manifest, written by the same helpers vibecomfy.run uses.
        import tempfile
        from astrid.pack_api import build_manifest, write_manifest
        name, data, media_type = self.output_for(job["job"])
        with tempfile.TemporaryDirectory() as scratch:
            (Path(scratch) / "artifacts").mkdir()
            (Path(scratch) / "artifacts" / name).write_bytes(data)
            write_manifest(Path(scratch) / "manifest.json", build_manifest(
                kind="vibecomfy.run", inputs={}, created=datetime.now(timezone.utc).isoformat(),
                outputs=[{"path": f"artifacts/{name}", "name": "vibecomfy_run", "ordinal": 0, "role": "result",
                          "is_primary": True, "media_type": media_type}],
                schema_version=2, warnings=[], engine_run_id=None, engine_prompt_id=None))
            manifest_bytes = (Path(scratch) / "manifest.json").read_bytes()
        tree = {f"{out}/artifacts/{name}": data, f"{out}/manifest.json": manifest_bytes}
        self.files.update(tree)
        result = {"status": "succeeded", "manifest": f"{out}/manifest.json"}
        self.files[root + "/result.json"] = json.dumps(result).encode()
        files = {path[len(out) + 1:]: {"sha256": _sha(data), "size": len(data)} for path, data in tree.items()}
        body = json.dumps({"schema_version": 1, "files": files}, sort_keys=True, indent=2) + "\n"
        self.files[f"{out}/{remote_placement.MANIFEST_NAME}"] = body.encode()
        printed = _sha(b"not the manifest") if self.corrupt_manifest else _sha(body.encode())
        stdout = (f"{remote_placement.RESULT_MARK}{json.dumps(result)}\n"
                  f"{remote_placement.MANIFEST_MARK}{printed}\n").encode()
        job["state"] = "exited"
        self._exit(directory, 0, stdout)

    listed = ()

    def _shipped(self, root):
        """Entry ids the job's selection.tar.gz makes resolvable on the pod, from its real bytes."""
        import tarfile
        import yaml
        data = self.files[root + "/selection.tar.gz"]
        ids = []
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
            names = archive.getnames()
            manifest = json.loads(archive.extractfile("selection.json").read())
            self.selections.append(manifest)
            for name in names:
                parts = name.split("/")
                if len(parts) == 3 and parts[0] == "entries" and parts[2] == "entry.json":
                    ids.append(json.loads(archive.extractfile(name).read())["id"])
                if (len(parts) == 5 and parts[0] == "packs" and parts[2] == "extensions"
                        and parts[3] == "vibecomfy.workflows" and name.endswith((".yaml", ".yml"))):
                    ids += [row["id"] for row in (yaml.safe_load(archive.extractfile(name).read()) or {}).get("entries", [])]
        return ids

    def kill_pod(self):
        """The provider terminates the pod between jobs (spot reclaim, user action)."""
        self.killed.append(self.pod_id)
        self.pods.clear()
        self.files.clear()  # its container disk goes with it
        self._new_pod_state()

    def output_for(self, job):
        """The pod's generated file for a job (an image unless a subclass knows the modality)."""
        return "cpu.png", self.image, "image/png"

    # -- HTTP ----------------------------------------------------------------

    def handle(self, request):
        host, path, method = request.url.host, request.url.path, request.method
        request.read()
        body = (json.loads(request.content)
                if request.content and request.headers.get("content-type", "").split(";", 1)[0] == "application/json"
                else {})
        self.events.append((method, host, path))

        def reply(data, status=200):
            encoded = json.dumps(data).encode()
            return httpx.Response(status, stream=httpx.ByteStream(encoded), request=request,
                                  headers={"Content-Type": "application/json", "Content-Length": str(len(encoded))})
        if host in {"api.runpod.io", "graphql.runpod.io"}:
            return reply({"data": self.graphql(body)})
        if host == "rest.runpod.io":
            if path.startswith("/v1/networkvolumes"):
                return reply(self._volumes())
            assert path.startswith("/v1/pods"), path
            if method == "DELETE":
                if path.endswith("/" + self.pod_id) and self.pod_id in self.killed:
                    # The provider already terminated this pod: an exact delete finds nothing.
                    self.events.append(("pod-delete-gone", self.pod_id))
                    return reply({}, 404)
                assert path.endswith("/" + self.pod_id) and self.pods
                record = json.loads(self.claim_path.read_text())
                assert record["release_authorized"] is True
                job = record["lease"].get("job")
                assert job is None or job["state"] in {"exited", "cancelled"}, "pod deleted with a live job"
                self.pods.clear()
                self.events.append(("pod-delete", self.pod_id))
                return reply({}, 204)
            assert method == "GET"
            if path == "/v1/pods":
                return reply(self.pods)
            if not self.pods:
                self.events.append(("pod-404", self.pod_id))
            return reply(self.pods[0] if self.pods else {}, 200 if self.pods else 404)
        if host == "astrid-egress-preflight-8000.proxy.runpod.net":
            return reply({}, 404)
        assert host == self.pod_id + "-8000.proxy.runpod.net", host
        assert "/engines/" not in path and path != "/engine-proxy", "the controller reached a pod engine"
        if path == "/health":
            return reply({"ok": True, "protocol": 1})
        if path == "/jobs" and method == "POST":
            code, stdout = self.command(body["command"])
            self.agent_jobs[body["id"]] = (code, stdout.encode())
            return reply({"id": body["id"]})
        if path.startswith("/jobs/"):
            code, stdout = self.agent_jobs[path.rsplit("/", 1)[1]]
            return reply({"status": "exited", "exit_code": code, "stdout": base64.b64encode(stdout).decode(),
                          "stderr": "", "stdout_offset": len(stdout), "stderr_offset": 0,
                          "stdout_eof": True, "stderr_eof": True})
        if path == "/uploads":
            upload_id = _sha(body["path"].encode())[:32]
            self.agent_jobs[upload_id] = body
            self.files[body["path"]] = b""
            return reply({"id": upload_id, "offset": 0, "complete": False})
        if path.startswith("/uploads/"):
            meta = self.agent_jobs[path.rsplit("/", 1)[1]]
            if method == "PUT":
                self.files[meta["path"]] += request.content
            data = self.files[meta["path"]]
            return reply({"offset": len(data), "complete": len(data) == meta["size"] and _sha(data) == meta["sha256"]})
        if path == "/download-chunk":
            target = body["path"]
            directory, _, name = target.rpartition("/")
            if target.endswith("/out/" + remote_placement.MANIFEST_NAME):
                self.events.append(("fetch-manifest", target.rsplit("/out/", 1)[0]))
            if name == "exit" and directory in self.detached:
                self._advance(directory)  # detached_poll reads status, then exit
            if target not in self.files:
                return reply({}, 400)
            data = self.files[target]
            return reply({"data": base64.b64encode(data[body["offset"]:]).decode(), "offset": body["offset"],
                          "total": len(data), "sha256": _sha(data[body["offset"]:]), "etag": _sha(data)})
        raise AssertionError((method, host, path))


@pytest.fixture
def journey(tmp_path, monkeypatch, real_runtime, tuple_sources):
    """Runtime, host and the provider fake; yields a small driver for one account."""
    from vibecomfy.workflow import VibeOutput, VibeWorkflow, WorkflowSource
    from vibecomfy.workflow_bundle import emit_bundle

    monkeypatch.setenv("ASTRID_RUNTIME_DATA_ROOT", str(tmp_path / "machine"))
    monkeypatch.setenv("ASTRID_MACHINE_POLL_SECONDS", "0.05")
    monkeypatch.setattr("astrid.packs.runpod.managed_sources.ensure_managed_sources", lambda: Path(tuple_sources))
    monkeypatch.setattr("astrid.packs.runpod.managed_sources._pins", lambda: tuple_pins(tuple_sources))
    monkeypatch.setenv("VIBECOMFY_ENTRIES_DIR", str(tmp_path / "entries"))
    folder = tmp_path / "entries" / "cpu-image"
    folder.mkdir(parents=True)
    workflow = VibeWorkflow("cpu-image", WorkflowSource("cpu-image"))
    workflow.add_node("SaveImage", _id="1", uid="sink", filename_prefix="cpu")
    workflow.register_input("filename_prefix", "1", "filename_prefix", "cpu", type="STRING")
    workflow.outputs = [VibeOutput("1", "SaveImage", name="image", artifact_kind="image", mime_type="image/png")]
    (folder / "source.json").write_text(json.dumps({"nodes": [
        {"id": 1, "type": "SaveImage", "inputs": [], "widgets_values": ["cpu"]}], "links": []}))
    emit_bundle(workflow, folder / "workflow.py", {"operation": "authored"})
    (folder / "entry.json").write_text(json.dumps({"id": "cpu-image", "kind": "image", "modes": ["t2i"],
        "bundle": "workflow.py", "needs": [], "binds": {}, "defaults": {}}))

    state = {}

    def start(volume=None, gpu_types=None):
        preferences = {"provider_account_ref": "cpu-fixture", "gpu_types": gpu_types or ["CPU fixture GPU"],
            "image": "runpod/base:1.0.3-dev-fix-pytorch-version-verification-cuda1300-ubuntu2404"
                     "@sha256:24795acee5856a3e6d383e879ccdd53edd25d24ea38d0edab5a1d3718ea385e6",
            "volume": ({"id": None, "name": None, "size_gb": 0, "mount_path": "/workspace"} if volume is None
                       else dict(volume)),
            "idle_timeout_seconds": 1800,
            "credential_ref": "env:RUNPOD_RUNPOD_FAKE_CREDENTIAL", "budget_cap_usd": 2.0}
        target_selection.save_runpod_preferences(preferences)
        claim_path = target_selection.canonical_claim_path("cpu-fixture")
        provider = MachineProviderHTTP(claim_path, tmp_path / "attempts", volume=volume)
        send, send_requests = httpx.Client.send, requests.Session.send

        def provider_response(request):
            try:
                return provider.handle(request)
            except Exception as error:
                (tmp_path / "provider-http-failure.json").write_text(json.dumps({
                    "type": type(error).__name__, "message": str(error)[:1000]}))
                raise

        def http_send(client, request, **kwargs):
            if request.url.host in {"127.0.0.1", "localhost", "::1"}:
                return send(client, request, **kwargs)
            response = provider_response(request)
            if not kwargs.get("stream"):
                response.read()
            return response

        def requests_send(session, request, **kwargs):
            if httpx.URL(request.url).host in {"127.0.0.1", "localhost", "::1"}:
                return send_requests(session, request, **kwargs)
            response = provider_response(httpx.Request(request.method, request.url, headers=request.headers,
                                                       content=request.body))
            result = requests.Response()
            result.status_code, result._content = response.status_code, response.read()
            return result
        connect = socket.socket.connect

        def local_connect(sock, address):
            assert not isinstance(address, tuple) or address[0] in {"127.0.0.1", "localhost", "::1"}
            return connect(sock, address)
        monkeypatch.setattr(httpx.Client, "send", http_send)
        monkeypatch.setattr(requests.Session, "send", requests_send)
        monkeypatch.setattr(socket.socket, "connect", local_connect)

        daemon = real_runtime()
        owner = WorkspaceClient(daemon.endpoint, daemon.token)
        client = AstridClient(remote=RemoteAstridClient(owner))
        project = client.projects.create(name="CPU machine", slug="cpu-machine", idempotency_key="project")
        assert project.ok, project.error
        project_id = project.data["project_id"]
        owner.select_project(project_id, idempotency_key="select")
        target_selection.save_runpod_preferences(preferences, client=client)
        packs = Path(__file__).resolve().parents[2] / "astrid" / "packs"
        host = GenericPackHost(pack_roots=[packs / "vibecomfy", packs / "runpod"],
            client=RuntimeProtocolClient(daemon.endpoint, daemon.worker_token), attempt_base=tmp_path / "attempts",
            credential_source={"RUNPOD_RUNPOD_FAKE_CREDENTIAL": "synthetic-only"})
        host.discover()
        host.preflight()
        host.register()
        worker = threading.Thread(target=host.run, kwargs={"poll_seconds": 0.02}, daemon=True)
        worker.start()
        state.update(provider=provider, daemon=daemon, host=host, worker=worker, client=client, owner=owner,
                     project=project_id, claim_path=claim_path, preferences=preferences)
        setup = client.invoke_result("runpod.setup", kind="action", project=None,
            inputs={"placement_policy": json.dumps({"kind": "runpod", "engine": "comfyui",
                                                    "preferences": preferences})},
            wait=True, timeout_seconds=90, poll_seconds=0.02, read_managed_outputs=False)
        assert setup.ok, setup.to_dict()
        return state

    def image(*, wait=True, invocation=None, placement="runpod", idle_seconds=1800):
        client = state["client"]
        quote = GenerationFacade().describe(placement=placement, client=client)
        return GenerationFacade().image(entry="cpu-image", placement=placement, client=client,
            project=state["project"], consent=("paid",), max_usd=1.5,
            deadline=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
            offer_digest=quote["offer_digest"], preparation_seconds=120, execution_seconds=60,
            idle_seconds=idle_seconds, wait=wait)

    def record():
        return json.loads(state["claim_path"].read_text())

    def release():
        result = GenerationFacade().release(engine="comfyui", placement="runpod", client=state["client"], wait=True)
        assert result.ok, result.to_dict()
        return record()

    driver = type("Journey", (), {})()
    driver.start, driver.image, driver.record, driver.release, driver.state = start, image, record, release, state
    driver.tmp = tmp_path
    try:
        yield driver
    finally:
        provider = state.get("provider")
        if provider is not None:
            (tmp_path / "provider-http-events.json").write_text(json.dumps(provider.events, indent=2, default=str))
            assert not any("/engines/" in str(event) or "/engine-proxy" in str(event) for event in provider.events)
        if "host" in state:
            state["host"]._shutdown.set()
            state["worker"].join(timeout=35)
            assert not state["worker"].is_alive()
            state["host"].shutdown()
            state["daemon"].stop()


def _released(provider, record):
    assert record["state"] == "cleanup_complete"
    assert record["cleanup"]["absence"]["exact_get_status"] == 404
    assert not provider.pods


def _machines_json(capsys):
    from astrid.core.machines_cli import main as machines

    capsys.readouterr()
    assert machines(["--json"]) == 0
    return json.loads(capsys.readouterr().out)


def test_cold_run_then_warm_second_run_reuses_the_machine(journey):
    j = journey
    s = j.start()
    provider = s["provider"]
    first = j.image()
    assert first.ok, first.to_dict()
    delivered = deliver_generation(first, client=s["client"], out=j.tmp / "first", project=s["project"])
    assert Path(delivered["image_paths"][0]).read_bytes() == provider.image
    assert provider.deploys == 1
    assert len(provider.kinds("pod_bootstrap.sh")) == 1
    assert len(provider.kinds("engines install comfyui")) == 1
    # The entry is listed from the shipped selection inside the job's one command.
    assert len(provider.kinds("generate list --json")) == 1
    assert len([c for c in provider.kinds("generate list --json") if "tar -xzf" in c]) == 1
    # T5f: the fresh pod's claim takes the template; the job's reset runs after the
    # digest-verified fetch and before settle (the lease is still the job's, not idle).
    claim_reset, job_reset = provider.resets
    assert claim_reset["dirs"] == [] and claim_reset["active"] is None
    (run_dir,) = {e[1] for e in provider.events if e[0] == "fetch-manifest"}
    fetched_at = max(i for i, e in enumerate(provider.events) if e == ("fetch-manifest", run_dir))
    assert job_reset["dirs"][0] == run_dir and job_reset["at"] > fetched_at
    assert job_reset["lease"] != "idle" and job_reset["job"]["state"] == "starting"
    assert job_reset["active"]["run_id"] == run_dir.rsplit("/", 1)[1]
    assert not [path for path in provider.files if path.startswith(run_dir + "/")], "the run directory survived"
    assert not [path for path in provider.files if path.startswith(job_reset["dirs"][1] + "/")]
    record = j.record()
    lease = record["lease"]
    assert lease["state"] == "idle" and lease["job"]["state"] == "exited"
    preparation = lease["preparation"]
    assert preparation["state"] == "prepared"
    assert preparation["storage"] == {"mode": "ephemeral"}
    assert preparation["pod_ready"] == {"entry": None, "ready": True, "found": "absent", "reset": "taken"}
    custody = preparation["custody"]
    assert custody["gpu_type"] == "CPU fixture GPU" and custody["account"] == "cpu-fixture"
    # T5c: the attempt records which machine ran it, and the task listing shows it.
    shown = s["client"].tasks.show(first.task_id)
    machine = shown.data["result"]["machine"]
    assert (machine["placement"], machine["machine"], machine["id"]) == ("runpod", "runpod.runpod", POD)
    assert machine["lease"] == record["operation_id"] and machine["device"] == "CPU fixture GPU"
    from astrid.core.cli.list_narrow import machine_label, render_list_table
    assert machine_label(shown.data) == f"runpod:{POD} (CPU fixture GPU)"
    assert f"runpod:{POD}" in render_list_table([shown.data], total=1, noun="tasks")
    assert custody["digest"].startswith("sha256:")
    # Progress: each poll copied one phase into the controller's attempt file before the job ended.
    assert provider.seen_controller_progress and provider.seen_controller_progress[0]["phase"] in {"align", "submit"}

    jobs_before = list(provider.commands)
    second = j.image()
    assert second.ok, second.to_dict()
    assert provider.deploys == 1, "the warm machine was not reused"
    new = provider.commands[len(jobs_before):]
    assert not [c for c in new if "pod_bootstrap.sh" in c or "engines install" in c]
    assert len([c for c in new if "generate list --json" in c]) == 1  # warm: only the job's own check
    assert len([c for c in new if " generate cpu-image " in c]) == 1
    # A warm job runs no up and no connect: no claim-time reset, the prepared connection only.
    assert not [c for c in new if "ensure_runtime" in c or "astrid-runtime" in c]
    assert len(provider.resets) == 3 and provider.resets[2]["dirs"]
    assert j.record()["lease"]["preparation"]["pod_ready"]["reset"] == "skipped"
    assert j.record()["lease"]["preparation"]["custody"] == custody
    _released(provider, j.release())
    order = [e[0] for e in provider.events if e[0] in {"pod-delete", "pod-404"}]
    assert order[:2] == ["pod-delete", "pod-404"]


def test_resolved_runpod_policy_controls_the_claim_and_warm_hold(journey):
    j = journey
    state = j.start(gpu_types=["gpu-exact"])
    result = j.image(placement={"kind": "runpod", "policy": {
        "gpu_types": ["CPU fixture GPU"], "idle_timeout_s": 0}})
    assert result.ok, result.to_dict()
    lease = j.record()["lease"]
    assert lease["idle_seconds"] == 0
    assert state["client"].tasks.show(result.task_id).data["result"]["machine"]["idle_timeout_s"] == 0
    assert state["provider"].deploys == 1


@pytest.mark.parametrize("placement", ["runpod", {"kind": "runpod", "policy": {"idle_timeout_s": 300}}],
                         ids=["default", "explicit"])
def test_paid_idle_bound_caps_placement_and_reports_effective_timeout(journey, placement):
    from astrid.sdk.generation import _runpod_operation
    j = journey
    state = j.start()
    result = j.image(placement=placement, idle_seconds=30)
    assert result.ok, result.to_dict()
    lease = j.record()["lease"]
    assert lease["idle_seconds"] == 30
    machine = state["client"].tasks.show(result.task_id).data["result"]["machine"]
    assert machine["idle_timeout_s"] == 30
    status = _runpod_operation("status", client=state["client"], wait=True)
    assert status.ok, status.to_dict()
    payload = status.raw_result['result']
    payload = payload.get('payload', payload)
    assert payload['target_operation']['lease']['idle_timeout_s'] == 30
    listing = _runpod_operation("ls", client=state["client"], wait=True)
    assert listing.ok, listing.to_dict()
    listing_payload = listing.raw_result['result']
    listing_payload = listing_payload.get('payload', listing_payload)
    assert listing_payload['target_operation']['leases'][0]['lease']['idle_timeout_s'] == 30
    _released(state["provider"], j.release())


def test_overridden_gpu_lease_remains_manageable_with_saved_preferences(journey):
    from astrid.sdk.generation import _runpod_operation
    j = journey
    state = j.start(gpu_types=["gpu-exact"])
    result = j.image(placement={"kind": "runpod", "policy": {
        "gpu_types": ["CPU fixture GPU"], "idle_timeout_s": 300}})
    assert result.ok, result.to_dict()
    assert j.record()["lease"]["state"] == "idle"
    for operation in ("status", "ls"):
        result = _runpod_operation(operation, client=state["client"], wait=True)
        assert result.ok, result.to_dict()
    _released(state["provider"], j.release())


def test_a_failed_reset_releases_the_pod_instead_of_settling_it(journey):
    """T5f: the outputs are delivered, but a pod whose reset failed is never left warm."""
    j = journey
    s = j.start()
    provider = s["provider"]
    provider.fail_reset = True  # the release-time reset (the claim's template reset still succeeds)
    first = j.image()
    assert first.ok, first.to_dict()
    delivered = deliver_generation(first, client=s["client"], out=j.tmp / "first", project=s["project"])
    assert Path(delivered["image_paths"][0]).read_bytes() == provider.image
    record = j.record()
    _released(provider, record)
    assert record["lease"]["ended"] == "reset_failed" and record["lease"]["job"]["state"] == "exited"
    assert [bool(r["dirs"]) for r in provider.resets] == [False, True]
    order = [e[0] for e in provider.events if e[0] in {"fetch-manifest", "reset", "pod-delete"}]
    assert order[-3:] == ["fetch-manifest", "reset", "pod-delete"]


def test_a_custody_change_refuses_warm_reuse(journey):
    j = journey
    s = j.start(volume=VOLUME)
    provider = s["provider"]
    first = j.image()
    assert first.ok, first.to_dict()
    preparation = j.record()["lease"]["preparation"]
    assert preparation["storage"]["mode"] == "volume" and preparation["storage"]["mount_id"] == 7
    assert preparation["custody"]["volume"] == {"id": VOLUME["id"], "mount_path": "/workspace",
                                                "cache_device": 64768, "mount_id": 7}
    assert [e for e in provider.events if e[0] == "storage_probe"] == [("storage_probe", "/workspace")]
    # The volume was remounted under the same pod: the pinned mount id differs.
    provider.mount_id = 8
    generations = len(provider.kinds(" generate cpu-image "))
    with pytest.raises(Exception, match=r"custody differs: volume\.mount_id"):
        j.image()
    assert len(provider.kinds(" generate cpu-image ")) == generations, "a job ran on a refused machine"
    _released(provider, j.record())
    assert provider.deploys == 1


def test_cancel_mid_poll_stops_the_job_then_releases(journey):
    j = journey
    s = j.start()
    provider, client = s["provider"], s["client"]
    started = j.image(wait=False)
    task_id = started.kernel_task_id
    assert task_id, started.to_dict()
    deadline = time.monotonic() + 120
    while not provider.detached or next(iter(provider.detached.values()))["polls"] < 2:
        assert time.monotonic() < deadline, "the generation job never polled twice"
        time.sleep(0.05)
    provider.job_hold.clear()  # keep it running until the cancel lands
    assert client.tasks.cancel(task_id).ok
    while j.record()["state"] != "cleanup_complete":
        assert time.monotonic() < deadline, "the cancelled machine was not released"
        time.sleep(0.05)
    record = j.record()
    assert record["lease"]["job"]["state"] == "cancelled"
    _released(provider, record)
    order = [e[0] for e in provider.events if e[0] in {"detached_cancel", "pod-delete", "pod-404"}]
    assert order[:3] == ["detached_cancel", "pod-delete", "pod-404"]
    assert not any(path.endswith("/out/wrapper-manifest.json") for path in provider.files)


def test_a_bad_mount_mapping_fails_before_install_and_releases(journey):
    j = journey
    s = j.start(volume=VOLUME)
    provider = s["provider"]
    provider.mount_path = "/mnt/elsewhere"  # the provider maps the volume somewhere else
    with pytest.raises(Exception, match="mount mapping refused before install"):
        j.image()
    assert not provider.kinds("engines install"), "install ran on an unpinned volume"
    assert not [e for e in provider.events if e[0] == "storage_probe"]
    _released(provider, j.record())


def test_a_killed_pod_ends_its_lease_and_the_next_default_job_claims_fresh(journey, capsys):
    """T5d: liveness before reuse; provider_terminated; a pinned job is refused; astrid machines lists both."""
    j = journey
    s = j.start()
    provider = s["provider"]
    first = j.image()
    assert first.ok, first.to_dict()
    assert j.record()["lease"]["state"] == "idle" and provider.deploys == 1
    assert [row["id"] for row in _machines_json(capsys) if row["live"]] == [POD]
    provider.kill_pod()
    second = j.image()
    assert second.ok, second.to_dict()
    assert provider.deploys == 2, "the dead machine was not replaced"
    assert len(provider.kinds("pod_bootstrap.sh")) == 2, "the fresh machine skipped bootstrap"
    machine = s["client"].tasks.show(second.task_id).data["result"]["machine"]
    assert machine["id"] == f"{POD}2" and machine["warm"] is False
    rows = {row["id"]: row for row in _machines_json(capsys)}
    assert rows[f"{POD}2"]["live"] and rows[f"{POD}2"]["state"] == "idle"
    assert rows[POD]["live"] is False and rows[POD]["ended"] == "provider_terminated"
    # A job pinned to the dead machine is refused with the reason and the live leases.
    with pytest.raises(Exception, match=rf"no live runpod lease for machine '{POD}' \(ended: provider_terminated\); live leases: {POD}2"):
        GenerationFacade().image(entry="cpu-image", placement={"kind": "runpod", "machine": POD},
            client=s["client"], project=s["project"], consent=("paid",), max_usd=1.5,
            deadline=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
            offer_digest=GenerationFacade().describe(placement="runpod", client=s["client"])["offer_digest"],
            preparation_seconds=120, execution_seconds=60, idle_seconds=1800, wait=True)
    # Pinned to the live one, it runs warm there.
    pinned = GenerationFacade().image(entry="cpu-image", placement={"kind": "runpod", "machine": f"{POD}2"},
        client=s["client"], project=s["project"], consent=("paid",), max_usd=1.5,
        deadline=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        offer_digest=GenerationFacade().describe(placement="runpod", client=s["client"])["offer_digest"],
        preparation_seconds=120, execution_seconds=60, idle_seconds=1800, wait=True)
    assert pinned.ok and provider.deploys == 2
    _released(provider, j.release())
    rows = {row["id"]: row for row in _machines_json(capsys)}
    assert rows[f"{POD}2"]["ended"] == "explicit" and rows[POD]["ended"] == "provider_terminated"


# -- T5e: the shipped selection ------------------------------------------------

STUDIO_PACK = """schema_version: 3
id: {pack_id}
name: {pack_id}
version: 1.0.0
pack_api: "2.3"
dependencies:
  packs: [vibecomfy]
  python_requirements: [vibecomfy]
documents:
  note:
    format_version: 1
    schema: {{type: object}}
"""
STUDIO_WORKFLOWS = """api: 1
summary: {pack_id} looks.
example: astrid generate image --entry {entry}
effects: none
ready_root: ../../workflows
entries:
  - id: {entry}
    kind: image
    modes: [t2i]
    bundle: look
"""


def _write_workflow_pack(parent, pack_id, entry):
    """A non-core pack with one vibecomfy.workflows entry (a canonical SaveImage bundle)."""
    from vibecomfy.workflow import VibeOutput, VibeWorkflow, WorkflowSource
    from vibecomfy.workflow_bundle import emit_bundle
    pack = parent / pack_id
    (pack / "extensions" / "vibecomfy.workflows").mkdir(parents=True)
    (pack / "pack.yaml").write_text(STUDIO_PACK.format(pack_id=pack_id))
    (pack / "extensions" / "vibecomfy.workflows" / "looks.yaml").write_text(
        STUDIO_WORKFLOWS.format(pack_id=pack_id, entry=entry))
    folder = pack / "workflows" / "look"
    folder.mkdir(parents=True)
    workflow = VibeWorkflow(entry, WorkflowSource(entry))
    workflow.add_node("SaveImage", _id="1", uid="sink", filename_prefix="look")
    workflow.register_input("filename_prefix", "1", "filename_prefix", "look", type="STRING")
    workflow.outputs = [VibeOutput("1", "SaveImage", name="image", artifact_kind="image", mime_type="image/png")]
    (folder / "source.json").write_text(json.dumps({"nodes": [
        {"id": 1, "type": "SaveImage", "inputs": [], "widgets_values": ["look"]}], "links": []}))
    emit_bundle(workflow, folder / "workflow.py", {"operation": "authored"})
    return pack


def _vibecomfy_distribution(monkeypatch):
    # This env runs VibeComfy from PYTHONPATH; a release installs it as a distribution,
    # which the pack validator checks a declared python requirement against.
    from types import SimpleNamespace
    from astrid.core.pack import loader
    real = loader.importlib_metadata.distributions
    monkeypatch.setattr(loader.importlib_metadata, "distributions",
                        lambda: [*real(), SimpleNamespace(metadata={"Name": "vibecomfy"}, version="0.1.0")])


def _install(tmp_path, monkeypatch, pack_id, entry):
    """Install a pack as an active managed source (Plan 06), from a local git repository."""
    from astrid.core.pack import source_setup
    repo = tmp_path / f"{pack_id}-repo"
    repo.mkdir()
    _write_workflow_pack(repo, pack_id, entry)
    git = lambda *a: subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True,  # noqa: E731
                                    text=True).stdout.strip()
    git("init", "-q")
    git("add", "-A")
    git("-c", "user.email=t@example.invalid", "-c", "user.name=t", "commit", "-qm", pack_id)
    monkeypatch.setenv("ASTRID_SOURCE_STATE", str(tmp_path / "pack-sources.json"))
    monkeypatch.setenv("ASTRID_SOURCE_DATA", str(tmp_path / "pack-data"))
    source_setup.provision([source_setup.SourceDeclaration(pack_id, str(repo), git("rev-parse", "HEAD"), pack_id)])
    (source,) = source_setup.active_sources()
    return source


def test_an_installed_pack_entry_and_an_as_entry_user_entry_run_on_the_machine(journey, monkeypatch):
    """T5e (a) and (b): the selection ships; the pod resolves both entries from it only."""
    from vibecomfy.entries import entries_dir, import_as_entry
    j = journey
    _vibecomfy_distribution(monkeypatch)
    source = _install(j.tmp, monkeypatch, "studio", "studio-look")
    # (b) a user entry saved the --as-entry way, from a canonical bundle.
    bundle = Path(source.pack_root) / "workflows" / "look" / "workflow.py"
    import_as_entry(bundle, "asentry-look")
    assert (Path(entries_dir()) / "asentry-look" / "entry.json").is_file()
    s = j.start()
    provider = s["provider"]
    provider.expect_entry = None
    for entry in ("studio-look", "asentry-look", "cpu-image"):
        result = GenerationFacade().image(entry=entry, placement="runpod", client=s["client"],
            project=s["project"], consent=("paid",), max_usd=1.5,
            deadline=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
            offer_digest=GenerationFacade().describe(placement="runpod", client=s["client"])["offer_digest"],
            preparation_seconds=120, execution_seconds=60, idle_seconds=1800, wait=True)
        assert result.ok, (entry, result.to_dict())
    assert [job["entry"] for job in provider.generations] == ["studio-look", "asentry-look", "cpu-image"]
    assert provider.deploys == 1  # one warm machine served all three
    shipped = provider.selections
    # Every job ships the whole active selection, digest-pinned to its Plan 06 source record.
    assert all([row["pack_id"] for row in sel["packs"]] == ["studio"] for sel in shipped)
    pack = shipped[0]["packs"][0]
    assert (pack["revision"], pack["tree_sha256"]) == (source.revision, source.tree_sha256)
    assert "extensions/vibecomfy.workflows/looks.yaml" in pack["files"]
    entries = {sel["entry"]: [row["id"] for row in sel["entries"]] for sel in shipped}
    assert entries["asentry-look"] == ["asentry-look"] and entries["cpu-image"] == ["cpu-image"]
    assert entries["studio-look"] == []  # a pack entry needs no user folder
    _released(provider, j.release())


def test_an_entry_from_a_pack_outside_the_active_selection_is_refused_before_the_claim(journey, monkeypatch):
    """T5e (c): a user-scope pack is not an active source, so it is never shipped or claimed for."""
    j = journey
    _vibecomfy_distribution(monkeypatch)
    loose = j.tmp / "user-packs"
    _write_workflow_pack(loose, "loose", "loose-look")
    monkeypatch.setenv("ASTRID_USER_PACKS", str(loose))
    s = j.start()
    provider = s["provider"]
    with pytest.raises(Exception, match=r"loose-look.*outside the active pack selection"):
        GenerationFacade().image(entry="loose-look", placement="runpod", client=s["client"],
            project=s["project"], consent=("paid",), max_usd=1.5,
            deadline=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
            offer_digest=GenerationFacade().describe(placement="runpod", client=s["client"])["offer_digest"],
            preparation_seconds=120, execution_seconds=60, idle_seconds=1800, wait=True)
    assert provider.deploys == 0 and not s["claim_path"].exists()
    assert not [e for e in provider.events if e[0] == "graphql" and "podFindAndDeployOnDemand" in e[1]]


def test_a_workflow_file_job_runs_on_the_machine_without_a_named_entry(journey):
    """T5g: a one-off workflow file (describe_workflow id 'workflow') is a bundle-only job."""
    j = journey
    s = j.start()
    provider = s["provider"]
    provider.expect_entry = None
    bundle = j.tmp / "entries" / "cpu-image" / "workflow.py"
    result = GenerationFacade().workflow(workflow=bundle, placement="runpod", client=s["client"],
        project=s["project"], consent=("paid",), max_usd=1.5,
        deadline=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        offer_digest=GenerationFacade().describe(placement="runpod", client=s["client"])["offer_digest"],
        preparation_seconds=120, execution_seconds=60, idle_seconds=1800, wait=True)
    assert result.ok, result.to_dict()
    (job,) = provider.generations
    assert job["entry"] is None and set(job["execution"]) >= {"python", "companion", "source"}
    assert provider.kinds(" generate workflow --placement local ")
    assert not provider.kinds(" generate cpu-image ")
    assert provider.selections[-1]["entry"] is None and provider.selections[-1]["entries"] == []
    machine = s["client"].tasks.show(result.task_id).data["result"]["machine"]
    assert machine["id"] == POD
    _released(provider, j.release())


@pytest.mark.parametrize("cold_owner", [False, True])
def test_machine_cli_release_binds_requested_id_through_runtime_to_owner(journey, monkeypatch, capsys, cold_owner):
    """A stale CLI journal cannot authorize releasing the host's replacement pod."""
    from contextlib import contextmanager
    from astrid.core import machines_cli
    from astrid.packs.runpod.machine import lease_row
    from astrid.sdk import AstridClient

    j = journey
    state = j.start()
    result = j.image()
    assert result.ok, result.to_dict()
    current = j.record()
    if cold_owner:
        bindings = state["host"]._target_bindings
        bindings.provider.lease_owner.close()
        bindings.provider = bindings.signature = None

    @contextmanager
    def launcher():
        yield state["client"]

    monkeypatch.setattr(AstridClient, "open_from_launcher", launcher)
    # The CLI read happened before the retained owner changed to this pod.
    monkeypatch.setattr(machines_cli, "_leases", lambda: [lease_row({**current, "pod_id": "prior-pod"})])
    assert machines_cli.main(["release", "prior-pod", "--json"]) == 1
    assert json.loads(capsys.readouterr().out.splitlines()[-1])["ok"] is False
    assert j.record() == current
    assert state["provider"].deploys == 1 and state["provider"].pods

    monkeypatch.setattr(machines_cli, "_leases", lambda: [lease_row(j.record())])
    assert machines_cli.main(["release", current["pod_id"], "--json"]) == 0
    released = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert released["ok"] is True and released["machine"]["id"] == current["pod_id"]
    _released(state["provider"], j.record())
    assert state["provider"].deploys == 1


def test_machine_cli_keep_then_release_uses_the_retained_owner(journey, monkeypatch, capsys):
    from contextlib import contextmanager
    from astrid.core import machines_cli
    from astrid.packs.runpod.machine import lease_row
    from astrid.sdk import AstridClient

    j = journey
    state = j.start()
    result = j.image()
    assert result.ok, result.to_dict()
    current = j.record()

    @contextmanager
    def launcher():
        yield state["client"]

    monkeypatch.setattr(AstridClient, "open_from_launcher", launcher)
    monkeypatch.setattr(machines_cli, "_leases", lambda: [lease_row(j.record())])
    monkeypatch.setattr(machines_cli, "_keep_consent_data", lambda machine_id, rate:
                        (None, current["paid_limits"]["offer_digest"]))
    assert machines_cli.main(["keep", current["pod_id"], "--for", "10m", "--max-usd", "1.5", "--json"]) == 0
    kept = json.loads(capsys.readouterr().out.splitlines()[-1])
    after_keep = j.record()
    assert kept["ok"] is True
    assert kept["machine"]["release_at"] == after_keep["lease"]["idle_expires_at"]
    hourly_usd = after_keep["lease"]["hourly_usd"]
    assert after_keep["paid_limits"]["max_usd"] == pytest.approx(min(1.5, 600 * hourly_usd / 3600))
    authorized_at = datetime.fromisoformat(after_keep["paid_limits"]["authorized_at"].replace("Z", "+00:00"))
    assert after_keep["lease"]["paid_expires_at"] <= (authorized_at + timedelta(minutes=10)).timestamp()
    assert after_keep["pod_id"] == current["pod_id"] and state["provider"].deploys == 1

    assert machines_cli.main(["release", current["pod_id"], "--json"]) == 0
    released = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert released["ok"] is True and released["machine"]["state"] == "ended"
    _released(state["provider"], j.record())


def test_machine_cli_claim_then_generate_reuses_pod_and_paid_expiry(journey, monkeypatch, capsys):
    from contextlib import contextmanager
    from astrid.core import machines_cli
    from astrid.packs.runpod.machine import lease_row
    from astrid.sdk import AstridClient

    j = journey
    state = j.start()
    bindings = state["host"]._target_bindings
    # Simulate a running host whose first RunPod request is claim, not setup.
    bindings.provider.lease_owner.close()
    bindings.provider = bindings.signature = None
    opening = []

    @contextmanager
    def launcher(**kwargs):
        opening.append(kwargs)
        yield state["client"]

    monkeypatch.setattr(AstridClient, "open_from_launcher", launcher)
    monkeypatch.setattr(machines_cli, "_leases", lambda: [lease_row(j.record())] if state["claim_path"].exists() else [])
    assert machines_cli.main(["claim", "--max-usd", "1.5", "--max-hourly-usd", "1",
                              "--for", "30m", "--consent", "paid", "--json"]) == 0
    claimed = json.loads(capsys.readouterr().out.splitlines()[-1])
    before = j.record()
    assert opening == [{"start_pack_host": False}]
    assert claimed["machine"]["id"] == POD and claimed["machine"]["state"] == "idle"
    assert claimed["machine"]["release_at"] == before["lease"]["idle_expires_at"]
    assert state["provider"].deploys == 1 and not state["provider"].generations
    result = GenerationFacade().image(entry="cpu-image", placement="runpod", client=state["client"],
        project=state["project"], consent=("paid",), max_usd=1.5,
        deadline=(datetime.now(timezone.utc) + timedelta(hours=2)).isoformat(),
        offer_digest=GenerationFacade().describe(placement="runpod", client=state["client"])["offer_digest"],
        preparation_seconds=120, execution_seconds=60, idle_seconds=120, wait=True)
    assert result.ok, result.to_dict()
    after = j.record()
    assert after["pod_id"] == before["pod_id"] and state["provider"].deploys == 1
    assert after["lease"]["paid_expires_at"] == before["lease"]["paid_expires_at"]
    assert after["lease"]["started_at"] == before["lease"]["started_at"]
    _released(state["provider"], j.release())


def test_machine_claim_quote_after_owner_loss_refuses_without_releasing_custody(journey):
    j = journey
    state = j.start()
    result = j.image()
    assert result.ok, result.to_dict()
    bindings = state["host"]._target_bindings
    bindings.provider.lease_owner.close()
    bindings.provider = bindings.signature = None
    before = j.record()
    with pytest.raises(Exception, match="target status task failed.*cpupod.*astrid machines"):
        GenerationFacade().describe(placement="runpod", client=state["client"], max_hourly_usd=1, fresh_claim=True)
    assert j.record() == before and state["provider"].pods and state["provider"].deploys == 1
    # Explicit release remains the separately authorized recovery path.
    _released(state["provider"], j.release())


# -- G3 slices 1-2: timing on every job, and one local failure receipt per failed job ---------------------

FRAME = "astrid-generation-failure: "


def _receipts():
    from astrid.packs.runpod import failure_receipt

    root = failure_receipt.failures_root()
    return root, sorted(root.glob("*/failure-receipt.json")) if root.exists() else []


def _newest_receipt():
    from astrid.packs.runpod import failure_receipt

    path = failure_receipt.find_receipt(None)
    assert path is not None, "no failure receipt was written"
    return json.loads(path.read_text(encoding="utf-8"))


def _timing_ok(block):
    assert block["schema"] == "astrid.generation.phase-timing.v1"
    stamps = block["stamps"]
    assert (stamps["capacity_wait_started_at"] <= stamps["claimed_at"] <= stamps["preparation_ended_at"]
            <= stamps["job_started_at"] <= stamps["job_ended_at"] <= stamps["released_at"])


def test_a_successful_job_carries_the_phase_timing_block_and_no_receipt(journey):
    j = journey
    s = j.start()
    first = j.image()
    assert first.ok, first.to_dict()
    machine = s["client"].tasks.show(first.task_id).data["result"]["machine"]
    _timing_ok(machine["timing"])
    assert machine["timing"]["phases"]["collection"]["seconds"] is not None
    assert _receipts()[1] == []
    _released(s["provider"], j.release())


def test_an_execution_failure_writes_a_receipt_and_both_records_carry_timing(journey):
    j = journey
    s = j.start()
    s["provider"].job_failure = (1, (FRAME + json.dumps({"code": "generation_failed", "category": "execution"}) + "\n").encode())
    with pytest.raises(Exception):
        j.image()
    receipt = _newest_receipt()
    assert receipt["stage"] == "execution" and receipt["cause"]["exit_code"] == 1
    assert receipt["collection"]["state"] == "not_collected" and receipt["files"] == []
    _timing_ok(receipt["timing"])
    _released(s["provider"], j.release())


def test_a_pod_validation_exit_writes_a_workflow_validation_receipt(journey):
    j = journey
    s = j.start()
    s["provider"].job_failure = (3, b"")
    with pytest.raises(Exception):
        j.image()
    receipt = _newest_receipt()
    assert receipt["stage"] == "workflow_validation" and receipt["stage_basis"] == "pod_exit"
    _released(s["provider"], j.release())


def test_a_manifest_digest_mismatch_writes_an_output_collection_receipt(journey):
    j = journey
    s = j.start()
    s["provider"].corrupt_manifest = True
    with pytest.raises(Exception):
        j.image()
    receipt = _newest_receipt()
    assert receipt["stage"] == "output_collection" and receipt["cause"]["exception"] == "DigestMismatch"
    assert receipt["machine"]["after"] == "released" and receipt["machine"]["release_reason"] == "output_digest_mismatch"
    _released(s["provider"], j.record())


def test_no_stock_writes_a_no_machine_receipt(journey):
    j = journey
    s = j.start()
    client = s["client"]
    quote = GenerationFacade().describe(placement="runpod", client=client)
    s["provider"].no_stock = True
    with pytest.raises(Exception):
        GenerationFacade().image(entry="cpu-image", placement="runpod", client=client, project=s["project"],
            consent=("paid",), max_usd=1.5, deadline=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
            offer_digest=quote["offer_digest"], preparation_seconds=120, execution_seconds=60,
            idle_seconds=1800, wait=True, wait_for_capacity=False)
    receipt = _newest_receipt()
    assert receipt["stage"] == "no_machine" and receipt["collection"]["state"] == "local_only"
    assert receipt["identity"]["machine_id"] is None


def test_generate_failure_prints_the_journey_receipt(journey, capsys):
    from astrid.core.generation.cli import main as generate

    j = journey
    s = j.start()
    s["provider"].job_failure = (1, (FRAME + json.dumps({"code": "generation_failed", "category": "execution"}) + "\n").encode())
    with pytest.raises(Exception):
        j.image()
    on_disk = _newest_receipt()
    capsys.readouterr()
    assert generate(["failure", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == on_disk
    _released(s["provider"], j.release())
