"""W1: the RunPod placement wrapper, with the provider and HTTPS stubbed (CPU only)."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from astrid.packs.runpod import remote_placement as rp
from astrid.packs.runpod.remote_placement import (
    Claim,
    DigestMismatch,
    Limits,
    RemotePlacement,
    RemotePlacementError,
)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class FakePod:
    """In-memory pod: a persistent volume (files), an agent and detached jobs."""

    def __init__(self, files: dict[str, bytes], marker: str):
        self.files = files  # shared across claims: the network volume
        self.marker = marker
        self.installed = False
        self.jobs: dict[str, str] = {}
        self.exec_log: list[str] = []
        self.job_log: list[str] = []
        self.put_log: list[str] = []
        self.fail_jobs: set[str] = set()  # substrings of commands that exit 1
        self.corrupt_put = False
        self.corrupt_get: str | None = None
        self.corrupt_manifest = False
        self.cancelled: list[str] = []

    # --- agent surface
    def exec(self, command, timeout=600):
        self.exec_log.append(command)
        if command.startswith("test -f "):
            path = command.split(" ", 2)[2].strip("'")
            return (0 if path in self.files else 1), "", ""
        if command.startswith("sha256sum "):
            path = command.split(" ", 1)[1].strip("'")
            if path not in self.files:
                return 1, "", "missing"
            return 0, f"{_sha(self.files[path])}  {path}\n", ""
        if "engines status" in command:
            body = {"status": "installed", "health": {"status": "healthy"}} if self.installed else None
            return 0, json.dumps(body) + "\n", ""
        if command.startswith("cat ") or command.startswith("tail "):
            path = command.split()[-1].strip("'")
            return 0, self.files.get(path, b"").decode(), ""
        return 0, "", ""

    def put(self, local, remote):
        self.put_log.append(remote)
        data = Path(local).read_bytes()
        self.files[remote] = data + b"X" if self.corrupt_put else data

    def get(self, remote, local):
        data = self.files[remote]
        if self.corrupt_get and remote.endswith(self.corrupt_get):
            data = data + b"tampered"
        Path(local).write_bytes(data)

    def job_start(self, command):
        self.job_log.append(command)
        job_id = f"{len(self.job_log):032x}"
        directory = f"/workspace/.astrid-jobs/{job_id}"
        rc = 1 if any(marker in command for marker in self.fail_jobs) else 0
        stdout = ""
        if rc == 0:
            if "pod_bootstrap.sh" in command:
                self.files[self.marker] = b"ok"
            if "engines install" in command:
                self.installed = True
            if "action run h3_av.transform" in command:
                stdout = self._transform(command)
        self.files[f"{directory}/stdout"] = stdout.encode()
        self.files[f"{directory}/stderr"] = b"boom" if rc else b""
        self.jobs[job_id] = rc
        return {"id": job_id, "dir": directory}

    def job_poll(self, job_id):
        return {"id": job_id, "state": "exited", "exit_code": self.jobs[job_id]}

    def job_cancel(self, job_id):
        self.cancelled.append(job_id)
        return {}

    def _transform(self, command: str) -> str:
        parts = command.split()
        out = parts[parts.index("--out") + 1].strip("'")
        candidate = b"candidate-bytes"
        outputs = {
            f"{out}/01-prepare/preparation.json": b'{"status": "prepared"}',
            f"{out}/05-compose/candidate.media": candidate,
            f"{out}/05-compose/composition-manifest.json": json.dumps(
                {"status": "composed", "candidate": {"sha256": _sha(candidate)}}
            ).encode(),
            f"{out}/08-final-receipt.json": b'{"overall_status": "candidate_verified"}',
            f"{out}/operation-state.json": b'{"journal": []}',
            f"{out}/04-run/huge-intermediate.bin": b"not declared",
        }
        self.files.update(outputs)
        result = {
            "status": "candidate_verified",
            "preparation": f"{out}/01-prepare/preparation.json",
            "candidate": f"{out}/05-compose/candidate.media",
            "composition": f"{out}/05-compose/composition-manifest.json",
            "final_receipt": f"{out}/08-final-receipt.json",
            "generated_audio": None,
        }
        prefix = out + "/"
        files = {
            path[len(prefix):]: {"sha256": _sha(data), "size": len(data)}
            for path, data in self.files.items()
            if path.startswith(prefix)
        }
        body = json.dumps({"schema_version": 1, "files": files}, sort_keys=True, indent=2) + "\n"
        if self.corrupt_manifest:
            body = body.replace("candidate", "candidatX", 1)
        self.files[f"{out}/{rp.MANIFEST_NAME}"] = body.encode()
        reported = _sha(body.encode()) if not self.corrupt_manifest else "0" * 64
        return f"{rp.RESULT_MARK}{json.dumps(result)}\n{rp.MANIFEST_MARK}{reported}\n"


class FakeProvider:
    def __init__(self, marker: str, volume: dict[str, bytes] | None = None):
        self.volume = volume if volume is not None else {}
        self.marker = marker
        self.claims = 0
        self.releases = 0
        self.existing: set[str] = set()
        self.pods: list[FakePod] = []
        self.claim_kwargs: dict = {}
        self.claim_error: BaseException | None = None
        self.release_error: BaseException | None = None
        self.leak_after_release = False

    def claim(self, spec):
        self.claims += 1
        self.spec = spec
        if self.claim_error is not None:
            raise self.claim_error
        pod = FakePod(self.volume, self.marker)
        # the engine state lives on the volume: remember it across claims
        pod.installed = self.volume.get("__installed__") == b"1"
        self.pods.append(pod)
        pod_id = f"pod-{self.claims}"
        self.existing.add(pod_id)
        kwargs = dict(
            cost_per_hr_eur=0.9, datacenter_id="EUR-NO-1", transport="https", storage_name="storage"
        )
        kwargs.update(self.claim_kwargs)
        return Claim(pod_id=pod_id, ops=pod, identity={"operation_id": f"op-{self.claims}"}, **kwargs)

    def release(self, claim):
        self.releases += 1
        if self.release_error is not None:
            raise self.release_error
        if not self.leak_after_release:
            self.existing.discard(claim.pod_id)
        pod = claim.ops
        self.volume["__installed__"] = b"1" if pod.installed else b""

    def pod_exists(self, pod_id):
        return pod_id in self.existing


@pytest.fixture
def sources(tmp_path: Path) -> Path:
    root = tmp_path / "sources"
    for directory in rp.SOURCE_DIRS:
        (root / directory).mkdir(parents=True)
        (root / directory / "mod.py").write_text(f"# {directory}\n", encoding="utf-8")
    return root


@pytest.fixture
def inputs(tmp_path: Path):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source-video")
    request = tmp_path / "request.json"
    request.write_text('{"request": 1}', encoding="utf-8")
    asset_map = tmp_path / "asset-map.json"
    asset_map.write_text(json.dumps({"source": str(source)}), encoding="utf-8")
    return request, asset_map


def _wrapper(provider, sources, **kwargs):
    kwargs.setdefault("verify_fn", lambda out, result: None)
    kwargs.setdefault("ffprobe_fn", lambda path: None)
    return RemotePlacement(
        provider, limits=Limits(poll_seconds=0), source_root=sources, sleep=lambda s: None, **kwargs
    )


def _go(wrapper, inputs, out):
    request, asset_map = inputs
    return wrapper.run(request=request, asset_map=asset_map, out=out)


def _provider(sources, volume=None):
    digest = rp.source_digest(sources)
    return FakeProvider(f"{rp.BOOTSTRAP_ROOT}/{digest}/ok", volume)


def test_happy_path_runs_all_seven_steps(tmp_path, sources, inputs):
    provider = _provider(sources)
    seen = {}
    wrapper = _wrapper(
        provider, sources,
        verify_fn=lambda out, result: seen.setdefault("verify", dict(result)),
        ffprobe_fn=lambda path: seen.setdefault("ffprobe", path),
    )
    out = tmp_path / "out"
    result = _go(wrapper, inputs, out)

    # 1 claim: https, storage, EUR-NO-1, caps passed through, exactly once
    assert provider.claims == 1
    assert (provider.spec.transport, provider.spec.storage_name, provider.spec.datacenter_id) == (
        "https", "storage", "EUR-NO-1")
    assert provider.spec.max_spend_eur == rp.DEFAULT_MAX_SPEND_EUR
    assert provider.spec.deadline_seconds == rp.DEFAULT_DEADLINE_SECONDS
    steps = {s["step"]: s["status"] for s in result["remote_steps"]}
    assert steps == {"claim": "done", "bootstrap": "done", "install": "done", "upload": "done",
                     "job": "done", "download": "done", "verify": "done", "release": "done"}
    pod = provider.pods[0]
    # 2 bootstrap ran the script after uploading archives; 3 install deadline 14400
    assert any("pod_bootstrap.sh" in c for c in pod.job_log)
    assert any(r.endswith("sources.tar.gz") for r in pod.put_log)
    install = next(c for c in pod.job_log if "engines install" in c)
    assert "--placement local" in install and "--deadline 14400" in install
    assert f"--root {rp.ENGINE_ROOT}" in install
    # 4 inputs uploaded
    assert any(r.endswith("request.json") for r in pod.put_log)
    assert any("000-source.mp4" in r for r in pod.put_log)
    # 5 the transform ran as one detached job, with the single canonical argv
    transform = next(c for c in pod.job_log if "action run h3_av.transform" in c)
    assert "--placement local" in transform and "python -m" not in transform
    # 6 downloads land in the local out dir; the undeclared intermediate is not pulled
    assert Path(result["candidate"]).read_bytes() == b"candidate-bytes"
    assert Path(result["candidate"]).is_relative_to(out.resolve())
    assert (out / "08-final-receipt.json").is_file() and (out / "operation-state.json").is_file()
    assert not (out / "04-run").exists()
    assert seen["verify"]["candidate"] == result["candidate"] and seen["ffprobe"] == Path(result["candidate"])
    # 7 released, GET 404, shape = local keys + placement and identity
    assert provider.releases == 1 and provider.existing == set()
    assert result["placement"] == "runpod"
    assert result["pod"]["pod_id"] == "pod-1" and result["pod"]["claim"] == {"operation_id": "op-1"}
    assert result["pod"]["released"] is True
    assert result["status"] == "candidate_verified" and result["final_receipt"].endswith("08-final-receipt.json")


def test_leave_running_skips_release_on_success_only(tmp_path, sources, inputs):
    provider = _provider(sources)
    result = _go(_wrapper(provider, sources, leave_running=True), inputs, tmp_path / "out")
    assert provider.releases == 0 and provider.existing == {"pod-1"}
    assert result["pod"]["released"] is False and result["pod"]["leave_running"] is True

    failing = _provider(sources)
    failing.claim_kwargs = {"datacenter_id": "US-TX-3"}
    with pytest.raises(RemotePlacementError):
        _go(_wrapper(failing, sources, leave_running=True), inputs, tmp_path / "out2")
    assert failing.releases == 1 and failing.existing == set()


def test_second_run_skips_bootstrap_and_install(tmp_path, sources, inputs):
    volume: dict[str, bytes] = {}
    first = _provider(sources, volume)
    _go(_wrapper(first, sources), inputs, tmp_path / "out1")
    second = _provider(sources, volume)
    result = _go(_wrapper(second, sources), inputs, tmp_path / "out2")

    steps = {s["step"]: s["status"] for s in result["remote_steps"]}
    assert steps["bootstrap"] == "skipped" and steps["install"] == "skipped"
    pod = second.pods[0]
    assert not any("pod_bootstrap.sh" in c or "engines install" in c for c in pod.job_log)
    assert not any(r.endswith("sources.tar.gz") for r in pod.put_log)
    assert len(pod.job_log) == 1  # only the transform
    assert second.releases == 1


STEP_FAILURES = {
    "claim_contract": lambda p, pod: p.claim_kwargs.update(datacenter_id="US-TX-3"),
    "claim_spend": lambda p, pod: p.claim_kwargs.update(cost_per_hr_eur=50.0),
    "claim_cost_unknown": lambda p, pod: p.claim_kwargs.update(cost_per_hr_eur=None),
    "claim_transport": lambda p, pod: p.claim_kwargs.update(transport="ssh"),
    "bootstrap": lambda p, pod: pod.fail_jobs.add("pod_bootstrap.sh"),
    "install": lambda p, pod: pod.fail_jobs.add("engines install"),
    "upload": lambda p, pod: setattr(pod, "corrupt_put", True),
    "job": lambda p, pod: pod.fail_jobs.add("action run h3_av.transform"),
    "download": lambda p, pod: setattr(pod, "corrupt_get", "candidate.media"),
}


@pytest.mark.parametrize("step", sorted(STEP_FAILURES))
def test_release_on_error_at_each_step(tmp_path, sources, inputs, step):
    provider = _provider(sources)
    original_claim = provider.claim

    def claim(spec):
        claimed = original_claim(spec)
        STEP_FAILURES[step](provider, claimed.ops)
        return claimed

    if step.startswith("claim_"):
        STEP_FAILURES[step](provider, None)
    else:
        provider.claim = claim
    with pytest.raises(RemotePlacementError):
        _go(_wrapper(provider, sources), inputs, tmp_path / "out")
    assert provider.claims == 1
    assert provider.releases == 1
    assert provider.existing == set()


def test_release_on_verify_error_and_ffprobe_error(tmp_path, sources, inputs):
    def boom(*_a):
        raise ValueError("verify refused")

    for kwargs in ({"verify_fn": boom}, {"ffprobe_fn": boom}):
        provider = _provider(sources)
        with pytest.raises(ValueError, match="verify refused"):
            _go(_wrapper(provider, sources, **kwargs), inputs, tmp_path / "out")
        assert provider.claims == 1 and provider.releases == 1 and provider.existing == set()


def test_release_on_deadline(tmp_path, sources, inputs):
    provider = _provider(sources)
    ticks = iter(range(0, 10_000_000, 4000))
    wrapper = RemotePlacement(
        provider, limits=Limits(deadline_seconds=10, poll_seconds=0), source_root=sources,
        verify_fn=lambda *_: None, ffprobe_fn=lambda *_: None,
        clock=lambda: float(next(ticks)), sleep=lambda s: None,
    )
    with pytest.raises(RemotePlacementError, match="deadline"):
        _go(wrapper, inputs, tmp_path / "out")
    assert provider.releases == 1 and provider.existing == set()


def test_running_job_is_cancelled_before_release(tmp_path, sources, inputs):
    provider = _provider(sources)
    wrapper = _wrapper(provider, sources)

    def stuck(self, job_id):
        raise ConnectionError("lost connection")

    original_claim = provider.claim

    def claim(spec):
        claimed = original_claim(spec)
        claimed.ops.job_poll = lambda job_id: (_ for _ in ()).throw(ConnectionError("lost"))
        return claimed

    provider.claim = claim
    with pytest.raises(ConnectionError):
        _go(wrapper, inputs, tmp_path / "out")
    assert provider.pods[0].cancelled and provider.releases == 1


def test_sha_mismatch_on_download_refuses(tmp_path, sources, inputs):
    provider = _provider(sources)
    original_claim = provider.claim

    def claim(spec):
        claimed = original_claim(spec)
        claimed.ops.corrupt_get = "candidate.media"
        return claimed

    provider.claim = claim
    out = tmp_path / "out"
    with pytest.raises(DigestMismatch, match="candidate.media"):
        _go(_wrapper(provider, sources), inputs, out)
    assert not (out / "05-compose" / "candidate.media").exists()  # refused copy removed
    assert provider.releases == 1


def test_manifest_digest_mismatch_refuses(tmp_path, sources, inputs):
    provider = _provider(sources)
    original_claim = provider.claim

    def claim(spec):
        claimed = original_claim(spec)
        claimed.ops.corrupt_manifest = True
        return claimed

    provider.claim = claim
    with pytest.raises(DigestMismatch, match="manifest"):
        _go(_wrapper(provider, sources), inputs, tmp_path / "out")
    assert provider.releases == 1


def test_candidate_must_match_composition_receipt(tmp_path):
    candidate = tmp_path / "c.media"
    candidate.write_bytes(b"x")
    composition = tmp_path / "c.json"
    composition.write_text(json.dumps({"candidate": {"sha256": "0" * 64}}))
    with pytest.raises(DigestMismatch):
        RemotePlacement._check_receipt_digests(
            {"composition": str(composition), "candidate": str(candidate)})


def test_never_a_second_claim(tmp_path, sources, inputs):
    provider = _provider(sources)
    wrapper = _wrapper(provider, sources)
    _go(wrapper, inputs, tmp_path / "out1")
    with pytest.raises(RemotePlacementError, match="second pod claim"):
        _go(wrapper, inputs, tmp_path / "out2")
    assert provider.claims == 1


def test_claim_failure_is_not_retried(tmp_path, sources, inputs):
    provider = _provider(sources)
    provider.claim_error = RuntimeError("no capacity")
    with pytest.raises(RuntimeError, match="no capacity"):
        _go(_wrapper(provider, sources), inputs, tmp_path / "out")
    assert provider.claims == 1 and provider.releases == 0


def test_partial_claim_custody_is_released(tmp_path, sources, inputs):
    provider = _provider(sources)
    error = RuntimeError("readiness failed after allocation")
    error.claim = Claim(pod_id="pod-x", ops=FakePod({}, "m"), identity={})
    provider.claim_error = error
    provider.existing.add("pod-x")
    with pytest.raises(RuntimeError):
        _go(_wrapper(provider, sources), inputs, tmp_path / "out")
    assert provider.claims == 1 and provider.releases == 1 and provider.existing == set()


def test_release_that_leaves_the_pod_alive_is_an_error(tmp_path, sources, inputs):
    provider = _provider(sources)
    provider.leak_after_release = True
    with pytest.raises(RemotePlacementError, match="still exists"):
        _go(_wrapper(provider, sources), inputs, tmp_path / "out")
    assert provider.claims == 1 and provider.releases == 1


def test_release_failure_does_not_hide_the_original_error(tmp_path, sources, inputs):
    provider = _provider(sources)
    provider.claim_kwargs = {"datacenter_id": "US-TX-3"}
    provider.release_error = RuntimeError("provider 500")
    with pytest.raises(RemotePlacementError, match="expected EUR-NO-1") as info:
        _go(_wrapper(provider, sources), inputs, tmp_path / "out")
    assert any("provider 500" in note for note in info.value.__notes__)
    assert provider.claims == 1


def test_preflight_refuses_before_any_claim(tmp_path, sources, inputs):
    provider = _provider(sources)
    request, asset_map = inputs
    wrapper = _wrapper(provider, sources)
    with pytest.raises(RemotePlacementError):
        wrapper.run(request=request, asset_map=asset_map, out=tmp_path / "o", cleanup_receipt=tmp_path / "r.json")
    bad = tmp_path / "exec.json"
    bad.write_text(json.dumps({"target": {"kind": "runpod", "pod_id": "p"}}))
    with pytest.raises(RemotePlacementError):
        wrapper.run(request=request, asset_map=asset_map, out=tmp_path / "o", execution_request=bad)
    assert provider.claims == 0


def test_command_is_defined_in_one_swappable_place(tmp_path, sources, inputs):
    provider = _provider(sources)
    calls = []

    def custom(remote_args, remote_out, **kwargs):
        calls.append((dict(remote_args), remote_out))
        return ["/opt/astrid/bin/astrid", "action", "run", "h3_av.transform", "--out", remote_out, "--swapped"]

    _go(_wrapper(provider, sources, command_argv=custom), inputs, tmp_path / "out")
    assert calls and any("--swapped" in c for c in provider.pods[0].job_log)
    default = rp.clip_command_argv({"request": "/r", "asset_map": "/a"}, "/o")
    assert default[:4] == [f"{rp.POD_VENV}/bin/astrid", "action", "run", "h3_av.transform"]
    assert "-m" not in default and "--placement" in default and default[default.index("--placement") + 1] == "local"


def test_source_digest_matches_pod_bootstrap_script(sources):
    shell = f"""
