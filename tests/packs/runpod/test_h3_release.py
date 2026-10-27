from __future__ import annotations

import base64
import copy
import hashlib
import io
import json
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from astrid.packs.runpod import h3_release as build
from astrid.packs.runpod import h3_release_remote as remote
from astrid.packs.runpod import worker_staging as staging


def test_owner_provisioned_200gb_volume_is_reused_without_resize():
    value = {"id": "2z2dlu1v8g", "name": "storage", "size": 200, "dataCenterId": "EUR-NO-1"}
    original = copy.deepcopy(value)
    result = build.normalize_volume(value, value["id"])
    assert result["size_gb"] == 200
    assert result["datacenter"] == "EUR-NO-1"
    assert value == original
    with pytest.raises(build.BuildError):
        build.normalize_volume({**value, "size": 99}, value["id"])


def test_unpopulated_attachment_waits_but_wrong_identity_rejects():
    volume = build.normalize_volume({"id":"v", "size":200,"dataCenterId":"EUR-NO-1"},"v")
    with pytest.raises(build.AttachmentPending):
        build.check_attachment({"id":"p"},volume,"image","cpu")
    with pytest.raises(build.BuildError,match="exact volume"):
        build.check_attachment({"networkVolumeId":"other","dataCenterId":"EUR-NO-1",
                                "volumeMountPath":"/workspace","imageName":"image","computeType":"CPU"},volume,"image","cpu")


class Clock:
    def __init__(self):
        self.value = time.time()

    def now(self):
        return self.value

    def sleep(self, seconds):
        self.value += seconds


def test_cleanup_only_reconciles_empty_intent_without_allocating(tmp_path):
    provider = Provider()
    path = tmp_path / "intent.json"
    path.write_text(json.dumps({"state": "allocating", "request_name": "known-intent", "volume_id": "vol"}))
    result = build.build_release(provider, None, volume_id="vol", handle_path=path, cleanup_only=True)
    assert result["state"] == "allocation_absent"
    assert provider.created == 0
    assert not provider.deleted
    assert result["reconciliation"]["status"] == "no_matching_pod"


class Provider:
    def __init__(self):
        self.pods = {}
        self.deleted = []
        self.gets = []
        self.created = 0
        self.capacity_failures = 0

    def volume(self, volume_id):
        return {"id": volume_id, "name": "astrid-h3", "size": 150, "dataCenterId": "TEST-1"}

    def create(self, volume, image, pod_type, name):
        if self.capacity_failures:
            self.capacity_failures -= 1
            raise build.CapacityUnavailable()
        self.created += 1
        pod_id = f"pod-{self.created}"
        self.pods[pod_id] = {"id": pod_id, "name": name, "networkVolumeId": volume["id"],
                             "dataCenterId": volume["datacenter"], "volumeMountPath": "/workspace",
                             "imageName": image, "computeType": "CPU", "costPerHr": 0.05}
        return self.pods[pod_id]

    def get(self, pod_id):
        self.gets.append(pod_id)
        return self.pods.get(pod_id)

    def delete(self, pod_id):
        self.deleted.append(pod_id)
        self.pods.pop(pod_id, None)

    def find(self, name):
        return [p for p in self.pods.values() if p["name"] == name]


class Transport:
    def __init__(self, failure=None):
        self.failure = failure
        self.closed = False

    def provision(self, request, timeout):
        assert 0 < timeout <= 10680
        if self.failure:
            raise self.failure
        manifest = remote.manifest_for(request, {"requirements_lock_sha256": "a" * 64})
        payload = json.dumps(manifest, indent=2).encode()
        return {"manifest_base64": base64.b64encode(payload).decode(),
                "manifest_sha256": hashlib.sha256(payload).hexdigest(),
                "release_root": request["root"], "release_id": request["release_id"],
                "gpu_qualified": False}

    def close(self):
        self.closed = True


def invoke(tmp_path, provider, transport=None, **kwargs):
    clock = Clock()
    handle = tmp_path / "handle.json"
    transport = transport or Transport()

    def connect(pod, timeout):
        assert json.loads(handle.read_text())["pod_id"] == pod["id"]
        return transport

    return build.build_release(provider, connect, volume_id="volume-1", handle_path=handle,
                               now=clock.now, sleep=clock.sleep, **kwargs)


