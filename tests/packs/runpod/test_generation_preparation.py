"""F04 typed preparation through real R02 and bootstrap/install with fakes."""
from __future__ import annotations

import json
import socket
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from astrid.core.generation.interfaces import RunPodPreparationRequest
from astrid.packs.runpod.generation import RunPodGenerationPreparer
from astrid.packs.runpod.prepared_task import PreparedWorkerError, PreparedWorkerSelection, _claim_target
from astrid.packs.runpod.remote_placement import Claim
from tests.packs.runpod.test_remote_placement import FakePod, _provider, _wrapper, sources as sources
from tests.packs.runpod.test_target_selection import case as case
from tests.packs.runpod.test_prepared_task import _Client, _Runtime, _task
from tests.packs.runpod.test_warm_session import small_lease as small_lease, lease_selection_case as lease_selection_case


def _selected_runtime_commit():
    repo_root = Path(__file__).resolve().parents[3]
    return json.loads((repo_root / "runtime.lock.json").read_text())["source_commit"]


@pytest.fixture
def retained(small_lease, sources, tmp_path, monkeypatch, request):
    """Real adapter→A3→lifecycle→HTTPS→canonical runner, fake paid/OS edges."""
    import importlib.util
    import time
    from pathlib import Path
    import runpod_lifecycle
    from runpod_lifecycle import api, lifecycle, https
    from runpod_lifecycle.config import RunPodConfig
    from runpod_lifecycle.pod import Pod
    from astrid.core.generation import engine_recipe, engine_runner, engine_platforms
    from astrid.core.generation.engine_contracts import FileFact
    from astrid.packs.runpod import remote_placement as rp
    from astrid.packs.runpod.target_selection import RunPodPreferences, RunPodLeaseCompatibility
    from tests.core.generation import test_engine_runner as canonical

    c = small_lease
    mode = getattr(request, "param", True)
    persistent, restricted = mode if isinstance(mode, tuple) else (mode, True)
    raw = {**c.raw, "datacenter_id": "DC-accepted", "idle_timeout_seconds": 1800}
    if not restricted:
        raw.pop("datacenter_id")
    if not persistent:
        raw["volume"] = None
    c.prefs = RunPodPreferences.from_mapping(raw)
    recipe = engine_recipe.load_recipe("comfyui", "linux-x86_64-cu130")
    plan = engine_runner.plan_install(recipe, root="/root/astrid/engines/comfyui", placement="local")
    compatibility = RunPodLeaseCompatibility(
        "sha256:" + plan.recipe_sha256, "sha256:" + plan.models_lock_sha256,
        _selected_runtime_commit(), next(s for s in recipe.sources if s.name == "vibecomfy").pin.revision,
    )
    module_path = Path(rp.__file__).with_name("adapter.py")
    spec = importlib.util.spec_from_file_location("test_path_loaded_runpod_adapter", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert not c.owner.path.exists() and c.creates == 0
    c.module = module
    c.dc, c.quote_rate, c.stock, c.actual_rate = "DC-accepted", 1.0, "available", 1.0
    c.actual_gpu_id = "gpu-provider-exact"
    c.create_kwargs, c.quotes, c.git_reads = [], [], []
    c.lost_start, c.cancel_unknown, c.dirty_sources = False, False, False
    # Opt-in: the fake pod's install honours the command's own --deadline
    # (seconds, as ``astrid engines install`` parses it) on ``c.monotonic``.
    c.install_uses_command_deadline, c.monotonic = False, time.monotonic

    def volumes(key):
        assert persistent, "ephemeral route looked up network volumes"
        return [{"id": "foreign", "name": "storage", "size": 100, "dataCenterId": c.dc},
                {"id": c.prefs.volume_id, "name": c.prefs.volume_name,
                 "size": c.prefs.volume_size_gb, "dataCenterId": "DC-accepted"}]

    async def availability(**kw):
        c.quotes.append(kw)
        return SimpleNamespace(gpu_type_id="gpu-provider-exact", datacenters=[SimpleNamespace(
            status=c.stock, price_per_hour=c.quote_rate, datacenter_id=c.dc)])

    monkeypatch.setattr(api, "get_network_volumes", volumes)
    monkeypatch.setattr(api, "get_pod_status", lambda *a: {"cost_per_hr": c.actual_rate})
    monkeypatch.setattr(runpod_lifecycle, "gpu_availability", availability)
    c.owner = module.broker_selection(
        c.prefs, RunPodConfig(api_key="fake-only", transport="https", https_token="test-only-" + "x" * 40,
                             container_disk_gb=40), account_ref="account-a",
        data_root=tmp_path / "machine", compatibility=compatibility,
    )
    c.owner.clock = lambda: datetime.fromtimestamp(c.clock(), timezone.utc)
    c.grant = replace(c.grant, offer_digest=c.owner.observe_offer().digest(c.prefs),
                      preparation_seconds=60, execution_seconds=60, idle_seconds=1800)

    def create(**kw):
        journal = c.read()
        assert journal["state"] == "allocation_pending" and journal["lease"]["state"] == "preparing"
        assert journal["network_volume_id"] == kw["network_volume_id"] == c.prefs.volume_id
        assert journal["network_volume_datacenter_id"] == kw["data_center_id"] == "DC-accepted"
        assert journal["accepted_gpu_type_id"] == kw["gpu_type_id"] == "gpu-provider-exact"
        assert https.read_token_file(journal["https_token_file"]) == kw["https_token"]
        assert kw["https_token"] not in c.owner.path.read_text()
        c.create_kwargs.append(kw)
        c.creates += 1
        pod = {"id": f"pod-{c.creates}", "name": kw["name"], "desiredStatus": "RUNNING",
               "actualStatus": "RUNNING", "machineType": c.prefs.gpu_types[0],
               "machine": {"gpuTypeId": c.actual_gpu_id, "dataCenterId": kw["data_center_id"]},
               "imageName": kw["image_name"], "networkVolumeId": kw["network_volume_id"],
               "dataCenterId": kw["data_center_id"]}
        c.pods.append(pod)
        return pod

    async def ready(*a, **kw):
        pass
    monkeypatch.setattr(runpod_lifecycle, "launch_when_available", lifecycle.launch_when_available)
    monkeypatch.setattr(lifecycle, "create_pod", create)
    monkeypatch.setattr(lifecycle, "find_gpu_type", lambda *a: pytest.fail("accepted GPU ID was re-resolved"))
    monkeypatch.setattr(lifecycle, "get_storage_volume_id", lambda *a: pytest.fail("exact ID became a name lookup"))
    monkeypatch.setattr(lifecycle, "check_and_expand_storage", lambda *a: pytest.fail("accepted volume was resized"))
    monkeypatch.setattr(Pod, "wait_ready", ready)

    def git_read(argv, **kw):
        c.git_reads.append(argv)
        revision = compatibility.runtime_commit if argv[2].endswith("astrid-runtime") else compatibility.vibecomfy_commit
        return SimpleNamespace(stdout=(" M source" if c.dirty_sources else "")
                               if "status" in argv else revision)
    monkeypatch.setattr(rp, "subprocess", SimpleNamespace(run=git_read))
    installation = rp.RunPodInstallation(compatibility.as_mapping())
    provider = _provider(sources)
    placement = _wrapper(provider, sources)
    # Reuse the canonical runner's fake OS/model/environment context. The
    # receipt below is emitted and inspected by production installer code.
    monkeypatch.setattr(canonical, "ROOT", installation.root)
    context = canonical.FakeContext()
    context.recipe = recipe
    context.health_objects = {node: {} for node in recipe.health.required_nodes}
    context.deadline = time.monotonic() + 60
    monkeypatch.setattr(engine_platforms, "platform_adapter", lambda key: context.adapter)

    c.mount_id = 7

    class RetainedPod(FakePod):
        def exec(self, command, timeout=600):
            if command.startswith("python3 -c ") and "ASTRID_STORAGE_DIAGNOSTIC" in command:
                # The shipped storage probe: answer with the receipt it prints.
                self.exec_log.append("storage-probe")
                import base64, shlex
                request = json.loads(base64.b64decode(shlex.split(command)[-1]))
                mapping = request.pop("provider_mapping")
                assert mapping["volume_mounts"][0]["volume_id"] == request["volume_id"]
                return 0, json.dumps({**request, "cache_device": 64768, "cache_mount_id": c.mount_id,
                                      "free_bytes": 10**12, "total_bytes": 2 * 10**12,
                                      "local_free_bytes": 10**12}), ""
            if "engines status" in command:
                self.exec_log.append(command)
                if not self.installed:
                    return 0, "null", ""
                status = engine_runner.engine_status(
                    "comfyui", platform=installation.platform, placement="local", root=installation.root,
                    context=context, deadline=context.deadline,
                )
                return 0, engine_runner._json(status), ""
            return super().exec(command, timeout)

        def job_start(self, command, *, job_id):
            lease = c.read()["lease"]
            # Preparation jobs and the machine's generation job are each checkpointed before start.
            # The release-time reset is checkpointed on the lease; the claim-time one on the preparation.
            custody = (lease["job"] if " generate " in command or ("pod_reset" in command and "--run-dir" in command)
                       else lease["preparation"]["job"])
            assert custody == {"id": job_id, "state": "starting"}
            old = super().job_start(command)
            self.jobs[job_id] = self.jobs.pop(old["id"])
            directory = "/workspace/.astrid-jobs/" + job_id
            for name in ("stdout", "stderr"):
                self.files[directory + "/" + name] = self.files.pop(old["dir"] + "/" + name)
            if self.active_job and self.active_job.get("job_dir") == old["dir"]:
                self.active_job["job_dir"] = directory  # the agent folder under its checkpointed id
            if "engines install" in command and not self.jobs[job_id] and c.install_uses_command_deadline:
                seconds = float(command.split("--deadline ", 1)[1].split()[0])
                try:
                    receipt = engine_runner.install_engine(plan, context=context, adapter=context.adapter,
                                                           deadline=c.monotonic() + seconds)
                except Exception as exc:  # the pod-side process exits non-zero
                    self.jobs[job_id] = 1
                    self.files[directory + "/stderr"] = f"{type(exc).__name__}: {exc}".encode()
                else:
                    self.files[installation.root + "/.astrid-engine/install.json"] = engine_runner._json(receipt).encode()
            elif "engines install" in command and not self.jobs[job_id]:
                receipt = engine_runner.install_engine(plan, context=context, adapter=context.adapter,
                                                       deadline=context.deadline)
                self.files[installation.root + "/.astrid-engine/install.json"] = engine_runner._json(receipt).encode()
            if c.lost_start:
                raise TimeoutError("lost detached-start ACK")
            return {"id": job_id, "dir": directory}

        def job_cancel(self, job_id):
            self.cancelled.append(job_id)
            return {"id": job_id, "state": "missing" if c.cancel_unknown else "cancelled"}

    pod = RetainedPod({}, provider.marker)
    class Transport:
        def __init__(self, pod_id, token):
            assert pod_id == "pod-1"
            assert token == https.read_token_file(c.read()["https_token_file"])
        def execute_command(self, *a): return pod.exec(*a)
        def put(self, *a): return pod.put(*a)
        def get(self, *a): return pod.get(*a)
        def detached_start(self, *a, **kw): return pod.job_start(*a, **kw)
        def detached_poll(self, *a): return pod.job_poll(*a)
        def detached_cancel(self, *a): return pod.job_cancel(*a)
        def disconnect(self): pass
    monkeypatch.setattr(https, "HttpsPodTransport", Transport)
    c.host = c.fresh()
    c.installation, c.placement, c.pod, c.provider, c.context = installation, placement, pod, provider, context
    c.adapter = module.build_adapter(lease_owner=c.host, placement=placement, installation=installation,
                                    validate=lambda r: None)

    def observe_mounts(pod_id, nonce):
        # Shape of runpod_lifecycle.discovery.observe_pod_mounts (f2948817).
        c.mount_observations = getattr(c, "mount_observations", 0) + 1
        return {"schema": "runpod.provider-mounts.v1", "pod_id": pod_id, "nonce": nonce,
                "volume_mounts": [{"volume_id": c.prefs.volume_id, "volume_type": "NETWORK_VOLUME",
                                   "mount_path": c.prefs.mount_path, "read_only": False}],
                "legacy_evidence": {"present": False, "valid": True, "conflict": False}}
    c.adapter.observe_mounts = observe_mounts
    source, workflow = tmp_path / "entry.json", tmp_path / "compiled.py"
    source.write_text("{}")
    workflow.write_text("# CPU fake compiled workflow")
    c.request = RunPodPreparationRequest({"engine": "comfyui", "kind": "workflow"}, source, workflow,
                                        {}, c.grant.consent, c.grant.max_usd, c.grant.deadline)
    return c


@pytest.mark.parametrize("retained", [True, False, (True, False), (False, False)], indirect=True)
def test_path_loaded_adapter_cold_and_warm_use_exact_placement_canonical_install_and_a3_release(retained):
    c = retained
    first = c.adapter.prepare(c.request, c.grant)
    assert c.creates == 1 and c.provider.claims == c.provider.releases == 0
    assert all(q["require_network_volume"] == (c.prefs.volume_id is not None) for q in c.quotes)
    assert all(q["datacenter_ids"] == (["DC-accepted"] if c.prefs.volume_id or c.prefs.datacenter_id else None)
               for q in c.quotes)
    assert first.installed["compatibility"] == c.installation.compatibility
    assert c.read()["lease"]["preparation"]["state"] == "prepared"
    assert "ordinary_preparation" not in c.read()
    install_commands = [line for line in c.pod.job_log if "engines install" in line]
    assert len(install_commands) == 1
    assert "install comfyui --platform linux-x86_64-cu130 --placement local" in install_commands[0]
    assert "vibecomfy-h3" not in install_commands[0]
    c.host.settle(first.lease)
    jobs = list(c.pod.job_log)
    second = c.adapter.prepare(c.request, replace(c.grant, invocation_id="invocation-2"))
    assert second.installed == first.installed and second.lease.generation == 2
    assert c.creates == 1 and c.pod.job_log == jobs
    with pytest.raises(Exception, match="another holder"):
        c.host.settle(first.lease)
    c.host.settle(second.lease)
    c.clock.advance(1800)
    assert c.deletes == ["pod-1"]
    assert c.read()["state"] == "cleanup_complete"
    assert c.read()["cleanup"]["absence"]["exact_get_status"] == 404


def test_retained_installation_accepts_only_the_existing_h3_recipe_and_root():
    from astrid.core.generation import engine_recipe, engine_runner
    from astrid.packs.runpod.remote_placement import RunPodInstallation
    from astrid.packs.runpod.target_selection import RunPodLeaseCompatibility

    recipe = engine_recipe.load_recipe("comfyui", "linux-x86_64-cu130")
    root = "/root/astrid/engines/comfyui"
    expected = engine_runner.plan_install(recipe, root=root, placement="local")
    vibe = next(source for source in recipe.sources if source.name == "vibecomfy")
    compatibility = RunPodLeaseCompatibility(
        "sha256:" + expected.recipe_sha256, "sha256:" + expected.models_lock_sha256,
        _selected_runtime_commit(), vibe.pin.revision,
    ).as_mapping()
    installation = RunPodInstallation(compatibility, engine="comfyui", root=root)
    assert installation.plan() == expected
    with pytest.raises(Exception, match="unsupported retained installation"):
        replace(installation, root="/root/astrid/engines/other").plan()


@pytest.mark.parametrize("change", ["denied", "cap", "rate", "datacenter", "stock", "engine", "tuple", "dirty", "validate"])
def test_retained_adapter_refuses_unsupported_or_unconsented_routes_before_create(retained, change):
    c = retained
    request, grant = c.request, c.grant
    if change == "denied": grant = replace(grant, consent=()); request = replace(request, consent=())
    if change == "cap": grant = replace(grant, max_usd=0.0001); request = replace(request, max_usd=0.0001)
    if change == "rate": c.quote_rate = 2.0
    if change == "datacenter": c.dc = "other"
    if change == "stock": c.stock = "unavailable"
    if change == "engine": request = replace(request, entry={"engine": "wan"})
    if change == "tuple":
        c.adapter.installation = replace(c.installation, compatibility={**c.installation.compatibility, "model_lock_digest": "sha256:" + "f" * 64})
    if change == "dirty": c.dirty_sources = True
    if change == "validate":
        c.adapter.validate = lambda r: (_ for _ in ()).throw(ValueError("invalid workflow"))
    with pytest.raises((ValueError, RuntimeError)):
        c.adapter.prepare(request, grant)
    assert c.creates == 0 and not c.owner.path.exists()
    assert not c.pod.job_log


def test_retained_unknown_detached_start_retains_exact_job_and_a3_reconciles_before_delete(retained):
    c = retained
    c.lost_start = c.cancel_unknown = True
    with pytest.raises(TimeoutError, match="lost detached-start"):
        c.adapter.prepare(c.request, c.grant)
    record = c.read()
    job = record["lease"]["preparation"]["job"]
    assert record["state"] == "cleanup_pending" and job["state"] == "starting"
    assert c.pod.cancelled == [job["id"]]
    with pytest.raises(Exception, match="unresolved"):
        c.adapter.prepare(c.request, replace(c.grant, invocation_id="retry"))
    assert c.creates == 1 and len(c.pod.job_log) == 1
    with pytest.raises(Exception, match="stop remains unresolved"):
        c.host.release()
    assert not c.deletes
    c.cancel_unknown = False
    c.host.release()
    assert c.deletes == ["pod-1"]
    assert c.read()["lease"]["preparation"]["job"] == {"id": job["id"], "state": "cancelled"}


def test_retained_install_failure_stays_with_a3_and_does_not_replay(retained):
    c = retained
    c.pod.fail_jobs.add("engines install")
    with pytest.raises(Exception, match="engine install"):
        c.adapter.prepare(c.request, c.grant)
    record = c.read()
    assert record["lease"]["preparation"]["job"]["state"] == "exited"
    assert record["lease"]["preparation"]["state"] == "unresolved"
    with pytest.raises(Exception, match="unresolved"):
        c.adapter.prepare(c.request, replace(c.grant, invocation_id="retry"))
    c.host.release()
    assert c.creates == 1 and c.deletes == ["pod-1"]


def test_retained_slow_model_download_stops_at_preparation_seconds_and_releases(retained, monkeypatch, tmp_path):
    """--preparation-seconds bounds the whole preparation, including the model download."""
    import time as real_time
    from astrid.core.generation import engine_runner, model_downloader
    from tests.core.generation import test_engine_runner as canonical

    c = retained
    assert c.grant.preparation_seconds == 60
    # The paid claim lifetime (USD cap at the quoted rate) alone would not bound preparation.
    assert c.grant.max_usd / c.actual_rate * 3600 > 10 * c.grant.preparation_seconds

    class FakeTime:  # one fake clock for controller, pod-side runner and downloader
        def __getattr__(self, name):
            return getattr(real_time, name)
        monotonic = staticmethod(c.clock)
        sleep = staticmethod(lambda seconds: None)
    for module in (engine_runner, model_downloader, canonical):
        monkeypatch.setattr(module, "time", FakeTime())
    c.placement._clock = c.clock
    c.monotonic = c.clock
    c.install_uses_command_deadline = True
    c.context.deadline = c.clock() + 10 ** 7
    started = c.clock()
    transferred = []

    class SlowRange:  # a stalled transfer: 1 KiB per 5 fake seconds, never complete
        status = 206

        def __init__(self, request):
            offset, end = request.headers["Range"].removeprefix("bytes=").split("-")
            self.headers = {"Content-Range": f"bytes {offset}-{end}/{self.total}"}

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self, size):
            c.clock.now += 5
            transferred.append(1024)
            return b"\0" * min(1024, size)

    def download(model, *, models_root, deadline):
        SlowRange.total = model.bytes
        return model_downloader.download_model(
            model, models_root=str(tmp_path / "models"), deadline=deadline,
            opener=lambda request, timeout: SlowRange(request))
    c.context.download_model = download

    with pytest.raises(Exception, match=r"preparation deadline of 60s exceeded during engine install"):
        c.adapter.prepare(c.request, c.grant)
    # The machine releases a failed preparation through the same A3 owner.
    c.host.release(reason="preparation_failed")
    assert transferred, "the fake download never started"
    assert c.clock() - started <= c.grant.preparation_seconds + 5
    install = next(line for line in c.pod.job_log if "engines install" in line)
    assert float(install.split("--deadline ", 1)[1].split()[0]) <= c.grant.preparation_seconds
    record = c.read()
    job = record["lease"]["preparation"]["job"]
    assert record["lease"]["preparation"]["state"] == "unresolved"
    assert c.pod.cancelled == [job["id"]] and job["state"] == "cancelled"
    assert c.creates == 1 and c.deletes == ["pod-1"]
    assert record["state"] == "cleanup_complete"


