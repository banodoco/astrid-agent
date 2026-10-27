"""Build a pinned H3 CUDA 13 release on an existing RunPod network volume.

The provider and transport are injected for offline tests. Importing this module
performs no network operations. Only main() constructs the live adapters.
"""
from __future__ import annotations

import argparse
import base64
import contextlib
import hashlib
import json
import math
import os
import re
import shlex
import signal
import sys
import time
import uuid
from pathlib import Path
from urllib.parse import quote

from filelock import FileLock

from astrid.packs.runpod import h3_release_remote as remote

_DATA = Path(__file__).parent


class BuildError(RuntimeError):
    pass


class CapacityUnavailable(BuildError):
    pass


class AllocationRejected(BuildError):
    pass


def release_inputs():
    recipe = json.loads((_DATA / "environment.linux-cu130.json").read_text())
    lock = json.loads((_DATA / "models.lock.json").read_text())
    # Provisioner changes create a new release as well as source/model pin changes.
    identity = {"recipe": recipe, "lock": lock,
                "provisioner_sha256": remote.digest(Path(remote.__file__))}
    digest = hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return {"recipe": recipe, "lock": lock, "recipe_sha256": digest,
            "release_id": "h3-cu130-" + digest,
            "root": "/workspace/h3-golden/releases/h3-cu130-" + digest}


def normalize_volume(value, volume_id):
    if value.get("id") != volume_id:
        raise BuildError("provider returned a different volume")
    size = value.get("size")
    if type(size) is not int or size < 100:
        raise BuildError("H3 build requires an existing volume of at least 100 GB (models plus runtime/headroom)")
    dc = value.get("dataCenterId")
    if not isinstance(dc, str) or not dc:
        raise BuildError("volume has no authoritative data center")
    return {"id": volume_id, "name": value.get("name"), "size_gb": size,
            "datacenter": dc, "mount": "/workspace"}


def check_attachment(pod, volume, image, pod_type):
    network = pod.get("networkVolume") or {}
    attached = pod.get("networkVolumeId", network.get("id"))
    machine = pod.get("machine") or {}
    dc = pod.get("dataCenterId", machine.get("dataCenterId", network.get("dataCenterId")))
    if (attached != volume["id"] or dc != volume["datacenter"]
            or pod.get("volumeMountPath") != volume["mount"]
            or pod.get("imageName") != image):
        raise BuildError("provider did not verify the exact volume, data center, mount and image")
    expected = "CPU" if pod_type == "cpu" else "GPU"
    if pod.get("computeType") != expected:
        raise BuildError("provider returned a different compute type")


def _cleanup(provider, handle, path, *, now=time.time, sleep=time.sleep):
    """A DELETE acknowledgment is insufficient: require exact-pod GET 404."""
    handle["cleanup"] = {"status": "pending", "pod_id": handle["pod_id"]}
    remote.atomic_json(path, handle)
    deadline = now() + 90
    for _ in range(3):
        try:
            provider.delete(handle["pod_id"])
        except TimeoutError:
            raise
        except Exception:  # noqa: BLE001 - cleanup must survive provider failures
            # Even a lost DELETE response may have deleted it; GET remains authoritative.
            pass
        attempt_deadline = min(deadline, now() + 25)
        while now() < attempt_deadline:
            try:
                absent = provider.get(handle["pod_id"]) is None
            except TimeoutError:
                raise
            except Exception:  # noqa: BLE001 - cleanup must survive provider failures
                break
            if absent:
                handle["cleanup"] = {"status": "verified_absent", "pod_id": handle["pod_id"],
                                     "verified_by": "GET /v1/pods/{id}: 404", "ended_at": now()}
                remote.atomic_json(path, handle)
                return
            sleep(min(2, max(0, deadline - now())))
    raise BuildError("cleanup unresolved; retain handle and rerun with --cleanup-only")