def test_success_saves_manifest_and_verifies_exact_cleanup(tmp_path):
    provider = Provider()
    provider.capacity_failures = 2
    result = invoke(tmp_path, provider)
    assert result["state"] == "ready"
    assert result["cleanup"]["status"] == "verified_absent"
    assert provider.deleted == ["pod-1"]
    assert provider.gets[-1] == "pod-1"
    assert not provider.pods
    assert remote.digest(result["manifest_path"]) == result["manifest_digest"].removeprefix("sha256:")
    assert json.loads((tmp_path / "handle.receipt.json").read_text()) == result


@pytest.mark.parametrize("failure", [RuntimeError("failed"), TimeoutError("timeout"),
                                    KeyboardInterrupt(), SystemExit(9)])
def test_release_on_failure_or_interruption(tmp_path, failure):
    provider, transport = Provider(), Transport(failure)
    with pytest.raises(type(failure)):
        invoke(tmp_path, provider, transport)
    assert transport.closed
    assert provider.deleted == ["pod-1"]
    assert not provider.pods
    saved = json.loads((tmp_path / "handle.json").read_text())
    assert saved["cleanup"]["status"] == "verified_absent"
    assert saved["state"] == "failed"
    assert (tmp_path / "handle.receipt.json").exists()


def test_hard_crash_resume_cleans_old_id_before_claim(tmp_path):
    provider = Provider()
    inputs = build.release_inputs()
    volume = build.normalize_volume(provider.volume("volume-1"), "volume-1")
    old = provider.create(volume, inputs["recipe"]["image"], "cpu", "previous-request")
    remote.atomic_json(tmp_path / "handle.json", {"volume_id": "volume-1", "pod_id": old["id"],
                                                "state": "building"})
    result = invoke(tmp_path, provider)
    assert provider.deleted == ["pod-1", "pod-2"]
    assert result["pod_id"] == "pod-2"
    assert not provider.pods


def test_lost_create_response_is_reconciled_and_released(tmp_path):
    provider = Provider()
    create = provider.create

    def lost(*args):
        create(*args)
        raise OSError("lost response")

    provider.create = lost
    with pytest.raises(OSError):
        invoke(tmp_path, provider)
    assert provider.created == 1
    assert provider.deleted == ["pod-1"]
    assert json.loads((tmp_path / "handle.json").read_text())["cleanup"]["status"] == "verified_absent"


def test_unknown_create_does_not_allow_duplicate_allocation(tmp_path):
    provider = Provider()
    remote.atomic_json(tmp_path / "handle.json", {"volume_id": "volume-1", "state": "allocating",
                                                "request_name": "lost-create"})
    with pytest.raises(build.BuildError, match="outcome unknown"):
        invoke(tmp_path, provider)
    assert not provider.created


def test_get_error_never_counts_as_cleanup(tmp_path):
    provider = Provider()
    original = provider.get

    def get(pod_id):
        if provider.deleted:
            raise OSError("provider unavailable")
        return original(pod_id)

    provider.get = get
    with pytest.raises(build.BuildError, match="cleanup unresolved"):
        invoke(tmp_path, provider)
    saved = json.loads((tmp_path / "handle.json").read_text())
    assert saved["cleanup"]["status"] == "pending"
    provider.get = original
    result = invoke(tmp_path, provider, cleanup_only=True)
    assert result["cleanup"]["status"] == "verified_absent"
    assert provider.created == 1


def test_attachment_and_price_fail_closed_and_release(tmp_path):
    provider = Provider()
    create = provider.create

    def wrong(*args):
        pod = create(*args)
        pod["networkVolumeId"] = "other-volume"
        return pod

    provider.create = wrong
    with pytest.raises(build.BuildError, match="exact volume"):
        invoke(tmp_path, provider)
    assert not provider.pods
    provider.create = create
    get = provider.get
    provider.get = lambda pod_id: ({**get(pod_id), "costPerHr": 9} if get(pod_id) else None)
    with pytest.raises(build.BuildError, match="price"):
        invoke(tmp_path, provider)
    assert not provider.pods