def test_retained_snapshot_bytes_changed_refuses_warm_reinstall(retained):
    c = retained
    prepared = c.adapter.prepare(c.request, c.grant)
    c.host.settle(prepared.lease)
    key = c.installation.root + "/.astrid-engine/install.json"
    raw = json.loads(c.pod.files[key])
    raw["receipt_id"] = "different-installed-receipt"
    c.pod.files[key] = json.dumps(raw).encode()
    jobs = list(c.pod.job_log)
    with pytest.raises(Exception, match="differs from its canonical receipt"):
        c.adapter.prepare(c.request, replace(c.grant, invocation_id="next"))
    assert c.pod.job_log == jobs and c.creates == 1
    c.host.release()
    assert c.deletes == ["pod-1"]


def test_retained_actual_gpu_id_mismatch_never_starts_preparation(retained, monkeypatch):
    # The accepted GPU id is confirmed from the provider's GraphQL pod.machine / gpuTypes catalog
    # (REST has no gpuTypeId on a fresh pod); the acquisition-time confirmation (stubbed) passed.
    from astrid.packs.runpod import worker_preparation
    from astrid.packs.runpod.worker_preparation import TargetObservationError
    c = retained
    c.actual_gpu_id = "wrong-provider-gpu"

    def provider_confirmation(api_key, contract, *, timeout_seconds):
        if contract.gpu_type == "gpu-provider-exact":
            raise TargetObservationError(f"GPU mismatch: provider reports {c.actual_gpu_id!r}")
        return contract.gpu_type, contract.datacenter_id
    monkeypatch.setattr(worker_preparation, "confirm_acquired_machine", provider_confirmation)
    with pytest.raises(Exception, match="requires cleanup"):
        c.adapter.prepare(c.request, c.grant)
    assert c.creates == 1 and not c.pod.job_log
    assert c.read()["state"] == "cleanup_pending"
    c.host.release()
    assert c.deletes == ["pod-1"]


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("generation preparation test attempted network access")
    monkeypatch.setattr(socket.socket, "connect", forbidden)