@contextlib.contextmanager
def bounded(seconds):
    """Bound even an unexpectedly stalled adapter; SIGTERM follows the same finally."""
    def timeout(_signum, _frame):
        raise TimeoutError("H3 build time limit exceeded")

    def interrupted(_signum, _frame):
        raise KeyboardInterrupt("H3 build interrupted")

    previous = {sig: signal.getsignal(sig) for sig in (signal.SIGALRM, signal.SIGTERM)}
    signal.signal(signal.SIGALRM, timeout)
    signal.signal(signal.SIGTERM, interrupted)
    signal.setitimer(signal.ITIMER_REAL, max(0.001, seconds))
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def cleanup(provider, handle, path, *, now=time.time, sleep=time.sleep):
    # Cleanup has its own hard limit within the 120 seconds reserved by the build.
    with bounded(110):
        previous = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
        for sig in previous:
            signal.signal(sig, signal.SIG_IGN)
        try:
            _cleanup(provider, handle, path, now=now, sleep=sleep)
        finally:
            for sig, handler in previous.items():
                signal.signal(sig, handler)


def build_release(provider, transport_factory, *, volume_id, handle_path, pod_type="cpu",
                  max_seconds=10800, claim_seconds=1800, max_hourly=0.30,
                  cleanup_only=False, now=time.time, sleep=time.sleep):
    if not 180 <= max_seconds <= 10800 or not 1 <= claim_seconds <= 1800:
        raise BuildError("max-seconds must be 180..10800; claim-seconds must be 1..1800")
    if not math.isfinite(max_hourly) or max_hourly <= 0:
        raise BuildError("max-hourly must be positive and finite")
    handle_path = Path(handle_path)
    handle_path.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(str(handle_path) + ".lock", timeout=0):
        inputs = release_inputs()
        handle = json.loads(handle_path.read_text()) if handle_path.exists() else None
        if handle and handle.get("volume_id") != volume_id:
            raise BuildError("handle belongs to a different volume")
        if handle and handle.get("pod_id") and handle.get("cleanup", {}).get("status") != "verified_absent":
            # Always reconcile a previous writer before claiming a replacement.
            cleanup(provider, handle, handle_path, now=now, sleep=sleep)
        elif handle and handle.get("state") == "allocating" and not handle.get("pod_id"):
            matches = provider.find(handle["request_name"])
            if not matches and cleanup_only:
                handle["state"] = "allocation_absent"
                handle["reconciliation"] = {"status": "no_matching_pod", "observed_at": now(),
                                            "verified_by": "GET /v1/pods: request_name absent"}
                remote.atomic_json(handle_path, handle)
                remote.atomic_json(handle_path.with_suffix(".receipt.json"), handle)
                return handle
            if len(matches) != 1:
                raise BuildError("allocation outcome unknown; reconcile request_name before using a new handle")
            handle["pod_id"] = matches[0]["id"]
            remote.atomic_json(handle_path, handle)
            cleanup(provider, handle, handle_path, now=now, sleep=sleep)
        if cleanup_only:
            if not handle:
                raise BuildError("no allocation handle to clean up")
            remote.atomic_json(handle_path.with_suffix(".receipt.json"), handle)
            return handle
        volume = normalize_volume(provider.volume(volume_id), volume_id)
        started = now()
        # Leave a cleanup budget inside the requested maximum billed time.
        deadline = started + max_seconds - 120
        claim_deadline = min(deadline, started + claim_seconds)
        handle = {"schema_version": "astrid.runpod.h3-build.v1", "volume_id": volume_id,
                  "request_name": "astrid-h3-build-" + uuid.uuid4().hex,
                  "started_at": started, "deadline": deadline, "max_seconds": max_seconds,
                  "max_hourly": max_hourly, "pod_type": pod_type,
                  "state": "allocating", "release_root": inputs["root"]}
        # The intent also covers the unavoidable lost-create-response window.
        remote.atomic_json(handle_path, handle)
        transport = None
        try:
            with bounded(deadline - now()):
                while True:
                    try:
                        pod = provider.create(volume, inputs["recipe"]["image"], pod_type,
                                              handle["request_name"])
                        break
                    except CapacityUnavailable:
                        if now() >= claim_deadline:
                            handle["state"] = "capacity_unavailable"
                            raise TimeoutError("build capacity wait exceeded")
                        sleep(min(10, max(0, claim_deadline - now())))
                # The first operation after create is a durable allocation handle.
                handle.update({"pod_id": pod["id"], "pod_created_at": now(), "state": "building"})
                remote.atomic_json(handle_path, handle)
                while True:
                    if now() >= claim_deadline:
                        raise TimeoutError("build pod SSH wait exceeded")
                    observed = provider.get(handle["pod_id"])
                    if observed is None:
                        raise BuildError("build pod disappeared")
                    check_attachment(observed, volume, inputs["recipe"]["image"], pod_type)
                    price = observed.get("costPerHr")
                    if (isinstance(price, bool) or not isinstance(price, (float, int))
                            or not math.isfinite(price) or price <= 0 or price > max_hourly):
                        raise BuildError("provider price is missing or exceeds --max-hourly")
                    handle["cost_per_hour"] = price
                    remote.atomic_json(handle_path, handle)
                    try:
                        transport = transport_factory(observed, min(10, claim_deadline - now()))
                        break
                    except (OSError, ConnectionError):
                        sleep(min(5, max(0, claim_deadline - now())))
                result = transport.provision({**inputs, "volume": volume, "deadline": deadline},
                                             max(1, int(deadline - now())))
                # Keep exact remote manifest bytes/digest for P2, without reserialization.
                manifest_bytes = base64.b64decode(result.pop("manifest_base64"), validate=True)
                if hashlib.sha256(manifest_bytes).hexdigest() != result["manifest_sha256"]:
                    raise BuildError("remote manifest digest mismatch")
                manifest = json.loads(manifest_bytes)
                if (manifest.get("release_id") != inputs["release_id"]
                        or manifest.get("candidate_namespace") != inputs["root"]
                        or manifest.get("volume") != volume):
                    raise BuildError("remote manifest returned a different release/volume")
                manifest_path = handle_path.with_suffix(".manifest.json")
                with manifest_path.open("wb") as stream:
                    stream.write(manifest_bytes)
                    stream.flush()
                    os.fsync(stream.fileno())
                handle.update({"state": "ready", "result": result, "manifest_path": str(manifest_path),
                               "manifest_digest": "sha256:" + result["manifest_sha256"]})
                remote.atomic_json(handle_path, handle)
        except BaseException as exc:
            if isinstance(exc, AllocationRejected) and not handle.get("pod_id"):
                handle["state"] = "allocation_rejected"
            elif handle["state"] != "capacity_unavailable":
                handle["state"] = "failed" if handle.get("pod_id") else "allocating"
            handle["error_type"] = type(exc).__name__
            # Do not persist provider exception text: it may contain credentials.
            remote.atomic_json(handle_path, handle)
            raise
        finally:
            try:
                if transport is not None:
                    transport.close()
            finally:
                if not handle.get("pod_id") and handle["state"] not in {"capacity_unavailable", "allocation_rejected"}:
                    # A POST can time out after allocation. Never retry an ambiguous POST.
                    matches = provider.find(handle["request_name"])
                    if len(matches) == 1:
                        handle["pod_id"] = matches[0]["id"]
                        remote.atomic_json(handle_path, handle)
                    else:
                        handle["state"] = "allocating"
                        remote.atomic_json(handle_path, handle)
                if handle.get("pod_id"):
                    try:
                        cleanup(provider, handle, handle_path, now=now, sleep=sleep)
                    finally:
                        remote.atomic_json(handle_path.with_suffix(".receipt.json"), handle)
        remote.atomic_json(handle_path.with_suffix(".receipt.json"), handle)
        return handle