def test_claim_wait_is_bounded_without_allocation(tmp_path):
    provider = Provider()
    provider.capacity_failures = 100
    with pytest.raises(TimeoutError, match="capacity wait"):
        invoke(tmp_path, provider, claim_seconds=5)
    assert not provider.created
    assert json.loads((tmp_path / "handle.json").read_text())["state"] == "capacity_unavailable"


def test_ssh_wait_timeout_releases(tmp_path):
    provider, clock = Provider(), Clock()

    def unavailable(*_args):
        raise ConnectionError()

    with pytest.raises(TimeoutError, match="transport readiness wait"):
        build.build_release(provider, unavailable, volume_id="volume-1",
                            handle_path=tmp_path / "handle.json", claim_seconds=5,
                            now=clock.now, sleep=clock.sleep)
    assert provider.deleted == ["pod-1"]
    assert not provider.pods


def test_lifecycle_payload_cpu_gpu_and_strict_get(monkeypatch):
    from runpod_lifecycle import api
    monkeypatch.setenv("RUNPOD_SSH_PUBLIC_KEY", "ssh-ed25519 PUBLIC-TEST")
    calls = []
    def create(*args, **kwargs):
        calls.append((args, kwargs))
        return {"id": "pod"}
    monkeypatch.setattr(api, "create_pod", create)
    def response(request):
        return httpx.Response(404 if request.url.path.endswith("missing") else 503)
    provider = build.RunPodProvider("TEST-KEY")
    provider.http.close()
    provider.http = httpx.Client(base_url="https://test/v1/", transport=httpx.MockTransport(response))
    try:
        volume = build.normalize_volume(Provider().volume("vol"), "vol")
        provider.create(volume, "image", "cpu", "name")
        cpu = calls[-1][1]
        assert cpu["compute_type"] == "CPU"
        assert cpu["cpu_flavor_priority"] == "custom"
        assert cpu["cpu_flavor_ids"] == ("cpu3c", "cpu5c")
        assert cpu["network_volume_id"] == "vol"
        assert cpu["data_center_id"] == "TEST-1"
        assert cpu["public_key_string"] == "ssh-ed25519 PUBLIC-TEST"
        provider.create(volume, "image", "NVIDIA GeForce RTX 5090", "name")
        assert calls[-1][1]["allowed_cuda_versions"] == ("13.0",)
        provider.cpu_only_gpu_build = True
        provider.create(volume, "image", "NVIDIA GeForce RTX 4090", "name")
        assert calls[-1][1]["allowed_cuda_versions"] is None
        assert calls[-1][1]["min_cuda_version"] is None
        assert provider.get("missing") is None
        with pytest.raises(build.BuildError):
            provider.get("unavailable")
    finally:
        provider.http.close()


class RangeResponse(io.BytesIO):
    def __init__(self, payload, start, end, *, honor=True):
        super().__init__(payload[start:end + 1])
        self.status = 206 if honor else 200
        self.headers = {"Content-Range": f"bytes {start}-{end}/{len(payload)}"}


def model_row(payload):
    return {"path": "vae/test.safetensors", "revision": "a" * 40,
            "bytes": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}


def test_real_downloader_crash_resume_skip_and_atomic_promotion(tmp_path):
    payload = b"model-bytes" * 25
    row = model_row(payload)
    destination = tmp_path / "test.safetensors"
    requests = []

    def opener(request, timeout):
        start, end = map(int, request.headers["Range"].removeprefix("bytes=").split("-"))
        requests.append(start)
        assert not destination.exists()
        if len(requests) == 2:
            raise KeyboardInterrupt()
        return RangeResponse(payload, start, end)

    with pytest.raises(KeyboardInterrupt):
        remote.download_model(row, destination, "repo", time.time() + 60, opener=opener, chunk_bytes=64)
    assert destination.with_suffix(".safetensors.part").read_bytes() == payload[:64]
    remote.download_model(row, destination, "repo", time.time() + 60, opener=opener, chunk_bytes=64)
    assert requests[:3] == [0, 64, 64]
    assert destination.read_bytes() == payload
    assert not destination.with_suffix(".safetensors.part").exists()
    count = len(requests)
    remote.download_model(row, destination, "repo", time.time() + 60, opener=opener)
    assert len(requests) == count


