"""Read-only attachment to the canonical installer registry.

Registry files are compatibility evidence, never process-control authority.
Remote file observations alone cannot establish a live composite. A journal
claim plus a verified preparation receipt permits a fresh target-local check
through the existing preparation transport. Local observation reuses
VibeComfy's actual verifier.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
from dataclasses import asdict
from pathlib import Path, PurePosixPath
import time

from astrid.core.generation import engine_recipe, engine_runner
from astrid.core.generation._engine_target_files import _TargetFiles, _FILE_FLAGS, _check
from astrid.core.generation.engine_platforms import LinuxPlatformAdapter

from .worker_session import ManagedSessionError, _DIGEST, _path, _process

ENGINE = "vibecomfy-h3"
PLATFORM = "linux-x86_64-cu130"
_LIMIT = 1024 * 1024
_NAMES = ("config.json", "launch.json", "pid", "comfy_pid",
          "comfy_process_start_identity", "url", "source_revision", "source_content_digest", "daemon.log")


def _json(raw):
    def pairs(items):
        value = {}
        for key, item in items:
            if key in value:
                raise ValueError("duplicate field")
            value[key] = item
        return value
    value = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs,
                       parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    if not isinstance(value, dict):
        raise ValueError("object required")
    return value


class InstalledVibeComfySessionTransport:
    """Shared file validation; subclasses supply actual observation boundaries."""

    def __init__(self, *, platform=PLATFORM):
        self.platform = platform

    def read_installed(self, runtime_root):
        """Observe receipt/config identity before freezing a deployment reference.

        The root comes from P2's existing candidate/runtime selection. It is
        never taken from a remote receipt or an ambient environment variable.
        This method is observational and does not assert live process custody.
        """
        deadline = time.monotonic() + 20
        try:
            root = str(PurePosixPath(_path(runtime_root, "installed runtime root")).parent)
            if runtime_root != root + "/runtime":
                raise ManagedSessionError("installed runtime root differs from canonical layout")
            recipe = engine_recipe.load_recipe(ENGINE, self.platform)
            adapter = LinuxPlatformAdapter(self.platform)
            plan = engine_runner.plan_install(recipe, root=root, placement="local")
            # Self-start executes the canonical local installer on the pod.
            receipt_path = root + "/.astrid-engine/install.json"
            receipt_bytes = self._read(root, receipt_path, deadline)
            receipt = engine_runner._parse_receipt(_json(receipt_bytes), plan)
            if receipt.status != "installed":
                raise ManagedSessionError("canonical installed receipt is required")
            registry = runtime_root + "/out/sessions/" + ENGINE
            files = {name: self._read(root, registry + "/" + name, deadline) for name in _NAMES}
            config, marker = _json(files["config.json"]), _json(files["launch.json"])
            launch = adapter.start_request(recipe, root, deadline=deadline)
            launch_value = {"argv": list(launch.argv), "cwd": launch.cwd, "env": dict(launch.env)}
            launch_sha = engine_runner._sha(launch_value)
            facts = next(s.facts for s in receipt.steps if s.kind == "start")
            if (facts.get("owner_id") != marker.get("launch_token")
                    or facts.get("launch_sha256") != launch_sha):
                raise ManagedSessionError("installed receipt and registry launch owner differ")
            output = launch.cwd + "/output"
            if "--output-directory" in launch.argv:
                output = launch.argv[launch.argv.index("--output-directory") + 1]
            expected = {
                "runtime_root": runtime_root, "cwd": launch.cwd, "port": recipe.start.port,
                "warm_policy": "never", "base_directory": launch.cwd,
                "models_root": root + "/models", "models_root_normalized": root + "/models",
                "output_directory": output, "locality": "managed_local_server",
                "server_log_path": registry + "/comfy.log",
            }
            if config != expected:
                raise ManagedSessionError("installed registry config differs from canonical recipe")
            url = "http://127.0.0.1:" + str(recipe.start.port)
            daemon = {"pid": marker.get("pid"), "birth_id": marker.get("process_start_identity")}
            comfy = {"pid": marker.get("comfy_pid"), "birth_id": marker.get("comfy_process_start_identity")}
            _process(daemon, "installed supervisor")
            _process(comfy, "installed child")
            token = marker.get("launch_token")
            source = next(s for s in recipe.sources if s.name == "vibecomfy")
            revision = files["source_revision"].decode().strip()
            content = files["source_content_digest"].decode().strip()
            if (files["pid"].decode().strip() != str(daemon["pid"])
                    or files["comfy_pid"].decode().strip() != str(comfy["pid"])
                    or daemon["pid"] == comfy["pid"]
                    or files["comfy_process_start_identity"].decode().strip() != comfy["birth_id"]
                    or marker.get("url") != url or files["url"].decode().strip() != url
                    or not isinstance(token, str) or not token
                    or revision != source.pin.revision or not _DIGEST.fullmatch(content)):
                raise ManagedSessionError("installed registry process, launch or source evidence differs")
            # Catch changes during the multi-file observation without treating a
            # collection of individually valid downloads as an atomic snapshot.
            for name in _NAMES:
                if self._read(root, registry + "/" + name, deadline) != files[name]:
                    raise ManagedSessionError("installed registry changed during observation")
            if self._read(root, receipt_path, deadline) != receipt_bytes:
                raise ManagedSessionError("installed receipt changed during observation")
            _check(deadline)
            return {
                "session_ref": ENGINE, "runtime_root": runtime_root,
                "python_executable": adapter.venv_python(root + "/" + next(
                    e.relative_path for e in recipe.environments if e.name == recipe.primary_environment)),
                "vibecomfy_root": root + "/" + source.relative_path,
                "config": config,
                "config_digest": "sha256:" + hashlib.sha256(files["config.json"]).hexdigest(),
                "output_root": output, "url": url, "daemon": daemon, "comfy": comfy,
                "launch_token_digest": "sha256:" + hashlib.sha256(token.encode()).hexdigest(),
                "source_revision": revision, "source_content_digest": content,
                "gpu_qualified": False,
            }
        except ManagedSessionError:
            raise
        except Exception as exc:
            raise ManagedSessionError("installed session evidence could not be verified") from exc

    def status(self, selection):
        if selection.session_ref != ENGINE:
            raise ManagedSessionError("installed attachment selected a different session")
        value = self.read_installed(selection.runtime_root)
        self._verify_live(selection, value)
        # Re-read after live verification; no snapshot authorizes later control.
        if self.read_installed(selection.runtime_root) != value:
            raise ManagedSessionError("installed session changed during live verification")
        return {**value, "account_ref": selection.account_ref, "pod_id": selection.pod_id,
                "ownership_verified": True}

    def start(self, selection):
        raise ManagedSessionError("installer-owned session cannot use legacy start")

    def stop(self, selection, receipt):
        raise ManagedSessionError("installer-owned session requires original supervisor stop")

    def confirm_stopped(self, selection, receipt):
        raise ManagedSessionError("installer-owned session stop is not observable through this attachment")


class LocalInstalledVibeComfySessionTransport(InstalledVibeComfySessionTransport):
    """Target-local read/verify only; retains no signal or provider authority."""

    def _read(self, root, path, deadline):
        files = _TargetFiles(root)
        parts = files._relative(path)
        with files._directory(parts[:-1], deadline) as parent:
            fd = os.open(parts[-1], _FILE_FLAGS, dir_fd=parent)
            try:
                before = files._regular(fd)
                if before.st_size > _LIMIT:
                    raise ManagedSessionError("installed evidence exceeds observation limit")
                data = bytearray()
                while True:
                    _check(deadline)
                    block = os.read(fd, min(65536, _LIMIT + 1 - len(data)))
                    if not block:
                        break
                    data.extend(block)
                    if len(data) > _LIMIT:
                        raise ManagedSessionError("installed evidence exceeds observation limit")
                after = files._regular(fd)
                if (len(data) != before.st_size or (before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                        != (after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
                    raise ManagedSessionError("installed evidence changed during read")
                return bytes(data)
            finally:
                os.close(fd)

    def _verify_live(self, selection, value):
        from vibecomfy.runtime import session as runtime

        registry = Path(selection.runtime_root, "out/sessions", ENGINE)
        if (Path(runtime.find_repo_root()) != Path(selection.vibecomfy_root)
                or runtime.current_source_revision() != value["source_revision"]
                or runtime.current_source_content_digest() != value["source_content_digest"]):
            raise ManagedSessionError("installed source differs from live selected VibeComfy checkout")
        if (runtime._process_start_identity(value["daemon"]["pid"]) != value["daemon"]["birth_id"]
                or runtime._process_start_identity(value["comfy"]["pid"]) != value["comfy"]["birth_id"]
                or not runtime._session_ready(registry)
                or not runtime._session_composite_ownership_verified(registry, value["daemon"]["pid"])):
            raise ManagedSessionError("installed session live composite ownership is unverified")


class RunPodInstalledVibeComfySessionTransport(InstalledVibeComfySessionTransport):
    """Installed attachment under exact product claim and preparation custody.

    Plain read-only attachments still cannot establish live ownership. Only
    the prepared owner can bind a durable P2 receipt before using its existing
    transport to observe the actual process/source/listener composite.
    """

    def __init__(self, pod, *, platform=PLATFORM, preparation_transport=None):
        super().__init__(platform=platform)
        self.pod = pod
        self.preparation_transport = preparation_transport
        self._prepared = None
        self._installed_source = None

    def bind_preparation(self, *, claim_handle_path, reference, staging):
        from .prepared_task import _claim_target, _read_mapping
        from .worker_staging import _SCHEMA, _claim_identity, _public_result

        try:
            path = Path(claim_handle_path)
            claim = _read_mapping(path, label="prepared claim")
            identity = _claim_identity(path, reference.effective_target["provider_account_ref"])
            journal_path = Path(staging["journal_path"])
            journal = _read_mapping(journal_path, label="preparation receipt")
            ordinary = claim.get("ordinary_preparation", {})
            if (self.preparation_transport is None
                    or getattr(self.preparation_transport, "pod", None) is not self.pod
                    or claim.get("acquisition_mode") != "allocate"
                    or claim.get("release_authorized") is not True
                    or claim.get("target_identity_verified") is not True
                    or claim.get("request_name") != "astrid-product-" + identity["operation_id"]
                    or claim.get("name") != claim["request_name"]
                    or claim.get("transport") != "https"
                    or ordinary.get("schema") != "astrid.runpod.ordinary-preparation.v1"
                    or ordinary.get("state") != "prepared" or ordinary.get("engine") != "comfyui"
                    or ordinary.get("claim_operation_id") != identity["operation_id"]
                    or ordinary.get("pod_id") != identity["pod_id"]
                    or ordinary.get("provider_account_ref") != identity["provider_account_ref"]
                    or str(self.pod.id) != identity["pod_id"]
                    or reference.effective_target != _claim_target(claim)
                    or journal.get("schema") != _SCHEMA
                    or journal.get("identity") != identity
                    or journal.get("status") != "cpu_ready"
                    or _public_result(journal, journal_path) != staging
                    or staging.get("gpu_qualified") is not False
                    or staging.get("worker_qualified") is not False):
                raise ValueError()
            custody = {key: claim.get(key) for key in (
                "operation_id", "provider_account_ref", "pod_id", "network_volume_id",
                "network_volume_name", "network_volume_size_gb", "network_volume_datacenter_id",
                "volume_mount_path", "gpu_type", "worker_image", "request_name", "name",
                "transport", "https_token_file", "acquisition_mode", "release_authorized",
                "target_identity_verified", "ordinary_preparation",
            )}
            prepared = (path, custody, journal_path, json.loads(json.dumps(staging)))
            if self._prepared is not None and self._prepared != prepared:
                raise ValueError()
            self._prepared = prepared
        except Exception:
            raise ManagedSessionError("journal claim and preparation receipt do not verify this installed attachment") from None

    def select_prepared_source(self, staged_root, installed_root):
        """Permit distinct locations only for a receipt-bound source comparison.

        The actual bytes of both trees are checked on the target by status;
        this selection alone never grants attachment or process authority.
        """
        if (self._prepared is None
                or self._prepared[3]["cpu_readiness"]["effective_paths"].get("vibecomfy_root") != staged_root):
            raise ManagedSessionError("installed source has no matching preparation receipt")
        selected = _path(installed_root, "installed VibeComfy checkout")
        if self._installed_source not in (None, selected):
            raise ManagedSessionError("installed VibeComfy checkout changed")
        self._installed_source = selected
        return selected

    def _prepared_custody(self, selection):
        from .prepared_task import _read_mapping
        from .worker_staging import _claim_identity, _public_result

        if self._prepared is None:
            raise ManagedSessionError(
                "read-only pod agent lacks live installer session composite verification; "
                "verified journal claim and preparation receipt are required"
            )
        path, custody, journal_path, staging = self._prepared
        claim = _read_mapping(path, label="prepared claim")
        journal = _read_mapping(journal_path, label="preparation receipt")
        facts = staging["cpu_readiness"]
        paths = facts["effective_paths"]
        if (claim.get("state") != "claimed"
                or any(claim.get(key) != value for key, value in custody.items())
                or journal.get("identity") != _claim_identity(path, selection.account_ref)
                or journal.get("status") != "cpu_ready"
                or _public_result(journal, journal_path) != staging
                or selection.account_ref != custody["provider_account_ref"]
                or selection.pod_id != custody["pod_id"]
                or facts.get("status") != "cpu_ready" or facts.get("gpu_qualified") is not False
                or facts.get("python_executable") != selection.python_executable
                or selection.vibecomfy_root != (self._installed_source or paths.get("vibecomfy_root"))
                or paths.get("comfy_root") != selection.comfy_root
                or paths.get("model_root") != selection.model_root
                or paths.get("output_root") != selection.output_root):
            raise ManagedSessionError("prepared installed-session custody changed")
        return facts

    def _read(self, root, path, deadline):
        _TargetFiles(root)._relative(path)
        if getattr(self.pod.config, "transport", None) != "https":
            raise ManagedSessionError("installed session observation requires original HTTPS claim")
        client = self.pod.https_transport()
        try:
            # These registry/receipt files fit one existing JSON chunk. Refuse
            # larger files instead of an unbounded download or Range fallback.
            _check(deadline)
            # Use the existing authenticated HTTP client without _request's
            # independent retry budget: all files share this caller deadline.
            response = client.http.request(
                "POST", "/download-chunk", json={"path": path, "offset": 0},
                timeout=min(20, max(0.001, deadline - time.monotonic())),
            )
            if not response.is_success:
                raise ManagedSessionError("installed evidence JSON download was rejected")
            data = response.json()
            raw = base64.b64decode(data["data"], validate=True)
            if (type(data["offset"]) is not int or data["offset"] != 0
                    or type(data["total"]) is not int or data["total"] != len(raw)
                    or len(raw) > _LIMIT or not isinstance(data["etag"], str) or not data["etag"]
                    or data["sha256"] != hashlib.sha256(raw).hexdigest()):
                raise ManagedSessionError("installed evidence JSON download identity differs")
            _check(deadline)
            return raw
        finally:
            client.disconnect()

    def _verify_live(self, selection, value):
        if str(self.pod.id) != selection.pod_id:
            raise ManagedSessionError("installed observation belongs to a different original pod")
        try:
            facts = self._prepared_custody(selection)
            from .worker_owner import _run_sync

            request = {"selection": asdict(selection), "platform": self.platform,
                       "astrid_root": facts["effective_paths"]["astrid_root"],
                       "staged_vibecomfy_root": facts["effective_paths"]["vibecomfy_root"],
                       "vibecomfy_tree": facts["tree_facts"]["vibecomfy_root"]}
            observed = _run_sync(self.preparation_transport._run_json_program(
                _PREPARED_INSTALLED_OBSERVATION, [json.dumps(request, separators=(",", ":"))],
                python=selection.python_executable, timeout_seconds=20,
            ))
            expected = {**value, "account_ref": selection.account_ref,
                        "pod_id": selection.pod_id, "ownership_verified": True}
            if observed != expected:
                raise ManagedSessionError("prepared installed-session live composite differs")
            self._prepared_custody(selection)
        except ManagedSessionError:
            raise
        except Exception:
            raise ManagedSessionError("prepared installed-session live composite could not be verified") from None


_PREPARED_INSTALLED_OBSERVATION = r'''import json,sys
from pathlib import Path
request=json.loads(sys.argv[1]); selected=request["selection"]
sys.path[:0]=[request["astrid_root"],selected["vibecomfy_root"]]
from astrid.packs.runpod.installed_session import LocalInstalledVibeComfySessionTransport
from astrid.packs.runpod.worker_session import ManagedSessionSelection
from astrid.packs.runpod.worker_staging import _tree_inventory
def check_sources():
 expected=request["vibecomfy_tree"]
 for value in (request["staged_vibecomfy_root"],selected["vibecomfy_root"]):
  root=Path(value)
  if root.resolve(strict=True)!=root: raise RuntimeError("prepared source path is aliased")
  digest,count=_tree_inventory(root)
  if {"tree_digest":digest,"file_count":count}!=expected:
   raise RuntimeError("installed and staged VibeComfy sources differ from preparation receipt")
check_sources()
transport=LocalInstalledVibeComfySessionTransport(platform=request["platform"])
observed=transport.status(ManagedSessionSelection(**selected))
check_sources()
print(json.dumps(observed,sort_keys=True,separators=(",",":")))
'''