@pytest.fixture
def generation(case, sources, tmp_path):
    case.now = datetime.now(timezone.utc)
    case.grant = replace(case.grant, deadline=(case.now + timedelta(hours=1)).isoformat())
    events = []
    provider = _provider(sources)
    placement = _wrapper(provider, sources)
    pod = FakePod({}, provider.marker)
    source, workflow = tmp_path / "source.json", tmp_path / "workflow.py"
    source.write_text('{"nodes": {}}')
    workflow.write_text("# normalized fake workflow")
    request = RunPodPreparationRequest(
        {"engine": "comfyui", "id": "image/fake", "kind": "image"}, source, workflow,
        {"seed": 17}, case.grant.consent, case.grant.max_usd, case.grant.deadline,
    )
    def validate(request):
        events.append("validate")
    def connect(claim):
        events.append("connect")
        return Claim(claim["pod_id"], pod, dict(claim))
    def select(claim):
        events.append("select-worker")
        return PreparedWorkerSelection(case.owner.path, tmp_path / "profile", tmp_path / "staging",
                                       tmp_path / "journal", _claim_target(claim), {}, {})
    preparer = RunPodGenerationPreparer(
        owner=case.owner, placement=placement, validate=validate, connect_claim=connect,
        select_worker=select, consent=case.grant,
    )
    return SimpleNamespace(case=case, preparer=preparer, request=request, events=events,
                           provider=provider, pod=pod)