def test_redirect_body_and_bad_sha_never_promote(tmp_path, monkeypatch):
    monkeypatch.setattr(remote.time, "sleep", lambda _seconds: None)
    payload = b"valid-model"
    row = model_row(payload)
    destination = tmp_path / "model"
    with pytest.raises(RuntimeError, match="range failed"):
        remote.download_model(row, destination, "repo", time.time() + 60,
                              opener=lambda *_a, **_kw: RangeResponse(payload, 0, len(payload) - 1, honor=False))
    assert not destination.exists()
    assert not (tmp_path / "model.part").exists()
    (tmp_path / "model.part").write_bytes(b"x" * len(payload))
    with pytest.raises(RuntimeError, match="SHA-256 mismatch"):
        remote.download_model(row, destination, "repo", time.time() + 60)
    assert not destination.exists()
    assert not (tmp_path / "model.part").exists()


def test_environment_installs_torch_last_then_skips_verified_environment(tmp_path):
    request = {**build.release_inputs(), "root": str(tmp_path), "deadline": time.time() + 60}
    provisioner = remote.Provisioner(request)
    provisioner.runtime.mkdir()
    provisioner.vibe.mkdir()
    (provisioner.vibe / "pyproject.toml").write_text('[build-system]\nrequires=["hatchling>=1.20"]\n')
    commands = []

    def run(argv, *, check=True):
        argv = list(map(str, argv))
        commands.append(argv)
        if "--prefix" in argv:
            provisioner.uv.parent.mkdir(parents=True)
            provisioner.uv.touch()
        if "venv" in argv:
            provisioner.python.parent.mkdir(parents=True)
            provisioner.python.touch()
        if "compile" in argv:
            lock = Path(argv[argv.index("-o") + 1])
            lock.write_text("# locked\nnvidia-library==1.0 \\\n    --hash=sha256:abc\ntorch @ https://test/torch.whl \\\n    --hash=sha256:def\ntorchvision==0.25.0+cu130\ntorchaudio==2.10.0+cu130\npip==25.2\n")
        return SimpleNamespace(returncode=0, stdout="uv " + request["recipe"]["uv"])

    provisioner.run = run
    provisioner.environment()
    installations = [c for c in commands if "sync" in c or "install" in c]
    assert "sync" in installations[-3]
    assert "--no-deps" in installations[-2] and "--no-build-isolation" in installations[-2]
    assert installations[-1][-3:] == list(request["recipe"]["torch_wheels"].values())
    assert "--force-reinstall" in installations[-1] and "--no-deps" in installations[-1]
    support = (provisioner.runtime / "support.lock").read_text()
    assert "nvidia-library" in support and "torch" not in support
    commands.clear()
    provisioner.environment()
    assert not any("compile" in c or "sync" in c or "install" in c for c in commands)
    assert any("check" in c for c in commands)


def test_remote_ready_is_last_and_failed_recheck_removes_marker(tmp_path, monkeypatch):
    request = {**build.release_inputs(), "root": str(tmp_path / "release"),
               "volume": {"mount": str(tmp_path)}, "deadline": time.time() + 60}
    payload = b"verified-model"
    request["lock"] = {"repository": "test/repo", "models": [model_row(payload)]}
    events = []
    writes = remote.atomic_json

    def record(path, value):
        events.append(Path(path).name)
        writes(path, value)

    def environment(provisioner):
        path = provisioner.runtime / "environment-receipt.json"
        if not path.exists():
            remote.atomic_json(path, {"requirements_lock_sha256": "b" * 64})

    def opener(request, timeout):
        return RangeResponse(payload, 0, len(payload) - 1)

    monkeypatch.setattr(remote.os.path, "ismount", lambda _p: True)
    monkeypatch.setattr(remote.Provisioner, "sources", lambda _self: None)
    monkeypatch.setattr(remote.Provisioner, "run", lambda *_a, **_k: SimpleNamespace(returncode=0, stdout=""))
    monkeypatch.setattr(remote.Provisioner, "environment", environment)
    monkeypatch.setattr(remote.urllib.request, "urlopen", opener)
    monkeypatch.setattr(remote, "atomic_json", record)
    result = remote.provision(request)
    assert events[-1] == "READY"
    assert result["gpu_qualified"] is False
    events.clear()
    monkeypatch.setattr(remote.urllib.request, "urlopen", lambda *_a, **_k: pytest.fail("redownload"))
    assert remote.provision(request) == result
    assert events == ["READY"]
    model = Path(request["root"]) / "models" / request["lock"]["models"][0]["path"]
    model.write_bytes(b"corrupt")
    with pytest.raises(RuntimeError, match="integrity"):
        remote.provision(request)
    assert not (Path(request["root"]) / "READY").exists()


