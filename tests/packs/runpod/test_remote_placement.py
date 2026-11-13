"""W1: the RunPod placement wrapper, with the provider and HTTPS stubbed (CPU only)."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from astrid.packs.runpod import remote_placement as rp
from astrid.packs.runpod.remote_placement import (
    Claim,
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
        self.listed: list[str] = ["cpu-image"]  # entries the pod's runtime lists as ready
        # The pod's one Runtime (T5f): container-disk state, not the volume.
        self.pod_state = "absent"  # absent | ready; an active job makes it pending
        self.active_job: dict | None = None
        self.template = False
        self.resets: list[dict] = []

    # --- agent surface
    def exec(self, command, timeout=600):
        self.exec_log.append(command)
        if "from astrid.core.gateway.doctor_bundle import redact_log" in command:
            import shlex

            argv = shlex.split(command)
            source, destination = argv[-2:]
            if source not in self.files:
                return 1, "", "missing"
            from astrid.core.gateway.doctor_bundle import redact_log

            safe = redact_log(self.files[source].decode("utf-8", errors="replace"))
            self.files[destination] = safe.encode()
            return 0, "", ""
        if command.startswith("rm -f "):
            self.files.pop(command.split(" ", 2)[2].strip("'"), None)
            return 0, "", ""
        if "O_NOFOLLOW" in command and "1048577" in command:
            import base64, shlex
            path = shlex.split(command)[-1]
            if path not in self.files or len(self.files[path]) > 1024 * 1024:
                return 1, "", "invalid receipt"
            return 0, base64.b64encode(self.files[path]).decode() + "\n", ""
        if "pod-state:" in command:
            state = "pending" if self.active_job is not None else self.pod_state
            return 0, f"pod-state:{state}\n", ""
        if command.startswith("rm -rf "):
            gone = command.split(" ", 2)[2].strip("'")
            for path in [p for p in self.files if p.startswith(gone + "/")]:
                del self.files[path]
            return 0, "", ""
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
        if "generate list --json" in command:
            return 0, json.dumps([{"id": entry, "ready": True} for entry in self.listed]), ""
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
        stdout, stderr = "", "boom" if rc else ""
        if "pod_reset reset" in command:
            stdout = self._reset(command, ok=rc == 0)
        elif " generate " in command and "--placement local" in command and rc == 0:
            rc, stderr = self._enter(command, directory)
        if rc == 0:
            if "pod_bootstrap.sh" in command:
                self.files[self.marker] = b"ok"
            if "engines install" in command:
                self.installed = True
            if "action run h3_av.transform" in command:
                stdout = self._transform(command)
            if " generate " in command and "--placement local" in command:
                stdout = self._generate(command)
        self.files[f"{directory}/stdout"] = stdout.encode()
        self.files[f"{directory}/stderr"] = stderr.encode()
        self.jobs[job_id] = rc
        return {"id": job_id, "dir": directory}

    def job_poll(self, job_id):
        return {"id": job_id, "state": "exited", "exit_code": self.jobs[job_id]}

    def _reset(self, command: str, *, ok: bool) -> str:
        """``pod_reset reset``: template (first) or restore+replace, sweep, ready.json."""
        import shlex as _shlex

        argv = _shlex.split(command)
        dirs = [argv[argv.index(flag) + 1] for flag in ("--run-dir", "--job-dir") if flag in argv]
        if not dirs and self.active_job is not None:
            # A redo at the claim: pod_reset reads the folders the stuck job recorded.
            dirs = [self.active_job[key] for key in ("run_dir", "job_dir") if self.active_job.get(key)]
        self.resets.append({"dirs": dirs, "ok": ok, "active": self.active_job})
        if not ok:
            self.pod_state = "absent"
            return json.dumps({"reset": "failed", "error": "RuntimeError: replace refused"}) + "\n"
        for gone in dirs:
            for path in [p for p in self.files if p.startswith(gone + "/")]:
                del self.files[path]
        taken = not self.template
        self.template, self.pod_state, self.active_job = True, "ready", None
        return json.dumps({"reset": "ok", "removed": len(dirs), "template": "taken" if taken else "restored"}) + "\n"

    def _enter(self, command: str, directory: str) -> tuple[int, str]:
        """The generation command's own gate: ready, no pending reset, entry listed."""
        import shlex as _shlex

        assert "ensure_runtime" not in command and "astrid-runtime" not in command, "a warm job ran up/connect"
        assert "export ASTRID_PREPARED_CONNECTION=/root/astrid/prepared-connection.json" in command
        assert "flock -s 9" in command and "ASTRID_RUNTIME_DOOR_PORT=0" in command
        if self.pod_state != "ready" or self.active_job is not None:
            return 75, "the pod is not ready: a reset is pending"
        active = next(row for row in command.splitlines()
                      if row.startswith("/opt/astrid/bin/python -c ") and "active-job.json" in row)
        argv = _shlex.split(active)
        assert argv[4] == '$(dirname $0)', argv[4]  # the job's own agent folder (bash "$d/command.sh")
        self.active_job = {**json.loads(argv[3]), "job_dir": directory}
        line = next(row for row in command.splitlines() if "--placement local" in row)
        entry = _shlex.split(line.split(" > ")[0])[2]
        if entry != "workflow" and entry not in self.listed:
            return 3, f"entry {entry!r} is not listed by the pod's runtime"
        return 0, ""

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