@pytest.mark.parametrize("entry", [
    {"id": "image/fake", "kind": "image"}, {"id": "video/fake", "kind": "video"},
    {"kind": "workflow"}, {"id": "h3_av", "kind": "video"},
])
def test_ordinary_entry_and_raw_workflow_prepare_exact_claim_without_transform(generation, entry):
    g = generation
    prepared = g.preparer.prepare(replace(g.request, entry={"engine": "comfyui", **entry}))
    assert g.events == ["validate", "connect", "select-worker"]
    assert g.case.creates == 1
    assert g.provider.claims == g.provider.releases == 0
    assert g.pod.installed
    assert all("h3_av.transform" not in command for command in g.pod.job_log)
    request = prepared.execution_request(None)
    assert request["remote_activation_required"] is True
    assert request["target"] == prepared.selection.target
    assert request["target"]["mounts"][0]["target"] == "/workspace"
    saved = json.loads(g.case.owner.path.read_text())
    assert saved["ordinary_preparation"] == prepared.receipt
    assert prepared.receipt["claim_operation_id"] == saved["operation_id"]
    assert prepared.receipt["state"] == "prepared"
    assert prepared.inputs == {"seed": 17}
    assert "inputs" not in prepared.receipt  # CAS remains Runtime's responsibility.