def test_produced_manifest_passes_existing_staging_validators(tmp_path):
    vibe = tmp_path / "vibe"
    vibe.mkdir()
    (vibe / "source.py").write_text("# source\n")
    for args in [("init",), ("add", "."),
                 ("-c", "user.name=Test", "-c", "user.email=test@local", "commit", "-m", "source")]:
        subprocess.run(["git", "-C", str(vibe), *args], check=True, capture_output=True)
    revision = subprocess.check_output(["git", "-C", str(vibe), "rev-parse", "HEAD"], text=True).strip()
    inputs = copy.deepcopy(build.release_inputs())
    inputs["recipe"]["vibecomfy_source"]["revision"] = revision
    volume = build.normalize_volume(Provider().volume("vol"), "vol")
    request = {**inputs, "volume": volume}
    manifest = remote.manifest_for(request, {"requirements_lock_sha256": "c" * 64})
    path = tmp_path / "manifest.json"
    remote.atomic_json(path, manifest)
    identity = {"volume_id": "vol", "volume_name": volume["name"], "volume_size_gb": 150,
                "volume_mount_path": "/workspace", "volume_datacenter_id": "TEST-1"}
    _binding, validated = staging._validate_release_manifest(
        path, "sha256:" + remote.digest(path), request["release_id"], target_identity=identity)
    inventory = [{"subdir": str(Path(row["path"]).parent), "name": Path(row["path"]).name,
                  "size": row["bytes"], "sha256": "sha256:" + row["sha256"]}
                 for row in manifest["models"]]
    comfy = request["root"] + "/runtime/ComfyUI"
    contract = staging._release_contract(
        validated, model_binding={"inventory": inventory}, vibecomfy_source=vibe,
        comfy_root=comfy, custom_nodes_lock_path=request["root"] + "/runtime/vibecomfy/custom_nodes.lock",
        custom_node_roots={"h3_custom_node_commit": comfy + "/custom_nodes/ComfyUI-H3-Motion-Context-MultiRef",
                           "videohelpersuite_commit": comfy + "/custom_nodes/ComfyUI-VideoHelperSuite"})
    assert contract["package_versions"]["torch"] == "2.10.0+cu130"
    assert contract["python_version"] == "3.12.10"
    assert contract["pip_check_required"] is True
    assert len(contract["custom_node_roots"]) == 2


def test_real_lock_and_recipe_pins():
    from astrid.core.generation.vibecomfy_dependency import VIBECOMFY_ENGINE_REVISION

    inputs = build.release_inputs()
    models = inputs["lock"]["models"]
    assert len(models) == 5
    assert sum(row["bytes"] for row in models) == 53484234760
    assert all(len(row["sha256"]) == 64 and len(row["revision"]) == 40 for row in models)
    assert inputs["recipe"]["vibecomfy_source"]["revision"] == VIBECOMFY_ENGINE_REVISION
    windows = json.loads((build._DATA.parent / "vibecomfy/environment.windows-nvidia.json").read_text())
    assert inputs["recipe"]["source"] == windows["source"]
    assert inputs["recipe"]["custom_nodes"] == windows["custom_nodes"]
    assert inputs["recipe"]["torch"] == windows["torch"]