class RunPodProvider:
    """Strict REST adapter: missing resources are only explicit 404s.

    lifecycle's GPU-only create helper cannot request CPU compute. Its status
    normalizer also drops volume identity and can conflate lookup errors with
    absence, so use the raw REST representation for these custody checks.
    """
    def __init__(self, key, *, min_cuda_version=None):
        import httpx
        self.min_cuda_version = min_cuda_version
        self.http = httpx.Client(base_url="https://rest.runpod.io/v1/", timeout=20,
                                 headers={"Authorization": "Bearer " + key})

    def request(self, method, path, **kwargs):
        response = self.http.request(method, path, **kwargs)
        if response.status_code == 404 and method in {"GET", "DELETE"}:
            return None
        if not response.is_success:
            if method == "POST" and response.status_code in {400, 500, 503} and any(
                marker in response.text.lower() for marker in
                ("no available", "no longer any instances", "not enough capacity", "out of stock")
            ):
                raise CapacityUnavailable("no compatible build capacity")
            detail = ""
            try:
                body = response.json()
                def messages(value):
                    if isinstance(value, str):
                        return [value]
                    if isinstance(value, list):
                        return [text for item in value for text in messages(item)]
                    if isinstance(value, dict):
                        return [text for name in ("message", "error", "errors", "detail", "details", "issues")
                                if name in value for text in messages(value[name])]
                    return []
                message = "; ".join(messages(body))
                if message:
                    for name in ("RUNPOD_API_KEY", "RUNPOD_SSH_PUBLIC_KEY"):
                        secret = os.environ.get(name)
                        if secret:
                            message = message.replace(secret, "[redacted]")
                    detail = ": " + message[:500] if message else ""
            except ValueError:
                pass
            error = AllocationRejected if method == "POST" and response.status_code in {400, 401, 403, 422} else BuildError
            raise error(f"RunPod {method} failed: HTTP {response.status_code}{detail}")
        return response.json() if response.content else {}

    def volume(self, volume_id):
        result = self.request("GET", "networkvolumes/" + quote(volume_id, safe=""))
        if result is None:
            raise BuildError("network volume does not exist")
        return result

    def create(self, volume, image, pod_type, name):
        if pod_type != "cpu" and self.min_cuda_version:
            inputs = {"name": name, "imageName": image, "gpuTypeId": pod_type,
                      "cloudType": "SECURE", "supportPublicIp": True, "startSsh": True,
                      "dataCenterId": volume["datacenter"], "gpuCount": 1, "volumeInGb": 0,
                      "containerDiskInGb": 20, "minVcpuCount": 2, "minMemoryInGb": 4,
                      "ports": "22/tcp", "volumeMountPath": volume["mount"],
                      "networkVolumeId": volume["id"], "minCudaVersion": self.min_cuda_version,
                      "env": [{"key": "PUBLIC_KEY", "value": os.environ["RUNPOD_SSH_PUBLIC_KEY"]}]}
            response = self.http.post("https://api.runpod.io/graphql", json={
                "query": "mutation($input: PodFindAndDeployOnDemandInput) { podFindAndDeployOnDemand(input: $input) { id name } }",
                "variables": {"input": inputs},
            })
            response.raise_for_status()
            result = response.json()
            if result.get("errors"):
                message = "; ".join(str(row.get("message", "provider error")) for row in result["errors"])
                for key in ("RUNPOD_API_KEY", "RUNPOD_SSH_PUBLIC_KEY"):
                    if os.environ.get(key):
                        message = message.replace(os.environ[key], "[redacted]")
                if "no longer any instances" in message.lower() or "no available" in message.lower():
                    raise CapacityUnavailable("no compatible build capacity")
                raise AllocationRejected("GPU minimum-CUDA placement rejected: " + message[:800])
            pod = result.get("data", {}).get("podFindAndDeployOnDemand")
            if not isinstance(pod, dict) or not pod.get("id"):
                raise BuildError("GPU placement returned no pod identity")
            return pod
        payload = {"name": name, "imageName": image, "cloudType": "SECURE",
                   "networkVolumeId": volume["id"], "volumeMountPath": volume["mount"],
                   "dataCenterIds": [volume["datacenter"]], "containerDiskInGb": 20,
                   "ports": ["22/tcp"], "supportPublicIp": True,
                   "env": {"PUBLIC_KEY": os.environ["RUNPOD_SSH_PUBLIC_KEY"]}}
        if pod_type == "cpu":
            payload.update({"computeType": "CPU", "vcpuCount": 2,
                            "cpuFlavorIds": ["cpu3c", "cpu5c", "cpu3g", "cpu5g", "cpu3m", "cpu5m"],
                            "cpuFlavorPriority": "custom"})
        else:
            payload.update({"computeType": "GPU", "gpuCount": 1, "gpuTypeIds": [pod_type],
                            "gpuTypePriority": "custom", "allowedCudaVersions": ["13.0"]})
        return self.request("POST", "pods", json=payload)

    def get(self, pod_id):
        return self.request("GET", "pods/" + quote(pod_id, safe=""))

    def delete(self, pod_id):
        self.request("DELETE", "pods/" + quote(pod_id, safe=""))

    def find(self, name):
        pods = self.request("GET", "pods")
        if not isinstance(pods, list):
            raise BuildError("provider pod discovery returned malformed data")
        return [pod for pod in pods if pod.get("name") == name]