@pytest.mark.parametrize("change", [
    {"entry": {"engine": "wan"}}, {"max_usd": 99}, {"consent": ()},
])
def test_unsupported_engine_or_undisclosed_limits_refuse_before_claim(generation, change):
    g = generation
    with pytest.raises(PreparedWorkerError):
        g.preparer.prepare(replace(g.request, **change))
    assert g.case.creates == 0
    assert not g.case.owner.path.exists()


def test_source_validation_failure_happens_before_claim_or_offer(generation):
    g = generation
    def invalid(request):
        raise ValueError("invalid raw workflow input")
    g.preparer.validate = invalid
    g.case.owner.observe_offer = lambda: pytest.fail("invalid source reached offer observation")
    with pytest.raises(ValueError, match="invalid raw workflow"):
        g.preparer.prepare(g.request)
    assert g.case.creates == 0


def test_ambiguous_install_retains_cleanup_and_cannot_replay(generation):
    g = generation
    g.pod.fail_jobs.add("engines install")
    with pytest.raises(Exception, match="engine install"):
        g.preparer.prepare(g.request)
    saved = json.loads(g.case.owner.path.read_text())
    assert saved["state"] == "cleanup_pending"
    assert saved["ordinary_preparation"]["state"] == "unresolved"
    calls = list(g.pod.job_log)
    with pytest.raises(PreparedWorkerError, match="unresolved"):
        g.preparer.prepare(g.request)
    assert g.case.creates == 1
    assert g.pod.job_log == calls