def test_git_source_steps_resume_and_reject_drift(tmp_path):
    request = copy.deepcopy(build.release_inputs())
    request.update(root=str(tmp_path / "release"), deadline=time.time() + 60)
    recipe = request["recipe"]

    def repository(name, files):
        path = tmp_path / name
        path.mkdir()
        for filename, content in files.items():
            (path / filename).write_text(content)
        for args in [("init",), ("add", "."),
                     ("-c", "user.name=Test", "-c", "user.email=test@local", "commit", "-m", "source")]:
            subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True)
        revision = subprocess.check_output(["git", "-C", str(path), "rev-parse", "HEAD"], text=True).strip()
        return {"repository": str(path), "revision": revision}

    for i, node in enumerate(recipe["custom_nodes"]):
        node.update(repository(f"node-{i}", {"source.py": "# node source\n"}))
    lock = "".join(f'[nodepacks.{node["name"]}]\ncommit="{node["revision"]}"\n'
                   for node in recipe["custom_nodes"])
    recipe["source"] = repository("comfy", {".gitignore": "custom_nodes/\n", "source.py": "# comfy\n"})
    recipe["vibecomfy_source"] = repository("vibe", {"custom_nodes.lock": lock})
    recipe["custom_nodes_lock_sha256"] = hashlib.sha256(lock.encode()).hexdigest()
    provisioner = remote.Provisioner(request)
    provisioner.runtime.mkdir(parents=True)
    provisioner.comfy.mkdir()
    # Simulate a crash after init, before adding the remote or fetching.
    subprocess.run(["git", "init", str(provisioner.comfy)], check=True, capture_output=True)
    commands = []
    run = provisioner.run

    def record(argv, **kwargs):
        commands.append(list(map(str, argv)))
        return run(argv, **kwargs)

    provisioner.run = record
    provisioner.sources()
    assert sum("fetch" in c for c in commands) == 4
    commands.clear()
    provisioner.sources()
    assert not any("fetch" in c or "checkout" in c for c in commands)
    (provisioner.comfy / "source.py").write_text("# drift\n")
    with pytest.raises(RuntimeError, match="uncommitted"):
        provisioner.sources()


def test_actual_sigterm_releases_build_pod(tmp_path):
    import os
    import signal

    class TerminatedTransport(Transport):
        def provision(self, *_args):
            os.kill(os.getpid(), signal.SIGTERM)
            pytest.fail("SIGTERM did not interrupt build")

    provider = Provider()
    with pytest.raises(KeyboardInterrupt):
        invoke(tmp_path, provider, TerminatedTransport())
    assert provider.deleted == ["pod-1"]
    assert not provider.pods


def test_actual_deadline_releases_build_pod(tmp_path, monkeypatch):
    bounded = build.bounded
    monkeypatch.setattr(build, "bounded", lambda seconds: bounded(min(0.05, seconds)))

    class StalledTransport(Transport):
        def provision(self, *_args):
            time.sleep(10)
            pytest.fail("deadline did not interrupt build")

    provider = Provider()
    with pytest.raises(TimeoutError):
        invoke(tmp_path, provider, StalledTransport())
    assert provider.deleted == ["pod-1"]
    assert not provider.pods


def test_legacy_tiny_redirect_part_is_quarantined_before_resume(tmp_path):
    payload = (32).to_bytes(8, "little") + b"{" + b"x" * (1024 * 1024)
    row = model_row(payload)
    destination = tmp_path / "model.safetensors"
    part = destination.with_name(destination.name + ".part")
    part.write_bytes(b"redirecting to xet")
    starts = []

    def opener(request, timeout):
        start, end = map(int, request.headers["Range"].removeprefix("bytes=").split("-"))
        starts.append(start)
        return RangeResponse(payload, start, end)

    remote.download_model(row, destination, "repo", time.time() + 60, opener=opener)
    assert starts == [0]
    assert destination.read_bytes() == payload
    assert part.with_name(part.name + ".quarantine").read_bytes() == b"redirecting to xet"


def test_rest_observation_enriches_datacenter_from_existing_sdk(monkeypatch):
    from runpod_lifecycle import api
    provider = build.RunPodProvider("test-key")
    monkeypatch.setattr(provider,"request",lambda *a: {"id":"p","gpuCount":1,"networkVolumeId":"v"})
    monkeypatch.setattr(api,"_get_runpod",lambda: SimpleNamespace(get_pod=lambda *a,**kw: {"machine":{"dataCenterId":"EUR-NO-1"}}))
    result=provider.get("p")
    assert result["machine"]["dataCenterId"] == "EUR-NO-1"
    assert result["computeType"] == "GPU"
    assert result["networkVolumeId"] == "v"