def _fake_generate(pod: "FakePod", command: str) -> str:
    """Fake pod-side local run: output tree, result line and digest manifest."""
    import shlex as _shlex

    line = next(row for row in command.splitlines() if "--placement local" in row)
    argv = _shlex.split(line.split(" > ")[0])
    root = argv[argv.index("--job") + 1].rsplit("/", 1)[0]
    out = f"{root}/out"
    pod.files[f"{root}/progress.json"] = b'{"phase": "fetch"}'
    tree = {f"{out}/artifacts/cpu.png": b"png-bytes", f"{out}/manifest.json": b'{"outputs": []}'}
    pod.files.update(tree)
    files = {path[len(out) + 1:]: {"sha256": _sha(data), "size": len(data)} for path, data in tree.items()}
    body = json.dumps({"schema_version": 1, "files": files}, sort_keys=True, indent=2) + "\n"
    pod.files[f"{out}/{rp.MANIFEST_NAME}"] = body.encode()
    return f"{rp.RESULT_MARK}{json.dumps({'status': 'succeeded'})}\n{rp.MANIFEST_MARK}{_sha(body.encode())}\n"


FakePod._generate = _fake_generate


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
def sources(tmp_path: Path, monkeypatch) -> Path:
    root = tmp_path / "sources"
    for directory in rp.SOURCE_DIRS:
        (root / directory).mkdir(parents=True)
        (root / directory / "mod.py").write_text(f"# {directory}\n", encoding="utf-8")
    (root / "src/astrid-agent/runtime.lock.json").write_text('{"source_commit": "1"}', encoding="utf-8")
    from astrid.packs.runpod import managed_sources
    monkeypatch.setattr(managed_sources, "_pins", lambda: {
        d: (f"https://example.invalid/{Path(d).name}", f"{i}" * 40) for i, d in enumerate(rp.SOURCE_DIRS)})
    return root


def _wrapper(provider, sources, **kwargs):
    return RemotePlacement(
        provider, limits=Limits(poll_seconds=0), source_root=sources, sleep=lambda s: None, **kwargs
    )


def _provider(sources, volume=None):
    digest = rp.source_digest(sources)
    return FakeProvider(f"{rp.BOOTSTRAP_ROOT}/{digest}/ok", volume)