def test_preparation_expiry_never_admits_worker(generation):
    from datetime import timedelta
    g = generation
    original = g.preparer.placement.prepare_claimed
    def expires(*args, **kwargs):
        result = original(*args, **kwargs)
        g.case.now += timedelta(hours=2)
        return result
    g.preparer.placement.prepare_claimed = expires
    with pytest.raises(PreparedWorkerError, match="paid lifetime"):
        g.preparer.prepare(g.request)
    assert "select-worker" not in g.events
    assert json.loads(g.case.owner.path.read_text())["state"] == "cleanup_pending"


def test_bound_client_hands_normalized_inputs_to_ordinary_prepared_boundary(generation):
    g = generation
    prepared = g.preparer.prepare(g.request)
    calls = []
    selection = replace(prepared.selection, composition_factory=lambda **kwargs: calls.append(kwargs) or object())
    prepared = replace(prepared, selection=selection)
    client = object()
    bound = prepared.bind_client(client, workflow_path=g.request.workflow,
                                 workflow_bundle_path=g.request.workflow.parent)
    assert bound._client is client
    assert calls[0]["workflow_inputs"] == {"seed": 17}
    with pytest.raises(PreparedWorkerError, match="inputs changed"):
        prepared.bind_client(client, workflow_path=g.request.workflow,
                             workflow_bundle_path=g.request.workflow.parent, workflow_inputs={"seed": 18})