set -eu; cd {sources}
for d in {' '.join(rp.SOURCE_DIRS)}; do
  find "$d" -type f -not -path '*/.git/*' -not -name '*.pyc' -not -path '*/__pycache__/*' -print0
done | LC_ALL=C sort -z | xargs -0 sha256sum | sha256sum | cut -c1-16
"""
    expected = subprocess.run(["bash", "-c", shell], capture_output=True, text=True, check=True).stdout.strip()
    assert rp.source_digest(sources) == expected


def test_manifest_script_matches_what_the_wrapper_checks(tmp_path):
    out = tmp_path / "out"
    (out / "sub").mkdir(parents=True)
    (out / "a.json").write_bytes(b"aa")
    (out / "sub" / "b.bin").write_bytes(b"bbb")
    script = tmp_path / "manifest.py"
    script.write_text(rp._MANIFEST_SCRIPT, encoding="utf-8")
    printed = subprocess.run(
        [sys.executable, "-I", str(script), str(out)], capture_output=True, text=True, check=True
    ).stdout.strip()
    manifest = out / rp.MANIFEST_NAME
    assert printed == rp.MANIFEST_MARK + _sha(manifest.read_bytes())
    files = json.loads(manifest.read_text())["files"]
    assert files["sub/b.bin"] == {"sha256": _sha(b"bbb"), "size": 3}
    assert rp.MANIFEST_NAME not in files


def test_transform_runpod_calls_the_wrapper(tmp_path, monkeypatch):
    from astrid.packs.h3_av.actions.transform import run as transform

    request = tmp_path / "request.json"
    request.write_text("{}")
    asset_map = tmp_path / "assets.json"
    asset_map.write_text("{}")
    seen = {}

    def fake(args):
        seen["placement"] = args.placement
        return {"placement": "runpod", "status": "candidate_verified"}

    monkeypatch.setattr(transform, "_run_remote_placement", fake)
    args = transform.build_parser().parse_args(
        ["--request", str(request), "--asset-map", str(asset_map), "--out", str(tmp_path / "o"),
         "--placement", "runpod"])
    assert transform.run_transform(args)["placement"] == "runpod"
    assert seen["placement"] == "runpod"
    assert not hasattr(transform, "PlacementNotImplemented")


def test_run_remote_placement_reads_caps_from_env(tmp_path, sources, inputs):
    provider = _provider(sources)
    request, asset_map = inputs
    args = SimpleNamespace(request=request, asset_map=asset_map, out=tmp_path / "o", project=None,
                           editorial_approved=False, execution_request=None, cleanup_receipt=None)
    env = {"ASTRID_RUNPOD_MAX_SPEND_EUR": "3.5", "ASTRID_RUNPOD_DEADLINE_SECONDS": "7200",
           "ASTRID_RUNPOD_SOURCE_ROOT": str(sources), "ASTRID_RUNPOD_LEAVE_RUNNING": "1"}
    result = rp.run_remote_placement(
        args, provider=provider, env=env, verify_fn=lambda *_: None, ffprobe_fn=lambda *_: None,
        sleep=lambda s: None)
    assert (provider.spec.max_spend_eur, provider.spec.deadline_seconds) == (3.5, 7200)
    assert result["pod"]["leave_running"] is True and provider.releases == 0