class LifecycleTransport:
    def __init__(self, pod, connect_timeout):
        from runpod_lifecycle.ssh import SSHClient
        port = (pod.get("portMappings") or {}).get("22")
        ip = pod.get("publicIp")
        if not ip or not port:
            raise ConnectionError("SSH route is not ready")
        self.ssh = SSHClient(ip, int(port), "root", private_key_path=os.environ["RUNPOD_SSH_KEY_PATH"],
                             timeout=max(1, int(connect_timeout)))
        try:
            self.ssh.connect()
        except Exception:  # noqa: BLE001 - cleanup must survive provider failures
            self.ssh.disconnect()
            raise ConnectionError("build pod SSH is not ready") from None

    def provision(self, request, timeout):
        program = Path(remote.__file__).read_bytes()
        wrapper = ("import base64,json; ns={'__name__':'h3_release_provisioner'}; "
                   "exec(compile(base64.b64decode('" + base64.b64encode(program).decode() + "'),'<h3-release>','exec'),ns); "
                   "request=json.loads(base64.b64decode('" + base64.b64encode(json.dumps(request).encode()).decode() + "')); "
                   "result=ns['provision'](request); "
                   "result['manifest_base64']=base64.b64encode((ns['Path'](request['root'])/'release-manifest.json').read_bytes()).decode(); "
                   "print(json.dumps(result))")
        code, stdout, _stderr = self.ssh.execute_command("python3 -c " + shlex.quote(wrapper), timeout=timeout)
        if code:
            raise BuildError("remote H3 build failed; inspect runtime/build.log on the volume")
        try:
            return json.loads(stdout.splitlines()[-1])
        except (ValueError, IndexError) as exc:
            raise BuildError("remote builder returned no valid receipt") from exc

    def close(self):
        self.ssh.disconnect()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--volume", required=True)
    parser.add_argument("--pod-type", default="cpu", help="cpu or exact RunPod GPU type ID")
    parser.add_argument("--max-seconds", type=int, default=10800)
    parser.add_argument("--claim-seconds", type=int, default=1800)
    parser.add_argument("--max-hourly", type=float, default=0.30)
    parser.add_argument("--min-cuda-version", help="Minimum compatible GPU host CUDA capability instead of an exact version filter")
    parser.add_argument("--handle", type=Path, help="Durable local allocation/recovery handle")
    parser.add_argument("--cleanup-only", action="store_true")
    parser.add_argument("--plan", action="store_true", help="Print pinned inputs without provider calls")
    args = parser.parse_args(argv)
    if args.plan:
        print(json.dumps(release_inputs(), indent=2))
        return 0
    required = ["RUNPOD_API_KEY"]
    if not args.cleanup_only:
        required += ["RUNPOD_SSH_PUBLIC_KEY", "RUNPOD_SSH_KEY_PATH"]
    if any(not os.environ.get(name) for name in required):
        parser.error("required environment: " + ", ".join(required))
    if not re.fullmatch(r"[A-Za-z0-9_-]+", args.volume):
        parser.error("invalid volume ID")
    handle = args.handle or Path.cwd() / ("h3-build-" + args.volume + ".json")
    provider = RunPodProvider(os.environ["RUNPOD_API_KEY"], min_cuda_version=args.min_cuda_version)
    try:
        result = build_release(provider, LifecycleTransport, volume_id=args.volume,
                               handle_path=handle, pod_type=args.pod_type,
                               max_seconds=args.max_seconds, claim_seconds=args.claim_seconds,
                               max_hourly=args.max_hourly, cleanup_only=args.cleanup_only)
    except (Exception, KeyboardInterrupt) as exc:  # noqa: BLE001 - CLI boundary
        detail = f": {exc}" if isinstance(exc, BuildError) else ""
        print(f"H3 build stopped ({type(exc).__name__}){detail}; recovery handle: {handle}", file=sys.stderr)
        return 1
    finally:
        provider.http.close()
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