@pytest.mark.parametrize("entry", [
    {"id": "image/fake", "kind": "image"}, {"id": "video/fake", "kind": "video"},
    {"kind": "workflow"}, {"id": "h3_av", "kind": "video"},
])
def test_preparation_no_wait_and_follow_keep_one_claim_and_exact_task(generation, entry, tmp_path):
    """Exercise the pack handoff; the public A10 follow bridge is a separate dependency."""
    g = generation
    prepared = g.preparer.prepare(replace(g.request, entry={"engine": "comfyui", **entry}))
    execution = prepared.execution_request(None)
    binding = {"target": execution["target"]}
    task = _task(execution_binding=binding)
    settled = _task(state="succeeded", execution_binding=binding)
    client = _Client([task, settled])
    runtime = _Runtime(client.events)
    selection = replace(prepared.selection, composition_factory=lambda **kwargs: runtime)
    prepared = replace(prepared, selection=selection)
    bound = prepared.bind_client(client, workflow_path=g.request.workflow,
                                 workflow_bundle_path=g.request.workflow.parent)

    assert bound.invoke_result(
        "vibecomfy.run", kind="executor", wait=False,
        recovery_path=str(tmp_path / "receipt.json"), execution_request=execution,
    ) is client.first
    bound.monitor_task(task)

    assert [event[0] for event in client.events] == ["invoke", "show", "activate", "show", "retire"]
    assert client.events[0][3]["execution_request"] == execution
    assert client.events[-1] == ("retire", settled)
    assert g.case.creates == 1
    assert g.provider.claims == g.provider.releases == 0
    assert sum("engines install" in command for command in g.pod.job_log) == 1


