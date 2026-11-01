"""Real local-machine activation for the public H3 CPU journey.

Reuse the parked-host/OS-inspector acceptance path exercised by
test_generic_host_activation_acceptance. Only the Comfy server is synthetic;
Runtime issues the placement, credential generation and executor incarnation.
"""
from __future__ import annotations

import hashlib
import json
import os
import socket
import sys
import time
from dataclasses import replace
from pathlib import Path

from astrid.core.execution.generic_host import (
    RuntimeProtocolClient,
    source_checkout_closure_digest,
    source_checkout_digest,
)
from astrid.core.gateway.dispatch import compose_profile_handoff
from astrid.sdk import host_bootstrap
from tests.helpers.runtime import runtime_checkout


class VerifiedH3Host:
    def __init__(self, root, daemon, pack_roots):
        self.root, self.daemon = Path(root), daemon
        self.pack_roots = tuple(pack_roots)
        self.target = {"kind": "machine", "id": socket.gethostname()}
        self.preparer = None
        self.qualification = None
        self.capacity = 2
        self.output_root = self.root / "host-outputs"
        self.output_root.mkdir()
        self._claim = daemon.service.claim_delegated_child

        def observe_claim(*args, **kwargs):
            try:
                result = self._claim(*args, **kwargs)
                with daemon.service.store._mutex:
                    capacity = daemon.service.store.conn.execute(
                        "SELECT max_concurrency FROM executors WHERE id='astrid-pack-host'",
                    ).fetchone()[0]
                    active = daemon.service.store.conn.execute(
                        "SELECT COUNT(*) FROM tasks WHERE executor_id='astrid-pack-host' AND status='running'",
                    ).fetchone()[0]
                assert capacity == self.capacity
                assert active == 2, "the parent and exact delegated child must hold both slots"
                binding = result["execution_binding"]
                (self.root / ("h3-child-binding-" + result["task_id"] + ".json")).write_text(json.dumps({
                    "registered_capacity": capacity, "running_tasks": active,
                    **{key: binding[key] for key in (
                        "actual_target", "verification", "executor_incarnation", "executor_id",
                    )},
                }, indent=2))
                return result
            except Exception as exc:
                with daemon.service.store._mutex:
                    executor = daemon.service.store.conn.execute(
                        "SELECT max_concurrency FROM executors WHERE id='astrid-pack-host'",
                    ).fetchone()
                    active = daemon.service.store.conn.execute(
                        "SELECT COUNT(*) FROM tasks WHERE executor_id='astrid-pack-host' AND status='running'",
                    ).fetchone()[0]
                details = getattr(exc, "details", None)
                (self.root / "h3-delegated-claim-error.json").write_text(json.dumps({
                    "type": type(exc).__name__, "message": str(exc),
                    "waiting_reason": details.get("waiting_reason") if isinstance(details, dict) else None,
                    "registered_capacity": executor[0] if executor else None, "running_tasks": active,
                }, indent=2))
                raise

        daemon.service.claim_delegated_child = observe_claim

    def activate(self, task_id):
        from runtime_protocol.remote_worker_activation import QualifiedRemoteWorkerLauncher
        from runtime_protocol.remote_worker_deployment import (
            ArtifactReference,
            DeploymentReference,
            deployment_binding_from_task,
            project_launch,
        )

        daemon, service = self.daemon, self.daemon.service
        source = Path(__file__).resolve().parents[2]
        task = service._task_resource(service.store.get_task(task_id))
        binding = deployment_binding_from_task(task)
        assert binding.placement.effective_target == self.target
        profile_path = self.root / "h3-readiness.json"
        profile = json.loads(profile_path.read_text())
        session_path = self.root / "placement-session.json"
        session_path.write_text(json.dumps({"session_ref": "h3-cpu-placement", "capacity": self.capacity}))
        boot = daemon.support_root / "boot-manifest.json"
        handoff = compose_profile_handoff(boot, support_root=daemon.support_root)
        executable = Path(sys.executable).resolve()
        import vibecomfy

        artifacts = tuple(sorted((
            ArtifactReference(name, path, host_bootstrap._host_artifact_digest(path))
            for name, path in (
                ("python", executable), ("runtime", runtime_checkout()),
                ("vibecomfy", Path(vibecomfy.__file__).resolve().parents[1]),
            )
        ), key=lambda item: item.name))
        health_client = RuntimeProtocolClient(daemon.endpoint, daemon.token)
        health = health_client.health()
        reference = DeploymentReference(
            deployment_id="h3-cpu-parent", revision="h3-cpu-parent-1", task_id=task_id,
            run_id=binding.admission_identity.run_id,
            target_ref="machine:" + self.target["id"], effective_target_ref="machine:" + self.target["id"],
            executable=next(item for item in artifacts if item.name == "python"), dependency_closure=artifacts,
            source_closure_digest="sha256:" + source_checkout_closure_digest(source),
            data_root=daemon.support_root.parent, support_root=daemon.support_root, runtime_endpoint=daemon.endpoint,
            runtime_instance_id=daemon.instance_id, runtime_epoch=health.runtime_epoch,
            runtime_schema_digest=health.schema_digest, model_root=Path(profile["launch"]["model_root"]["path"]),
            capacity=self.capacity, session_ref="h3-cpu-placement",
            session_config_digest="sha256:" + hashlib.sha256(session_path.read_bytes()).hexdigest(),
            output_root=self.output_root, credential_ref=str(daemon.worker_credential_path),
            executor_id=host_bootstrap.PACK_HOST_ACTOR, boot_manifest_path=boot,
            boot_manifest_hash="sha256:" + handoff["sha256"], readiness_profile_path=profile_path,
            readiness_profile_hash="sha256:" + hashlib.sha256(profile_path.read_bytes()).hexdigest(),
            source_checkout=source, source_checkout_digest=source_checkout_digest(source),
            pack_roots=self.pack_roots, ready_file=daemon.support_root / "h3-host.ready.json",
            admission_identity=binding.admission_identity, capability_identity=binding.capability_identity,
            input_bindings=binding.input_bindings, original_target=self.target, effective_target=self.target,
            execution_target=self.target,
        )
        # Carry the existing CPU Comfy session through the real launch seam.
        # None of these values supplies placement or activation authority.
        names = (
            "PATH", "HOME", "TMPDIR", "PYTHONDONTWRITEBYTECODE",
            "ASTRID_C11_SCHEMA_PATH", "ASTRID_C11_EXPECTED_PATH", "ASTRID_C11_EVIDENCE_PATH",
            "ASTRID_C11_SESSION_DIR", "ASTRID_VIBECOMFY_CHECKOUT",
            "ASTRID_VIBECOMFY_CANDIDATE_KIND", "ASTRID_VIBECOMFY_CANDIDATE_REVISION",
            "ASTRID_VIBECOMFY_CANDIDATE_CONTENT_DIGEST", "ASTRID_VIBECOMFY_ATTESTED_REVISION",
            "ASTRID_VIBECOMFY_ATTESTED_CONTENT_DIGEST",
        )
        environment = {name: os.environ[name] for name in names if name in os.environ}

        def launch(selected):
            projected = project_launch(selected)
            return replace(projected, env_items=tuple(sorted({**projected.env(), **environment}.items())))

        # The launch projection supplied by the Runtime launcher is consumed
        # by the preparer. Keep its selected fixture environment explicit.
        class Preparer(host_bootstrap._ParkedPackHostPreparer):
            def prepare(self, selected):
                return super().prepare(selected.reference)

        self.preparer = Preparer(launch, timeout_seconds=30)

        def runtime_health():
            return {**vars(health_client.health()), "runtime_session_id": service.runtime_session_id}

        def executor_observer(executor_id):
            with service.store._mutex:
                row = service.store.conn.execute("SELECT * FROM executors WHERE id=?", (executor_id,)).fetchone()
                return service.store._executor_result(row) if row is not None else None

        inspector = host_bootstrap._ParkedPackHostInspector(
            runtime_health, session_config_path=session_path, executor_observer=executor_observer,
        )
        owner = {"actor": "owner", "scopes": ["admin"]}
        launcher = QualifiedRemoteWorkerLauncher(
            runtime=service, credentials=None, preparer=self.preparer, inspector=inspector,
            credential_control=lambda task_id, body: daemon.remote_credential_control(task_id, body, identity=owner),
            scopes=host_bootstrap.PACK_HOST_SCOPES,
        )
        parked = launcher.park(reference, target=self.target)
        self.qualification = launcher.activate(task, reference, parked)
        assert service._latest_remote_activation(task_id) == self.qualification
        (self.root / "h3-activation.json").write_text(json.dumps({
            "qualification": self.qualification, "observation": parked.observation,
        }, indent=2))
        deadline = time.monotonic() + 10
        while True:
            with service.store._mutex:
                claimed = service.store.execution_binding(task_id)
            if isinstance(claimed, dict) and claimed.get("actual_target") is not None:
                break
            assert time.monotonic() < deadline, "activated host did not bind the H3 parent"
            time.sleep(0.02)
        assert claimed["actual_target"] == self.target
        assert claimed["verification"] == {
            "method": "credential_claim", "verified": True,
            "evidence_digest": self.qualification["evidence_digest"],
        }
        assert claimed["executor_incarnation"] == self.qualification["executor_incarnation"]
        (self.root / "h3-verified-parent-binding.json").write_text(json.dumps({
            key: claimed[key] for key in ("actual_target", "verification", "executor_incarnation", "executor_id")
        }, indent=2))

    def receipts(self):
        """Read host settlement evidence after successful scratch cleanup."""
        deadline = time.monotonic() + 5
        while True:
            service = self.daemon.service
            with service.store._mutex:
                parent = service.store.get_task(self.qualification["task_id"])
                family = [parent, *service.store.delegated_children(parent["id"], parent["attempt_id"])]
                # Runtime retains the host's result/process evidence even
                # after the host deletes its ephemeral result.json spool.
                rows = [service._task_resource(row) for row in family]
                rows = [{**row, "status": row["state"]} for row in rows]
            if rows[0]["status"] in {"succeeded", "failed", "cancelled"} or time.monotonic() >= deadline:
                (self.root / "h3-terminal-receipts.json").write_text(json.dumps(rows, indent=2))
                return rows
            time.sleep(0.02)

    def close(self):
        try:
            if self.preparer is not None and self.preparer._handle is not None:
                self.preparer.abort(self.preparer._handle)
        finally:
            self.daemon.service.claim_delegated_child = self._claim