def test_bootstrap_archive_and_actual_extract_command_agree_on_source_layout(tmp_path, sources):
    provider = _provider(sources)
    pod = FakePod({}, provider.marker)
    _wrapper(provider, sources).prepare_claimed(Claim("pod-1", pod, {}), out=tmp_path / "out", deadline_seconds=120)
    archive = next(data for path, data in pod.files.items() if path.endswith("sources.tar.gz"))
    work = tmp_path / "unpack"
    work.mkdir()
    (work / "sources.tar.gz").write_bytes(archive)
    # Execute the production extraction command with only the expensive
    # bootstrap body replaced by assertions over the CPU source fixture.
    checks = "\n".join("test -f " + directory + "/mod.py" for directory in rp.SOURCE_DIRS)
    (work / "pod_bootstrap.sh").write_text("set -eu\n" + checks + "\n")
    command = next(command for command in pod.job_log if "pod_bootstrap.sh" in command)
    result = subprocess.run(["bash", "-c", "tar -xzf" + command.split("tar -xzf", 1)[1]],
                            cwd=work, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_prepare_claimed_reuses_bootstrap_install_without_claim_or_transform(tmp_path, sources):
    provider = _provider(sources)
    claim = provider.claim(rp.ClaimSpec())
    wrapper = _wrapper(provider, sources)
    state = wrapper.prepare_claimed(claim, out=tmp_path / "prepare", deadline_seconds=120)
    assert state.claim is claim
    assert provider.claims == 1
    assert provider.releases == 0
    assert [step["step"] for step in state.steps] == ["bootstrap", "install"]
    assert all("h3_av.transform" not in command for command in claim.ops.job_log)
    assert not any("/01-prepare/" in path for path in claim.ops.files)


def test_changed_source_bootstraps_even_when_previous_product_marker_exists(tmp_path, sources):
    old = _provider(sources)
    files = {old.marker: b"ok"}
    (sources / "src/astrid-agent/mod.py").write_text("# new source install payload\n")
    provider = _provider(sources, volume=files)
    assert provider.marker != old.marker
    claim = provider.claim(rp.ClaimSpec())
    _wrapper(provider, sources).prepare_claimed(claim, out=tmp_path / "prepare", deadline_seconds=120)
    assert any("pod_bootstrap.sh" in command for command in claim.ops.job_log)
    assert files[provider.marker] == files[old.marker] == b"ok"


def test_prepare_claimed_install_is_model_free(tmp_path, sources):
    """Plan 32 T5h: a claim installs the engine only; no model flag, no model fetch on the pod."""
    provider = _provider(sources)
    claim = provider.claim(rp.ClaimSpec())
    state = _wrapper(provider, sources).prepare_claimed(claim, out=tmp_path / "prepare", deadline_seconds=120)
    install = next(c for c in claim.ops.job_log if "engines install" in c)
    assert "--model-cache" not in install
    assert not any("fetch" in c or "download" in c for c in claim.ops.job_log)
    assert [step["step"] for step in state.steps] == ["bootstrap", "install"]


@pytest.mark.parametrize("config_present", [True, False], ids=["all-artifacts", "missing-config"])
def test_install_failure_keeps_redacted_artifacts_and_indexes_registry_config(tmp_path, sources, config_present):
    from astrid.core.generation.engine_custody import engine_log_path

    provider = _provider(sources)
    claim = provider.claim(rp.ClaimSpec())
    pod = claim.ops
    pod.fail_jobs = {"engines install"}
    # Use the production path helpers and realistic JSON/text secrets. The
    # absent registry config must still appear as a missing row in the index.
    root, engine = rp.ENGINE_ROOT, rp.ENGINE_ID
    registry = f"{root}/runtime/out/sessions/{engine}"
    pod.files[engine_log_path(root, engine)] = b"Bearer abc-secret\n"
    pod.files[f"{registry}/daemon.log"] = b"https://user:pass@example.invalid/path\n"
    pod.files[f"{root}/.astrid-engine/session/session.json"] = b'{"owner_id":"private-owner","state":"ready"}'
    if config_present:
        pod.files[f"{registry}/config.json"] = b'{"token":"config-secret"}'
    # The job directory is allocated by the fake at launch; source paths are
    # on the shared pod volume and are read by the remote redaction command.
    with pytest.raises(RemotePlacementError):
        _wrapper(provider, sources).prepare_claimed(claim, out=tmp_path / "out", deadline_seconds=120)

    index_path = tmp_path / "out/logs/index.json"
    index = json.loads(index_path.read_text())
    rows = {row["name"]: row for row in index["logs"]}
    expected = {f"engine.install.{name}" for name in
                ("engine-log", "supervisor-log", "session-marker", "registry-config")}
    assert expected <= rows.keys()
    config_row = rows["engine.install.registry-config"]
    assert config_row["missing"] is (not config_present)
    assert "path" not in config_row
    files = [path for path in (tmp_path / "out/logs").iterdir() if path.is_file() and path.name != "index.json"]
    assert len(files) >= (6 if config_present else 5)  # stdout, stderr, and engine artifacts
    contents = "\n".join(path.read_text() for path in files)
    assert "<redacted>" in contents
    assert "abc-secret" not in contents and "user:pass" not in contents and "private-owner" not in contents
    assert "config-secret" not in contents


def test_volume_is_sized_by_the_job_manifest_rows_not_the_model_lock():
    installation = rp.RunPodInstallation(compatibility={})
    rows = ({"name": "a", "size": 100}, {"name": "b", "size": 23})
    assert installation.model_bytes(rows) == 123
    assert installation.model_bytes(()) == 1  # a warm job needs no model bytes


def test_ordinary_connector_uses_only_the_verified_claim_and_existing_https_ops(monkeypatch):
    from tests.packs.runpod.test_worker_preparation import _claim_handle

    record = _claim_handle(
        operation_id="a" * 32, request_name="astrid-product-" + "a" * 32,
        name="astrid-product-" + "a" * 32, transport="https", target_identity_verified=True,
        volume_mount_path="/workspace", https_token_file="/fake/agent.token",
    )
    reads = []
    monkeypatch.setattr(rp, "_read_token", lambda value: reads.append(value) or "fake-only")
    claim = rp.connect_claimed_for_preparation(record)
    assert reads == [record]
    assert isinstance(claim.ops, rp._HttpsOps)
    assert claim.pod_id == record["pod_id"]
    assert claim.identity["provider_account_ref"] == record["provider_account_ref"]
    assert claim.handle == record
    with pytest.raises(RemotePlacementError, match="journal-created"):
        rp.connect_claimed_for_preparation({**record, "request_name": "another-broker"})
    assert reads == [record]


@pytest.mark.parametrize("seconds", [0, -1, float("inf"), float("nan")])
def test_prepare_claimed_refuses_missing_paid_lifetime_without_provider_use(tmp_path, seconds):
    provider = FakeProvider("unused")
    wrapper = _wrapper(provider, None)
    with pytest.raises(RemotePlacementError, match="remaining paid lifetime"):
        wrapper.prepare_claimed(None, out=tmp_path / "prepare", deadline_seconds=seconds)
    assert provider.claims == provider.releases == 0


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


def test_source_archive_carries_git_and_the_one_identity_manifest_the_verifier_reads(tmp_path, sources):
    import tarfile
    (sources / "src/astrid-runtime/.git").mkdir()
    (sources / "src/astrid-runtime/.git/HEAD").write_text("ref")
    (sources / "src/astrid-runtime/.gitignore").write_text("x")
    (sources / "src/astrid-runtime/mod.pyc").write_bytes(b"x")
    archive = rp._build_source_archive(sources, tmp_path / "a.tar.gz")
    with tarfile.open(archive) as tar:
        names = tar.getnames()
        manifest = json.loads(tar.extractfile("src/.pod-source.json").read())
    assert "src/astrid-runtime/.git/HEAD" in names and "src/astrid-runtime/.gitignore" in names
    assert "src/astrid-runtime/mod.pyc" not in names
    # pod_source_identity._verify requires exactly these keys, resolved against root "src".
    assert set(manifest) == {"heads", "runtime_lock_sha256"}
    assert manifest["heads"] == {Path(d).name: f"{i}" * 40 for i, d in enumerate(rp.SOURCE_DIRS)}
    assert set(manifest["heads"]) == {"astrid-agent", "astrid-runtime", "runpod-lifecycle", "VibeComfy"}
    assert manifest["runtime_lock_sha256"] == rp.sha256_file(sources / "src/astrid-agent/runtime.lock.json")


def test_engine_custody_root_is_not_on_the_workspace_fuse_mount():
    """Engine custody needs an owner-only (0700) directory; /workspace is a FUSE mount that ignores mkdir's
    mode, so a custody root under it fails at the first helper with unsafe_path/helper-identity
    (attempt 06). Every pod-side engine root the wrapper hands out must live off that mount.
    """
    from pathlib import PurePosixPath

    def on_workspace(path: str) -> bool:
        return PurePosixPath(path).is_relative_to("/workspace")

    assert not on_workspace(rp.ENGINE_ROOT)
    assert f"ASTRID_ENGINE_ROOT:-{rp.ENGINE_ROOT}}}" in rp.POD_BOOTSTRAP_SCRIPT
    for engine in ("comfyui",):
        assert not on_workspace(f"{rp.ENGINE_BASE}/{engine}")
    assert not on_workspace(rp.RunPodInstallation.root)