def test_missing_sdk_cancel_bridge_retains_custody_and_reports_stop_unknown(generation):
    admission = SimpleNamespace(task_id="task-exact")
    assert generation.preparer.cancel(admission=admission, reason="user") == {
        "task_id": "task-exact", "state": "cancellation_unresolved",
        "custody_retained": True, "observed_stop": False,
    }


def test_host_adapter_composition_quotes_approved_cuda_version(monkeypatch, tmp_path):
    """The real composition admits a 13.1 host at the provider HTTP boundary."""
    import importlib.util
    import httpx
    from astrid.packs.runpod import adapter as adapter_module
    from astrid.packs.runpod.cuda_versions import DEFAULT_ALLOWED_CUDA_VERSIONS

    source = Path(adapter_module.__file__)
    spec = importlib.util.spec_from_file_location("test_host_composed_runpod_adapter", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    observed = []
    host_cuda = "13.1"

    def send(client, request, **kwargs):
        assert request.method == "POST" and request.url.host in {"api.runpod.io", "graphql.runpod.io"}
        body = json.loads(request.content)
        query, variables = body["query"], body.get("variables", {})
        if "GpuCatalogue" in query:
            data = {"gpuTypes": [{"id": "gpu-exact", "displayName": "GPU fixture"}]}
        elif "DataCenterStock" in query:
            allowed = variables["availability"]["allowedCudaVersions"]
            observed.append(tuple(allowed))
            assert tuple(allowed) == DEFAULT_ALLOWED_CUDA_VERSIONS
            data = {"dataCenters": [{"id": "DC-test", "storageSupport": False,
                "gpuAvailability": [{"gpuTypeId": "gpu-exact", "available": host_cuda in allowed,
                                     "stockStatus": "High" if host_cuda in allowed else "None"}]}]}
        else:
            assert "DataCenterPrices" in query
            assert tuple(variables["p0"]["allowedCudaVersions"]) == DEFAULT_ALLOWED_CUDA_VERSIONS
            data = {"gpuTypes": [{"id": "gpu-exact", "dc0": {
                "stockStatus": "High", "uninterruptablePrice": 1.0}}]}
        return httpx.Response(200, request=request, json={"data": data})

    monkeypatch.setattr(httpx.Client, "send", send)
    monkeypatch.setattr(socket.socket, "connect", lambda *args: pytest.fail("external socket forbidden"))
    monkeypatch.setenv("ASTRID_RUNTIME_DATA_ROOT", str(tmp_path / "runtime-data"))
    adapter = module.build_host_adapter(
        preferences={"provider_account_ref": "cuda-policy", "gpu_types": ["GPU fixture"],
                     "image": "fixture/cuda130@sha256:" + "1" * 64},
        engine="comfyui", holds=lambda *a, **k: None,
        quiesce=lambda *a, **k: None, credentials=lambda name: "fake-only",
        environ={},
    )
    try:
        quote = adapter.quote()
        assert quote["offer"]["gpu_type_id"] == "gpu-exact"
        assert observed == [DEFAULT_ALLOWED_CUDA_VERSIONS] and host_cuda in observed[0]
    finally:
        adapter.lease_owner.close()

